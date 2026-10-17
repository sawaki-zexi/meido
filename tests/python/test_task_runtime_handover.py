"""Durable schedule handover through TaskRuntime's public host boundary."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from backend.app.task_runtime.runtime import TaskBudget, TaskExecution, TaskOrigin, TaskOutcome, TaskRuntime


class MarkerExecutor:
    def __init__(self, directory: Path):
        self.directory = directory

    def run(self, execution: TaskExecution) -> TaskOutcome:
        with (self.directory / "starts").open("a") as starts:
            starts.write("1")
        execution.write("completed-result\n")
        return TaskOutcome("succeeded")

    def cancel(self) -> None:
        pass


def test_repeated_handover_after_restart_returns_the_same_execution(tmp_path):
    origin = TaskOrigin("role", "role:role")
    root = tmp_path / "tasks"
    tasks = TaskRuntime(root, max_retained_per_role=1)
    try:
        task_id = tasks.submit(origin, MarkerExecutor(tmp_path), budget=TaskBudget(), kind="schedule",
                               submission_key="occurrence-1", retain_until_ack=True)
        assert tasks.query(task_id, origin, wait_seconds=3).status == "succeeded"
    finally:
        tasks.close()
    restarted = TaskRuntime(root, max_retained_per_role=1)
    try:
        assert restarted.submit(origin, MarkerExecutor(tmp_path), budget=TaskBudget(), kind="schedule",
                                submission_key="occurrence-1", retain_until_ack=True) == task_id
        assert restarted.query(task_id, origin).output == "completed-result\n"
        assert [item.task_id for item in restarted.list(origin, kind="schedule")] == [task_id]
        assert (tmp_path / "starts").read_text() == "1"
    finally:
        restarted.close()


def test_unacknowledged_result_survives_expiry_and_occupies_existing_quota(tmp_path):
    origin = TaskOrigin("role", "role:role")
    tasks = TaskRuntime(tmp_path / "tasks", retention_seconds=-1, max_retained_per_role=1)
    try:
        task_id = tasks.submit(origin, MarkerExecutor(tmp_path), budget=TaskBudget(), kind="schedule",
                               submission_key="occurrence-1", retain_until_ack=True)
        result = tasks.query(task_id, origin, wait_seconds=3)
        assert result.output == "completed-result\n"
        tasks.close()
        tasks = TaskRuntime(tmp_path / "tasks", retention_seconds=-1, max_retained_per_role=1)
        assert tasks.query(task_id, origin).output == "completed-result\n"
        with pytest.raises(ValueError, match="retention budget"):
            tasks.submit(origin, MarkerExecutor(tmp_path), budget=TaskBudget(), kind="schedule",
                         submission_key="occurrence-2", retain_until_ack=True)
        with pytest.raises(KeyError):
            tasks.acknowledge_result(task_id, TaskOrigin("other", "role:other"))
        assert tasks.query(task_id, origin).output == "completed-result\n"
        tasks.acknowledge_result(task_id, origin)
        tasks.acknowledge_result(task_id, origin)
        assert tasks.list(origin, kind="schedule") == ()
        assert not Path(result.log_path).exists()
        tasks.close()
        tasks = TaskRuntime(tmp_path / "tasks", retention_seconds=-1, max_retained_per_role=1)
        with pytest.raises(KeyError, match="no longer retained"):
            tasks.submit(origin, MarkerExecutor(tmp_path), budget=TaskBudget(), kind="schedule",
                         submission_key="occurrence-1", retain_until_ack=True)
        assert (tmp_path / "starts").read_text() == "1"
        replacement = tasks.submit(origin, MarkerExecutor(tmp_path), budget=TaskBudget(), kind="schedule",
                                   submission_key="occurrence-2", retain_until_ack=True)
        assert replacement != task_id
        assert tasks.query(replacement, origin, wait_seconds=3).status == "succeeded"
    finally:
        tasks.close()


def test_concurrent_handover_reuses_running_task_before_concurrency_checks(tmp_path):
    started, release = threading.Event(), threading.Event()

    class WaitingExecutor(MarkerExecutor):
        def run(self, execution: TaskExecution) -> TaskOutcome:
            started.set()
            assert release.wait(3)
            return super().run(execution)

        def cancel(self) -> None:
            release.set()

    origin = TaskOrigin("role", "role:role")
    tasks = TaskRuntime(tmp_path / "tasks", max_concurrent=1, max_per_role=1)
    try:
        def handover() -> str:
            return tasks.submit(origin, WaitingExecutor(tmp_path), budget=TaskBudget(), kind="schedule",
                                submission_key="same-slot", retain_until_ack=True)

        with ThreadPoolExecutor(max_workers=2) as callers:
            attempts = [callers.submit(handover) for _ in range(2)]
            ids = [attempt.result(timeout=3) for attempt in attempts]
        assert ids[0] == ids[1]
        assert started.wait(3)
        with pytest.raises(ValueError, match="not terminal"):
            tasks.acknowledge_result(ids[0], origin)
        with pytest.raises(PermissionError):
            tasks.submit(origin, MarkerExecutor(tmp_path), budget=TaskBudget(), kind="schedule",
                         submission_key="same-slot", authorized=lambda: False)
        release.set()
        assert tasks.query(ids[0], origin, wait_seconds=3).status == "succeeded"
        assert (tmp_path / "starts").read_text() == "1"
    finally:
        release.set()
        tasks.close()


@pytest.mark.parametrize("changes,kind", [
    ({"role_id": "other"}, "schedule"),
    ({"session_key": "another-session"}, "schedule"),
    ({"source_thread_id": "other-thread"}, "schedule"),
    ({"source_account_id": "other-account"}, "schedule"),
    ({"source_scope_id": "other-binding"}, "schedule"),
    ({}, "spawn"),
])
def test_handover_keys_do_not_cross_role_origin_or_kind(tmp_path, changes, kind):
    origin = TaskOrigin("role", "role:role", source_thread_id="thread", source_account_id="account", source_scope_id="binding")
    tasks = TaskRuntime(tmp_path / "tasks")
    try:
        first = tasks.submit(origin, MarkerExecutor(tmp_path), budget=TaskBudget(), kind="schedule", submission_key="slot")
        assert tasks.query(first, origin, wait_seconds=3).status == "succeeded"
        later_origin = replace(origin, parent_run_id="later-run", tool_call_id="later-call")
        assert tasks.submit(later_origin, MarkerExecutor(tmp_path), budget=TaskBudget(), kind="schedule", submission_key="slot") == first
        different = replace(origin, **changes)
        second = tasks.submit(different, MarkerExecutor(tmp_path), budget=TaskBudget(), kind=kind, submission_key="slot")
        assert first != second
        assert tasks.query(second, different, wait_seconds=3).status == "succeeded"
    finally:
        tasks.close()


def test_crash_after_submission_does_not_restart_the_same_handover(tmp_path):
    root = tmp_path / "tasks"
    crash_host = """
