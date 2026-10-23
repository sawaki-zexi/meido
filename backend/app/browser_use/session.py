from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from uuid import uuid4

from .config import BrowserUseConfig
from .daemon import BrowserDaemon
from .mcp import McpStdioSession, McpToolError
from .profile import ProfileLease
from .runtime import BrowserRuntime


class BrowserSession:
    def __init__(self, root: Path, role_id: str, config: BrowserUseConfig, runtime: BrowserRuntime) -> None:
        identity = hashlib.sha256(role_id.encode()).hexdigest()
        self._identity = identity
        self.profile = root / "profiles" / identity
        self.profile.mkdir(parents=True, exist_ok=True)
        self._root = root
        self._config = config
        self._runtime = runtime
        self._name = f"role-{identity[:16]}-{uuid4().hex[:12]}"
        namespace = "meido-" + hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
        self._env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_BROWSER_")}
        self._env.update({
            "AGENT_BROWSER_SESSION": self._name,
            "AGENT_BROWSER_NAMESPACE": namespace,
            "AGENT_BROWSER_PROFILE": str(self.profile),
            "AGENT_BROWSER_EXECUTABLE_PATH": str(runtime.chrome),
            "AGENT_BROWSER_HEADED": "true" if config.headed else "false",
            "AGENT_BROWSER_CONFIG": str(root / "run" / f"{self._name}.json"),
            "AGENT_BROWSER_IDLE_TIMEOUT_MS": "0",
            "AGENT_BROWSER_SOCKET_DIR": str(root / "run"),
        })
        Path(self._env["AGENT_BROWSER_SOCKET_DIR"]).mkdir(parents=True, exist_ok=True)
        self._client: McpStdioSession | None = None
        self._daemon: BrowserDaemon | None = None
        self._lease: ProfileLease | None = None
        self._config_path = Path(self._env["AGENT_BROWSER_CONFIG"])
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed and self._client is None and self._daemon is None and self._lease is None

    async def call(self, name: str, arguments: dict[str, object], **kwargs) -> object:
        del kwargs
        if self._closed:
            raise RuntimeError("Browser Use 会话已停止，请重新调用工具")
        try:
            if self._client is None:
                self._lease = ProfileLease(self.profile)
                self._config_path.parent.mkdir(parents=True, exist_ok=True)
                self._config_path.write_text("{}", encoding="utf-8")
                self._daemon = BrowserDaemon(self._runtime.agent_browser, self.profile, self._env)
                await self._daemon.start(self._config.timeout_seconds)
                self._client = McpStdioSession(
                    [str(self._runtime.agent_browser), "mcp", "--tools", "core"],
                    cwd=str(self.profile), env=self._env,
                )
                try:
                    await self._client.connect()
                except BaseException:
                    await self.close()
                    raise
            if name == "agent_browser_screenshot":
                screenshots = self._root / "screenshots" / self._identity
                screenshots.mkdir(parents=True, exist_ok=True)
                extension = ".jpg" if arguments.get("format") == "jpeg" else ".png"
                arguments = {
                    **arguments,
                    "path": str(screenshots / f"latest{extension}"),
                }
            return await self._client.call(
                name,
                {**arguments, "session": self._name, "timeoutMs": self._config.timeout_seconds * 1000},
                timeout=self._config.timeout_seconds,
            )
        except asyncio.CancelledError:
            await self.close()
            raise
        except McpToolError:
            # A completed page/action error (for example a stale @ref) keeps
            # the established browser alive so the model can observe again.
            raise
        except Exception:
            await self.close()
            raise

    async def close(self, *, graceful: bool = False) -> None:
        if self.closed:
            return
        self._closed = True
        failure = None
        client, daemon = self._client, self._daemon
        try:
            if client is not None:
                try:
                    if graceful:
                        await client.call("agent_browser_close", {"session": self._name}, timeout=5)
                except Exception:
                    pass
                disconnect = getattr(client, "disconnect", None)
                if disconnect is None:
                    disconnect = client.close
                await disconnect()
                self._client = None
        except BaseException as error:
            failure = error
        try:
            if daemon is not None:
                await daemon.close()
                self._daemon = None
        except BaseException as error:
            failure = failure or error
        # Keep the profile lease and the exact failed native references until
        # both original owners confirm exit. A retry never substitutes owners.
        if self._client is None and self._daemon is None:
            self._config_path.unlink(missing_ok=True)
            if self._lease is not None:
                self._lease.close()
                self._lease = None
        if failure is not None:
            raise failure
