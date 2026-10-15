"""Owner-authored stdio launch grants and durable, source-bound connection intent."""
from __future__ import annotations

from dataclasses import asdict, dataclass
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
            written: list[str] = []
            try:
                for key, value in values.items():
                    credential_key = item.credential_key(key)
                    self.credentials.set(credential_key, value)
                    written.append(credential_key)
                with self._connection() as db:
                    db.execute("INSERT OR REPLACE INTO mcp_servers VALUES (?,?)", (item.server_id, json.dumps(asdict(item))))
                    db.execute("DELETE FROM mcp_intents WHERE server_id=?", (item.server_id,))
            except Exception:
                for key in written:
                    self._clear_credential(key)
                raise RuntimeError("MCP configuration could not be saved") from None
            if old:
                for key in old.env_keys:
                    self._clear_credential(old.credential_key(key))
            return item

    def _clear_credential(self, key: str) -> None:
        try:
            self.credentials.set(key, "")
        except Exception:
            # Stale vault entries are unreachable by the published revision.
            pass

    def delete(self, server_id: str) -> bool:
        with self._lock:
            old = self.get(server_id)
            if old is None:
                return False
            with self._connection() as db:
                db.execute("DELETE FROM mcp_servers WHERE server_id=?", (server_id,))
                db.execute("DELETE FROM mcp_intents WHERE server_id=?", (server_id,))
            for key in old.env_keys:
                self._clear_credential(old.credential_key(key))
            return True

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
