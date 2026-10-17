"""Preauthorized MCP grants adapted to ordinary AgentTools and capability revisions."""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Mapping
import hashlib
import json
import threading
from typing import Any

from .agent_runtime.capabilities import CapabilityResolution
from .agent_runtime.discovery import ServiceGrant
from .agent_runtime.tools import AgentToolResult, ToolContext, ToolDefinition, ToolRegistry
from .agent_runtime.types import ImageContent
from .mcp_config import McpConfigStore, McpServer, MCP_MANAGEMENT_NAMES
from .mcp_stdio import McpProtocolError, McpStdioClient, finish_cleanup, schema_validator, validate_instance


def management_definitions() -> tuple[ToolDefinition, ...]:
    identity = {"type": "object", "properties": {"server_id": {"type": "string", "maxLength": 24}}, "required": ["server_id"], "additionalProperties": False}
    return tuple(ToolDefinition(name, description, schema, risk=risk, source="builtin:mcp", exposure="deferred", timeout_seconds=120, output_limit=16000) for name, description, schema, risk in (
        ("mcp_add", "连接主人已预授权的 MCP 服务，仅传 server_id；发现的工具需再用 tool_search 加载。", identity, "external"),
        ("mcp_list", "查看本来源已授权 MCP 服务、连接意愿、健康状态和工具目录，不启动服务。", {"type": "object", "properties": {}, "additionalProperties": False}, "read_only"),
        ("mcp_remove", "断开本来源 MCP 服务并清除持久连接意愿，取消在途调用，撤销动态工具。", identity, "mutating"),
    ))


class McpManagementTool:
    def __init__(self, definition: ToolDefinition, bundle: McpRun | None = None) -> None:
        self.definition = definition
        self.bundle = bundle

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        ToolRegistry.validate_definition(self.definition, arguments)
        if self.bundle is None:
            raise PermissionError("MCP management requires an authorized run")
        self.bundle.require_context(context, self.definition.name)
        data: dict[str, object]
        if self.definition.name == "mcp_list":
            data = {"servers": self.bundle.list_services()}
        else:
            server_id = str(arguments["server_id"])
            if self.definition.name == "mcp_add":
                try:
                    data = await self.bundle.add(server_id, context)
                except McpProtocolError as error:
                    return AgentToolResult(str(error), details={"serverId": server_id}, is_error=True)
            else:
                data = await self.bundle.remove(server_id, context)
        return AgentToolResult(json.dumps(data, ensure_ascii=False), details=data)


