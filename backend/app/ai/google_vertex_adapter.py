"""Google Vertex Generative AI adapter with ADC support."""

from __future__ import annotations

import asyncio
from urllib.parse import quote

from .google_adapter import GoogleGenerativeAdapter
from .model_protocol import GOOGLE_VERTEX_API


def _google_adc_token() -> str:
    import google.auth
    from google.auth.transport.requests import Request

    credentials, _project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials.refresh(Request())
    if not credentials.token:
        raise RuntimeError("Google Vertex ADC 未能获取访问令牌")
    return credentials.token


class GoogleVertexAdapter(GoogleGenerativeAdapter):
    def __init__(self, timeout: float | None = None) -> None:
        super().__init__(timeout, api=GOOGLE_VERTEX_API)

    def _url(self, configuration, *, stream: bool) -> str:
        project = configuration.apiOptions.get("project", "").strip()
        location = configuration.apiOptions.get("location", "us-central1").strip()
        if not project:
            raise ValueError("Google Vertex 协议需要填写 Google Cloud 项目 ID")
        if not location:
            raise ValueError("Google Vertex 协议需要填写 Google Cloud 区域")
        base_url = configuration.baseUrl.rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]
        suffix = ":streamGenerateContent?alt=sse" if stream else ":generateContent"
        return (
            f"{base_url}/v1/projects/{quote(project, safe='')}/locations/{quote(location, safe='')}"
            f"/publishers/google/models/{quote(configuration.model, safe='')}{suffix}"
        )

    async def _authorize(self, configuration, headers: dict[str, str]) -> str:
        project = configuration.apiOptions.get("project", "").strip()
        if project:
            headers["x-goog-user-project"] = project
        if configuration.apiKey:
            return configuration.apiKey
        token = await asyncio.to_thread(_google_adc_token)
        headers["authorization"] = f"Bearer {token}"
        return token
