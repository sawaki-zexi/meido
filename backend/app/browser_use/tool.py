from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Mapping

from ..agent_runtime.tools import AgentToolResult, ToolContext, ToolDefinition
from ..agent_runtime.types import ImageContent
from .config import BrowserUseConfig
from .mcp import McpToolError
from .runtime import BrowserRuntimeUnavailable
from .safety import sanitize_browser_text

_OWNED = frozenset({
    "session", "namespace", "extraArgs", "headed", "all", "path", "screenshotDir",
    "restore", "restoreSave", "restoreCheckFn", "restoreCheckText", "restoreCheckUrl",
    "idleTimeout", "timeoutMs", "caCert", "clearCaCert", "allowedDomains", "webgpu", "webmcp",
})


class BrowserTool:
    context_precedence = frozenset({"role_id"})

    def __init__(self, sessions: "BrowserSessions", spec: Mapping[str, Any]) -> None:
        self._sessions = sessions
        self._spec = dict(spec)
        self.definition = ToolDefinition(
            name=str(spec["name"]),
            description="[Browser Use] " + str(spec["description"]) + " 当前角色独立浏览器；导航或引用失效后先 snapshot 重新观察。",
            input_schema=dict(spec["inputSchema"]),
            risk="external",
            source="plugin:browser_use",
            version="0.38.2",
            timeout_seconds=sessions.timeout_seconds,
            output_limit=12000, search_hint='浏览器 网页 导航 点击 填表 标签页 截图 browser',
        )

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        if _OWNED.intersection(arguments):
            return AgentToolResult("Browser Use 会话、进程与截图路径由宿主管理", is_error=True, details={"status": "denied"})
        properties = self.definition.input_schema.get("properties")
        allowed = set(properties) if isinstance(properties, Mapping) else set()
        filtered = {key: value for key, value in arguments.items() if key in allowed}
        result = await self._sessions.call(
            context.role_id,
            self.definition.name,
            filtered,
            context.signal,
            run_id=context.run_id,
        )
        return result


def load_tools(sessions: "BrowserSessions") -> tuple[BrowserTool, ...]:
    specs = load_tool_specs()
    return tuple(BrowserTool(sessions, spec) for spec in specs)


