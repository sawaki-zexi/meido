import asyncio
import ctypes
import os
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.app.agent_runtime import CancellationToken, ToolContext
from backend.app.agent_runtime.shell_tool import ShellTool


def command(code):
    return shlex.join([str(Path(sys.executable)).replace("\\", "/"), "-u", "-c", code])


def context(role="role-a", source="role:role-a", run="run-1", token=None):
    return ToolContext(role, source, run, token or CancellationToken(), "call-1")


def test_background_command_survives_parent_and_returns_complete_result(tmp_path):
    from backend.app.task_runtime import TaskRuntime
    from backend.app.agent_runtime.shell_tool import TaskOutputTool

    tasks = TaskRuntime(tmp_path / "tasks")
    shell = ShellTool(tmp_path, allowed_commands=[sys.executable], tasks=tasks)
    output = TaskOutputTool(tasks)

    async def run():
        parent = context()
        started = time.monotonic()
        submitted = await shell.execute({
            "command": command("import time; print('started'); time.sleep(.3); print('finished')"),
            "run_in_background": True,
            "description": "受控后台命令",
        }, parent)
        assert submitted.is_error is False
        assert time.monotonic() - started < .25
        task_id = submitted.details["taskId"]
        parent.signal.cancel()
        result = await output.execute({"task_id": task_id, "block": True, "timeout_ms": 2000}, context(run="run-2"))
        assert result.is_error is False
        assert result.details["status"] == "succeeded"
        assert "started" in result.content and "finished" in result.content
        assert Path(result.details["logPath"]).read_text(encoding="utf-8") == "started\nfinished\n"
        assert result.details["origin"]["parentRunId"] == "run-1"
        assert result.details["origin"]["toolCallId"] == "call-1"

    try:
        asyncio.run(run())
    finally:
        tasks.close()


def test_auto_promotion_retains_hard_deadline_and_polling_never_restarts(tmp_path):
    from backend.app.task_runtime import TaskRuntime
    from backend.app.agent_runtime.shell_tool import TaskOutputTool

    tasks = TaskRuntime(tmp_path / "tasks")
    shell = ShellTool(tmp_path, allowed_commands=[sys.executable], tasks=tasks,
                      timeout_seconds=.4, promote_after_seconds=.03)
    output = TaskOutputTool(tasks)

    async def run():
        result = await shell.execute({"command": command(
            "from pathlib import Path; import time; Path('starts').open('a').write('1'); print('ready'); time.sleep(2)"
        )}, context())
        assert result.details["background"] is True
        task_id = result.details["taskId"]
        observed = await output.execute({"task_id": task_id, "block": True, "timeout_ms": 10}, context())
        assert observed.details["status"] in {"queued", "running"}
        final = await output.execute({"task_id": task_id, "block": True, "timeout_ms": 2000}, context())
        assert final.details["status"] == "timed_out"
        assert final.details["durationSeconds"] < 1.5
        assert (tmp_path / "starts").read_text() == "1"
        repeated = await output.execute({"task_id": task_id}, context())
        assert repeated.details["status"] == "timed_out"
        assert repeated.details["taskId"] == task_id

    try:
        asyncio.run(run())
    finally:
        tasks.close()


def test_large_stdout_stderr_are_drained_with_distinct_preview_and_log_limits(tmp_path):
    from backend.app.task_runtime import TaskRuntime

    tasks = TaskRuntime(tmp_path / "tasks", max_log_bytes=1024)
    shell = ShellTool(tmp_path, allowed_commands=[sys.executable], tasks=tasks, max_output_chars=50)
    try:
        result = asyncio.run(shell.execute({"command": command(
            "import sys; print('x'*200000); print('y'*200000, file=sys.stderr); print('done')"
        ), "auto_promote": False}, context()))
        assert result.details["status"] == "succeeded"
        assert result.details["outputChars"] > 400000
        assert result.details["truncated"] is True
        assert result.details["logTruncated"] is True
        assert result.details["logComplete"] is False
        assert Path(result.details["logPath"]).stat().st_size == 1024
        assert len(result.content) < 300
    finally:
        tasks.close()


