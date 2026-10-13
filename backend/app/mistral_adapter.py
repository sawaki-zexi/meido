"""Native Mistral Conversations wire adapter.

Mistral's endpoint resembles OpenAI at a glance, but its conversation
messages, image parts, tool results and streaming events have their own
normalization rules. The conversion lives here so a Mistral configuration
cannot accidentally inherit another provider's payload assumptions.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
import json
import time

import httpx

from .message_transform import transform_messages
from .model_adapter import _raise_for_status_with_details, _response_error_detail
from .model_protocol import compatible_tool_names, effective_tools, json_object, normalize_tools, remap_message_tool_names, safe_tool_id, tool_result_text
from .model_types import (
    ModelAssistantMessage, ModelCapabilities, ModelContentBlock, ModelImageBlock,
    ModelMessage, ModelResponseMetadata, ModelStreamDone, ModelStreamError,
    ModelStreamEvent, ModelStreamStart, ModelTextBlock, ModelTextDelta,
    ModelTextEnd, ModelTextStart, ModelThinkingBlock, ModelThinkingDelta,
    ModelThinkingEnd, ModelThinkingStart, ModelToolCallBlock,
    ModelToolCallDelta, ModelToolCallEnd, ModelToolCallStart,
    ModelToolResultMessage, ModelUserMessage, ModelSystemMessage, ModelUsage,
)
from .models import ModelConfiguration


_CAPABILITIES = ModelCapabilities(
    supports_images=True,
    supports_reasoning=True,
    preserve_thinking_signatures=False,
    tool_call_id_pattern=r"[A-Za-z0-9_-]+",
    max_tool_call_id_length=9,
)


def _configuration(configuration: ModelConfiguration) -> ModelConfiguration:
    base_url = configuration.baseUrl.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"
    return configuration.model_copy(update={"baseUrl": base_url})


def _text(blocks: Sequence[object]) -> str:
    return "".join(block.text for block in blocks if isinstance(block, ModelTextBlock))


def _content(blocks: Sequence[object]) -> str | list[dict[str, object]]:
    parts: list[dict[str, object]] = []
    for block in blocks:
        if isinstance(block, ModelTextBlock):
            parts.append({"type": "text", "text": block.text})
        elif isinstance(block, ModelImageBlock):
            parts.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{block.media_type};base64,{block.data_base64}",
                    "detail": block.detail,
                },
            })
    if not parts:
        return ""
    if all(part["type"] == "text" for part in parts):
        return "".join(str(part["text"]) for part in parts)
    return parts


def _messages_to_wire(messages: Sequence[ModelMessage]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for message in messages:
        if isinstance(message, ModelSystemMessage):
            result.append({"role": "system", "content": _text(message.content)})
            continue
        if isinstance(message, ModelUserMessage):
            result.append({"role": "user", "content": _content(message.content)})
            continue
        if isinstance(message, ModelAssistantMessage):
            item: dict[str, object] = {"role": "assistant", "content": _content(message.content)}
            thinking = "".join(block.text for block in message.content if isinstance(block, ModelThinkingBlock))
            if thinking:
                item["reasoning_content"] = thinking
            calls = [block for block in message.content if isinstance(block, ModelToolCallBlock)]
            if calls:
                item["tool_calls"] = [{
                    "id": safe_tool_id(call.id, max_length=9),
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":")),
                    },
                } for call in calls]
            result.append(item)
            continue
        if isinstance(message, ModelToolResultMessage):
            result.append({
                "role": "tool",
                "tool_call_id": safe_tool_id(message.tool_call_id, max_length=9),
                "content": tool_result_text(message.content),
            })
    return result


def _tools_to_wire(tools: Sequence[Mapping[str, object]], aliases: Mapping[str, str]) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for tool in normalize_tools(tools):
        name = aliases.get(str(tool["name"]), str(tool["name"]))
        output.append({
            "type": "function",
            "function": {
                "name": name,
                "description": str(tool.get("description", "")),
                "parameters": dict(tool["parameters"]),
            },
        })
    return output


def _usage(value: object) -> ModelUsage:
    if not isinstance(value, Mapping):
        return ModelUsage()
    prompt_details = value.get("prompt_tokens_details")
    completion_details = value.get("completion_tokens_details")
    return ModelUsage(
        input_tokens=_number(value.get("prompt_tokens")),
        output_tokens=_number(value.get("completion_tokens")),
        cache_read_tokens=_number(prompt_details.get("cached_tokens")) if isinstance(prompt_details, Mapping) else 0,
        reasoning_tokens=_number(completion_details.get("reasoning_tokens")) if isinstance(completion_details, Mapping) else 0,
    )


def _number(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class MistralConversationsAdapter:
    def __init__(self, timeout: float | None = None) -> None:
        self.timeout = timeout

    def _prepare(self, messages, configuration, tools, *, stream: bool, max_tokens: int | None):
        configuration = _configuration(configuration)
        transformed = transform_messages(
            messages,
            capabilities=_CAPABILITIES,
            target_provider=configuration.provider,
            target_api="mistral-conversations",
            target_model=configuration.model,
        )
        normalized = normalize_tools(effective_tools(transformed, tools))
        aliases, reverse = compatible_tool_names(normalized, transformed)
        transformed = remap_message_tool_names(transformed, aliases)
        payload: dict[str, object] = {
            "model": configuration.model,
            "messages": _messages_to_wire(transformed),
            "stream": stream,
        }
        wire_tools = _tools_to_wire(normalized, aliases)
        if wire_tools:
            payload["tools"] = wire_tools
            payload["tool_choice"] = "auto"
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        headers = {"Authorization": f"Bearer {configuration.apiKey}", "content-type": "application/json"}
        return configuration, payload, headers, reverse

    async def stream_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check: Callable[[], None] | None = None) -> AsyncIterator[ModelStreamEvent]:
        configuration, payload, headers, aliases = self._prepare(messages, configuration, tools, stream=True, max_tokens=max_tokens)
        text = ""
        thinking = ""
        calls: OrderedDict[str, dict[str, str]] = OrderedDict()
        block_order: list[tuple[str, str]] = []
        response_id = None
        response_model = None
        usage = ModelUsage()
        stop_reason = None
        completed = False
        text_open = thinking_open = False
        yield ModelStreamStart()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream("POST", f"{configuration.baseUrl}/chat/completions", headers=headers, json=payload) as response:
                    await _raise_for_status_with_details(response, configuration.apiKey)
                    data: list[str] = []
                    async for line in response.aiter_lines():
                        if cancel_check:
                            cancel_check()
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip())
                            continue
                        if line.strip() or not data:
                            continue
                        raw = "\n".join(data).strip()
                        data.clear()
                        if raw == "[DONE]":
                            completed = True
                            break
                        try:
                            chunk = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(chunk, Mapping):
                            continue
                        if chunk.get("error"):
                            error = chunk["error"]
                            message = error.get("message") if isinstance(error, Mapping) else str(error)
                            detail = str(message or "Mistral 返回错误")
                            if configuration.apiKey:
                                detail = detail.replace(configuration.apiKey, "***")
                            yield ModelStreamError(detail)
                            return
                        response_id = chunk.get("id") if isinstance(chunk.get("id"), str) else response_id
                        response_model = chunk.get("model") if isinstance(chunk.get("model"), str) else response_model
                        if chunk.get("usage") is not None:
                            usage = _usage(chunk["usage"])
                        choices = chunk.get("choices")
                        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], Mapping) else {}
                        finish = choice.get("finish_reason")
                        if isinstance(finish, str) and finish:
                            stop_reason = {"stop": "stop", "tool_calls": "tool_use", "length": "length"}.get(finish, finish)
                        delta = choice.get("delta") if isinstance(choice, Mapping) else {}
                        if not isinstance(delta, Mapping):
                            continue
                        value = delta.get("content")
                        if isinstance(value, str) and value:
                            if not text_open:
                                text_open = True
                                block_order.append(("text", "text"))
                                yield ModelTextStart()
                            text += value
                            yield ModelTextDelta(value)
                        reason = delta.get("reasoning_content", delta.get("reasoning"))
                        if isinstance(reason, str) and reason:
                            if not thinking_open:
                                thinking_open = True
                                block_order.append(("thinking", "thinking"))
                                yield ModelThinkingStart()
                            thinking += reason
                            yield ModelThinkingDelta(reason)
                        raw_calls = delta.get("tool_calls")
                        if isinstance(raw_calls, list):
                            for raw_call in raw_calls:
                                if not isinstance(raw_call, Mapping):
                                    continue
                                key = str(raw_call.get("index", len(calls)))
                                if key not in calls:
                                    calls[key] = {"id": "", "name": "", "arguments": ""}
                                    block_order.append(("tool", key))
                                call = calls[key]
                                if isinstance(raw_call.get("id"), str):
                                    call["id"] += raw_call["id"]
                                function = raw_call.get("function")
                                if not isinstance(function, Mapping):
                                    continue
                                if isinstance(function.get("name"), str):
                                    call["name"] += function["name"]
                                arguments = function.get("arguments")
                                if isinstance(arguments, str):
                                    call["arguments"] += arguments
                                elif isinstance(arguments, Mapping):
                                    call["arguments"] += json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
                                if not call.get("started"):
                                    call["started"] = "1"
                                    yield ModelToolCallStart(call["id"] or f"call-{key}", call["name"])
                                fragment = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False, separators=(",", ":")) if isinstance(arguments, Mapping) else ""
                                if fragment:
                                    yield ModelToolCallDelta(call["id"] or f"call-{key}", fragment)
                    if data and not completed and "[DONE]" in "\n".join(data):
                        completed = True
            if not completed:
                yield ModelStreamError("模型流式响应未收到 [DONE] 终止事件")
                return
            if text_open:
                yield ModelTextEnd()
            if thinking_open:
                yield ModelThinkingEnd()
            blocks: list[ModelContentBlock] = []
            for kind, key in block_order:
                if kind == "text":
                    blocks.append(ModelTextBlock(text))
                elif kind == "thinking":
                    blocks.append(ModelThinkingBlock(thinking))
                else:
                    call = calls[key]
                    try:
                        arguments = json_object(call["arguments"], label=f"模型工具参数: {call['name'] or key}")
                    except ValueError as error:
                        yield ModelStreamError(str(error))
                        return
                    name = aliases.get(call["name"], call["name"])
                    block = ModelToolCallBlock(safe_tool_id(call["id"] or f"call-{key}", max_length=9), name, arguments)
                    blocks.append(block)
                    yield ModelToolCallEnd(block)
            yield ModelStreamDone(ModelAssistantMessage(content=tuple(blocks), metadata=ModelResponseMetadata(
                provider=configuration.provider, api="mistral-conversations", model=response_model or configuration.model,
                response_id=response_id, usage=usage, stop_reason=stop_reason or ("tool_use" if calls else "stop"), timestamp=int(time.time() * 1000),
            )))
        except httpx.HTTPStatusError as error:
            detail = _response_error_detail(error.response, configuration.apiKey)
            message = f"HTTP {error.response.status_code} {error.response.reason_phrase}"
            yield ModelStreamError(f"{message}: {detail}" if detail else message)
        except RuntimeError as error:
            if str(error) == "agent run cancelled":
                raise
            yield ModelStreamError(str(error))
        except Exception as error:
            yield ModelStreamError(str(error).replace(configuration.apiKey, "***") if configuration.apiKey else str(error))

    async def complete_messages(self, messages, configuration, tools=(), *, max_tokens=None):
        configuration, payload, headers, aliases = self._prepare(messages, configuration, tools, stream=False, max_tokens=max_tokens)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{configuration.baseUrl}/chat/completions", headers=headers, json=payload)
            await _raise_for_status_with_details(response, configuration.apiKey)
        data = response.json()
        if not isinstance(data, Mapping):
            raise RuntimeError("模型响应不是 JSON 对象")
        choices = data.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], Mapping) else {}
        raw = choice.get("message") if isinstance(choice, Mapping) and isinstance(choice.get("message"), Mapping) else {}
        blocks: list[ModelContentBlock] = []
        if isinstance(raw.get("reasoning_content"), str) and raw["reasoning_content"]:
            blocks.append(ModelThinkingBlock(raw["reasoning_content"]))
        if isinstance(raw.get("content"), str) and raw["content"]:
            blocks.append(ModelTextBlock(raw["content"]))
        for raw_call in raw.get("tool_calls", []) if isinstance(raw.get("tool_calls"), list) else []:
            if not isinstance(raw_call, Mapping):
                continue
            function = raw_call.get("function") if isinstance(raw_call.get("function"), Mapping) else {}
            arguments = json_object(function.get("arguments", "{}"), label="模型工具参数")
            name = aliases.get(str(function.get("name", "")), str(function.get("name", "")))
            blocks.append(ModelToolCallBlock(safe_tool_id(str(raw_call.get("id", "call-0")), max_length=9), name, arguments))
        finish = choice.get("finish_reason") if isinstance(choice, Mapping) else None
        reason = {"stop": "stop", "tool_calls": "tool_use", "length": "length"}.get(str(finish), str(finish or "stop"))
        return ModelAssistantMessage(content=tuple(blocks), metadata=ModelResponseMetadata(
            provider=configuration.provider, api="mistral-conversations", model=str(data.get("model") or configuration.model),
            response_id=data.get("id") if isinstance(data.get("id"), str) else None, usage=_usage(data.get("usage")), stop_reason=reason, timestamp=int(time.time() * 1000),
        ))

    async def stream_rich_messages(self, *args, **kwargs) -> AsyncIterator[ModelStreamEvent]:
        async for event in self.stream_messages(*args, **kwargs):
            yield event
