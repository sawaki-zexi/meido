"""Ordinary memory IO with cancellation-safe worker retirement.

Cancelling an executor Future does not stop its thread. Always retain and
join the real worker before returning to the transaction's rollback/lock.
"""
from __future__ import annotations

import asyncio
import threading
import inspect
from contextlib import suppress


async def run_memory_io(operation, *, signal=None, timeout=None, on_interrupt=None):
    worker = asyncio.create_task(asyncio.to_thread(operation))
    deadline = asyncio.get_running_loop().time() + timeout if timeout is not None else None
    try:
        while True:
            if signal is not None:
                signal.raise_if_cancelled()
            remaining = deadline - asyncio.get_running_loop().time() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                raise asyncio.TimeoutError("memory IO deadline exceeded")
            done, _ = await asyncio.wait({worker}, timeout=min(.01, remaining) if remaining is not None else .01)
            if done:
                if signal is not None:
                    signal.raise_if_cancelled()
                if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                    raise asyncio.TimeoutError("memory IO deadline exceeded")
                return worker.result()
    except BaseException:
        if on_interrupt is not None:
            on_interrupt()
        # shield alone would let a second cancellation release the lock. Keep
        # waiting until the underlying operation is terminal, even then.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        with suppress(BaseException):
            worker.result()
        raise


async def run_async_memory_io(factory, *, signal=None, timeout=None):
    interrupted = threading.Event()

    def invoke():
        async def operation():
            result = factory()
            return await result if inspect.isawaitable(result) else result

        async def run():
            pending = asyncio.create_task(operation())
            try:
                while not pending.done():
                    if interrupted.is_set():
                        pending.cancel()
                    await asyncio.wait({pending}, timeout=.01)
                return await pending
            finally:
                if not pending.done():
                    pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending

        # asyncio.run also retires threads spawned by query's async embedding
        # and backfill code. No executor work survives this worker's terminal.
        return asyncio.run(run())

    return await run_memory_io(invoke, signal=signal, timeout=timeout, on_interrupt=interrupted.set)
