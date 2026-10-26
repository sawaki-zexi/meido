"""Bounded MCP 2025-06-18 stdio client with ownership of its entire process tree.

Browser Use keeps its independent, fixed binary protocol client.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.validators import validator_for  # type: ignore[import-untyped]
from referencing import Registry

from .browser_use.process import WindowsJob, spawn_process_tree
from .mcp_config import McpServer, minimal_environment


MAX_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_CATALOG_BYTES = 512 * 1024
MAX_TOOLS = 128
MAX_PAGES = 16
SCHEMA_VALIDATION_SECONDS = 2
CANCEL_NOTIFICATION_SECONDS = .05

# Fixed host code, never a server-supplied command. Isolated Python ignores cwd
# and PYTHONPATH; the empty registry never retrieves remote references.
_SCHEMA_WORKER = """
import json, sys
from jsonschema import Draft202012Validator
from jsonschema.validators import validator_for
from referencing import Registry
try:
    schema, value = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    validator_for(schema, default=Draft202012Validator)(schema, registry=Registry()).validate(value)
except Exception:
    sys.exit(1)
"""


class McpProtocolError(RuntimeError):
    """Stable host messages; never include peer errors, argv or credentials."""


def schema_validator(schema: object):
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise McpProtocolError("MCP schema must describe an object")
    pending = [(schema, 0)]
    count = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if count > 3000 or depth > 32:
            raise McpProtocolError("MCP schema exceeds complexity budget")
        if isinstance(value, dict):
            if any(key in value for key in ("$id", "$dynamicRef", "$recursiveRef")):
                raise McpProtocolError("MCP schema has unsupported reference semantics")
            reference = value.get("$ref")
            if reference is not None and (not isinstance(reference, str) or not reference.startswith("#/$defs/")):
                raise McpProtocolError("MCP schema references must be local definitions")
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    try:
        validator_class = validator_for(schema, default=Draft202012Validator)
        if "$schema" in schema and schema["$schema"] not in ("https://json-schema.org/draft/2020-12/schema", "http://json-schema.org/draft-07/schema#"):
            raise ValueError()
        validator_class.check_schema(schema)
        return validator_class(schema, registry=Registry())
    except Exception:
        raise McpProtocolError("MCP schema is invalid or unsupported") from None


async def validate_instance(schema: dict[str, Any], value: object) -> bool:
    """A hostile regex/reference graph must be killable without blocking Stop."""
    owner = None
    args = (sys.executable, "-I", "-c", _SCHEMA_WORKER)
    directory = str(Path(sys.executable).resolve().parent)
    environment = minimal_environment()
    if os.name == "nt":
        process, owner = await spawn_process_tree(*args, cwd=directory, env=environment,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    else:
        process = await asyncio.create_subprocess_exec(*args, cwd=directory, env=environment,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)

    async def dispose() -> None:
        if owner is not None:
            await asyncio.to_thread(owner.close)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
            except ProcessLookupError:
                pass
        await process.wait()

    try:
        payload = json.dumps([schema, value], ensure_ascii=False, allow_nan=False).encode("utf-8")
        try:
            await asyncio.wait_for(process.communicate(payload), SCHEMA_VALIDATION_SECONDS)
        except asyncio.TimeoutError:
            raise McpProtocolError("MCP schema validation exceeded time budget") from None
        return process.returncode == 0
    finally:
        await finish_cleanup(asyncio.create_task(dispose()))


async def finish_cleanup(task: asyncio.Task) -> None:
    """Repeated Stop/disconnect must not interrupt owned resource disposal."""
    interrupted = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            interrupted = error
    task.result()
    if interrupted is not None:
        raise interrupted


class McpStdioClient:
    def __init__(self, config: McpServer, environment: Mapping[str, str], *, job_name: str | None = None) -> None:
        self.config = config
        self._environment = dict(environment)
        self._job_name = job_name
        self._cleanup_complete = False
        self._process: asyncio.subprocess.Process | None = None
        self._owner: WindowsJob | None = None
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        self._closing: asyncio.Task | None = None
        self._spawning: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._failure: McpProtocolError | None = None
        self.closed = False

    @property
    def process_id(self) -> int | None:
        """Identity of this client's owned child, never a peer-supplied PID."""
        return self._process.pid if self._process is not None else None

    @property
    def failure(self) -> str | None:
        return str(self._failure) if self._failure is not None else None

    @property
    def exit_confirmed(self) -> bool:
        """Closed dispatch alone is not evidence that owned resources exited."""
        return self._cleanup_complete

    async def connect(self) -> tuple[dict[str, Any], ...]:
        try:
            if self._spawning is not None or self.closed:
                raise McpProtocolError("MCP connection cannot be reused")
            # Spawn may return after Stop. Keep its task owned and shielded so
            # close can join it before claiming there were no child resources.
            self._spawning = asyncio.create_task(self._spawn())
            await asyncio.shield(self._spawning)
            async with asyncio.timeout(self.config.timeout_seconds):
                response = await self._request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "Meido", "version": "1"}})
                if response.get("protocolVersion") != "2025-06-18" or not isinstance(response.get("capabilities", {}).get("tools"), dict):
                    raise McpProtocolError("MCP server did not negotiate the supported tools protocol")
                await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                tools: list[dict[str, Any]] = []
                names: set[str] = set()
                cursors: set[str] = set()
                params: dict[str, object] = {}
                size = 0
                for _ in range(MAX_PAGES):
                    page = await self._request("tools/list", params)
                    items = page.get("tools")
                    if not isinstance(items, list):
                        raise McpProtocolError("MCP tools/list response is invalid")
                    size += len(json.dumps(items).encode())
                    if size > MAX_CATALOG_BYTES or len(tools) + len(items) > MAX_TOOLS:
                        raise McpProtocolError("MCP tool directory exceeds budget")
                    for item in items:
                        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                            raise McpProtocolError("MCP tool declaration is invalid")
                        name = item["name"]
                        if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", name) or len(f"mcp_{self.config.server_id}__{name}") > 64 or name in names:
                            raise McpProtocolError("MCP tool name is invalid or duplicated")
                        if not isinstance(item.get("description", ""), str) or len(item.get("description", "")) > 8000:
                            raise McpProtocolError("MCP tool description exceeds budget")
                        schema_validator(item.get("inputSchema"))
                        if "outputSchema" in item:
                            schema_validator(item["outputSchema"])
                        names.add(name)
                        tools.append(item)
                    cursor = page.get("nextCursor")
                    if cursor is None:
                        return tuple(tools)
                    if not isinstance(cursor, str) or not cursor or len(cursor) > 2048 or cursor in cursors:
                        raise McpProtocolError("MCP pagination cursor is invalid or repeated")
                    cursors.add(cursor)
                    params = {"cursor": cursor}
                raise McpProtocolError("MCP pagination exceeds budget")
        except BaseException as error:
            await self.close()
            if isinstance(error, asyncio.CancelledError):
                raise
            if isinstance(error, McpProtocolError):
                raise
            raise McpProtocolError("MCP connection or discovery failed") from None

    async def _spawn(self) -> None:
        if os.name == "nt":
            self._process, self._owner = await spawn_process_tree(*self.config.command, cwd=self.config.cwd,
                env=self._environment, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                job_name=self._job_name)
        else:
            self._process = await asyncio.create_subprocess_exec(*self.config.command, cwd=self.config.cwd,
                env=self._environment, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        self._reader = asyncio.create_task(self._read())
        self._stderr = asyncio.create_task(self._drain_stderr())

    async def call(self, name: str, arguments: Mapping[str, object]) -> dict[str, Any]:
        try:
            return await self._request("tools/call", {"name": name, "arguments": dict(arguments)})
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as error:
            if isinstance(error, McpProtocolError):
                # Authority polling can observe closed while disposal is still
                # pending. Preserve the host's precise error before that await.
                self._failure = error
            await self.close()
            raise

    async def _send(self, message: dict[str, object]) -> None:
        if self._failure:
            raise self._failure
        if self.closed or self._process is None or self._process.stdin is None:
            raise McpProtocolError("MCP connection is closed")
        data = json.dumps(message, ensure_ascii=False, allow_nan=False).encode() + b"\n"
        if len(data) > MAX_MESSAGE_BYTES:
            raise McpProtocolError("MCP request exceeds budget")
        try:
            self._process.stdin.write(data)
            await self._process.stdin.drain()
        except (OSError, ConnectionError):
            raise McpProtocolError("MCP connection was lost") from None

    async def _request(self, method: str, params: dict[str, object]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            # A peer can stop reading stdin. The request deadline must include
            # transport backpressure, not begin only after drain has completed.
            async with asyncio.timeout(self.config.timeout_seconds):
                await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
                return await future
        except (asyncio.CancelledError, asyncio.TimeoutError):
            # MCP forbids cancelling initialize; process ownership still bounds it.
            if method != "initialize":
                try:
                    async with asyncio.timeout(CANCEL_NOTIFICATION_SECONDS):
                        await self._send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": request_id, "reason": "host cancelled"}})
                except (Exception, asyncio.CancelledError):
                    # Best effort only. Repeated Stop must reach close even if
                    # another cancellation interrupts this notification.
                    pass
            raise
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # The reader may fail a response while this request is still
                # blocked in drain; consume that otherwise unobserved failure.
                future.exception()

    async def _read(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        buffer = bytearray()
        unsolicited = 0
        try:
            while chunk := await self._process.stdout.read(65536):
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, rest = buffer.partition(b"\n")
                    buffer = bytearray(rest)
                    if len(line) > MAX_MESSAGE_BYTES:
                        raise ValueError()
                    message = json.loads(line.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                        raise ValueError()
                    request_id = message.get("id")
                    if "method" in message:
                        unsolicited += 1
                        if unsolicited > 64:
                            raise ValueError()
                        if request_id is not None:
                            await self._send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "client capability unavailable"}})
                        continue
                    pending = self._pending.get(request_id) if isinstance(request_id, int) and not isinstance(request_id, bool) else None
                    if pending is None or pending.done():
                        unsolicited += 1
                        if unsolicited > 64:
                            raise ValueError()
                        continue
                    if "error" in message:
                        pending.set_exception(McpProtocolError("MCP peer rejected the request"))
                    elif isinstance(message.get("result"), dict):
                        pending.set_result(message["result"])
                    else:
                        raise ValueError()
                if len(buffer) > MAX_MESSAGE_BYTES:
                    raise ValueError()
            raise McpProtocolError("MCP process exited or closed stdout")
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._failure is None:
                self._failure = McpProtocolError("MCP protocol stream is invalid or closed")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(self._failure)
            # A peer may close stdout but leave itself or descendants alive.
            # Disposal runs separately so it never waits for its own reader.
            if self._closing is None:
                self._closing = asyncio.create_task(self._close())

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        # Drain without retaining arbitrary peer logs or credentials.
        while await self._process.stderr.read(8192):
            pass

    async def _discard_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        # A rejected oversized line can leave StreamReader's pipe paused.
        # Keep draining without parsing/retaining while the owned peer exits.
        while await self._process.stdout.read(65536):
            pass

    async def _wait_owned_group_exit(self, group_id: int) -> None:
        # killpg queues signals; reaping the leader alone does not prove that
        # descendants have exited. Do not count a zombie as disposed either.
        while True:
            try:
                os.killpg(group_id, 0)  # type: ignore[attr-defined]
            except ProcessLookupError:
                return
            await asyncio.sleep(.005)

    async def close(self) -> None:
        self.closed = True
        if self._closing is None or self._closing.done() and not self._cleanup_complete:
            self._closing = asyncio.create_task(self._close())
        await finish_cleanup(self._closing)

    async def _close(self) -> None:
        self.closed = True
        if self._spawning is not None:
            # Failure to spawn creates no process, but its outcome must settle.
            await asyncio.gather(self._spawning, return_exceptions=True)
        process = self._process
        try:
            if process is not None:
                if self._reader is not None:
                    self._reader.cancel()
                    await asyncio.gather(self._reader, return_exceptions=True)
                self._reader = asyncio.create_task(self._discard_stdout())
                if process.stdin:
                    process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), .2)
                except asyncio.TimeoutError:
                    pass
                if self._owner:
                    await asyncio.to_thread(self._owner.close)
                elif os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
                    except ProcessLookupError:
                        pass
                elif process.returncode is None:
                    process.kill()
                async with asyncio.timeout(5):
                    await asyncio.gather(process.wait(), *(task for task in (self._reader, self._stderr) if task))
                    if os.name != "nt":
                        await self._wait_owned_group_exit(process.pid)
        finally:
            for task in (self._reader, self._stderr):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (self._reader, self._stderr) if task), return_exceptions=True)
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(McpProtocolError("MCP connection was closed"))
        self._cleanup_complete = True
