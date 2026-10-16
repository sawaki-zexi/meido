"""A synchronous Coding boundary owning a cancellable async HTTP request."""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid

import httpx
from anyio.lowlevel import get_async_backend

from .owned_process import CleanupFailed


class _Request:
    def __init__(self):
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task | None = None
        self.thread: threading.Thread | None = None
        self.value: object = None
        self.error: BaseException | None = None
        self.cancel_sent = False

    def abort(self):
        self.cancelled.set()
        with self.lock:
            if self.loop is not None and self.task is not None and not self.cancel_sent:
                self.cancel_sent = True
                try:
                    self.loop.call_soon_threadsafe(self.task.cancel)
                except RuntimeError:
                    # A just-closed event loop is confirmed by the join below.
                    pass

    def join(self):
        if self.thread is not None:
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                raise CleanupFailed('owned Coding HTTP execution has not exited')


class OwnedHTTP:
    """Each request has one non-daemon owner, absolute budget and closed client."""
    def __init__(self, base_url, headers):
        self.base_url, self.headers = base_url, headers
        # Match the old synchronous Client's construction boundary. Loading
        # TLS roots/backend code is pure preparation, not a pending connection.
        self._tls = httpx.create_ssl_context(trust_env=False)
        get_async_backend('asyncio')
        self._lock = threading.RLock()
        self._active: _Request | None = None
        self._closed = False

    @property
    def is_closed(self):
        with self._lock:
            return self._closed and self._active is None

    def request(self, method, path, *, data, absent, check):
        # Freeze this request's limit once. Calling check never renews either
        # the owner's original deadline or the existing 20-second HTTP cap.
        deadline = time.monotonic() + min(20, check())
        request = _Request()
        def guard():
            if request.cancelled.is_set():
                raise PermissionError('owned Coding HTTP request was closed')
            remaining = min(check(), deadline - time.monotonic())
            if remaining <= 0:
                raise TimeoutError('owned Coding HTTP absolute deadline ended')
            return remaining

        async def stream():
            remaining = guard()
            async with httpx.AsyncClient(base_url=self.base_url, trust_env=False, follow_redirects=False,
                    verify=self._tls, timeout=httpx.Timeout(remaining, connect=min(5, remaining)), headers=self.headers) as client:
                guard()
                async with client.stream(method, path, json=data) as response:
                    guard()
                    if absent and response.status_code == 404:
                        return None
                    if response.status_code != (201 if method == 'POST' else 200):
                        raise ValueError('GitHub operation was not confirmed')
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        guard()
                        body.extend(chunk)
                        if len(body) > 256 * 1024:
                            raise ValueError('GitHub response exceeds the operation budget')
                    guard()
                    value = json.loads(body)
                    guard()
                    return value

        async def run():
            with request.lock:
                request.loop = asyncio.get_running_loop()
                request.task = asyncio.current_task()
            if request.cancelled.is_set():
                raise PermissionError('owned Coding HTTP request was closed')
            return await stream()

        def execute():
            try:
                # asyncio cancellation interrupts stalled connect/read awaits;
                # context managers close the transport before this thread ends.
                # asyncio.run also joins any DNS/default-executor work. If that
                # cannot finish, join raises and ownership stays retained.
                request.value = asyncio.run(run())
            except BaseException as error:
                request.error = error
            finally:
                request.done.set()

        with self._lock:
            if self._closed:
                raise PermissionError('owned Coding HTTP admission closed')
            if self._active is not None:
                raise CleanupFailed('previous Coding HTTP request is still owned')
            self._active = request
            request.thread = threading.Thread(target=execute, name='Meido.Coding.HTTP.' + uuid.uuid4().hex)
            request.thread.start()
        try:
            while not request.done.wait(.01):
                guard()
            request.join()
            guard()
            if request.error is not None:
                raise request.error
            return request.value
        except BaseException:
            request.abort()
            request.join()
            raise
        finally:
            with self._lock:
                if request.thread is not None and not request.thread.is_alive() and self._active is request:
                    self._active = None

    def close(self):
        with self._lock:
            self._closed = True
            request = self._active
        if request is not None:
            request.abort()
            request.join()
            with self._lock:
                if self._active is request:
                    self._active = None
