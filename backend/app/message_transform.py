"""Pure transcript transforms used before a provider request."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence

from .model_types import (
    ModelAssistantMessage,
    ModelCapabilities,
    ModelContentBlock,
    ModelImageBlock,
    ModelMessage,
    ModelSystemMessage,
    ModelTextBlock,
    ModelThinkingBlock,
    ModelToolCallBlock,
    ModelToolResultMessage,
    ModelUserMessage,
)

_USER_IMAGE_PLACEHOLDER = "(image omitted: model does not support images)"
_TOOL_IMAGE_PLACEHOLDER = "(tool image omitted: model does not support images)"


def _image_placeholder(*, tool_result: bool) -> ModelTextBlock:
    return ModelTextBlock(_TOOL_IMAGE_PLACEHOLDER if tool_result else _USER_IMAGE_PLACEHOLDER)


def _normalize_id(value: str, capabilities: ModelCapabilities) -> str:
    pattern = capabilities.tool_call_id_pattern
    max_length = capabilities.max_tool_call_id_length
    if (pattern is None or re.fullmatch(pattern, value)) and (
        max_length is None or len(value) <= max_length
    ):
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    prefix = "call_"
    width = max(1, (max_length or 64) - len(prefix))
    candidate = prefix + digest[:width]
    if pattern is not None and re.fullmatch(pattern, candidate) is None:
        candidate = "call_" + digest
    return candidate[:max_length] if max_length else candidate


def _unique_tool_call_id(
    value: str,
    capabilities: ModelCapabilities,
    id_map: dict[str, str],
    used_ids: set[str],
) -> str:
    existing = id_map.get(value)
    if existing is not None:
        return existing
    candidate = _normalize_id(value, capabilities)
    if candidate in used_ids:
        max_length = capabilities.max_tool_call_id_length or 64
        pattern = capabilities.tool_call_id_pattern
        for counter in range(1024):
            digest = hashlib.sha256(f"{value}\0{counter}".encode("utf-8")).hexdigest()
            candidate = f"call_{digest}"[:max_length]
            if pattern is not None and re.fullmatch(pattern, candidate) is None:
                candidate = digest[:max_length]
            if candidate not in used_ids and (
                pattern is None or re.fullmatch(pattern, candidate)
            ):
                break
        else:
            raise ValueError("无法生成符合服务商约束的唯一工具调用 ID")
    id_map[value] = candidate
    used_ids.add(candidate)
    return candidate


def _transform_blocks(
    blocks: Iterable[ModelContentBlock] | str | None,
    *,
    capabilities: ModelCapabilities,
    cross_model: bool,
    image_placeholder: str,
    id_map: dict[str, str],
    used_ids: set[str],
) -> tuple[ModelContentBlock, ...]:
    if blocks is None:
        return ()
    if isinstance(blocks, str):
        return (ModelTextBlock(blocks),) if blocks else ()
    result: list[ModelContentBlock] = []
    for block in blocks:
        if isinstance(block, ModelImageBlock):
            if capabilities.supports_images:
                result.append(block)
            elif not result or not (
                isinstance(result[-1], ModelTextBlock)
                and result[-1].text == image_placeholder
            ):
                result.append(ModelTextBlock(image_placeholder))
            continue
        if isinstance(block, ModelTextBlock):
            text_signature = (
                block.text_signature
                if not cross_model and capabilities.preserve_thinking_signatures
                else None
            )
            result.append(
                block if text_signature == block.text_signature
                else ModelTextBlock(block.text, text_signature=text_signature)
            )
            continue
        if isinstance(block, ModelThinkingBlock):
            if block.redacted:
                if (
                    not cross_model
                    and capabilities.supports_reasoning
                    and capabilities.preserve_thinking_signatures
                ):
                    result.append(block)
                continue
            if cross_model or not capabilities.supports_reasoning:
                if block.text.strip():
                    result.append(ModelTextBlock(block.text))
                continue
            if not block.text.strip() and not block.signature:
                continue
            signature = block.signature if capabilities.preserve_thinking_signatures else None
            result.append(ModelThinkingBlock(block.text, signature))
            continue
        if isinstance(block, ModelToolCallBlock):
            normalized = (
                _unique_tool_call_id(block.id, capabilities, id_map, used_ids)
                if cross_model else block.id
            )
            result.append(
                ModelToolCallBlock(
                    normalized,
                    block.name,
                    dict(block.arguments),
                    thought_signature=(
                        block.thought_signature
                        if not cross_model and capabilities.preserve_thinking_signatures
                        else None
                    ),
                    namespace=block.namespace,
                )
            )
            continue
        result.append(block)
    return tuple(result)


def _transform_message(
    message: ModelMessage,
    *,
    capabilities: ModelCapabilities,
    cross_model: bool,
    id_map: dict[str, str],
    used_ids: set[str],
) -> ModelMessage:
    if isinstance(message, ModelSystemMessage):
        blocks = _transform_blocks(
            message.content,
            capabilities=capabilities,
            cross_model=cross_model,
            image_placeholder=_USER_IMAGE_PLACEHOLDER,
            id_map=id_map,
            used_ids=used_ids,
        )
        return ModelSystemMessage(
            tuple(block for block in blocks if isinstance(block, ModelTextBlock)),
            message.tools_added,
            message.tools_removed,
        )
    if isinstance(message, ModelUserMessage):
        return ModelUserMessage(_transform_blocks(
            message.content,
            capabilities=capabilities,
            cross_model=cross_model,
            image_placeholder=_USER_IMAGE_PLACEHOLDER,
            id_map=id_map,
            used_ids=used_ids,
        ))
    if isinstance(message, ModelAssistantMessage):
        return ModelAssistantMessage(
            _transform_blocks(
                message.content,
                capabilities=capabilities,
                cross_model=cross_model,
                image_placeholder=_USER_IMAGE_PLACEHOLDER,
                id_map=id_map,
                used_ids=used_ids,
            ),
            message.metadata,
        )
    return ModelToolResultMessage(
        _transform_blocks(
            message.content,
            capabilities=capabilities,
            cross_model=cross_model,
            image_placeholder=_TOOL_IMAGE_PLACEHOLDER,
            id_map=id_map,
            used_ids=used_ids,
        ),
        id_map.get(message.tool_call_id, message.tool_call_id),
        message.tool_name,
        message.is_error,
    )


def _assistant_calls(message: ModelAssistantMessage) -> tuple[ModelToolCallBlock, ...]:
    return tuple(block for block in message.content if isinstance(block, ModelToolCallBlock))


def transform_messages(
    messages: Sequence[ModelMessage],
    *,
    capabilities: ModelCapabilities = ModelCapabilities(),
    source_provider: str | None = None,
    target_provider: str | None = None,
    source_api: str | None = None,
    target_api: str | None = None,
    source_model: str | None = None,
    target_model: str | None = None,
) -> tuple[ModelMessage, ...]:
    """Return a replayable transcript without mutating the caller's history.

    Tool results are kept adjacent to their assistant call. Missing results are
    represented by deterministic error results; results for unknown calls are
    omitted because providers reject those histories.
    """

    id_map: dict[str, str] = {}
    used_ids: set[str] = set()
    transformed: list[ModelMessage] = []
    for message in messages:
        if (
            isinstance(message, ModelAssistantMessage)
            and message.metadata.stop_reason in {"error", "aborted"}
        ):
            continue
        message_source_model = source_model
        message_source_provider = source_provider
        message_source_api = source_api
        if isinstance(message, ModelAssistantMessage):
            message_source_model = message_source_model or message.metadata.model
            message_source_provider = message_source_provider or message.metadata.provider
            message_source_api = message_source_api or message.metadata.api
        identity_pairs = (
            (message_source_provider, target_provider),
            (message_source_api, target_api),
            (message_source_model, target_model),
        )
        target_identity_known = any(target for _source, target in identity_pairs)
        identity_incomplete = target_identity_known and any(
            bool(source) != bool(target) for source, target in identity_pairs
        )
        cross_model = identity_incomplete or any(
            source and target and source != target for source, target in identity_pairs
        )
        transformed.append(_transform_message(
            message,
            capabilities=capabilities,
            cross_model=cross_model,
            id_map=id_map,
            used_ids=used_ids,
        ))

    result: list[ModelMessage] = []
    pending: dict[str, ModelToolCallBlock] = {}
    deferred_system: list[ModelSystemMessage] = []
    for message in transformed:
        if isinstance(message, ModelSystemMessage):
            if pending:
                deferred_system.append(message)
            else:
                result.append(message)
            continue
        if isinstance(message, ModelAssistantMessage):
            if pending:
                result.extend(_missing_results(pending.values()))
                pending.clear()
                result.extend(deferred_system)
                deferred_system.clear()
            calls = _assistant_calls(message)
            pending.update({call.id: call for call in calls})
            result.append(message)
            continue
        if isinstance(message, ModelToolResultMessage):
            if message.tool_call_id not in pending:
                continue
            pending.pop(message.tool_call_id)
            result.append(message)
            if not pending and deferred_system:
                result.extend(deferred_system)
                deferred_system.clear()
            continue
        if pending:
            result.extend(_missing_results(pending.values()))
            pending.clear()
            result.extend(deferred_system)
            deferred_system.clear()
        result.append(message)
    if pending:
        result.extend(_missing_results(pending.values()))
    result.extend(deferred_system)
    return tuple(result)


def _missing_results(calls: Iterable[ModelToolCallBlock]) -> tuple[ModelToolResultMessage, ...]:
    return tuple(
        ModelToolResultMessage(
            content=(ModelTextBlock("No result provided"),),
            tool_call_id=call.id,
            tool_name=call.name,
            is_error=True,
        )
        for call in calls
    )
