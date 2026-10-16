"""Adapter for Pi's provider-neutral ``/messages`` SSE contract."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import aclosing
from dataclasses import asdict
import json
import time

import httpx

from .request_budget import image_budget_client_kwargs, budgeted_model_stream, budgeted_model_completion

from .model_adapter import _raise_for_status_with_details, _response_error_detail
from .message_transform import transform_messages
from .model_protocol import copy_json, effective_tools, model_cost, normalize_tools, safe_tool_id
from .model_types import (
    ModelAssistantMessage, ModelCapabilities, ModelContentBlock, ModelImageBlock, ModelMessage,
    ModelResponseMetadata, ModelStreamDone, ModelStreamError, ModelStreamEvent,
    ModelStreamStart, ModelTextBlock, ModelTextDelta, ModelTextEnd,
    ModelTextStart, ModelThinkingBlock, ModelThinkingDelta, ModelThinkingEnd,
    ModelThinkingStart, ModelToolCallBlock, ModelToolCallDelta, ModelToolCallEnd,
    ModelToolCallStart, ModelUserMessage,
    ModelSystemMessage, ModelUsage,
)
from ..models import ModelConfiguration


def _message(message: ModelMessage) -> dict[str, object]:
    if isinstance(message, ModelSystemMessage):
        output: dict[str, object] = {
            "role": "system",
            "content": "\n\n".join(block.text for block in message.content),
        }
        if message.timestamp is not None:
            output["timestamp"] = message.timestamp
        if message.tools_added:
            output["toolsAdded"] = [
                {
                    "name": item["name"],
                    "description": item.get("description", ""),
                    "parameters": item["parameters"],
                }
                for item in message.tools_added
            ]
        if message.tools_removed:
            output["toolsRemoved"] = list(message.tools_removed)
        return output
    if isinstance(message, ModelUserMessage):
        output = {"role": "user", "content": [_block(block) for block in message.content]}
        if message.timestamp is not None:
            output["timestamp"] = message.timestamp
        return output
    if isinstance(message, ModelAssistantMessage):
        output = {"role": "assistant", "content": [_block(block) for block in message.content], "metadata": copy_json(asdict(message.metadata))}
        if message.timestamp is not None:
            output["timestamp"] = message.timestamp
        return output
    output = {
        "role": "toolResult",
        "toolCallId": safe_tool_id(message.tool_call_id),
        "toolName": message.tool_name,
        "isError": message.is_error,
        "content": [_block(block) for block in message.content],
    }
    if message.details is not None:
        output["details"] = copy_json(message.details)
    if message.usage != ModelUsage():
        output["usage"] = copy_json(asdict(message.usage))
    if message.timestamp is not None:
        output["timestamp"] = message.timestamp
    return output


def _block(block: object) -> dict[str, object]:
    if isinstance(block, ModelTextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ModelThinkingBlock):
        return {"type": "thinking", "thinking": block.text, "thinkingSignature": block.signature}
    if isinstance(block, ModelImageBlock):
        return {"type": "image", "mimeType": block.media_type, "data": block.data_base64}
    if isinstance(block, ModelToolCallBlock):
        return {"type": "toolCall", "id": safe_tool_id(block.id), "name": block.name, "arguments": dict(block.arguments)}
    return {"type": "text", "text": ""}


def _usage(value: object) -> ModelUsage:
    if not isinstance(value, Mapping):
        return ModelUsage()
    return ModelUsage(
        input_tokens=_number(value.get("input", value.get("input_tokens"))),
        output_tokens=_number(value.get("output", value.get("output_tokens"))),
        cache_read_tokens=_number(value.get("cacheRead", value.get("cache_read_tokens"))),
        cache_write_tokens=_number(value.get("cacheWrite", value.get("cache_write_tokens"))),
        reasoning_tokens=_number(value.get("reasoning", value.get("reasoning_tokens"))),
        cost=model_cost(value),
    )


def _number(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class PiMessagesAdapter:
    def __init__(self, timeout: float | None = None) -> None:
        self.timeout = timeout

    def _prepare(self, messages: Sequence[ModelMessage], configuration: ModelConfiguration, tools: Sequence[Mapping[str, object]], *, stream: bool, max_tokens: int | None) -> tuple[str, dict[str, str], dict[str, object]]:
        messages = transform_messages(
            messages,
            capabilities=ModelCapabilities(
                supports_images=True,
                supports_reasoning=True,
                preserve_thinking_signatures=True,
                tool_call_id_pattern=r"[A-Za-z0-9_-]+",
                max_tool_call_id_length=64,
            ),
            target_provider=configuration.provider,
            target_api="pi-messages",
            target_model=configuration.model,
        )
        context = {"messages": [_message(message) for message in messages]}
        options: dict[str, object] = {"stream": stream}
        if max_tokens is not None:
            options["maxTokens"] = max_tokens
        normalized = normalize_tools(effective_tools(messages, tools))
        if normalized:
            first = context["messages"][0] if context["messages"] and context["messages"][0].get("role") == "system" else None
            if first is None:
                first = {"role": "system", "content": ""}
                context["messages"].insert(0, first)
            first["toolsAdded"] = [
                {"name": item["name"], "description": item["description"], "parameters": item["parameters"]}
                for item in normalized
            ]
        return f"{configuration.baseUrl.rstrip('/')}/messages", {
            "authorization": f"Bearer {configuration.apiKey}",
            "accept": "text/event-stream",
            "content-type": "application/json",
        }, {"model": configuration.model, "context": context, "options": options}

    @budgeted_model_stream
    async def stream_messages(self, messages: Sequence[ModelMessage], configuration: ModelConfiguration, tools: Sequence[Mapping[str, object]] = (), *, max_tokens: int | None = None, cancel_check: Callable[[], None] | None = None) -> AsyncGenerator[ModelStreamEvent, None]:
        url, headers, payload = self._prepare(messages, configuration, tools, stream=True, max_tokens=max_tokens)
        blocks: list[ModelContentBlock] = []
        text_parts: dict[int, str] = {}
        thinking_parts: dict[int, str] = {}
        tool_parts: dict[int, dict[str, str]] = {}
        usage = ModelUsage()
        response_id = None
        reason = "stop"
        terminal = False
        yield ModelStreamStart()
        try:
            async with httpx.AsyncClient(timeout=self.timeout, **image_budget_client_kwargs("pi-messages")) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    await _raise_for_status_with_details(response, configuration.apiKey)
                    event_name = ""
                    data_lines: list[str] = []
                    async for line in response.aiter_lines():
                        if cancel_check:
                            cancel_check()
                        if line.startswith("event:"):
                            event_name = line[6:].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                        elif not line.strip() and data_lines:
                            raw = "\n".join(data_lines)
                            data_lines.clear()
                            if raw == "[DONE]":
                                continue
                            try:
                                event = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            if isinstance(event, Mapping):
                                if event.get("type") in {"done", "error"}:
                                    terminal = True
                                consumed = self._consume(event_name or str(event.get("type", "")), event, blocks, text_parts, thinking_parts, tool_parts)
                                consumed_items = consumed if isinstance(consumed, list) else [consumed]
                                for item in consumed_items:
                                    if item == ():
                                        continue
                                    if isinstance(item, tuple):
                                        yield item[0]
                                        if item[1] is not None:
                                            usage = item[1]
                                    else:
                                        yield item
                                if event.get("type") == "done":
                                    reason = {"toolUse": "tool_use"}.get(str(event.get("reason")), str(event.get("reason") or "stop"))
                                    usage = _usage(event.get("usage"))
                                    response_id = event.get("responseId") if isinstance(event.get("responseId"), str) else response_id
                                if event.get("type") == "error":
                                    detail = str(event.get("errorMessage") or "Pi Messages 请求失败")
                                    if configuration.apiKey:
                                        detail = detail.replace(configuration.apiKey, "***")
                                    yield ModelStreamError(detail)
                                    return
                    if data_lines:
                        try:
                            event = json.loads("\n".join(data_lines))
                        except json.JSONDecodeError:
                            event = None
                        if isinstance(event, Mapping) and event.get("type") == "done":
                            terminal = True
                            reason = {"toolUse": "tool_use"}.get(str(event.get("reason")), str(event.get("reason") or "stop"))
                            usage = _usage(event.get("usage"))
            if not terminal:
                yield ModelStreamError("模型流式响应未收到 done/error 终止事件")
                return
            yield ModelStreamDone(ModelAssistantMessage(content=tuple(blocks), metadata=ModelResponseMetadata(provider=configuration.provider, api="pi-messages", model=configuration.model, response_id=response_id, usage=usage, stop_reason=reason, timestamp=int(time.time() * 1000))))
        except httpx.HTTPStatusError as error:
            detail = _response_error_detail(error.response, configuration.apiKey)
            yield ModelStreamError(f"HTTP {error.response.status_code} {error.response.reason_phrase}: {detail}" if detail else f"HTTP {error.response.status_code} {error.response.reason_phrase}")
        except RuntimeError as error:
            if str(error) == "agent run cancelled":
                raise
            yield ModelStreamError(str(error))
        except Exception as error:
            yield ModelStreamError(str(error).replace(configuration.apiKey, "***") if configuration.apiKey else str(error))

    def _consume(self, event_type: str, event: Mapping[str, object], blocks: list[ModelContentBlock], texts: dict[int, str], thinkings: dict[int, str], tools: dict[int, dict[str, str]]):
        kind = str(event.get("type") or event_type)
        index = event.get("contentIndex", 0)
        index = index if isinstance(index, int) else 0
        if kind == "text_start":
            texts.setdefault(index, "")
            blocks.append(ModelTextBlock(""))
            return (ModelTextStart(), None)
        if kind == "text_delta":
            delta = str(event.get("delta") or "")
            texts[index] = texts.get(index, "") + delta
            if blocks and isinstance(blocks[-1], ModelTextBlock):
                blocks[-1] = ModelTextBlock(texts[index])
            return (ModelTextDelta(delta), None)
        if kind == "text_end":
            return ModelTextEnd()
        if kind == "thinking_start":
            thinkings.setdefault(index, "")
            blocks.append(ModelThinkingBlock(""))
            return (ModelThinkingStart(), None)
        if kind == "thinking_delta":
            delta = str(event.get("delta") or "")
            thinkings[index] = thinkings.get(index, "") + delta
            if blocks and isinstance(blocks[-1], ModelThinkingBlock):
                blocks[-1] = ModelThinkingBlock(thinkings[index])
            return (ModelThinkingDelta(delta), None)
        if kind == "thinking_end":
            return ModelThinkingEnd()
        if kind == "toolcall_start":
            tool_id = str(event.get("id") or f"call-{index}")
            tools[index] = {"id": tool_id, "name": str(event.get("toolName") or ""), "arguments": ""}
            return ModelToolCallStart(tool_id, tools[index]["name"])
        if kind == "toolcall_delta":
            delta = str(event.get("delta") or "")
            tools.setdefault(index, {"id": f"call-{index}", "name": "", "arguments": ""})["arguments"] += delta
            return ModelToolCallDelta(tools[index]["id"], delta)
        if kind == "toolcall_end":
            raw_call = event.get("toolCall")
            if not isinstance(raw_call, Mapping):
                raw_call = tools.get(index, {})
            arguments = raw_call.get("arguments") if isinstance(raw_call.get("arguments"), Mapping) else {}
            call = ModelToolCallBlock(str(raw_call.get("id") or tools.get(index, {}).get("id") or f"call-{index}"), str(raw_call.get("name") or tools.get(index, {}).get("name") or ""), dict(arguments))
            blocks.append(call)
            return ModelToolCallEnd(call)
        return ()

    @budgeted_model_completion
    async def complete_messages(self, messages, configuration, tools=(), *, max_tokens=None) -> ModelAssistantMessage:
        events = [event async for event in self.stream_messages(messages, configuration, tools, max_tokens=max_tokens)]
        done = next((event for event in reversed(events) if isinstance(event, ModelStreamDone)), None)
        error = next((event for event in reversed(events) if isinstance(event, ModelStreamError)), None)
        if done:
            return done.message
        raise RuntimeError(error.error if error else "模型响应无法完成")

    async def stream_rich_messages(self, *args, **kwargs) -> AsyncGenerator[ModelStreamEvent, None]:
        async with aclosing(self.stream_messages(*args, **kwargs)) as stream:
            async for event in stream:
                yield event
