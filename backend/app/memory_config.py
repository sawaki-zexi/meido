from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from pydantic import BaseModel


class MemorySettings(BaseModel):
    enabled: bool = True
    embeddingEnabled: bool = True


class MemorySettingsStore:
    """Persist the application-wide memory lifecycle switches."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def get(self) -> MemorySettings:
        with self._lock:
            if not self.path.exists():
                return MemorySettings()
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("记忆设置格式无效")
            return MemorySettings.model_validate(raw)

    def save(self, settings: MemorySettings) -> MemorySettings:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({"version": 1, **settings.model_dump()}, ensure_ascii=False, indent=2) + "\n"
            temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            try:
                temporary.write_text(payload, encoding="utf-8")
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            return settings.model_copy(deep=True)

    def update(self, *, enabled: bool | None = None, embedding_enabled: bool | None = None) -> MemorySettings:
        current = self.get()
        return self.save(current.model_copy(update={
            "enabled": current.enabled if enabled is None else enabled,
            "embeddingEnabled": current.embeddingEnabled if embedding_enabled is None else embedding_enabled,
        }))
