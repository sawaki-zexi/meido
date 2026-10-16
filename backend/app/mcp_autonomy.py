"""Explicit operation grants for an actual host-owned autonomous origin.

Uses the existing bounded MCP transport. It never manufactures an ordinary
owner chat bundle or persists an ordinary chat connection intent.
"""
import asyncio
from dataclasses import dataclass
import hashlib
import json

from .mcp_stdio import McpProtocolError, McpStdioClient, finish_cleanup, validate_instance


def declaration_hash(declaration):
    return hashlib.sha256(json.dumps(declaration, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AutonomyMcpGrant:
    server_id: str
    config_revision: str
    grant_revision: str
    operations: tuple[tuple[str, str], ...]


class AutonomyMcpRun:
    def __init__(self, host, origin, grants, authorized, role_gate, server_gates):
        self.host, self.origin, self.authorized = host, origin, authorized
        self.role_id = origin.role_id
        self.loop = asyncio.get_running_loop()
        self.closed = self.exit_confirmed = self.cleanup_failed = self._revoked = False
        self.clients = {}
        self.configs = {}
        self.errors = {}
        self._permissions: dict[str, dict[str, str]] = {}
        self._catalogs = {}
        self._client_locks: dict[str, asyncio.Lock] = {}
        self._role_gate, self._server_gates = role_gate, server_gates
        self._closing: asyncio.Task[None] | None = None
        self._operations = set()
        self._secrets = {}
        for grant in grants:
            config = host.store.get(grant.server_id)
            self.configs[grant.server_id] = config
            if (config is None or not config.enabled or self.role_id not in config.role_ids
                    or (config.revision, config.grant_id) != (grant.config_revision, grant.grant_revision)):
                self.errors[grant.server_id] = "autonomous MCP configuration grant is unavailable"
            permissions = self._permissions.setdefault(grant.server_id, {})
            for name, digest in grant.operations:
                if name in permissions and permissions[name] != digest:
                    raise ValueError("conflicting autonomous MCP definition grants")
                permissions[name] = digest
            self._client_locks.setdefault(grant.server_id, asyncio.Lock())
        self._watcher = asyncio.create_task(self._watch())

    def _require(self, server_id, name=None):
        try:
            live = not self._revoked and self.authorized()
            config = self.configs.get(server_id)
            unchanged = config is not None and self.host.store.get(server_id) == config
        except Exception:
            live = unchanged = False
        if not live or not unchanged:
            self._begin_close()
        if self.closed or self._revoked:
            raise PermissionError("autonomous MCP host origin was revoked")
        if server_id in self.errors:
            raise PermissionError("autonomous MCP service grant was revoked or changed")
        if name is not None and name not in self._permissions.get(server_id, {}):
            raise PermissionError("MCP operation was not frozen in this autonomous grant")
        return config

    async def _watch(self):
        try:
            while not self.closed:
                await asyncio.sleep(.05)
                if (not self.authorized()
                        or any(self.host.store.get(key) != config for key, config in self.configs.items())):
                    # Return before cleanup joins this watcher. Awaiting close
                    # here would make the watcher and close task join each other.
                    self._begin_close()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed authority/config read cannot keep an idle peer alive.
            self._begin_close()

    @staticmethod
    def _observe_cleanup(task):
        if not task.cancelled():
            task.exception()  # Preserve the task for explicit retry/reporting.

    async def _client(self, server_id):
        async with self._client_locks[server_id]:
            config = self._require(server_id)
            if server_id not in self.clients:
                environment = self.host.store.environment(config)
                self._secrets[server_id] = tuple(environment[key] for key in config.env_keys if environment.get(key))
                client = McpStdioClient(config, environment)
                self.clients[server_id] = client
                try:
                    declarations = await client.connect()
                    catalog = {declaration["name"]: declaration for declaration in declarations}
                    for name, digest in self._permissions[server_id].items():
                        if name not in catalog or declaration_hash(catalog[name]) != digest:
                            raise PermissionError("autonomous MCP tool definition changed")
                    self._require(server_id)
                    self._catalogs[server_id] = catalog
                except BaseException:
                    self.errors[server_id] = "autonomous MCP directory validation failed"
                    await client.close()
                    raise
            return self.clients[server_id]

    async def call(self, server_id, name, arguments):
        self._require(server_id, name)
        operation = asyncio.create_task(self._call(server_id, name, arguments))
        self._operations.add(operation)
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            operation.cancel()
            async def join():
                await asyncio.gather(operation, return_exceptions=True)
            try:
                await finish_cleanup(asyncio.create_task(join()))
            finally:
                await self.close()
            raise
        finally:
            self._operations.discard(operation)

    async def _call(self, server_id, name, arguments):
        config = self._require(server_id, name)
        acquired_role = acquired_server = False
        server_gate = self._server_gates[server_id]
        try:
            async with asyncio.timeout(min(30, config.timeout_seconds)):
                while not (acquired_role := self._role_gate.acquire(blocking=False)):
                    self._require(server_id, name)
                    await asyncio.sleep(.01)
                while not (acquired_server := server_gate.acquire(blocking=False)):
                    self._require(server_id, name)
                    await asyncio.sleep(.01)
                client = await self._client(server_id)
                declaration = self._catalogs[server_id][name]
                if not await validate_instance(declaration["inputSchema"], arguments):
                    raise ValueError("autonomous MCP arguments do not match the frozen schema")
                self._require(server_id, name)
                response = await client.call(name, arguments)
                self._require(server_id, name)
                if len(json.dumps(response, ensure_ascii=False).encode("utf-8")) > 256 * 1024:
                    raise McpProtocolError("autonomous source response exceeds byte budget")
                if "outputSchema" in declaration and not await validate_instance(
                        declaration["outputSchema"], response.get("structuredContent")):
                    raise McpProtocolError("autonomous source response violates its schema")
                from .mcp_runtime import _redact_value
                return _redact_value(response, self._secrets[server_id])
        finally:
            if acquired_server:
                server_gate.release()
            if acquired_role:
                self._role_gate.release()

    async def close_server_from_any_loop(self, server_id):
        # A frozen autonomous operation never reconnects after revocation.
        self.errors[server_id] = "autonomous MCP service was revoked"
        await self.close_from_any_loop()

    async def close_from_any_loop(self):
        if self.exit_confirmed:
            return
        if asyncio.get_running_loop() is self.loop:
            await self.close()
        elif self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self.close(), self.loop)
            async def join():
                await asyncio.wrap_future(future)
            await finish_cleanup(asyncio.create_task(join()))
        else:
            raise RuntimeError("autonomous MCP cleanup still belongs to its original loop")

    async def close(self):
        await finish_cleanup(self._begin_close())

    def _begin_close(self) -> asyncio.Task[None]:
        self.closed = self._revoked = True
        if self._closing is None or self._closing.done() and not self.exit_confirmed:
            self._closing = asyncio.create_task(self._close())
            self._closing.add_done_callback(self._observe_cleanup)
        return self._closing

    async def _close(self):
        self.closed = True
        try:
            if self._watcher is not asyncio.current_task():
                self._watcher.cancel()
                await asyncio.gather(self._watcher, return_exceptions=True)
            # Own child operation tasks, not their callers: a caller's finally
            # may itself await close. Joining that caller would self-deadlock.
            operations = tuple(self._operations)
            for operation in operations:
                operation.cancel()
            await asyncio.gather(*operations, return_exceptions=True)
            results = await asyncio.gather(*(client.close() for client in self.clients.values()), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            if any(getattr(client, "exit_confirmed", True) is not True for client in self.clients.values()):
                raise RuntimeError("autonomous MCP process cleanup is unconfirmed")
            self.exit_confirmed = True
            self.cleanup_failed = False
            with self.host._lock:
                if self.host._autonomy_runs.get(self.origin.tick_id) is self:
                    del self.host._autonomy_runs[self.origin.tick_id]
        except BaseException:
            self.cleanup_failed = True
            raise