class McpRemoteTool:
    def __init__(self, bundle: McpRun, config: McpServer, declaration: dict[str, Any], secrets: tuple[str, ...]) -> None:
        self.bundle = bundle
        self.config = config
        self.remote_name = declaration["name"]
        self._input = schema_validator(declaration["inputSchema"])
        self._output = schema_validator(declaration["outputSchema"]) if "outputSchema" in declaration else None
        self._secrets = secrets
        encoded = json.dumps(declaration, ensure_ascii=False, sort_keys=True)
        if _redact_value(declaration, secrets) != declaration:
            raise McpProtocolError("MCP tool declaration contains protected configuration")
        version = hashlib.sha256((config.revision + encoded).encode()).hexdigest()[:24]
        self.definition = ToolDefinition(f"mcp_{config.server_id}__{self.remote_name}", declaration.get("description") or self.remote_name,
            declaration["inputSchema"], risk=config.risk, source=f"mcp:{config.server_id}", version=version,
            exposure="deferred", timeout_seconds=config.timeout_seconds, output_limit=config.max_output_chars)

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        self.bundle.require_context(context)
        denial = self.bundle.authorization_denial(self.definition)
        if denial is not None:
            raise PermissionError(denial)
        if len(json.dumps(dict(arguments), ensure_ascii=False)) > 65536:
            raise ValueError("MCP arguments exceed budget")
        # Full JSON Schema validation is offline; no peer-selected URL is fetched.
        try:
            valid_input = await validate_instance(self._input.schema, dict(arguments))
        except McpProtocolError as error:
            return AgentToolResult(str(error), is_error=True)
        if not valid_input:
            return AgentToolResult("MCP arguments do not match the frozen input schema", is_error=True)
        client = self.bundle.clients.get(self.config.server_id)
        if client is None or client.closed:
            raise McpProtocolError("MCP service is disconnected; use mcp_add")
        try:
            response = await client.call(self.remote_name, arguments)
            context.signal.raise_if_cancelled()
            self.bundle.require_context(context)
            denial = self.bundle.authorization_denial(self.definition)
            if denial is not None:
                raise PermissionError(denial)
            return await self._result(response)
        except BaseException as error:
            self.bundle.errors[self.config.server_id] = str(error) if isinstance(error, McpProtocolError) else "MCP call failed or was cancelled; reconnect before another call"
            await client.close()
            if isinstance(error, McpProtocolError):
                return AgentToolResult(str(error), is_error=True)
            raise

    async def _result(self, response: dict[str, Any]) -> AgentToolResult:
        content = response.get("content")
        if not isinstance(content, list) or len(content) > 64 or not isinstance(response.get("isError", False), bool):
            raise McpProtocolError("MCP tool result is invalid")
        structured = response.get("structuredContent")
        if structured is not None and not isinstance(structured, dict):
            raise McpProtocolError("MCP structured result must be an object")
        if self._output is not None:
            if not await validate_instance(self._output.schema, structured):
                raise McpProtocolError("MCP structured result does not match output schema")
        texts = []
        images: list[ImageContent] = []
        for block in content:
            if not isinstance(block, dict):
                raise McpProtocolError("MCP content block is invalid")
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif kind == "image" and block.get("mimeType") in {"image/png", "image/jpeg", "image/webp", "image/gif"} and isinstance(block.get("data"), str):
                data = block["data"]
                try:
                    raw = base64.b64decode(data, validate=True)
                except ValueError:
                    raise McpProtocolError("MCP image encoding is invalid") from None
                if len(images) >= 4 or len(raw) > 1024 * 1024:
                    raise McpProtocolError("MCP images exceed budget")
                if any(secret.encode() in raw for secret in self._secrets):
                    raise McpProtocolError("MCP image contains protected configuration")
                images.append(ImageContent(block["mimeType"], data))
            elif kind in {"resource_link", "resource", "audio"}:
                # References stay data; the host never dereferences remote URLs.
                texts.append(json.dumps(_redact_value(block, self._secrets), ensure_ascii=False))
            else:
                raise McpProtocolError("MCP content type is unsupported or malformed")
        if structured is not None:
            texts.append(json.dumps({"structuredContent": structured}, ensure_ascii=False))
        safe_structured = _redact_value(structured, self._secrets)
        # Structured values are cleaned before encoding, including escaped keys
        # and nested values. Replacing the encoded JSON would miss credentials
        # containing quotes, backslashes or control characters.
        if structured is not None:
            texts[-1] = json.dumps({"structuredContent": safe_structured}, ensure_ascii=False)
        text = _redact_value("\n".join(texts), self._secrets)
        limit = self.config.max_output_chars
        truncated = len(text) > limit
        return AgentToolResult(text[:limit], details={"serverId": self.config.server_id, "remoteTool": self.remote_name,
            "structuredContent": safe_structured if structured is not None and len(json.dumps(safe_structured)) <= limit else None,
            "truncated": truncated}, is_error=response.get("isError", False), images=tuple(images))


