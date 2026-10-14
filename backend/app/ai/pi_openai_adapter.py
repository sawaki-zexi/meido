"""Pi-style rich stream for OpenAI Chat Completions compatible providers."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
import json
import time

import httpx
from partial_json_parser import MalformedJSON, loads as parse_partial_json

from .message_transform import transform_messages
from .model_adapter import (
    _compatible_function_names,
    _is_deepseek,
    _raise_for_status_with_details,
    _response_error_detail,
    _remap_function_names,
)
from .model_protocol import effective_tools, model_cost, normalize_tools
from .model_event_stream import AssistantEventStream
from .model_types import (
    ModelAssistantMessage,
    ModelCapabilities,
    ModelContentBlock,
    ModelImageBlock,
    ModelMessage,
    ModelResponseMetadata,
    ModelStreamDone,
    ModelStreamError,
    ModelStreamEvent,
    ModelStreamStart,
    ModelTextBlock,
    ModelTextDelta,
    ModelTextStart,
    ModelTextEnd,
    ModelThinkingBlock,
    ModelThinkingDelta,
    ModelThinkingEnd,
    ModelThinkingStart,
    ModelToolCallBlock,
    ModelToolCallDelta,
    ModelToolCallEnd,
    ModelToolCallStart,
    ModelToolResultMessage,
    ModelUserMessage,
    ModelSystemMessage,
    ModelUsage,
)
from ..models import ModelConfiguration


@dataclass(frozen=True, slots=True)
class OpenAICompletionsOptions:
    """Compatibility switches for providers that expose OpenAI-shaped APIs."""

    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    source_model: str | None = None
    send_reasoning_content: bool = False
    include_usage: bool = True
    use_max_completion_tokens: bool = False
    disable_deepseek_thinking_for_tools: bool = True
    supports_finish_reason: bool = True


@dataclass(slots=True)
class _ToolAggregate:
    id: str = ""
    name: str = ""
    arguments: str = ""


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _usage(payload: object) -> ModelUsage:
    if not isinstance(payload, Mapping):
        return ModelUsage()
    prompt = _int(payload.get("prompt_tokens", payload.get("input_tokens")))
    output = _int(payload.get("completion_tokens", payload.get("output_tokens")))
    cached = _int(payload.get("cache_read_input_tokens"))
    cache_write = _int(payload.get("cache_write_input_tokens"))
    reasoning = _int(payload.get("reasoning_tokens"))
    prompt_details = payload.get("prompt_tokens_details")
    if isinstance(prompt_details, Mapping):
        cached = max(cached, _int(prompt_details.get("cached_tokens")))
    output_details = payload.get("completion_tokens_details")
    if isinstance(output_details, Mapping):
        reasoning = max(reasoning, _int(output_details.get("reasoning_tokens")))
    return ModelUsage(
        input_tokens=prompt,
        output_tokens=output,
        cache_read_tokens=cached,
        cache_write_tokens=cache_write,
        reasoning_tokens=reasoning,
        cost=model_cost(payload),
    )


def _reasoning(value: object) -> tuple[str, str | None]:
    if isinstance(value, str):
        return value, None
    if not isinstance(value, list):
        return "", None
    text_parts: list[str] = []
    signature: str | None = None
    for item in value:
        if isinstance(item, str):
            text_parts.append(item)
        elif isinstance(item, Mapping):
            text = item.get("text", item.get("content"))
            if isinstance(text, str):
                text_parts.append(text)
            item_signature = item.get("signature", item.get("data"))
            if item_signature is not None:
                signature = item_signature if isinstance(item_signature, str) else json.dumps(item_signature, ensure_ascii=False, separators=(",", ":"))
    return "".join(text_parts), signature


def _text_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, Mapping) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)


def _index(value: object, fallback: int) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str) and value:
        return value
    return str(fallback)


def _safe_json(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"))
    return ""


class _ResponseAccumulator:
    def __init__(
        self,
        internal_names: Mapping[str, str],
        provider: str | None = None,
        *,
        supports_finish_reason: bool = True,
    ) -> None:
        self.internal_names = internal_names
        self.provider = provider
        self.supports_finish_reason = supports_finish_reason
        self.text_parts: list[str] = []
        self.thinking_parts: list[str] = []
        self.thinking_signature: str | None = None
        self.tool_calls: OrderedDict[str, _ToolAggregate] = OrderedDict()
        self.block_order: list[tuple[str, str]] = []
        self.usage = ModelUsage()
        self.response_id: str | None = None
        self.response_model: str | None = None
        self.stop_reason: str | None = None
        self.error_message: str | None = None
        self.tool_error: str | None = None
        self._text_started = False
        self._thinking_started = False
        self._tool_started: set[str] = set()

    def consume(self, chunk: Mapping[str, object]) -> list[ModelStreamEvent]:
        events: list[ModelStreamEvent] = []
        if isinstance(chunk.get("id"), str):
            self.response_id = chunk["id"]
        if isinstance(chunk.get("model"), str):
            self.response_model = chunk["model"]
        if chunk.get("usage") is not None:
            self.usage = _usage(chunk.get("usage"))
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            return events
        choice = choices[0]
        finish = choice.get("finish_reason")
        if isinstance(finish, str) and finish:
            self.stop_reason = _stop_reason(finish)
            if self.stop_reason == "error":
                self.error_message = f"Provider finish_reason: {finish}"
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            delta = choice.get("message") if isinstance(choice.get("message"), Mapping) else {}
        events.extend(self._consume_delta(delta))
        return events

    def _consume_delta(self, delta: Mapping[str, object]) -> list[ModelStreamEvent]:
        events: list[ModelStreamEvent] = []
        content = _text_content(delta.get("content"))
        if content:
            if not self._text_started:
                self._text_started = True
                self.block_order.append(("text", ""))
                events.append(ModelTextStart())
            self.text_parts.append(content)
            events.append(ModelTextDelta(content))

        reasoning_value = delta.get("reasoning_content")
        if reasoning_value is None:
            reasoning_value = delta.get("reasoning")
        if reasoning_value is None:
            reasoning_value = delta.get("reasoning_text")
        reasoning_text, signature = _reasoning(reasoning_value)
        details_text, details_signature = _reasoning(delta.get("reasoning_details"))
        reasoning_text = reasoning_text or details_text
        signature = signature or details_signature
        if reasoning_text or signature:
            if not self._thinking_started:
                self._thinking_started = True
                self.block_order.append(("thinking", ""))
                events.append(ModelThinkingStart())
            self.thinking_parts.append(reasoning_text)
            self.thinking_signature = self.thinking_signature or signature
            if reasoning_text:
                events.append(ModelThinkingDelta(reasoning_text))

        raw_calls = delta.get("tool_calls")
        if isinstance(raw_calls, list):
            for offset, raw_call in enumerate(raw_calls):
                events.extend(self._consume_tool_call(raw_call, offset))
        legacy = delta.get("function_call")
        if isinstance(legacy, Mapping):
            events.extend(self._consume_tool_call({"index": "legacy", "function": legacy}, 0))
        return events

    def _consume_tool_call(self, raw_call: object, fallback: int) -> list[ModelStreamEvent]:
        if not isinstance(raw_call, Mapping):
            return []
        key = _index(raw_call.get("index"), fallback)
        call = self.tool_calls.setdefault(key, _ToolAggregate())
        events: list[ModelStreamEvent] = []
        raw_id = raw_call.get("id")
        if isinstance(raw_id, str):
            call.id += raw_id
        function = raw_call.get("function")
        if not isinstance(function, Mapping):
            function = raw_call
        name = function.get("name")
        if isinstance(name, str):
            call.name += name
        arguments = function.get("arguments")
        if arguments is not None and not isinstance(arguments, (str, Mapping)):
            self.tool_error = f"模型工具参数格式无效: {call.name or key}"
            return events
        fragment = _safe_json(arguments)
        if fragment:
            call.arguments += fragment
        if key not in self._tool_started:
            self._tool_started.add(key)
            self.block_order.append(("tool", key))
            events.append(ModelToolCallStart(call.id or f"call-{key}", call.name))
        if fragment:
            events.append(ModelToolCallDelta(call.id or f"call-{key}", fragment))
        return events

    def finish(self) -> tuple[list[ModelStreamEvent], ModelAssistantMessage | None, str | None]:
        events: list[ModelStreamEvent] = []
        if self._text_started:
            events.append(ModelTextEnd())
        if self._thinking_started:
            events.append(ModelThinkingEnd())
        if self.tool_error:
            return events, None, self.tool_error
        blocks: list[ModelContentBlock] = []
        for kind, key in self.block_order:
            if kind == "text":
                blocks.append(ModelTextBlock("".join(self.text_parts)))
            elif kind == "thinking":
                blocks.append(ModelThinkingBlock("".join(self.thinking_parts), self.thinking_signature))
            elif kind == "tool":
                aggregate = self.tool_calls[key]
                try:
                    arguments = json.loads(aggregate.arguments or "{}")
                except json.JSONDecodeError:
                    try:
                        arguments = parse_partial_json(aggregate.arguments)
                    except MalformedJSON:
                        return events, None, f"模型工具参数不是有效 JSON: {aggregate.name or key}"
                if not isinstance(arguments, dict):
                    return events, None, f"模型工具参数必须是 JSON 对象: {aggregate.name or key}"
                call_id = aggregate.id or f"call-{key}"
                name = self.internal_names.get(aggregate.name, aggregate.name)
                call = ModelToolCallBlock(call_id, name, arguments)
                blocks.append(call)
                events.append(ModelToolCallEnd(call))
        if self.stop_reason == "error":
            return events, None, self.error_message or "模型服务以错误原因终止"
        stop_reason = self.stop_reason
        if stop_reason is None:
            if self.supports_finish_reason:
                return events, None, "Stream ended without finish_reason"
            stop_reason = "tool_use" if self.tool_calls else "stop"
        message = ModelAssistantMessage(
            content=tuple(blocks),
            metadata=ModelResponseMetadata(
                provider=self.provider,
                api="openai-completions",
                model=self.response_model,
                response_id=self.response_id,
                usage=self.usage,
                stop_reason=stop_reason,
                timestamp=int(time.time() * 1000),
            ),
        )
        return events, message, None


def _stop_reason(value: str) -> str:
    return {
        "tool_calls": "tool_use",
        "function_call": "tool_use",
        "stop": "stop",
        "length": "length",
        "content_filter": "error",
    }.get(value, "error")


def _message_to_wire(message: ModelMessage, *, send_reasoning: bool) -> dict[str, object]:
    if isinstance(message, ModelSystemMessage):
        text = "".join(block.text for block in message.content)
        return {"role": "system", "content": text}
    if isinstance(message, ModelUserMessage):
        return {"role": "user", "content": _blocks_to_wire_content(message.content)}
    if isinstance(message, ModelToolResultMessage):
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": _blocks_to_wire_content(message.content),
        }
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[dict[str, object]] = []
    for block in message.content:
        if isinstance(block, ModelTextBlock):
            text_parts.append(block.text)
        elif isinstance(block, ModelThinkingBlock):
            thinking_parts.append(block.text)
        elif isinstance(block, ModelToolCallBlock):
            tool_calls.append({
                "id": block.id,
                "type": "function",
                "function": {
                    "name": block.name,
                    "arguments": json.dumps(block.arguments, ensure_ascii=False, separators=(",", ":")),
                },
            })
    item: dict[str, object] = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        item["tool_calls"] = tool_calls
    if send_reasoning and thinking_parts:
        item["reasoning_content"] = "".join(thinking_parts)
    return item


def _blocks_to_wire_content(blocks: Sequence[ModelContentBlock]) -> object:
    text = "".join(block.text for block in blocks if isinstance(block, ModelTextBlock))
    images = [block for block in blocks if isinstance(block, ModelImageBlock)]
    if not images:
        return text
    parts: list[dict[str, object]] = [{"type": "text", "text": text}]
    parts.extend({
        "type": "image_url",
        "image_url": {
            "url": f"data:{image.media_type};base64,{image.data_base64}",
            "detail": image.detail,
        },
    } for image in images)
    return parts


def _effective_tools(
    messages: Sequence[ModelMessage],
    tools: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    return effective_tools(messages, tools)


def _has_tool_history(messages: Sequence[ModelMessage]) -> bool:
    for message in messages:
        if isinstance(message, ModelToolResultMessage):
            return True
        if isinstance(message, ModelAssistantMessage) and any(
            isinstance(block, ModelToolCallBlock) for block in message.content
        ):
            return True
    return False


async def _sse_payloads(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    data: list[str] = []
    async for line in lines:
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            if data:
                previous = "\n".join(data).strip()
                try:
                    json.loads(previous)
                except json.JSONDecodeError:
                    pass
                else:
                    yield previous
                    data.clear()
            data.append(line[5:].lstrip())
            continue
        if not line.strip() and data:
            yield "\n".join(data).strip()
            data.clear()
    if data:
        yield "\n".join(data).strip()


class OpenAICompletionsRichAdapter:
    """Parse OpenAI-compatible SSE into Pi-style assistant events."""

    def __init__(self, timeout: float | None = None, options: OpenAICompletionsOptions | None = None) -> None:
        self.timeout = timeout
        self.options = options or OpenAICompletionsOptions()

    def _prepare_request(
        self,
        messages: Sequence[ModelMessage],
        configuration: ModelConfiguration,
        tools: Sequence[Mapping[str, object]],
        *,
        max_tokens: int | None,
        stream: bool,
    ) -> tuple[dict[str, object], dict[str, str], dict[str, str]]:
        options = self.options
        transformed = transform_messages(
            messages,
            capabilities=options.capabilities,
            source_model=options.source_model,
            target_provider=configuration.provider,
            target_api=configuration.api,
            target_model=configuration.model,
        )
        raw_tools = _effective_tools(transformed, tools)
        normalized_tools = normalize_tools(raw_tools)
        wire_definitions = [{
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        } for tool in normalized_tools]
        send_reasoning = options.send_reasoning_content and not (
            _is_deepseek(configuration)
            and raw_tools
            and options.disable_deepseek_thinking_for_tools
        )
        raw_messages = tuple(
            _message_to_wire(message, send_reasoning=send_reasoning)
            for message in transformed
        )
        aliases = _compatible_function_names(wire_definitions, raw_messages)
        wire_tools = _remap_function_names(wire_definitions, aliases, tools=True)
        wire_messages = _remap_function_names(raw_messages, aliases)
        request_data: dict[str, object] = {
            "model": configuration.model,
            "messages": wire_messages,
            "stream": stream,
        }
        if wire_tools:
            request_data["tools"] = wire_tools
            request_data["tool_choice"] = "auto"
            if _is_deepseek(configuration) and options.disable_deepseek_thinking_for_tools:
                request_data["thinking"] = {"type": "disabled"}
        elif _has_tool_history(transformed):
            request_data["tools"] = []
        if stream and options.include_usage:
            request_data["stream_options"] = {"include_usage": True}
        if max_tokens is not None:
            request_data["max_completion_tokens" if options.use_max_completion_tokens else "max_tokens"] = max_tokens
        headers = {"Authorization": f"Bearer {configuration.apiKey}"} if configuration.apiKey else {}
        return request_data, headers, {alias: name for name, alias in aliases.items()}

    async def stream_messages(
        self,
        messages: Sequence[ModelMessage],
        configuration: ModelConfiguration,
        tools: Sequence[Mapping[str, object]] = (),
        *,
        max_tokens: int | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        request_data, headers, internal_names = self._prepare_request(
            messages,
            configuration,
            tools,
            max_tokens=max_tokens,
            stream=True,
        )
        accumulator = _ResponseAccumulator(
            internal_names,
            configuration.provider,
            supports_finish_reason=self.options.supports_finish_reason,
        )
        completed = False
        yield ModelStreamStart()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream(
                    "POST",
                    f"{configuration.baseUrl.rstrip('/')}/chat/completions",
                    headers=headers,
                    json=request_data,
                ) as response:
                    await _raise_for_status_with_details(response, configuration.apiKey)
                    async for payload in _sse_payloads(response.aiter_lines()):
                        if cancel_check is not None:
                            cancel_check()
                        if payload == "[DONE]":
                            completed = True
                            break
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(chunk, Mapping):
                            continue
                        if chunk.get("error") is not None:
                            detail = chunk.get("error")
                            message = detail.get("message") if isinstance(detail, Mapping) else str(detail)
                            if not isinstance(message, str) or not message:
                                message = "模型服务返回错误"
                            if configuration.apiKey:
                                message = message.replace(configuration.apiKey, "***")
                            yield ModelStreamError(message)
                            return
                        for event in accumulator.consume(chunk):
                            yield event
        except httpx.HTTPStatusError as error:
            response = error.response
            detail = _response_error_detail(response, configuration.apiKey)
            message = f"HTTP {response.status_code} {response.reason_phrase}"
            if detail:
                message = f"{message}: {detail}"
            yield ModelStreamError(message)
            return
        except RuntimeError as error:
            if str(error) == "agent run cancelled":
                raise
            detail = str(error)
            if configuration.apiKey:
                detail = detail.replace(configuration.apiKey, "***")
            yield ModelStreamError(detail)
            return
        except Exception as error:
            detail = str(error)
            if configuration.apiKey:
                detail = detail.replace(configuration.apiKey, "***")
            yield ModelStreamError(detail)
            return
        if not completed:
            yield ModelStreamError("模型流式响应未正常结束")
            return
        events, message, error = accumulator.finish()
        if error or message is None:
            yield ModelStreamError(error or "模型响应无法完成")
            return
        for event in events:
            yield event
        yield ModelStreamDone(message)

    async def complete_messages(
        self,
        messages: Sequence[ModelMessage],
        configuration: ModelConfiguration,
        tools: Sequence[Mapping[str, object]] = (),
        *,
        max_tokens: int | None = None,
    ) -> ModelAssistantMessage:
        """Complete one request through the same parser used by the rich stream."""

        request_data, headers, internal_names = self._prepare_request(
            messages,
            configuration,
            tools,
            max_tokens=max_tokens,
            stream=False,
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{configuration.baseUrl.rstrip('/')}/chat/completions",
                    headers=headers,
                    json=request_data,
                )
                await _raise_for_status_with_details(response, configuration.apiKey)
                payload = response.json()
        except httpx.HTTPStatusError as error:
            detail = _response_error_detail(error.response, configuration.apiKey)
            message = f"HTTP {error.response.status_code} {error.response.reason_phrase}"
            if detail:
                message = f"{message}: {detail}"
            raise RuntimeError(message) from error
        except (httpx.HTTPError, json.JSONDecodeError) as error:
            detail = str(error).replace(configuration.apiKey, "***") if configuration.apiKey else str(error)
            raise RuntimeError(detail) from error
        if not isinstance(payload, Mapping):
            raise RuntimeError("模型响应不是 JSON 对象")
        if payload.get("error") is not None:
            detail = payload.get("error")
            message = detail.get("message") if isinstance(detail, Mapping) else str(detail)
            if not isinstance(message, str) or not message:
                message = "模型服务返回错误"
            if configuration.apiKey:
                message = message.replace(configuration.apiKey, "***")
            raise RuntimeError(message)
        accumulator = _ResponseAccumulator(
            internal_names,
            configuration.provider,
            supports_finish_reason=self.options.supports_finish_reason,
        )
        accumulator.consume(payload)
        _, message, error = accumulator.finish()
        if message is None:
            raise RuntimeError(error or "模型响应无法完成")
        return message

    async def stream_rich_messages(
        self,
        messages: Sequence[ModelMessage],
        configuration: ModelConfiguration,
        tools: Sequence[Mapping[str, object]] = (),
        *,
        max_tokens: int | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        async for event in self.stream_messages(
            messages,
            configuration,
            tools,
            max_tokens=max_tokens,
            cancel_check=cancel_check,
        ):
            yield event

    def stream(
        self,
        messages: Sequence[ModelMessage],
        configuration: ModelConfiguration,
        tools: Sequence[Mapping[str, object]] = (),
        *,
        max_tokens: int | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> AssistantEventStream:
        stream = AssistantEventStream()

        async def pump() -> None:
            try:
                async for event in self.stream_messages(
                    messages,
                    configuration,
                    tools,
                    max_tokens=max_tokens,
                    cancel_check=cancel_check,
                ):
                    await stream.push(event)
            except Exception as error:
                await stream.push(ModelStreamError(str(error)))

        import asyncio
        asyncio.create_task(pump())
        return stream


def response_error_detail(response: httpx.Response, api_key: str) -> str:
    """Expose the redacted provider detail for callers that need diagnostics."""

    return _response_error_detail(response, api_key)
