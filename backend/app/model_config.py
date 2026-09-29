import json
import os
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .models import ModelConfiguration, ModelConfigurationInput


PROVIDER_PRESETS = (
    {
        "id": "openai",
        "label": "OpenAI",
        "provider": "openai",
        "baseUrl": "https://api.openai.com/v1",
        "modelHint": "gpt-4.1-mini",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "provider": "deepseek",
        "baseUrl": "https://api.deepseek.com",
        "modelHint": "deepseek-chat",
    },
    {
        "id": "dashscope",
        "label": "阿里云百炼",
        "provider": "dashscope",
        "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "modelHint": "qwen-plus",
    },
    {
        "id": "moonshot",
        "label": "Moonshot",
        "provider": "moonshot",
        "baseUrl": "https://api.moonshot.cn/v1",
        "modelHint": "kimi-k2-0905-preview",
    },
    {
        "id": "zhipu",
        "label": "智谱",
        "provider": "zhipu",
        "baseUrl": "https://open.bigmodel.cn/api/paas/v4",
        "modelHint": "glm-4.5",
    },
    {
        "id": "siliconflow",
        "label": "硅基流动",
        "provider": "siliconflow",
        "baseUrl": "https://api.siliconflow.cn/v1",
        "modelHint": "Qwen/Qwen3-32B",
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "provider": "openrouter",
        "baseUrl": "https://openrouter.ai/api/v1",
        "modelHint": "openai/gpt-4.1-mini",
    },
    {
        "id": "ollama",
        "label": "Ollama",
        "provider": "ollama",
        "baseUrl": "http://localhost:11434/v1",
        "modelHint": "qwen3:8b",
    },
)
def validate_model_configuration(data: ModelConfigurationInput) -> None:
    preset = next((item for item in PROVIDER_PRESETS if item["id"] == data.providerId), None)
    if preset is None and data.providerId != "custom":
        raise ValueError("未知的服务商预设")
    if preset and data.provider != preset["provider"]:
        raise ValueError("服务商标识与所选预设不匹配")
    parsed = urlsplit(data.baseUrl)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("服务地址必须是有效的 HTTP 或 HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("服务地址不能包含账号、密钥、查询参数或片段")
    if parsed.path.rstrip("/").endswith("/chat/completions"):
        raise ValueError("服务地址请填写 API 根地址，不要包含 /chat/completions")


def public_configuration(config: ModelConfiguration | None) -> dict | None:
    if config is None:
        return None
    return {
        "id": config.id,
        "providerId": config.providerId,
        "provider": config.provider,
        "baseUrl": config.baseUrl,
        "model": config.model,
        "apiKeyConfigured": bool(config.apiKey),
    }


class ModelConfigurationStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def _read(self) -> tuple[list[ModelConfiguration], str | None]:
        if not self.path.exists():
            return [], None
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("configurations"), list):
            configurations = [ModelConfiguration.model_validate(item) for item in data["configurations"]]
            active_id = data.get("activeId")
            return configurations, str(active_id) if active_id else (configurations[0].id if configurations else None)
        if isinstance(data, dict) and data.get("providerId"):
            legacy = ModelConfiguration.model_validate({**data, "id": data.get("id") or "model-default"})
            return [legacy], legacy.id
        return [], None

    def _write(self, configurations: list[ModelConfiguration], active_id: str | None) -> None:
        payload = json.dumps(
            {
                "version": 2,
                "activeId": active_id,
                "configurations": [item.model_dump() for item in configurations],
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def list(self) -> list[ModelConfiguration]:
        with self._lock:
            configurations, _ = self._read()
            return [item.model_copy(deep=True) for item in configurations]

    def active_id(self) -> str | None:
        with self._lock:
            _, active_id = self._read()
            return active_id

    def get(self, configuration_id: str | None = None) -> ModelConfiguration | None:
        with self._lock:
            configurations, active_id = self._read()
            target_id = configuration_id or active_id
            item = next((item for item in configurations if item.id == target_id), None)
            return item.model_copy(deep=True) if item else None

    def save(self, data: ModelConfigurationInput) -> ModelConfiguration:
        validate_model_configuration(data)
        with self._lock:
            configurations, active_id = self._read()
            current = next((item for item in configurations if item.id == active_id), None)
            candidate = ModelConfiguration.model_validate({**data.model_dump(), "id": current.id if current else f"model-{uuid.uuid4().hex}"})
            if current:
                configurations = [candidate if item.id == current.id else item for item in configurations]
            else:
                configurations.append(candidate)
            self._write(configurations, candidate.id)
            return candidate.model_copy(deep=True)

    def create(self, data: ModelConfigurationInput) -> ModelConfiguration:
        validate_model_configuration(data)
        with self._lock:
            configurations, _ = self._read()
            candidate = ModelConfiguration.model_validate(data.model_dump())
            configurations.append(candidate)
            self._write(configurations, candidate.id)
            return candidate.model_copy(deep=True)

    def update(self, configuration_id: str, data: ModelConfigurationInput) -> ModelConfiguration:
        validate_model_configuration(data)
        with self._lock:
            configurations, _ = self._read()
            if not any(item.id == configuration_id for item in configurations):
                raise KeyError(configuration_id)
            candidate = ModelConfiguration.model_validate({**data.model_dump(), "id": configuration_id})
            configurations = [candidate if item.id == configuration_id else item for item in configurations]
            self._write(configurations, candidate.id)
            return candidate.model_copy(deep=True)

    def activate(self, configuration_id: str) -> ModelConfiguration:
        with self._lock:
            configurations, _ = self._read()
            candidate = next((item for item in configurations if item.id == configuration_id), None)
            if candidate is None:
                raise KeyError(configuration_id)
            self._write(configurations, configuration_id)
            return candidate.model_copy(deep=True)

    def delete(self, configuration_id: str) -> str | None:
        with self._lock:
            configurations, active_id = self._read()
            if not any(item.id == configuration_id for item in configurations):
                raise KeyError(configuration_id)
            remaining = [item for item in configurations if item.id != configuration_id]
            next_active = active_id if active_id != configuration_id else (remaining[0].id if remaining else None)
            self._write(remaining, next_active)
            return next_active
