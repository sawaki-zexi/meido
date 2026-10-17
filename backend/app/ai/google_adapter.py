"""Google Generative AI wire adapter.

Google's ``contents``/``parts`` format is deliberately kept here instead of
being routed through the OpenAI compatibility adapter.  That prevents tool
schemas and function results from being sent with the wrong shape.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import aclosing
import json
import time

import httpx

from .request_budget import image_budget_client_kwargs, budgeted_model_stream, budgeted_model_completion

from .message_transform import transform_messages
from .model_adapter import _payload_error_detail, _raise_for_status_with_details, _response_error_detail
from .model_protocol import compatible_tool_names, effective_tools, model_cost, normalize_tools, remap_message_tool_names, safe_tool_id, tool_result_text
from .model_types import (
    ModelAssistantMessage, ModelCapabilities, ModelContentBlock, ModelImageBlock,
    ModelMessage, ModelResponseMetadata, ModelStreamDone, ModelStreamError,
    ModelStreamEvent, ModelStreamStart, ModelTextBlock, ModelTextDelta,
    ModelTextEnd, ModelTextStart, ModelThinkingBlock, ModelThinkingDelta,
    ModelThinkingEnd, ModelThinkingStart, ModelToolCallBlock, ModelToolCallEnd,
    ModelToolCallStart, ModelToolCallDelta, ModelToolResultMessage,
    ModelUserMessage, ModelSystemMessage, ModelUsage,
)
from ..models import ModelConfiguration


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _usage(value: object) -> ModelUsage:
    if not isinstance(value, Mapping):
        return ModelUsage()
    cached = _int(value.get("cachedContentTokenCount"))
    thoughts = _int(value.get("thoughtsTokenCount"))
    prompt = max(0, _int(value.get("promptTokenCount")) - cached)
    output = _int(value.get("candidatesTokenCount")) + thoughts
    return ModelUsage(input_tokens=prompt, output_tokens=output, cache_read_tokens=cached, reasoning_tokens=thoughts, cost=model_cost(value))


def _text(blocks: Sequence[object]) -> str:
    return "".join(block.text for block in blocks if isinstance(block, ModelTextBlock))


def _stop_reason(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError("模型响应缺少 finishReason")
    mapped = {"STOP": "stop", "MAX_TOKENS": "length"}.get(value)
    if mapped is None:
        raise RuntimeError(f"模型响应包含错误 finishReason: {value}")
    return mapped


def _parts(blocks: Sequence[object], *, user: bool) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for block in blocks:
        if isinstance(block, ModelTextBlock):
            part: dict[str, object] = {"text": block.text}
            if block.text_signature:
                part["thoughtSignature"] = block.text_signature
            result.append(part)
        elif isinstance(block, ModelImageBlock):
            result.append({"inlineData": {"mimeType": block.media_type, "data": block.data_base64}})
        elif not user and isinstance(block, ModelThinkingBlock):
            if block.text or block.signature:
                part = {"text": block.text, "thought": True}
                if block.signature:
                    part["thoughtSignature"] = block.signature
                result.append(part)
        elif not user and isinstance(block, ModelToolCallBlock):
            part = {"functionCall": {"name": block.name, "args": dict(block.arguments), "id": safe_tool_id(block.id)}}
            if block.thought_signature:
                part["thoughtSignature"] = block.thought_signature
            result.append(part)
    return result or [{"text": ""}]


def _messages_to_wire(messages: Sequence[ModelMessage]) -> tuple[str, list[dict[str, object]]]:
    system: list[str] = []
    result: list[dict[str, object]] = []
    for message in messages:
        if isinstance(message, ModelSystemMessage):
            system.append(_text(message.content))
        elif isinstance(message, ModelUserMessage):
            result.append({"role": "user", "parts": _parts(message.content, user=True)})
        elif isinstance(message, ModelAssistantMessage):
            result.append({"role": "model", "parts": _parts(message.content, user=False)})
        elif isinstance(message, ModelToolResultMessage):
            response = {
                "name": message.tool_name,
                "response": {
                    "error" if message.is_error else "output": tool_result_text(message.content),
                },
            }
            part = {"functionResponse": response}
            if (
                result
                and result[-1].get("role") == "user"
                and isinstance(result[-1].get("parts"), list)
                and all(isinstance(item, Mapping) and "functionResponse" in item for item in result[-1]["parts"])
            ):
                result[-1]["parts"].append(part)
            else:
                result.append({"role": "user", "parts": [part]})
    return "\n\n".join(item for item in system if item), result


def _tools(tools: Sequence[Mapping[str, object]], aliases: Mapping[str, str]) -> list[dict[str, object]]:
    def google_schema(schema: object) -> object:
        if isinstance(schema, list):
            return [google_schema(item) for item in schema]
        if isinstance(schema, bool):
            raise ValueError("Google Generative AI 工具 schema 不支持布尔子 schema")
        if not isinstance(schema, Mapping):
            raise ValueError("Google Generative AI 工具 schema 子节点必须是对象")
        # Gemini function declarations use a restricted OpenAPI subset. Keep
        # validation in the shared layer, then remove keywords Gemini rejects.
        unsupported = {
            "$defs", "$ref", "allOf", "anyOf", "contains", "dependentSchemas",
            "if", "not", "oneOf", "patternProperties", "prefixItems",
            "propertyNames", "then", "unevaluatedItems", "unevaluatedProperties",
        }
        found = unsupported.intersection(schema)
        if found:
            raise ValueError(f"Google Generative AI 工具 schema 暂不支持 {', '.join(sorted(found))}")
        additional = schema.get("additionalProperties")
        if additional is not None and not isinstance(additional, bool):
            raise ValueError("Google Generative AI 工具 schema 不支持 additionalProperties schema")
        if "additionalItems" in schema:
            raise ValueError("Google Generative AI 工具 schema 不支持 additionalItems")
        if isinstance(schema.get("items"), list):
            raise ValueError("Google Generative AI 工具 schema 不支持 items 元组")
        enum = schema.get("enum")
        if isinstance(enum, list) and any(not isinstance(item, str) for item in enum):
            raise ValueError("Google Generative AI 工具 schema.enum 只支持字符串")
        result: dict[str, object] = {}
        for key, value in schema.items():
            if key in {"additionalProperties", "default"}:
                continue
            if key == "properties" and isinstance(value, Mapping):
                result[str(key)] = {str(prop): google_schema(child) for prop, child in value.items()}
            elif key == "items":
                result[str(key)] = google_schema(value)
            else:
                result[str(key)] = value
        schema_type = result.get("type")
        if isinstance(schema_type, list):
            types = [item for item in schema_type if item != "null"]
            if len(types) != 1:
                raise ValueError("Google Generative AI 工具 schema 只支持可空的单一类型")
            result["type"] = types[0]
            result["nullable"] = "null" in schema_type
        return result

    declarations = []
    for tool in normalize_tools(tools):
        declarations.append({
            "name": aliases.get(str(tool["name"]), str(tool["name"])),
            "description": tool["description"],
            "parameters": google_schema(tool["parameters"]),
        })
    return [{"functionDeclarations": declarations}] if declarations else []


class GoogleGenerativeAdapter:
    def __init__(
        self,
        timeout: float | None = None,
        capabilities: ModelCapabilities | None = None,
        *,
        api: str = "google-generative",
    ) -> None:
        self.timeout = timeout
        self.capabilities = capabilities or ModelCapabilities(
            supports_images=True,
            supports_reasoning=True,
            preserve_thinking_signatures=True,
        )
        self.api = api

    def _url(self, configuration, *, stream: bool) -> str:
        suffix = ":streamGenerateContent?alt=sse" if stream else ":generateContent"
        return f"{configuration.baseUrl.rstrip('/')}/models/{configuration.model}{suffix}"

    def _headers(self, configuration, *, stream: bool) -> dict[str, str]:
        headers = {"content-type": "application/json", "accept": "text/event-stream" if stream else "application/json"}
        if configuration.apiKey:
            headers["x-goog-api-key"] = configuration.apiKey
        return headers

    async def _authorize(self, configuration, headers: dict[str, str]) -> str:
        return configuration.apiKey

    def _prepare(self, messages: Sequence[ModelMessage], configuration: ModelConfiguration, tools: Sequence[Mapping[str, object]], *, stream: bool, max_tokens: int | None) -> tuple[str, dict[str, str], dict[str, object], dict[str, str]]:
        transformed = transform_messages(
            messages,
            capabilities=self.capabilities,
            target_provider=configuration.provider,
            target_api=self.api,
            target_model=configuration.model,
        )
        normalized = normalize_tools(effective_tools(transformed, tools))
        aliases, reverse = compatible_tool_names(normalized, transformed, pattern=r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
        transformed = remap_message_tool_names(transformed, aliases)
        system, contents = _messages_to_wire(transformed)
        config: dict[str, object] = {}
        if max_tokens is not None:
            config["maxOutputTokens"] = max_tokens
        payload: dict[str, object] = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        function_tools = _tools(normalized, aliases)
        if function_tools:
            payload["tools"] = function_tools
        if config:
            payload["generationConfig"] = config
        url = self._url(configuration, stream=stream)
        headers = self._headers(configuration, stream=stream)
        return url, headers, payload, reverse

    @budgeted_model_stream
    async def stream_messages(self, messages: Sequence[ModelMessage], configuration: ModelConfiguration, tools: Sequence[Mapping[str, object]] = (), *, max_tokens: int | None = None, cancel_check: Callable[[], None] | None = None) -> AsyncGenerator[ModelStreamEvent, None]:
        url, headers, payload, name_aliases = self._prepare(messages, configuration, tools, stream=True, max_tokens=max_tokens)
        text = ""
        thinking = ""
        blocks: list[ModelContentBlock] = []
        usage = ModelUsage()
        response_id = None
        stop_reason = None
        current_kind: str | None = None
        completed = False
        redaction_key = configuration.apiKey
        yield ModelStreamStart()
        try:
            redaction_key = await self._authorize(configuration, headers)
            async with httpx.AsyncClient(timeout=self.timeout, **image_budget_client_kwargs(self.api)) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    await _raise_for_status_with_details(response, redaction_key)
                    async for line in response.aiter_lines():
                        if cancel_check:
                            cancel_check()
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if not raw or raw == "[DONE]":
                            continue
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(data, Mapping):
                            continue
                        error = _payload_error_detail(data, redaction_key)
                        if error:
                            yield ModelStreamError(f"模型响应失败: {error}")
                            return
                        usage = _usage(data.get("usageMetadata"))
                        candidates = data.get("candidates")
                        candidate = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], Mapping) else {}
                        finish = candidate.get("finishReason")
                        if isinstance(finish, str):
                            stop_reason = _stop_reason(finish)
                            completed = True
                        content = candidate.get("content") if isinstance(candidate, Mapping) else None
                        parts = content.get("parts") if isinstance(content, Mapping) else None
                        if not isinstance(parts, list):
                            continue
                        for part in parts:
                            if not isinstance(part, Mapping):
                                continue
                            part_text = part.get("text")
                            if isinstance(part_text, str) and part_text:
                                is_thought = bool(part.get("thought"))
                                next_kind = "thinking" if is_thought else "text"
                                if current_kind != next_kind:
                                    if current_kind == "thinking":
                                        yield ModelThinkingEnd()
                                    elif current_kind == "text":
                                        yield ModelTextEnd()
                                    signature = part.get("thoughtSignature")
                                    signature = signature if isinstance(signature, str) else None
                                    if is_thought:
                                        thinking = ""
                                        blocks.append(ModelThinkingBlock("", signature))
                                        yield ModelThinkingStart()
                                    else:
                                        text = ""
                                        blocks.append(ModelTextBlock("", text_signature=signature))
                                        yield ModelTextStart()
                                    current_kind = next_kind
                                previous = blocks[-1]
                                signature = part.get("thoughtSignature")
                                signature = signature if isinstance(signature, str) else None
                                if is_thought:
                                    thinking += part_text
                                    retained_signature = previous.signature or signature if isinstance(previous, ModelThinkingBlock) else signature
                                    blocks[-1] = ModelThinkingBlock(thinking, retained_signature)
                                    yield ModelThinkingDelta(part_text)
                                else:
                                    text += part_text
                                    retained_signature = previous.text_signature or signature if isinstance(previous, ModelTextBlock) else signature
                                    blocks[-1] = ModelTextBlock(text, text_signature=retained_signature)
                                    yield ModelTextDelta(part_text)
                            function = part.get("functionCall")
                            if isinstance(function, Mapping):
                                if current_kind == "thinking":
                                    yield ModelThinkingEnd()
                                    current_kind = None
                                elif current_kind == "text":
                                    yield ModelTextEnd()
                                    current_kind = None
                                call_id = safe_tool_id(str(function.get("id") or f"call-{len(blocks)}"))
                                wire_name = str(function.get("name") or "")
                                name = name_aliases.get(wire_name, wire_name)
                                arguments = function.get("args") if isinstance(function.get("args"), Mapping) else {}
                                signature = part.get("thoughtSignature")
                                call = ModelToolCallBlock(
                                    call_id,
                                    name,
                                    dict(arguments),
                                    thought_signature=signature if isinstance(signature, str) else None,
                                )
                                blocks.append(call)
                                encoded = json.dumps(dict(arguments), ensure_ascii=False, separators=(",", ":"))
                                yield ModelToolCallStart(call_id, name)
                                yield ModelToolCallDelta(call_id, encoded)
                                yield ModelToolCallEnd(call)
            if current_kind == "text":
                yield ModelTextEnd()
            if current_kind == "thinking":
                yield ModelThinkingEnd()
            if not completed:
                yield ModelStreamError("模型流式响应未正常结束")
                return
            if stop_reason is None:
                yield ModelStreamError("模型响应缺少 finishReason")
                return
            yield ModelStreamDone(ModelAssistantMessage(content=tuple(blocks), metadata=ModelResponseMetadata(provider=configuration.provider, api=self.api, model=configuration.model, response_id=response_id, usage=usage, stop_reason=stop_reason, timestamp=int(time.time() * 1000))))
        except httpx.HTTPStatusError as error:
            detail = _response_error_detail(error.response, redaction_key)
            yield ModelStreamError(f"HTTP {error.response.status_code} {error.response.reason_phrase}: {detail}" if detail else f"HTTP {error.response.status_code} {error.response.reason_phrase}")
        except RuntimeError as error:
            if str(error) == "agent run cancelled":
                raise
            yield ModelStreamError(str(error))
        except Exception as error:
            message = str(error).replace(redaction_key, "***") if redaction_key else str(error)
            yield ModelStreamError(message)

    @budgeted_model_completion
    async def complete_messages(self, messages: Sequence[ModelMessage], configuration: ModelConfiguration, tools: Sequence[Mapping[str, object]] = (), *, max_tokens: int | None = None) -> ModelAssistantMessage:
        url, headers, payload, name_aliases = self._prepare(messages, configuration, tools, stream=False, max_tokens=max_tokens)
        redaction_key = await self._authorize(configuration, headers)
        async with httpx.AsyncClient(timeout=self.timeout, **image_budget_client_kwargs(self.api)) as client:
            response = await client.post(url, headers=headers, json=payload)
            await _raise_for_status_with_details(response, redaction_key)
        data = response.json()
        if not isinstance(data, Mapping):
            raise RuntimeError("模型响应不是 JSON 对象")
        error = _payload_error_detail(data, redaction_key)
        if error:
            raise RuntimeError(f"模型响应失败: {error}")
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], Mapping):
            raise RuntimeError("模型响应缺少 candidates")
        candidate = candidates[0]
        content = candidate.get("content") if isinstance(candidate, Mapping) else {}
        parts = content.get("parts") if isinstance(content, Mapping) else []
        blocks: list[ModelContentBlock] = []
        for part in parts if isinstance(parts, list) else []:
            if not isinstance(part, Mapping):
                continue
            if isinstance(part.get("text"), str):
                signature = part.get("thoughtSignature") if isinstance(part.get("thoughtSignature"), str) else None
                blocks.append(
                    ModelThinkingBlock(part["text"], signature)
                    if part.get("thought")
                    else ModelTextBlock(part["text"], text_signature=signature)
                )
            function = part.get("functionCall")
            if isinstance(function, Mapping):
                args = function.get("args") if isinstance(function.get("args"), Mapping) else {}
                wire_name = str(function.get("name") or "")
                signature = part.get("thoughtSignature")
                blocks.append(ModelToolCallBlock(
                    safe_tool_id(str(function.get("id") or f"call-{len(blocks)}")),
                    name_aliases.get(wire_name, wire_name),
                    dict(args),
                    thought_signature=signature if isinstance(signature, str) else None,
                ))
        reason = _stop_reason(candidate.get("finishReason"))
        return ModelAssistantMessage(content=tuple(blocks), metadata=ModelResponseMetadata(provider=configuration.provider, api=self.api, model=configuration.model, usage=_usage(data.get("usageMetadata")), stop_reason=reason, timestamp=int(time.time() * 1000)))

    async def stream_rich_messages(self, *args, **kwargs) -> AsyncGenerator[ModelStreamEvent, None]:
        async with aclosing(self.stream_messages(*args, **kwargs)) as stream:
            async for event in stream:
                yield event