def load_tool_specs() -> tuple[Mapping[str, Any], ...]:
    value = json.loads((Path(__file__).with_name("tools.json")).read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("Browser Use 工具清单必须是数组")
    return tuple(item for item in value if isinstance(item, Mapping))


BROWSER_USE_TOOL_NAMES = tuple(str(spec["name"]) for spec in load_tool_specs())


def result_from_remote(value: object, *, max_image_bytes: int = 2_000_000) -> AgentToolResult:
    text = ""
    images: tuple[ImageContent, ...] = ()
    if isinstance(value, Mapping):
        text_value = value.get("text") or value.get("content") or value.get("message")
        if isinstance(text_value, str):
            text = text_value
        raw = value.get("image") or value.get("data")
        media = value.get("mimeType") or value.get("mediaType") or "image/png"
        if isinstance(raw, str) and raw and len(raw) <= max_image_bytes * 2 and str(media).startswith("image/"):
            try:
                decoded = base64.b64decode(raw, validate=True)
            except (ValueError, TypeError):
                decoded = b""
            if len(decoded) <= max_image_bytes:
                images = (ImageContent(str(media), base64.b64encode(decoded).decode("ascii")),)
    else:
        text = str(value)
    text, redactions = sanitize_browser_text(text)
    details: dict[str, object] = {"status": "succeeded", "imageCount": len(images)}
    if redactions:
        details["redactions"] = redactions
    return AgentToolResult(text[:12000], images=images, details=details)


class BrowserSessions:
    """Host-owned role sessions; the transport is injected for deterministic tests."""

    def __init__(self, root: Path, config: BrowserUseConfig, *, transport: Any = None, resources: Path | None = None, runtime: Any = None, enabled=lambda: True) -> None:
        self.root = root
        self.config = config
        self.timeout_seconds = config.timeout_seconds
        self._transport = transport
        self._resources = resources or root
        self._runtime = runtime
        self._sessions: dict[tuple[str, str], Any] = {}
        self._locks: dict[tuple[str, str], Any] = {}
        self._closing_runs: set[tuple[str, str]] = set()
        self._close_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._active: dict[tuple[str, str], set[asyncio.Task[object]]] = {}
        self._disabled_roles: set[str] = set()
        self._closed = False
        self._enabled = enabled
        self._epoch = 0

    @staticmethod
    def _key(role_id: str, run_id: str | None) -> tuple[str, str]:
        return role_id, run_id or ""

    def dependency_status(self) -> dict[str, str]:
        """Check local requirements without creating a session or process."""
        if self._closed or not self._enabled():
            return {"status": "unavailable", "reason": "Browser Use 插件已停用"}
        if self._transport is not None:
            return {"status": "ready", "reason": "已配置浏览器传输；尚未验证实际连接"}
        try:
            from .runtime import BrowserRuntime
            runtime = self._runtime or BrowserRuntime.resolve(self._resources)
            if not runtime.agent_browser.is_file() or not runtime.chrome.is_file():
                raise BrowserRuntimeUnavailable("Browser Use 运行组件缺失")
        except BrowserRuntimeUnavailable as error:
            return {"status": "unavailable", "reason": str(error)}
        return {"status": "ready", "reason": "本机运行组件存在；尚未启动或验证浏览器"}

    async def call(
        self,
        role_id: str,
        name: str,
        arguments: dict[str, object],
        signal,
        *,
        run_id: str = "",
    ) -> AgentToolResult:
        if self._closed or not self._enabled():
            return AgentToolResult("Browser Use 插件已停用", is_error=True, details={"status": "denied", "errorType": "PluginDisabled"})
        if not role_id.strip():
            return AgentToolResult("当前会话缺少角色身份，无法操作浏览器", is_error=True, details={"status": "failed"})
        if role_id in self._disabled_roles:
            return AgentToolResult("Browser Use 角色授权已关闭", is_error=True, details={"status": "denied", "errorType": "PluginDisabled"})
        epoch = self._epoch
        key = self._key(role_id, run_id)
        if name == "agent_browser_close":
            try:
                await self.close_run(role_id, run_id)
                return AgentToolResult("浏览器会话已关闭，站点状态已保留。", details={"status": "succeeded"})
            except asyncio.CancelledError:
                raise
            except Exception as error:
                return AgentToolResult(
                    "Browser Use 会话清理失败",
                    is_error=True,
                    details={"status": "failed", "errorType": type(error).__name__},
                )
        if key in self._closing_runs:
            return AgentToolResult("Browser Use 会话正在关闭，请稍后重试", is_error=True, details={"status": "failed"})
        lock = self._locks.setdefault(key, asyncio.Lock())
        task = asyncio.current_task()
        if task is not None:
            self._active.setdefault(key, set()).add(task)
        try:
            async with lock:
                signal.raise_if_cancelled()
                if key in self._closing_runs or self._closed or not self._enabled() or epoch != self._epoch:
                    return AgentToolResult("Browser Use 插件已停用", is_error=True, details={"status": "denied", "errorType": "PluginDisabled"})
                try:
                    session = self._sessions.get(key)
                    if session is None:
                        if self._transport is None:
                            from .runtime import BrowserRuntime
                            self._runtime = self._runtime or BrowserRuntime.resolve(self._resources)
                            self.root.mkdir(parents=True, exist_ok=True)
                        if self._transport is not None:
                            session = self._transport
                        else:
                            from .session import BrowserSession
                            session = BrowserSession(self.root, role_id, self.config, self._runtime)
                        self._sessions[key] = session
                    value = await self._call_with_cancellation(session, name, arguments, role_id, run_id, signal)
                    return result_from_remote(value)
                except asyncio.TimeoutError:
                    return AgentToolResult("Browser Use 操作超时", is_error=True, details={"status": "timed_out", "errorType": "TimeoutError"})
                except McpToolError as error:
                    safe_error, _ = sanitize_browser_text(str(error))
                    return AgentToolResult("Browser Use 网页操作失败", is_error=True, details={"status": "failed", "errorType": "McpToolError", "message": safe_error[:500]})
                except BrowserRuntimeUnavailable as error:
                    return AgentToolResult(
                        "Browser Use 原生运行组件不可用",
                        is_error=True,
                        details={"status": "unavailable", "errorType": type(error).__name__},
                    )
                except RuntimeError as error:
                    if signal.is_cancelled():
                        raise
                    return AgentToolResult(
                        "Browser Use 操作失败",
                        is_error=True,
                        details={"status": "failed", "errorType": type(error).__name__},
                    )
                except Exception as error:
                    return AgentToolResult("Browser Use 操作失败", is_error=True, details={"status": "failed", "errorType": type(error).__name__})
                finally:
                    if getattr(locals().get("session"), "closed", False) and self._sessions.get(key) is session:
                        self._sessions.pop(key, None)
        finally:
            if task is not None:
                active = self._active.get(key)
                if active is not None:
                    active.discard(task)
                    if not active:
                        self._active.pop(key, None)

    async def _call_with_cancellation(self, session, name, arguments, role_id, run_id, signal):
        import asyncio
        task = asyncio.create_task(
            session.call(
                name,
                arguments,
                role_id=role_id,
                run_id=run_id,
                timeout=self.timeout_seconds,
                signal=signal,
            )
        )
        try:
            while not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
                except asyncio.TimeoutError:
                    if signal.is_cancelled():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        raise RuntimeError("agent run cancelled")
            return await task
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def close(self) -> None:
        self._closed = True
        failures = []
        keys = tuple(dict.fromkeys((*self._sessions, *self._active, *self._close_tasks)))
        for role_id, run_id in keys:
            try:
                await self.close_run(role_id, run_id)
            except Exception as error:
                failures.append(error)
        if failures:
            raise RuntimeError(f"Browser Use 会话清理失败（{len(failures)} 个角色）") from failures[0]

    async def close_role(self, role_id: str) -> None:
        keys = tuple(dict.fromkeys(
            key for key in (*self._sessions, *self._active, *self._close_tasks)
            if key[0] == role_id
        ))
        for key in keys:
            await self.close_run(*key)

    async def reconfigure(self, config: BrowserUseConfig) -> None:
        """Caller revokes admission before joining the original owners."""
        self._epoch += 1
        keys = tuple(dict.fromkeys((*self._sessions, *self._active, *self._close_tasks)))
        for key in keys:
            await self.close_run(*key)
        self.config = config
        self.timeout_seconds = config.timeout_seconds

    async def set_role_enabled(self, role_id: str, enabled: bool) -> None:
        if enabled:
            self._disabled_roles.discard(role_id)
            return
        self._disabled_roles.add(role_id)
        await self.close_role(role_id)

    async def close_run(self, role_id: str, run_id: str = "") -> None:
        key = self._key(role_id, run_id)
        if key not in self._sessions and key not in self._active and key not in self._close_tasks:
            return
        task = self._close_tasks.get(key)
        if task is None:
            self._closing_runs.add(key)
            task = asyncio.create_task(self._close_run(key))
            self._close_tasks[key] = task
            task.add_done_callback(lambda completed: self._forget_close_task(key, completed))
        try:
            await asyncio.shield(task)
        finally:
            if task.done() and self._close_tasks.get(key) is task:
                self._close_tasks.pop(key, None)

    def _forget_close_task(self, key: tuple[str, str], task: asyncio.Task[None]) -> None:
        if self._close_tasks.get(key) is task:
            self._close_tasks.pop(key, None)

    async def _close_run(self, key: tuple[str, str]) -> None:
        self._closing_runs.add(key)
        active = tuple(self._active.get(key, ()))
        for task in active:
            task.cancel()
        try:
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            lock = self._locks.setdefault(key, asyncio.Lock())
            await lock.acquire()
            session = self._sessions.get(key)
            if session is None:
                lock.release()
                return
            try:
                close = getattr(session, "close", None)
                if close is not None:
                    result = close(graceful=True) if session is not self._transport else close()
                    if hasattr(result, "__await__"):
                        await result
                if self._sessions.get(key) is session:
                    self._sessions.pop(key, None)
            finally:
                lock.release()
        finally:
            self._closing_runs.discard(key)

    def delete_role_data(self, role_id: str) -> None:
        identity = hashlib.sha256(role_id.encode()).hexdigest()
        root = self.root.resolve()
        for parent in (root / "profiles", root / "screenshots"):
            target = (parent / identity).resolve()
            if target.parent != parent.resolve():
                raise RuntimeError("Browser Use 角色数据路径越界")
            if target.exists():
                shutil.rmtree(target)
