from collections.abc import AsyncIterator
import json
import os
from typing import Protocol

import httpx

from .models import Message, ModelConfiguration, Role


class ModelAdapter(Protocol):
    async def stream_reply(
        self,
        role: Role,
        history: list[Message],
        configuration: ModelConfiguration | None = None,
        memory_context: str = "",
    ) -> AsyncIterator[str]: ...


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
        if configuration is None:
            base_url = os.getenv("MEIDO_MODEL_BASE_URL", "").strip().rstrip("/")
            model = os.getenv("MEIDO_MODEL", "").strip()
            api_key = os.getenv("MEIDO_API_KEY", "").strip()
        else:
            base_url = configuration.baseUrl.rstrip("/")
            model = configuration.model
            api_key = configuration.apiKey
        if not base_url or not model:
            raise RuntimeError("尚未配置模型服务，请先在模型设置中保存连接信息")

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
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend({"role": message.role, "content": message.content} for message in history)
        async for delta in self.stream_messages(
            messages,
            ModelConfiguration(
                providerId=configuration.providerId if configuration else "custom",
                provider=configuration.provider if configuration else "custom",
                baseUrl=base_url,
                model=model,
                apiKey=api_key,
            ),
        ):
            yield delta

    async def stream_messages(
        self,
        messages: list[dict[str, str]],
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
