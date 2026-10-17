"""Durable autonomous handovers survive real Runtime/SQLite boundary faults.

Fault injection wraps the public Runtime handover/ACK and blocks SQLite writes;
it never fabricates a completed task, business result or acknowledgement.
"""
import asyncio
import sqlite3
import threading

import pytest

from backend.app.task_runtime import TaskBudget, TaskOrigin, TaskOutcome
from backend.app.proactive_runtime import retention
from test_proactive_admission import enable
from test_proactive_configuration import system  # noqa: F401


pytestmark = pytest.mark.usefixtures("recovery_environment")


@pytest.fixture(autouse=True)
def recovery_environment(tmp_path, monkeypatch, local_platform_only):
    # This autouse module fixture precedes `system`, so its network
    # guard and roots remain installed through app/executor/Runtime teardown.
    monkeypatch.setenv("MEIDO_DATA_DIR", str(tmp_path / "isolated-data"))
    monkeypatch.setenv("MEIDO_ROLES_DIR", str(tmp_path / "isolated-roles"))
    monkeypatch.setenv("MEIDO_OWNER_KNOWLEDGE_DATA_DIR", str(tmp_path / "isolated-owner"))
    monkeypatch.delenv("MEIDO_PROJECT_ROOT", raising=False)
    yield


class ExecutionProbe:
    def __init__(self, executor, starts, started):
        self.executor, self.starts, self.started = executor, starts, started

    def run(self, execution):
        self.starts.append(execution)
        self.started.set()
        return self.executor.run(execution)

    def cancel(self):
        self.executor.cancel()


class HandoverProbe:
    def __init__(self, tasks):
        self.real_submit = tasks.submit
        self.calls, self.accepted, self.starts = [], [], []
        self.started = threading.Event()

    def submit(self, origin, executor, **kwargs):
        self.calls.append((origin, kwargs["kind"], kwargs["submission_key"]))
        identifier = self.real_submit(origin, ExecutionProbe(executor, self.starts, self.started), **kwargs)
        self.accepted.append(identifier)
        return identifier


async def recovered(app):
    async with asyncio.timeout(5):
        while True:
            await app.recover()
            runs = app.query("role")["runs"]
            if runs and all(run["runtimeAcknowledged"] for run in runs):
                return runs
            await asyncio.sleep(.01)


def storage_bytes(sessions):
    # Observe the retention module's public logical-allocation projection;
    # no business rows or successful outcomes are synthesized here.
    with sqlite3.connect(sessions.database_path) as db:
        return retention.usage(db, "role")["globalBytes"]


def test_accepted_submit_with_lost_return_recovers_the_original_execution_once(system, monkeypatch):
    app, _, tasks, now, _ = system
    enable(app)
    probe = HandoverProbe(tasks)

    def lose_return(origin, executor, **kwargs):
        probe.submit(origin, executor, **kwargs)
        raise RuntimeError("controlled accepted Runtime response loss")

    async def exercise():
        with monkeypatch.context() as patch:
            patch.setattr(tasks, "submit", lose_return)
            now[0] += 1800
            assert await app.on_clock() == 1
            runs = await recovered(app)
            for _ in range(3):
                await app.recover()
        assert len(runs) == 1
        assert runs[0]["status"] == "no_events"
        assert runs[0]["taskId"] == probe.accepted[0]
        assert len(probe.calls) == len(probe.starts) == 1
        origin, kind, key = probe.calls[0]
        assert key == runs[0]["id"] and kind == "autonomy_tick"
        assert tasks.find_submission(origin, kind=kind, submission_key=key) == probe.accepted[0]

    asyncio.run(exercise())


def test_task_id_write_failure_preserves_owned_handover_until_local_recovery(system, monkeypatch):
    app, sessions, tasks, now, _ = system
    enable(app)
    probe = HandoverProbe(tasks)

    async def exercise():
        with monkeypatch.context() as patch:
            patch.setattr(tasks, "submit", probe.submit)
            with sqlite3.connect(sessions.database_path) as db:
                db.execute("CREATE TRIGGER recovery_fail_task_id BEFORE UPDATE OF task_id ON autonomy_runs WHEN NEW.task_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'controlled handover write failure'); END")
            try:
                now[0] += 1800
                try:
                    await app.on_clock()
                except sqlite3.Error:
                    pass  # The public clock may report the deferred local write.
                pending = app.query("role")["runs"]
                assert len(pending) == len(probe.accepted) == 1
                assert pending[0]["taskId"] is None
                assert not pending[0]["runtimeAcknowledged"]
            finally:
                with sqlite3.connect(sessions.database_path) as db:
                    db.execute("DROP TRIGGER recovery_fail_task_id")
            runs = await recovered(app)
        assert runs[0]["status"] == "no_events"
        assert runs[0]["taskId"] == probe.accepted[0]
        assert len(probe.calls) == len(probe.starts) == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("prune_result", [False, True], ids=["retained", "pruned-tombstone"])
