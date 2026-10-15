from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

from ..task_runtime import TaskBudget, TaskOrigin, TaskRuntime, TaskSnapshot
from ..task_runtime.process import ProcessExecutor
from .tools import AgentToolResult, ToolContext, ToolDefinition, ToolRegistry


SHELL_TOOL_NAMES = frozenset({"shell", "task_output", "task_stop"})


def _origin(context: ToolContext) -> TaskOrigin:
    return TaskOrigin(context.role_id, context.session_key, context.run_id, context.tool_call_id)


def _result(snapshot: TaskSnapshot, *, background: bool = False) -> AgentToolResult:
    details = snapshot.details()
    details["background"] = background
    output = snapshot.output
    if details["truncated"]:
        output = f"[output truncated to {snapshot.budget.output_chars} chars; see retained log]\n{output}"
    if snapshot.log_truncated:
        output = "[log storage budget reached; retained log is incomplete]\n" + output
    if background:
        output = f"[background task {snapshot.task_id}; use task_output or task_stop]\n{output}"
    elif snapshot.status not in {"succeeded", "running", "queued"}:
        output = f"[{snapshot.status}] {output}"
    return AgentToolResult(output or f"[{snapshot.status}]", details=details,
                           is_error=snapshot.status not in {"succeeded", "running", "queued"})


def _error(text: str, status: str = "denied") -> AgentToolResult:
    return AgentToolResult(text, is_error=True, details={"status": status, "exitCode": None})


class ShellTool:
    """Keep argv policy at the tool boundary; delegate lifecycle to TaskRuntime."""

    definition = ToolDefinition(
        name="shell",
        description="Run an allowlisted argv command in the role workspace. Long commands may become owned background tasks; use task_output/task_stop with their ID. timeout is seconds within the host budget.",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "minLength": 1},
                "description": {"type": "string", "maxLength": 500},
                "timeout": {"type": "number", "minimum": 0.001},
                "run_in_background": {"type": "boolean"},
                "auto_promote": {"type": "boolean"},
            },
            "required": ["command"], "additionalProperties": False,
        }, risk="external",
    )

    def __init__(
        self, workspace: str | Path, *, allowed_commands: list[str] | tuple[str, ...] = (),
        timeout_seconds: float = 30, max_output_chars: int = 20_000,
        tasks: TaskRuntime | None = None, promote_after_seconds: float = 15,
        origin_resolver: Callable[[ToolContext], TaskOrigin] = _origin,
        authorized: Callable[[ToolContext], bool] | None = None,
    ) -> None:
        self.definition = replace(self.definition, source="role", timeout_seconds=timeout_seconds + 2,
                                  output_limit=max_output_chars)
        self.workspace = Path(workspace).resolve()
        self.allowed_commands = tuple(item.strip() for item in allowed_commands if item.strip())
        self.timeout_seconds, self.max_output_chars = timeout_seconds, max_output_chars
        self.tasks, self.promote_after_seconds = tasks, promote_after_seconds
        self.origin_resolver, self.authorized = origin_resolver, authorized

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        try:
            ToolRegistry.validate_definition(self.definition, arguments)
        except ValueError as error:
            return _error(str(error))
        if self.authorized is not None and not self.authorized(context):
            return _error("Shell authorization has been revoked")
        if context.signal.is_cancelled():
            return _error("parent run is cancelled", "cancelled")
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            return _error("command must be a non-empty string")
        try:
            argv = shlex.split(command, posix=True)
        except ValueError as error:
            return _error(f"invalid command: {error}")
        if not argv:
            return _error("command must be a non-empty string")
        raw_executable, executable = argv[0], Path(argv[0]).name
        allowed = any(
            (Path(item).is_absolute() and os.path.normcase(str(Path(argv[0]).resolve())) == os.path.normcase(str(Path(item).resolve())))
            or (not Path(item).is_absolute() and "/" not in raw_executable and "\\" not in raw_executable
                and os.path.normcase(executable) == os.path.normcase(item))
            for item in self.allowed_commands
        )
        if not allowed:
            return _error("command denied by role policy")
        if not self.workspace.is_dir():
            return _error("workspace is unavailable", "failed")
        timeout = arguments.get("timeout", self.timeout_seconds)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= self.timeout_seconds:
            return _error("timeout exceeds role host budget")
        clean_env = {key: value for key, value in os.environ.items()
                     if key in {"PATH", "SystemRoot", "WINDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL", "TMP", "TEMP"}}
        # Lazy standalone ownership keeps catalog construction read-only. The
        # application always injects its shared runtime and closes it on exit.
        if self.tasks is None:
            self.tasks = TaskRuntime(self.workspace.parent / ".meido-tasks" / hashlib.sha256(str(self.workspace).encode()).hexdigest())
        tasks, origin = self.tasks, self.origin_resolver(context)
        authority = self.authorized
        try:
            task_id = tasks.submit(origin, ProcessExecutor(argv, self.workspace, clean_env),
                                   budget=TaskBudget(timeout, self.max_output_chars, tasks.max_log_bytes), kind="shell",
                                   description=str(arguments.get("description", "")),
                                   authorized=(lambda: authority(context)) if authority is not None else None)
        except (PermissionError, ValueError) as error:
            return _error(str(error))
        detached = False
        started = time.monotonic()
        try:
            while True:
                if self.authorized is not None and not self.authorized(context):
                    return _error("Shell authorization has been revoked")
                snapshot = tasks.query(task_id, origin)
                if snapshot.ended_at is not None:
                    return _result(snapshot)
                if context.signal.is_cancelled():
                    return _result(await asyncio.to_thread(tasks.stop, task_id, origin))
                if arguments.get("run_in_background", False) or (
                    arguments.get("auto_promote", True) and time.monotonic() - started >= self.promote_after_seconds
                ):
                    detached = True
                    return _result(snapshot, background=True)
                await asyncio.sleep(.01)
        except asyncio.CancelledError:
            if not detached:
                stopping = asyncio.create_task(asyncio.to_thread(tasks.stop, task_id, origin))
                await asyncio.shield(stopping)
            raise


