"""File-backed QQ processes also work in Windows reload server event loops."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Mapping
from typing import BinaryIO

from ..browser_use.process import WindowsJob, spawn_process_tree


class _FileProcess:
    def __init__(self, process: subprocess.Popen) -> None:
        self._process = process

    @property
    def returncode(self) -> int | None:
        return self._process.poll()

    def kill(self) -> None:
        self._process.kill()

    async def wait(self) -> int:
        return await asyncio.to_thread(self._process.wait)


async def spawn_napcat(
    *command: str, cwd: str, env: Mapping[str, str], stdout: BinaryIO,
    stderr: int = subprocess.STDOUT,
) -> tuple[asyncio.subprocess.Process | _FileProcess, WindowsJob | None]:
    if sys.platform != "win32" or not isinstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop):
        return await spawn_process_tree(*command, cwd=cwd, env=env, stdout=stdout, stderr=stderr)
    # Windows SelectorEventLoop has no subprocess transport. QQ writes to a
    # file, so Popen needs no loop-bound pipes. Spawn suspended and assign the
    # existing kill-on-close Job before resuming; no await can abandon it.
    process = subprocess.Popen(
        command, cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL,
        stdout=stdout, stderr=stderr, creationflags=0x08000000 | 0x00000004,
    )
    try:
        return _FileProcess(process), WindowsJob(process.pid, resume=True)
    except BaseException:
        process.kill()
        process.wait(timeout=5)
        raise
