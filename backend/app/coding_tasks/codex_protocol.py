"""Incremental stdout contract for the fixed Codex 0.159.3 exec JSONL profile."""
from __future__ import annotations

import uuid

from .executor import strict_json
from .errors import CodingFailure


class CodexProtocol:
    def __init__(self, *, expected_session=None):
        self.expected_session = expected_session
        self.session = None
        self.started = False
        self.completed = False
        self._pending = bytearray()

    def feed(self, chunk: bytes):
        self._pending.extend(chunk)
        while b'\n' in self._pending:
            line, _, rest = self._pending.partition(b'\n')
            self._pending = bytearray(rest)
            if not line or len(line) > 65536:
                raise CodingFailure('codex_jsonl_frame_invalid_or_over_budget')
            try:
                event = strict_json(line.decode('utf-8', errors='strict'))
            except ValueError:
                raise CodingFailure('codex_jsonl_invalid') from None
            self._event(event)
        if len(self._pending) > 65536:
            raise CodingFailure('codex_jsonl_frame_over_budget')

    def _event(self, event):
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            raise CodingFailure('codex_jsonl_event_invalid')
        kind = event['type']
        if kind == 'thread.started':
            session = event.get('thread_id')
            try:
                valid_uuid = isinstance(session, str) and str(uuid.UUID(session)) == session
            except ValueError:
                valid_uuid = False
            if self.session is not None or not valid_uuid or self.expected_session and self.expected_session != session:
                raise CodingFailure('codex_exact_session_mismatch')
            self.session = session
        elif kind == 'turn.started':
            if self.session is None or self.started or self.completed:
                raise CodingFailure('codex_turn_order_invalid')
            self.started = True
        elif kind == 'turn.completed':
            if not self.started or self.completed:
                raise CodingFailure('codex_turn_order_invalid')
            self.completed = True
        elif kind in {'turn.failed', 'error'}:
            raise CodingFailure('codex_native_turn_failed')
        elif kind in {'item.started', 'item.updated', 'item.completed'}:
            if not self.started or self.completed or not isinstance(event.get('item'), dict):
                raise CodingFailure('codex_item_order_invalid')
        else:
            raise CodingFailure('codex_jsonl_event_unsupported')

    def finish(self, *, returncode: int) -> str:
        if returncode != 0 or self._pending or self.session is None or not self.started or not self.completed:
            raise CodingFailure('codex_turn_incomplete')
        return self.session
