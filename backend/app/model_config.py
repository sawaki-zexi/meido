import json
import os
import threading
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

    def get(self) -> ModelConfiguration | None:
        with self._lock:
            if not self.path.exists():
                return None
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return ModelConfiguration.model_validate(data)

    def save(self, data: ModelConfigurationInput) -> ModelConfiguration:
        validate_model_configuration(data)
        candidate = ModelConfiguration.model_validate(data.model_dump())
        payload = json.dumps(candidate.model_dump(), ensure_ascii=False, indent=2) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            try:
                temporary.write_text(payload, encoding="utf-8")
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        return candidate
