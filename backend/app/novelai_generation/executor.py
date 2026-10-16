"""One TaskRuntime-owned async HTTP execution, including its cancellation join."""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable

from ..task_runtime.runtime import TaskExecution, TaskOutcome


class GenerationExecutor:
    def __init__(self, execute: Callable[[TaskExecution], Awaitable[TaskOutcome]]):
        self._execute = execute
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None

    def run(self, execution: TaskExecution) -> TaskOutcome:
        async def owned():
            with self._lock:
                self._loop, self._task = asyncio.get_running_loop(), asyncio.current_task()
                if self._cancelled.is_set() and self._task is not None:
                    self._task.cancel()
            return await self._execute(execution)
        try:
            return asyncio.run(owned())
        except asyncio.CancelledError:
            return TaskOutcome('cancelled')
        finally:
            with self._lock:
                self._loop = self._task = None

    def cancel(self):
        with self._lock:
            if self._cancelled.is_set():
                return
            self._cancelled.set()
            loop, task = self._loop, self._task
            if loop is not None and task is not None and not loop.is_closed():
                loop.call_soon_threadsafe(task.cancel)
