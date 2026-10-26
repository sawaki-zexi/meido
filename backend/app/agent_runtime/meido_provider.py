from __future__ import annotations

import inspect
import json
from dataclasses import asdict
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime, timezone
from typing import cast

from ..models import Message, ModelConfiguration, Role
from ..ai.model_adapter import ModelAdapter, ModelStreamError, ModelTextDelta, ModelToolCall
from ..ai.model_types import (
    ModelAssistantMessage as RichAssistantMessage,
    ModelCost as RichCost,
    ModelImageBlock as RichImageBlock,
    ModelContentBlock as RichContentBlock,
    ModelMessage as RichMessage,
    ModelResponseMetadata as RichResponseMetadata,
    ModelStreamDone,
    ModelStreamError as RichStreamError,
    ModelTextBlock as RichTextBlock,
    ModelTextDelta as RichTextDelta,
    ModelThinkingBlock as RichThinkingBlock,
    ModelToolCallBlock as RichToolCallBlock,
    ModelToolCallEnd as RichToolCallEnd,
    ModelToolResultMessage as RichToolResultMessage,
    ModelSystemMessage as RichSystemMessage,
    ModelUserMessage as RichUserMessage,
    ModelUsage as RichUsage,
)
from ..browser_use.safety import sanitize_browser_arguments, sanitize_browser_text
from .provider import AssistantDoneEvent, CancellationToken, ProviderErrorEvent, ProviderEvent, ProviderStartEvent, TextDeltaEvent, ToolCallEndEvent
from .types import AssistantMessage, ToolCall, UserMessage, ToolResultMessage


def _metadata_count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _metadata_cost(value: object) -> RichCost:
    if not isinstance(value, Mapping):
        return RichCost()

    def number(*keys: str) -> float:
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool) and candidate >= 0:
                return float(candidate)
        return 0.0

    return RichCost(
        input=number("input", "input_cost"),
        output=number("output", "output_cost"),
        cache_read=number("cache_read", "cacheRead", "cache_read_cost"),
        cache_write=number("cache_write", "cacheWrite", "cache_write_cost"),
        total=number("total", "total_cost"),
    )


def _rich_usage(value: object) -> RichUsage:
    if isinstance(value, RichUsage):
        return value
    if not isinstance(value, Mapping):
        return RichUsage()
    return RichUsage(
        input_tokens=_metadata_count(value.get("input_tokens", value.get("inputTokens", value.get("input")))),
        output_tokens=_metadata_count(value.get("output_tokens", value.get("outputTokens", value.get("output")))),
        cache_read_tokens=_metadata_count(value.get("cache_read_tokens", value.get("cacheReadTokens", value.get("cacheRead")))),
        cache_write_tokens=_metadata_count(value.get("cache_write_tokens", value.get("cacheWriteTokens", value.get("cacheWrite")))),
        reasoning_tokens=_metadata_count(value.get("reasoning_tokens", value.get("reasoningTokens", value.get("reasoning")))),
        cost=_metadata_cost(value.get("cost")),
    )


def _content_block_from_metadata(value: object) -> RichContentBlock | None:
    if not isinstance(value, Mapping):
        return None
    block_type = value.get("type")
    if block_type == "text":
        text = value.get("text")
        signature = value.get("text_signature")
        if not isinstance(text, str) or (signature is not None and not isinstance(signature, str)):
            return None
        return RichTextBlock(text, text_signature=signature)
    if block_type == "thinking":
        text = value.get("text", "")
        signature = value.get("signature")
        redacted = value.get("redacted", False)
        if not isinstance(text, str) or (signature is not None and not isinstance(signature, str)) or not isinstance(redacted, bool):
            return None
        return RichThinkingBlock(text, signature, redacted=redacted)
    if block_type == "image":
        media_type = value.get("media_type")
        data_base64 = value.get("data_base64")
        detail = value.get("detail", "auto")
        if (
            not isinstance(media_type, str)
            or not isinstance(data_base64, str)
            or not isinstance(detail, str)
            or detail not in {"low", "high", "auto"}
        ):
            return None
        return RichImageBlock(media_type, data_base64, detail)
    if block_type == "tool_call":
        call_id = value.get("id")
        name = value.get("name")
        arguments = value.get("arguments")
        thought_signature = value.get("thought_signature")
        namespace = value.get("namespace")
        if (
            not isinstance(call_id, str)
            or not isinstance(name, str)
            or not isinstance(arguments, Mapping)
            or (thought_signature is not None and not isinstance(thought_signature, str))
            or (namespace is not None and not isinstance(namespace, str))
        ):
            return None
        safe_arguments = dict(arguments)
        if name.startswith("agent_browser_"):
            safe_arguments = sanitize_browser_arguments(name, safe_arguments)
        return RichToolCallBlock(
            call_id,
            name,
            safe_arguments,
            thought_signature=thought_signature,
            namespace=namespace,
        )
    return None


