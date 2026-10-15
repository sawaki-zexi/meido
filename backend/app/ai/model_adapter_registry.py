"""Select the wire adapter declared by a saved model configuration."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import aclosing

from .anthropic_adapter import AnthropicMessagesAdapter
from .azure_openai_responses_adapter import AzureOpenAIResponsesAdapter
from .bedrock_adapter import BedrockConverseAdapter
from .google_adapter import GoogleGenerativeAdapter
from .google_vertex_adapter import GoogleVertexAdapter
from .mistral_adapter import MistralConversationsAdapter, _capabilities as mistral_capabilities
from .model_protocol import (
    ANTHROPIC_MESSAGES_API, BEDROCK_CONVERSE_API, GOOGLE_GENERATIVE_API,
    MISTRAL_CONVERSATIONS_API, OPENAI_COMPLETIONS_API, OPENAI_RESPONSES_API,
    PI_MESSAGES_API, AZURE_OPENAI_RESPONSES_API, OPENAI_CODEX_RESPONSES_API,
    GOOGLE_VERTEX_API, json_object,
)
from .model_types import (
    ModelAssistantMessage, ModelContentBlock, ModelImageBlock, ModelMessage,
    ModelSystemMessage, ModelTextBlock, ModelThinkingBlock, ModelToolCallBlock,
    ModelToolResultMessage, ModelUserMessage,
)
from ..models import ModelConfiguration
from .model_adapter import OpenAICompatibleAdapter
from .openai_responses_adapter import OpenAIResponsesAdapter
from .openai_codex_responses_adapter import OpenAICodexResponsesAdapter
from .pi_messages_adapter import PiMessagesAdapter


class ModelAdapterRegistry:
    def __init__(self, *, timeout: float | None = None) -> None:
        self._adapters = {
            OPENAI_COMPLETIONS_API: OpenAICompatibleAdapter(timeout=timeout),
            OPENAI_RESPONSES_API: OpenAIResponsesAdapter(timeout=timeout),
            ANTHROPIC_MESSAGES_API: AnthropicMessagesAdapter(timeout=timeout),
            GOOGLE_GENERATIVE_API: GoogleGenerativeAdapter(timeout=timeout),
            MISTRAL_CONVERSATIONS_API: MistralConversationsAdapter(timeout=timeout),
            BEDROCK_CONVERSE_API: BedrockConverseAdapter(timeout=timeout),
            PI_MESSAGES_API: PiMessagesAdapter(timeout=timeout),
            AZURE_OPENAI_RESPONSES_API: AzureOpenAIResponsesAdapter(timeout=timeout),
            OPENAI_CODEX_RESPONSES_API: OpenAICodexResponsesAdapter(timeout=timeout),
            GOOGLE_VERTEX_API: GoogleVertexAdapter(timeout=timeout),
        }

    def get(self, configuration: ModelConfiguration | None):
        api = configuration.api if configuration is not None else OPENAI_COMPLETIONS_API
        try:
            return self._adapters[api]
        except KeyError as error:
            raise ValueError(f"不支持的模型协议: {api}") from error

    def supports_images(self, configuration: ModelConfiguration) -> bool:
        adapter = self.get(configuration)
        if configuration.api == MISTRAL_CONVERSATIONS_API:
            return mistral_capabilities(configuration.model).supports_images
        capabilities = getattr(adapter, "capabilities", None)
        options = getattr(adapter, "options", None)
        capabilities = capabilities or getattr(options, "capabilities", None)
        return capabilities.supports_images if capabilities is not None else True


class RoutedModelAdapter:
    """Legacy Runtime surface that routes every call using its config snapshot."""

    def __init__(self, registry: ModelAdapterRegistry) -> None:
        self.registry = registry

    def stream_reply(self, role, history, configuration=None, memory_context=""):
        configuration = OpenAICompatibleAdapter._effective_configuration(configuration)

        async def stream() -> AsyncIterator[str]:
            profile = role.profile
            system = "\n\n".join(part for part in (
                f"你正在扮演 {role.name}。",
                f"简介：{role.description}" if role.description else "",
                f"角色设定：{profile.profile}",
                f"性格：{profile.personality}" if profile.personality else "",
                f"行为规则：{profile.behaviorRules}" if profile.behaviorRules else "",
                f"回复约束：{profile.responseConstraints}" if profile.responseConstraints else "",
                f"称呼用户：{profile.nickname}" if profile.nickname else "",
                memory_context,
            ) if part)
            messages = [{"role": "system", "content": system}]
            messages.extend({"role": item.role, "content": item.content} for item in history)
            async for event in self.stream_messages(messages, configuration):
                yield event
        return stream()

    async def complete_messages(self, messages, configuration, *, max_tokens=None):
        adapter = self.registry.get(configuration)
        if configuration is None or configuration.api == OPENAI_COMPLETIONS_API:
            return await adapter.complete_messages(messages, configuration, max_tokens=max_tokens)
        rich = getattr(adapter, "complete_messages", None)
        if rich is None:
            raise ValueError(f"模型协议不支持 completion: {configuration.api}")
        from .model_types import ModelTextBlock
        result = await rich(_convert_legacy_messages(messages), configuration, max_tokens=max_tokens)
        return "".join(block.text for block in result.content if isinstance(block, ModelTextBlock))

    def stream_messages(self, messages, configuration, *, max_tokens=None):
        adapter = self.registry.get(configuration)
        if configuration is None or configuration.api == OPENAI_COMPLETIONS_API:
            return adapter.stream_messages(messages, configuration, max_tokens=max_tokens)

        async def stream() -> AsyncIterator[str]:
            request = _convert_legacy_messages(messages)
            async for event in adapter.stream_messages(request, configuration, max_tokens=max_tokens):
                if getattr(event, "type", None) == "text_delta":
                    yield event.text
                elif getattr(event, "type", None) == "error":
                    raise RuntimeError(event.error)
        return stream()

    def stream_structured_messages(self, messages, configuration, tools, *, max_tokens=None):
        adapter = self.registry.get(configuration)
        if configuration is None or configuration.api == OPENAI_COMPLETIONS_API:
            return adapter.stream_structured_messages(messages, configuration, tools, max_tokens=max_tokens)
        return _stream_legacy_structured(adapter, messages, configuration, tools, max_tokens=max_tokens)

    def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
        adapter = self.registry.get(configuration)
        stream = getattr(adapter, "stream_rich_messages", adapter.stream_messages)
        return stream(
            messages, configuration, tools, max_tokens=max_tokens, cancel_check=cancel_check,
        )

    async def complete_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None):
        adapter = self.registry.get(configuration)
        if configuration is None or configuration.api == OPENAI_COMPLETIONS_API:
            return await adapter.complete_rich_messages(
                messages,
                configuration,
                tools,
                max_tokens=max_tokens,
            )
        return await adapter.complete_messages(messages, configuration, tools, max_tokens=max_tokens)


def _convert_legacy_messages(messages: Sequence[Mapping[str, object]]) -> list[ModelMessage]:
    result: list[ModelMessage] = []
    call_names: dict[str, str] = {}
    legacy_call_ids: dict[str, str] = {}
    for index, message in enumerate(messages):
        role = message.get("role")
        content = _legacy_content_blocks(message.get("content"))
        if role in ("system", "developer"):
            result.append(ModelSystemMessage(tuple(block for block in content if isinstance(block, ModelTextBlock))))
        elif role == "user":
            result.append(ModelUserMessage(content))
        elif role == "assistant":
            blocks = list(content)
            reasoning = message.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                blocks.insert(0, ModelThinkingBlock(reasoning))
            calls = message.get("tool_calls", [])
            legacy = message.get("function_call")
            if isinstance(legacy, Mapping) and not calls:
                call_id = f"legacy-call-{index}"
                calls = [{"id": call_id, "function": legacy}]
                legacy_call_ids[str(legacy.get("name", ""))] = call_id
            if not isinstance(calls, list):
                raise ValueError("工具调用列表必须是数组")
            for call in calls:
                function = call.get("function") if isinstance(call, Mapping) else None
                if not isinstance(function, Mapping):
                    raise ValueError("工具调用缺少 function 对象")
                name = function.get("name")
                if not isinstance(name, str) or not name:
                    raise ValueError("工具调用缺少有效名称")
                call_id = call.get("id")
                if not isinstance(call_id, str) or not call_id:
                    raise ValueError("工具调用缺少有效 ID")
                blocks.append(ModelToolCallBlock(call_id, name, json_object(function.get("arguments", {}))))
                call_names[call_id] = name
            result.append(ModelAssistantMessage(tuple(blocks)))
        elif role in ("tool", "function"):
            call_id = message.get("tool_call_id") if role == "tool" else legacy_call_ids.get(str(message.get("name", "")))
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("工具结果缺少有效调用 ID")
            name = message.get("name", call_names.get(call_id, ""))
            result.append(ModelToolResultMessage(content, call_id, name if isinstance(name, str) else ""))
    return result


def _legacy_content_blocks(content: object) -> tuple[ModelContentBlock, ...]:
    if isinstance(content, str):
        return (ModelTextBlock(content),) if content else ()
    blocks: list[ModelContentBlock] = []
    for part in content if isinstance(content, list) else ():
        if not isinstance(part, Mapping):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            blocks.append(ModelTextBlock(part["text"]))
        elif part.get("type") == "image_url":
            image = part.get("image_url")
            url = image.get("url", "") if isinstance(image, Mapping) else image
            if not isinstance(url, str):
                raise ValueError("图片 URL 必须是字符串")
            prefix, separator, data = url.partition(";base64,")
            if prefix.startswith("data:image/") and separator:
                detail = image.get("detail", "auto") if isinstance(image, Mapping) else "auto"
                blocks.append(ModelImageBlock(prefix[5:], data, detail if detail in ("low", "high", "auto") else "auto"))
            else:
                blocks.append(ModelTextBlock("[image omitted: remote image]"))
    return tuple(blocks)


async def _stream_legacy_structured(adapter, messages, configuration, tools, *, max_tokens):
    from .model_adapter import ModelStreamError, ModelTextDelta, ModelToolCall
    from .model_types import ModelStreamError as RichError, ModelTextDelta as RichTextDelta, ModelToolCallEnd
    async with aclosing(adapter.stream_rich_messages(_convert_legacy_messages(messages), configuration, tools, max_tokens=max_tokens)) as stream:
        async for event in stream:
            if isinstance(event, RichTextDelta):
                yield ModelTextDelta(event.text)
            elif isinstance(event, ModelToolCallEnd):
                yield ModelToolCall(event.call.id, event.call.name, event.call.arguments)
            elif isinstance(event, RichError):
                yield ModelStreamError(event.error)