def test_successful_runtime_ack_with_failed_dirty_clear_reuses_durable_copy(system, monkeypatch, prune_result):
    app, sessions, tasks, now, _ = system
    enable(app)
    probe, acknowledgements = HandoverProbe(tasks), []
    real_ack = tasks.acknowledge_result

    def acknowledged(identifier, origin):
        real_ack(identifier, origin)
        acknowledgements.append((identifier, origin))

    def no_result_requery(*args, **kwargs):
        raise AssertionError("durably copied Runtime result must not be queried again")

    async def exercise():
        with monkeypatch.context() as patch:
            patch.setattr(tasks, "submit", probe.submit)
            patch.setattr(tasks, "acknowledge_result", acknowledged)
            now[0] += 1800
            assert await app.on_clock() == 1
            origin, kind, key = probe.calls[0]
            identifier = probe.accepted[0]
            terminal = await asyncio.to_thread(tasks.query, identifier, origin, wait_seconds=3)
            assert terminal.ended_at is not None
            with sqlite3.connect(sessions.database_path) as db:
                db.execute("CREATE TRIGGER recovery_fail_ack_clear BEFORE UPDATE OF result_ack_pending ON autonomy_runs WHEN OLD.result_ack_pending=1 AND NEW.result_ack_pending=0 BEGIN SELECT RAISE(ABORT,'controlled post-ACK dirty write failure'); END")
            try:
                await app.recover()
                assert acknowledgements == [(identifier, origin)]
                assert not app.query("role")["runs"][0]["runtimeAcknowledged"]
                if prune_result:
                    tasks.retention_seconds = -1
                    assert tasks.list(origin, kind=kind) == ()
                    assert tasks.find_submission(origin, kind=kind, submission_key=key) == identifier
            finally:
                with sqlite3.connect(sessions.database_path) as db:
                    db.execute("DROP TRIGGER recovery_fail_ack_clear")
            patch.setattr(tasks, "query", no_result_requery)
            runs = await recovered(app)
            await app.recover()
        assert runs[0]["status"] == "no_events"
        assert acknowledgements == [(identifier, origin), (identifier, origin)]
        assert len(probe.calls) == len(probe.starts) == 1

    asyncio.run(exercise())


def test_temporarily_disabled_runtime_lookup_keeps_original_cleanup_duty(system, monkeypatch):
    app, _, tasks, now, _ = system
    enable(app)
    probe = HandoverProbe(tasks)

    def accept_then_hide(origin, executor, **kwargs):
        probe.submit(origin, executor, **kwargs)
        assert probe.started.wait(3), "accepted local executor did not enter"
        tasks.set_role_enabled("role", False)
        raise RuntimeError("controlled response loss during temporary owner disable")

    async def exercise():
        with monkeypatch.context() as patch:
            patch.setattr(tasks, "submit", accept_then_hide)
            try:
                now[0] += 1800
                assert await app.on_clock() == 1
                origin, kind, key = probe.calls[0]
                with pytest.raises(PermissionError):
                    tasks.find_submission(origin, kind=kind, submission_key=key)
                for _ in range(3):
                    await app.recover()
                pending = app.query("role")["runs"]
                assert len(pending) == 1
                assert pending[0]["taskId"] is None
                assert pending[0]["status"] != "rejected"
                assert not pending[0]["runtimeAcknowledged"]
                assert len(probe.calls) == len(probe.accepted) == 1
            finally:
                tasks.set_role_enabled("role", True)
            runs = await recovered(app)
        assert runs[0]["taskId"] == probe.accepted[0]
        assert len(probe.calls) == len(probe.starts) == 1

    asyncio.run(exercise())


