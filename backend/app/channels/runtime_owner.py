"""Keep channel coroutine resources on their original service loop."""
import asyncio
from concurrent.futures import Future
import threading

from ..mcp_stdio import finish_cleanup


class ChannelRuntimeOwner:
    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._gate = threading.RLock()
        self._pending: set[Future] = set()

    @property
    def loop(self):
        with self._gate:
            return self._loop

    def bind(self):
        loop = asyncio.get_running_loop()
        with self._gate:
            previous = self._loop
            if previous is loop:
                return False
            if previous is not None and (previous.is_running() or self._pending):
                raise RuntimeError('channel work still belongs to its original runtime loop')
            self._loop = loop
            return True

    async def run(self, operation):
        caller = asyncio.get_running_loop()
        with self._gate:
            loop = self._loop
            if loop is not None and (loop.is_closed() or not loop.is_running()):
                raise RuntimeError('the original channel runtime loop is unavailable')
            if loop is not None and loop is not caller:
                completion: Future = Future()
                self._pending.add(completion)
        # Pure local fixtures have never bound a service loop. Ordinary channel
        # turns keep their original Task, contextvars and streaming queues.
        if loop is None or loop is caller:
            return await operation()

        cancelled = threading.Event()
        original: list[asyncio.Task] = []
        cancel_sent = False

        def complete(value=None, error=None):
            with self._gate:
                self._pending.discard(completion)
            if error is not None:
                completion.set_exception(error)
            else:
                completion.set_result(value)

        def finished(task):
            # Only the actual asyncio Task's terminal callback completes this
            # envelope. It is never cancelled as a substitute for joining.
            try:
                value = task.result()
            except BaseException as error:
                complete(error=error)
            else:
                complete(value=value)

        def start():
            if cancelled.is_set():
                complete(error=asyncio.CancelledError())
                return
            try:
                task = loop.create_task(operation())
            except BaseException as error:
                complete(error=error)
                return
            original.append(task)
            task.add_done_callback(finished)
            if cancelled.is_set():
                cancel()

        def cancel():
            nonlocal cancel_sent
            if original and not original[0].done() and not cancel_sent:
                cancel_sent = True
                original[0].cancel()

        try:
            # call_soon_threadsafe carries the caller's contextvars; creating
            # the original Task here preserves its admitted event lease.
            loop.call_soon_threadsafe(start)
        except BaseException:
            with self._gate:
                self._pending.discard(completion)
            raise

        result = asyncio.wrap_future(completion)

        async def receive():
            return await asyncio.shield(result)

        joined = asyncio.create_task(receive())
        try:
            return await asyncio.shield(joined)
        except BaseException:
            cancelled.set()
            try:
                loop.call_soon_threadsafe(cancel)
            except RuntimeError:
                # Losing a loop is not proof its original request has exited.
                # The real completion envelope retains that responsibility.
                pass
            try:
                await finish_cleanup(joined)
            except BaseException:
                pass
            raise
