from collections.abc import AsyncIterator
import json
import os
from typing import Protocol

import httpx

from .models import Message, Role


class ModelAdapter(Protocol):
    async def stream_reply(self, role: Role, history: list[Message]) -> AsyncIterator[str]: ...


class OpenAICompatibleAdapter:
    """Streams chat completions from an OpenAI-compatible endpoint."""

    async def stream_reply(self, role: Role, history: list[Message]) -> AsyncIterator[str]:
        base_url = os.getenv("MEIDO_MODEL_BASE_URL", "").strip().rstrip("/")
        model = os.getenv("MEIDO_MODEL", "").strip()
        api_key = os.getenv("MEIDO_API_KEY", "").strip()
        if not base_url or not model:
            raise RuntimeError("尚未配置模型服务，请设置 MEIDO_MODEL_BASE_URL 和 MEIDO_MODEL")

        profile = role.profile
        system_prompt = "\n\n".join(part for part in [
            f"你正在扮演 {role.name}。",
            f"简介：{role.description}" if role.description else "",
            f"角色设定：{profile.profile}",
            f"性格：{profile.personality}" if profile.personality else "",
            f"行为规则：{profile.behaviorRules}" if profile.behaviorRules else "",
            f"回复约束：{profile.responseConstraints}" if profile.responseConstraints else "",
            f"称呼用户：{profile.nickname}" if profile.nickname else "",
        ] if part)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend({"role": message.role, "content": message.content} for message in history)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers=headers,
                json={"model": model, "messages": messages, "stream": True},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
                    if isinstance(delta, str) and delta:
                        yield delta
