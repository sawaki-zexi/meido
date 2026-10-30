"""Bounded working-summary sources and batches, adapted from Shiori 71c8b0e.

Copyright (c) 2026 YinFengWindy. MIT: third_party/shiori-agent/LICENSE.
Meido keeps source IDs and its native transcript/transaction ownership.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from .types import AgentMessage, AssistantMessage, ToolResultMessage, UserMessage

TOOL_RESULT_CHAR_BUDGET = 10000
MIN_TEXT_CHARS = 200
Record = dict[str, Any]


def truncate_summary_text(text: str, limit: int) -> str:
    """Keep the head, tail and omitted size using Shiori's tool-result rule."""
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    while True:
        marker = f'…{omitted} chars truncated…'
        keep = max(0, limit - len(marker))
        actual_omitted = len(text) - keep
        if actual_omitted == omitted:
            break
        omitted = actual_omitted
    head = keep // 2
    tail = keep - head
    truncated = text[:head] + marker + (text[-tail:] if tail else '')
    return f'Total output lines: {len(text.splitlines())}\n\n{truncated}'


def summary_source(message: AgentMessage) -> Record:
    """Only original text, attachment references and tool facts; never images."""
    record: Record = {'role': message.role, 'content': message.content,
                      'source_message_ids': message.source_message_ids}
    if isinstance(message, UserMessage):
        record['attachments'] = [{'name': item.name, 'media_type': item.media_type, 'text': item.text}
                                 for item in message.attachments]
    if isinstance(message, AssistantMessage) and message.tool_calls:
        record['tool_calls'] = [asdict(call) for call in message.tool_calls]
    if isinstance(message, ToolResultMessage):
        record['content'] = truncate_summary_text(message.content, TOOL_RESULT_CHAR_BUDGET)
        record['tool_call_id'] = message.tool_call_id
        record['tool_name'] = message.tool_name
        record['attachments'] = [{'media_type': image.media_type} for image in message.images]
    return record


def shrink_summary_source(value: Any, limit: int) -> Any:
    """Shrink long text recursively while retaining exact provenance and call IDs."""
    if isinstance(value, str):
        return truncate_summary_text(value, limit)
    if isinstance(value, dict):
        return {key: item if key in {'source_message_ids', 'id', 'tool_call_id'}
                else shrink_summary_source(item, limit) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [shrink_summary_source(item, limit) for item in value]
    return value


def next_summary_batch(previous: Record | None, pending: list[Record],
                       fits: Callable[[Record], bool]) -> tuple[int, Record]:
    """Choose the longest consecutive prefix within this state's request budget."""
    def payload(records: list[Record]) -> Record:
        return {'previous_summary': previous, 'messages': records}

    request = payload(pending)
    if fits(request):
        return len(pending), request
    low, high = 0, len(pending) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if fits(payload(pending[:middle])):
            low = middle
        else:
            high = middle - 1
    if low:
        return low, payload(pending[:low])
    if pending:
        limit = TOOL_RESULT_CHAR_BUDGET
        while limit >= MIN_TEXT_CHARS:
            request = payload([shrink_summary_source(pending[0], limit)])
            if fits(request):
                return 1, request
            limit //= 2
    raise ValueError('工作摘要请求超过模型输入预算：最低可用原文仍无法装入')