def _redact_value(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            for representation in (secret, json.dumps(secret, ensure_ascii=False)[1:-1], json.dumps(secret, ensure_ascii=True)[1:-1]):
                value = value.replace(representation, "[redacted]")
        return value
    if isinstance(value, dict):
        return {_redact_value(key, secrets): _redact_value(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, secrets) for item in value]
    return value


class McpHost:
    def __init__(self, store: McpConfigStore) -> None:
        self.store = store
        self._runs: dict[str, McpRun] = {}
        self._autonomy_runs: dict[str, Any] = {}
        self._observed_catalogs: dict[str, tuple[str, str, Any]] = {}
        self._autonomy_role_gates: dict[str, threading.BoundedSemaphore] = {}
        self._autonomy_server_gates: dict[tuple[str, str], threading.Lock] = {}
        self._lock = threading.RLock()

    def observe_catalog(self, config, declarations):
        """Cache bounded local metadata after an actual authorized handshake."""
        from copy import deepcopy
        if len(declarations) > 128 or len(json.dumps(declarations).encode('utf-8')) > 256 * 1024:
            return
        with self._lock:
            if self.store.get(config.server_id) != config:
                return
            if config.server_id not in self._observed_catalogs and len(self._observed_catalogs) >= 32:
                self._observed_catalogs.pop(next(iter(self._observed_catalogs)))
            self._observed_catalogs[config.server_id] = (config.revision, config.grant_id, deepcopy(declarations))

    def observed_catalog(self, role_id, server_id):
        from copy import deepcopy
        with self._lock:
            config = self.store.get(server_id)
            previous = self._observed_catalogs.get(server_id)
            if config is None or not config.enabled or role_id not in config.role_ids:
                return 'unobserved', ()
            if previous is None:
                return 'unobserved', ()
            if previous[:2] != (config.revision, config.grant_id):
                return 'stale', ()
            return 'observed', tuple(deepcopy(previous[2]))

    def freeze_autonomy(self, origin, grants, *, authorized, mount_grants=()):
        """The proactive host supplies its persisted origin and exact operations."""
        from .mcp_autonomy import AutonomyMcpRun
        all_grants = (*grants, *mount_grants)
        if (not authorized() or not all_grants or len(grants) > 8 or len(mount_grants) > 8
                or len({grant.server_id for grant in all_grants}) > 8):
            raise PermissionError("a live autonomous host grant is required")
        with self._lock:
            if origin.tick_id in self._autonomy_runs:
                raise RuntimeError("autonomous MCP run already owns a bundle")
            if any(run.role_id == origin.role_id and not run.exit_confirmed
                   and (run.closed or run.cleanup_failed) for run in self._autonomy_runs.values()):
                raise RuntimeError("prior autonomous MCP cleanup is pending")
            role_gate = self._autonomy_role_gates.setdefault(origin.role_id, threading.BoundedSemaphore(2))
            server_gates = {grant.server_id: self._autonomy_server_gates.setdefault(
                (origin.role_id, grant.server_id), threading.Lock()) for grant in all_grants}
            run = AutonomyMcpRun(self, origin, grants, authorized, role_gate, server_gates, mount_grants=mount_grants)
            self._autonomy_runs[origin.tick_id] = run
            return run

    def autonomy_cleanup_pending(self, role_id: str) -> bool:
        with self._lock:
            return any(run.role_id == role_id and not run.exit_confirmed
                       and (run.closed or run.cleanup_failed) for run in self._autonomy_runs.values())

    async def close_autonomy_role(self, role_id: str) -> None:
        """Retry only this role's autonomous owners; ordinary intent is intact."""
        with self._lock:
            runs = tuple(run for run in self._autonomy_runs.values() if run.role_id == role_id)
        await self._join_cleanup([run.close_from_any_loop() for run in runs])

    async def close_autonomy_run(self, tick_id: str) -> None:
        """Keep one executor loop alive until its precise peer is reaped."""
        with self._lock:
            run = self._autonomy_runs.get(tick_id)
        if run is not None:
            await run.close_from_any_loop()

    def freeze(self, *, role_id: str, session_key: str, run_id: str, source: dict[str, object] | None,
               source_denial: Callable[[], str | None], tool_enabled: Callable[[str], bool], owner: bool) -> McpRun:
        scope = dict(source) if source is not None else {"role_id": role_id, "session_key": session_key, "kind": "desktop"}
        scope.pop("message_created_at", None)
        scope_id = hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        configs = tuple(config for config in self.store.all() if owner and config.enabled and role_id in config.role_ids)
        if session_key.startswith("thread:") and (source is None or source.get("owner_context") is not True):
            owner = False
            configs = ()
        bundle = McpRun(self, role_id, session_key, run_id, scope_id, configs, source_denial, tool_enabled, owner)
        with self._lock:
            if run_id in self._runs:
                raise RuntimeError("MCP run already owns a bundle")
            self._runs[run_id] = bundle
        return bundle

    def public(self) -> list[dict[str, object]]:
        with self._lock:
            runs = (*self._runs.values(), *self._autonomy_runs.values())
        with self.store._connection() as db:
            counts = dict(db.execute("SELECT server_id, COUNT(*) FROM mcp_intents GROUP BY server_id"))
        result = []
        for config in self.store.all():
            errors = set()
            if self.store.cleanup_pending(config.server_id):
                errors.add("MCP credential cleanup is pending; retry saving or deleting")
            active = 0
            for run in runs:
                client = run.clients.get(config.server_id)
                error = run.errors.get(config.server_id) or (client.failure if client is not None else None)
                if error is not None:
                    errors.add(error)
                active += int(client is not None and not client.closed and client.failure is None)
            result.append({**config.public(), "connectionCount": counts.get(config.server_id, 0), "activeProcesses": active, "errors": sorted(errors)})
        return result

    async def revoke(self, server_id: str) -> None:
        with self._lock:
            runs = (*self._runs.values(), *self._autonomy_runs.values())
        await self._join_cleanup([run.close_server_from_any_loop(server_id)
                                  for run in runs if server_id in run.configs])

    async def delete_role(self, role_id: str) -> None:
        self.store.delete_role_intents(role_id)
        with self._lock:
            runs = tuple(run for run in (*self._runs.values(), *self._autonomy_runs.values()) if run.role_id == role_id)
        await self._join_cleanup([run.close_server_from_any_loop(server_id)
                                  for run in runs for server_id in run.configs])

    async def close(self) -> None:
        with self._lock:
            runs = tuple(self._runs.values())
            autonomous = tuple(self._autonomy_runs.values())
        await self._join_cleanup([
            *(run.close_server_from_any_loop(server_id) for run in runs for server_id in run.configs),
            *(run.close_from_any_loop() for run in autonomous),
        ])

    @staticmethod
    async def _join_cleanup(actions):
        async def join():
            results = await asyncio.gather(*actions, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        # A failed peer must not prevent other owned peers from being reaped.
        await finish_cleanup(asyncio.create_task(join()))


class McpRun:
    def __init__(self, host: McpHost, role_id: str, session_key: str, run_id: str, scope_id: str,
                 configs: tuple[McpServer, ...], source_denial: Callable[[], str | None], tool_enabled: Callable[[str], bool], owner: bool) -> None:
        self.host, self.role_id, self.session_key, self.run_id, self.scope_id = host, role_id, session_key, run_id, scope_id
        self.configs = {item.server_id: item for item in configs}
        self.source_denial = source_denial
        self._source_revocation: str | None = None
        self.tool_enabled = tool_enabled
        self.owner = owner
        self.clients: dict[str, McpStdioClient] = {}
        self.remote_tools: dict[str, tuple[McpRemoteTool, ...]] = {}
        self.errors: dict[str, str] = {}
        self.disconnected: set[str] = set()
        self.capabilities: CapabilityResolution | None = None
        self.loop = asyncio.get_running_loop()
        self._closing: asyncio.Task | None = None
        self.closed = False
        self._watchdog = asyncio.create_task(self._watch_authority())

    @property
    def grants(self) -> tuple[ServiceGrant, ...]:
        return tuple(ServiceGrant(item.grant_id, item.server_id, self.role_id, frozenset({item.risk})) for item in self.configs.values())

    @property
    def tools(self) -> tuple[McpManagementTool | McpRemoteTool, ...]:
        return (*(McpManagementTool(item, self) for item in management_definitions()), *(tool for items in self.remote_tools.values() for tool in items))

    def bind(self, capabilities: CapabilityResolution) -> None:
        if self.capabilities is not None:
            raise RuntimeError("MCP run capability directory already bound")
        self.capabilities = capabilities

    def _source_authorization_denial(self) -> str | None:
        # Once this run observes a revoked origin, resuming the platform does
        # not revive its old capability directory. A fresh inbound run can
        # independently recheck the current binding and saved connection intent.
        if self._source_revocation is None:
            self._source_revocation = self.source_denial()
        return self._source_revocation

    def require_context(self, context: ToolContext, management_name: str | None = None) -> None:
        context.signal.raise_if_cancelled()
        if not self.owner or (context.role_id, context.session_key, context.run_id) != (self.role_id, self.session_key, self.run_id) or self.closed:
            raise PermissionError("MCP tool context does not match its frozen run")
        denial = self._source_authorization_denial()
        if denial is not None:
            raise PermissionError(denial)
        if management_name is not None and not self.tool_enabled(management_name):
            raise PermissionError("MCP management permission was revoked")

    def authorization_denial(self, definition: ToolDefinition) -> str | None:
        if not self.owner:
            return "MCP is not allowed for external context"
        denial = self._source_authorization_denial()
        if denial is not None:
            return denial
        if self.closed:
            return "MCP run is closed"
        if definition.source == "builtin:mcp":
            return None if self.tool_enabled(definition.name) else "MCP management permission was revoked"
        server_id = definition.source.removeprefix("mcp:")
        config = self.configs.get(server_id)
        if config is None or self.host.store.get(server_id) != config:
            return "MCP service grant was revoked or changed"
        if server_id in self.disconnected:
            return "MCP service connection was removed"
        client = self.clients.get(server_id)
        if client is None or client.closed:
            return self.errors.get(server_id) or (client.failure if client is not None else None) or "MCP service is disconnected; use mcp_add"
        return None

    async def prepare(self) -> None:
        try:
            for config in self.configs.values():
                if self.host.store.has_intent(self.scope_id, config):
                    try:
                        await self._connect(config)
                    except McpProtocolError as error:
                        self.errors[config.server_id] = str(error)
        except BaseException:
            await self.close()
            raise

    async def _watch_authority(self) -> None:
        while not self.closed:
            await asyncio.sleep(.05)
            source_denied = self._source_authorization_denial() is not None
            for server_id, config in self.configs.items():
                if source_denied or self.host.store.get(server_id) != config:
                    self.errors[server_id] = "MCP grant or original source was revoked"
                    await self.close_server(server_id)

    async def _connect(self, config: McpServer) -> tuple[McpRemoteTool, ...]:
        if self._source_authorization_denial() is not None or self.host.store.get(config.server_id) != config:
            raise PermissionError("MCP grant or original source was revoked")
        previous = self.clients.get(config.server_id)
        if previous is not None:
            await previous.close()
        try:
            environment = self.host.store.environment(config)
        except Exception:
            raise McpProtocolError("MCP environment credential is unavailable") from None
        secrets = tuple(environment[key] for key in config.env_keys)
        client = McpStdioClient(config, environment)
        self.clients[config.server_id] = client
        try:
            declarations = await client.connect()
            tools = tuple(McpRemoteTool(self, config, declaration, secrets) for declaration in declarations)
            if self._source_authorization_denial() is not None or self.host.store.get(config.server_id) != config:
                raise PermissionError("MCP grant or original source was revoked")
            self.remote_tools[config.server_id] = tools
            self.host.observe_catalog(config, declarations)
            self.disconnected.discard(config.server_id)
            self.errors.pop(config.server_id, None)
            return tools
        except BaseException:
            await client.close()
            raise

    async def add(self, server_id: str, context: ToolContext) -> dict[str, object]:
        config = self.configs.get(server_id)
        if config is None or self.host.store.get(server_id) != config or self.capabilities is None:
            raise PermissionError("MCP server was not authorized at run start or was revoked")
        existing = self.clients.get(server_id)
        if existing is not None and not existing.closed and server_id not in self.disconnected:
            return {"serverId": server_id, "status": "connected", "tools": [item.definition.name for item in self.remote_tools.get(server_id, ())]}
        try:
            tools = await self._connect(config)
            self.require_context(context, "mcp_add")
            # A reconnect can restore the exact already-admitted directory; it
            # cannot silently change schemas inside an issued run snapshot.
            current = self.capabilities.current()
            admitted = {item.name: item for item in current.snapshot.candidates if item.source == f"mcp:{server_id}"}
            if admitted:
                # The version hashes the complete declaration and immutable
                # owner configuration; frozen schemas normalize arrays to tuples.
                if {item.definition.name: item.definition.version for item in tools} != {name: item.version for name, item in admitted.items()}:
                    raise McpProtocolError("MCP tool directory changed; start a new run")
            elif tools:
                current.stage_service_tools(context, grant_id=config.grant_id, parent_snapshot_id=current.snapshot.snapshot_id, tools=tools)
            self.host.store.set_intent(self.scope_id, self.role_id, self.session_key, config)
            return {"serverId": server_id, "status": "connected", "tools": [item.definition.name for item in tools], "tip": "结果持久化后用 tool_search 加载远端工具。"}
        except BaseException as error:
            self.errors[server_id] = str(error) if isinstance(error, McpProtocolError) else "MCP connection failed or was revoked"
            await self.close_server(server_id)
            raise

    async def remove(self, server_id: str, context: ToolContext) -> dict[str, object]:
        config = self.configs.get(server_id)
        if config is None or self.capabilities is None:
            raise PermissionError("MCP server was not authorized at run start")
        self.host.store.remove_intent(self.scope_id, server_id)
        await self.close_server(server_id)
        current = self.capabilities.current()
        current.stage_service_removal(context, grant_id=config.grant_id, parent_snapshot_id=current.snapshot.snapshot_id)
        return {"serverId": server_id, "status": "disconnected"}

    def list_services(self) -> list[dict[str, object]]:
        visible = {item.name for item in self.capabilities.current().snapshot.tools} if self.capabilities else set()
        failures = {server_id: client.failure for server_id, client in self.clients.items() if client.failure is not None}
        errors = {**failures, **self.errors}
        return [{"serverId": item.server_id, "name": item.name, "risk": item.risk,
                 "connectionDesired": self.host.store.has_intent(self.scope_id, item),
                 "status": "revoked" if self.host.store.get(item.server_id) != item else "connected" if item.server_id in self.clients and not self.clients[item.server_id].closed and item.server_id not in errors else "failed" if item.server_id in errors else "disconnected",
                 "error": errors.get(item.server_id),
                 "tools": [{"name": tool.definition.name, "loaded": tool.definition.name in visible} for tool in self.remote_tools.get(item.server_id, ())]} for item in self.configs.values()]

    async def close_server_from_any_loop(self, server_id: str) -> None:
        if self.loop is asyncio.get_running_loop():
            await self.close_server(server_id)
        elif not self.loop.is_closed():
            future = asyncio.run_coroutine_threadsafe(self.close_server(server_id), self.loop)
            await asyncio.wrap_future(future)

    async def close_server(self, server_id: str) -> None:
        self.disconnected.add(server_id)
        client = self.clients.get(server_id)
        if client is not None:
            await client.close()

    async def close(self) -> None:
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        await finish_cleanup(self._closing)

    async def _close(self) -> None:
        self.closed = True
        try:
            self._watchdog.cancel()
            await asyncio.gather(self._watchdog, return_exceptions=True)
            await asyncio.gather(*(client.close() for client in self.clients.values()))
        finally:
            with self.host._lock:
                self.host._runs.pop(self.run_id, None)
