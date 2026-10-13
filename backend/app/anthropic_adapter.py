"""Native Anthropic Messages adapter for the Pi-style model boundary."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
import json
import time

import httpx

from .message_transform import transform_messages
from .model_adapter import _payload_error_detail, _raise_for_status_with_details, _response_error_detail
from .model_protocol import compatible_tool_names, effective_tools, json_object, model_cost, normalize_tools, remap_message_tool_names, safe_tool_id
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
from .models import ModelConfiguration


@dataclass(frozen=True, slots=True)
class AnthropicMessagesOptions:
    capabilities: ModelCapabilities = field(default_factory=lambda: ModelCapabilities(
        supports_images=True,
        supports_reasoning=True,
        preserve_thinking_signatures=True,
        tool_call_id_pattern=r"[A-Za-z0-9_-]+",
        max_tool_call_id_length=64,
    ))
    max_tokens: int = 1024
    version: str = "2023-06-01"


@dataclass(slots=True)
class _Block:
    kind: str
    id: str = ""
    name: str = ""
    text: str = ""
    arguments: str = ""
    arguments_started: bool = False
    signature: str | None = None
    redacted: bool = False


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _usage(value: object) -> ModelUsage:
    if not isinstance(value, Mapping):
        return ModelUsage()
    return ModelUsage(
        input_tokens=_int(value.get("input_tokens")),
        output_tokens=_int(value.get("output_tokens")),
        cache_read_tokens=_int(value.get("cache_read_input_tokens")),
        cache_write_tokens=_int(value.get("cache_creation_input_tokens")),
        cost=model_cost(value),
    )


def _text(blocks: Sequence[object]) -> str:
    return "".join(item.get("text", "") for item in blocks if isinstance(item, Mapping) and isinstance(item.get("text"), str))


def _content_blocks(blocks: Sequence[object]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    text = ""
    for block in blocks:
        if isinstance(block, ModelTextBlock):
            text += block.text
        elif isinstance(block, ModelImageBlock):
            if text:
                result.append({"type": "text", "text": text})
                text = ""
            result.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": block.media_type,
                    "data": block.data_base64,
                },
            })
    if text or not result:
        result.append({"type": "text", "text": text})
    return result


def _tool_wire(tool: Mapping[str, object], aliases: Mapping[str, str]) -> dict[str, object]:
    return {
        "name": aliases.get(str(tool["name"]), str(tool["name"])),
        "description": str(tool.get("description", "")),
        "input_schema": _anthropic_schema(tool["parameters"]),
    }


def _anthropic_schema(schema: object) -> object:
    """Convert a tool schema to Anthropic's supported JSON Schema subset.

    Anthropic's strict tool mode rejects several validation keywords with a
    request-wide 400. Dropping those keywords would silently change tool
    validation, so fail before sending the request and include the schema path.
    Metadata that does not affect validation is safe to omit.
    """
    annotations = {"$schema", "$id", "title", "default", "examples", "deprecated", "readOnly", "writeOnly"}
    unsupported = {
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
        "maxItems", "uniqueItems", "minContains", "maxContains", "minProperties", "maxProperties",
    }
    formats = {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}

    def convert(value: object, path: str) -> object:
        if isinstance(value, bool):
            return value
        if isinstance(value, list):
            return [convert(item, f"{path}[{index}]") for index, item in enumerate(value)]
        if not isinstance(value, Mapping):
            return value
        result: dict[str, object] = {}
        for key, child in value.items():
            key = str(key)
            child_path = f"{path}.{key}"
            if key in unsupported or (key == "minItems" and child not in (0, 1)):
                raise ValueError(f"Anthropic 工具 schema 不支持 {child_path} 约束")
            if key == "format" and child not in formats:
                raise ValueError(f"Anthropic 工具 schema 不支持 {child_path} 格式: {child}")
            if key in annotations:
                continue
            if key == "properties" and isinstance(child, Mapping):
                result[key] = {str(name): convert(item, f"{child_path}.{name}") for name, item in child.items()}
            elif key in {"items", "additionalProperties", "additionalItems", "contains", "not", "if", "then", "else"}:
                result[key] = convert(child, child_path)
            elif key in {"allOf", "anyOf", "oneOf", "prefixItems"} and isinstance(child, list):
                result[key] = [convert(item, f"{child_path}[{index}]") for index, item in enumerate(child)]
            elif key in {"$defs", "definitions", "patternProperties", "dependentSchemas"} and isinstance(child, Mapping):
                result[key] = {str(name): convert(item, f"{child_path}.{name}") for name, item in child.items()}
            else:
                result[key] = child
        return result

    return convert(schema, "$")


def _messages_to_wire(
    messages: Sequence[ModelMessage],
    aliases: Mapping[str, str],
) -> tuple[str, list[dict[str, object]]]:
    system_parts: list[str] = []
    wire: list[dict[str, object]] = []
    for message in messages:
        if isinstance(message, ModelSystemMessage):
            system_parts.append("".join(block.text for block in message.content))
        elif isinstance(message, ModelUserMessage):
            wire.append({"role": "user", "content": _content_blocks(message.content)})
        elif isinstance(message, ModelAssistantMessage):
            content: list[dict[str, object]] = []
            for block in message.content:
                if isinstance(block, ModelTextBlock):
                    content.append({"type": "text", "text": block.text})
                elif isinstance(block, ModelThinkingBlock):
                    if block.redacted:
                        if block.signature:
                            content.append({"type": "redacted_thinking", "data": block.signature})
                    elif block.signature:
                        content.append({
                            "type": "thinking",
                            "thinking": block.text,
                            "signature": block.signature,
                        })
                    elif block.text.strip():
                        content.append({"type": "text", "text": block.text})
                elif isinstance(block, ModelToolCallBlock):
                    content.append({
                        "type": "tool_use",
                        "id": safe_tool_id(block.id),
                        "name": aliases.get(block.name, block.name),
                        "input": dict(block.arguments),
                    })
            wire.append({"role": "assistant", "content": content or [{"type": "text", "text": ""}]})
        elif isinstance(message, ModelToolResultMessage):
            result = {
                "type": "tool_result",
                "tool_use_id": safe_tool_id(message.tool_call_id),
                "content": _content_blocks(message.content),
            }
            if message.is_error:
                result["is_error"] = True
            if wire and wire[-1].get("role") == "user":
                previous = wire[-1].get("content")
                if isinstance(previous, list):
                    previous.append(result)
                else:
                    wire.append({"role": "user", "content": [result]})
            else:
                wire.append({"role": "user", "content": [result]})
    return "\n\n".join(part for part in system_parts if part), wire


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
    def __init__(self, name_aliases: Mapping[str, str] | None = None) -> None:
        self.name_aliases = name_aliases or {}
        self.blocks: OrderedDict[int, _Block] = OrderedDict()
        self.usage = ModelUsage()
        self.response_id: str | None = None
        self.model: str | None = None
        self.stop_reason: str | None = None
        self.completed = False
        self.text_open: set[int] = set()
        self.thinking_open: set[int] = set()
        self.tool_open: set[int] = set()

    def consume(self, event_type: str, payload: Mapping[str, object]) -> list[ModelStreamEvent]:
        events: list[ModelStreamEvent] = []
        if event_type == "message_start":
            message = payload.get("message")
            if isinstance(message, Mapping):
                self.response_id = message.get("id") if isinstance(message.get("id"), str) else None
                self.model = message.get("model") if isinstance(message.get("model"), str) else None
                self.usage = _usage(message.get("usage"))
        elif event_type == "content_block_start":
            index = _int(payload.get("index"))
            raw = payload.get("content_block")
            if not isinstance(raw, Mapping):
                return events
            kind = str(raw.get("type", ""))
            block = _Block(kind=kind)
            if kind == "tool_use":
                block.id = str(raw.get("id", ""))
                block.name = str(raw.get("name", ""))
                if "input" in raw:
                    block.arguments = json.dumps(raw["input"], ensure_ascii=False, separators=(",", ":"))
                self.tool_open.add(index)
                events.append(ModelToolCallStart(block.id or f"call-{index}", block.name))
            elif kind == "thinking":
                self.thinking_open.add(index)
                events.append(ModelThinkingStart())
                block.signature = raw.get("signature") if isinstance(raw.get("signature"), str) else None
            elif kind == "redacted_thinking":
                block.signature = raw.get("data") if isinstance(raw.get("data"), str) else None
                block.redacted = True
            elif kind == "text":
                self.text_open.add(index)
                events.append(ModelTextStart())
                block.text = str(raw.get("text", ""))
            self.blocks[index] = block
        elif event_type == "content_block_delta":
            index = _int(payload.get("index"))
            block = self.blocks.get(index)
            delta = payload.get("delta")
            if block is None or not isinstance(delta, Mapping):
                return events
            kind = delta.get("type")
            if kind == "text_delta" and isinstance(delta.get("text"), str):
                block.text += delta["text"]
                events.append(ModelTextDelta(delta["text"]))
            elif kind == "thinking_delta" and isinstance(delta.get("thinking"), str):
                block.text += delta["thinking"]
                events.append(ModelThinkingDelta(delta["thinking"]))
            elif kind == "signature_delta" and isinstance(delta.get("signature"), str):
                block.signature = (block.signature or "") + delta["signature"]
            elif kind == "input_json_delta" and isinstance(delta.get("partial_json"), str):
                if not block.arguments_started:
                    block.arguments = ""
                    block.arguments_started = True
                block.arguments += delta["partial_json"]
                events.append(ModelToolCallDelta(block.id or f"call-{index}", delta["partial_json"]))
        elif event_type == "message_delta":
            delta = payload.get("delta")
            if isinstance(delta, Mapping) and isinstance(delta.get("stop_reason"), str):
                self.stop_reason = delta["stop_reason"]
            self.usage = ModelUsage(
                input_tokens=self.usage.input_tokens,
                output_tokens=_int(payload.get("usage", {}).get("output_tokens")) if isinstance(payload.get("usage"), Mapping) else self.usage.output_tokens,
                cache_read_tokens=self.usage.cache_read_tokens,
                cache_write_tokens=self.usage.cache_write_tokens,
            )
        elif event_type == "content_block_stop":
            index = _int(payload.get("index"))
            if index in self.text_open:
                self.text_open.remove(index)
                events.append(ModelTextEnd())
            if index in self.thinking_open:
                self.thinking_open.remove(index)
                events.append(ModelThinkingEnd())
        elif event_type == "message_stop":
            self.completed = True
        return events

    def finish(self) -> tuple[list[ModelStreamEvent], ModelAssistantMessage | None, str | None]:
        if not self.completed:
            return [], None, "模型流式响应未正常结束"
        blocks: list[ModelContentBlock] = []
        events: list[ModelStreamEvent] = []
        for block in self.blocks.values():
            if block.kind == "text":
                blocks.append(ModelTextBlock(block.text))
            elif block.kind in {"thinking", "redacted_thinking"}:
                blocks.append(ModelThinkingBlock(block.text, block.signature, redacted=block.redacted))
            elif block.kind == "tool_use":
                try:
                    arguments = json_object(block.arguments)
                except ValueError as error:
                    return events, None, str(error)
                call = ModelToolCallBlock(block.id or f"call-{len(blocks)}", self.name_aliases.get(block.name, block.name), arguments)
                blocks.append(call)
                events.append(ModelToolCallEnd(call))
        reason = {
            "end_turn": "stop",
            "stop_sequence": "stop",
            "tool_use": "tool_use",
            "max_tokens": "length",
        }.get(self.stop_reason or "", self.stop_reason or ("tool_use" if any(isinstance(item, ModelToolCallBlock) for item in blocks) else "stop"))
        message = ModelAssistantMessage(
            content=tuple(blocks),
            metadata=ModelResponseMetadata(
                provider="anthropic",
                api="anthropic-messages",
                model=self.model,
                response_id=self.response_id,
                usage=self.usage,
                stop_reason=reason,
                timestamp=int(time.time() * 1000),
            ),
        )
        return events, message, None


class AnthropicMessagesAdapter:
    def __init__(self, timeout: float | None = None, options: AnthropicMessagesOptions | None = None) -> None:
        self.timeout = timeout
        self.options = options or AnthropicMessagesOptions()

    def _prepare(
        self,
        messages: Sequence[ModelMessage],
        configuration: ModelConfiguration,
        tools: Sequence[Mapping[str, object]],
        *,
        max_tokens: int | None,
        stream: bool,
    ) -> tuple[dict[str, object], dict[str, str], dict[str, str]]:
        transformed = transform_messages(
            messages,
            capabilities=self.options.capabilities,
            target_provider="anthropic",
            target_api="anthropic-messages",
            target_model=configuration.model,
        )
        normalized = normalize_tools(effective_tools(transformed, tools))
        aliases, reverse = compatible_tool_names(normalized, transformed)
        transformed = remap_message_tool_names(transformed, aliases)
        system, wire_messages = _messages_to_wire(transformed, aliases)
        payload: dict[str, object] = {
            "model": configuration.model,
            "max_tokens": max_tokens or self.options.max_tokens,
            "messages": wire_messages,
            "stream": stream,
        }
        if system:
            payload["system"] = system
        if normalized:
            payload["tools"] = [_tool_wire(tool, aliases) for tool in normalized]
        headers = {
            "x-api-key": configuration.apiKey,
            "anthropic-version": self.options.version,
            "content-type": "application/json",
        }
        return payload, headers, reverse

    async def stream_messages(
        self,
        messages: Sequence[ModelMessage],
        configuration: ModelConfiguration,
        tools: Sequence[Mapping[str, object]] = (),
        *,
        max_tokens: int | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        payload, headers, name_aliases = self._prepare(messages, configuration, tools, max_tokens=max_tokens, stream=True)
        accumulator = _Accumulator(name_aliases)
        yield ModelStreamStart()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream(
                    "POST",
                    f"{configuration.baseUrl.rstrip('/')}/messages",
                    headers=headers,
                    json=payload,
                ) as response:
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
                        if not isinstance(data, Mapping):
                            continue
                        if event_type == "error" or data.get("type") == "error":
                            error = data.get("error", data)
                            message = error.get("message") if isinstance(error, Mapping) else str(error)
                            if configuration.apiKey:
                                message = str(message).replace(configuration.apiKey, "***")
                            yield ModelStreamError(str(message or "模型服务返回错误"))
                            return
                        actual_type = str(data.get("type", event_type))
                        for event in accumulator.consume(actual_type, data):
                            yield event
        except httpx.HTTPStatusError as error:
            detail = _response_error_detail(error.response, configuration.apiKey)
            message = f"HTTP {error.response.status_code} {error.response.reason_phrase}"
            if detail:
                message += f": {detail}"
            yield ModelStreamError(message)
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

    async def complete_messages(
        self,
        messages: Sequence[ModelMessage],
        configuration: ModelConfiguration,
        tools: Sequence[Mapping[str, object]] = (),
        *,
        max_tokens: int | None = None,
    ) -> ModelAssistantMessage:
        payload, headers, name_aliases = self._prepare(messages, configuration, tools, max_tokens=max_tokens, stream=False)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{configuration.baseUrl.rstrip('/')}/messages",
                headers=headers,
                json=payload,
            )
            await _raise_for_status_with_details(response, configuration.apiKey)
        data = response.json()
        if not isinstance(data, Mapping):
            raise RuntimeError("模型响应不是 JSON 对象")
        error = _payload_error_detail(data, configuration.apiKey)
        if error:
            raise RuntimeError(f"模型响应失败: {error}")
        if not isinstance(data.get("content"), list):
            raise RuntimeError("模型响应缺少 content 内容块")
        accumulator = _Accumulator(name_aliases)
        accumulator.response_id = data.get("id") if isinstance(data.get("id"), str) else None
        accumulator.model = data.get("model") if isinstance(data.get("model"), str) else None
        accumulator.usage = _usage(data.get("usage"))
        accumulator.stop_reason = data.get("stop_reason") if isinstance(data.get("stop_reason"), str) else None
        for index, block in enumerate(data["content"]):
            if not isinstance(block, Mapping):
                continue
            kind = str(block.get("type", ""))
            item = _Block(
                kind=kind,
                id=str(block.get("id", "")),
                name=str(block.get("name", "")),
                text=str(block.get("text", block.get("thinking", ""))),
                arguments=json.dumps(block.get("input", {}), ensure_ascii=False, separators=(",", ":")),
                signature=(
                    block.get("data") if kind == "redacted_thinking" and isinstance(block.get("data"), str)
                    else block.get("signature") if isinstance(block.get("signature"), str)
                    else None
                ),
                redacted=kind == "redacted_thinking",
            )
            accumulator.blocks[index] = item
        accumulator.completed = True
        _, message, error = accumulator.finish()
        if message is None:
            raise RuntimeError(error or "模型响应无法完成")
        return message

    async def stream_rich_messages(self, *args, **kwargs) -> AsyncIterator[ModelStreamEvent]:
        async for event in self.stream_messages(*args, **kwargs):
            yield event