class TaskOutputTool:
    definition = ToolDefinition(
        name="task_output", description="Read an owned task's status, bounded output tail and retained log location. Waiting observes the same task and never restarts it.",
        input_schema={"type": "object", "properties": {
            "task_id": {"type": "string"}, "block": {"type": "boolean"},
            "timeout_ms": {"type": "integer", "minimum": 0, "maximum": 30_000},
        }, "required": ["task_id"], "additionalProperties": False},
        source="role", timeout_seconds=32,
    )

    def __init__(self, tasks: TaskRuntime, *, origin_resolver: Callable[[ToolContext], TaskOrigin] = _origin,
                 authorized: Callable[[ToolContext], bool] | None = None):
        self.tasks, self.origin_resolver, self.authorized = tasks, origin_resolver, authorized

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        try:
            ToolRegistry.validate_definition(self.definition, arguments)
            if self.authorized is not None and not self.authorized(context):
                return _error("Shell authorization has been revoked")
            task_id, origin = str(arguments["task_id"]), self.origin_resolver(context)
            timeout_ms = arguments.get("timeout_ms", 30_000)
            wait_ms = timeout_ms if isinstance(timeout_ms, int) and arguments.get("block", False) else 0
            deadline = time.monotonic() + wait_ms / 1000
            while True:
                if self.authorized is not None and not self.authorized(context):
                    return _error("Shell authorization has been revoked")
                snapshot = self.tasks.query(task_id, origin)
                if snapshot.ended_at is not None or time.monotonic() >= deadline or context.signal.is_cancelled():
                    return _result(snapshot)
                await asyncio.sleep(min(.025, max(0, deadline - time.monotonic())))
        except (KeyError, ValueError):
            return _error("task not found in this role and original context", "not_found")


class TaskStopTool(TaskOutputTool):
    definition = ToolDefinition(
        name="task_stop", description="Stop the complete owned task process tree; repeated stops preserve its terminal result.",
        input_schema={"type": "object", "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"], "additionalProperties": False},
        risk="mutating", source="role", timeout_seconds=15,
    )

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        try:
            ToolRegistry.validate_definition(self.definition, arguments)
            if self.authorized is not None and not self.authorized(context):
                return _error("Shell authorization has been revoked")
            snapshot = await asyncio.to_thread(self.tasks.stop, str(arguments["task_id"]), self.origin_resolver(context))
            return _result(snapshot)
        except (KeyError, ValueError):
            return _error("task not found in this role and original context", "not_found")
