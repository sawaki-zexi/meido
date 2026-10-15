"""HTTP SSE transport for OpenAI Codex Responses."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping

from .model_protocol import OPENAI_CODEX_RESPONSES_API
from .openai_responses_adapter import OpenAIResponsesAdapter


class OpenAICodexResponsesAdapter(OpenAIResponsesAdapter):
    def __init__(self, timeout: float | None = None) -> None:
        super().__init__(timeout, api=OPENAI_CODEX_RESPONSES_API)

    def _url(self, configuration) -> str:
        base_url = configuration.baseUrl.rstrip("/")
        if base_url.endswith("/codex/responses"):
            return base_url
        if base_url.endswith("/codex"):
            return f"{base_url}/responses"
        return f"{base_url}/codex/responses"

    def _prepare(self, messages, configuration, tools, *, max_tokens, stream):
        payload, _headers, aliases = super()._prepare(
            messages, configuration, tools, max_tokens=max_tokens, stream=stream,
        )
        payload["store"] = False
        from .model_types import ModelSystemMessage

        instructions = [
            "".join(block.text for block in message.content)
            for message in messages
            if isinstance(message, ModelSystemMessage)
        ]
        payload["instructions"] = "\n\n".join(value for value in instructions if value) or "You are a helpful assistant."
        payload["input"] = [item for item in payload["input"] if item.get("role") != "system"]
        payload["include"] = ["reasoning.encrypted_content"]
        payload["tool_choice"] = "auto"
        payload["parallel_tool_calls"] = True
        payload["text"] = {"verbosity": "low"}
        return payload, self._headers(configuration), aliases

    def _headers(self, configuration):
        account_id = configuration.apiOptions.get("accountId", "").strip()
        if not account_id:
            token_parts = configuration.apiKey.split(".")
            if len(token_parts) == 3:
                try:
                    payload = token_parts[1] + "=" * (-len(token_parts[1]) % 4)
                    claims = json.loads(base64.urlsafe_b64decode(payload))
                    auth_claim = claims.get("https://api.openai.com/auth", {}) if isinstance(claims, Mapping) else {}
                    candidate = auth_claim.get("chatgpt_account_id", "") if isinstance(auth_claim, Mapping) else ""
                    account_id = candidate.strip() if isinstance(candidate, str) else ""
                except (ValueError, json.JSONDecodeError):
                    account_id = ""
        if not account_id:
            raise ValueError("OpenAI Codex Responses 需要 ChatGPT Account ID 或包含账户声明的 OAuth token")
        return {
            "Authorization": f"Bearer {configuration.apiKey}",
            "chatgpt-account-id": account_id,
            "originator": "pi",
            "openai-beta": "responses=experimental",
            "accept": "text/event-stream",
            "content-type": "application/json",
        }

    async def complete_messages(self, messages, configuration, tools=(), *, max_tokens=None):
        from .model_types import ModelStreamDone, ModelStreamError

        result = None
        async for event in self.stream_messages(messages, configuration, tools, max_tokens=max_tokens):
            if isinstance(event, ModelStreamError):
                raise RuntimeError(event.error)
            if isinstance(event, ModelStreamDone):
                result = event.message
        if result is None:
            raise RuntimeError("Codex Responses 流未返回最终消息")
        return result
