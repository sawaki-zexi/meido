"""In-memory recovery of quiesced channel state during role deletion.

The snapshot never reaches a file, API response or logger. Credentials stay
in the process only until deletion succeeds or the original role is restored.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from .credentials import credential_key

if TYPE_CHECKING:
    from ..multichannel import MultichannelStore

_CHANNEL_TABLES = (
    "channel_accounts", "channel_account_settings", "channel_threads",
    "channel_identities", "channel_owner_chats", "channel_events",
    "channel_targets", "channel_delivery_attempts", "channel_group_settings",
    "channel_group_context", "channel_member_profiles", "channel_member_names", "channel_memory_cursors",
    "channel_external_revisions", "channel_external_members", "channel_external_member_sources", "channel_external_member_candidates",
    "channel_listening_records", "channel_listening_config", "channel_listening_toggles",
    "channel_listening_cursors", "channel_natural_settings", "channel_natural_quota", "channel_push_targets", "channel_push_events",
    "channel_avatars", "sessions", "messages", "agent_runs", "tool_audits", "proactive_messages",
)
_SECRET_NAMES = {
    "telegram": ("bot_token",), "qq": ("access_token",),
    "qqbot": ("client_secret",),
    "feishu": ("app_secret", "verification_token", "encrypt_key"),
}


@dataclass(frozen=True)
class TableSnapshot:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...] = field(repr=False)
    predicate: str = field(repr=False)
    parameters: tuple[Any, ...] = field(repr=False)


@dataclass(frozen=True)
class ChannelRoleSnapshot:
    role_id: str
    tables: dict[str, TableSnapshot] = field(repr=False)
    credentials: dict[str, str] = field(repr=False)
    avatar_files: dict[str, bytes] = field(repr=False)
    listening_windows: dict[str, Any] = field(repr=False)


def _membership(column: str, values: tuple[str, ...]) -> tuple[str, tuple[Any, ...]]:
    return (f"{column} IN ({','.join('?' for _ in values)})", values) if values else ("0", ())


def snapshot_role(store: MultichannelStore, role_id: str) -> ChannelRoleSnapshot:
    """Snapshot after channels and their background workers are quiesced."""

    tables: dict[str, TableSnapshot] = {}
    with store._connect() as db:
        db.execute("BEGIN")
        existing = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        accounts = db.execute("SELECT account_id,channel FROM channel_accounts WHERE role_id=?", (role_id,)).fetchall()
        account_ids = tuple(str(row[0]) for row in accounts)
        threads = db.execute("SELECT thread_id,session_key FROM channel_threads WHERE role_id=?", (role_id,)).fetchall()
        thread_ids = tuple(str(row[0]) for row in threads)
        session_keys = tuple({f"role:{role_id}", *(str(row[1]) for row in threads)})
        session_predicate, session_parameters = _membership("session_key", session_keys)
        message_ids = tuple(str(row[0]) for row in db.execute(f"SELECT message_id FROM messages WHERE {session_predicate}", session_parameters))
        for table in _CHANNEL_TABLES:
            if table not in existing:
                continue
            columns = tuple(str(row[1]) for row in db.execute(f"PRAGMA table_info({table})"))
            if table == "channel_identities":
                predicate, parameters = _membership("account_id", account_ids)
                predicate = f"scope='account' AND ({predicate})"
            elif table == "channel_listening_config":
                predicate, parameters = _membership("key", thread_ids)
            elif table == "proactive_messages":
                predicate, parameters = _membership("message_id", message_ids)
            elif "role_id" in columns:
                predicate, parameters = "role_id=?", (role_id,)
            elif "thread_id" in columns:
                predicate, parameters = _membership("thread_id", thread_ids)
            elif "account_id" in columns:
                predicate, parameters = _membership("account_id", account_ids)
            elif "session_key" in columns:
                predicate, parameters = session_predicate, session_parameters
            else:
                raise ValueError(f"缺少渠道角色快照范围：{table}")
            selection = "rowid,*" if table == "messages" else "*"
            rows = tuple(tuple(row) for row in db.execute(f"SELECT {selection} FROM {table} WHERE {predicate}", parameters))
            if table == "messages":
                columns = ("rowid", *columns)
            tables[table] = TableSnapshot(columns, rows, predicate, parameters)
    secrets = {
        credential_key(str(row[1]), str(row[0]), name): store.credentials.get(credential_key(str(row[1]), str(row[0]), name))
        for row in accounts for name in _SECRET_NAMES.get(str(row[1]), ())
    }
    avatar_files = {}
    avatars = tables.get("channel_avatars")
    if avatars:
        index = avatars.columns.index("file_name")
        for row in avatars.rows:
            name = str(row[index])
            path = store.avatars.root / name
            if name and path.parent == store.avatars.root and path.is_file():
                avatar_files[name] = path.read_bytes()
    windows = {thread_id: deepcopy(store.listening._windows[thread_id]) for thread_id in thread_ids if thread_id in store.listening._windows}
    return ChannelRoleSnapshot(role_id, tables, secrets, avatar_files, windows)


def restore_role(store: MultichannelStore, role_id: str, snapshot: ChannelRoleSnapshot) -> None:
    if snapshot.role_id != role_id:
        raise ValueError("渠道快照不属于当前角色")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for table, saved in reversed(tuple(snapshot.tables.items())):
            db.execute(f"DELETE FROM {table} WHERE {saved.predicate}", saved.parameters)
        for table, saved in snapshot.tables.items():
            for row in saved.rows:
                columns, values = saved.columns, row
                if table == "messages":
                    occupied = db.execute("SELECT message_id FROM messages WHERE rowid=?", (row[0],)).fetchone()
                    if occupied is not None:
                        # Another role may have written after this role's rows
                        # were removed. Never overwrite its newly assigned row.
                        columns, values = columns[1:], row[1:]
                db.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in values)})", values)
    if snapshot.avatar_files:
        store.avatars.root.mkdir(parents=True, exist_ok=True)
        for name, content in snapshot.avatar_files.items():
            path = store.avatars.root / name
            if path.parent != store.avatars.root:
                raise ValueError("渠道头像快照路径无效")
            path.write_bytes(content)
    for key, value in snapshot.credentials.items():
        store.credentials.set(key, value)
    store.listening._windows.update(deepcopy(snapshot.listening_windows))
