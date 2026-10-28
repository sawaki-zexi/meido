from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass
import hashlib
import json
import os
import re
from typing import Protocol, Literal

import httpx

from .request_budget import image_budget_client_kwargs, budgeted_model_stream, budgeted_model_completion

from ..models import Message, ModelConfiguration, Role
from .model_types import ModelMessage, ModelStreamEvent as RichModelStreamEvent
from .model_protocol import copy_json


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    id: str
    name: str
    arguments: dict[str, object]
    type: Literal["tool_call"] = "tool_call"
    arguments_complete: bool = True


@dataclass(frozen=True, slots=True)
class ModelStreamError:
    error: str
    type: Literal["error"] = "error"


@dataclass(frozen=True, slots=True)
class ModelResponseDone:
    """Legacy structured adapter's semantic completion (iterator EOF is not it)."""

    stop_reason: str
    type: Literal["done"] = "done"


ModelStreamEvent = ModelTextDelta | ModelToolCall | ModelStreamError | ModelResponseDone

_FUNCTION_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def _compatible_function_names(
    tools: Sequence[Mapping[str, object]],
    messages: Sequence[Mapping[str, object]],
) -> dict[str, str]:
    names: list[str] = []
    for tool in tools:
        function = tool.get("function")
        candidate = function if isinstance(function, Mapping) else tool
        candidate_name = candidate.get("name") if isinstance(candidate, Mapping) else None
        if isinstance(candidate_name, str):
            names.append(candidate_name)
    for message in messages:
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            for call in calls:
                if not isinstance(call, Mapping):
                    continue
                function = call.get("function")
                candidate = function if isinstance(function, Mapping) else call
                candidate_name = candidate.get("name") if isinstance(candidate, Mapping) else None
                if isinstance(candidate_name, str):
                    names.append(candidate_name)
        function_call = message.get("function_call")
        legacy_name = function_call.get("name") if isinstance(function_call, Mapping) else None
        if isinstance(legacy_name, str):
            names.append(legacy_name)
        role_name = message.get("name")
        if message.get("role") in {"function", "tool"} and isinstance(role_name, str):
            names.append(role_name)

    unique_names = list(dict.fromkeys(names))
    reserved = {name for name in unique_names if _FUNCTION_NAME_PATTERN.fullmatch(name)}
    used: set[str] = set()
    aliases: dict[str, str] = {}
    for index, name in enumerate(unique_names):
        if _FUNCTION_NAME_PATTERN.fullmatch(name) and name not in used:
            alias = name
        else:
            digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
            for width in range(12, 53, 4):
                alias = f"meido_tool_{digest[:width]}"
                if alias not in reserved and alias not in used:
                    break
            else:
                alias = f"meido_tool_{digest[:45]}_{index}"
        aliases[name] = alias
        used.add(alias)
    return aliases