def test_human_seal_joins_slow_original_submit_before_next_admission(system, monkeypatch):
    app, sessions, tasks, now, _ = system
    enable(app)
    probe = HandoverProbe(tasks)
    entered, release = threading.Event(), threading.Event()
    tasks.max_retained_per_role = 1

    def slow_original(origin, executor, **kwargs):
        entered.set()
        assert release.wait(5), "controlled submit release timed out"
        return probe.submit(origin, executor, **kwargs)

    async def exercise():
        clock = None
        with monkeypatch.context() as patch:
            patch.setattr(tasks, "submit", slow_original)
            now[0] += 1800
            clock = asyncio.create_task(app.on_clock())
            finish_human = None
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                old = app.query("role")["runs"][0]
                finish_human = app.accept_human_run("role", "actual-owner-input")
                await app.recover()
                pending = app.query("role")
                assert pending["cleanupPending"] and pending["humanBusy"]
                assert len(pending["runs"]) == 1
                assert not pending["runs"][0]["runtimeAcknowledged"]
                assert pending["runs"][0]["status"] != "rejected"
                assert storage_bytes(sessions) >= 3 * 1024 * 1024
                release.set()
                assert await clock == 1
                runs = await recovered(app)
                assert runs[0]["status"] == "rejected"
                assert runs[0]["taskId"] is None
                assert len(probe.calls) == 1 and probe.accepted == probe.starts == []
                origin, kind, key = probe.calls[0]
                assert key == old["id"]
                assert tasks.find_submission(origin, kind=kind, submission_key=key) is None
                assert storage_bytes(sessions) < 128 * 1024
            finally:
                release.set()
                if clock is not None:
                    await asyncio.gather(clock, return_exceptions=True)
                if finish_human is not None:
                    finish_human()
        async with asyncio.timeout(5):
            while app.query("role")["cleanupPending"]:
                await app.recover()
                await asyncio.sleep(.01)
        # The original late submit consumed no Runtime retention slot. A new
        # genuine clock occurrence can use the sole slot after human cleanup.
        now[0] += 1800
        assert await app.on_clock() == 1
        runs = await recovered(app)
        assert len(runs) == 2
        assert sorted(run["status"] for run in runs) == ["no_events", "rejected"]

    asyncio.run(exercise())


@pytest.mark.parametrize("budget", ["concurrency", "retention"])
def test_authoritatively_rejected_runtime_budget_does_not_leave_permanent_pending(system, monkeypatch, budget):
    app, sessions, tasks, now, _ = system
    enable(app)
    probe = HandoverProbe(tasks)
    started, release = threading.Event(), threading.Event()
    held_origin = TaskOrigin("role", "fixture:role")

    class LocalWork:
        def run(self, execution):
            started.set()
            assert release.wait(5), "controlled Runtime budget release timed out"
            return TaskOutcome("succeeded")

        def cancel(self):
            release.set()

    if budget == "concurrency":
        tasks.max_concurrent = 1
    else:
        tasks.max_retained_per_role = 1
        release.set()
    held = tasks.submit(held_origin, LocalWork(), kind="fixture", budget=TaskBudget(timeout_seconds=10),
        submission_key="existing-owned-work", retain_until_ack=True)
    assert started.wait(3)
    if budget == "retention":
        assert tasks.query(held, held_origin, wait_seconds=3).status == "succeeded"

    async def exercise():
        try:
            with monkeypatch.context() as patch:
                patch.setattr(tasks, "submit", probe.submit)
                now[0] += 1800
                assert await app.on_clock() == 1
                assert len(probe.calls) == 1 and probe.accepted == probe.starts == []
                origin, kind, key = probe.calls[0]
                assert tasks.find_submission(origin, kind=kind, submission_key=key) is None
                runs = await recovered(app)
                assert runs[0]["status"] == "rejected"
                assert runs[0]["taskId"] is None
                assert runs[0]["runtimeAcknowledged"]
                assert storage_bytes(sessions) < 128 * 1024
            release.set()
            terminal = await asyncio.to_thread(tasks.query, held, held_origin, wait_seconds=3)
            assert terminal.status == "succeeded"
            tasks.acknowledge_result(held, held_origin)
            tasks.retention_seconds = -1
            assert tasks.list(held_origin, kind="fixture") == ()
            now[0] += 1800
            assert await app.on_clock() == 1
            runs = await recovered(app)
            assert len(runs) == 2
            assert sorted(run["status"] for run in runs) == ["no_events", "rejected"]
        finally:
            release.set()
            try:
                terminal = await asyncio.to_thread(tasks.query, held, held_origin, wait_seconds=3)
                if terminal.ended_at is not None:
                    tasks.acknowledge_result(held, held_origin)
            except KeyError:
                pass  # The deliberately ACKed result may already be pruned.

    asyncio.run(exercise())
