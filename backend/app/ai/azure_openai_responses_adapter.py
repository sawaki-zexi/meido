"""Azure OpenAI Responses transport using the shared Responses converter."""

from __future__ import annotations

from .model_protocol import AZURE_OPENAI_RESPONSES_API
from .openai_responses_adapter import OpenAIResponsesAdapter


class AzureOpenAIResponsesAdapter(OpenAIResponsesAdapter):
    def __init__(self, timeout: float | None = None) -> None:
        super().__init__(timeout, api=AZURE_OPENAI_RESPONSES_API)

    def _url(self, configuration) -> str:
        base_url = configuration.baseUrl.rstrip("/")
        if base_url.endswith("/responses"):
            return base_url
        if base_url.endswith("/openai"):
            base_url += "/v1"
        elif not base_url.endswith("/openai/v1"):
            base_url += "/openai/v1"
        return f"{base_url}/responses"

    def _headers(self, configuration) -> dict[str, str]:
        return {"api-key": configuration.apiKey, "content-type": "application/json"}

    def _prepare(self, messages, configuration, tools, *, max_tokens, stream):
        payload, headers, aliases = super()._prepare(
            messages, configuration, tools, max_tokens=max_tokens, stream=stream,
        )
        deployment = configuration.apiOptions.get("deploymentName", "").strip()
        if deployment:
            payload["model"] = deployment
        return payload, headers, aliases
