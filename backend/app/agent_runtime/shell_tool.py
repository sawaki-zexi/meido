from __future__ import annotations

import asyncio
import os
import shlex
import signal as signal_module
import subprocess
import time
from pathlib import Path
from collections.abc import Mapping, Callable

from .tools import AgentToolResult, ToolContext, ToolDefinition


class ShellTool:
    """Execute an explicitly allowlisted executable inside a role workspace."""

    definition = ToolDefinition(
        name="shell",
        description="Run an approved executable without shell expansion in the role workspace and return bounded output.",
        input_schema={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
        risk="external",
    )

    def __init__(
        self,
        workspace: str | Path,
        *,
        allowed_commands: list[str] | tuple[str, ...] = (),
        timeout_seconds: float = 30.0,
        max_output_chars: int = 20_000,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        # Keep absolute paths in their original form. Lowercasing a complete
        # path breaks valid case-sensitive POSIX paths such as hosted Python
        # installations; command names are normalized only when matched.
        self.allowed_commands = tuple(item.strip() for item in allowed_commands if item.strip())
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars

    async def execute(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
        on_update: Callable[[AgentToolResult], None] | None = None,
    ) -> AgentToolResult:
        del on_update
        unknown = [key for key in arguments if key != "command"]
        if unknown:
            return AgentToolResult(f"unknown arguments: {', '.join(str(key) for key in unknown)}", is_error=True)
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            return AgentToolResult("command must be a non-empty string", is_error=True)
        try:
            # Parse a command line into argv without invoking a shell. POSIX
            # quoting is the common model-facing format on every platform.
            argv = shlex.split(command, posix=True)
        except ValueError as error:
            return AgentToolResult(f"invalid command: {error}", is_error=True)
        if not argv:
            return AgentToolResult("command must be a non-empty string", is_error=True)
        executable = Path(argv[0]).name
        raw_executable = argv[0]
        allowed = any(
            (
                Path(item).is_absolute()
                and os.path.normcase(str(Path(argv[0]).resolve()))
                == os.path.normcase(str(Path(item).resolve()))
            )
            or (
                not Path(item).is_absolute()
                and "/" not in raw_executable
                and "\\" not in raw_executable
                and os.path.normcase(executable) == os.path.normcase(item)
            )
            for item in self.allowed_commands
        )
        if not allowed:
            return self._result("command denied by role policy", status="denied", exit_code=None)
        if not self.workspace.is_dir():
            return self._result("workspace is unavailable", status="failed", exit_code=None)

        clean_env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "SystemRoot", "WINDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL", "TMP", "TEMP"}
        }
        started = time.monotonic()
        process: asyncio.subprocess.Process | None = None
        stdout_task: asyncio.Task[tuple[str, bool, int]] | None = None
        stderr_task: asyncio.Task[tuple[str, bool, int]] | None = None
        wait_task: asyncio.Task[int] | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.workspace,
                env=clean_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            stdout_task = asyncio.create_task(self._read_stream(process.stdout))
            stderr_task = asyncio.create_task(self._read_stream(process.stderr))
            wait_task = asyncio.create_task(process.wait())
            deadline = started + self.timeout_seconds
            status = "succeeded"
            while not wait_task.done():
                if context.signal.is_cancelled():
                    status = "cancelled"
                    await self._terminate(process)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    status = "timed_out"
                    await self._terminate(process)
                    break
                try:
                    await asyncio.wait_for(asyncio.shield(wait_task), min(remaining, 0.05))
                except asyncio.TimeoutError:
                    continue
            await wait_task
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            exit_code = process.returncode
            output = stdout[0] + ("\nstderr: " + stderr[0] if stderr[0] else "")
            truncated = stdout[1] or stderr[1]
            output_chars = stdout[2] + stderr[2]
            return self._result(output, status=status if status != "succeeded" else ("succeeded" if exit_code == 0 else "failed"), exit_code=exit_code, started=started, truncated=truncated, output_chars=output_chars)
        except FileNotFoundError:
            return self._result(f"executable not found: {argv[0]}", status="failed", exit_code=None, started=started)
        except asyncio.CancelledError:
            if process is not None:
                await self._terminate(process)
            pending = [task for task in (stdout_task, stderr_task, wait_task) if task is not None]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            raise
        except Exception as error:
            if process is not None:
                await self._terminate(process)
            return self._result(str(error), status="failed", exit_code=None, started=started)

    async def _read_stream(self, stream: asyncio.StreamReader | None) -> tuple[str, bool, int]:
        if stream is None:
            return "", False, 0
        captured = ""
        total = 0
        truncated = False
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            total += len(text)
            if len(captured) + len(text) > self.max_output_chars:
                truncated = True
            captured = (captured + text)[-self.max_output_chars :]
        return captured, truncated, total

    def _result(self, output: str, *, status: str, exit_code: int | None, started: float | None = None, truncated: bool = False, output_chars: int | None = None) -> AgentToolResult:
        truncated = truncated or len(output) > self.max_output_chars
        preview = output[-self.max_output_chars :] if truncated else output
        details = {
            "status": status,
            "exitCode": exit_code,
            "truncated": truncated,
            "outputChars": output_chars if output_chars is not None else len(output),
            "durationSeconds": round(time.monotonic() - started, 3) if started is not None else None,
        }
        text = preview or f"[{status}]"
        if truncated:
            text = f"[output truncated to {self.max_output_chars} chars]\n{text}"
        if status != "succeeded":
            text = f"[{status}] {text}"
        return AgentToolResult(text, details=details, is_error=status != "succeeded")

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            process.kill()
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=1.0)
            except (FileNotFoundError, OSError, asyncio.TimeoutError, ProcessLookupError):
                pass
        else:
            try:
                killpg = getattr(os, "killpg", None)
                if callable(killpg):
                    killpg(process.pid, signal_module.SIGTERM)
                else:
                    process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    if callable(killpg):
                        killpg(process.pid, getattr(signal_module, "SIGKILL", signal_module.SIGTERM))
                    else:
                        process.kill()
            except ProcessLookupError:
                pass
            except OSError:
                process.kill()
