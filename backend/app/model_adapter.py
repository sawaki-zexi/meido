from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
import json
import os
from typing import Protocol, Literal

import httpx

from .models import Message, ModelConfiguration, Role


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


@dataclass(frozen=True, slots=True)
class ModelStreamError:
    error: str
    type: Literal["error"] = "error"


ModelStreamEvent = ModelTextDelta | ModelToolCall | ModelStreamError


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


class OpenAICompatibleAdapter:
    """Streams chat completions from an OpenAI-compatible endpoint."""

    def __init__(self, timeout: float | None = None) -> None:
        self.timeout = timeout

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
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers=headers,
                json=request_data,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        completed = True
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
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
        request_data: dict[str, object] = {
            "model": configuration.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "stream": True,
        }
        if max_tokens is not None:
            request_data["max_tokens"] = max_tokens
        calls: dict[int, dict[str, str]] = {}
        completed = False
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers=headers,
                json=request_data,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        completed = True
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(chunk, dict) and chunk.get("error"):
                        error = chunk["error"]
                        message = error.get("message", "模型服务返回错误") if isinstance(error, dict) else str(error)
                        if configuration.apiKey:
                            message = message.replace(configuration.apiKey, "***")
                        yield ModelStreamError(message)
                        return
                    choices = chunk.get("choices", []) if isinstance(chunk, dict) else []
                    choice = choices[0] if isinstance(choices, list) and choices else {}
                    delta = choice.get("delta", {}) if isinstance(choice, dict) else {}
                    content = delta.get("content") if isinstance(delta, dict) else None
                    if isinstance(content, str) and content:
                        yield ModelTextDelta(content)
                    raw_calls = delta.get("tool_calls", []) if isinstance(delta, dict) else []
                    if not isinstance(raw_calls, list):
                        continue
                    for raw_call in raw_calls:
                        if not isinstance(raw_call, dict):
                            continue
                        raw_index = raw_call.get("index", 0)
                        index = int(raw_index) if isinstance(raw_index, int) else 0
                        aggregate = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if isinstance(raw_call.get("id"), str):
                            aggregate["id"] += raw_call["id"]
                        function = raw_call.get("function")
                        if not isinstance(function, dict):
                            continue
                        if isinstance(function.get("name"), str):
                            aggregate["name"] += function["name"]
                        arguments = function.get("arguments", "")
                        if isinstance(arguments, str):
                            aggregate["arguments"] += arguments
                        elif isinstance(arguments, dict):
                            aggregate["arguments"] += json.dumps(arguments, ensure_ascii=False)
        if not completed:
            raise RuntimeError("模型流式响应未正常结束")
        for index in sorted(calls):
            aggregate = calls[index]
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
                name=aggregate["name"],
                arguments=arguments,
            )

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
