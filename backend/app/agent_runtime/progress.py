"""Call-local disposable snapshots; final results never enter this buffer."""
from __future__ import annotations

import asyncio
from collections import deque
import json
import math

from .tools import AgentToolResult

MAX_PENDING_SNAPSHOTS = 32
MAX_PENDING_BYTES = 256 * 1024
MAX_SNAPSHOT_BYTES = 32 * 1024
MAX_TEXT_BYTES = 16 * 1024
MAX_DETAILS_BYTES = 8 * 1024


def utf8_prefix(value: str, limit: int) -> str:
    # Slice before encoding: the host never copies an arbitrarily large string.
    return value[:limit].encode('utf-8', errors='replace')[:limit].decode('utf-8', errors='ignore')


def _encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')


def _safe_details(value: object) -> tuple[dict[str, object], bool]:
    """Bound work *before* copy/serialization, including cycles and hostile types."""
    remaining = MAX_DETAILS_BYTES - 1024  # reserve host-owned annotations
    nodes = 128
    omitted_fields = False

    def copy(item: object, depth: int = 0) -> object:
        nonlocal remaining, nodes, omitted_fields
        nodes -= 1
        if nodes < 0 or depth > 6:
            raise ValueError
        kind = type(item)
        if kind in {dict, list, tuple}:
            if len(item) > nodes:
                raise ValueError
            remaining -= 2 + 2 * len(item)
            if remaining < 0:
                raise ValueError
            if kind is dict:
                result = {}
                for key, child in item.items():
                    if type(key) is not str:
                        raise ValueError
                    copied_key = copy(key, depth + 1)
                    if key.casefold() in {"image", "images", "base64", "data_base64", "image_base64"}:
                        omitted_fields = True
                        result[copied_key] = "[omitted]"
                        remaining -= 11
                    else:
                        result[copied_key] = copy(child, depth + 1)
                return result
            return [copy(child, depth + 1) for child in item]
        if kind not in {str, int, float, bool, type(None)}:
            raise ValueError
        if kind is str and len(item) > remaining:
            raise ValueError
        if kind is int and item.bit_length() > 1024:
            raise ValueError
        if kind is float and not math.isfinite(item):
            raise ValueError
        remaining -= len(_encoded(item))
        if remaining < 0:
            raise ValueError
        return item

    try:
        safe = copy(value)
        return (safe if isinstance(safe, dict) else {} if safe is None else {'value': safe}), omitted_fields
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return {}, True


class ToolProgressBuffer:
    """Synchronous callbacks coalesce oldest snapshots, without spawning tasks."""

    def __init__(self, *, output_limit: int | None = None, envelope_overhead: int = 2048) -> None:
        self.output_limit = output_limit
        self.envelope_overhead = envelope_overhead
        self.ready = asyncio.Event()
        self._pending: deque[tuple[AgentToolResult, int]] = deque()
        self.pending_bytes = 0
        self.peak_count = 0
        self.peak_bytes = 0
        self.dropped_updates = 0
        self.accepted_updates = 0
        self.closed = False

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def add(self, result: AgentToolResult) -> None:
        if self.closed or not isinstance(result, AgentToolResult) or not isinstance(result.content, str):
            return
        if self.envelope_overhead > MAX_SNAPSHOT_BYTES - MAX_TEXT_BYTES - MAX_DETAILS_BYTES:
            self.dropped_updates += 1
            return
        is_error = result.is_error is True
        limit = min(MAX_TEXT_BYTES, self.output_limit) if self.output_limit is not None else MAX_TEXT_BYTES
        content = utf8_prefix(result.content, limit)
        details, omitted = _safe_details(result.details)
        details['status'] = 'running'
        details['outputChars'] = len(result.content)
        details['truncated'] = content != result.content
        if self.output_limit is not None:
            details['outputLimit'] = self.output_limit
        if omitted:
            details['detailsOmitted'] = True
        if result.images:
            details['imagesOmitted'] = len(result.images)
        # Reserve transport IDs, sequence and counters. JSON escaping (notably
        # control characters) can be larger than the text's UTF-8 byte count.
        while len(_encoded({'result': content, 'details': details, 'isError': is_error})) > MAX_SNAPSHOT_BYTES - self.envelope_overhead:
            content = utf8_prefix(content, max(0, len(content.encode('utf-8')) // 2))
            details['truncated'] = True
        snapshot = AgentToolResult(content, details=details, is_error=is_error)
        size = len(_encoded({'result': content, 'details': details, 'isError': is_error})) + self.envelope_overhead
        while self._pending and (len(self._pending) >= MAX_PENDING_SNAPSHOTS or self.pending_bytes + size > MAX_PENDING_BYTES):
            _, discarded_size = self._pending.popleft()
            self.pending_bytes -= discarded_size
            self.dropped_updates += 1
        self._pending.append((snapshot, size))
        self.pending_bytes += size
        self.accepted_updates += 1
        self.peak_count = max(self.peak_count, len(self._pending))
        self.peak_bytes = max(self.peak_bytes, self.pending_bytes)
        self.ready.set()

    def pop(self) -> AgentToolResult:
        result, size = self._pending.popleft()
        self.pending_bytes -= size
        if not self._pending:
            self.ready.clear()
        return result

    def close(self, *, discard: bool = False) -> None:
        self.closed = True
        if discard:
            self.dropped_updates += len(self._pending)
            self._pending.clear()
            self.pending_bytes = 0
        self.ready.set()