def _remap_function_names(
    values: Sequence[Mapping[str, object]],
    aliases: Mapping[str, str],
    *,
    tools: bool = False,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for value in values:
        copied = copy_json(value)
        if not isinstance(copied, dict):
            continue
        if tools:
            function = copied.get("function")
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                function["name"] = aliases.get(function["name"], function["name"])
            elif isinstance(copied.get("name"), str):
                copied["name"] = aliases.get(copied["name"], copied["name"])
        else:
            calls = copied.get("tool_calls")
            if isinstance(calls, list):
                for position, call in enumerate(calls):
                    if not isinstance(call, Mapping):
                        continue
                    call = dict(call)
                    function = call.get("function") if isinstance(call, dict) else None
                    if isinstance(function, dict) and isinstance(function.get("name"), str):
                        function["name"] = aliases.get(function["name"], function["name"])
                    elif isinstance(call.get("name"), str):
                        call["name"] = aliases.get(call["name"], call["name"])
                    calls[position] = call
            function_call = copied.get("function_call")
            if isinstance(function_call, Mapping) and isinstance(function_call.get("name"), str):
                function_call = dict(function_call)
                function_call["name"] = aliases.get(function_call["name"], function_call["name"])
                copied["function_call"] = function_call
            if copied.get("role") in {"function", "tool"} and isinstance(copied.get("name"), str):
                copied["name"] = aliases.get(copied["name"], copied["name"])
        result.append(copied)
    return result


def _call_index(raw_index: object, fallback: int) -> int:
    if isinstance(raw_index, int) and not isinstance(raw_index, bool) and raw_index >= 0:
        return raw_index
    if isinstance(raw_index, str) and raw_index.isdigit():
        return int(raw_index)
    return fallback


def _append_tool_call_fragment(
    calls: dict[int, dict[str, str]],
    index: int,
    identifier: object,
    function: object,
    *,
    complete: bool = False,
) -> None:
    if not isinstance(function, Mapping):
        return
    aggregate = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})

    def merge_text(field: str, value: object, *, authoritative: bool = False) -> None:
        if not isinstance(value, str) or not value:
            return
        current = aggregate[field]
        if not current:
            aggregate[field] = value
        elif authoritative:
            if value.startswith(current):
                aggregate[field] = value
            elif not current.startswith(value):
                aggregate[field] = value
        elif field in {"id", "name"}:
            if value == current or current.endswith(value):
                return
            if value.startswith(current):
                aggregate[field] = value
            else:
                aggregate[field] += value
        else:
            aggregate[field] += value

    merge_text("id", str(identifier) if isinstance(identifier, (str, int)) else None, authoritative=complete)
    merge_text("name", function.get("name"), authoritative=complete)
    arguments = function.get("arguments", "")
    arguments_are_complete = isinstance(arguments, (Mapping, list))
    if arguments_are_complete:
        arguments = json.dumps(copy_json(arguments), ensure_ascii=False)
    merge_text("arguments", arguments, authoritative=complete or arguments_are_complete)


