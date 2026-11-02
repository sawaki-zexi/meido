"""Independent host Markdown optimizer, adapted from Shiori c14152ed.

Copyright (c) 2026 YinFengWindy. MIT: third_party/shiori-agent/LICENSE.
"""
from __future__ import annotations

import asyncio
import json
import logging
import inspect
from datetime import datetime, timezone
from pathlib import Path

from .memory_optimizer import MemoryOptimizer
from .memory_document_lock import document_lock, MarkdownCommitGuard
from .memory_schema import DOCUMENT_DEFAULTS, normalize_memory_document, pending_body
from .memory_markdown_prompts import (_MERGE_SYSTEM, _MERGE_PROMPT, _SELF_SYSTEM,
    _SELF_PROMPT, _SELF_REWRITE_NOTE)

logger = logging.getLogger(__name__)


class HostMemoryOptimizer(MemoryOptimizer):
    SELF_RULES_VERSION = 2

    @classmethod
    def should_run(cls, roles_root, role_id, *, now=None, interval_seconds=MemoryOptimizer.DEFAULT_INTERVAL_SECONDS):
        """Startup eligibility follows the fixed service's overdue-only pass."""
        folder = Path(roles_root) / role_id / 'memory'
        if not folder.is_dir():
            return False
        state = cls._read_state(folder / '.optimizer.json')
        if state.get('self_rules_version') != cls.SELF_RULES_VERSION:
            return True
        try:
            previous = datetime.fromisoformat(str(state.get('last_memory_optimized_at') or ''))
        except ValueError:
            return True
        if previous.tzinfo is None:
            previous = previous.astimezone()
        return ((now or datetime.now(timezone.utc)).astimezone() - previous).total_seconds() >= interval_seconds

    def __init__(self, roles_root, request, *, enabled=lambda: True, step_delay_seconds=15, now=None):
        super().__init__(roles_root, now=now)
        self.request, self.enabled, self.step_delay_seconds = request, enabled, step_delay_seconds

    async def _request(self, *args):
        guard = MarkdownCommitGuard()
        try:
            kwargs = {'commit_guard': guard} if 'commit_guard' in inspect.signature(self.request).parameters else {}
            return str(await asyncio.to_thread(self.request, *args, **kwargs) or '').strip()
        except asyncio.CancelledError:
            guard.cancel()
            raise
        except Exception:
            logger.warning('Markdown optimization request failed', exc_info=True)
            return ''

    async def optimize(self, role_id):
        from .memory_documents import MemoryDocuments
        memory_dir = MemoryDocuments(self.roles_root).memory_dir(role_id)
        if not memory_dir.exists() or not self.enabled():
            return False
        pending, snapshot = memory_dir / 'PENDING.md', memory_dir / 'PENDING.snapshot.md'
        memory, self_file, history = (memory_dir / name for name in ('MEMORY.md', 'SELF.md', 'HISTORY.md'))
        state_file = memory_dir / '.optimizer.json'
        lock = document_lock(memory_dir)
        with lock:
            state = self._read_state(state_file)
            self._recover_pending_snapshot(pending, snapshot, committed=state.get('pending_snapshot_committed') is True)
            state.pop('pending_snapshot_committed', None)
            self._snapshot_pending(pending, snapshot)
            facts = self._read_text(snapshot)
            old_memory = self._read_text(memory)
            rewrite = int(state.get('self_rules_version', 0)) < self.SELF_RULES_VERSION
        changed = False
        committed = False
        try:
            merge_needed = bool(old_memory.strip() or pending_body(facts))
            if not merge_needed and not rewrite and not state.get('self_update_pending'):
                return False
            if merge_needed:
                merged = await self._request(role_id, _MERGE_SYSTEM, _MERGE_PROMPT.format(
                    today=self.now().strftime('%Y-%m-%d'), memory=old_memory or '（空）', pending=facts or '（无新内容）'), 16384)
                with lock:
                    if not self.enabled() or self._read_text(memory) != old_memory:
                        return False
                    if merged:
                        state.update(pending_snapshot_committed=True, self_update_pending=True)
                        writes = {memory: normalize_memory_document('MEMORY.md', merged),
                            state_file: json.dumps(state, ensure_ascii=False, indent=2) + '\n'}
                        if pending_body(facts):
                            writes[history] = self._read_text(history).rstrip() + '\n\n[memory_optimizer] PENDING 归档:\n' + facts
                        self.commit_documents(writes)
                        committed = changed = True
                        snapshot.unlink(missing_ok=True)
                        state.pop('pending_snapshot_committed', None)
                    else:
                        self._restore_pending_snapshot(pending, snapshot)
                # The fixed optimizer attempts SELF even if MEMORY failed.
                if self.step_delay_seconds:
                    await asyncio.sleep(self.step_delay_seconds)
            if not self.enabled():
                return changed
            with lock:
                old_self = self._read_text(self_file)
            updated = await self._request(role_id, _SELF_SYSTEM, _SELF_PROMPT.format(
                self_content=old_self or DOCUMENT_DEFAULTS['SELF.md'], pending=facts or '（无新内容）',
                rewrite_note=_SELF_REWRITE_NOTE if rewrite else ''), 2048)
            with lock:
                if self.enabled() and updated and self._read_text(self_file) == old_self:
                    self.commit_documents({self_file: normalize_memory_document('SELF.md', updated)})
                    state.update(self_rules_version=self.SELF_RULES_VERSION)
                    state.pop('self_update_pending', None)
                    changed = True
                state['last_memory_optimized_at'] = self.now().isoformat()
                self._write_state(state_file, state)
            return changed
        finally:
            with lock:
                if committed:
                    # A failed unlink must not resurrect facts already merged.
                    snapshot.unlink(missing_ok=True)
                else:
                    self._restore_pending_snapshot(pending, snapshot)
