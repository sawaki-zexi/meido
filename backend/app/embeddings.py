from __future__ import annotations

from collections.abc import Callable
import math
import time
from typing import Protocol

import httpx

from .models import EmbeddingConfiguration, ModelConfiguration


class EmbeddingProvider(Protocol):
    def embed(self, role_id: str, text: str) -> list[float] | None: ...


class SharedEmbeddingProvider:
    """Use one configured OpenAI-compatible embedding model across memory features."""

    MAX_BATCH_SIZE = 10
    MAX_TEXT_LENGTH = 2000
    BATCH_INTERVAL_SECONDS = 0.3

    def __init__(
        self,
        configuration: Callable[[], EmbeddingConfiguration],
        timeout: float = 10,
        *,
        _state: dict[str, str | None] | None = None,
    ) -> None:
        self.configuration = configuration
        self.timeout = timeout
        self._state = _state if _state is not None else {"last_error": None}

    @property
    def last_error(self) -> str | None:
        return self._state["last_error"]

    @last_error.setter
    def last_error(self, value: str | None) -> None:
        self._state["last_error"] = value

    @property
    def model_id(self) -> str:
        config = self.configuration()
        return f"{config.provider}:{config.baseUrl.rstrip('/')}:{config.model}:dimensions={config.dimensions}"

    @property
    def model_name(self) -> str:
        return self.configuration().model

    @property
    def dimensions(self) -> int:
        return self.configuration().dimensions

    @property
    def ready(self) -> bool:
        try:
            config = self.configuration()
            return bool(config.baseUrl and config.model and config.apiKey)
        except Exception:
            return False

    def snapshot(self) -> SharedEmbeddingProvider:
        config = self.configuration()
        return SharedEmbeddingProvider(
            lambda: config.model_copy(deep=True),
            timeout=self.timeout,
            _state=self._state,
        )

    def _request(self, inputs: list[str], config: EmbeddingConfiguration | None = None) -> list[list[float]]:
        config = config or self.configuration()
        payload: dict[str, object] = {
            "model": config.model,
            "input": inputs,
            "dimensions": config.dimensions,
        }
        try:
            response = httpx.post(
                f"{config.baseUrl.rstrip('/')}/embeddings",
                headers={"Authorization": f"Bearer {config.apiKey}"} if config.apiKey else {},
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            raw = response.json().get("data")
            if not isinstance(raw, list) or len(raw) != len(inputs):
                raise ValueError("embedding 响应数量无效")
            vectors: list[list[float]] = []
            for item in sorted(raw, key=lambda value: value.get("index", 0) if isinstance(value, dict) else 0):
                vector = item.get("embedding") if isinstance(item, dict) else None
                if not isinstance(vector, list) or len(vector) != config.dimensions or not vector:
                    raise ValueError("embedding 响应向量维度无效")
                if not all(isinstance(value, (int, float)) for value in vector):
                    raise ValueError("embedding 响应向量格式无效")
                values = [float(value) for value in vector]
                norm = math.sqrt(sum(value * value for value in values))
                if not math.isfinite(norm) or norm == 0:
                    raise ValueError("embedding 响应向量不可归一化")
                vectors.append([value / norm for value in values])
            self.last_error = None
            return vectors
        except Exception as error:
            self.last_error = str(error)[:500]
            raise

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        config = self.configuration()
        bounded_texts = [text[: self.MAX_TEXT_LENGTH] for text in texts]
        vectors: list[list[float]] = []
        for offset in range(0, len(bounded_texts), self.MAX_BATCH_SIZE):
            if offset:
                time.sleep(self.BATCH_INTERVAL_SECONDS)
            vectors.extend(self._request(bounded_texts[offset : offset + self.MAX_BATCH_SIZE], config))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._request([text[: self.MAX_TEXT_LENGTH]])[0]

    def embed(self, role_id: str, text: str) -> list[float] | None:
        del role_id
        return self.embed_query(text)


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
