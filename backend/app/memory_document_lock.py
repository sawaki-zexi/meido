"""Serialize short document mutations across background I/O threads.

No network call or coroutine suspension belongs inside this critical section.
"""
from pathlib import Path
from threading import Lock, RLock
import asyncio

_guard = Lock()
_locks: dict[str, RLock] = {}


def document_lock(memory_dir):
    key = str(Path(memory_dir).resolve()).casefold()
    with _guard:
        return _locks.setdefault(key, RLock())


class MarkdownCommitGuard:
    """Cancellation waits only for an already-started short file/DB commit."""
    def __init__(self):
        self.lock = RLock()
        self.cancelled = False
        self.requests = set()

    def cancel(self):
        with self.lock:
            self.cancelled = True
            requests = tuple(self.requests)
        for loop, task in requests:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The request's own loop already completed and closed.

    async def run(self, awaitable):
        """Bridge a host cancellation into the worker's actual async HTTP task."""
        task = asyncio.create_task(awaitable)
        entry = (asyncio.get_running_loop(), task)
        with self.lock:
            if self.cancelled:
                task.cancel()
            else:
                self.requests.add(entry)
        try:
            return await task
        finally:
            with self.lock:
                self.requests.discard(entry)
