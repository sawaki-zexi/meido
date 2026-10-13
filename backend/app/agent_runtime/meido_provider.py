from __future__ import annotations

import inspect
import json
from dataclasses import asdict
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime, timezone
from typing import cast

from ..models import Message, ModelConfiguration, Role
from ..model_adapter import ModelAdapter, ModelStreamError, ModelTextDelta, ModelToolCall
from ..model_types import (
    ModelAssistantMessage as RichAssistantMessage,
    ModelImageBlock as RichImageBlock,
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


class MeidoProvider:
    """Adapt Meido's model boundary to the provider-neutral Runtime contract."""

    def __init__(
        self,
        adapter: ModelAdapter,
        role: Role,
        configuration: ModelConfiguration | None,
        memory_context: str = "",
        capability_prompt: str = "",
    ) -> None:
        self.adapter = adapter
        self.role = role
        self.configuration = configuration
        self.memory_context = memory_context
        self.capability_prompt = capability_prompt

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[object],
        tools: Sequence[dict[str, object]],
        signal: CancellationToken,
        session_id: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        del model, session_id
        rich = getattr(self.adapter, "stream_rich_messages", None)
        if rich is not None:
            return self._stream_rich_structured(system, messages, tools, signal, rich)
        if tools:
            structured = getattr(self.adapter, "stream_structured_messages", None)
            if structured is None:
                async def unsupported() -> AsyncIterator[ProviderEvent]:
                    yield ProviderStartEvent()
                    yield ProviderErrorEvent("当前模型适配器尚未支持结构化工具调用")
                return unsupported()
            return self._stream_structured(system, messages, tools, signal, structured)
        return self._stream(messages, signal)

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
                source = (
                    rich(
                        runtime_messages,
                        self.configuration,
                        [dict(tool) for tool in tools],
                        cancel_check=signal.raise_if_cancelled,
                    )
                    if accepts_cancel_check
                    else rich(runtime_messages, self.configuration, [dict(tool) for tool in tools])
                )
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
                        response_metadata["thinking"] = [
                            {
                                "text": block.text,
                                "signature": block.signature,
                            }
                            for block in event.message.content
                            if isinstance(block, RichThinkingBlock)
                        ]
                        yield AssistantDoneEvent(AssistantMessage(
                            content=content,
                            tool_calls=tuple(tool_calls),
                            stop_reason="tool_use" if tool_calls else stop_reason,
                            metadata=response_metadata,
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
                source = structured(runtime_messages, self.configuration, [dict(tool) for tool in tools])
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
        for message in messages:
            if isinstance(message, UserMessage):
                result.append({"role": "user", "content": _user_content(message)})
            elif isinstance(message, AssistantMessage):
                item: dict[str, object] = {"role": "assistant", "content": message.content or None}
                if message.tool_calls:
                    item["tool_calls"] = [
                        {
                            "id": call.id,
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
                    "tool_call_id": message.tool_call_id,
                    "content": content,
                })
        return result

    def _rich_messages(self, system: str, messages: Sequence[object]) -> list[RichMessage]:
        result: list[RichMessage] = [RichSystemMessage((RichTextBlock(self._structured_system_prompt(system)),))]
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
                result.append(RichUserMessage(tuple(blocks)))
            elif isinstance(message, AssistantMessage):
                metadata = message.metadata if isinstance(message.metadata, Mapping) else {}
                raw_usage = metadata.get("usage")
                usage = raw_usage if isinstance(raw_usage, Mapping) else {}
                blocks: list[object] = []
                raw_thinking = metadata.get("thinking")
                if isinstance(raw_thinking, list):
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
                if message.content:
                    blocks.append(RichTextBlock(message.content))
                blocks.extend(
                    RichToolCallBlock(
                        call.id,
                        call.name,
                        sanitize_browser_arguments(call.name, call.arguments),
                    )
                    for call in message.tool_calls
                )
                timestamp = metadata.get("timestamp")
                result.append(RichAssistantMessage(
                    tuple(blocks),
                    RichResponseMetadata(
                        provider=metadata.get("provider") if isinstance(metadata.get("provider"), str) else None,
                        api=metadata.get("api") if isinstance(metadata.get("api"), str) else None,
                        model=metadata.get("model") if isinstance(metadata.get("model"), str) else None,
                        response_id=metadata.get("response_id") if isinstance(metadata.get("response_id"), str) else None,
                        usage=RichUsage(
                            input_tokens=_metadata_count(usage.get("input_tokens")),
                            output_tokens=_metadata_count(usage.get("output_tokens")),
                            cache_read_tokens=_metadata_count(usage.get("cache_read_tokens")),
                            cache_write_tokens=_metadata_count(usage.get("cache_write_tokens")),
                            reasoning_tokens=_metadata_count(usage.get("reasoning_tokens")),
                        ),
                        stop_reason=(
                            metadata.get("stop_reason")
                            if isinstance(metadata.get("stop_reason"), str)
                            else message.stop_reason
                        ),
                        error_message=metadata.get("error_message") if isinstance(metadata.get("error_message"), str) else None,
                        timestamp=_metadata_count(timestamp) if isinstance(timestamp, int) and not isinstance(timestamp, bool) else None,
                    ),
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
                    tool_call_id=message.tool_call_id,
                    tool_name=message.tool_name,
                    is_error=message.is_error,
                ))
        return result

    def _structured_system_prompt(self, system: str) -> str:
        if not system:
            return self._role_system_prompt()
        return "\n\n".join(part for part in (system, self.capability_prompt) if part)

    def _role_system_prompt(self) -> str:
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
            self.capability_prompt,
        ] if part)

    def _combined_memory_context(self) -> str:
        return "\n\n".join(part for part in (self.memory_context, self.capability_prompt) if part)

    def _role_with_context(self) -> Role:
        profile = self.role.profile.model_copy(deep=True)
        profile.profile = "\n\n".join(
            part for part in (profile.profile, self.capability_prompt) if part
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
        source: object = (
            stream_reply(role, history, self.configuration, memory_context=self._combined_memory_context())
            if accepts_memory
            else stream_reply(role, history, self.configuration)
        )
        content = ""
        async for delta in cast(AsyncIterator[str], source):
            signal.raise_if_cancelled()
            content += delta
            yield TextDeltaEvent(delta)
        yield AssistantDoneEvent(AssistantMessage(content=content))

    async def _stream_multimodal(self, messages: Sequence[object], signal: CancellationToken) -> AsyncIterator[ProviderEvent]:
        prompt = self._role_system_prompt()
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
            source = self.adapter.stream_messages(wire_messages, self.configuration)
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
