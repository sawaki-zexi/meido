# Adapted from Shiori-Agent 71c8b0e (MIT; third_party/shiori-agent).
"""Argument-vector subprocess calls; Herdr server lifetime is never owned here."""

import asyncio
import json
import os
import re
import shutil
import sys
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


from .config import HerdrConfig

# Batch files cross cmd.exe parsing even with create_subprocess_exec. Reject
# expansion, grouping, quoting and control syntax instead of attempting to escape it.
_UNSAFE_BATCH_CHARACTERS = re.compile(r'[&|<>^%!"()\x00-\x1f\x7f]')


class CommandError(RuntimeError):
    """A command failed; timeout/transport errors may leave Herdr work running."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "command_failed",
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


class HerdrCli:
    """Every Herdr command is scoped to the configured named local session."""

    def __init__(self, config: HerdrConfig, cwd: Path) -> None:
        self.config = config
        self.cwd = cwd

    async def require_supported_version(self) -> None:
        """Herdr 0.9.4 fixes native agent startup on Windows npm installations."""
        output = await self.command([self.config.executable, "--version"])
        version = re.search(r"\bherdr\s+(\d+)\.(\d+)\.(\d+)\b", output)
        if version is None or tuple(int(part) for part in version.groups()) < (0, 9, 4):
            raise CommandError(
                "Herdr 插件要求 Herdr >= 0.9.4，请先更新本机 Herdr。",
                code="unsupported_version",
            )
        # A fresh CLI can still talk to an older persistent server. This command
        # is read-only and uses a top-level JSON shape, unlike control responses.
        output = await self.command(
            [
                self.config.executable,
                "--session",
                self.config.session,
                "status",
                "server",
                "--json",
            ]
        )
        try:
            status = json.loads(output)
            if not isinstance(status, dict):
                raise ValueError("server status must be an object")
        except ValueError as error:
            raise CommandError("Herdr 返回了无法识别的服务状态", code="invalid_response") from error
        if status.get("running") is not True:
            raise CommandError(
                f"Herdr session {self.config.session!r} 未运行，请先启动指定 session。",
                code="server_unavailable",
            )
        version = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(status.get("version", "")))
        if (
            version is None
            or tuple(int(part) for part in version.groups()) < (0, 9, 4)
            or status.get("compatible") is not True
            or status.get("endpoint_compatible") is not True
        ):
            raise CommandError(
                "指定 Herdr session 的运行服务必须升级到兼容的 >= 0.9.4 版本。",
                code="unsupported_version",
            )

    async def command(
        self, argv: list[str], *, cwd: Path | None = None, timeout: float | None = None
    ) -> str:
        """Run one validated argv command; terminate only this child on timeout."""
        executable = argv[0]
        if sys.platform == "win32":
            # CreateProcess does not resolve bare npm commands through PATHEXT.
            # Resolve once before spawning; never retry a failed command.
            if os.path.dirname(executable):
                executable = str((cwd or self.cwd) / executable)
            resolved = shutil.which(executable)
            if resolved is None:
                raise CommandError(f"找不到可执行程序：{argv[0]}", code="executable_not_found")
            executable = resolved
            if Path(executable).suffix.casefold() in {".cmd", ".bat"} and any(
                _UNSAFE_BATCH_CHARACTERS.search(argument)
                for argument in (executable, *argv[1:])
            ):
                raise CommandError(
                    "Windows .cmd/.bat 的路径或参数含可能被 cmd 重解释的字符；"
                    "请改用原生可执行程序或 Node/Python 脚本的 argv 记录该验证。",
                    code="unsafe_windows_batch_argument",
                )
        # Uvicorn reload uses a Windows SelectorEventLoop, which cannot spawn
        # asyncio subprocesses. The worker owns exactly this CLI child; cancelling
        # its caller joins cleanup before returning, without touching the server.
        stopped = threading.Event()
        worker = asyncio.create_task(asyncio.to_thread(
            self._command, [executable, *argv[1:]], cwd or self.cwd,
            timeout or self.config.cli_timeout_seconds, stopped))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            stopped.set()
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if worker.done() and not worker.cancelled():
                worker.exception()
            raise

    @staticmethod
    def _command(argv: list[str], cwd: Path, timeout: float, stopped: threading.Event) -> str:
        try:
            process = subprocess.Popen(argv, cwd=str(cwd), env=dict(os.environ),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        except OSError as error:
            raise CommandError(str(error), code="executable_not_found" if isinstance(error, FileNotFoundError)
                               else "command_failed") from error
        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if stopped.is_set() or remaining <= 0:
                    raise CommandError("CLI 等待超时或已停止；输入可能已送达，未重发请求。", code="timeout")
                try:
                    stdout, stderr = process.communicate(timeout=min(.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            output = stdout.decode("utf-8", errors="replace")
            if process.returncode:
                message = stderr.decode("utf-8", errors="replace").strip() or output
                code = "command_failed"
                try:
                    payload = json.loads(message)
                    detail = payload.get("error", {})
                    code = str(detail.get("code", code))
                    message = str(detail.get("message", message))
                except (ValueError, AttributeError):
                    pass
                raise CommandError(message[:12000] or f"命令退出码 {process.returncode}",
                                   code=code, exit_code=process.returncode)
            return output
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    async def call(self, *args: str, timeout: float | None = None) -> dict[str, Any]:
        """Decode a structured Herdr response, including server-side failures.

        `timeout` replaces the configured CLI timeout for this process.
        """
        output = await self.command(
            [self.config.executable, "--session", self.config.session, *args],
            timeout=timeout,
        )
        try:
            payload = json.loads(output)
            if not isinstance(payload, dict):
                raise ValueError("response must be an object")
            if payload.get("ok") is False or "error" in payload:
                error = payload["error"]
                raise CommandError(str(error["message"]), code=str(error["code"]))
            result = payload["result"]
            if not isinstance(result, dict):
                raise ValueError("result must be an object")
            return result
        except (ValueError, KeyError, TypeError) as error:
            raise CommandError(
                "Herdr 返回了无法识别的响应", code="invalid_response"
            ) from error

    async def read(self, agent: str, *, source: str = "recent-unwrapped") -> str:
        """Herdr agent read is text, unlike the other command responses."""
        return (
            await self.command(
                [
                    self.config.executable,
                    "--session",
                    self.config.session,
                    "agent",
                    "read",
                    agent,
                    "--source",
                    source,
                    "--lines",
                    "100",
                ]
            )
        )[-16000:]
