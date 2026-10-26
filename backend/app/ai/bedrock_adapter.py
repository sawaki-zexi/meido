"""Amazon Bedrock Converse adapter using Bedrock bearer API keys."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from contextlib import aclosing
import json
import struct
import time
from urllib.parse import quote

import httpx

from .request_budget import image_budget_client_kwargs, budgeted_model_stream, budgeted_model_completion

from .message_transform import transform_messages
from .model_adapter import _payload_error_detail, _raise_for_status_with_details
from .model_protocol import preserve_stop_reason
from .model_protocol import compatible_tool_names, effective_tools, model_cost, normalize_tools, remap_message_tool_names, safe_tool_id
from .model_types import (
    ModelAssistantMessage, ModelCapabilities, ModelContentBlock, ModelImageBlock, ModelMessage,
    ModelThinkingBlock,
    ModelResponseMetadata, ModelStreamDone, ModelStreamEvent, ModelStreamStart,
    ModelStreamError, ModelTextBlock, ModelTextDelta, ModelTextEnd,
    ModelTextStart, ModelToolCallBlock, ModelToolCallDelta, ModelToolCallEnd,
    ModelToolCallStart, ModelToolResultMessage, ModelUserMessage,
    ModelThinkingDelta, ModelThinkingEnd, ModelThinkingStart,
    ModelSystemMessage, ModelUsage,
)
from ..models import ModelConfiguration


def _capabilities() -> ModelCapabilities:
    return ModelCapabilities(
        supports_images=True,
        supports_reasoning=True,
        preserve_thinking_signatures=True,
        tool_call_id_pattern=r"[A-Za-z0-9_-]+",
        max_tool_call_id_length=64,
    )


def _supports_thinking_signature(model: str) -> bool:
    return "claude" in model.lower()


def _sanitize_document(value: object, *, path: str = "$") -> object:
    """Copy a tool document and reject object keys Bedrock cannot encode."""
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"Bedrock 工具参数对象键不能为空: {path}")
            result[key] = _sanitize_document(item, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize_document(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    return value


def _content(blocks: Sequence[object], *, supports_thinking_signature: bool = True) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for block in blocks:
        if isinstance(block, ModelTextBlock):
            output.append({"text": block.text})
        elif isinstance(block, ModelThinkingBlock):
            if block.text:
                reasoning: dict[str, object] = {"text": block.text}
                if supports_thinking_signature and block.signature:
                    reasoning["signature"] = block.signature
                output.append({"reasoningContent": {"reasoningText": reasoning}})
        elif isinstance(block, ModelImageBlock):
            fmt = block.media_type.split("/", 1)[-1].lower()
            if fmt == "jpeg":
                fmt = "jpeg"
            elif fmt not in {"png", "gif", "webp"}:
                output.append({"text": f"[image omitted: {block.media_type or 'unknown'}]"})
                continue
            output.append({"image": {"format": fmt, "source": {"bytes": block.data_base64}}})
    return output or [{"text": ""}]


def _wire_messages(
    messages: Sequence[ModelMessage],
    *,
    supports_thinking_signature: bool = True,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    system: list[dict[str, object]] = []
    result: list[dict[str, object]] = []
    for message in messages:
        if isinstance(message, ModelSystemMessage):
            value = "\n\n".join(block.text for block in message.content)
            if value:
                system.append({"text": value})
        elif isinstance(message, ModelUserMessage):
            result.append({"role": "user", "content": _content(
                message.content,
                supports_thinking_signature=supports_thinking_signature,
            )})
        elif isinstance(message, ModelAssistantMessage):
            non_tool_blocks = tuple(block for block in message.content if not isinstance(block, ModelToolCallBlock))
            content = _content(
                non_tool_blocks,
                supports_thinking_signature=supports_thinking_signature,
            ) if non_tool_blocks else []
            content.extend({"toolUse": {
                "toolUseId": safe_tool_id(block.id),
                "name": block.name,
                "input": _sanitize_document(block.arguments),
            }} for block in message.content if isinstance(block, ModelToolCallBlock))
            if not content:
                continue
            result.append({"role": "assistant", "content": content})
        elif isinstance(message, ModelToolResultMessage):
            block = {"toolResult": {
                "toolUseId": safe_tool_id(message.tool_call_id),
                "status": "error" if message.is_error else "success",
                "content": _content(
                    message.content,
                    supports_thinking_signature=supports_thinking_signature,
                ),
            }}
            if (
                result
                and result[-1].get("role") == "user"
                and isinstance(result[-1].get("content"), list)
                and all(isinstance(item, Mapping) and "toolResult" in item for item in result[-1]["content"])
            ):
                result[-1]["content"].append(block)
            else:
                result.append({"role": "user", "content": [block]})
    return system, result


def _wire_tools(tools: Sequence[Mapping[str, object]], aliases: Mapping[str, str]) -> list[dict[str, object]]:
    return [{"toolSpec": {
        "name": aliases.get(str(item["name"]), str(item["name"])),
        "description": item["description"],
        "inputSchema": {"json": item["parameters"]},
    }} for item in normalize_tools(tools)]


class BedrockConverseAdapter:
    """The bearer-token endpoint returns one JSON response, projected as events."""

    def __init__(self, timeout: float | None = None) -> None:
        self.timeout = timeout

    def _prepare(self, messages: Sequence[ModelMessage], configuration: ModelConfiguration, tools: Sequence[Mapping[str, object]], max_tokens: int | None):
        messages = transform_messages(
            messages,
            capabilities=_capabilities(),
            target_provider=configuration.provider,
            target_api="bedrock-converse",
            target_model=configuration.model,
        )
        normalized = normalize_tools(effective_tools(messages, tools))
        aliases, reverse = compatible_tool_names(normalized, messages)
        messages = remap_message_tool_names(messages, aliases)
        system, wire = _wire_messages(
            messages,
            supports_thinking_signature=_supports_thinking_signature(configuration.model),
        )
        payload: dict[str, object] = {"messages": wire}
        if system:
            payload["system"] = system
        if max_tokens is not None:
            payload["inferenceConfig"] = {"maxTokens": max_tokens}
        tool_config = _wire_tools(normalized, aliases)
        if tool_config:
            payload["toolConfig"] = {"tools": tool_config}
        url = f"{configuration.baseUrl.rstrip('/')}/model/{quote(configuration.model, safe='')}/converse"
        return url, {"authorization": f"Bearer {configuration.apiKey}", "content-type": "application/json"}, payload, reverse

    async def _complete(self, messages, configuration, tools, *, max_tokens, cancel_check):
        if not configuration.apiKey:
            raise ValueError("Bedrock Converse 需要 AWS Bedrock API Key")
        if cancel_check:
            cancel_check()
        url, headers, payload, name_aliases = self._prepare(messages, configuration, tools, max_tokens)
        async with httpx.AsyncClient(timeout=self.timeout, **image_budget_client_kwargs("bedrock-converse")) as client:
            response = await client.post(url, headers=headers, json=payload)
            await _raise_for_status_with_details(response, configuration.apiKey)
        data = response.json()
        if not isinstance(data, Mapping):
            raise RuntimeError("模型响应不是 JSON 对象")
        error = _payload_error_detail(data, configuration.apiKey)
        if error:
            raise RuntimeError(f"模型响应失败: {error}")
        if not isinstance(data.get("output"), Mapping) or not isinstance(data["output"].get("message"), Mapping):
            raise RuntimeError("模型响应缺少 output.message")
        output = data["output"]
        message = output["message"]
        if not isinstance(message.get("content"), list):
            raise RuntimeError("模型响应缺少 message.content")
        content = message["content"]
        blocks: list[ModelContentBlock] = []
        for item in content:
            if not isinstance(item, Mapping):
                continue
            if isinstance(item.get("text"), str):
                blocks.append(ModelTextBlock(item["text"]))
            reasoning = item.get("reasoningContent")
            reasoning_text = reasoning.get("reasoningText") if isinstance(reasoning, Mapping) else None
            if isinstance(reasoning_text, Mapping) and isinstance(reasoning_text.get("text"), str):
                blocks.append(ModelThinkingBlock(
                    reasoning_text["text"],
                    reasoning_text.get("signature") if isinstance(reasoning_text.get("signature"), str) else None,
                ))
            tool = item.get("toolUse")
            if isinstance(tool, Mapping):
                args = tool.get("input") if isinstance(tool.get("input"), Mapping) else {}
                wire_name = str(tool.get("name") or "")
                blocks.append(ModelToolCallBlock(safe_tool_id(str(tool.get("toolUseId") or f"call-{len(blocks)}")), name_aliases.get(wire_name, wire_name), dict(args)))
        usage_data = data.get("usage") if isinstance(data.get("usage"), Mapping) else {}
        usage = ModelUsage(
            input_tokens=_nonnegative(usage_data.get("inputTokens")),
            output_tokens=_nonnegative(usage_data.get("outputTokens")),
            cache_read_tokens=_nonnegative(usage_data.get("cacheReadInputTokens")),
            cache_write_tokens=_nonnegative(usage_data.get("cacheWriteInputTokens")),
            cost=model_cost(usage_data),
        )
        stop = _stop_reason(data.get("stopReason"))
        return ModelAssistantMessage(content=tuple(blocks), metadata=ModelResponseMetadata(
            provider=configuration.provider, api="bedrock-converse", model=configuration.model,
            response_id=None, usage=usage, stop_reason=stop, timestamp=int(time.time() * 1000),
        ))

    @staticmethod
    async def _event_payloads(response: httpx.Response) -> AsyncIterator[Mapping[str, object]]:
        buffer = b""
        async for chunk in response.aiter_bytes():
            buffer += chunk
            while len(buffer) >= 12:
                total, headers_length = struct.unpack(">II", buffer[:8])
                if total < 16 or total > 16 * 1024 * 1024 or len(buffer) < total:
                    break
                payload_start = 12 + headers_length
                payload_end = total - 4
                if payload_start > payload_end:
                    raise RuntimeError("Bedrock Event Stream 帧长度无效")
                payload = buffer[payload_start:payload_end]
                buffer = buffer[total:]
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError as error:
                    raise RuntimeError("Bedrock Event Stream 负载不是 JSON") from error
                if isinstance(data, Mapping):
                    yield data
        if buffer:
            raise RuntimeError("Bedrock Event Stream 未完整结束")

    @budgeted_model_stream
    async def stream_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check: Callable[[], None] | None = None) -> AsyncGenerator[ModelStreamEvent, None]:
        if not configuration.apiKey:
            yield ModelStreamStart()
            yield ModelStreamError("Bedrock Converse 需要 AWS Bedrock API Key")
            return
        messages = transform_messages(
            messages,
            capabilities=_capabilities(),
            target_provider=configuration.provider,
            target_api="bedrock-converse",
            target_model=configuration.model,
        )
        normalized = normalize_tools(effective_tools(messages, tools))
        tool_aliases, name_aliases = compatible_tool_names(normalized, messages)
        messages = remap_message_tool_names(messages, tool_aliases)
        system, wire = _wire_messages(
            messages,
            supports_thinking_signature=_supports_thinking_signature(configuration.model),
        )
        payload: dict[str, object] = {"messages": wire}
        if system:
            payload["system"] = system
        if max_tokens is not None:
            payload["inferenceConfig"] = {"maxTokens": max_tokens}
        tool_config = _wire_tools(normalized, tool_aliases)
        if tool_config:
            payload["toolConfig"] = {"tools": tool_config}
        url = f"{configuration.baseUrl.rstrip('/')}/model/{quote(configuration.model, safe='')}/converse-stream"
        headers = {
            "authorization": f"Bearer {configuration.apiKey}",
            "accept": "application/vnd.amazon.eventstream",
            "content-type": "application/json",
        }
        text_parts: dict[int, str] = {}
        thinking_parts: dict[int, dict[str, str]] = {}
        tool_parts: dict[int, dict[str, str]] = {}
        usage = ModelUsage()
        response_id = None
        stop_reason: str | None = None
        terminal = False
        text_started = False
        thinking_started = False
        yield ModelStreamStart()
        try:
            async with httpx.AsyncClient(timeout=self.timeout, **image_budget_client_kwargs("bedrock-converse")) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    await _raise_for_status_with_details(response, configuration.apiKey)
                    async for event in self._event_payloads(response):
                        if cancel_check:
                            cancel_check()
                        if isinstance(event.get("contentBlockStart"), Mapping):
                            item = event["contentBlockStart"]
                            index = item.get("contentBlockIndex", 0)
                            index = index if isinstance(index, int) else 0
                            start = item.get("start") if isinstance(item.get("start"), Mapping) else {}
                            tool = start.get("toolUse") if isinstance(start, Mapping) else None
                            if isinstance(tool, Mapping):
                                call_id = safe_tool_id(str(tool.get("toolUseId") or f"call-{index}"))
                                wire_name = str(tool.get("name") or "")
                                name = name_aliases.get(wire_name, wire_name)
                                tool_parts[index] = {"id": call_id, "name": name, "arguments": ""}
                                yield ModelToolCallStart(call_id, name)
                        delta_item = event.get("contentBlockDelta")
                        if isinstance(delta_item, Mapping):
                            index = delta_item.get("contentBlockIndex", 0)
                            index = index if isinstance(index, int) else 0
                            delta = delta_item.get("delta") if isinstance(delta_item.get("delta"), Mapping) else {}
                            if isinstance(delta.get("text"), str):
                                value = delta["text"]
                                text_parts[index] = text_parts.get(index, "") + value
                                if not text_started:
                                    text_started = True
                                    yield ModelTextStart()
                                yield ModelTextDelta(value)
                            tool_delta = delta.get("toolUse")
                            if isinstance(tool_delta, Mapping) and isinstance(tool_delta.get("input"), str):
                                value = tool_delta["input"]
                                tool_parts.setdefault(index, {"id": f"call-{index}", "name": "", "arguments": ""})["arguments"] += value
                                yield ModelToolCallDelta(tool_parts[index]["id"], value)
                            reasoning_delta = delta.get("reasoningContent")
                            reasoning_text = reasoning_delta.get("reasoningText") if isinstance(reasoning_delta, Mapping) else None
                            if isinstance(reasoning_text, Mapping):
                                value = reasoning_text.get("text")
                                if isinstance(value, str):
                                    item = thinking_parts.setdefault(index, {"text": "", "signature": ""})
                                    item["text"] += value
                                    if not thinking_started:
                                        thinking_started = True
                                        yield ModelThinkingStart()
                                    yield ModelThinkingDelta(value)
                                signature = reasoning_text.get("signature")
                                if isinstance(signature, str):
                                    thinking_parts.setdefault(index, {"text": "", "signature": ""})["signature"] += signature
                        stop = event.get("messageStop")
                        if isinstance(stop, Mapping):
                            stop_reason = preserve_stop_reason(stop_reason, _stop_reason(stop.get("stopReason")))
                            terminal = True
                        metadata = event.get("metadata")
                        if isinstance(metadata, Mapping) and isinstance(metadata.get("usage"), Mapping):
                            usage_data = metadata["usage"]
                            usage = ModelUsage(
                                input_tokens=_nonnegative(usage_data.get("inputTokens")),
                                output_tokens=_nonnegative(usage_data.get("outputTokens")),
                                cache_read_tokens=_nonnegative(usage_data.get("cacheReadInputTokens")),
                                cache_write_tokens=_nonnegative(usage_data.get("cacheWriteInputTokens")),
                                cost=model_cost(usage_data),
                            )
                        if isinstance(event.get("messageStart"), Mapping):
                            response_id = event["messageStart"].get("messageId") if isinstance(event["messageStart"].get("messageId"), str) else response_id
            if not terminal:
                yield ModelStreamError("模型流式响应未收到 Bedrock messageStop")
                return
            blocks: list[ModelContentBlock] = []
            if text_started:
                yield ModelTextEnd()
            if thinking_started:
                yield ModelThinkingEnd()
            for index in sorted(set(text_parts) | set(thinking_parts) | set(tool_parts)):
                if index in text_parts:
                    blocks.append(ModelTextBlock(text_parts[index]))
                if index in thinking_parts:
                    item = thinking_parts[index]
                    blocks.append(ModelThinkingBlock(item["text"], item["signature"] or None))
                if index in tool_parts:
                    item = tool_parts[index]
                    try:
                        arguments = json.loads(item["arguments"] or "{}")
                    except json.JSONDecodeError:
                        yield ModelStreamError(f"模型工具参数不是有效 JSON: {item['name'] or index}")
                        return
                    if not isinstance(arguments, dict):
                        yield ModelStreamError(f"模型工具参数必须是 JSON 对象: {item['name'] or index}")
                        return
                    call = ModelToolCallBlock(item["id"], item["name"], arguments, arguments_complete=bool(item["arguments"].strip()))
                    blocks.append(call)
                    yield ModelToolCallEnd(call)
            message = ModelAssistantMessage(content=tuple(blocks), metadata=ModelResponseMetadata(
                provider=configuration.provider, api="bedrock-converse", model=configuration.model,
                response_id=response_id, usage=usage, stop_reason=stop_reason, timestamp=int(time.time() * 1000),
            ))
        except Exception as error:
            if isinstance(error, RuntimeError) and str(error) == "agent run cancelled":
                raise
            text = str(error).replace(configuration.apiKey, "***") if configuration.apiKey else str(error)
            yield ModelStreamError(text)
            return
        yield ModelStreamDone(message)

    @budgeted_model_completion
    async def complete_messages(self, messages, configuration, tools=(), *, max_tokens=None) -> ModelAssistantMessage:
        return await self._complete(messages, configuration, tools, max_tokens=max_tokens, cancel_check=None)

    async def stream_rich_messages(self, *args, **kwargs) -> AsyncGenerator[ModelStreamEvent, None]:
        async with aclosing(self.stream_messages(*args, **kwargs)) as stream:
            async for event in stream:
                yield event


def _nonnegative(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _stop_reason(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError("模型响应缺少 stopReason")
    mapped = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "tool_use": "tool_use",
        "max_tokens": "length",
        "model_context_window_exceeded": "length",
    }.get(value)
    if mapped is None:
        raise RuntimeError(f"模型响应包含未知 stopReason: {value}")
    return mapped
