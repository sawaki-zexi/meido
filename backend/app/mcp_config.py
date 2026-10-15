"""Owner-authored stdio launch grants and durable, source-bound connection intent."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .credentials import CredentialStore


MCP_MANAGEMENT_NAMES = frozenset({"mcp_add", "mcp_list", "mcp_remove"})
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def minimal_environment() -> dict[str, str]:
    allowed = {"PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL", "HOME", "USERPROFILE"}
    return {key: value for key, value in os.environ.items() if key in allowed}


class McpServerInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    serverId: str = Field(pattern=r"^[a-z][a-z0-9_]{0,23}$")
    name: str = Field(min_length=1, max_length=100)
    command: list[str] = Field(min_length=1, max_length=32)
    cwd: str = Field(min_length=1, max_length=4096)
    env: dict[str, str] | None = None
    roleIds: list[str] = Field(default_factory=list, max_length=100)
    enabled: bool = True
    risk: Literal["read_only", "mutating", "external"] = "external"
    timeoutSeconds: float = Field(default=30, ge=1, le=120)
    maxOutputChars: int = Field(default=20000, ge=1024, le=200000)
    enableDiscovery: bool = True

    @field_validator("command")
    @classmethod
    def fixed_command(cls, value: list[str]) -> list[str]:
        if any(not arg or "\x00" in arg or len(arg) > 8192 for arg in value) or sum(map(len, value)) > 32768:
            raise ValueError("MCP command is invalid or too long")
        executable = Path(value[0])
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("MCP executable must be an existing absolute file")
        return value

    @field_validator("cwd")
    @classmethod
    def fixed_directory(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("MCP working directory must be an existing absolute directory")
        return str(path.resolve())

    @field_validator("env")
    @classmethod
    def environment_keys(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is not None and (len(value) > 64 or any(not _ENV_KEY.fullmatch(key) or "\x00" in item or len(item) > 16384 for key, item in value.items())):
            raise ValueError("MCP environment is invalid or too large")
        return value


class McpCredentialCleanupError(RuntimeError):
    """The owner must retry an operation; cleanup ownership remains durable."""


@dataclass(frozen=True)
class McpServer:
    server_id: str
    name: str
    command: tuple[str, ...]
    cwd: str
    env_keys: tuple[str, ...]
    role_ids: tuple[str, ...]
    enabled: bool
    risk: str
    timeout_seconds: float
    max_output_chars: int
    revision: str

    @property
    def grant_id(self) -> str:
        return f"mcp:{self.server_id}:{self.revision}"

    def credential_key(self, key: str) -> str:
        return f"mcp:{self.server_id}:{self.revision}:env:{key}"

    def public(self) -> dict[str, object]:
        return {"serverId": self.server_id, "name": self.name, "command": list(self.command), "cwd": self.cwd,
                "envKeys": list(self.env_keys), "roleIds": list(self.role_ids), "enabled": self.enabled,
                "risk": self.risk, "timeoutSeconds": self.timeout_seconds, "maxOutputChars": self.max_output_chars,
                "revision": self.revision}


class McpConfigStore:
    def __init__(self, path: Path, credentials: CredentialStore) -> None:
        self.path = path
        self.credentials = credentials
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS mcp_servers (server_id TEXT PRIMARY KEY, configuration TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mcp_intents (
                    scope_id TEXT NOT NULL, role_id TEXT NOT NULL, session_key TEXT NOT NULL,
                    server_id TEXT NOT NULL, revision TEXT NOT NULL,
                    PRIMARY KEY(scope_id, server_id));
                CREATE TABLE IF NOT EXISTS mcp_credential_cleanup (
                    server_id TEXT NOT NULL, credential_key TEXT PRIMARY KEY);
            """)

    def _connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def get(self, server_id: str) -> McpServer | None:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT configuration FROM mcp_servers WHERE server_id=?", (server_id,)).fetchone()
        if row is None:
            return None
        data = json.loads(row[0])
        for key in ("command", "env_keys", "role_ids"):
            data[key] = tuple(data[key])
        return McpServer(**data)

    def all(self) -> tuple[McpServer, ...]:
        with self._lock, self._connection() as db:
            rows = db.execute("SELECT server_id FROM mcp_servers ORDER BY server_id").fetchall()
            return tuple(item for (server_id,) in rows if (item := self.get(server_id)) is not None)

    def list_public(self) -> list[dict[str, object]]:
        return [item.public() for item in self.all()]

    def save(self, data: McpServerInput) -> McpServer:
        with self._lock:
            self._drain_cleanup(data.serverId)
            old = self.get(data.serverId)
            requested = data.env if data.env is not None else {key: "" for key in old.env_keys} if old else {}
            values: dict[str, str] = {}
            for key, value in requested.items():
                values[key] = value or (self.credentials.get(old.credential_key(key)) if old and key in old.env_keys else "")
                if not values[key]:
                    raise ValueError("new MCP environment values cannot be empty")
            item = McpServer(data.serverId, data.name.strip(), tuple(data.command), data.cwd, tuple(sorted(values)),
                             tuple(sorted(set(data.roleIds))), data.enabled, data.risk, data.timeoutSeconds,
                             data.maxOutputChars, uuid4().hex)
            # Reserve ownership before the first vault write: even a vault
            # operation that writes and then raises remains safely retryable.
            with self._connection() as db:
                self._queue_cleanup(db, item)
            try:
                for key, value in values.items():
                    credential_key = item.credential_key(key)
                    self.credentials.set(credential_key, value)
                with self._connection() as db:
                    db.execute("INSERT OR REPLACE INTO mcp_servers VALUES (?,?)", (item.server_id, json.dumps(asdict(item))))
                    db.execute("DELETE FROM mcp_intents WHERE server_id=?", (item.server_id,))
                    db.executemany("DELETE FROM mcp_credential_cleanup WHERE credential_key=?", ((item.credential_key(key),) for key in item.env_keys))
                    if old is not None:
                        self._queue_cleanup(db, old)
            except Exception:
                try:
                    self._drain_cleanup(item.server_id)
                except McpCredentialCleanupError:
                    self._disable_server(item.server_id)
                    raise
                raise RuntimeError("MCP configuration could not be saved") from None
            try:
                self._drain_cleanup(item.server_id)
            except McpCredentialCleanupError:
                self._disable_server(item.server_id)
                raise
            return item

    def _disable_server(self, server_id: str) -> None:
        current = self.get(server_id)
        with self._connection() as db:
            if current is not None:
                db.execute("UPDATE mcp_servers SET configuration=? WHERE server_id=?", (json.dumps(asdict(replace(current, enabled=False))), server_id))
            db.execute("DELETE FROM mcp_intents WHERE server_id=?", (server_id,))

    def cleanup_pending(self, server_id: str) -> bool:
        with self._lock, self._connection() as db:
            return db.execute("SELECT 1 FROM mcp_credential_cleanup WHERE server_id=? LIMIT 1", (server_id,)).fetchone() is not None

    def _queue_cleanup(self, db: sqlite3.Connection, config: McpServer) -> None:
        db.executemany("INSERT OR IGNORE INTO mcp_credential_cleanup VALUES (?,?)",
            ((config.server_id, config.credential_key(key)) for key in config.env_keys))

    def _drain_cleanup(self, server_id: str) -> None:
        with self._connection() as db:
            keys = db.execute("SELECT credential_key FROM mcp_credential_cleanup WHERE server_id=?", (server_id,)).fetchall()
        failed = False
        for (key,) in keys:
            try:
                self.credentials.set(key, "")
            except Exception:
                failed = True
            else:
                with self._connection() as db:
                    db.execute("DELETE FROM mcp_credential_cleanup WHERE credential_key=?", (key,))
        if failed:
            raise McpCredentialCleanupError("MCP credential cleanup is pending; retry saving or deleting")

    def prepare_delete(self, server_id: str) -> bool:
        """Revoke before awaiting process disposal or touching the vault."""
        with self._lock:
            old = self.get(server_id)
            with self._connection() as db:
                if old is not None:
                    self._queue_cleanup(db, old)
                    db.execute("UPDATE mcp_servers SET configuration=? WHERE server_id=?", (json.dumps(asdict(replace(old, enabled=False))), server_id))
                db.execute("DELETE FROM mcp_intents WHERE server_id=?", (server_id,))
            return old is not None

    def delete(self, server_id: str) -> bool:
        with self._lock:
            existed = self.prepare_delete(server_id)
            self._drain_cleanup(server_id)
            with self._connection() as db:
                db.execute("DELETE FROM mcp_servers WHERE server_id=?", (server_id,))
            return existed

    def environment(self, config: McpServer) -> dict[str, str]:
        # An explicit small platform baseline, never the host's credentials.
        env = minimal_environment()
        for key in config.env_keys:
            value = self.credentials.get(config.credential_key(key))
            if not value:
                raise RuntimeError("MCP environment credential is unavailable")
            env[key] = value
        return env

    def set_intent(self, scope_id: str, role_id: str, session_key: str, config: McpServer) -> None:
        with self._lock:
            if self.get(config.server_id) != config:
                raise PermissionError("MCP grant changed during connection")
            with self._connection() as db:
                db.execute("INSERT OR REPLACE INTO mcp_intents VALUES (?,?,?,?,?)", (scope_id, role_id, session_key, config.server_id, config.revision))

    def has_intent(self, scope_id: str, config: McpServer) -> bool:
        with self._lock, self._connection() as db:
            return db.execute("SELECT 1 FROM mcp_intents WHERE scope_id=? AND server_id=? AND revision=?", (scope_id, config.server_id, config.revision)).fetchone() is not None

    def remove_intent(self, scope_id: str, server_id: str) -> None:
        with self._lock, self._connection() as db:
            db.execute("DELETE FROM mcp_intents WHERE scope_id=? AND server_id=?", (scope_id, server_id))

    def delete_role_intents(self, role_id: str) -> None:
        with self._lock, self._connection() as db:
            db.execute("DELETE FROM mcp_intents WHERE role_id=?", (role_id,))
