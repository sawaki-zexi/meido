"""Authoritative keyed observation survives filtering and result retention."""
from dataclasses import replace

import pytest

from backend.app.task_runtime import TaskRuntime, TaskOrigin, TaskBudget
from test_task_runtime_handover import MarkerExecutor


pytestmark = pytest.mark.usefixtures("local_platform_only")


def test_lookup_preserves_original_key_after_result_prune_without_reexecuting(tmp_path):
    tasks = TaskRuntime(tmp_path / "tasks", retention_seconds=-1)
    origin = TaskOrigin("role", "autonomy:role", source_scope_id="one-tick")
    try:
        assert tasks.find_submission(origin, kind="autonomy_tick", submission_key="tick") is None
        identifier = tasks.submit(origin, MarkerExecutor(tmp_path), kind="autonomy_tick",
            budget=TaskBudget(), submission_key="tick", retain_until_ack=True)
        assert tasks.query(identifier, origin, wait_seconds=3).status == "succeeded"
        assert tasks.find_submission(origin, kind="autonomy_tick", submission_key="tick") == identifier
        assert tasks.find_submission(replace(origin, source_scope_id="another-tick"), kind="autonomy_tick", submission_key="tick") is None
        tasks.acknowledge_result(identifier, origin)
        assert tasks.list(origin, kind="autonomy_tick") == ()
        assert tasks.find_submission(origin, kind="autonomy_tick", submission_key="tick") == identifier
        assert (tmp_path / "starts").read_text() == "1"
    finally:
        tasks.close()


def test_temporarily_disabled_lookup_cannot_be_interpreted_as_absent(tmp_path):
    tasks = TaskRuntime(tmp_path / "tasks")
    origin = TaskOrigin("role", "autonomy:role", source_scope_id="one-tick")
    try:
        tasks.set_role_enabled("role", False)
        with pytest.raises(PermissionError):
            tasks.find_submission(origin, kind="autonomy_tick", submission_key="tick")
        tasks.set_role_enabled("role", True)
        assert tasks.find_submission(origin, kind="autonomy_tick", submission_key="tick") is None
    finally:
        tasks.close()
