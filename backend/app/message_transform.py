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

_PLACEHOLDER_PREFIX = "[image omitted:"


def _image_placeholder(image: ModelImageBlock) -> ModelTextBlock:
    media_type = image.media_type or "unknown"
    return ModelTextBlock(f"{_PLACEHOLDER_PREFIX} {media_type}]")


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


def _transform_blocks(
    blocks: Iterable[ModelContentBlock],
    *,
    capabilities: ModelCapabilities,
    cross_model: bool,
    id_map: dict[str, str],
) -> tuple[ModelContentBlock, ...]:
    result: list[ModelContentBlock] = []
    for block in blocks:
        if isinstance(block, ModelImageBlock):
            if capabilities.supports_images:
                result.append(block)
            elif not result or not (
                isinstance(result[-1], ModelTextBlock)
                and result[-1].text.startswith(_PLACEHOLDER_PREFIX)
            ):
                result.append(_image_placeholder(block))
            continue
        if isinstance(block, ModelThinkingBlock):
            if cross_model or not capabilities.supports_reasoning:
                if block.text:
                    result.append(ModelTextBlock(block.text))
            else:
                result.append(block)
            continue
        if isinstance(block, ModelToolCallBlock):
            normalized = _normalize_id(block.id, capabilities)
            id_map.setdefault(block.id, normalized)
            result.append(
                ModelToolCallBlock(normalized, block.name, dict(block.arguments))
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
) -> ModelMessage:
    if isinstance(message, ModelSystemMessage):
        return message
    if isinstance(message, ModelUserMessage):
        return ModelUserMessage(_transform_blocks(
            message.content,
            capabilities=capabilities,
            cross_model=cross_model,
            id_map=id_map,
        ))
    if isinstance(message, ModelAssistantMessage):
        return ModelAssistantMessage(
            _transform_blocks(
                message.content,
                capabilities=capabilities,
                cross_model=cross_model,
                id_map=id_map,
            ),
            message.metadata,
        )
    return ModelToolResultMessage(
        _transform_blocks(
            message.content,
            capabilities=capabilities,
            cross_model=cross_model,
            id_map=id_map,
        ),
        id_map.get(
            message.tool_call_id,
            _normalize_id(message.tool_call_id, capabilities),
        ),
        message.tool_name,
        message.is_error,
    )


def _assistant_calls(message: ModelAssistantMessage) -> tuple[ModelToolCallBlock, ...]:
    return tuple(block for block in message.content if isinstance(block, ModelToolCallBlock))


def transform_messages(
    messages: Sequence[ModelMessage],
    *,
    capabilities: ModelCapabilities = ModelCapabilities(),
    source_model: str | None = None,
    target_model: str | None = None,
) -> tuple[ModelMessage, ...]:
    """Return a replayable transcript without mutating the caller's history.

    Tool results are kept adjacent to their assistant call. Missing results are
    represented by deterministic error results; results for unknown calls are
    omitted because providers reject those histories.
    """

    cross_model = bool(source_model and target_model and source_model != target_model)
    id_map: dict[str, str] = {}
    transformed = [
        _transform_message(
            message,
            capabilities=capabilities,
            cross_model=cross_model,
            id_map=id_map,
        )
        for message in messages
        if not (
            isinstance(message, ModelAssistantMessage)
            and message.metadata.stop_reason in {"error", "aborted"}
        )
    ]

    result: list[ModelMessage] = []
    pending: dict[str, ModelToolCallBlock] = {}
    for message in transformed:
        if isinstance(message, ModelAssistantMessage):
            if pending:
                result.extend(_missing_results(pending.values()))
                pending.clear()
            calls = _assistant_calls(message)
            pending.update({call.id: call for call in calls})
            result.append(message)
            continue
        if isinstance(message, ModelToolResultMessage):
            if message.tool_call_id not in pending:
                continue
            pending.pop(message.tool_call_id)
            result.append(message)
            continue
        if pending:
            result.extend(_missing_results(pending.values()))
            pending.clear()
        result.append(message)
    if pending:
        result.extend(_missing_results(pending.values()))
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
