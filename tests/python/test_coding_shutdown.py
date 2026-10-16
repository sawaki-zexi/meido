"""An unconfirmed Coding resource must not skip other application owners."""
import asyncio
import threading

import pytest

from backend.app import main
from backend.app.coding_tasks.application import CodingApplication
from backend.app.coding_tasks.config import CodingRoleConfig
from backend.app.coding_tasks import owned_process
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskBudget, TaskOrigin, TaskOutcome, TaskRuntime
from test_coding_application import GrantedConfiguration


pytestmark = pytest.mark.usefixtures("local_platform_only")


@pytest.mark.parametrize("secondary_failure", [False, True], ids=["coding-only", "two-independent-failures"])
def test_unconfirmed_coding_cleanup_still_drains_other_owners_and_can_retry(tmp_path, monkeypatch, secondary_failure):
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data/sessions.db")
    sessions.open_role_session("coding-role")
    runtime = TaskRuntime(tmp_path / "runtime")
    coding = CodingApplication(tmp_path / "coding", runtime, sessions,
        config=GrantedConfiguration(), role_config=lambda _: CodingRoleConfig(enabled=True),
        authorized=lambda _: True)
    origin = TaskOrigin("coding-role", "role:coding-role", "parent", "shutdown")
    record, _ = coding.ledger.admit(origin, requirement="owned cleanup obligation",
        repository={"id": "repo-a", "revision": "r"},
        profile={"id": "profile-a", "revision": "p", "executor": "herdr"}, max_daily=5)
    coding.ledger.update_attempt(record["attemptId"], status="unknown", cleanupState="running",
        resourceJobs=["Local\\Meido.Coding.integration-shutdown-query"])
    query_released = threading.Event()

    def observe(_):
        if not query_released.is_set():
            raise owned_process.CleanupFailed("controlled unconfirmed Coding Job query")
        return False

    monkeypatch.setattr(owned_process, "named_job_active", observe)
    cancelled, running = threading.Event(), threading.Event()

    class IndependentWork:
        def run(self, execution):
            running.set()
            assert cancelled.wait(15), "independent owned executor was not stopped"
            return TaskOutcome("cancelled")

        def cancel(self):
            cancelled.set()

    independent_origin = TaskOrigin("independent-role", "role:independent-role", "human", "other")
    runtime.submit(independent_origin, IndependentWork(), kind="fixture_independent_owner",
        budget=TaskBudget(15, 100, 1024))
    assert running.wait(2)
    closed = []
    secondary_pending = [secondary_failure]
    secondary_error = OSError("controlled independent screen cleanup failure")

    class IndependentOwner:
        def __init__(self, name):
            self.name = name

        async def close(self):
            closed.append(self.name)
            if self.name == "screen_perception" and secondary_pending[0]:
                raise secondary_error

        async def drain(self):
            closed.append(self.name)

    names = ("_schedules", "_spawn_completion_worker", "screen_perception", "computer_use",
        "_novelai", "mail_service", "owner_knowledge_scheduler", "owner_knowledge_plugin",
        "todo_plugin", "memory_optimizer_loop", "memory_worker", "memory_optimizer_worker",
        "browser_use_sessions", "mcp_host")
    monkeypatch.setattr(main, "_coding", coding)
    monkeypatch.setattr(main, "task_runtime", runtime)
    for name in names:
        monkeypatch.setattr(main, name, IndependentOwner(name))
    try:
        expected = ExceptionGroup if secondary_failure else owned_process.CleanupFailed
        with pytest.raises(expected) as raised:
            asyncio.run(main._close_memory_workers())
        if secondary_failure:
            errors = raised.value.exceptions
            assert len(errors) == 2
            assert isinstance(errors[0], owned_process.CleanupFailed)
            assert errors[1] is secondary_error
        assert cancelled.is_set()
        assert closed == list(names)
        assert coding.ledger.attempt(record["attemptId"])["cleanupState"] == "running"
        assert coding._resources_closed is False
        # A real later successful resource observation allows retry; the first
        # shutdown did not erase Coding's ledger or pretend cleanup completed.
        query_released.set()
        secondary_pending[0] = False
        asyncio.run(main._close_memory_workers())
        assert coding._resources_closed is True
        assert closed == [*names, *names]
    finally:
        query_released.set()
        asyncio.run(coding.close())
        runtime.close()


def test_previously_cancelled_owned_worker_still_allows_resource_close(tmp_path):
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data/sessions.db")
    runtime = TaskRuntime(tmp_path / "runtime")
    coding = CodingApplication(tmp_path / "coding", runtime, sessions,
        config=GrantedConfiguration(), role_config=lambda _: CodingRoleConfig(), authorized=lambda _: False)

    async def exercise():
        coding.start()
        await asyncio.sleep(.01)
        worker = coding._worker
        assert worker is not None
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        assert worker.cancelled()
        try:
            await coding.close()
            assert coding._resources_closed is True
        finally:
            # The failed implementation has already joined this terminal
            # worker; retain real resource cleanup even for the RED run.
            coding._worker = None
            await coding.close()

    try:
        asyncio.run(exercise())
    finally:
        runtime.close()


@pytest.mark.parametrize("cancel_worker", [False, True], ids=["caller-only", "caller-and-worker"])
def test_cancelling_close_preserves_the_request_and_owned_cleanup_retry(tmp_path, monkeypatch, cancel_worker):
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data/sessions.db")
    runtime = TaskRuntime(tmp_path / "runtime")
    coding = CodingApplication(tmp_path / "coding", runtime, sessions,
        config=GrantedConfiguration(), role_config=lambda _: CodingRoleConfig(), authorized=lambda _: False)

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def held_tick():
            entered.set()
            await release.wait()

        monkeypatch.setattr(coding, "tick", held_tick)
        coding.start()
        await entered.wait()
        worker = coding._worker
        assert worker is not None
        closing = asyncio.create_task(coding.close())
        try:
            await asyncio.sleep(.01)
            if cancel_worker:
                worker.cancel()
            closing.cancel()
            if cancel_worker:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await closing
            if not cancel_worker:
                assert not worker.done()
            assert coding._resources_closed is False
            release.set()
            await asyncio.gather(worker, return_exceptions=True)
            await coding.close()
            assert coding._resources_closed is True
        finally:
            release.set()
            await asyncio.gather(closing, worker, return_exceptions=True)
            coding._worker = None
            await coding.close()

    try:
        asyncio.run(exercise())
    finally:
        runtime.close()