def _response_error_detail(response: httpx.Response, api_key: str) -> str:
    try:
        payload = json.loads(response.content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = None
    detail = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            detail = error["message"]
        elif isinstance(payload.get("message"), str):
            detail = payload["message"]
        elif isinstance(payload.get("detail"), str):
            detail = payload["detail"]
    if not isinstance(detail, str) or not detail.strip():
        detail = response.text.strip()
    if api_key:
        detail = detail.replace(api_key, "***")
    return detail[:500]


def _payload_error_detail(payload: object, api_key: str) -> str | None:
    """Return a provider error from a successful HTTP response body."""
    if not isinstance(payload, Mapping):
        return None
    error = payload.get("error")
    if isinstance(error, Mapping):
        detail = error.get("message") or error.get("detail") or error.get("code")
    elif isinstance(error, str):
        detail = error
    elif payload.get("type") == "error":
        detail = payload.get("message") or payload.get("errorMessage")
    else:
        return None
    if detail is None:
        detail = str(error)
    text = str(detail).strip()
    if api_key:
        text = text.replace(api_key, "***")
    return text[:500] or "模型服务返回错误"


def _is_deepseek(configuration: ModelConfiguration) -> bool:
    return configuration.providerId == "deepseek" or configuration.baseUrl.rstrip("/").lower() == "https://api.deepseek.com"


async def _raise_for_status_with_details(response: httpx.Response, api_key: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        await response.aread()
        detail = _response_error_detail(response, api_key)
        message = f"HTTP {response.status_code} {response.reason_phrase}"
        if detail:
            message = f"{message}: {detail}"
        raise httpx.HTTPStatusError(message, request=error.request, response=response) from error


class ModelAdapter(Protocol):
    def stream_reply(
        self,
        role: Role,
        history: list[Message],
        configuration: ModelConfiguration | None = None,
        memory_context: str = "",
    ) -> AsyncIterator[str]: ...

    async def complete_messages(
        self,
        messages: Sequence[Mapping[str, object]],
        configuration: ModelConfiguration,
        *,
        max_tokens: int | None = None,
    ) -> str: ...

    async def complete_rich_messages(
        self,
        messages: Sequence[ModelMessage],
        configuration: ModelConfiguration | None,
        tools: Sequence[Mapping[str, object]] = (),
        *,
        max_tokens: int | None = None,
    ): ...

    def stream_messages(
        self,
        messages: Sequence[Mapping[str, object]],
        configuration: ModelConfiguration,
        *,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]: ...

    def stream_structured_messages(
        self,
        messages: Sequence[Mapping[str, object]],
        configuration: ModelConfiguration | None,
        tools: list[dict[str, object]],
        *,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ModelStreamEvent]: ...

    def stream_rich_messages(
        self,
        messages: Sequence[ModelMessage],
        configuration: ModelConfiguration | None,
        tools: Sequence[Mapping[str, object]] = (),
        *,
        max_tokens: int | None = None,
        cancel_check=None,
    ) -> AsyncIterator[RichModelStreamEvent]: ...


def supports_auxiliary_thinking_control(configuration: ModelConfiguration | None) -> bool:
    """Only known Chat Completions providers have an explicit thinking-off contract."""
    if configuration is None or configuration.api != "openai-completions":
        return False
    identity = f"{configuration.provider} {configuration.baseUrl}".lower()
    return any(name in identity for name in ("deepseek", "dashscope", "xiaomimimo.com"))


class OpenAICompatibleAdapter:
    """Streams chat completions from an OpenAI-compatible endpoint."""

    def __init__(self, timeout: float | None = None) -> None:
        self.timeout = timeout

    async def stream_rich_messages(
        self,
        messages,
        configuration: ModelConfiguration | None,
        tools: Sequence[Mapping[str, object]] = (),
        *,
        max_tokens: int | None = None,
        cancel_check=None,
        disable_thinking: bool = False,
    ):
        """Expose the Pi-style stream while retaining legacy adapter methods."""

        from .pi_openai_adapter import OpenAICompletionsRichAdapter

        effective_configuration = self._effective_configuration(configuration)
        from .pi_openai_adapter import OpenAICompletionsOptions

        adapter = OpenAICompletionsRichAdapter(
            timeout=self.timeout,
            options=OpenAICompletionsOptions(
                send_reasoning_content=_is_deepseek(effective_configuration),
                supports_finish_reason=False,
                disable_thinking=disable_thinking,
            ),
        )
        async with aclosing(adapter.stream_rich_messages(
            messages,
            effective_configuration,
            tools,
            max_tokens=max_tokens,
            cancel_check=cancel_check,
        )) as stream:
            async for event in stream:
                yield event

    async def complete_rich_messages(
        self,
        messages,
        configuration: ModelConfiguration | None,
        tools: Sequence[Mapping[str, object]] = (),
        *,
        max_tokens: int | None = None,
    ):
        from .pi_openai_adapter import OpenAICompletionsRichAdapter

        effective_configuration = self._effective_configuration(configuration)
        from .pi_openai_adapter import OpenAICompletionsOptions

        adapter = OpenAICompletionsRichAdapter(
            timeout=self.timeout,
            options=OpenAICompletionsOptions(
                send_reasoning_content=_is_deepseek(effective_configuration),
            ),
        )
        return await adapter.complete_messages(
            messages,
            effective_configuration,
            tools,
            max_tokens=max_tokens,
        )

    async def stream_reply(
        self,
        role: Role,
        history: list[Message],
        configuration: ModelConfiguration | None = None,
        memory_context: str = "",
    ) -> AsyncIterator[str]:
        effective_configuration = self._effective_configuration(configuration)

        profile = role.profile
        system_prompt = "\n\n".join(part for part in [
            f"你正在扮演 {role.name}。",
            f"简介：{role.description}" if role.description else "",
            f"角色设定：{profile.profile}",
            f"性格：{profile.personality}" if profile.personality else "",
            f"行为规则：{profile.behaviorRules}" if profile.behaviorRules else "",
            f"回复约束：{profile.responseConstraints}" if profile.responseConstraints else "",
            f"称呼用户：{profile.nickname}" if profile.nickname else "",
            memory_context,
        ] if part)
        messages: list[dict[str, object]] = [{"role": "system", "content": system_prompt}]
        messages.extend({"role": message.role, "content": message.content} for message in history)
        async for delta in self.stream_messages(
            messages,
            effective_configuration,
        ):
            yield delta

    @staticmethod
    def _effective_configuration(configuration: ModelConfiguration | None) -> ModelConfiguration:
        if configuration is not None:
            return configuration
        base_url = os.getenv("MEIDO_MODEL_BASE_URL", "").strip().rstrip("/")
        model = os.getenv("MEIDO_MODEL", "").strip()
        api_key = os.getenv("MEIDO_API_KEY", "").strip()
        if not base_url or not model:
            raise RuntimeError("尚未配置模型服务，请先在模型设置中保存连接信息")
        return ModelConfiguration(
            providerId="custom",
            provider="custom",
            baseUrl=base_url,
            model=model,
            apiKey=api_key,
        )

    @budgeted_model_stream
    async def stream_messages(
        self,
        messages: Sequence[Mapping[str, object]],
        configuration: ModelConfiguration,
        *,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        base_url = configuration.baseUrl.rstrip("/")
        headers = {"Authorization": f"Bearer {configuration.apiKey}"} if configuration.apiKey else {}
        request_data: dict[str, object] = {"model": configuration.model, "messages": messages, "stream": True}
        if max_tokens is not None:
            request_data["max_tokens"] = max_tokens
        completed = False
        async with httpx.AsyncClient(timeout=self.timeout, **image_budget_client_kwargs("openai-completions")) as client:
            async with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers=headers,
                json=request_data,
            ) as response:
                await _raise_for_status_with_details(response, configuration.apiKey)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        completed = True
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError as error:
                        raise RuntimeError("模型流包含无效 JSON") from error
                    if isinstance(chunk, dict) and chunk.get("error"):
                        error = chunk["error"]
                        message = error.get("message", "模型服务返回错误") if isinstance(error, dict) else str(error)
                        if configuration.apiKey:
                            message = message.replace(configuration.apiKey, "***")
                        raise RuntimeError(message)
                    delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
                    if isinstance(delta, str) and delta:
                        yield delta
        if not completed:
            raise RuntimeError("模型流式响应未正常结束")

    @budgeted_model_stream
    async def stream_structured_messages(
        self,
        messages: Sequence[Mapping[str, object]],
        configuration: ModelConfiguration | None,
        tools: list[dict[str, object]],
        *,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        """Stream text and aggregate OpenAI-compatible tool call deltas.

        Providers differ in how they split tool-call IDs, names, and JSON
        arguments across chunks. The adapter owns that wire detail and only
        exposes complete, provider-neutral calls to the Runtime provider.
        """
        configuration = self._effective_configuration(configuration)
        base_url = configuration.baseUrl.rstrip("/")
        headers = {"Authorization": f"Bearer {configuration.apiKey}"} if configuration.apiKey else {}
        from .model_protocol import normalize_tools

        normalized_tools = normalize_tools(tools)
        wire_definitions = [
            {
                "type": "function",
                "function": {
                    "name": item["name"],
                    "description": item["description"],
                    "parameters": item["parameters"],
                },
            }
            for item in normalized_tools
        ]
        aliases = _compatible_function_names(wire_definitions, messages)
        wire_tools = _remap_function_names(wire_definitions, aliases, tools=True)
        wire_messages = _remap_function_names(messages, aliases)
        internal_names = {alias: name for name, alias in aliases.items()}
        request_data: dict[str, object] = {
            "model": configuration.model,
            "messages": wire_messages,
            "tools": wire_tools,
            "tool_choice": "auto",
            "stream": True,
        }
        # DeepSeek enables thinking by default, but its tool-call protocol
        # requires replaying reasoning_content, which this runtime does not
        # retain. Use the documented non-thinking mode for tool turns.
        if _is_deepseek(configuration):
            request_data["thinking"] = {"type": "disabled"}
        if max_tokens is not None:
            request_data["max_tokens"] = max_tokens
        calls: dict[int, dict[str, str]] = {}
        completed = False
        stop_reason = "incomplete"
        async with httpx.AsyncClient(timeout=self.timeout, **image_budget_client_kwargs("openai-completions")) as client:
            async with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers=headers,
                json=request_data,
            ) as response:
                await _raise_for_status_with_details(response, configuration.apiKey)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        completed = True
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError as error:
                        raise RuntimeError("模型流包含无效 JSON") from error
                    if isinstance(chunk, dict) and chunk.get("error"):
                        error = chunk["error"]
                        message = error.get("message", "模型服务返回错误") if isinstance(error, dict) else str(error)
                        if configuration.apiKey:
                            message = message.replace(configuration.apiKey, "***")
                        yield ModelStreamError(message)
                        return
                    choices = chunk.get("choices", []) if isinstance(chunk, dict) else []
                    choice = choices[0] if isinstance(choices, list) and choices else {}
                    if isinstance(choice, dict) and choice.get("finish_reason") is not None:
                        from .pi_openai_adapter import _stop_reason
                        from .model_protocol import preserve_stop_reason
                        stop_reason = preserve_stop_reason(None if stop_reason == "incomplete" else stop_reason, _stop_reason(str(choice["finish_reason"])))
                    delta = choice.get("delta", {}) if isinstance(choice, dict) else {}
                    message = choice.get("message", {}) if isinstance(choice, dict) else {}
                    content = delta.get("content") if isinstance(delta, Mapping) else None
                    if not isinstance(content, str) and isinstance(message, Mapping):
                        content = message.get("content")
                    if isinstance(content, str) and content:
                        yield ModelTextDelta(content)
                    raw_calls = delta.get("tool_calls", []) if isinstance(delta, Mapping) else []
                    if isinstance(raw_calls, list):
                        for position, raw_call in enumerate(raw_calls):
                            if not isinstance(raw_call, Mapping):
                                continue
                            function = raw_call.get("function")
                            if not isinstance(function, Mapping):
                                function = raw_call
                            _append_tool_call_fragment(
                                calls,
                                _call_index(raw_call.get("index"), position),
                                raw_call.get("id"),
                                function,
                            )
                    legacy_function = delta.get("function_call") if isinstance(delta, Mapping) else None
                    if isinstance(legacy_function, Mapping):
                        _append_tool_call_fragment(calls, 0, None, legacy_function)
                    # Some compatible servers return a complete message while
                    # streaming. Merge it without duplicating received fragments.
                    if isinstance(message, Mapping):
                        message_calls = message.get("tool_calls", [])
                        if isinstance(message_calls, list):
                            for position, raw_call in enumerate(message_calls):
                                if not isinstance(raw_call, Mapping):
                                    continue
                                function = raw_call.get("function")
                                if not isinstance(function, Mapping):
                                    function = raw_call
                                _append_tool_call_fragment(
                                    calls,
                                    _call_index(raw_call.get("index"), position),
                                    raw_call.get("id"),
                                    function,
                                    complete=True,
                                )
                        message_function = message.get("function_call")
                        if isinstance(message_function, Mapping):
                            _append_tool_call_fragment(calls, 0, None, message_function, complete=True)
        if not completed:
            raise RuntimeError("模型流式响应未正常结束")
        for index in sorted(calls):
            aggregate = calls[index]
            if not aggregate["name"]:
                yield ModelStreamError(f"模型工具调用缺少函数名: {aggregate['id'] or index}")
                return
            try:
                arguments = json.loads(aggregate["arguments"] or "{}")
            except json.JSONDecodeError:
                yield ModelStreamError(f"模型工具参数不是有效 JSON: {aggregate['name'] or index}")
                return
            if not isinstance(arguments, dict):
                yield ModelStreamError(f"模型工具参数必须是 JSON 对象: {aggregate['name'] or index}")
                return
            yield ModelToolCall(
                id=aggregate["id"] or f"call-{index}",
                name=internal_names.get(aggregate["name"], aggregate["name"]),
                arguments=arguments,
                arguments_complete=bool(aggregate["arguments"].strip()),
            )

        yield ModelResponseDone(stop_reason if calls or stop_reason != "incomplete" else "stop")

    @budgeted_model_completion
    async def complete_messages(
        self,
        messages: Sequence[Mapping[str, object]],
        configuration: ModelConfiguration,
        *,
        max_tokens: int | None = None,
    ) -> str:
        """Collect a bounded completion through the same compatible endpoint.

        Memory workers use this non-streaming-shaped helper so extraction and
        consolidation can be tested with a small fake adapter while production
        continues to use the existing streaming transport.
        """
        parts: list[str] = []
        async for delta in self.stream_messages(messages, configuration, max_tokens=max_tokens):
            parts.append(delta)
        return "".join(parts)
