"""Bounded in-memory handover intake, following Shiori's pause/resume barrier."""
from __future__ import annotations

import asyncio
import contextvars
import logging
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)
Action = Callable[[], Awaitable[object]]


class ChannelIntake:
    def __init__(self, capacity: int = 64) -> None:
        if capacity < 1:
            raise ValueError("入站缓冲容量必须为正数")
        self.capacity = capacity
        self._paused: set[str] = set()
        self._closed: set[str] = set()
        self._buffers: dict[str, deque[tuple[Action, Action]]] = defaultdict(deque)
        self._tasks: dict[asyncio.Task, str] = {}

    def pause(self, account_id: str) -> None:
        self._paused.add(account_id)
        self._closed.discard(account_id)

    def defer(self, account_id: str, replay: Action, reject: Action) -> bool:
        if account_id not in self._paused or account_id in self._closed or len(self._buffers[account_id]) >= self.capacity:
            return False
        self._buffers[account_id].append((replay, reject))
        return True

    def resume(self, account_id: str) -> None:
        self._paused.discard(account_id)
        self._closed.discard(account_id)
        for replay, _reject in self._buffers.pop(account_id, ()):
            task = contextvars.Context().run(asyncio.create_task, replay())
            self._tasks[task] = account_id
            task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task) -> None:
        account_id = self._tasks.pop(task, "")
        if not task.cancelled() and task.exception():
            logger.warning("缓冲入站处理失败 account=%s error=%s", account_id, type(task.exception()).__name__)

    async def close(self, account_id: str) -> None:
        self._closed.add(account_id)
        self._paused.add(account_id)
        for _replay, reject in self._buffers.pop(account_id, ()):
            try:
                await reject()
            except Exception as error:
                logger.warning("缓冲入站拒收提示失败 account=%s error=%s", account_id, type(error).__name__)

    async def drain(self, account_id: str) -> None:
        tasks = [task for task, source in tuple(self._tasks.items()) if source == account_id]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