def process_alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.parametrize("stop", [False, True])
def test_real_child_and_grandchild_are_owned_after_stop_or_parent_exit(tmp_path, stop):
    from backend.app.task_runtime import TaskRuntime
    from backend.app.agent_runtime.shell_tool import TaskOutputTool, TaskStopTool

    tasks = TaskRuntime(tmp_path / "tasks")
    shell = ShellTool(tmp_path, allowed_commands=[sys.executable], tasks=tasks, timeout_seconds=5)
    output, stopper = TaskOutputTool(tasks), TaskStopTool(tasks)
    grandchild = "import time; time.sleep(10)"
    child = (
        "import subprocess,sys,time; from pathlib import Path; "
        f"p=subprocess.Popen([sys.executable,'-c',{grandchild!r}]); "
        "Path('grandchild.pid').write_text(str(p.pid)); time.sleep(10)"
    )
    parent = (
        "import subprocess,sys,time; from pathlib import Path; "
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
        "Path('child.pid').write_text(str(p.pid))\n"
        "while not Path('release-parent').exists(): time.sleep(.01)"
    )

    async def run():
        submitted = await shell.execute({"command": command(parent), "run_in_background": True}, context())
        task_id = submitted.details["taskId"]
        deadline = time.monotonic() + 2
        while not (tmp_path / "grandchild.pid").exists():
            assert time.monotonic() < deadline, "controlled grandchild did not start"
            await asyncio.sleep(.01)
        pids = [int((tmp_path / name).read_text()) for name in ("child.pid", "grandchild.pid")]
        assert all(process_alive(pid) for pid in pids)
        if stop:
            first, second = await asyncio.gather(
                stopper.execute({"task_id": task_id}, context()), stopper.execute({"task_id": task_id}, context())
            )
            assert first.details["status"] == second.details["status"] == "cancelled"
        else:
            (tmp_path / "release-parent").touch()
            result = await output.execute({"task_id": task_id, "block": True, "timeout_ms": 2000}, context())
            assert result.details["status"] == "succeeded"
        assert all(not process_alive(pid) for pid in pids)

    try:
        asyncio.run(run())
    finally:
        tasks.close()


def test_lost_host_authorization_stops_an_already_backgrounded_process(tmp_path):
    from backend.app.task_runtime import TaskRuntime
    from backend.app.agent_runtime.shell_tool import TaskOutputTool

    grant = tmp_path / "grant"
    grant.touch()
    tasks = TaskRuntime(tmp_path / "tasks")
    def authorized(_):
        return grant.exists()
    shell = ShellTool(tmp_path, allowed_commands=[sys.executable], tasks=tasks, authorized=authorized)
    output = TaskOutputTool(tasks, authorized=authorized)

    async def run():
        submitted = await shell.execute({"command": command(
            "import os,time; from pathlib import Path; Path('pid').write_text(str(os.getpid())); time.sleep(10)"
        ), "run_in_background": True}, context())
        deadline = time.monotonic() + 2
        while not (tmp_path / "pid").exists():
            assert time.monotonic() < deadline
            await asyncio.sleep(.01)
        pid = int((tmp_path / "pid").read_text())
        grant.unlink()
        denied = await output.execute({"task_id": submitted.details["taskId"]}, context())
        assert denied.is_error is True
        assert "logPath" not in denied.details
        deadline = time.monotonic() + 1
        while process_alive(pid) and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        assert not process_alive(pid)

    try:
        asyncio.run(run())
    finally:
        tasks.close()


