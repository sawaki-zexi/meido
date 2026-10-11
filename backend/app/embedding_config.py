from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit

from .models import EmbeddingConfiguration, EmbeddingConfigurationInput


DEFAULT_EMBEDDING_CONFIGURATION = EmbeddingConfiguration()


def validate_embedding_configuration(data: EmbeddingConfigurationInput) -> None:
    parsed = urlsplit(data.baseUrl)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("服务地址必须是有效的 HTTP 或 HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("服务地址不能包含账号、密钥、查询参数或片段")
    if parsed.path.rstrip("/").endswith("/embeddings"):
        raise ValueError("服务地址请填写 API 根地址，不要包含 /embeddings")


def public_embedding_configuration(config: EmbeddingConfiguration | None) -> dict[str, object]:
    config = config or DEFAULT_EMBEDDING_CONFIGURATION
    return {
        "provider": config.provider,
        "baseUrl": config.baseUrl,
        "model": config.model,
        "dimensions": config.dimensions,
        "apiKeyConfigured": bool(config.apiKey),
    }


class EmbeddingConfigurationStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def get(self) -> EmbeddingConfiguration:
        with self._lock:
            if not self.path.exists():
                return DEFAULT_EMBEDDING_CONFIGURATION.model_copy(deep=True)
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("向量模型配置格式无效")
            return EmbeddingConfiguration.model_validate({**DEFAULT_EMBEDDING_CONFIGURATION.model_dump(), **data})

    def save(self, data: EmbeddingConfigurationInput) -> EmbeddingConfiguration:
        validate_embedding_configuration(data)
        with self._lock:
            current = self.get()
            candidate = EmbeddingConfiguration.model_validate({
                **data.model_dump(),
                "apiKey": data.apiKey or current.apiKey,
            })
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({"version": 1, **candidate.model_dump()}, ensure_ascii=False, indent=2) + "\n"
            temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            try:
                temporary.write_text(payload, encoding="utf-8")
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            return candidate.model_copy(deep=True)
