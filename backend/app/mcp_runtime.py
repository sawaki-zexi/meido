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


CLEANUP_FAILED = "MCP process cleanup failed; resources remain owned; retry cleanup"
CLEANUP_UNCONFIRMED = "MCP cleanup is unconfirmed and still belongs to its original loop"


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
                    data = await self.bundle.add(server_id, context, on_update=on_update)
                except McpProtocolError as error:
                    return AgentToolResult(str(error), details={"serverId": server_id}, is_error=True)
            else:
                data = await self.bundle.remove(server_id, context)
        return AgentToolResult(json.dumps(data, ensure_ascii=False), details=data)


class McpRemoteTool:
    def __init__(self, bundle: McpRun, config: McpServer, declaration: dict[str, Any], secrets: tuple[str, ...], client: McpStdioClient) -> None:
        self.bundle = bundle
        self.config = config
        self._client = client
        self._generation = bundle._generations[config.server_id]
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
            exposure="deferred", timeout_seconds=config.timeout_seconds, output_limit=config.max_output_chars,
            namespace_summary=_redact_value(config.name, secrets))

    def _require_binding(self, context: ToolContext) -> McpStdioClient:
        self.bundle.require_context(context)
        denial = self.bundle.authorization_denial(self.definition)
        if denial is not None:
            raise PermissionError(denial)
        if (self.bundle.clients.get(self.config.server_id) is not self._client
                or self.bundle._generations.get(self.config.server_id) != self._generation):
            raise PermissionError("MCP connection changed; use the current registered tool")
        return self._client

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        self._require_binding(context)
        if len(json.dumps(dict(arguments), ensure_ascii=False)) > 65536:
            raise ValueError("MCP arguments exceed budget")
        # Full JSON Schema validation is offline; no peer-selected URL is fetched.
        try:
            valid_input = await validate_instance(self._input.schema, dict(arguments))
        except McpProtocolError as error:
            return AgentToolResult(str(error), is_error=True)
        if not valid_input:
            return AgentToolResult("MCP arguments do not match the current input schema", is_error=True)
        # Schema validation awaits a separate worker; recheck both current
        # authority and the issued request's exact peer immediately before call.
        client = self._require_binding(context)
        try:
            response = await client.call(self.remote_name, arguments)
            context.signal.raise_if_cancelled()
            self._require_binding(context)
            return await self._result(response)
        except BaseException as error:
            # A stale response belongs to its already-retired peer, not the
            # replacement. Never revoke/dispose the latter on the old call's behalf.
            if self.bundle.clients.get(self.config.server_id) is client:
                self.bundle.errors[self.config.server_id] = str(error) if isinstance(error, McpProtocolError) else "MCP call failed or was cancelled; reconnect before another call"
                await self.bundle.close_server(self.config.server_id)
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
            cleanup_owners = []
            for run in runs:
                client = run.clients.get(config.server_id)
                error = run.errors.get(config.server_id) or (client.failure if client is not None else None)
                if error is not None:
                    errors.add(error)
                active += int(client is not None and not client.closed and client.failure is None)
                if isinstance(run, McpRun):
                    diagnostic = run.cleanup_diagnostic(config.server_id)
                    if diagnostic is not None:
                        cleanup_owners.append(diagnostic)
                        errors.add(CLEANUP_UNCONFIRMED if diagnostic["state"] == "cleanup_unconfirmed" else CLEANUP_FAILED if diagnostic["state"] == "cleanup_failed" else "MCP process cleanup is pending")
            result.append({**config.public(), "connectionCount": counts.get(config.server_id, 0), "activeProcesses": active, "errors": sorted(errors), "cleanupOwners": cleanup_owners})
        return result

    def require_available(self, owner: McpRun, server_id: str) -> None:
        # Call under _lock together with publication of the new child owner.
        # Display names and new configuration revisions never identify resources.
        for run in self._runs.values():
            if run is owner or (run.role_id, run.scope_id) != (owner.role_id, owner.scope_id):
                continue
            client = run.clients.get(server_id)
            if server_id in run._opening or client is not None and not client.exit_confirmed:
                raise McpProtocolError("Prior MCP connection or cleanup is pending for this source")

    async def retry_cleanup(self, server_id: str) -> None:
        """Only reap retained ordinary owners; no grant/intent/business changes."""
        with self._lock:
            runs = tuple(run for run in self._runs.values() if run.cleanup_diagnostic(server_id) is not None)
        await self._join_cleanup([run.close_from_any_loop() if run.closed else
            run.close_server_from_any_loop(server_id) for run in runs])

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
            *(run.close_from_any_loop() for run in runs),
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
        self.direct_exposure = False
        self.loop = asyncio.get_running_loop()
        self._closing: asyncio.Task | None = None
        self._opening: dict[str, asyncio.Task] = {}
        self._server_closing: dict[str, asyncio.Task] = {}
        self._add_locks = {server_id: asyncio.Lock() for server_id in self.configs}
        self._epochs: dict[str, int] = {}
        self._generations: dict[str, int] = {}
        self._cleanup_errors: dict[str, str] = {}
        self.closed = self.exit_confirmed = self.cleanup_failed = False
        self._watchdog = asyncio.create_task(self._watch_authority())

    @property
    def grants(self) -> tuple[ServiceGrant, ...]:
        return tuple(ServiceGrant(item.grant_id, item.server_id, self.role_id, frozenset({item.risk})) for item in self.configs.values())

    @property
    def tools(self) -> tuple[McpManagementTool | McpRemoteTool, ...]:
        return (*(McpManagementTool(item, self) for item in management_definitions()), *(tool for items in self.remote_tools.values() for tool in items))

    def bind(self, capabilities: CapabilityResolution, *, direct_exposure=False) -> None:
        if self.capabilities is not None:
            raise RuntimeError("MCP run capability directory already bound")
        self.capabilities = capabilities
        self.direct_exposure = direct_exposure

    def _source_authorization_denial(self) -> str | None:
        # Once this run observes a revoked origin, resuming the platform does
        # not revive its old capability directory. A fresh inbound run can
        # independently recheck the current binding and saved connection intent.
        if self._source_revocation is None:
            try:
                self._source_revocation = self.source_denial()
            except Exception:
                self._source_revocation = "MCP source authority is unavailable"
                self._begin_close()
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
        try:
            unchanged = config is not None and self.host.store.get(server_id) == config
        except Exception:
            self._begin_close()
            return "MCP service authority is unavailable"
        if not unchanged:
            return "MCP service grant was revoked or changed"
        if server_id in self.disconnected:
            return self.errors.get(server_id) or "MCP service connection was removed"
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
        try:
            while not self.closed:
                await asyncio.sleep(.05)
                if self._source_authorization_denial() is not None:
                    self._begin_close()
                    return
                for server_id, config in self.configs.items():
                    if self.host.store.get(server_id) != config and server_id not in self.disconnected:
                        self.errors[server_id] = "MCP grant or original source was revoked"
                        self._begin_server_close(server_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Like autonomy, seal synchronously and leave cleanup to its owned
            # task. Awaiting close here would make it join its own watcher.
            self._source_revocation = "MCP service authority is unavailable"
            self._begin_close()

    def _require_connection(self, config: McpServer) -> None:
        try:
            allowed = not self.closed and self._source_authorization_denial() is None and self.host.store.get(config.server_id) == config
        except Exception:
            self._begin_close()
            allowed = False
        if not allowed:
            raise PermissionError("MCP grant or original source was revoked")

    async def _connect(self, config: McpServer, on_update=None) -> tuple[McpRemoteTool, ...]:
        self._require_connection(config)
        server_id = config.server_id
        closing = self._server_closing.get(server_id)
        if closing is not None and not closing.done():
            raise McpProtocolError("MCP process cleanup is pending; retry after cleanup")
        operation = self._opening.get(server_id)
        if operation is None:
            operation = asyncio.create_task(self._connect_owned(config, self._epochs.get(server_id, 0), on_update))
            self._opening[server_id] = operation
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            if not asyncio.current_task().cancelling():
                raise McpProtocolError("MCP connection was closed while opening") from None
            # Own the child, not this caller (whose finally can itself close).
            await self.close_server(server_id)
            raise
        finally:
            if operation.done() and self._opening.get(server_id) is operation:
                del self._opening[server_id]

    async def _connect_owned(self, config: McpServer, epoch: int, on_update=None) -> tuple[McpRemoteTool, ...]:
        server_id = config.server_id
        previous = self.clients.get(server_id)
        if previous is not None and not previous.exit_confirmed:
            await self._dispose_client(server_id, previous)
        self._require_connection(config)
        try:
            environment = self.host.store.environment(config)
        except Exception:
            raise McpProtocolError("MCP environment credential is unavailable") from None
        secrets = tuple(environment[key] for key in config.env_keys)
        with self.host._lock:
            self.host.require_available(self, server_id)
            client = McpStdioClient(config, environment)
            self.clients[server_id] = client
            self._generations[server_id] = self._generations.get(server_id, 0) + 1
        try:
            if on_update is not None:
                # Host phase only, after grant checks and when an owned attempt
                # actually starts. Never stream configuration or peer output.
                on_update(AgentToolResult("正在连接 MCP 服务并准备工具目录。", details={"phase": "connecting"}))
            declarations = await client.connect()
            tools = tuple(McpRemoteTool(self, config, declaration, secrets, client) for declaration in declarations)
            self._require_connection(config)
            if epoch != self._epochs.get(server_id, 0):
                raise McpProtocolError("MCP connection was removed while opening")
            self.host.observe_catalog(config, declarations)
            self.remote_tools[server_id] = tools
            self.disconnected.discard(server_id)
            self.errors.pop(server_id, None)
            return tools
        except BaseException:
            await self._dispose_client(server_id, client)
            raise

    async def add(self, server_id: str, context: ToolContext, on_update=None) -> dict[str, object]:
        self.require_context(context, "mcp_add")
        if server_id not in self._add_locks:
            raise PermissionError("MCP server was not authorized at run start")
        async with self._add_locks[server_id]:
            return await self._add(server_id, context, on_update)

    async def _add(self, server_id: str, context: ToolContext, on_update=None) -> dict[str, object]:
        self.require_context(context, "mcp_add")
        config = self.configs.get(server_id)
        if config is None or self.capabilities is None:
            raise PermissionError("MCP server was not authorized at run start or was revoked")
        self._require_connection(config)
        existing = self.clients.get(server_id)
        if existing is not None and not existing.closed and server_id not in self.disconnected and server_id not in self._opening:
            return {"serverId": server_id, "status": "connected", "tools": [item.definition.name for item in self.remote_tools.get(server_id, ())]}
        try:
            tools = await self._connect(config, on_update)
            self.require_context(context, "mcp_add")
            current = self.capabilities.current()
            if tools:
                current.stage_service_tools(context, grant_id=config.grant_id,
                    parent_snapshot_id=current.snapshot.snapshot_id, tools=tools,
                    rebind=True, activate=self.direct_exposure)
            else:
                current.stage_service_removal(context, grant_id=config.grant_id,
                    parent_snapshot_id=current.snapshot.snapshot_id)
            self.host.store.set_intent(self.scope_id, self.role_id, self.session_key, config)
            return {"serverId": server_id, "status": "connected", "tools": [item.definition.name for item in tools],
                    "tip": "结果持久化后可直接使用远端工具。" if self.direct_exposure else "结果持久化后用 tool_search 加载远端工具。"}
        except BaseException as error:
            self.errors[server_id] = str(error) if isinstance(error, McpProtocolError) else "MCP connection failed or was revoked"
            # An opening child already disposes itself. Do not immediately
            # turn one failed cleanup into an implicit second attempt.
            if server_id not in self._cleanup_errors:
                await self.close_server(server_id)
            raise

    async def remove(self, server_id: str, context: ToolContext) -> dict[str, object]:
        config = self.configs.get(server_id)
        if config is None or self.capabilities is None:
            raise PermissionError("MCP server was not authorized at run start")
        cleanup = self._begin_server_close(server_id)
        self.host.store.remove_intent(self.scope_id, server_id)
        await finish_cleanup(cleanup)
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
                 "error": self._cleanup_errors.get(item.server_id) or errors.get(item.server_id),
                 "cleanup": self.cleanup_diagnostic(item.server_id),
                 "tools": [{"name": tool.definition.name, "loaded": tool.definition.name in visible} for tool in self.remote_tools.get(item.server_id, ())]} for item in self.configs.values()]

    def cleanup_diagnostic(self, server_id: str) -> dict[str, object] | None:
        client = self.clients.get(server_id)
        if (client is None or client.exit_confirmed) and server_id not in self._opening:
            return None
        if not (self.closed or server_id in self.disconnected or client is not None and client.closed or server_id in self._cleanup_errors):
            return None
        config = self.configs.get(server_id)
        return {"runId": self.run_id, "roleId": self.role_id, "sessionKey": self.session_key,
                "scopeId": self.scope_id, "serverId": server_id,
                "configRevision": config.revision if config else None,
                "grantRevision": config.grant_id if config else None,
                "connectionGeneration": self._generations.get(server_id, 0),
                "state": "cleanup_unconfirmed" if self._cleanup_errors.get(server_id) == CLEANUP_UNCONFIRMED else
                    "cleanup_failed" if server_id in self._cleanup_errors else "cleanup_pending"}

    async def _from_any_loop(self, server_id: str | None) -> None:
        if self.exit_confirmed:
            return
        if server_id is None:
            self.closed = True
        else:
            # Seal even if the original loop is stopped: when it resumes an
            # old opening must not publish or restore intent before retry.
            with self.host._lock:
                self.disconnected.add(server_id)
                self._epochs[server_id] = self._epochs.get(server_id, 0) + 1
        if self.loop is asyncio.get_running_loop():
            await (self.close() if server_id is None else self.close_server(server_id))
        elif self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self.close() if server_id is None else self.close_server(server_id), self.loop)
            async def join():
                await asyncio.wrap_future(future)
            await finish_cleanup(asyncio.create_task(join()))
        else:
            keys = self.clients.keys() | self._opening.keys() if server_id is None else {server_id}
            if all(key not in self._opening and (key not in self.clients or self.clients[key].exit_confirmed) for key in keys):
                if server_id is None:
                    self.exit_confirmed = True
                    self.cleanup_failed = False
                    with self.host._lock:
                        if self.host._runs.get(self.run_id) is self:
                            del self.host._runs[self.run_id]
                return
            self.cleanup_failed = True
            for key, client in self.clients.items():
                if (server_id is None or server_id == key) and not client.exit_confirmed:
                    self._cleanup_errors[key] = CLEANUP_UNCONFIRMED
            raise McpProtocolError(CLEANUP_UNCONFIRMED)

    async def close_from_any_loop(self) -> None:
        await self._from_any_loop(None)

    async def close_server_from_any_loop(self, server_id: str) -> None:
        await self._from_any_loop(None if self.closed else server_id)

    @staticmethod
    def _observe_cleanup(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()

    async def _dispose_client(self, server_id: str, client: McpStdioClient) -> None:
        try:
            if not client.exit_confirmed:
                await client.close()
            if not client.exit_confirmed:
                raise McpProtocolError(CLEANUP_FAILED)
        except BaseException:
            self.cleanup_failed = True
            self._cleanup_errors[server_id] = CLEANUP_FAILED
            raise McpProtocolError(CLEANUP_FAILED) from None
        self._cleanup_errors.pop(server_id, None)
        self.cleanup_failed = bool(self._cleanup_errors)

    def _begin_server_close(self, server_id: str) -> asyncio.Task:
        self.disconnected.add(server_id)
        task = self._server_closing.get(server_id)
        client = self.clients.get(server_id)
        if task is None or task.done() and (server_id in self._opening or client is not None and not client.exit_confirmed):
            self._epochs[server_id] = self._epochs.get(server_id, 0) + 1
            task = asyncio.create_task(self._close_server(server_id))
            task.add_done_callback(self._observe_cleanup)
            self._server_closing[server_id] = task
        return task

    async def close_server(self, server_id: str) -> None:
        await finish_cleanup(self._begin_server_close(server_id))

    async def _close_server(self, server_id: str) -> None:
        opening = self._opening.get(server_id)
        if opening is not None:
            was_opening = not opening.done()
            opening.cancel()
            await asyncio.gather(opening, return_exceptions=True)
            if self._opening.get(server_id) is opening:
                del self._opening[server_id]
            if was_opening and server_id in self._cleanup_errors:
                # The child just attempted disposal. Preserve that failure;
                # a second explicit close may retry the same retained peer.
                raise McpProtocolError(CLEANUP_FAILED)
        client = self.clients.get(server_id)
        if client is not None:
            await self._dispose_client(server_id, client)

    def _begin_close(self) -> asyncio.Task:
        self.closed = True
        if self._closing is None or self._closing.done() and not self.exit_confirmed:
            self._closing = asyncio.create_task(self._close())
            self._closing.add_done_callback(self._observe_cleanup)
        return self._closing

    async def close(self) -> None:
        if not self.exit_confirmed:
            await finish_cleanup(self._begin_close())

    async def _close(self) -> None:
        try:
            self._watchdog.cancel()
            await asyncio.gather(self._watchdog, return_exceptions=True)
            servers = self.clients.keys() | self._opening.keys()
            results = await asyncio.gather(*(self.close_server(server_id) for server_id in servers), return_exceptions=True)
            if any(isinstance(result, BaseException) for result in results) or any(not client.exit_confirmed for client in self.clients.values()):
                raise McpProtocolError(CLEANUP_FAILED)
            self.exit_confirmed = True
            self.cleanup_failed = False
            with self.host._lock:
                if self.host._runs.get(self.run_id) is self:
                    del self.host._runs[self.run_id]
        except BaseException:
            self.cleanup_failed = True
            raise