def test_task_id_does_not_grant_other_role_or_source_access_to_output_or_stop(tmp_path):
    from backend.app.task_runtime import TaskOrigin, TaskRuntime
    from backend.app.agent_runtime.shell_tool import TaskOutputTool, TaskStopTool

    tasks = TaskRuntime(tmp_path / "tasks")

    def origin(source):
        return lambda ctx: TaskOrigin(ctx.role_id, ctx.session_key, ctx.run_id, ctx.tool_call_id,
                                       "snapshot-v3", source, "account-1")

    shell = ShellTool(tmp_path, allowed_commands=[sys.executable], tasks=tasks, origin_resolver=origin("thread-1"))
    output, stopper = TaskOutputTool(tasks, origin_resolver=origin("thread-1")), TaskStopTool(tasks, origin_resolver=origin("thread-1"))

    async def run():
        submitted = await shell.execute({"command": command("import time; print('private-log'); time.sleep(.4)"),
                                         "run_in_background": True}, context())
        task_id = submitted.details["taskId"]
        for tool in (output, stopper, TaskOutputTool(tasks, origin_resolver=origin("thread-2")),
                     TaskStopTool(tasks, origin_resolver=origin("thread-2"))):
            caller = context(role="role-b") if tool in (output, stopper) else context()
            denied = await tool.execute({"task_id": task_id}, caller)
            unknown = await tool.execute({"task_id": "unknown"}, caller)
            assert denied == unknown
            assert denied.is_error is True
            assert "private-log" not in denied.content and "logPath" not in denied.details
        final = await output.execute({"task_id": task_id, "block": True, "timeout_ms": 2000}, context(run="later-run"))
        assert final.details["status"] == "succeeded"
        assert "private-log" in final.content
        assert final.details["origin"]["capabilityVersion"] == "snapshot-v3"

    try:
        asyncio.run(run())
    finally:
        tasks.close()


def test_shutdown_and_reopen_preserve_interrupted_result_without_reexecution(tmp_path):
    from backend.app.task_runtime import TaskRuntime
    from backend.app.agent_runtime.shell_tool import TaskOutputTool

    task_root = tmp_path / "tasks"
    tasks = TaskRuntime(task_root)
    shell = ShellTool(tmp_path, allowed_commands=[sys.executable], tasks=tasks)
    result = asyncio.run(shell.execute({"command": command("import time; print('before-shutdown'); time.sleep(10)"),
                                       "run_in_background": True}, context()))
    task_id = result.details["taskId"]
    tasks.close()
    restarted = TaskRuntime(task_root)
    try:
        observed = asyncio.run(TaskOutputTool(restarted).execute({"task_id": task_id}, context(run="after-restart")))
        assert observed.details["status"] == "interrupted"
        assert observed.details["taskId"] == task_id
        assert observed.details["origin"]["parentRunId"] == "run-1"
    finally:
        restarted.close()