def _content_block_metadata(block: RichContentBlock) -> dict[str, object]:
    value = asdict(block)
    if isinstance(block, RichToolCallBlock) and block.name.startswith("agent_browser_"):
        value["arguments"] = sanitize_browser_arguments(block.name, block.arguments)
    return value


def _content_blocks_from_metadata(value: object) -> tuple[RichContentBlock, ...] | None:
    if not isinstance(value, list):
        return None
    blocks = tuple(_content_block_from_metadata(item) for item in value)
    if any(block is None for block in blocks):
        return None
    return tuple(block for block in blocks if block is not None)


def _provider_call_ids(message: AssistantMessage) -> dict[str, str]:
    raw = message.metadata.get("provider_tool_call_ids", {})
    mapping = raw if isinstance(raw, Mapping) else {}
    return {call.id: value if isinstance(value := mapping.get(call.id), str) and value else call.id
            for call in message.tool_calls}


class MeidoProvider:
    """Adapt Meido's model boundary to the provider-neutral Runtime contract."""

    def __init__(
        self,
        adapter: ModelAdapter,
        role: Role,
        configuration: ModelConfiguration | None,
        memory_context: str = "",
        capability_prompt: str = "",
        *, structured_without_tools: bool = False, max_response_tokens: int | None = None,
        context_blocks=None, context_safety_margin: int = 0,
    ) -> None:
        self.adapter = adapter
        self.role = role
        self.configuration = configuration
        self.memory_context = memory_context
        self.capability_prompt = capability_prompt
        self._tool_prompt = ""
        self.structured_without_tools = structured_without_tools
        self.max_response_tokens = max_response_tokens
        self.context_blocks = context_blocks
        self.context_safety_margin = context_safety_margin
        self._request_context = ""

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[object],
        tools: Sequence[Mapping[str, object]],
        signal: CancellationToken,
        session_id: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        del model, session_id
        self._set_tool_prompt(tools)
        self._refresh_request_context(system, messages, tools)
        rich = getattr(self.adapter, "stream_rich_messages", None)
        if rich is not None:
            return self._stream_rich_structured(system, messages, tools, signal, rich)
        if tools or self.structured_without_tools:
            structured = getattr(self.adapter, "stream_structured_messages", None)
            if structured is None:
                # Built-in tools are part of the default catalog, so a legacy
                # text-only adapter can still serve ordinary chat.  It cannot
                # execute a tool call, but rejecting the whole turn merely
                # because the catalog is non-empty would make the default
                # tools unusable for adapters that intentionally expose only
                # ``stream_reply`` (and would also break attachment fallback).
                return self._stream(messages, signal)
            return self._stream_structured(system, messages, tools, signal, structured)
        return self._stream(messages, signal)

    def _set_tool_prompt(self, tools):
        names = []
        for tool in tools:
            function = tool.get("function")
            if isinstance(function, Mapping) and isinstance(function.get("name"), str):
                names.append(function["name"])
        self._tool_prompt = "\n".join((
            "[本次运行工具]",
            "当前提供给模型的工具：" + ("、".join(names) if names else "无"),
            "仅以上名称可在本次运行调用。不要根据旧对话或其他角色的工具清单声称具备能力。",
            "工具可见不等于外部依赖已验证；实际执行状态以工具结果为准。",
        ))

    def estimate_request_tokens(self, system, messages, tools):
        self._set_tool_prompt(tools)
        self._refresh_request_context(system, messages, tools)
        return self._estimate_rendered(system, messages, tools)

    def _refresh_request_context(self, system, messages, tools):
        self._request_context = ""
        if self.context_blocks is None:
            return
        capacity = (self.configuration.contextWindowTokens if self.configuration else None) or 32768
        available = capacity - (self.max_response_tokens or 2048) - self.context_safety_margin
        for block in self.context_blocks():
            previous = self._request_context
            self._request_context = "\n\n".join(filter(None, (previous, block)))
            if self._estimate_rendered(system, messages, tools) >= available:
                self._request_context = previous

    def _estimate_rendered(self, system, messages, tools):
        from .context_provider import estimate_tokens
        # Use the same rendered system/memory/capability/messages as the actual rich request.
        rendered = []
        image_tokens = 0
        for message in self._rich_messages(system, messages):
            value = asdict(message)
            content = []
            for block in message.content:
                if isinstance(block, RichImageBlock):
                    image_tokens += 85 if block.detail == 'low' else 4096
                    content.append({'type': 'image'})
                else:
                    content.append(asdict(block))
            value['content'] = content
            rendered.append(value)
        return estimate_tokens({'messages': rendered, 'tools': list(tools)}) + 32 + image_tokens

    def _stream_rich_structured(self, system, messages, tools, signal, rich) -> AsyncIterator[ProviderEvent]:
        async def stream() -> AsyncIterator[ProviderEvent]:
            runtime_messages = self._rich_messages(system, messages)
            yield ProviderStartEvent()
            content = ""
            tool_calls: list[ToolCall] = []
            try:
                try:
                    accepts_cancel_check = "cancel_check" in inspect.signature(rich).parameters
                except (TypeError, ValueError):
                    accepts_cancel_check = False
                options: dict[str, object] = {"cancel_check": signal.raise_if_cancelled} if accepts_cancel_check else {}
                if self.max_response_tokens is not None and "max_tokens" in inspect.signature(rich).parameters:
                    options["max_tokens"] = self.max_response_tokens
                source = rich(runtime_messages, self.configuration, [dict(tool) for tool in tools], **options)
                async for event in source:
                    signal.raise_if_cancelled()
                    if isinstance(event, RichTextDelta):
                        content += event.text
                        yield TextDeltaEvent(event.text)
                    elif isinstance(event, RichToolCallEnd):
                        call = ToolCall(event.call.id, event.call.name, dict(event.call.arguments))
                        tool_calls.append(call)
                        yield ToolCallEndEvent(call)
                    elif isinstance(event, RichStreamError):
                        yield ProviderErrorEvent(event.error)
                        return
                    elif isinstance(event, ModelStreamDone):
                        final_content = "".join(
                            block.text for block in event.message.content if isinstance(block, RichTextBlock)
                        )
                        final_calls = tuple(
                            ToolCall(block.id, block.name, dict(block.arguments))
                            for block in event.message.content
                            if isinstance(block, RichToolCallBlock)
                        )
                        if final_content and not content:
                            content = final_content
                        if final_calls:
                            tool_calls = list(final_calls)
                        stop_reason = event.message.metadata.stop_reason or ("tool_use" if tool_calls else "stop")
                        if stop_reason == "error":
                            yield ProviderErrorEvent(event.message.metadata.error_message or "模型调用失败")
                            return
                        response_metadata = asdict(event.message.metadata)
                        if event.message.timestamp is not None:
                            response_metadata["timestamp"] = event.message.timestamp
                        response_metadata["thinking"] = [
                            {
                                "text": block.text,
                                "signature": block.signature,
                            }
                            for block in event.message.content
                            if isinstance(block, RichThinkingBlock)
                        ]
                        response_metadata["content_blocks"] = [
                            _content_block_metadata(block) for block in event.message.content
                        ]
                        yield AssistantDoneEvent(AssistantMessage(
                            content=content,
                            tool_calls=tuple(tool_calls),
                            stop_reason="tool_use" if tool_calls else stop_reason,
                            metadata=response_metadata,
                            timestamp=event.message.timestamp or event.message.metadata.timestamp,
                        ))
                        return
            except Exception as error:
                if isinstance(error, RuntimeError) and str(error) == "agent run cancelled":
                    raise
                yield ProviderErrorEvent(str(error))
        return stream()

    def _stream_structured(self, system, messages, tools, signal, structured) -> AsyncIterator[ProviderEvent]:
        async def stream() -> AsyncIterator[ProviderEvent]:
            runtime_messages = self._structured_messages(system, messages)
            yield ProviderStartEvent()
            content = ""
            tool_calls: list[ToolCall] = []
            try:
                options = ({'max_tokens': self.max_response_tokens} if self.max_response_tokens is not None
                           and 'max_tokens' in inspect.signature(structured).parameters else {})
                source = structured(runtime_messages, self.configuration, [dict(tool) for tool in tools], **options)
                async for event in source:
                    signal.raise_if_cancelled()
                    if isinstance(event, ModelTextDelta):
                        content += event.text
                        yield TextDeltaEvent(event.text)
                    elif isinstance(event, ModelToolCall):
                        call = ToolCall(event.id, event.name, event.arguments)
                        tool_calls.append(call)
                        yield ToolCallEndEvent(call)
                    elif isinstance(event, ModelStreamError):
                        yield ProviderErrorEvent(event.error)
                        return
                yield AssistantDoneEvent(AssistantMessage(
                    content=content,
                    tool_calls=tuple(tool_calls),
                    stop_reason="tool_use" if tool_calls else "stop",
                ))
            except Exception as error:
                if isinstance(error, RuntimeError) and str(error) == "agent run cancelled":
                    raise
                yield ProviderErrorEvent(str(error))
        return stream()

    def _structured_messages(self, system: str, messages: Sequence[object]) -> list[dict[str, object]]:
        prompt = self._structured_system_prompt(system)
        result: list[dict[str, object]] = [{"role": "system", "content": prompt}]
        provider_ids: dict[str, str] = {}
        for message in messages:
            if isinstance(message, UserMessage):
                result.append({"role": "user", "content": _user_content(message)})
            elif isinstance(message, AssistantMessage):
                provider_ids.update(_provider_call_ids(message))
                item: dict[str, object] = {"role": "assistant", "content": message.content or None}
                if message.tool_calls:
                    item["tool_calls"] = [
                        {
                            "id": provider_ids.get(call.id, call.id),
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(
                                    sanitize_browser_arguments(call.name, call.arguments),
                                    ensure_ascii=False,
                                ),
                            },
                        }
                        for call in message.tool_calls
                    ]
                result.append(item)
            elif isinstance(message, ToolResultMessage):
                tool_content = message.content
                if message.tool_name.startswith("agent_browser_"):
                    tool_content, _ = sanitize_browser_text(tool_content)
                content: object = tool_content
                if message.images:
                    multimodal_content: list[dict[str, object]] = [
                        {"type": "text", "text": tool_content},
                    ]
                    multimodal_content.extend(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{image.media_type};base64,{image.data_base64}",
                                "detail": image.detail,
                            },
                        }
                        for image in message.images
                    )
                    content = multimodal_content
                result.append({
                    "role": "tool",
                    "tool_call_id": provider_ids.get(message.tool_call_id, message.tool_call_id),
                    "content": content,
                })
        return result

    def _rich_messages(self, system: str, messages: Sequence[object]) -> list[RichMessage]:
        result: list[RichMessage] = [RichSystemMessage((RichTextBlock(self._structured_system_prompt(system)),))]
        provider_ids: dict[str, str] = {}
        for message in messages:
            if isinstance(message, UserMessage):
                blocks: list[object] = [RichTextBlock(message.content)]
                for attachment in message.attachments:
                    if attachment.image is not None:
                        blocks.append(RichImageBlock(
                            attachment.image.media_type,
                            attachment.image.data_base64,
                            attachment.image.detail,
                        ))
                    elif attachment.text:
                        blocks[0] = RichTextBlock(f"{blocks[0].text}\n\n[{attachment.name}]\n{attachment.text}")
                result.append(RichUserMessage(tuple(blocks), timestamp=message.timestamp))
            elif isinstance(message, AssistantMessage):
                provider_ids.update(_provider_call_ids(message))
                metadata = message.metadata if isinstance(message.metadata, Mapping) else {}
                raw_usage = metadata.get("usage")
                usage = raw_usage if isinstance(raw_usage, Mapping) else {}
                blocks: list[object] = []
                raw_thinking = metadata.get("thinking")
                raw_content_blocks = _content_blocks_from_metadata(metadata.get("content_blocks"))
                if raw_content_blocks == () and (message.content or message.tool_calls or raw_thinking):
                    raw_content_blocks = None
                if raw_content_blocks is not None:
                    blocks.extend(raw_content_blocks)
                elif isinstance(raw_thinking, list):
                    for item in raw_thinking:
                        if not isinstance(item, dict):
                            continue
                        text = item.get("text")
                        if isinstance(text, str) and text:
                            signature = item.get("signature")
                            blocks.append(RichThinkingBlock(
                                text,
                                signature if isinstance(signature, str) else None,
                            ))
                if raw_content_blocks is None:
                    if message.content:
                        blocks.append(RichTextBlock(message.content))
                    blocks.extend(
                        RichToolCallBlock(
                            provider_ids.get(call.id, call.id),
                            call.name,
                            sanitize_browser_arguments(call.name, call.arguments),
                        )
                        for call in message.tool_calls
                    )
                timestamp = metadata.get("timestamp", message.timestamp)
                result.append(RichAssistantMessage(
                    tuple(blocks),
                    RichResponseMetadata(
                        provider=metadata.get("provider") if isinstance(metadata.get("provider"), str) else None,
                        api=metadata.get("api") if isinstance(metadata.get("api"), str) else None,
                        model=metadata.get("model") if isinstance(metadata.get("model"), str) else None,
                        response_id=metadata.get("response_id") if isinstance(metadata.get("response_id"), str) else None,
                        usage=_rich_usage(usage),
                        stop_reason=(
                            metadata.get("stop_reason")
                            if isinstance(metadata.get("stop_reason"), str)
                            else message.stop_reason
                        ),
                        error_message=metadata.get("error_message") if isinstance(metadata.get("error_message"), str) else None,
                        timestamp=_metadata_count(timestamp) if isinstance(timestamp, int) and not isinstance(timestamp, bool) else None,
                    ),
                    timestamp=_metadata_count(timestamp) if isinstance(timestamp, int) and not isinstance(timestamp, bool) else None,
                ))
            elif isinstance(message, ToolResultMessage):
                text = message.content
                if message.tool_name.startswith("agent_browser_"):
                    text, _ = sanitize_browser_text(text)
                blocks = [RichTextBlock(text)]
                blocks.extend(
                    RichImageBlock(image.media_type, image.data_base64, image.detail)
                    for image in message.images
                )
                result.append(RichToolResultMessage(
                    content=tuple(blocks),
                    tool_call_id=provider_ids.get(message.tool_call_id, message.tool_call_id),
                    tool_name=message.tool_name,
                    is_error=message.is_error,
                    details=message.details,
                    usage=_rich_usage(message.usage),
                    nested_calls=tuple(
                        RichToolCallBlock(call.id, call.name, sanitize_browser_arguments(call.name, call.arguments))
                        for call in message.nested_calls
                    ),
                    timestamp=message.timestamp,
                ))
        return result

    def _structured_system_prompt(self, system: str) -> str:
        if not system:
            return self.role_system_prompt()
        return "\n\n".join(part for part in (system, self.capability_prompt, self._tool_prompt, self._request_context) if part)

    def role_system_prompt(self) -> str:
        profile = self.role.profile
        return "\n\n".join(part for part in [
            f"你正在扮演 {self.role.name}。",
            f"简介：{self.role.description}" if self.role.description else "",
            f"角色设定：{profile.profile}",
            f"性格：{profile.personality}" if profile.personality else "",
            f"行为规则：{profile.behaviorRules}" if profile.behaviorRules else "",
            f"回复约束：{profile.responseConstraints}" if profile.responseConstraints else "",
            f"称呼用户：{profile.nickname}" if profile.nickname else "",
            self.memory_context,
            self._request_context,
            self.capability_prompt,
            self._tool_prompt,
        ] if part)

    def _combined_memory_context(self) -> str:
        return "\n\n".join(part for part in (self.memory_context, self.capability_prompt, self._request_context) if part)

    def _role_with_context(self) -> Role:
        profile = self.role.profile.model_copy(deep=True)
        profile.profile = "\n\n".join(
            part for part in (profile.profile, self._combined_memory_context()) if part
        )
        return self.role.model_copy(deep=True, update={"profile": profile})

    async def _stream(self, messages: Sequence[object], signal: CancellationToken) -> AsyncIterator[ProviderEvent]:
        if any(isinstance(message, UserMessage) and message.attachments for message in messages):
            async for event in self._stream_multimodal(messages, signal):
                yield event
            return
        history: list[Message] = []
        for message in messages:
            if isinstance(message, UserMessage):
                history.append(_message("user", message.content))
            elif isinstance(message, AssistantMessage):
                history.append(_message("assistant", message.content))
            elif isinstance(message, ToolResultMessage):
                history.append(_message("tool", message.content))
        stream_reply = self.adapter.stream_reply
        try:
            accepts_memory = "memory_context" in inspect.signature(stream_reply).parameters
        except (TypeError, ValueError):
            accepts_memory = False
        yield ProviderStartEvent()
        role = self.role if accepts_memory or not (self.memory_context or self.capability_prompt) else self._role_with_context()
        options = {'memory_context': self._combined_memory_context()} if accepts_memory else {}
        if self.max_response_tokens is not None and 'max_tokens' in inspect.signature(stream_reply).parameters:
            options['max_tokens'] = self.max_response_tokens
        source: object = stream_reply(role, history, self.configuration, **options)
        content = ""
        async for delta in cast(AsyncIterator[str], source):
            signal.raise_if_cancelled()
            content += delta
            yield TextDeltaEvent(delta)
        yield AssistantDoneEvent(AssistantMessage(content=content))

    async def _stream_multimodal(self, messages: Sequence[object], signal: CancellationToken) -> AsyncIterator[ProviderEvent]:
        prompt = self.role_system_prompt()
        wire_messages: list[dict[str, object]] = [{"role": "system", "content": prompt}]
        for message in messages:
            if isinstance(message, UserMessage):
                wire_messages.append({"role": "user", "content": _user_content(message)})
            elif isinstance(message, AssistantMessage):
                wire_messages.append({"role": "assistant", "content": message.content})
            elif isinstance(message, ToolResultMessage):
                wire_messages.append({"role": "tool", "content": message.content, "tool_call_id": message.tool_call_id})
        yield ProviderStartEvent()
        content = ""
        try:
            options = ({'max_tokens': self.max_response_tokens} if self.max_response_tokens is not None
                       and 'max_tokens' in inspect.signature(self.adapter.stream_messages).parameters else {})
            source = self.adapter.stream_messages(wire_messages, self.configuration, **options)
            async for delta in source:
                signal.raise_if_cancelled()
                content += delta
                yield TextDeltaEvent(delta)
            yield AssistantDoneEvent(AssistantMessage(content=content))
        except Exception as error:
            yield ProviderErrorEvent(str(error))


def _message(role: str, content: str) -> Message:
    return Message(
        id="runtime",
        sessionKey="runtime",
        sequence=0,
        role=role,
        content=content,
        status="completed",
        createdAt=datetime.now(timezone.utc),
    )


def _user_content(message: UserMessage) -> str | list[dict[str, object]]:
    if not message.attachments:
        return message.content
    parts: list[dict[str, object]] = [{"type": "text", "text": message.content}]
    for attachment in message.attachments:
        if attachment.image is not None:
            parts.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{attachment.image.media_type};base64,{attachment.image.data_base64}",
                    "detail": attachment.image.detail,
                },
            })
        elif attachment.text:
            parts[0]["text"] = f"{parts[0]['text']}\n\n[{attachment.name}]\n{attachment.text}"
    return parts
