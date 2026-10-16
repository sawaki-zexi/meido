"""A dropped connection remains owned until its actual cleanup completes."""
from __future__ import annotations

import asyncio
import importlib
import json
import threading
from types import SimpleNamespace

import pytest


async def wait_until(predicate, *, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


class DroppedConnection:
    """Explicit connection seam: transport is down, cleanup still owns work.

    ``closed`` records completion of resource cleanup, independently from
    ``is_connected``.  A dropped socket can still own SDK tasks/resources.
    """

    def __init__(self, *, first, failure):
        self.first, self.failure = first, failure
        self.thread = None
        self.connect_entered = threading.Event()
        self.cleanup_entered = threading.Event()
        self.cleanup_failed = threading.Event()
        self.cleanup_cancelled = threading.Event()
        self.cleanup_release = threading.Event()
        self.connect_release = threading.Event()
        self.closed = threading.Event()
        self.cleanup_calls = 0

    async def connect(self):
        self.thread = threading.current_thread()
        self.connect_entered.set()
        if not self.first:
            # A bad replacement cannot spin through unbounded retries while
            # the assertion is preparing its failure report.
            while not self.connect_release.is_set():
                await asyncio.sleep(0.005)

    def is_connected(self):
        return False

    async def keepalive(self):
        await asyncio.Future()

    async def disconnect(self):
        self.cleanup_calls += 1
        self.cleanup_entered.set()
        if self.first and self.failure in {"exception", "cancelled"} and self.cleanup_calls == 1:
            self.cleanup_failed.set()
            if self.failure == "cancelled":
                raise asyncio.CancelledError
            raise RuntimeError("controlled first disconnect failure")
        if not self.first:
            self.closed.set()
            return
        try:
            while not self.cleanup_release.is_set():
                await asyncio.sleep(0.005)
        except asyncio.CancelledError:
            # Deliberately model a cleanup that returned on cancellation
            # without releasing its resource.  The runner must retain it.
            self.cleanup_cancelled.set()
            self.cleanup_failed.set()
            raise
        self.closed.set()


@pytest.fixture
def cleanup_runner(tmp_path, monkeypatch, local_platform_only):
    for key, child in (
        ("MEIDO_DATA_DIR", "data"),
        ("MEIDO_ROLES_DIR", "roles"),
        ("MEIDO_OWNER_KNOWLEDGE_DATA_DIR", "owner-knowledge"),
    ):
        monkeypatch.setenv(key, str((tmp_path / child).resolve()))
    module = importlib.import_module("backend.app.channels.feishu_connection")
    runners, connections, overlaps = [], [], []

    def create(*, failure):
        def factory(_on_event):
            unresolved = [item for item in connections if item.connect_entered.is_set() and not item.closed.is_set()]
            if unresolved:
                overlaps.append(len(unresolved))
            connection = DroppedConnection(first=not connections, failure=failure)
            connections.append(connection)
            return connection

        runner = module.FeishuLongConnectionRunner(factory, lambda _payload: None,
                                                   reconnect_min=0.005, reconnect_max=0.005)
        runners.append(runner)
        return runner

    env = SimpleNamespace(module=module, create=create, runners=runners,
                          connections=connections, overlaps=overlaps)
    yield env

    async def cleanup():
        for connection in connections:
            connection.cleanup_release.set()
            connection.connect_release.set()
        # Never let a failed-version stop prevent joining the exact threads
        # observed by the connection seam while the guard is still installed.
        stop_results = await asyncio.gather(*(runner.stop() for runner in runners), return_exceptions=True)
        threads = {item.thread for item in connections if item.thread is not None}
        for thread in threads:
            await asyncio.to_thread(thread.join, 3)
            assert not thread.is_alive()
        # A RED can expose an already-abandoned fake resource.  Release it in
        # the fixture after the production assertions, never count that as a
        # successful production cleanup.
        for connection in connections:
            if not connection.closed.is_set():
                await connection.disconnect()
        assert all(item.closed.is_set() for item in connections)
        assert local_platform_only == []
        print(json.dumps({"fixtureConnections": len(connections), "ownedThreadsAlive": 0,
                          "nonLocalAttempts": len(local_platform_only),
                          "teardownStopErrors": [type(item).__name__ for item in stop_results
                                                 if isinstance(item, BaseException)]}))

    asyncio.run(cleanup())


@pytest.mark.parametrize("failure", ["exception", "cancelled", "timeout"])
def test_dropped_connection_cleanup_failure_does_not_create_replacement_before_explicit_stop(cleanup_runner, monkeypatch, failure):
    env = cleanup_runner
    if failure == "timeout":
        # Only this deliberately non-cooperative cleanup uses shorter budgets.
        monkeypatch.setattr(env.module, "DISCONNECT_TIMEOUT", 0.03)
        monkeypatch.setattr(env.module, "STOP_TIMEOUT", 0.1)

    async def scenario():
        runner = env.create(failure=failure)
        runner.start()
        try:
            await wait_until(lambda: bool(env.connections) and (
                env.connections[0].cleanup_failed.is_set() if failure == "exception"
                else env.connections[0].cleanup_entered.is_set()))
            first = env.connections[0]
            # Observe beyond the configured cleanup deadline without requiring
            # a particular internal task/cancellation implementation.
            await asyncio.sleep(0.08)
            assert not first.closed.is_set()
            assert env.overlaps == [], "a replacement was created while the dropped connection still owned resources"
            first.cleanup_release.set()
            await runner.stop()
            assert first.closed.is_set()
            assert first.cleanup_calls >= (2 if failure == "exception" else 1)
            assert not runner.alive
            assert runner.connection_snapshot()["reason"] == "stopped"
        finally:
            for connection in env.connections:
                connection.cleanup_release.set()
                connection.connect_release.set()
            await asyncio.gather(runner.stop(), return_exceptions=True)

    asyncio.run(scenario())


def test_stop_and_repeated_caller_cancellation_do_not_interrupt_disconnect_already_in_progress(cleanup_runner):
    env = cleanup_runner

    async def scenario():
        runner = env.create(failure="barrier")
        runner.start()
        stop = None
        try:
            await wait_until(lambda: bool(env.connections) and env.connections[0].cleanup_entered.is_set())
            first = env.connections[0]
            stop = asyncio.create_task(runner.stop())
            # The connection is already in its cleanup phase before the
            # first stop request; stop must join that cleanup, not cancel it.
            await asyncio.sleep(0.02)
            for _ in range(3):
                stop.cancel()
                await asyncio.sleep(0)
            assert not first.cleanup_cancelled.is_set(), "stop cancelled the disconnect it was supposed to own"
            assert not stop.done(), "stop finished before owned disconnect was released"
            assert runner.alive and not first.closed.is_set()
            first.cleanup_release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(stop, 3)
            assert first.closed.is_set()
            assert first.cleanup_calls == 1
            assert not runner.alive
            assert runner.connection_snapshot()["reason"] == "stopped"
            assert env.overlaps == []
        finally:
            for connection in env.connections:
                connection.cleanup_release.set()
                connection.connect_release.set()
            if stop is not None:
                await asyncio.gather(stop, return_exceptions=True)
            await asyncio.gather(runner.stop(), return_exceptions=True)

    asyncio.run(scenario())
