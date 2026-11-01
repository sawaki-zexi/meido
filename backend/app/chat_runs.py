"""Desktop task ownership and bounded, replaceable SSE subscriptions.

Task/previous-turn/terminal ordering follows Shiori be6343d's DesktopChatService.
Copyright (c) 2026 YinFengWindy. MIT: third_party/shiori-agent/LICENSE.
RuntimeManager remains the executor and cancellation/role-lock owner.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from .agent_runtime.progress import utf8_prefix
from .agent_runtime.runtime import RuntimeManager, complete_despite_cancellation
from .models import AgentRun, Message
from .session_store import SessionStore

MAX_EVENTS = 128
MAX_EVENT_BYTES = 256 * 1024
MAX_TOOL_CALLS = 32
MAX_ACTIVE_RUNS = 128
MAX_FINISHED_RUNS = 16
TERMINAL_EVENTS = {'assistant_completed', 'assistant_failed'}
OWNER_FAILURE = '回复执行或收尾失败，部分内容可能未保存，请重新检查'
logger = logging.getLogger(__name__)


def encode_event(kind: str, payload: dict[str, Any]) -> str:
    return f'event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'


class ChatRun:
    def __init__(self, run: AgentRun) -> None:
        # Keep identity, not a second copy of the full prompt/configuration.
        self.run = run.model_copy(update={'modelSnapshot': {}})
        self.sequence = 0
        self.phase = 'queued' if run.status == 'queued' else 'preparing'
        self.finished = False
        self.started = False
        self.cancel_requested = False
        self.task: asyncio.Task[None] | None = None
        self.invalidate: Callable[[], None] | None = None
        # Read the owner's current placeholder only when recovering a view.
        # The owner already accumulates text; do not keep another token buffer.
        self.current_message: Callable[[], Message | None] | None = None
        self.calls: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.events: deque[tuple[int, str, int]] = deque()
        self.event_bytes = 0
        self.changed = asyncio.Event()
        self.terminal: tuple[str, dict[str, Any]] | None = None

    def emit(self, kind: str, payload: dict[str, Any]) -> None:
        # A terminal unlocks the next send. Do not publish it while the owner
        # still holds persistence, tool cleanup or natural-sequence obligations.
        if kind in TERMINAL_EVENTS and not self.finished:
            self.terminal = (kind, payload)
            return
        self.sequence += 1
        payload = {**payload, 'runId': self.run.runId, 'sequence': self.sequence}
        if kind.startswith('tool_execution_'):
            call_id = str(payload['toolCallId'])
            call = self.calls.setdefault(call_id, {'toolCallId': call_id,
                'toolName': payload['toolName'], 'result': '', 'status': 'running', 'droppedUpdates': 0})
            for key in ('argumentsPreview', 'result', 'status', 'droppedUpdates'):
                if key in payload:
                    call[key] = utf8_prefix(str(payload[key]), 16 * 1024) if key == 'result' else payload[key]
            while len(self.calls) > MAX_TOOL_CALLS:
                self.calls.popitem(last=False)
        encoded = encode_event(kind, payload)
        size = len(encoded.encode('utf-8'))
        # Oversized events are recovered from SQLite, just like a slow reader's
        # gap. No per-subscriber queues or token logs can backpressure execution.
        if size <= MAX_EVENT_BYTES:
            self.events.append((self.sequence, encoded, size))
            self.event_bytes += size
        else:
            self.events.clear()
            self.event_bytes = 0
        while len(self.events) > MAX_EVENTS or self.event_bytes > MAX_EVENT_BYTES:
            self.event_bytes -= self.events.popleft()[2]
        self.changed.set()

    def set_phase(self, phase: str) -> None:
        if self.phase == phase or self.finished:
            return
        self.phase = phase
        self.emit('assistant_phase', {'phase': phase})

    def snapshot(self, persisted: AgentRun | None = None, process=None) -> dict[str, Any]:
        """Capture owner state on its event loop; never perform database I/O."""
        # Runtime may have committed success before desktop delivery/cleanup
        # fails. The completed owner's outcome governs this public task.
        status = self.run.status if self.finished or persisted is None else persisted.status
        return {'runId': self.run.runId, 'roleId': self.run.roleId,
            'sessionKey': self.run.sessionKey, 'sequence': self.sequence,
            # Runtime can finish before natural delivery and capability cleanup.
            'status': status if self.finished else ('queued' if self.phase == 'queued' else 'running'),
            'phase': self.phase, 'stopping': self.cancel_requested,
            **({'process': process} if process is not None else {}),
            'tools': [dict(call) for call in self.calls.values()],
            'error': self.terminal[1].get('error') if self.terminal else (
                OWNER_FAILURE if self.finished and self.run.error else None)}

    async def subscribe(self, sessions: SessionStore, after: int | None = None) -> AsyncIterator[str]:
        cursor = after
        while True:
            # Capture cursor and notification on the same event loop as emit.
            # Clearing this shared level-triggered event cannot lose a wakeup:
            # every reader checks the sequence before awaiting it.
            self.changed.clear()
            if cursor is None or cursor > self.sequence or (cursor < self.sequence and
                    (not self.events or cursor < self.events[0][0] - 1)):
                cursor = self.sequence
                yield encode_event('run_snapshot', self.snapshot())
            else:
                for sequence, encoded, _ in tuple(self.events):
                    if sequence > cursor:
                        cursor = sequence
                        yield encoded
            if self.finished and cursor >= self.sequence:
                return
            if cursor < self.sequence:
                continue
            try:
                await asyncio.wait_for(self.changed.wait(), 15)
            except TimeoutError:
                yield ': keepalive\n\n'


class ChatRunService:
    def __init__(self, sessions: SessionStore, runtime: RuntimeManager) -> None:
        self.sessions, self.runtime = sessions, runtime
        self.runs: OrderedDict[str, ChatRun] = OrderedDict()
        self.closing = False

    def active(self, role_id: str) -> ChatRun | None:
        return next((run for run in reversed(self.runs.values())
                     if run.run.roleId == role_id and not run.finished), None)

    def check_capacity(self) -> None:
        if self.closing or sum(not run.finished for run in self.runs.values()) >= MAX_ACTIVE_RUNS:
            raise RuntimeError('聊天服务正在收尾或等待队列已满，请稍后重试')

    def read_fence(self, role_id: str, run_id: str | None = None):
        """Validate a worker read without making producers wait for readers.

        The retained role identity detects a run that starts and ends during an
        idle read, without invalidation from unrelated roles while it is known.
        """
        active = self.active(role_id)
        exact = self.runs.get(run_id) if run_id is not None else None
        latest = next((turn.run.runId for turn in reversed(self.runs.values()) if turn.run.roleId == role_id), None)
        if latest is None:
            # Finished owners are bounded. With no retained role identity, keep
            # the original conservative fence so a newly finished/evicted run
            # cannot disappear between the two reads.
            latest = next(reversed(self.runs), None)
        return (latest, active, active.sequence if active else None,
            exact, exact.sequence if exact else None)

    def start(self, run: AgentRun, execute: Callable[[ChatRun], Awaitable[None]],
              finish: Callable[[ChatRun], Awaitable[None]]) -> ChatRun:
        turn = ChatRun(run)
        self.runs[run.runId] = turn

        async def runner() -> None:
            turn.started = True
            failed = False
            try:
                if not self.runtime.attach_current_task(run.runId) or turn.cancel_requested:
                    raise asyncio.CancelledError
                await execute(turn)
            except asyncio.CancelledError:
                self.runtime.cancel_run(run.runId, interrupt=False)
            except Exception as error:
                failed = True
                logger.error('Desktop execution failed run=%s type=%s', run.runId, type(error).__name__)
            finally:
                await complete_despite_cancellation(self._finish(turn, finish, failed=failed))

        turn.task = asyncio.create_task(runner(), name=f'desktop-chat:{run.runId}')
        return turn

    async def _finish(self, turn: ChatRun, finish, *, failed: bool) -> None:
        """The sole terminal publisher, including failed persistence or final reads."""
        try:
            await finish(turn)
        except BaseException as error:
            failed = True
            logger.error('Desktop cleanup failed run=%s type=%s', turn.run.runId, type(error).__name__)
        try:
            persisted = await asyncio.to_thread(self.sessions.get_run, turn.run.runId)
            if persisted is not None:
                turn.run = persisted.model_copy(update={'modelSnapshot': {}})
        except Exception as error:
            failed = True
            logger.error('Desktop terminal read failed run=%s type=%s', turn.run.runId, type(error).__name__)
        finally:
            turn.current_message = None
            if failed:
                turn.run = turn.run.model_copy(update={'status': 'failed', 'error': OWNER_FAILURE})
                try:
                    await asyncio.to_thread(self.sessions.record_run_error, turn.run.runId, OWNER_FAILURE)
                except Exception as error:
                    # A broken database cannot guarantee a new durable error.
                    # Keep the live failure explicit; no hidden retry or replay.
                    logger.error('Desktop failure could not be saved run=%s type=%s', turn.run.runId, type(error).__name__)
                if turn.terminal is not None:
                    turn.terminal = ('assistant_failed', {**turn.terminal[1], 'error': OWNER_FAILURE})
            turn.finished = True
            turn.phase = 'finished'
            if turn.terminal is not None:
                turn.emit(*turn.terminal)
                turn.terminal = (turn.terminal[0], {'error': turn.terminal[1].get('error')})
            else:
                turn.emit('run_snapshot', turn.snapshot())
            finished = [key for key, value in self.runs.items() if value.finished]
            for key in finished[:-MAX_FINISHED_RUNS]:
                self.runs.pop(key, None)

    async def stop(self, run_id: str) -> bool:
        turn = self.runs.get(run_id)
        if turn is None:
            return await self.runtime.stop_run(run_id)
        if not turn.finished and not turn.cancel_requested:
            turn.cancel_requested = True
            if turn.invalidate is not None:
                turn.invalidate()
            persisted = await asyncio.to_thread(self.sessions.get_run, run_id)
            if turn.started and persisted is not None and persisted.status in {'queued', 'created', 'running'}:
                self.runtime.cancel_run(run_id)
        if turn.task is not None:
            await asyncio.shield(asyncio.gather(turn.task, return_exceptions=True))
        return True

    async def close(self) -> None:
        self.closing = True
        await asyncio.gather(*(self.stop(key) for key, run in tuple(self.runs.items()) if not run.finished))

    def snapshot(self, run: AgentRun, process=None) -> dict[str, Any]:
        turn = self.runs.get(run.runId)
        if turn is not None:
            return turn.snapshot(run, process)
        # Restart recovers unfinished DB rows once; it never reconstructs tasks.
        return {'runId': run.runId, 'roleId': run.roleId, 'sessionKey': run.sessionKey,
            'sequence': 0, 'status': 'failed' if run.error and run.status in {
                'completed', 'cancelled', 'failed', 'max_turns'} else run.status,
            'phase': 'finished', 'stopping': False, 'tools': [],
            'error': OWNER_FAILURE if run.error == OWNER_FAILURE else (
                '回复未完成；已保存的历史仍然保留' if run.error else None)}
