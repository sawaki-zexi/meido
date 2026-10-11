from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path

from .process import WindowsJob, release_process_tree_owner, spawn_process_tree, terminate_process_tree
from .safety import sanitize_browser_text


class BrowserDaemon:
    """Owns the fixed agent-browser daemon and waits until its session is reachable."""

    def __init__(self, executable: Path, profile: Path, env: dict[str, str]) -> None:
        self._executable = executable
        self._profile = profile
        self._env = env
        self._process: asyncio.subprocess.Process | None = None
        self._owner: WindowsJob | None = None
        self._output: deque[str] = deque()
        self._output_chars = 0
        self._output_tasks: list[asyncio.Task[None]] = []
        self._port_file: Path | None = None
        self._config_file: Path | None = None

    async def start(self, timeout: float) -> None:
        run_dir = Path(self._env["AGENT_BROWSER_SOCKET_DIR"])
        namespace = self._env["AGENT_BROWSER_NAMESPACE"]
        session = self._env["AGENT_BROWSER_SESSION"]
        socket_dir = run_dir / "namespaces" / namespace / "run"
        socket_dir.mkdir(parents=True, exist_ok=True)
        self._port_file = socket_dir / f"{session}.port"
        self._config_file = socket_dir / f"{session}.config"
        with self._config_file.open("x", encoding="utf-8") as config:
            # Verified for the pinned 0.38.2 daemon defaults in Shiori's source.
            config.write("785b971085b3e7e9")
        try:
            async with asyncio.timeout(timeout):
                self._process, self._owner = await spawn_process_tree(
                    str(self._executable),
                    cwd=str(self._profile),
                    env={**self._env, "AGENT_BROWSER_DAEMON": "1"},
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                assert self._process.stdout is not None
                assert self._process.stderr is not None
                self._output_tasks = [
                    asyncio.create_task(self._drain_output(self._process.stdout)),
                    asyncio.create_task(self._drain_output(self._process.stderr)),
                ]
                while True:
                    process = self._process
                    if process.returncode is not None:
                        diagnostic = "".join(self._output)[:4000]
                        raise RuntimeError(f"Browser Use daemon 启动失败：{diagnostic}")
                    port = self._read_port()
                    if port is not None:
                        try:
                            _, writer = await asyncio.open_connection("127.0.0.1", port)
                        except (ConnectionRefusedError, OSError):
                            pass
                        else:
                            writer.close()
                            await writer.wait_closed()
                            return
                    await asyncio.sleep(0.02)
        except BaseException:
            await self.close()
            raise

    async def _drain_output(self, stream) -> None:
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                text, _ = sanitize_browser_text(chunk.decode("utf-8", errors="replace"))
                if not text:
                    continue
                self._output.append(text)
                self._output_chars += len(text)
                while self._output_chars > 4000 and self._output:
                    removed = self._output.popleft()
                    self._output_chars -= len(removed)
        except asyncio.CancelledError:
            raise

    def _read_port(self) -> int | None:
        if self._port_file is None or not self._port_file.is_file():
            return None
        try:
            port = int(self._port_file.read_text(encoding="utf-8").strip())
        except (OSError, UnicodeError, ValueError):
            return None
        return port if 0 < port < 65536 else None

    async def close(self) -> None:
        process, self._process = self._process, None
        owner, self._owner = self._owner, None
        failure = None
        try:
            if owner is not None:
                try:
                    await release_process_tree_owner(owner)
                except BaseException as error:
                    failure = error
            if process is not None and process.returncode is None:
                if owner is None or failure is not None:
                    await terminate_process_tree(process)
                else:
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        await terminate_process_tree(process)
            elif process is not None:
                await terminate_process_tree(process)
            if failure is not None:
                raise failure
        finally:
            for path in (self._port_file, self._config_file):
                if path is not None:
                    path.unlink(missing_ok=True)
            self._port_file = None
            self._config_file = None
            if self._output_tasks:
                for task in self._output_tasks:
                    task.cancel()
                await asyncio.gather(*self._output_tasks, return_exceptions=True)
                self._output_tasks = []
            self._output.clear()
            self._output_chars = 0
