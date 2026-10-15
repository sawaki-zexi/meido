"""Native OpenAI Responses API adapter."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
import hashlib
import json
import time

import httpx

from .message_transform import transform_messages
from .model_adapter import _raise_for_status_with_details, _response_error_detail
from .model_protocol import compatible_tool_names, effective_tools, json_object, model_cost, normalize_tools, remap_message_tool_names, tool_result_text
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
    output_index: int | None = None
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
        cost=model_cost(value),
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


def _same_connection(message: ModelAssistantMessage, provider: str, api: str, model: str) -> bool:
    metadata = message.metadata
    return metadata.provider == provider and metadata.api == api and metadata.model == model


def _text_signature(signature: str | None) -> tuple[str | None, str | None]:
    if not signature:
        return None, None
    if not signature.startswith("{"):
        return signature, None
    try:
        value = json.loads(signature)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(value, Mapping) or value.get("v") != 1 or not isinstance(value.get("id"), str):
        return None, None
    phase = value.get("phase")
    return value["id"], phase if phase in {"commentary", "final_answer"} else None


def _messages_to_input(
    messages: Sequence[ModelMessage],
    *,
    target_provider: str,
    target_api: str,
    target_model: str,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    message_index = 0
    for message in messages:
        if isinstance(message, ModelSystemMessage):
            result.append({"role": "system", "content": _text(message.content)})
        elif isinstance(message, ModelUserMessage):
            result.append({"role": "user", "content": _responses_content(message.content, input_side=True)})
        elif isinstance(message, ModelAssistantMessage):
            same_connection = _same_connection(message, target_provider, target_api, target_model)
            text_index = 0
            for block in message.content:
                if isinstance(block, ModelTextBlock):
                    fallback_id = f"msg_pi_{message_index}" if text_index == 0 else f"msg_pi_{message_index}_{text_index}"
                    text_index += 1
                    item_id, phase = _text_signature(block.text_signature)
                    item_id = item_id or fallback_id
                    if len(item_id) > 64:
                        item_id = f"msg_{hashlib.sha256(item_id.encode('utf-8')).hexdigest()[:56]}"
                    item: dict[str, object] = {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": block.text, "annotations": []}],
                        "status": "completed",
                        "id": item_id,
                    }
                    if phase:
                        item["phase"] = phase
                    result.append(item)
                elif isinstance(block, ModelThinkingBlock):
                    if block.signature:
                        try:
                            item = json.loads(block.signature)
                        except json.JSONDecodeError as error:
                            raise ValueError("Responses reasoning 签名不是有效 JSON") from error
                        if not isinstance(item, Mapping) or item.get("type") != "reasoning":
                            raise ValueError("Responses reasoning 签名不是 reasoning item")
                        result.append(dict(item))
                    elif block.text:
                        fallback_id = f"msg_pi_{message_index}" if text_index == 0 else f"msg_pi_{message_index}_{text_index}"
                        text_index += 1
                        result.append({
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": block.text, "annotations": []}],
                            "status": "completed",
                            "id": fallback_id,
                        })
                elif isinstance(block, ModelToolCallBlock):
                    call_id, separator, item_id = block.id.partition("|")
                    item: dict[str, object] = {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": block.name,
                        "arguments": json.dumps(block.arguments, ensure_ascii=False, separators=(",", ":")),
                    }
                    if separator and item_id.startswith("fc_") and same_connection:
                        item["id"] = item_id
                    if same_connection and block.namespace is not None:
                        item["namespace"] = block.namespace
                    result.append(item)
            message_index += 1
        elif isinstance(message, ModelToolResultMessage):
            text = tool_result_text(message.content)
            result.append({
                "type": "function_call_output",
                "call_id": message.tool_call_id.partition("|")[0],
                "output": text,
            })
    return result


def _response_output_blocks(
    output: Sequence[object],
    name_aliases: Mapping[str, str],
) -> tuple[ModelContentBlock, ...]:
    blocks: list[ModelContentBlock] = []
    for item in output:
        if not isinstance(item, Mapping):
            continue
        item_type = str(item.get("type", ""))
        if item_type == "reasoning":
            summary = item.get("summary")
            content = item.get("content")
            parts = summary if isinstance(summary, list) and summary else content
            text = "\n\n".join(
                str(part.get("text", ""))
                for part in parts if isinstance(part, Mapping)
            ) if isinstance(parts, list) else ""
            blocks.append(ModelThinkingBlock(
                text,
                json.dumps(item, ensure_ascii=False, separators=(",", ":")),
            ))
        elif item_type == "message":
            content = item.get("content")
            parts = [
                str(part.get("text", ""))
                for part in content
                if isinstance(part, Mapping) and part.get("type") == "output_text"
            ] if isinstance(content, list) else []
            item_id = item.get("id")
            phase = item.get("phase")
            signature = json.dumps(
                {"v": 1, "id": item_id, **({"phase": phase} if phase in {"commentary", "final_answer"} else {})},
                ensure_ascii=False,
                separators=(",", ":"),
            ) if isinstance(item_id, str) else None
            blocks.append(ModelTextBlock("".join(parts), text_signature=signature))
        elif item_type in {"function_call", "custom_tool_call"}:
            call_id = item.get("call_id") if isinstance(item.get("call_id"), str) else ""
            item_id = item.get("id") if isinstance(item.get("id"), str) else ""
            identifier = f"{call_id}|{item_id}" if call_id and item_id else call_id or item_id
            name = str(item.get("name") or "")
            raw_arguments = item.get("arguments", item.get("input", "{}"))
            arguments = json_object(raw_arguments)
            blocks.append(ModelToolCallBlock(
                identifier,
                name_aliases.get(name, name),
                arguments,
                namespace=item.get("namespace") if isinstance(item.get("namespace"), str) else None,
            ))
    return tuple(blocks)


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
        self.text_started: set[str] = set()
        self.thinking_started: set[str] = set()
        self.tool_started: set[str] = set()
        self.completed_output: list[object] | None = None
        self.completed_items: dict[str, tuple[int | None, tuple[ModelContentBlock, ...]]] = {}
        self.completed_item_order: list[str] = []

    @staticmethod
    def _output_index(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def _output_key(self, data: Mapping[str, object], kind: str) -> str:
        output_index = self._output_index(data.get("output_index"))
        if output_index is not None:
            return f"output:{output_index}"
        item_id = data.get("item_id")
        return f"item:{item_id}" if isinstance(item_id, str) else kind

    def _get(self, key: str, kind: str = "function_call", output_index: int | None = None) -> _Output:
        if key not in self.outputs:
            self.outputs[key] = _Output(kind=kind, output_index=output_index)
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
                key = self._output_key(data, "text")
                output_index = self._output_index(data.get("output_index"))
                if key not in self.text_started:
                    self.text_started.add(key)
                    self._get(key, "text", output_index)
                    events.append(ModelTextStart())
                self._get(key, "text", output_index).text += delta
                events.append(ModelTextDelta(delta))
        elif event_type == "response.reasoning_summary_text.delta":
            delta = data.get("delta")
            if isinstance(delta, str):
                key = self._output_key(data, "thinking")
                output_index = self._output_index(data.get("output_index"))
                if key not in self.thinking_started:
                    self.thinking_started.add(key)
                    self._get(key, "thinking", output_index)
                    events.append(ModelThinkingStart())
                self._get(key, "thinking", output_index).text += delta
                events.append(ModelThinkingDelta(delta))
        elif event_type == "response.output_item.added":
            item = data.get("item")
            if isinstance(item, Mapping):
                output_index = self._output_index(data.get("output_index"))
                item_type = item.get("type")
                if item_type in {"function_call", "custom_tool_call"}:
                    key = self._output_key(data, "function_call")
                    call = self._get(key, str(item_type), output_index)
                    call.id = str(item.get("call_id") or item.get("id") or key)
                    call.name = str(item.get("name") or "")
                    self.tool_started.add(key)
                    events.append(ModelToolCallStart(call.id, call.name))
                elif item_type == "message":
                    self._get(self._output_key(data, "text"), "text", output_index)
                elif item_type == "reasoning":
                    self._get(self._output_key(data, "thinking"), "thinking", output_index)
        elif event_type == "response.function_call_arguments.delta":
            key = self._output_key(data, "function_call")
            call = self._get(key, output_index=self._output_index(data.get("output_index")))
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
            key = self._output_key(data, "function_call")
            call = self._get(key, output_index=self._output_index(data.get("output_index")))
            if isinstance(data.get("call_id"), str):
                call.id = data["call_id"]
            if isinstance(data.get("name"), str):
                call.name = data["name"]
            if isinstance(data.get("arguments"), str):
                call.arguments = data["arguments"]
        elif event_type == "response.output_item.done":
            item = data.get("item")
            if isinstance(item, Mapping):
                output_index = self._output_index(data.get("output_index"))
                item_key = self._output_key(data, str(item.get("type") or "item"))
                if item_key not in self.completed_item_order:
                    self.completed_item_order.append(item_key)
                self.completed_items[item_key] = (
                    output_index,
                    _response_output_blocks((item,), self.name_aliases),
                )
        elif event_type == "response.completed":
            self.completed = True
            response = data.get("response")
            if isinstance(response, Mapping):
                status = response.get("status")
                self.stop_reason = "stop" if status == "completed" else str(status) if status else None
                output = response.get("output")
                if isinstance(output, list):
                    self.completed_output = output
        elif event_type == "response.failed":
            error = data.get("response", data)
            return [ModelStreamError(str(error.get("error") if isinstance(error, Mapping) else error))]
        return events

    def finish(self) -> tuple[list[ModelStreamEvent], ModelAssistantMessage | None, str | None]:
        if not self.completed:
            return [], None, "模型流式响应未正常结束"
        events: list[ModelStreamEvent] = []
        events.extend(ModelTextEnd() for _ in self.text_started)
        events.extend(ModelThinkingEnd() for _ in self.thinking_started)
        if self.completed_output is not None:
            try:
                blocks = _response_output_blocks(self.completed_output, self.name_aliases)
            except ValueError as error:
                return events, None, str(error)
            events.extend(ModelToolCallEnd(block) for block in blocks if isinstance(block, ModelToolCallBlock))
            reason = self.stop_reason or (
                "tool_use" if any(isinstance(block, ModelToolCallBlock) for block in blocks) else "stop"
            )
            return events, ModelAssistantMessage(
                content=blocks,
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
        blocks: list[ModelContentBlock] = []
        if self.completed_item_order:
            fallback_order = {key: index for index, key in enumerate(self.completed_item_order)}
            keys = sorted(
                self.completed_item_order,
                key=lambda key: (
                    self.completed_items[key][0] is None,
                    self.completed_items[key][0]
                    if self.completed_items[key][0] is not None
                    else fallback_order[key],
                ),
            )
            for key in keys:
                blocks.extend(self.completed_items[key][1])
        else:
            fallback_order = {key: index for index, key in enumerate(self.order)}
            keys = sorted(
                self.order,
                key=lambda key: (
                    self.outputs[key].output_index is None,
                    self.outputs[key].output_index
                    if self.outputs[key].output_index is not None
                    else fallback_order[key],
                ),
            )
        for key in (() if self.completed_item_order else keys):
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
        provider = self.provider or configuration.provider
        payload: dict[str, object] = {
            "model": configuration.model,
            "input": _messages_to_input(
                transformed,
                target_provider=provider,
                target_api=self.api,
                target_model=configuration.model,
            ),
            "stream": stream,
        }
        wire_tools = _tools_to_wire(normalized, aliases)
        if wire_tools:
            payload["tools"] = wire_tools
        if max_tokens is not None:
            payload["max_output_tokens"] = max_tokens
        headers = self._headers(configuration)
        return payload, headers, reverse

    async def stream_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None) -> AsyncGenerator[ModelStreamEvent, None]:
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
        output = data.get("output")
        blocks = _response_output_blocks(output, name_aliases) if isinstance(output, list) else ()
        stop_reason = "tool_use" if any(isinstance(block, ModelToolCallBlock) for block in blocks) else "stop"
        return ModelAssistantMessage(
            content=blocks,
            metadata=ModelResponseMetadata(
                provider=self.provider or configuration.provider,
                api=self.api,
                model=data.get("model") if isinstance(data.get("model"), str) else configuration.model,
                response_id=data.get("id") if isinstance(data.get("id"), str) else None,
                usage=_usage(data.get("usage")),
                stop_reason=stop_reason,
                timestamp=int(time.time() * 1000),
            ),
        )

    async def stream_rich_messages(self, *args, **kwargs):
        async with aclosing(self.stream_messages(*args, **kwargs)) as stream:
            async for event in stream:
                yield event
