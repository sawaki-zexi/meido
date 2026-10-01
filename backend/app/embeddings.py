from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import httpx

from .models import ModelConfiguration


class EmbeddingProvider(Protocol):
    def embed(self, role_id: str, text: str) -> list[float] | None: ...


class OpenAICompatibleEmbeddingAdapter:
    """Request vectors from the current role model provider's compatible endpoint."""

    def __init__(self, configuration_for_role: Callable[[str], ModelConfiguration | None], timeout: float = 2) -> None:
        self.configuration_for_role = configuration_for_role
        self.timeout = timeout

    def embed(self, role_id: str, text: str) -> list[float] | None:
        configuration = self.configuration_for_role(role_id)
        if configuration is None:
            return None
        response = httpx.post(
            f"{configuration.baseUrl.rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {configuration.apiKey}"} if configuration.apiKey else {},
            json={"model": configuration.model, "input": text},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        vector = payload["data"][0]["embedding"]
        if not isinstance(vector, list) or not vector or not all(isinstance(value, (int, float)) for value in vector):
            raise ValueError("embedding 响应格式无效")
        return [float(value) for value in vector]
