"""Native OpenAI Responses API adapter."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
import json
import time

import httpx

from .message_transform import transform_messages
from .model_adapter import _raise_for_status_with_details, _response_error_detail
from .model_protocol import compatible_tool_names, effective_tools, json_object, normalize_tools, remap_message_tool_names, safe_tool_id, tool_result_text
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
    ModelTextEnd,
    ModelTextStart,
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


@dataclass(frozen=True, slots=True)
class OpenAIResponsesOptions:
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    include_usage: bool = True


@dataclass(slots=True)
class _Output:
    kind: str
    id: str = ""
    name: str = ""
    text: str = ""
    arguments: str = ""
    signature: str | None = None


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _usage(value: object) -> ModelUsage:
    if not isinstance(value, Mapping):
        return ModelUsage()
    details = value.get("output_tokens_details")
    reasoning = _int(details.get("reasoning_tokens")) if isinstance(details, Mapping) else 0
    return ModelUsage(
        input_tokens=_int(value.get("input_tokens")),
        output_tokens=_int(value.get("output_tokens")),
        cache_read_tokens=_int(value.get("input_tokens_details", {}).get("cached_tokens")) if isinstance(value.get("input_tokens_details"), Mapping) else 0,
        reasoning_tokens=reasoning,
    )


def _text(blocks: Sequence[object]) -> str:
    return "".join(block.text for block in blocks if isinstance(block, ModelTextBlock))


def _responses_content(blocks: Sequence[object], *, input_side: bool) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for block in blocks:
        if isinstance(block, ModelTextBlock):
            result.append({"type": "input_text" if input_side else "output_text", "text": block.text})
        elif isinstance(block, ModelImageBlock):
            result.append({
                "type": "input_image",
                "image_url": f"data:{block.media_type};base64,{block.data_base64}",
                "detail": block.detail,
            })
    return result


def _messages_to_input(messages: Sequence[ModelMessage]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for message in messages:
        if isinstance(message, ModelSystemMessage):
            result.append({"role": "system", "content": _text(message.content)})
        elif isinstance(message, ModelUserMessage):
            result.append({"role": "user", "content": _responses_content(message.content, input_side=True)})
        elif isinstance(message, ModelAssistantMessage):
            for block in message.content:
                if isinstance(block, ModelTextBlock):
                    result.append({"role": "assistant", "content": [{"type": "output_text", "text": block.text}]})
                elif isinstance(block, ModelThinkingBlock):
                    if block.text:
                        result.append({"role": "assistant", "content": [{"type": "output_text", "text": block.text}]})
                elif isinstance(block, ModelToolCallBlock):
                    result.append({
                        "type": "function_call",
                        "call_id": safe_tool_id(block.id),
                        "name": block.name,
                        "arguments": json.dumps(block.arguments, ensure_ascii=False, separators=(",", ":")),
                    })
        elif isinstance(message, ModelToolResultMessage):
            text = tool_result_text(message.content)
            result.append({
                "type": "function_call_output",
                "call_id": safe_tool_id(message.tool_call_id),
                "output": text,
            })
    return result


def _tools_to_wire(tools: Sequence[Mapping[str, object]], aliases: Mapping[str, str]) -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "name": aliases.get(str(tool["name"]), str(tool["name"])),
            "description": str(tool.get("description", "")),
            "parameters": dict(tool["parameters"]),
            "strict": False,
        }
        for tool in normalize_tools(tools)
    ]


def _sse_events(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, str]]:
    async def iterator() -> AsyncIterator[tuple[str, str]]:
        event_name = "message"
        data: list[str] = []
        async for line in lines:
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip() or "message"
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line.strip() and data:
                yield event_name, "\n".join(data)
                event_name = "message"
                data.clear()
        if data:
            yield event_name, "\n".join(data)
    return iterator()


class _Accumulator:
    def __init__(
        self,
        name_aliases: Mapping[str, str] | None = None,
        *,
        provider: str = "openai",
        api: str = "openai-responses",
    ) -> None:
        self.name_aliases = name_aliases or {}
        self.provider = provider
        self.api = api
        self.outputs: OrderedDict[str, _Output] = OrderedDict()
        self.order: list[str] = []
        self.usage = ModelUsage()
        self.response_id: str | None = None
        self.model: str | None = None
        self.stop_reason: str | None = None
        self.completed = False
        self.text_started = False
        self.thinking_started = False
        self.tool_started: set[str] = set()

    def _get(self, key: str, kind: str = "function_call") -> _Output:
        if key not in self.outputs:
            self.outputs[key] = _Output(kind=kind)
            self.order.append(key)
        return self.outputs[key]

    def consume(self, event_type: str, data: Mapping[str, object]) -> list[ModelStreamEvent]:
        events: list[ModelStreamEvent] = []
        response = data.get("response") if isinstance(data.get("response"), Mapping) else data
        if isinstance(response, Mapping):
            if isinstance(response.get("id"), str):
                self.response_id = response["id"]
            if isinstance(response.get("model"), str):
                self.model = response["model"]
            if response.get("usage") is not None:
                self.usage = _usage(response.get("usage"))
        if event_type in {"response.created", "response.in_progress"}:
            return events
        if event_type == "response.output_text.delta":
            delta = data.get("delta")
            if isinstance(delta, str):
                if not self.text_started:
                    self.text_started = True
                    key = "text"
                    self._get(key, "text")
                    events.append(ModelTextStart())
                self._get("text", "text").text += delta
                events.append(ModelTextDelta(delta))
        elif event_type == "response.reasoning_summary_text.delta":
            delta = data.get("delta")
            if isinstance(delta, str):
                if not self.thinking_started:
                    self.thinking_started = True
                    self._get("thinking", "thinking")
                    events.append(ModelThinkingStart())
                self._get("thinking", "thinking").text += delta
                events.append(ModelThinkingDelta(delta))
        elif event_type == "response.output_item.added":
            item = data.get("item")
            if isinstance(item, Mapping) and item.get("type") in {"function_call", "custom_tool_call"}:
                key = str(item.get("id") or item.get("call_id") or f"call-{len(self.order)}")
                call = self._get(key)
                call.id = str(item.get("call_id") or item.get("id") or key)
                call.name = str(item.get("name") or "")
                self.tool_started.add(key)
                events.append(ModelToolCallStart(call.id, call.name))
        elif event_type == "response.function_call_arguments.delta":
            key = str(data.get("item_id") or data.get("output_index") or f"call-{len(self.order)}")
            call = self._get(key)
            if isinstance(data.get("call_id"), str):
                call.id = data["call_id"]
            if isinstance(data.get("name"), str):
                call.name = data["name"]
            fragment = data.get("delta")
            if isinstance(fragment, str):
                call.arguments += fragment
                if key not in self.tool_started:
                    self.tool_started.add(key)
                    events.append(ModelToolCallStart(call.id or key, call.name))
                events.append(ModelToolCallDelta(call.id or key, fragment))
        elif event_type == "response.function_call_arguments.done":
            key = str(data.get("item_id") or data.get("output_index") or f"call-{len(self.order)}")
            call = self._get(key)
            if isinstance(data.get("call_id"), str):
                call.id = data["call_id"]
            if isinstance(data.get("name"), str):
                call.name = data["name"]
            if isinstance(data.get("arguments"), str):
                call.arguments = data["arguments"]
        elif event_type == "response.completed":
            self.completed = True
            response = data.get("response")
            if isinstance(response, Mapping):
                status = response.get("status")
                self.stop_reason = "stop" if status == "completed" else str(status) if status else None
        elif event_type == "response.failed":
            error = data.get("response", data)
            return [ModelStreamError(str(error.get("error") if isinstance(error, Mapping) else error))]
        return events

    def finish(self) -> tuple[list[ModelStreamEvent], ModelAssistantMessage | None, str | None]:
        if not self.completed:
            return [], None, "模型流式响应未正常结束"
        events: list[ModelStreamEvent] = []
        if self.text_started:
            events.append(ModelTextEnd())
        if self.thinking_started:
            events.append(ModelThinkingEnd())
        blocks: list[ModelContentBlock] = []
        for key in self.order:
            item = self.outputs[key]
            if item.kind == "text":
                blocks.append(ModelTextBlock(item.text))
            elif item.kind == "thinking":
                blocks.append(ModelThinkingBlock(item.text, item.signature))
            elif item.kind in {"function_call", "custom_tool_call"}:
                try:
                    arguments = json_object(item.arguments)
                except ValueError as error:
                    return events, None, str(error)
                call = ModelToolCallBlock(item.id or key, self.name_aliases.get(item.name, item.name), arguments)
                blocks.append(call)
                events.append(ModelToolCallEnd(call))
        reason = self.stop_reason or ("tool_use" if any(isinstance(block, ModelToolCallBlock) for block in blocks) else "stop")
        return events, ModelAssistantMessage(
            content=tuple(blocks),
            metadata=ModelResponseMetadata(
                provider=self.provider,
                api=self.api,
                model=self.model,
                response_id=self.response_id,
                usage=self.usage,
                stop_reason=reason,
                timestamp=int(time.time() * 1000),
            ),
        ), None


class OpenAIResponsesAdapter:
    def __init__(
        self,
        timeout: float | None = None,
        options: OpenAIResponsesOptions | None = None,
        *,
        api: str = "openai-responses",
        provider: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.options = options or OpenAIResponsesOptions()
        self.api = api
        self.provider = provider

    def _url(self, configuration) -> str:
        return f"{configuration.baseUrl.rstrip('/')}/responses"

    def _headers(self, configuration) -> dict[str, str]:
        return {"Authorization": f"Bearer {configuration.apiKey}", "content-type": "application/json"}

    def _accumulator(self, aliases, configuration) -> _Accumulator:
        return _Accumulator(aliases, provider=self.provider or configuration.provider, api=self.api)

    def _prepare(self, messages, configuration, tools, *, max_tokens, stream):
        transformed = transform_messages(
            messages,
            capabilities=self.options.capabilities,
            target_provider=self.provider or configuration.provider,
            target_api=self.api,
            target_model=configuration.model,
        )
        normalized = normalize_tools(effective_tools(transformed, tools))
        aliases, reverse = compatible_tool_names(normalized, transformed)
        transformed = remap_message_tool_names(transformed, aliases)
        payload: dict[str, object] = {
            "model": configuration.model,
            "input": _messages_to_input(transformed),
            "stream": stream,
        }
        wire_tools = _tools_to_wire(normalized, aliases)
        if wire_tools:
            payload["tools"] = wire_tools
        if max_tokens is not None:
            payload["max_output_tokens"] = max_tokens
        headers = self._headers(configuration)
        return payload, headers, reverse

    async def stream_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None) -> AsyncIterator[ModelStreamEvent]:
        payload, headers, name_aliases = self._prepare(messages, configuration, tools, max_tokens=max_tokens, stream=True)
        accumulator = self._accumulator(name_aliases, configuration)
        yield ModelStreamStart()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream("POST", self._url(configuration), headers=headers, json=payload) as response:
                    await _raise_for_status_with_details(response, configuration.apiKey)
                    async for event_type, raw in _sse_events(response.aiter_lines()):
                        if cancel_check:
                            cancel_check()
                        if raw == "[DONE]":
                            accumulator.completed = True
                            break
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(data, Mapping):
                            for event in accumulator.consume(str(data.get("type", event_type)), data):
                                if isinstance(event, ModelStreamError):
                                    error = event.error.replace(configuration.apiKey, "***") if configuration.apiKey else event.error
                                    yield ModelStreamError(error)
                                    return
                                yield event
        except httpx.HTTPStatusError as error:
            detail = _response_error_detail(error.response, configuration.apiKey)
            yield ModelStreamError(f"HTTP {error.response.status_code} {error.response.reason_phrase}: {detail}" if detail else f"HTTP {error.response.status_code} {error.response.reason_phrase}")
            return
        except RuntimeError as error:
            if str(error) == "agent run cancelled":
                raise
            yield ModelStreamError(str(error))
            return
        except Exception as error:
            yield ModelStreamError(str(error).replace(configuration.apiKey, "***") if configuration.apiKey else str(error))
            return
        events, message, error = accumulator.finish()
        if error or message is None:
            yield ModelStreamError(error or "模型响应无法完成")
            return
        for event in events:
            yield event
        yield ModelStreamDone(message)

    async def complete_messages(self, messages, configuration, tools=(), *, max_tokens=None):
        payload, headers, name_aliases = self._prepare(messages, configuration, tools, max_tokens=max_tokens, stream=False)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self._url(configuration), headers=headers, json=payload)
            await _raise_for_status_with_details(response, configuration.apiKey)
        data = response.json()
        if not isinstance(data, Mapping):
            raise RuntimeError("模型响应不是 JSON 对象")
        if isinstance(data.get("error"), Mapping):
            error = data["error"]
            detail = error.get("message") if isinstance(error.get("message"), str) else str(error)
            if configuration.apiKey:
                detail = detail.replace(configuration.apiKey, "***")
            raise RuntimeError(f"模型响应失败: {detail}")
        if str(data.get("status", "")).lower() in {"failed", "incomplete", "cancelled"}:
            detail = str(data.get("incomplete_details") or data.get("status"))
            if configuration.apiKey:
                detail = detail.replace(configuration.apiKey, "***")
            raise RuntimeError(f"模型响应失败: {detail}")
        accumulator = self._accumulator(name_aliases, configuration)
        accumulator.response_id = data.get("id") if isinstance(data.get("id"), str) else None
        accumulator.model = data.get("model") if isinstance(data.get("model"), str) else None
        accumulator.usage = _usage(data.get("usage"))
        accumulator.stop_reason = "stop"
        output = data.get("output")
        if isinstance(output, list):
            for index, item in enumerate(output):
                if not isinstance(item, Mapping):
                    continue
                item_type = str(item.get("type", ""))
                key = str(item.get("id") or item.get("call_id") or index)
                block = _Output(item_type, str(item.get("call_id") or item.get("id") or key), str(item.get("name") or ""))
                if item_type == "message":
                    block.kind = "text"
                    block.text = _text([ModelTextBlock(str(part.get("text", ""))) for part in item.get("content", []) if isinstance(part, Mapping)]) if isinstance(item.get("content"), list) else str(item.get("content", ""))
                elif item_type in {"function_call", "custom_tool_call"}:
                    block.arguments = str(item.get("arguments", "{}"))
                accumulator.outputs[key] = block
                accumulator.order.append(key)
        accumulator.completed = True
        _, message, error = accumulator.finish()
        if message is None:
            detail = error or "模型响应无法完成"
            if configuration.apiKey:
                detail = detail.replace(configuration.apiKey, "***")
            raise RuntimeError(detail)
        return message

    async def stream_rich_messages(self, *args, **kwargs):
        async for event in self.stream_messages(*args, **kwargs):
            yield event