def test_unclean_host_exit_kills_job_and_reopen_marks_missing_handle_interrupted(tmp_path):
    from backend.app.task_runtime import TaskRuntime
    from backend.app.agent_runtime.shell_tool import TaskOutputTool

    if os.name != "nt":
        pytest.skip("host crash ownership is guaranteed by Windows Job on the supported desktop platform")
    code = "import os,time; from pathlib import Path; Path('owned.pid').write_text(str(os.getpid())); time.sleep(20)"
    host = f'''
import asyncio, json, os, time
from pathlib import Path
from backend.app.task_runtime import TaskRuntime
from backend.app.agent_runtime import CancellationToken, ToolContext
from backend.app.agent_runtime.shell_tool import ShellTool
root = Path({str(tmp_path)!r})
tasks = TaskRuntime(root / 'tasks')
tool = ShellTool(root, allowed_commands=[{sys.executable!r}], tasks=tasks)
result = asyncio.run(tool.execute({{'command': {command(code)!r}, 'run_in_background': True}}, ToolContext('role-a','role:role-a','run-1',CancellationToken(),'call-1')))
deadline = time.monotonic() + 5
while not (root / 'owned.pid').exists():
    if time.monotonic() > deadline: raise TimeoutError('child did not start')
    time.sleep(.01)
(root / 'receipt.json').write_text(json.dumps(result.details))
os._exit(0)
'''
    subprocess.run([sys.executable, "-c", host], cwd=Path(__file__).resolve().parents[2], check=True, timeout=10,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    pid = int((tmp_path / "owned.pid").read_text())
    assert not process_alive(pid)
    task_id = json.loads((tmp_path / "receipt.json").read_text())["taskId"]
    tasks = TaskRuntime(tmp_path / "tasks")
    try:
        observed = asyncio.run(TaskOutputTool(tasks).execute({"task_id": task_id}, context()))
        assert observed.details["status"] == "interrupted"
        assert observed.details["taskId"] == task_id
        assert not process_alive(pid)
    finally:
        tasks.close()


def test_preview_truncation_still_locates_complete_log_within_storage_budget(tmp_path):
    from backend.app.task_runtime import TaskRuntime

    tasks = TaskRuntime(tmp_path / "tasks")
    shell = ShellTool(tmp_path, allowed_commands=[sys.executable], tasks=tasks, max_output_chars=30)
    try:
        result = asyncio.run(shell.execute({"command": command("print('begin-' + 'x'*100000 + '-end')"),
                                           "auto_promote": False}, context()))
        assert result.details["status"] == "succeeded"
        assert result.details["truncated"] is True
        assert result.details["logComplete"] is True
        assert result.details["logTruncated"] is False
        assert Path(result.details["logPath"]).read_text(encoding="utf-8") == "begin-" + "x" * 100000 + "-end\n"
        assert "-end" in result.content and len(result.content) < 150
    finally:
        tasks.close()


def test_role_concurrency_and_retained_task_limits_are_enforced(tmp_path):
    from backend.app.task_runtime import TaskRuntime
    from backend.app.agent_runtime.shell_tool import TaskOutputTool

    tasks = TaskRuntime(tmp_path / "tasks", max_per_role=1, max_retained_per_role=1)
    shell = ShellTool(tmp_path, allowed_commands=[sys.executable], tasks=tasks)
    output = TaskOutputTool(tasks)

    async def run():
        first = await shell.execute({"command": command("import time; print('first'); time.sleep(.3)"),
                                     "run_in_background": True}, context())
        denied = await shell.execute({"command": command("print('must-not-start')"), "run_in_background": True}, context())
        assert denied.is_error is True
        assert "concurrency" in denied.content
        complete = await output.execute({"task_id": first.details["taskId"], "block": True, "timeout_ms": 2000}, context())
        assert complete.details["status"] == "succeeded"
        second = await shell.execute({"command": command("print('second')"), "auto_promote": False}, context())
        assert second.details["status"] == "succeeded"
        old = await output.execute({"task_id": first.details["taskId"]}, context())
        assert old.details["status"] == "not_found"
        assert not Path(first.details["logPath"]).exists()
        assert Path(second.details["logPath"]).read_text(encoding="utf-8") == "second\n"

    try:
        asyncio.run(run())
    finally:
        tasks.close()


def test_async_foreground_cancellation_reaps_real_process_and_records_cancelled(tmp_path):
    from backend.app.task_runtime import TaskRuntime

    tasks = TaskRuntime(tmp_path / "tasks")
    shell = ShellTool(tmp_path, allowed_commands=[sys.executable], tasks=tasks)

    async def run():
        foreground = asyncio.create_task(shell.execute({"command": command(
            "import os,time; from pathlib import Path; Path('pid').write_text(str(os.getpid())); time.sleep(10)"
        ), "auto_promote": False}, context()))
        deadline = time.monotonic() + 2
        while not (tmp_path / "pid").exists():
            assert time.monotonic() < deadline
            await asyncio.sleep(.01)
        pid = int((tmp_path / "pid").read_text())
        foreground.cancel()
        with pytest.raises(asyncio.CancelledError):
            await foreground
        assert not process_alive(pid)
        recovered = await shell.execute({"command": command("print('recovered')")}, context(run="recovered-run"))
        assert recovered.details["status"] == "succeeded"

    try:
        asyncio.run(run())
    finally:
        tasks.close()


def test_nonzero_exit_and_model_timeout_cannot_escape_host_budget(tmp_path):
    from backend.app.task_runtime import TaskRuntime

    tasks = TaskRuntime(tmp_path / "tasks")
    shell = ShellTool(tmp_path, allowed_commands=[sys.executable], tasks=tasks, timeout_seconds=1)
    try:
        denied = asyncio.run(shell.execute({"command": command("print('must-not-run')"), "timeout": 60,
                                           "run_in_background": True}, context()))
        assert denied.is_error is True and "taskId" not in denied.details
        failed = asyncio.run(shell.execute({"command": command("import sys; print('nonzero'); sys.exit(7)")}, context()))
        assert failed.details["status"] == "failed"
        assert failed.details["exitCode"] == 7
        assert failed.is_error is True and "nonzero" in failed.content
    finally:
        tasks.close()


def test_role_log_budget_reserves_running_tasks_before_retaining_old_logs(tmp_path):
    from backend.app.task_runtime import TaskRuntime
    from backend.app.agent_runtime.shell_tool import TaskOutputTool

    tasks = TaskRuntime(tmp_path / "tasks", max_log_bytes=512, max_role_log_bytes=512)
    shell = ShellTool(tmp_path, allowed_commands=[sys.executable], tasks=tasks)
    output = TaskOutputTool(tasks)

    async def run():
        first = await shell.execute({"command": command("print('old-log')")}, context())
        second = await shell.execute({"command": command("import time; print('running'); time.sleep(.3)"),
                                      "run_in_background": True}, context())
        assert not Path(first.details["logPath"]).exists(), "reserve the new task's full storage budget at admission"
        denied = await shell.execute({"command": command("print('must-not-run')"), "run_in_background": True}, context())
        assert denied.is_error is True
        final = await output.execute({"task_id": second.details["taskId"], "block": True, "timeout_ms": 2000}, context())
        assert final.details["status"] == "succeeded"

    try:
        asyncio.run(run())
    finally:
        tasks.close()


@pytest.mark.parametrize("keep_running", [False, True])
def test_real_log_write_failure_stops_execution_and_reports_incomplete_failed_result(tmp_path, keep_running):
    from backend.app.task_runtime import TaskRuntime
    from backend.app.agent_runtime.shell_tool import TaskOutputTool

    tasks = TaskRuntime(tmp_path / "tasks")
    shell = ShellTool(tmp_path, allowed_commands=[sys.executable], tasks=tasks)
    output = TaskOutputTool(tasks)

    async def run():
        submitted = await shell.execute({"command": command(
            "import os,time; from pathlib import Path\n"
            "Path('owned.pid').write_text(str(os.getpid()))\n"
            "while not Path('release-output').exists(): time.sleep(.01)\n"
            "print('must-not-be-silently-lost')\n"
            + ("time.sleep(10)" if keep_running else "")
        ), "run_in_background": True}, context())
        log_path = Path(submitted.details["logPath"])
        log_path.unlink()
        log_path.mkdir()
        (tmp_path / "release-output").touch()
        observed = await output.execute({"task_id": submitted.details["taskId"], "block": True, "timeout_ms": 2000}, context())
        assert observed.details["status"] == "failed"
        assert observed.is_error is True
        assert observed.details["logComplete"] is False
        assert observed.details["logTruncated"] is True
        assert observed.details["logError"] in {"PermissionError", "IsADirectoryError"}
        assert "log write failed" in observed.content
        assert "log" in observed.details["progress"].lower()
        assert not process_alive(int((tmp_path / "owned.pid").read_text()))
        await asyncio.to_thread(tasks.delete_role, "role-a")
        assert not log_path.exists()

    try:
        asyncio.run(run())
    finally:
        tasks.close()
