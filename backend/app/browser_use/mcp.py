from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping

from .process import WindowsJob, release_process_tree_owner, spawn_process_tree, terminate_process_tree


class McpToolError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class McpStdioSession:
    """Small MCP stdio client for the pinned agent-browser binary."""

    def __init__(self, command: list[str], *, cwd: str, env: dict[str, str]) -> None:
        self.command = command
        self.cwd = cwd
        self.env = env
        self.process: asyncio.subprocess.Process | None = None
        self._owner: WindowsJob | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[object]] = {}
        self._pending_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        self.process, self._owner = await spawn_process_tree(
            *self.command,
            cwd=self.cwd,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._reader_task = asyncio.create_task(self._read_responses())
        try:
            await asyncio.wait_for(self._request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "meido", "version": "0.1"},
            }), timeout=8)
            await asyncio.wait_for(self._notify("notifications/initialized", {}), timeout=2)
        except BaseException:
            await self.close()
            raise

    async def call(self, name: str, arguments: dict[str, object], *, timeout: float) -> object:
        response = await asyncio.wait_for(self._request("tools/call", {"name": name, "arguments": arguments}), timeout=timeout)
        if not isinstance(response, Mapping):
            return response
        if response.get("isError"):
            raise McpToolError(str(response.get("error") or "浏览器工具返回错误"))
        content = response.get("content")
        if not isinstance(content, list):
            return response.get("structuredContent", response)
        texts = [item.get("text", "") for item in content if isinstance(item, Mapping) and item.get("type") == "text"]
        images = [item for item in content if isinstance(item, Mapping) and item.get("type") == "image"]
        result: dict[str, object] = {"text": "\n".join(str(item) for item in texts if item)}
        if images:
            image = images[0]
            result.update({"image": image.get("data"), "mimeType": image.get("mimeType", "image/png")})
        return result

    async def _request(self, method: str, params: Mapping[str, object]) -> object:
        async with self._pending_lock:
            self._next_id += 1
            request_id = self._next_id
            future = asyncio.get_running_loop().create_future()
            self._pending[request_id] = future
        try:
            await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})
            return await future
        finally:
            async with self._pending_lock:
                self._pending.pop(request_id, None)

    async def _notify(self, method: str, params: Mapping[str, object]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    async def _write(self, payload: Mapping[str, object]) -> None:
        async with self._write_lock:
            if self.process is None or self.process.stdin is None:
                raise RuntimeError("Browser Use MCP 连接未建立")
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.process.stdin.write(raw + b"\n")
            await self.process.stdin.drain()

    async def _read(self) -> object:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("Browser Use MCP 连接未建立")
        line = await self.process.stdout.readline()
        if not line:
            raise RuntimeError("Browser Use MCP 进程已退出")
        return json.loads(line)

    async def _read_responses(self) -> None:
        try:
            while True:
                response = await self._read()
                if not isinstance(response, Mapping):
                    continue
                request_id = response.get("id")
                if not isinstance(request_id, int):
                    continue
                async with self._pending_lock:
                    future = self._pending.get(request_id)
                    if future is None or future.done():
                        continue
                    if "error" in response:
                        error = response["error"]
                        message = error.get("message", "MCP 请求失败") if isinstance(error, Mapping) else str(error)
                        future.set_exception(McpToolError(str(message)))
                    else:
                        future.set_result(response.get("result"))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            async with self._pending_lock:
                pending = tuple(self._pending.values())
            for future in pending:
                if not future.done():
                    future.set_exception(error)

    async def _drain_stderr(self) -> None:
        if self.process is None or self.process.stderr is None:
            return
        while await self.process.stderr.readline():
            pass

    async def close(self) -> None:
        process = self.process
        self.process = None
        owner, self._owner = self._owner, None
        async with self._pending_lock:
            pending = tuple(self._pending.values())
            for future in pending:
                if not future.done():
                    future.cancel()
            self._pending.clear()
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None
        try:
            if owner is not None:
                try:
                    await release_process_tree_owner(owner)
                except BaseException:
                    if process is not None:
                        await terminate_process_tree(process)
                    raise
            if process is not None:
                if owner is None:
                    await terminate_process_tree(process)
                elif process.returncode is None:
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        await terminate_process_tree(process)
        finally:
            if process is not None and process.returncode is None:
                await terminate_process_tree(process)

    async def disconnect(self) -> None:
        process = self.process
        if process is None:
            await self._stop_reader_tasks()
            return
        owner, self._owner = self._owner, None
        if process.stdin is not None:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            await terminate_process_tree(process)
        finally:
            self.process = None
            try:
                await release_process_tree_owner(owner)
            finally:
                await self._stop_reader_tasks()

    async def _stop_reader_tasks(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None