import os, sys, threading
from pathlib import Path
from backend.app.task_runtime.runtime import TaskBudget, TaskOrigin, TaskRuntime
started = threading.Event()
class Executor:
    def run(self, execution):
        with (Path(sys.argv[2]) / 'starts').open('a') as marker:
            marker.write('1')
        started.set()
        threading.Event().wait(30)
    def cancel(self):
        pass
tasks = TaskRuntime(Path(sys.argv[1]))
task_id = tasks.submit(TaskOrigin('role', 'role:role'), Executor(), budget=TaskBudget(), kind='schedule',
                       submission_key='crashed-slot', retain_until_ack=True)
assert started.wait(3)
print(task_id, flush=True)
os._exit(0)
"""
    crashed = subprocess.run([sys.executable, "-c", crash_host, str(root), str(tmp_path)],
                             check=True, capture_output=True, text=True, timeout=10)
    task_id = crashed.stdout.strip()
    origin = TaskOrigin("role", "role:role")
    tasks = TaskRuntime(root, retention_seconds=-1)
    try:
        assert tasks.submit(origin, MarkerExecutor(tmp_path), budget=TaskBudget(), kind="schedule",
                            submission_key="crashed-slot", retain_until_ack=True) == task_id
        assert tasks.query(task_id, origin).status == "interrupted"
        assert (tmp_path / "starts").read_text() == "1"
    finally:
        tasks.close()


def test_retained_result_still_consumes_the_role_log_quota(tmp_path):
    origin = TaskOrigin("role", "role:role")
    tasks = TaskRuntime(tmp_path / "tasks", max_role_log_bytes=32)
    try:
        task_id = tasks.submit(origin, MarkerExecutor(tmp_path), budget=TaskBudget(log_bytes=32), kind="schedule",
                               submission_key="first", retain_until_ack=True)
        assert tasks.query(task_id, origin, wait_seconds=3).status == "succeeded"
        with pytest.raises(ValueError, match="retention budget"):
            tasks.submit(origin, MarkerExecutor(tmp_path), budget=TaskBudget(log_bytes=32), kind="schedule",
                         submission_key="second", retain_until_ack=True)
        assert tasks.query(task_id, origin).output == "completed-result\n"
        assert (tmp_path / "starts").read_text() == "1"
    finally:
        tasks.close()


def test_role_deletion_removes_retained_results_and_handover_identity(tmp_path):
    origin = TaskOrigin("role", "role:role")
    directories = []

    class WorkspaceExecutor(MarkerExecutor):
        def run(self, execution: TaskExecution) -> TaskOutcome:
            workspace = execution.directory()
            (workspace / "private").write_text("schedule work")
            directories.append(workspace)
            return super().run(execution)

    tasks = TaskRuntime(tmp_path / "tasks", retention_seconds=-1)
    try:
        task_id = tasks.submit(origin, WorkspaceExecutor(tmp_path), budget=TaskBudget(), kind="schedule",
                               submission_key="occurrence", retain_until_ack=True)
        result = tasks.query(task_id, origin, wait_seconds=3)
        assert result.status == "succeeded"
        tasks.delete_role(origin.role_id)
        assert not Path(result.log_path).exists()
        assert len(directories) == 1 and not directories[0].exists()
        with pytest.raises(KeyError):
            tasks.query(task_id, origin)
        tasks.set_role_enabled(origin.role_id, True)
        replacement = tasks.submit(origin, MarkerExecutor(tmp_path), budget=TaskBudget(), kind="schedule",
                                   submission_key="occurrence", retain_until_ack=True)
        assert replacement != task_id
        assert tasks.query(replacement, origin, wait_seconds=3).status == "succeeded"
    finally:
        tasks.close()


def test_role_reconciliation_cleans_handover_identity_after_result_expiry(tmp_path):
    origin = TaskOrigin("deleted-role", "role:deleted-role")
    root = tmp_path / "tasks"
    tasks = TaskRuntime(root, retention_seconds=-1)
    try:
        task_id = tasks.submit(origin, MarkerExecutor(tmp_path), budget=TaskBudget(), kind="schedule",
                               submission_key="occurrence", retain_until_ack=True)
        assert tasks.query(task_id, origin, wait_seconds=3).status == "succeeded"
        tasks.acknowledge_result(task_id, origin)
        assert tasks.list(origin, kind="schedule") == ()
    finally:
        tasks.close()
    restarted = TaskRuntime(root, retention_seconds=-1)
    try:
        restarted.reconcile_roles(set())
        restarted.set_role_enabled(origin.role_id, True)
        replacement = restarted.submit(origin, MarkerExecutor(tmp_path), budget=TaskBudget(), kind="schedule",
                                       submission_key="occurrence", retain_until_ack=True)
        assert replacement != task_id
        assert restarted.query(replacement, origin, wait_seconds=3).status == "succeeded"
    finally:
        restarted.close()


def test_pruned_keyed_result_ack_is_idempotent_only_for_the_same_owned_submission(tmp_path):
    origin = TaskOrigin('role', 'role:role', source_thread_id='source', source_scope_id='binding')
    tasks = TaskRuntime(tmp_path / 'tasks', retention_seconds=-1)
    try:
        task_id = tasks.submit(origin, MarkerExecutor(tmp_path), budget=TaskBudget(), kind='schedule',
            submission_key='owned-occurrence', retain_until_ack=True)
        assert tasks.query(task_id, origin, wait_seconds=3).status == 'succeeded'
        tasks.acknowledge_result(task_id, origin)
        assert tasks.list(origin, kind='schedule') == ()
        tasks.acknowledge_result(task_id, origin)
        for foreign in [replace(origin, role_id='other'), replace(origin, session_key='other'),
                        replace(origin, source_thread_id='other'), replace(origin, source_account_id='other'),
                        replace(origin, source_scope_id='other')]:
            with pytest.raises(KeyError):
                tasks.acknowledge_result(task_id, foreign)
        with pytest.raises(KeyError):
            tasks.acknowledge_result('unknown-task', origin)
        tasks.set_role_enabled(origin.role_id, False)
        with pytest.raises(KeyError):
            tasks.acknowledge_result(task_id, origin)
        tasks.set_role_enabled(origin.role_id, True)
        tasks.delete_role(origin.role_id)
        tasks.set_role_enabled(origin.role_id, True)
        with pytest.raises(KeyError):
            tasks.acknowledge_result(task_id, origin)
    finally:
        tasks.close()
