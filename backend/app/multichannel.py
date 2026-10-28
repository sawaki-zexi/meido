"""Shiori-style channel accounts, identities and network conversation storage.

The channel adapter deliberately has no dependency on the agent runtime.  It
normalizes platform events, persists an isolated network thread and invokes a
host callback for a role turn.  Keeping that boundary explicit prevents a
plugin supplied identity flag from granting owner permissions.
"""
from __future__ import annotations

import hashlib
import asyncio
import json
import secrets
import sqlite3
import uuid
from threading import RLock
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Protocol, TYPE_CHECKING
from pathlib import Path
from contextlib import nullcontext

import httpx
from fastapi import HTTPException
from .chat_attachments import MAX_ATTACHMENT_BYTES
from .models import Message

from .credentials import CredentialStore, SystemCredentialStore
from .channels.credentials import credential_key
from .session_store import SessionStore
from .session_manager import public_message
from .storage import initialize_databases

if TYPE_CHECKING:
    from .channels.models import InboundEvent
    from .channels.role_snapshot import ChannelRoleSnapshot


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass(frozen=True, slots=True)
class ChannelAccount:
    account_id: str
    role_id: str
    channel: str
    enabled: bool
    allow_private: bool
    allow_group: bool
    blocked_senders: tuple[str, ...]
    display_name: str
    platform_id: str | None
    created_at: str
    updated_at: str
    last_error: str | None = None

    def source_snapshot(self) -> dict[str, object]:
        """Capture public account identity without carrying any credentials."""

        return {
            "platform": self.channel,
            "platformAccountId": self.platform_id or self.account_id,
            "accountId": self.account_id,
            "displayName": self.display_name,
        }

    def public_dict(self, *, token_configured: bool = True) -> dict[str, object]:
        return {
            "accountId": self.account_id,
            "roleId": self.role_id,
            "channel": self.channel,
            "enabled": self.enabled,
            "allowPrivate": self.allow_private,
            "allowGroup": self.allow_group,
            "blockedSenders": list(self.blocked_senders),
            "displayName": self.display_name,
            "platformId": self.platform_id,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "lastError": self.last_error,
            "tokenConfigured": token_configured,
        }


@dataclass(frozen=True, slots=True)
class ChannelThread:
    thread_id: str
    session_key: str
    role_id: str
    channel: str
    account_id: str
    chat_id: str
    chat_type: str
    title: str
    last_activity: str
    metadata: dict[str, Any]

    def public_dict(self) -> dict[str, object]:
        return {
            "threadId": self.thread_id,
            "sessionKey": self.session_key,
            "roleId": self.role_id,
            "channel": self.channel,
            "accountId": self.account_id,
            "chatId": self.chat_id,
            "chatType": self.chat_type,
            "title": self.title,
            "lastActivity": self.last_activity,
            "metadata": self.metadata,
            "viaAccount": self.metadata.get("viaAccount"),
            "accountRemoved": bool(self.metadata.get("accountRemoved", False)),
            "latestMessage": self.metadata.get("latestMessage"),
        }


class TelegramTransport(Protocol):
    async def get_me(self, token: str) -> Mapping[str, Any]: ...

    async def send_message(self, token: str, chat_id: str, text: str, *, reply_to: str | None = None, message_thread_id: int | None = None, parse_mode: str | None = None) -> Mapping[str, Any]: ...

    async def edit_message_text(self, token: str, chat_id: str, message_id: str, text: str) -> Mapping[str, Any]: ...

    async def send_chat_action(self, token: str, chat_id: str, *, authorize_dispatch=None) -> bool: ...

    async def get_updates(self, token: str, *, offset: int | None = None, timeout: int = 25) -> list[Mapping[str, Any]]: ...

    async def get_file(self, token: str, file_id: str) -> Mapping[str, Any]: ...

    async def download_file(self, token: str, file_path: str) -> tuple[bytes, str]: ...

    async def send_photo(self, token: str, chat_id: str, data: bytes, file_name: str, *, caption: str = "", reply_to: str | None = None, message_thread_id: int | None = None, parse_mode: str | None = None) -> Mapping[str, Any]: ...

    async def send_document(self, token: str, chat_id: str, data: bytes, file_name: str, *, caption: str = "", reply_to: str | None = None, message_thread_id: int | None = None, parse_mode: str | None = None) -> Mapping[str, Any]: ...


class TelegramBotApi:
    """Small injectable Bot API client; tests can replace it without sockets."""

    def __init__(self, base_url: str = "https://api.telegram.org") -> None:
        self.base_url = base_url.rstrip("/")

    async def get_me(self, token: str) -> Mapping[str, Any]:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{self.base_url}/bot{token}/getMe")
            response.raise_for_status()
            payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("description") or "Telegram Bot API 请求失败"))
        return payload.get("result") or {}

    async def _profile_request(self, token: str, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{self.base_url}/bot{token}/{method}", params=params)
            response.raise_for_status()
            payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError("Telegram 头像资料请求失败")
        return payload.get("result") or {}

    async def get_chat(self, token: str, chat_id: str) -> Mapping[str, Any]:
        return await self._profile_request(token, "getChat", {"chat_id": chat_id})

    async def get_chat_member(self, token: str, chat_id: str, user_id: str) -> Mapping[str, Any]:
        """Read one member with a hard streamed response budget."""
        from .channels.directory import DirectoryError, MAX_RESPONSE_BYTES
        async with httpx.AsyncClient(timeout=10) as client:
            async with client.stream("GET", f"{self.base_url}/bot{token}/getChatMember",
                                     params={"chat_id": chat_id, "user_id": user_id},
                                     headers={"Accept-Encoding": "identity"}) as response:
                if response.status_code in {401, 403}:
                    raise DirectoryError("permission_denied")
                if not 200 <= response.status_code < 300:
                    raise DirectoryError("platform_rejected")
                if response.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
                    raise DirectoryError("unsupported_response_encoding")
                length = response.headers.get("content-length", "0")
                if not length.isdecimal():
                    raise DirectoryError("invalid_platform_response")
                if int(length) > MAX_RESPONSE_BYTES:
                    raise DirectoryError("directory_too_large")
                data = bytearray()
                async for chunk in response.aiter_raw(chunk_size=64 * 1024):
                    if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise DirectoryError("directory_too_large")
                    data.extend(chunk)
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeError) as error:
            raise DirectoryError("invalid_platform_response") from error
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise DirectoryError("platform_rejected")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise DirectoryError("invalid_platform_response")
        return result

    async def get_user_profile_photos(self, token: str, user_id: str) -> Mapping[str, Any]:
        return await self._profile_request(token, "getUserProfilePhotos", {"user_id": user_id, "limit": 1})

    async def send_message(self, token: str, chat_id: str, text: str, *, reply_to: str | None = None, message_thread_id: int | None = None, parse_mode: str | None = None, entities=None) -> Mapping[str, Any]:
        body: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if entities:
            body['entities'] = entities
        if message_thread_id is not None:
            body["message_thread_id"] = message_thread_id
        if parse_mode is not None:
            body["parse_mode"] = parse_mode
        if reply_to:
            body["reply_parameters"] = {"message_id": int(reply_to) if str(reply_to).isdigit() else reply_to}
        from .channels.delivery import check_dispatch, telegram_response
        check_dispatch()
        async with httpx.AsyncClient(timeout=20) as client:
            check_dispatch()
            response = await client.post(f"{self.base_url}/bot{token}/sendMessage", json=body)
            return telegram_response(response)

    async def edit_message_text(self, token: str, chat_id: str, message_id: str, text: str, *, entities=None) -> Mapping[str, Any]:
        body = {"chat_id": chat_id, "message_id": int(message_id) if message_id.isdigit() else message_id, "text": text}
        if entities is not None:
            body['entities'] = entities
        from .channels.delivery import check_dispatch, telegram_response
        check_dispatch()
        async with httpx.AsyncClient(timeout=20) as client:
            check_dispatch()
            response = await client.post(f"{self.base_url}/bot{token}/editMessageText", json=body)
            result = telegram_response(response)
            return {'message_id': message_id} if result.get('not_modified') else result

    async def send_chat_action(self, token: str, chat_id: str, *, authorize_dispatch=None) -> bool:
        from .channels.delivery import telegram_response
        async with httpx.AsyncClient(timeout=10) as client:
            if authorize_dispatch is not None:
                authorize_dispatch()
            response = await client.post(f"{self.base_url}/bot{token}/sendChatAction",
                json={'chat_id': chat_id, 'action': 'typing'})
            return telegram_response(response) is True

    async def _send_media(self, method: str, token: str, chat_id: str, data: bytes, file_name: str, *, caption: str = "", reply_to: str | None = None, message_thread_id: int | None = None, parse_mode: str | None = None, caption_entities=None) -> Mapping[str, Any]:
        body: dict[str, Any] = {"chat_id": chat_id, "caption": caption}
        if caption_entities:
            body['caption_entities'] = json.dumps(caption_entities, ensure_ascii=False)
        if message_thread_id is not None:
            body["message_thread_id"] = message_thread_id
        if parse_mode is not None:
            body["parse_mode"] = parse_mode
        if reply_to:
            body["reply_parameters"] = json.dumps({"message_id": int(reply_to) if str(reply_to).isdigit() else reply_to})
        field = "photo" if method == "sendPhoto" else "document"
        from .channels.delivery import check_dispatch, telegram_response
        check_dispatch()
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/bot{token}/{method}",
                data=body,
                files={field: (file_name, data)},
            )
            return telegram_response(response)

    async def send_photo(self, token: str, chat_id: str, data: bytes, file_name: str, *, caption: str = "", reply_to: str | None = None, message_thread_id: int | None = None, parse_mode: str | None = None, caption_entities=None) -> Mapping[str, Any]:
        return await self._send_media("sendPhoto", token, chat_id, data, file_name, caption=caption, reply_to=reply_to, message_thread_id=message_thread_id, parse_mode=parse_mode, caption_entities=caption_entities)

    async def send_document(self, token: str, chat_id: str, data: bytes, file_name: str, *, caption: str = "", reply_to: str | None = None, message_thread_id: int | None = None, parse_mode: str | None = None, caption_entities=None) -> Mapping[str, Any]:
        return await self._send_media("sendDocument", token, chat_id, data, file_name, caption=caption, reply_to=reply_to, message_thread_id=message_thread_id, parse_mode=parse_mode, caption_entities=caption_entities)

    async def get_updates(self, token: str, *, offset: int | None = None, timeout: int = 25) -> list[Mapping[str, Any]]:
        params: dict[str, Any] = {"timeout": max(0, min(timeout, 50)), "allowed_updates": ["message", "edited_message"]}
        if offset is not None:
            params["offset"] = offset
        async with httpx.AsyncClient(timeout=max(35, timeout + 10)) as client:
            response = await client.get(f"{self.base_url}/bot{token}/getUpdates", params=params)
            response.raise_for_status()
            payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("description") or "Telegram 更新获取失败"))
        result = payload.get("result") or []
        return [item for item in result if isinstance(item, Mapping)]

    async def get_file(self, token: str, file_id: str) -> Mapping[str, Any]:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{self.base_url}/bot{token}/getFile", params={"file_id": file_id})
            response.raise_for_status()
            payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError("Telegram 附件信息获取失败")
        return payload.get("result") or {}

    async def download_file(self, token: str, file_path: str) -> tuple[bytes, str]:
        path = file_path.replace("\\", "/")
        if path.startswith("/") or any(part in {".", ".."} for part in path.split("/")):
            raise ValueError("Telegram 附件路径无效")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{self.base_url}/file/bot{token}/{path}")
            response.raise_for_status()
            if len(response.content) > MAX_ATTACHMENT_BYTES:
                raise ValueError("Telegram 附件超过 10 MB")
            return response.content, response.headers.get("content-type", "application/octet-stream").split(";", 1)[0]


MessageHandler = Callable[[ChannelThread, str, dict[str, Any], bool], Awaitable[None] | None]
CommandHandler = Callable[[ChannelThread, str, str], Awaitable[str] | str]
AdmissionHandler = Callable[[str], bool]


class MultichannelStore:
    """Durable account, identity, pairing and network thread state."""

    def __init__(self, database_path: str | Path, session_store: SessionStore, credentials: CredentialStore | None = None) -> None:
        self.database_path = str(database_path)
        self.sessions = session_store
        self.credentials = credentials or SystemCredentialStore()
        self._pairing_lock = RLock()
        self._pending_pairing: tuple[str, datetime] | None = None
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, object]]]] = {}
        # The application initializes the shared database before constructing
        # this service. Keep the service safe for isolated workers and tests as
        # well, so the first network event cannot hit a missing messages table.
        initialize_databases(Path(self.database_path).parent)
        self._initialize()
        from .channels.avatars import ChannelAvatarStore
        self.avatars = ChannelAvatarStore(self)
        from .channels.listening import GroupListeningStore
        self.listening = GroupListeningStore(self)
        from .channels.external_memory import ExternalMemoryRepository
        self.external_memory = ExternalMemoryRepository(self)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS channel_accounts (
                    account_id TEXT PRIMARY KEY, role_id TEXT NOT NULL, channel TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0, allow_private INTEGER NOT NULL DEFAULT 1,
                    allow_group INTEGER NOT NULL DEFAULT 0, blocked_senders_json TEXT NOT NULL DEFAULT '[]',
                    display_name TEXT NOT NULL DEFAULT '', platform_id TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, last_error TEXT, UNIQUE(role_id, channel)
                );
                CREATE TABLE IF NOT EXISTS channel_account_settings (
                    account_id TEXT PRIMARY KEY, config_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS channel_platform_settings (
                    channel TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS channel_identities (
                    identity_id TEXT PRIMARY KEY, role_id TEXT NOT NULL, channel TEXT NOT NULL,
                    scope TEXT NOT NULL, platform_user_id TEXT NOT NULL, account_id TEXT NOT NULL,
                    owner INTEGER NOT NULL DEFAULT 0, paired_at TEXT, context_since TEXT,
                    target_chat_id TEXT, updated_at TEXT NOT NULL,
                    UNIQUE(role_id, channel, scope, platform_user_id, account_id)
                );
                CREATE TABLE IF NOT EXISTS channel_pairing_codes (
                    code_hash TEXT PRIMARY KEY, role_id TEXT NOT NULL, channel TEXT NOT NULL,
                    expires_at TEXT NOT NULL, used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS channel_threads (
                    thread_id TEXT PRIMARY KEY, session_key TEXT NOT NULL UNIQUE, role_id TEXT NOT NULL,
                    channel TEXT NOT NULL, account_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                    chat_type TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', last_activity TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}', UNIQUE(role_id, channel, account_id, chat_id, chat_type)
                );
                CREATE TABLE IF NOT EXISTS channel_events (
                    event_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, direction TEXT NOT NULL,
                    external_message_id TEXT, created_at TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(thread_id, external_message_id)
                );
                CREATE TABLE IF NOT EXISTS channel_owner_chats (
                    role_id TEXT NOT NULL, channel TEXT NOT NULL, account_id TEXT NOT NULL,
                    scope TEXT NOT NULL, platform_user_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                    context_since TEXT NOT NULL, last_seen TEXT NOT NULL,
                    PRIMARY KEY(role_id,channel,account_id,scope,platform_user_id,chat_id)
                );
                CREATE TABLE IF NOT EXISTS channel_delivery_attempts (
                    attempt_id TEXT PRIMARY KEY, role_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                    account_id TEXT NOT NULL, target_chat_id TEXT NOT NULL, content TEXT NOT NULL,
                    status TEXT NOT NULL, platform_message_id TEXT, error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS channel_targets (
                    target_id TEXT PRIMARY KEY, role_id TEXT NOT NULL, channel TEXT NOT NULL,
                    account_id TEXT NOT NULL, kind TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '',
                    chat_id TEXT NOT NULL, capabilities_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    UNIQUE(account_id, target_id)
                );
                CREATE TABLE IF NOT EXISTS channel_member_names (
                    role_id TEXT NOT NULL, channel TEXT NOT NULL, account_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL, chat_type TEXT NOT NULL, sender_id TEXT NOT NULL,
                    name TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    PRIMARY KEY(role_id,channel,account_id,chat_id,chat_type,sender_id,name)
                );
                CREATE TABLE IF NOT EXISTS channel_member_name_schema (version INTEGER PRIMARY KEY);
                CREATE INDEX IF NOT EXISTS channel_member_names_role_seen ON channel_member_names(role_id,last_seen DESC);
                CREATE TABLE IF NOT EXISTS channel_group_settings (
                    role_id TEXT NOT NULL, account_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                    listening INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                    PRIMARY KEY(role_id,account_id,chat_id)
                );
                CREATE TABLE IF NOT EXISTS channel_group_context (
                    role_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                    group_note TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
                    summary_updated_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                    PRIMARY KEY(role_id, thread_id)
                );
                CREATE INDEX IF NOT EXISTS channel_threads_role_activity ON channel_threads(role_id, last_activity DESC);
                CREATE INDEX IF NOT EXISTS channel_events_thread_time ON channel_events(thread_id, created_at);
                CREATE INDEX IF NOT EXISTS channel_targets_account_seen ON channel_targets(account_id, last_seen DESC);
                CREATE INDEX IF NOT EXISTS channel_targets_role_account_id ON channel_targets(role_id,account_id,target_id);
                CREATE INDEX IF NOT EXISTS channel_targets_group_lookup ON channel_targets(role_id,account_id,kind,chat_id);
                """
            )
            self._migrate_thread_chat_type(db)
            self._migrate_member_names(db)
            group_columns = {row[1] for row in db.execute("PRAGMA table_info(channel_group_settings)")}
            for column, default in (("replies_enabled", 1), ("reply_revision", 0)):
                if column not in group_columns:
                    db.execute(f"ALTER TABLE channel_group_settings ADD COLUMN {column} INTEGER NOT NULL DEFAULT {default}")
            delivery_columns = {row[1] for row in db.execute("PRAGMA table_info(channel_delivery_attempts)")}
            for column in ("source_session_key", "source_run_id", "source_tool_call_id", "source_kind", "account_generation", "details_json"):
                if column not in delivery_columns:
                    db.execute(f"ALTER TABLE channel_delivery_attempts ADD COLUMN {column} TEXT")
            for table in ("channel_account_settings", "channel_platform_settings"):
                if "revision" not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
            # Platform identities describe the desktop owner, rather than a
            # role. Preserve the oldest binding when upgrading the previous
            # role/account-scoped representation to Shiori's global scope.
            identities = db.execute("SELECT * FROM channel_identities ORDER BY paired_at,identity_id").fetchall()
            known_identities: set[tuple[str, str, str, str]] = set()
            for identity in identities:
                binding_account = "" if identity["scope"] == "platform" else str(identity["account_id"])
                key = (str(identity["channel"]), str(identity["scope"]), str(identity["platform_user_id"]), binding_account)
                if key in known_identities:
                    db.execute("DELETE FROM channel_identities WHERE identity_id=?", (identity["identity_id"],))
                    continue
                known_identities.add(key)
            db.execute("UPDATE channel_identities SET account_id='' WHERE scope='platform'")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS channel_identity_global ON channel_identities(channel,scope,platform_user_id,account_id)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS channel_platform_account ON channel_accounts(channel,platform_id) WHERE platform_id IS NOT NULL AND platform_id<>''")
            # Legacy persisted codes must never become valid after a restart.
            db.execute("DELETE FROM channel_pairing_codes")
            # Existing role sessions are the desktop conversation. Expose
            # them as a thread without moving or rewriting any history.
            for row in db.execute("SELECT session_key, role_id, created_at, updated_at FROM sessions").fetchall():
                role_id = str(row["role_id"])
                db.execute(
                    "INSERT OR IGNORE INTO channel_threads(thread_id,session_key,role_id,channel,account_id,chat_id,chat_type,title,last_activity,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (f"desktop:{role_id}", str(row["session_key"]), role_id, "desktop", "", "desktop", "desktop", "桌面会话", str(row["updated_at"]), "{}"),
                )

    def _migrate_member_names(self, db: sqlite3.Connection) -> None:
        """Backfill once from host directories/events; profile names are model text."""
        if db.execute("SELECT 1 FROM channel_member_name_schema WHERE version=1").fetchone():
            return
        for row in db.execute("SELECT * FROM channel_targets WHERE kind='group_member'"):
            metadata = _loads(row["metadata_json"])
            self._remember_member_name(db, str(row["role_id"]), str(row["channel"]), str(row["account_id"]),
                str(metadata.get("group_id") or metadata.get("groupId") or ""), "group", str(row["chat_id"]),
                str(row["display_name"]), str(row["last_seen"]))
        for table, condition in (("channel_events", "WHERE e.direction='inbound'"),
                                 ("channel_listening_records", "")):
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                continue
            for row in db.execute(
                "SELECT t.role_id,t.channel,t.account_id,t.chat_id,t.chat_type,e.metadata_json,e.created_at "
                f"FROM {table} e JOIN channel_threads t ON t.thread_id=e.thread_id {condition}"
            ):
                metadata = _loads(row["metadata_json"])
                sender, name = metadata.get("senderId"), metadata.get("senderName")
                if isinstance(sender, str) and isinstance(name, str):
                    self._remember_member_name(db, str(row["role_id"]), str(row["channel"]), str(row["account_id"]),
                        str(row["chat_id"]), str(row["chat_type"]), sender, name, str(row["created_at"]))
        db.execute("INSERT INTO channel_member_name_schema VALUES(1)")

    @staticmethod
    def _migrate_thread_chat_type(db: sqlite3.Connection) -> None:
        legacy_columns = ["role_id", "channel", "account_id", "chat_id"]
        legacy_unique = any(
            bool(index["unique"]) and [str(row["name"]) for row in db.execute("SELECT name FROM pragma_index_info(?)", (index["name"],))] == legacy_columns
            for index in db.execute("PRAGMA index_list(channel_threads)").fetchall()
        )
        if not legacy_unique:
            return
        # SQLite cannot remove an inline UNIQUE constraint. Rebuild only the
        # directory table; stable thread/session IDs keep every history,
        # run, listening record and delivery reference unchanged.
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE channel_threads_with_chat_type (
            thread_id TEXT PRIMARY KEY, session_key TEXT NOT NULL UNIQUE, role_id TEXT NOT NULL,
            channel TEXT NOT NULL, account_id TEXT NOT NULL, chat_id TEXT NOT NULL,
            chat_type TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', last_activity TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}', UNIQUE(role_id,channel,account_id,chat_id,chat_type)
        )""")
        columns = "thread_id,session_key,role_id,channel,account_id,chat_id,chat_type,title,last_activity,metadata_json"
        db.execute(f"INSERT INTO channel_threads_with_chat_type({columns}) SELECT {columns} FROM channel_threads")
        db.execute("DROP TABLE channel_threads")
        db.execute("ALTER TABLE channel_threads_with_chat_type RENAME TO channel_threads")
        db.execute("CREATE INDEX channel_threads_role_activity ON channel_threads(role_id,last_activity DESC)")

    def channel_enabled(self, channel: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT enabled FROM channel_platform_settings WHERE channel=?", (channel,)).fetchone()
        return bool(row["enabled"]) if row is not None else True

    def set_channel_enabled(self, channel: str, enabled: bool) -> None:
        if channel not in {"telegram", "qq", "qqbot", "feishu"} or not isinstance(enabled, bool):
            raise ValueError("渠道或启停状态无效")
        with self._connect() as db:
            db.execute(
                "INSERT INTO channel_platform_settings(channel,enabled,updated_at,revision) VALUES(?,?,?,1) "
                "ON CONFLICT(channel) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at,revision=channel_platform_settings.revision+1",
                (channel, int(enabled), _now()),
            )

    def account_effectively_enabled(self, account: ChannelAccount) -> bool:
        return account.enabled and self.channel_enabled(account.channel)

    def register_desktop_thread(self, role_id: str, session_key: str, created_at: str, updated_at: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO channel_threads(thread_id,session_key,role_id,channel,account_id,chat_id,chat_type,title,last_activity,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET session_key=excluded.session_key,last_activity=excluded.last_activity",
                (f"desktop:{role_id}", session_key, role_id, "desktop", "", "desktop", "desktop", "桌面会话", updated_at or created_at, "{}"),
            )

    def delete_role(self, role_id: str) -> None:
        with self._connect() as db:
            accounts = [
                (str(row["account_id"]), str(row["channel"]))
                for row in db.execute("SELECT account_id,channel FROM channel_accounts WHERE role_id=?", (role_id,))
            ]
        # Clear secrets before deleting the account rows.  If the credential
        # vault is unavailable, keep the rows so the operation can be retried
        # and the orphaned secrets remain attributable to this role.
        for account_id, channel in accounts:
            self._clear_channel_credentials(channel, account_id, strict=True)
        self.avatars.delete_role(role_id)
        self.listening.delete_role(role_id)
        with self._connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            db.execute("DELETE FROM channel_member_names WHERE role_id=?", (role_id,))
            db.execute("DELETE FROM channel_external_revisions WHERE role_id=?", (role_id,))
            for table in ("channel_external_members", "channel_external_member_sources", "channel_external_member_candidates"):
                db.execute(f"DELETE FROM {table} WHERE role_id=?", (role_id,))
            if "channel_natural_quota" in tables:
                db.execute("DELETE FROM channel_natural_quota WHERE role_id=?", (role_id,))
            if "channel_member_profiles" in tables:
                db.execute("DELETE FROM channel_member_profiles WHERE role_id=?", (role_id,))
            if "channel_memory_cursors" in tables:
                db.execute("DELETE FROM channel_memory_cursors WHERE thread_id IN (SELECT thread_id FROM channel_threads WHERE role_id=?)", (role_id,))
            db.execute(
                "DELETE FROM messages WHERE session_key IN (SELECT session_key FROM channel_threads WHERE role_id=?)",
                (role_id,),
            )
            db.execute("DELETE FROM channel_events WHERE thread_id IN (SELECT thread_id FROM channel_threads WHERE role_id=?)", (role_id,))
            db.execute("DELETE FROM channel_threads WHERE role_id=?", (role_id,))
            db.execute("DELETE FROM channel_group_context WHERE role_id=?", (role_id,))
            db.execute("DELETE FROM channel_identities WHERE scope='account' AND account_id IN (SELECT account_id FROM channel_accounts WHERE role_id=?)", (role_id,))
            db.execute("DELETE FROM channel_owner_chats WHERE role_id=?", (role_id,))
            db.execute("DELETE FROM channel_delivery_attempts WHERE role_id=?", (role_id,))
            db.execute("DELETE FROM channel_targets WHERE role_id=?", (role_id,))
            db.execute("DELETE FROM channel_pairing_codes WHERE role_id=?", (role_id,))
            db.execute("DELETE FROM channel_account_settings WHERE account_id IN (SELECT account_id FROM channel_accounts WHERE role_id=?)", (role_id,))
            db.execute("DELETE FROM channel_accounts WHERE role_id=?", (role_id,))
            db.execute("DELETE FROM channel_group_settings WHERE role_id=?", (role_id,))

    def snapshot_role(self, role_id: str) -> ChannelRoleSnapshot:
        from .channels.role_snapshot import snapshot_role

        return snapshot_role(self, role_id)

    def restore_role(self, role_id: str, snapshot: ChannelRoleSnapshot) -> None:
        from .channels.role_snapshot import restore_role

        restore_role(self, role_id, snapshot)

    def _credential_key(self, account_id: str) -> str:
        return credential_key("telegram", account_id, "bot_token")

    def _clear_channel_credentials(self, channel: str, account_id: str, *, strict: bool = False) -> None:
        credential_names = {
            "telegram": ("bot_token",),
            "qq": ("access_token",),
            "qqbot": ("client_secret",),
            "feishu": ("app_secret", "verification_token", "encrypt_key"),
        }.get(channel, ())
        failures: list[str] = []
        for name in credential_names:
            try:
                self.credentials.set(credential_key(channel, account_id, name), "")
            except Exception as error:
                if strict:
                    failures.append(type(error).__name__)
        if failures:
            raise RuntimeError("渠道凭据清理失败：" + ", ".join(failures))

    def save_account(self, role_id: str, *, account_id: str | None = None, token: str | None = None,
                     enabled: bool = False, allow_private: bool = True, allow_group: bool = False,
                     blocked_senders: list[str] | None = None, display_name: str = "") -> ChannelAccount:
        now = _now()
        with self._connect() as db:
            existing = db.execute("SELECT account_id FROM channel_accounts WHERE role_id=? AND channel='telegram'", (role_id,)).fetchone()
            account_id = account_id or (str(existing[0]) if existing else f"telegram-{uuid.uuid4().hex}")
            owner = db.execute("SELECT role_id,channel FROM channel_accounts WHERE account_id=?", (account_id,)).fetchone()
            if owner is not None and (str(owner["role_id"]) != role_id or str(owner["channel"]) != "telegram"):
                raise ValueError("渠道账号 ID 已属于其他角色或平台")
            if token is not None and not token.strip():
                raise ValueError("Telegram Bot Token 不能为空")
        old_token = self.credentials.get(self._credential_key(account_id))
        policy = (int(enabled), int(allow_private), int(allow_group), _json(sorted(set(blocked_senders or []))))
        try:
            if token is not None:
                self.credentials.set(self._credential_key(account_id), token.strip())
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                previous = db.execute("SELECT enabled,allow_private,allow_group,blocked_senders_json FROM channel_accounts WHERE account_id=?", (account_id,)).fetchone()
                db.execute(
                    """INSERT INTO channel_accounts(account_id,role_id,channel,enabled,allow_private,allow_group,blocked_senders_json,display_name,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET enabled=excluded.enabled,allow_private=excluded.allow_private,
                       allow_group=excluded.allow_group,blocked_senders_json=excluded.blocked_senders_json,display_name=excluded.display_name,updated_at=excluded.updated_at""",
                    (account_id, role_id, "telegram", *policy, display_name.strip(), now, now),
                )
                if previous is None or tuple(previous) != policy or token is not None and token.strip() != old_token:
                    self._advance_account_revision(db, account_id)
        except Exception:
            try:
                self.credentials.set(self._credential_key(account_id), old_token)
            except Exception:
                pass
            raise
        return self.get_account(account_id)  # type: ignore[return-value]

    def set_channel_config(self, account_id: str, config: dict[str, Any]) -> None:
        with self._connect() as db:
            if db.execute("SELECT 1 FROM channel_accounts WHERE account_id=?", (account_id,)).fetchone() is None:
                raise KeyError(account_id)
            db.execute(
                "INSERT INTO channel_account_settings(account_id,config_json,revision) VALUES(?,?,1) ON CONFLICT(account_id) DO UPDATE SET config_json=excluded.config_json,revision=channel_account_settings.revision+1",
                (account_id, _json(config)),
            )

    def channel_config(self, account_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT config_json FROM channel_account_settings WHERE account_id=?", (account_id,)).fetchone()
        return _loads(str(row[0])) if row else {}

    def directory_revision(self, account_id: str, channel: str) -> tuple[int, int]:
        """Monotonic config/permission generations prevent cursor revival."""
        with self._connect() as db:
            account = db.execute("SELECT revision FROM channel_account_settings WHERE account_id=?", (account_id,)).fetchone()
            platform = db.execute("SELECT revision FROM channel_platform_settings WHERE channel=?", (channel,)).fetchone()
        return (int(account[0]) if account else 0, int(platform[0]) if platform else 0)

    @staticmethod
    def _advance_account_revision(db: sqlite3.Connection, account_id: str) -> None:
        """Invalidate connection evidence for real credential/policy changes."""
        db.execute("INSERT INTO channel_account_settings(account_id,config_json,revision) VALUES(?,'{}',1) "
            "ON CONFLICT(account_id) DO UPDATE SET revision=channel_account_settings.revision+1", (account_id,))

    def set_group_listening(self, role_id: str, account_id: str, chat_id: str, enabled: bool, *,
                            operator: str = "user", authorize: Callable[[], None] | None = None) -> None:
        if operator not in {"user", "role"}:
            raise ValueError("未知旁听操作方")
        account = self.get_account(account_id)
        if account is None or account.role_id != role_id or account.channel != "qq":
            raise KeyError(account_id)
        thread = self.get_thread_by_chat(role_id, account_id, str(chat_id), chat_type="group")
        if thread is None and authorize is not None:
            raise KeyError(chat_id)
        if thread is None and self.group_listening(role_id, account_id, chat_id) != bool(enabled):
            thread = self.ensure_channel_thread(role_id, "qq", account_id, str(chat_id), "group")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if authorize is not None:
                authorize()
            account = self.get_account(account_id)
            if account is None or account.role_id != role_id or account.channel != "qq":
                raise KeyError(account_id)
            if thread is not None:
                current = self.get_thread(role_id, thread.thread_id)
                if current is None or (current.account_id, current.channel, current.chat_id, current.chat_type) != (account_id, "qq", str(chat_id), "group"):
                    raise KeyError(chat_id)
            row = db.execute("SELECT listening FROM channel_group_settings WHERE role_id=? AND account_id=? AND chat_id=?",
                (role_id, account_id, str(chat_id))).fetchone()
            changed = bool(row and row[0]) != bool(enabled)
            now = _now()
            db.execute(
                "INSERT INTO channel_group_settings(role_id,account_id,chat_id,listening,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(role_id,account_id,chat_id) DO UPDATE SET listening=excluded.listening,updated_at=excluded.updated_at",
                (role_id, account_id, str(chat_id), int(enabled), now),
            )
            if changed and thread is not None:
                db.execute("INSERT INTO channel_listening_toggles VALUES(?,?,?,?)", (thread.thread_id, int(enabled), operator, now))

        if changed and thread is not None:
            self.listening.invalidate(thread.thread_id)

    def group_listening(self, role_id: str, account_id: str, chat_id: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT listening FROM channel_group_settings WHERE role_id=? AND account_id=? AND chat_id=?", (role_id, account_id, str(chat_id))).fetchone()
        return bool(row and row[0])

    def group_reply_policy(self, role_id: str, account_id: str, chat_id: str) -> tuple[bool, int]:
        """A group preference and its generation; older groups keep replying."""
        with self._connect() as db:
            row = db.execute(
                "SELECT replies_enabled,reply_revision FROM channel_group_settings WHERE role_id=? AND account_id=? AND chat_id=?",
                (role_id, account_id, str(chat_id)),
            ).fetchone()
        return (bool(row[0]), int(row[1])) if row else (True, 0)

    def set_group_replies(self, role_id: str, account_id: str, chat_id: str, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("群回复开关必须是布尔值")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            account = self.get_account(account_id)
            if account is None or account.role_id != role_id or account.channel != "qq":
                raise KeyError(account_id)
            current, revision = self.group_reply_policy(role_id, account_id, chat_id)
            changed = current != enabled
            db.execute(
                "INSERT INTO channel_group_settings(role_id,account_id,chat_id,replies_enabled,reply_revision,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(role_id,account_id,chat_id) DO UPDATE SET "
                "replies_enabled=excluded.replies_enabled,reply_revision=excluded.reply_revision,updated_at=excluded.updated_at",
                (role_id, account_id, str(chat_id), int(enabled), revision + int(changed), _now()),
            )
        thread = self.get_thread_by_chat(role_id, account_id, str(chat_id), chat_type="group")
        if changed and thread is not None:
            self.listening.invalidate(thread.thread_id)

    def list_group_settings(self, role_id: str, account_id: str) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT chat_id,listening,updated_at FROM channel_group_settings WHERE role_id=? AND account_id=? ORDER BY updated_at DESC",
                (role_id, account_id),
            ).fetchall()
        return [{"chatId": str(row[0]), "listening": bool(row[1]), "updatedAt": str(row[2])} for row in rows]

    def register_channel_account(
        self,
        role_id: str,
        channel: str,
        *,
        account_id: str,
        enabled: bool = False,
        allow_private: bool = True,
        allow_group: bool = False,
        blocked_senders: list[str] | None = None,
        display_name: str = "",
        platform_id: str | None = None,
    ) -> ChannelAccount:
        """Register a non-Telegram adapter account without storing credentials.

        Channel configuration APIs own their secret fields and put them in the
        credential store. This method persists only routing and policy state.
        """

        if not channel or channel == "telegram" or not account_id:
            raise ValueError("渠道或账号 ID 无效")
        now = _now()
        policy = (int(enabled), int(allow_private), int(allow_group), _json(sorted(set(blocked_senders or []))), platform_id)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT role_id,channel FROM channel_accounts WHERE account_id=?", (account_id,)).fetchone()
            if existing is not None and (str(existing["role_id"]) != role_id or str(existing["channel"]) != channel):
                raise ValueError("渠道账号 ID 已属于其他角色或平台")
            self._check_platform_identity(db, account_id, channel, platform_id)
            previous = db.execute("SELECT enabled,allow_private,allow_group,blocked_senders_json,platform_id FROM channel_accounts WHERE account_id=?", (account_id,)).fetchone()
            db.execute(
                """INSERT INTO channel_accounts(account_id,role_id,channel,enabled,allow_private,allow_group,blocked_senders_json,display_name,platform_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET enabled=excluded.enabled,
                   allow_private=excluded.allow_private,allow_group=excluded.allow_group,
                   blocked_senders_json=excluded.blocked_senders_json,display_name=excluded.display_name,
                   platform_id=excluded.platform_id,updated_at=excluded.updated_at""",
                (account_id, role_id, channel, int(enabled), int(allow_private), int(allow_group),
                 _json(sorted(set(blocked_senders or []))), display_name.strip(), platform_id, now, now),
            )
            if previous is None or tuple(previous) != policy:
                self._advance_account_revision(db, account_id)
        return self.get_account(account_id)  # type: ignore[return-value]

    @staticmethod
    def _check_platform_identity(db: sqlite3.Connection, account_id: str, channel: str, platform_id: str | None) -> None:
        if not platform_id:
            return
        current = db.execute("SELECT platform_id FROM channel_accounts WHERE account_id=?", (account_id,)).fetchone()
        if current is not None and current[0] and str(current[0]) != platform_id:
            raise ValueError("渠道账号的平台身份不可替换，请先删除原账号再添加新账号")
        other = db.execute("SELECT account_id FROM channel_accounts WHERE channel=? AND platform_id=? AND account_id<>?", (channel, platform_id, account_id)).fetchone()
        if other is not None:
            raise ValueError("平台账号已属于另一个角色或渠道账号")

    def set_platform_identity(self, account_id: str, platform_id: str, *, display_name: str | None = None) -> ChannelAccount:
        """Record the transport-verified account identity with unique ownership."""

        platform_id = str(platform_id).strip()
        if not platform_id:
            raise ValueError("平台账号身份不能为空")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            account = db.execute("SELECT channel FROM channel_accounts WHERE account_id=?", (account_id,)).fetchone()
            if account is None:
                raise KeyError(account_id)
            self._check_platform_identity(db, account_id, str(account[0]), platform_id)
            db.execute("UPDATE channel_accounts SET platform_id=?,display_name=COALESCE(?,display_name),last_error=NULL,updated_at=? WHERE account_id=?", (platform_id, display_name, _now(), account_id))
        return self.get_account(account_id)  # type: ignore[return-value]

    def get_account(self, account_id: str) -> ChannelAccount | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM channel_accounts WHERE account_id=?", (account_id,)).fetchone()
        return self._account(row) if row else None

    def get_role_account(self, role_id: str, channel: str = "telegram") -> ChannelAccount | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM channel_accounts WHERE role_id=? AND channel=?", (role_id, channel)).fetchone()
        return self._account(row) if row else None

    def list_role_accounts(self, role_id: str) -> list[ChannelAccount]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM channel_accounts WHERE role_id=? ORDER BY channel,account_id", (role_id,)).fetchall()
        return [self._account(row) for row in rows]

    def list_role_accounts_for_all_roles(self) -> list[ChannelAccount]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM channel_accounts ORDER BY role_id, channel, account_id").fetchall()
        return [self._account(row) for row in rows]

    def remember_inbound_member(self, role_id: str, event: InboundEvent) -> None:
        """Preserve an admitted event's name before directory refresh replaces it."""
        account = self.get_account(event.account_id)
        if account is None or (account.role_id, account.channel) != (role_id, event.channel):
            raise PermissionError("member observation does not belong to this role account")
        with self._connect() as db:
            self._remember_member_name(db, role_id, event.channel, event.account_id,
                event.chat_id, event.chat_type.value, event.sender_id, event.sender_name, _now())
            thread = self.get_thread_by_chat(role_id, event.account_id, event.chat_id, chat_type=event.chat_type.value)
            if thread is not None:
                self.external_memory.members.observe_in(db, thread, {"senderId": event.sender_id,
                    "senderName": event.sender_name, "viaAccount": account.source_snapshot()}, _now())

    def remember_targets(self, role_id: str, targets: list[Any], *, authorized: Callable[[], bool] | None = None) -> None:
        """Persist an adapter's observed/reachable target directory."""

        now = _now()
        with self._connect() as db:
            if authorized is not None and not authorized():
                raise PermissionError("directory_permission_revoked")
            for target in targets:
                account_id = str(getattr(target, "account_id", ""))
                target_id = str(getattr(target, "id", ""))
                if not account_id or not target_id:
                    continue
                kind = getattr(getattr(target, "kind", None), "value", getattr(target, "kind", ""))
                capabilities = [getattr(item, "value", str(item)) for item in (getattr(target, "capabilities", ()) or ())]
                metadata = getattr(target, "metadata", {}) or {}
                row = db.execute("SELECT first_seen,display_name FROM channel_targets WHERE target_id=?", (target_id,)).fetchone()
                first_seen = str(row[0]) if row else now
                account = db.execute("SELECT channel FROM channel_accounts WHERE account_id=? AND role_id=?", (account_id, role_id)).fetchone()
                if account is None:
                    continue
                db.execute(
                    """INSERT INTO channel_targets(target_id,role_id,channel,account_id,kind,display_name,chat_id,capabilities_json,metadata_json,first_seen,last_seen)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(target_id) DO UPDATE SET role_id=excluded.role_id,channel=excluded.channel,
                       kind=excluded.kind,display_name=excluded.display_name,chat_id=excluded.chat_id,
                       capabilities_json=excluded.capabilities_json,metadata_json=excluded.metadata_json,last_seen=excluded.last_seen""",
                    (target_id, role_id, str(account["channel"]), account_id, str(kind),
                     str(getattr(target, "display_name", "")), str(getattr(target, "chat_id", "")),
                     _json(capabilities), _json(metadata), first_seen, now),
                )
                if kind == "group_member":
                    name = str(getattr(target, "display_name", ""))
                    observed_at = now if row is None or str(row["display_name"]) != name else None
                    raw_time = metadata.get("name_observed_at")
                    if isinstance(raw_time, str):
                        try:
                            stamp = datetime.fromisoformat(raw_time)
                            if stamp.tzinfo is not None:
                                observed_at = min(now, stamp.astimezone(timezone.utc).isoformat())
                        except ValueError:
                            pass
                    # Cached directory entries retain their observation time;
                    # refreshing another member is not a new name observation.
                    if observed_at is not None:
                        self._remember_member_name(db, role_id, str(account["channel"]), account_id,
                            str(metadata.get("group_id") or metadata.get("groupId") or ""), "group",
                            str(getattr(target, "chat_id", "")), name, observed_at)
            if authorized is not None and not authorized():
                raise PermissionError("directory_permission_revoked")

    @staticmethod
    def _remember_member_name(db: sqlite3.Connection, role_id: str, channel: str, account_id: str,
                              chat_id: str, chat_type: str, sender_id: str, name: str, now: str) -> None:
        """Record a host observation, never a model-generated profile name."""
        if not chat_id or not sender_id or not name.strip():
            return
        now = datetime.fromisoformat(now).astimezone(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO channel_member_names VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(role_id,channel,account_id,chat_id,chat_type,sender_id,name) DO UPDATE SET "
            "first_seen=min(channel_member_names.first_seen,excluded.first_seen),last_seen=max(channel_member_names.last_seen,excluded.last_seen)",
            (role_id, channel, account_id, chat_id, chat_type, sender_id, name, now, now),
        )

    def list_targets(self, role_id: str, account_id: str, *, limit: int | None = None, stable: bool = False,
                     max_bytes: int | None = None) -> list[Any]:
        """Read one role-owned account's durable target directory."""

        from .channels.models import Target, TargetKind

        with self._connect() as db:
            if max_bytes is not None:
                db.execute("BEGIN")
            order = "target_id" if stable else "last_seen DESC, target_id"
            sql = f"SELECT * FROM channel_targets WHERE role_id=? AND account_id=? ORDER BY {order}"
            parameters: tuple = (role_id, account_id)
            if limit is not None:
                sql += " LIMIT ?"
                parameters += (limit,)
            if max_bytes is not None:
                size = db.execute("SELECT COALESCE(SUM(length(CAST(display_name AS BLOB))+length(CAST(metadata_json AS BLOB))"
                    "+length(CAST(capabilities_json AS BLOB))+length(CAST(target_id AS BLOB))+length(CAST(chat_id AS BLOB))+128),0) FROM ("
                    + sql + ")", parameters).fetchone()[0]
                if size > max_bytes:
                    from .channels.directory import DirectoryError
                    raise DirectoryError("directory_too_large")
            rows = db.execute(sql, parameters).fetchall()
        result: list[Target] = []
        for row in rows:
            try:
                kind = TargetKind(str(row["kind"]))
            except ValueError:
                continue
            try:
                capabilities = frozenset(TargetKind(str(value)) for value in json.loads(row["capabilities_json"] or "[]"))
            except (TypeError, ValueError):
                capabilities = frozenset({kind})
            result.append(Target(
                id=str(row["target_id"]), kind=kind, display_name=str(row["display_name"]),
                account_id=str(row["account_id"]), chat_id=str(row["chat_id"]),
                capabilities=capabilities, metadata=_loads(row["metadata_json"]),
            ))
        return result

    def owner_target(self, role_id: str, account_id: str) -> Any | None:
        """Resolve the paired owner's private chat for one role account."""

        return next(iter(self.owner_targets(role_id, account_id)), None)

    def known_group(self, role_id: str, account_id: str, group_id: str) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM channel_targets WHERE role_id=? AND account_id=? AND kind='group' AND chat_id=? LIMIT 1",
                (role_id, account_id, group_id)).fetchone() is not None

    def owner_targets(self, role_id: str, account_id: str) -> list[Any]:
        """Return every currently bound private target, newest chat first."""

        account = self.get_account(account_id)
        if account is None or account.role_id != role_id:
            return []
        with self._connect() as db:
            rows = db.execute(
                """SELECT c.chat_id FROM channel_owner_chats c
                   JOIN channel_identities i
                     ON i.channel=c.channel AND (i.scope='platform' OR i.account_id=c.account_id)
                    AND i.scope=c.scope AND i.platform_user_id=c.platform_user_id AND i.owner=1
                   WHERE c.role_id=? AND c.account_id=?
                   ORDER BY c.last_seen DESC""",
                (role_id, account_id),
            ).fetchall()
        directory = {target.chat_id: target for target in self.list_targets(role_id, account_id) if target.kind.value == "private"}
        return [directory[str(row[0])] for row in rows if str(row[0]) in directory]

    def get_target(self, role_id: str, account_id: str, target_id: str) -> Any | None:
        return next((item for item in self.list_targets(role_id, account_id) if item.id == target_id), None)

    def token(self, account_id: str) -> str:
        return self.credentials.get(self._credential_key(account_id))

    def set_account_state(self, account_id: str, *, enabled: bool | None = None, last_error: str | None = None) -> ChannelAccount:
        account = self.get_account(account_id)
        if account is None:
            raise KeyError(account_id)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT enabled FROM channel_accounts WHERE account_id=?", (account_id,)).fetchone()
            if current is None:
                raise KeyError(account_id)
            if enabled is not None and bool(current[0]) != enabled:
                self._advance_account_revision(db, account_id)
            db.execute("UPDATE channel_accounts SET enabled=COALESCE(?,enabled),last_error=?,updated_at=? WHERE account_id=?", (None if enabled is None else int(enabled), last_error, _now(), account_id))
        return self.get_account(account_id)  # type: ignore[return-value]

    def delete_account(self, account_id: str) -> None:
        with self._connect() as db:
            account = db.execute("SELECT role_id,channel FROM channel_accounts WHERE account_id=?", (account_id,)).fetchone()
        if account is None:
            return
        channel = str(account["channel"])
        role_id = str(account["role_id"])
        # Clear secrets before removing the account row. If the system vault
        # fails, callers can leave the account disabled and retry cleanup.
        self._clear_channel_credentials(channel, account_id, strict=True)
        with self._connect() as db:
            for row in db.execute("SELECT thread_id,metadata_json FROM channel_threads WHERE account_id=?", (account_id,)).fetchall():
                thread_metadata = _loads(row["metadata_json"])
                thread_metadata["accountRemoved"] = True
                db.execute("UPDATE channel_threads SET metadata_json=? WHERE thread_id=?", (_json(thread_metadata), row["thread_id"]))
            db.execute("DELETE FROM channel_account_settings WHERE account_id=?", (account_id,))
            db.execute("DELETE FROM channel_group_settings WHERE account_id=?", (account_id,))
            db.execute("DELETE FROM channel_accounts WHERE account_id=?", (account_id,))
            db.execute("DELETE FROM channel_identities WHERE scope='account' AND account_id=?", (account_id,))
            db.execute("DELETE FROM channel_owner_chats WHERE account_id=?", (account_id,))
            db.execute("DELETE FROM channel_targets WHERE account_id=?", (account_id,))
            remaining = db.execute("SELECT 1 FROM channel_accounts WHERE role_id=? AND channel=? LIMIT 1", (role_id, channel)).fetchone()
            if remaining is None:
                db.execute("DELETE FROM channel_pairing_codes WHERE role_id=? AND channel=?", (role_id, channel))

    def create_pairing_code(self, role_id: str = "", channel: str = "telegram", *, ttl_seconds: int = 600) -> str:
        del role_id, channel  # Issuing UI location does not limit the receiver.
        if ttl_seconds < 1:
            raise ValueError("配对码有效期必须为正数")
        code = "MEIDO-" + secrets.token_hex(4).upper()
        digest = hashlib.sha256(code.encode()).hexdigest()
        expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        with self._pairing_lock:
            self._pending_pairing = (digest, expires)
        return code

    def consume_pairing_code(self, code: str, *, role_id: str, channel: str, scope: str, platform_user_id: str, account_id: str, chat_id: str) -> bool:
        if scope not in {"platform", "account"} or not platform_user_id.strip() or not chat_id.strip():
            raise ValueError("渠道身份格式无效")
        digest = hashlib.sha256(code.strip().upper().encode()).hexdigest()
        now = _now()
        with self._pairing_lock:
            pending = self._pending_pairing
            if pending is None or pending[1] <= datetime.now(timezone.utc):
                self._pending_pairing = None
                return False
            if not secrets.compare_digest(digest, pending[0]):
                return False
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                account = db.execute("SELECT role_id,channel FROM channel_accounts WHERE account_id=?", (account_id,)).fetchone()
                if account is None or (str(account["role_id"]), str(account["channel"])) != (role_id, channel):
                    return False
                db.execute(
                    """INSERT INTO channel_identities(identity_id,role_id,channel,scope,platform_user_id,account_id,owner,paired_at,context_since,target_chat_id,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(channel,scope,platform_user_id,account_id) DO UPDATE SET owner=1,paired_at=excluded.paired_at,target_chat_id=excluded.target_chat_id,updated_at=excluded.updated_at""",
                    (f"identity-{uuid.uuid4().hex}", role_id, channel, scope, platform_user_id, "" if scope == "platform" else account_id, 1, now, now, chat_id, now),
                )
                db.execute(
                    "INSERT INTO channel_owner_chats(role_id,channel,account_id,scope,platform_user_id,chat_id,context_since,last_seen) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(role_id,channel,account_id,scope,platform_user_id,chat_id) DO UPDATE SET last_seen=excluded.last_seen",
                    (role_id, channel, account_id, scope, platform_user_id, chat_id, now, now),
                )
                self.external_memory.invalidate_identity_in(db, channel, scope, account_id, platform_user_id)
            self._pending_pairing = None
            return True

    def identity_is_owner(self, *, role_id: str, channel: str, scope: str, platform_user_id: str, account_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                """SELECT i.owner FROM channel_identities i JOIN channel_accounts a
                   ON a.account_id=? AND a.role_id=? AND a.channel=i.channel
                   WHERE i.channel=? AND i.scope=? AND i.platform_user_id=?
                   AND (i.scope='platform' OR i.account_id=a.account_id)""",
                (account_id, role_id, channel, scope, platform_user_id),
            ).fetchone()
        return bool(row and row[0])

    def list_identities(self) -> list[dict[str, object]]:
        """List the desktop owner's global platform and application bindings."""

        with self._connect() as db:
            rows = db.execute("SELECT * FROM channel_identities WHERE owner=1 ORDER BY paired_at,identity_id").fetchall()
            chats = db.execute("SELECT * FROM channel_owner_chats ORDER BY last_seen").fetchall()
        return [
            {
                "identityId": str(row["identity_id"]), "channel": str(row["channel"]),
                "scope": str(row["scope"]), "platformUserId": str(row["platform_user_id"]),
                "accountId": str(row["account_id"]) or None, "pairedAt": row["paired_at"],
                "chats": [
                    {"roleId": str(chat["role_id"]), "accountId": str(chat["account_id"]), "chatId": str(chat["chat_id"]), "contextSince": str(chat["context_since"])}
                    for chat in chats
                    if (chat["channel"], chat["scope"], chat["platform_user_id"]) == (row["channel"], row["scope"], row["platform_user_id"])
                    and (row["scope"] == "platform" or chat["account_id"] == row["account_id"])
                ],
            }
            for row in rows
        ]

    def unbind_identity(self, identity_id: str) -> None:
        """Revoke a binding and its private-context/owner-delivery grants."""

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            identity = db.execute("SELECT * FROM channel_identities WHERE identity_id=?", (identity_id,)).fetchone()
            if identity is None:
                raise KeyError(identity_id)
            self.external_memory.invalidate_identity_in(db, identity["channel"], identity["scope"], identity["account_id"], identity["platform_user_id"])
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_push_targets'").fetchone():
                db.execute(
                    """DELETE FROM channel_push_targets WHERE EXISTS (
                         SELECT 1 FROM channel_targets t JOIN channel_owner_chats c
                           ON c.role_id=t.role_id AND c.account_id=t.account_id AND c.chat_id=t.chat_id
                         WHERE t.target_id=channel_push_targets.target_id AND t.kind='private'
                           AND c.channel=? AND c.scope=? AND c.platform_user_id=?
                           AND (?='platform' OR c.account_id=?)
                       )""",
                    (identity["channel"], identity["scope"], identity["platform_user_id"], identity["scope"], identity["account_id"]),
                )
            db.execute("DELETE FROM channel_owner_chats WHERE channel=? AND scope=? AND platform_user_id=? AND (?='platform' OR account_id=?)", (identity["channel"], identity["scope"], identity["platform_user_id"], identity["scope"], identity["account_id"]))
            db.execute("DELETE FROM channel_identities WHERE identity_id=?", (identity_id,))
        for account in self.list_role_accounts_for_all_roles():
            if account.channel == identity["channel"] and (identity["scope"] == "platform" or account.account_id == identity["account_id"]):
                self.publish(account.role_id, {"type": "identities_updated", "roleId": account.role_id})

    def remember_owner_chat(self, *, role_id: str, channel: str, scope: str, platform_user_id: str, account_id: str, chat_id: str) -> None:
        if not self.identity_is_owner(role_id=role_id, channel=channel, scope=scope, platform_user_id=platform_user_id, account_id=account_id):
            return
        now = _now()
        with self._connect() as db:
            db.execute(
                "INSERT INTO channel_owner_chats(role_id,channel,account_id,scope,platform_user_id,chat_id,context_since,last_seen) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(role_id,channel,account_id,scope,platform_user_id,chat_id) DO UPDATE SET last_seen=excluded.last_seen",
                (role_id, channel, account_id, scope, platform_user_id, chat_id, now, now),
            )

    def owner_chat_context_since(self, *, role_id: str, channel: str, account_id: str, chat_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT c.context_since FROM channel_owner_chats c JOIN channel_identities i
                   ON i.channel=c.channel AND (i.scope='platform' OR i.account_id=c.account_id)
                   AND i.scope=c.scope AND i.platform_user_id=c.platform_user_id AND i.owner=1
                   WHERE c.role_id=? AND c.channel=? AND c.account_id=? AND c.chat_id=?
                   ORDER BY c.last_seen DESC LIMIT 1""",
                (role_id, channel, account_id, chat_id),
            ).fetchone()
        return str(row[0]) if row else None

    def owner_target_binding(self, role_id: str, account_id: str, chat_id: str) -> dict[str, str] | None:
        """Freeze only the selected owner destination's binding generation."""
        with self._connect() as db:
            row = db.execute('''SELECT i.identity_id,i.paired_at,c.context_since
                FROM channel_owner_chats c JOIN channel_identities i
                ON i.channel=c.channel AND (i.scope='platform' OR i.account_id=c.account_id)
                AND i.scope=c.scope AND i.platform_user_id=c.platform_user_id AND i.owner=1
                WHERE c.role_id=? AND c.account_id=? AND c.chat_id=? ORDER BY c.last_seen DESC LIMIT 1''',
                (role_id, account_id, chat_id)).fetchone()
        return {'identityId': str(row[0]), 'pairedAt': str(row[1]), 'contextSince': str(row[2])} if row else None

    def owner_context(self, role_id: str, *, exclude_thread_id: str, limit: int = 24) -> list[tuple[str, str, str, str]]:
        """Return recent desktop and paired-private text after each pairing boundary."""

        with self._connect() as db:
            rows = db.execute(
                """SELECT t.title,m.role,CASE WHEN json_extract(m.metadata_json,'$.partialDelivery')=1 THEN json_extract(m.metadata_json,'$.deliveredContent') ELSE m.content END AS content,m.created_at
                   FROM channel_threads t JOIN messages m ON m.session_key=t.session_key
                   WHERE t.role_id=? AND t.thread_id<>? AND m.message_type='text'
                     AND (m.status='completed' OR (m.role='assistant' AND json_extract(m.metadata_json,'$.partialDelivery')=1))
                     AND (t.channel='desktop' OR m.role<>'assistant' OR json_extract(m.metadata_json,'$.deliveryStatus')='sent' OR json_extract(m.metadata_json,'$.partialDelivery')=1)
                     AND (t.channel='desktop' OR (t.chat_type='private' AND EXISTS (
                       SELECT 1 FROM channel_owner_chats c JOIN channel_identities i
                         ON i.channel=c.channel AND (i.scope='platform' OR i.account_id=c.account_id)
                         AND i.scope=c.scope AND i.platform_user_id=c.platform_user_id AND i.owner=1
                       WHERE c.role_id=t.role_id AND c.channel=t.channel AND c.account_id=t.account_id
                         AND c.chat_id=t.chat_id AND m.created_at>=c.context_since
                         AND (m.run_id IS NULL OR EXISTS (
                           SELECT 1 FROM messages source
                           WHERE source.run_id=m.run_id AND source.session_key=m.session_key
                             AND source.role='user' AND source.message_type='text'
                             AND source.created_at>=c.context_since
                         ))
                     )))
                   ORDER BY m.created_at DESC LIMIT ?""",
                (role_id, exclude_thread_id, max(1, min(limit, 100))),
            ).fetchall()
        return [(str(row["title"]), str(row["role"]), str(row["content"]), str(row["created_at"])) for row in reversed(rows)]

    def owner_context_messages(self, role_id: str) -> list[Message]:
        """Read authorised owner history with a stable maintenance-only order.

        SQLite row IDs provide a monotonic cursor across transcripts. Stored
        message sequences and their original session references stay intact.
        Group and visitor transcripts never enter this owner memory view.
        """

        with self._connect() as db:
            rows = db.execute(
                """SELECT m.rowid AS context_sequence,m.message_id,m.session_key FROM messages m
                   WHERE (m.status='completed' OR (m.role='assistant' AND json_extract(m.metadata_json,'$.partialDelivery')=1))
                     AND m.message_type='text'
                     AND (m.session_key=? OR EXISTS (
                       SELECT 1 FROM channel_threads t JOIN channel_owner_chats c
                         ON c.role_id=t.role_id AND c.account_id=t.account_id AND c.chat_id=t.chat_id
                       JOIN channel_identities i ON i.channel=c.channel AND i.scope=c.scope
                         AND i.platform_user_id=c.platform_user_id AND i.owner=1
                         AND (i.scope='platform' OR i.account_id=c.account_id)
                       WHERE t.role_id=? AND t.chat_type='private' AND t.session_key=m.session_key
                         AND m.created_at>=c.context_since
                         AND (m.run_id IS NULL OR EXISTS (
                           SELECT 1 FROM messages source
                           WHERE source.run_id=m.run_id AND source.session_key=m.session_key
                             AND source.role='user' AND source.message_type='text'
                             AND source.created_at>=c.context_since
                         ))
                         AND (m.role<>'assistant' OR json_extract(m.metadata_json,'$.deliveryStatus')='sent' OR json_extract(m.metadata_json,'$.partialDelivery')=1)
                     )) ORDER BY m.rowid""",
                (f"role:{role_id}", role_id),
            ).fetchall()
        sessions = {str(row["session_key"]) for row in rows}
        from .channels.delivery_projection import delivered_message
        messages = {message.id: message for key in sessions for item in self.sessions.list_messages(key)
                    if (message := delivered_message(item)) is not None}
        return [
            messages[str(row["message_id"])].model_copy(update={
                "sequence": int(row["context_sequence"]),
                "metadata": {
                    **messages[str(row["message_id"])].metadata,
                    "contextSequenceSource": "global-rowid",
                    "sourceSessionKey": str(row["session_key"]),
                    "sourceSequence": messages[str(row["message_id"])].sequence,
                },
            })
            for row in rows if str(row["message_id"]) in messages
        ]

    def ensure_thread(self, role_id: str, account_id: str, chat_id: str, chat_type: str, *, title: str = "", metadata: dict[str, Any] | None = None) -> ChannelThread:
        return self.ensure_channel_thread(role_id, "telegram", account_id, chat_id, chat_type, title=title, metadata=metadata)

    def ensure_channel_thread(self, role_id: str, channel: str, account_id: str, chat_id: str, chat_type: str, *, title: str = "", metadata: dict[str, Any] | None = None) -> ChannelThread:
        if chat_type not in {"private", "group"}:
            raise ValueError("外部会话类型必须是 private 或 group")
        account = self.get_account(account_id)
        if account is None or account.role_id != role_id or account.channel != channel:
            raise KeyError(account_id)
        now = _now()
        with self._connect() as db:
            row = db.execute("SELECT * FROM channel_threads WHERE role_id=? AND channel=? AND account_id=? AND chat_id=? AND chat_type=?", (role_id, channel, account_id, str(chat_id), chat_type)).fetchone()
            thread_metadata = {**(_loads(row["metadata_json"]) if row else {}), **(metadata or {})}
            thread_metadata["viaAccount"] = account.source_snapshot()
            thread_metadata["accountRemoved"] = False
            if row:
                db.execute("UPDATE channel_threads SET title=COALESCE(NULLIF(?,''),title),metadata_json=?,last_activity=? WHERE thread_id=?", (title, _json(thread_metadata), now, row["thread_id"]))
            else:
                thread_id = f"thread-{chat_type}-{uuid.uuid4().hex}"
                session_key = f"thread:{thread_id}"
                db.execute("INSERT INTO channel_threads(thread_id,session_key,role_id,channel,account_id,chat_id,chat_type,title,last_activity,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)", (thread_id, session_key, role_id, channel, account_id, str(chat_id), chat_type, title, now, _json(thread_metadata)))
        return self.get_thread_by_chat(role_id, account_id, str(chat_id), chat_type=chat_type)  # type: ignore[return-value]

    def get_thread_by_chat(self, role_id: str, account_id: str, chat_id: str, *, chat_type: str | None = None) -> ChannelThread | None:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM channel_threads WHERE role_id=? AND account_id=? AND chat_id=? AND (? IS NULL OR chat_type=?)", (role_id, account_id, str(chat_id), chat_type, chat_type)).fetchall()
        if len(rows) > 1:
            raise ValueError("平台 ID 对应多个会话类型，请明确指定 chat_type")
        return self._thread(rows[0]) if rows else None

    def get_thread(self, role_id: str, thread_id: str) -> ChannelThread | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM channel_threads WHERE role_id=? AND thread_id=?", (role_id, thread_id)).fetchone()
        return self._thread(row) if row else None

    def public_thread_payload(self, thread: ChannelThread) -> dict[str, object]:
        """Return the complete network row used by HTTP and live updates."""
        payload = thread.public_dict()
        payload["avatarUrl"] = self.avatars.url(thread.account_id, "chat", thread.chat_id)
        payload["latestMessage"] = self.latest_message_preview(thread)
        payload["externalMemoryAvailable"] = thread.channel != "desktop" and not self.external_memory.is_owner_thread(thread)
        return payload

    def latest_message_preview(self, thread: ChannelThread) -> dict[str, object] | None:
        with self._connect() as db:
            latest = db.execute(
                "SELECT content,role,status,created_at FROM messages WHERE session_key=? "
                "AND message_type IN ('text','proactive_reminder','internal_completion') "
                "ORDER BY sequence DESC LIMIT 1", (thread.session_key,),
            ).fetchone()
        return {"content": latest["content"], "role": latest["role"], "status": latest["status"],
                "createdAt": latest["created_at"]} if latest is not None else None

    def public_message_payload(self, thread: ChannelThread, message: Message | Mapping[str, object]) -> dict[str, object]:
        """Project a message using current identity and avatar bindings."""
        if isinstance(message, Message):
            payload = public_message(message).model_dump(mode="json")
        else:
            payload = dict(message)
        metadata = dict(payload.get("metadata") or {})
        metadata["threadId"] = thread.thread_id
        sender_id = str(metadata.get("senderId") or "")
        scope = "platform" if thread.channel in {"telegram", "qq"} else "account"
        metadata["senderAvatarUrl"] = self.avatars.url(thread.account_id, "sender", sender_id)
        metadata["senderIsOwner"] = bool(sender_id) and self.identity_is_owner(
            role_id=thread.role_id,
            channel=thread.channel,
            scope=scope,
            platform_user_id=sender_id,
            account_id=thread.account_id,
        )
        source = metadata.get("viaAccount")
        if isinstance(source, dict):
            source = dict(source)
            own_id = str(source.get("platformAccountId") or thread.account_id)
            source["avatarUrl"] = self.avatars.url(thread.account_id, "account", own_id)
            metadata["viaAccount"] = source
        payload["metadata"] = metadata
        return payload

    def list_threads(self, role_id: str, *, limit: int = 100, network_only: bool = False) -> list[ChannelThread]:
        page_limit = max(1, min(limit, 500))
        with self._connect() as db:
            channel_clause = (" AND channel <> 'desktop' AND EXISTS (SELECT 1 FROM messages m "
                              "WHERE m.session_key=channel_threads.session_key AND m.message_type IN "
                              "('text','proactive_reminder','internal_completion'))") if network_only else ""
            rows = db.execute(
                f"SELECT * FROM channel_threads WHERE role_id=?{channel_clause} ORDER BY last_activity DESC, thread_id DESC LIMIT ?",
                (role_id, page_limit),
            ).fetchall()
        threads: list[ChannelThread] = []
        for row in rows:
            thread = self._thread(row)
            threads.append(replace(thread, metadata={**thread.metadata, "latestMessage": self.latest_message_preview(thread)}))
        return threads

    def list_group_threads(self, role_id: str, account_id: str, *, limit: int = 500) -> list[ChannelThread]:
        """Read a bounded account-scoped QQ group directory without requiring messages."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM channel_threads WHERE role_id=? AND account_id=? AND channel='qq' "
                "AND chat_type='group' ORDER BY last_activity DESC, thread_id DESC LIMIT ?",
                (role_id, account_id, max(1, min(limit, 501))),
            ).fetchall()
        return [self._thread(row) for row in rows]

    def clear_listening_messages(self, role_id: str, account_id: str, chat_id: str) -> int:
        """Remove only stored NapCat旁听 messages for one group."""

        thread = self.get_thread_by_chat(role_id, account_id, str(chat_id), chat_type="group")
        if thread is None or thread.channel != "qq" or thread.chat_type != "group":
            raise KeyError(chat_id)
        return self.listening.clear(role_id, thread.thread_id)

    def append_event(self, thread: ChannelThread, *, direction: str, content: str, external_message_id: str | None, metadata: dict[str, Any] | None = None, role: str = "user", status: str = "completed") -> tuple[bool, Any]:
        if status not in {"completed", "failed", "streaming", "pending"}:
            raise ValueError("无效消息状态")
        event_id = f"event-{uuid.uuid4().hex}"
        now = _now()
        message_id = f"{thread.session_key}:{uuid.uuid4().hex}"
        message_metadata = {**(metadata or {}), "channel": thread.channel, "accountId": thread.account_id or None, "threadId": thread.thread_id, "direction": direction, "chatId": thread.chat_id, "groupName": thread.title if thread.chat_type == "group" else ""}
        # Adapter metadata is never authoritative about the account that
        # carried a message. Persist the host's snapshot once, at ingress or
        # after an outbound receipt, and keep it when the account is removed.
        message_metadata.pop("viaAccount", None)
        account = self.get_account(thread.account_id)
        snapshot = account.source_snapshot() if account is not None else thread.metadata.get("viaAccount")
        if snapshot:
            message_metadata["viaAccount"] = snapshot
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("INSERT INTO channel_events(event_id,thread_id,direction,external_message_id,created_at,metadata_json) VALUES(?,?,?,?,?,?)", (event_id, thread.thread_id, direction, external_message_id or None, now, _json(metadata or {})))
            except sqlite3.IntegrityError:
                return False, None
            if direction == "inbound" and role == "user" and message_metadata.get("channelTurnState") != "invalidated":
                self.external_memory.members.observe_in(db, thread, message_metadata, now)
            sequence = int(db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM messages WHERE session_key=?", (thread.session_key,)).fetchone()[0])
            db.execute(
                "INSERT INTO messages(message_id,session_key,sequence,role,content,status,created_at,message_type,metadata_json) VALUES(?,?,?,?,?,?,?,'text',?)",
                (message_id, thread.session_key, sequence, role, content, status, now, _json(message_metadata)),
            )
            if direction == "inbound" and role == "user":
                sender, name = message_metadata.get("senderId"), message_metadata.get("senderName")
                if isinstance(sender, str) and isinstance(name, str):
                    self._remember_member_name(db, thread.role_id, thread.channel, thread.account_id,
                        thread.chat_id, thread.chat_type, sender, name, now)
            db.execute("UPDATE channel_threads SET last_activity=? WHERE thread_id=?", (now, thread.thread_id))
        message = Message(
            id=message_id,
            sessionKey=thread.session_key,
            sequence=sequence,
            role=role,
            content=content,
            status=status,
            createdAt=datetime.fromisoformat(now),
            metadata=message_metadata,
        )
        current_thread = self.get_thread(thread.role_id, thread.thread_id) or thread
        self.publish(thread.role_id, {
            "type": "message",
            "roleId": thread.role_id,
            "thread": self.public_thread_payload(current_thread),
            "message": self.public_message_payload(current_thread, message),
        })
        return True, message

    def has_external_event(self, thread_id: str, external_message_id: str) -> bool:
        if not external_message_id:
            return False
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM channel_events WHERE thread_id=? AND external_message_id=? LIMIT 1",
                (thread_id, external_message_id),
            ).fetchone()
        return row is not None

    def publish(self, role_id: str, payload: dict[str, object]) -> None:
        for queue in tuple(self._subscribers.get(role_id, ())):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except asyncio.QueueEmpty:
                    pass

    def begin_delivery(self, thread: ChannelThread, content: str, *, source_session_key: str | None = None, source_run_id: str | None = None, source_tool_call_id: str | None = None, source_kind: str | None = None, account_generation: str | None = None) -> str:
        attempt_id = f"delivery-{uuid.uuid4().hex}"
        now = _now()
        with self._connect() as db:
            db.execute(
                "INSERT INTO channel_delivery_attempts(attempt_id,role_id,thread_id,account_id,target_chat_id,content,status,created_at,updated_at,source_session_key,source_run_id,source_tool_call_id,source_kind,account_generation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, thread.role_id, thread.thread_id, thread.account_id, thread.chat_id, content, "pending", now, now, source_session_key, source_run_id, source_tool_call_id, source_kind, account_generation),
            )
        return attempt_id

    def finish_delivery(self, attempt_id: str, *, status: str, platform_message_id: str | None = None, error: str | None = None, content: str | None = None) -> None:
        if status not in {"pending", "sent", "failed", "uncertain"}:
            raise ValueError("无效的投递状态")
        with self._connect() as db:
            db.execute(
                "UPDATE channel_delivery_attempts SET status=?,platform_message_id=?,error=?,content=COALESCE(?,content),updated_at=? WHERE attempt_id=?",
                (status, platform_message_id, error, content, _now(), attempt_id),
            )

    def record_delivery_parts(self, attempt_id: str, details: dict[str, Any]) -> None:
        with self._connect() as db:
            cursor = db.execute('UPDATE channel_delivery_attempts SET details_json=json_patch(COALESCE(details_json,\'{}\'),?),updated_at=? WHERE attempt_id=? AND status=?',
                (_json(details), _now(), attempt_id, 'pending'))
            if cursor.rowcount != 1:
                raise ValueError('original_delivery_is_no_longer_pending')
            db.execute("""UPDATE messages SET metadata_json=json_patch(metadata_json,?)
                WHERE json_extract(metadata_json,'$.deliveryAttemptId')=?""", (_json(details), attempt_id))

    def reserve_passive_reply(self, attempt_id: str, message_id: str, count: int) -> tuple[int, ...]:
        """Reserve QQBot's per-source quota in the original durable ledger."""
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            attempt = db.execute('SELECT account_id,status,details_json FROM channel_delivery_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
            if not attempt or attempt['status'] != 'pending' or not 1 <= count <= 5:
                raise ValueError('qqbot_passive_quota_exhausted')
            existing = _loads(attempt['details_json'])
            if existing.get('passiveReplyId') not in {None, message_id}:
                raise ValueError('qqbot_passive_source_changed')
            last = db.execute('''SELECT COALESCE(MAX(CAST(json_extract(details_json,'$.passiveReplyLastSeq') AS INTEGER)),0)
                FROM channel_delivery_attempts WHERE account_id=? AND json_extract(details_json,'$.passiveReplyId')=?''',
                (attempt['account_id'], message_id)).fetchone()[0]
            if last + count > 5:
                raise ValueError('qqbot_passive_quota_exhausted')
            db.execute('UPDATE channel_delivery_attempts SET details_json=? WHERE attempt_id=?',
                (_json({**existing, 'passiveReplyId': message_id, 'passiveReplyLastSeq': last + count}), attempt_id))
            return tuple(range(last + 1, last + count + 1))

    def set_channel_turn_state(self, thread: ChannelThread, message_id: str, state: str) -> None:
        """Keep accepted QQ work observable without inventing another user/run."""
        if state not in {"queued", "running", "completed", "failed", "cancelled", "invalidated", "interrupted"}:
            raise ValueError("invalid channel turn state")
        with self._connect() as db:
            row = db.execute("SELECT metadata_json FROM messages WHERE message_id=? AND session_key=? AND role='user'",
                             (message_id, thread.session_key)).fetchone()
            if row is None:
                return
            metadata = {**_loads(row[0]), "channelTurnState": state}
            db.execute("UPDATE messages SET metadata_json=? WHERE message_id=?", (_json(metadata), message_id))
        message = next((item for item in self.sessions.list_messages(thread.session_key) if item.id == message_id), None)
        if message is not None:
            self.publish(thread.role_id, {"type": "message", "roleId": thread.role_id,
                "thread": self.public_thread_payload(self.get_thread(thread.role_id, thread.thread_id) or thread),
                "message": self.public_message_payload(thread, message)})

    def mark_interrupted_channel_turns(self) -> int:
        """A restart retains accepted input, but never replays its external work."""
        with self._connect() as db:
            cursor = db.execute("""UPDATE messages SET metadata_json=json_set(metadata_json,
                '$.channelTurnState', 'interrupted')
                WHERE role='user' AND json_extract(metadata_json, '$.channel')='qq'
                AND json_extract(metadata_json, '$.direction')='inbound'
                AND json_extract(metadata_json, '$.channelTurnState') IN ('queued', 'running')""")
            return cursor.rowcount

    def mark_interrupted_deliveries_uncertain(self) -> int:
        """Preserve receipts; unfinished work is never an outbound replay queue."""
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT * FROM channel_delivery_attempts WHERE status='pending'").fetchall()
            for row in rows:
                details = _loads(row['details_json'])
                parts = details.get('parts', [])
                for part in parts:
                    if part.get('status') == 'sending':
                        part.update(status='uncertain', error='process_interrupted')
                from .channels.delivery_projection import confirmed_parts
                sent = confirmed_parts(details)
                status = ('uncertain' if not parts or any(part.get('status') == 'uncertain' for part in parts)
                          else 'sent' if not details.get('streaming') and len(sent) == len(parts) else 'failed')
                identifier = sent[0]['platformMessageId'] if sent else row['platform_message_id']
                if parts:
                    details.update(parts=parts, partialDelivery=bool(sent) and status != 'sent',
                        deliveredContent=''.join(part.get('content', '') for part in sent if part.get('kind') != 'preview'),
                        platform_message_ids=[part['platformMessageId'] for part in sent])
                db.execute('UPDATE channel_delivery_attempts SET status=?,platform_message_id=?,details_json=?,error=?,updated_at=? WHERE attempt_id=?',
                    (status, identifier, _json(details), None if status == 'sent' else 'process_interrupted', _now(), row['attempt_id']))
                db.execute("""UPDATE messages SET status=?,metadata_json=json_patch(metadata_json,?)
                    WHERE json_extract(metadata_json,'$.deliveryAttemptId')=?""",
                    ('completed' if sent else 'failed', _json({**details, 'deliveryStatus': status,
                        'platformMessageId': identifier, 'error': 'process_interrupted'}), row['attempt_id']))
            return len(rows)

    def list_deliveries(self, role_id: str, *, limit: int = 100) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM channel_delivery_attempts WHERE role_id=? ORDER BY created_at DESC LIMIT ?", (role_id, max(1, min(limit, 500)))).fetchall()
        return [{
            "attemptId": str(row["attempt_id"]), "threadId": str(row["thread_id"]), "accountId": str(row["account_id"]),
            "targetChatId": str(row["target_chat_id"]), "content": str(row["content"]), "status": str(row["status"]),
            "platformMessageId": row["platform_message_id"], "error": row["error"], "createdAt": str(row["created_at"]), "updatedAt": str(row["updated_at"]),
            "sourceSessionKey": row["source_session_key"], "sourceRunId": row["source_run_id"], "sourceToolCallId": row["source_tool_call_id"], "sourceKind": row["source_kind"], "accountGeneration": row["account_generation"],
            **_loads(row['details_json']),
        } for row in rows]

    def delivery_for_source(self, role_id: str, *, source_kind: str, source_tool_call_id: str) -> tuple[str, str] | None:
        """Find an authoritative receipt by its stable host outbox marker."""
        with self._connect() as db:
            row = db.execute('SELECT status,attempt_id FROM channel_delivery_attempts WHERE role_id=? AND source_kind=? AND source_tool_call_id=? ORDER BY created_at LIMIT 1',
                             (role_id, source_kind, source_tool_call_id)).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def runtime_reply_receipt(self, role_id: str, session_key: str, run_id: str) -> dict[str, Any] | None:
        """Reconcile only the original Runtime reply, including a stopped stream."""
        with self._connect() as db:
            rows = db.execute('''SELECT d.*,t.channel FROM channel_delivery_attempts d
                JOIN channel_threads t ON t.thread_id=d.thread_id AND t.role_id=d.role_id
                WHERE d.role_id=? AND d.source_session_key=? AND d.source_run_id=?
                AND d.source_kind IN ('reply','natural') AND t.session_key=? LIMIT 2''',
                (role_id, session_key, run_id, session_key)).fetchall()
        if len(rows) != 1 or rows[0]['status'] == 'pending':
            return None
        row = rows[0]
        details = _loads(row['details_json'])
        return {'channel': row['channel'], 'accountId': row['account_id'], 'threadId': row['thread_id'],
            'deliveryStatus': row['status'], 'deliveryAttemptId': row['attempt_id'],
            'platformMessageId': row['platform_message_id'], 'deliveryError': row['error'],
            **{key: details[key] for key in ('parts', 'partialDelivery', 'deliveredContent', 'receiptKind') if key in details}}

    def delivery_receipt_for_source(self, role_id: str, *, source_kind: str, source_tool_call_id: str,
                                    connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        """Look up the original complete attempt, never a bounded recent list."""
        with nullcontext(connection) if connection is not None else self._connect() as db:
            rows = db.execute('''SELECT * FROM channel_delivery_attempts
                WHERE role_id=? AND source_kind=? AND source_tool_call_id=? ORDER BY created_at,attempt_id LIMIT 2''',
                (role_id, source_kind, source_tool_call_id)).fetchall()
            if len(rows) > 1:
                raise ValueError('original autonomous source marker has conflicting attempts')
            return dict(rows[0]) if rows else None

    def project_autonomy_delivery_once(self, role_id: str, tick_id: str, intent_id: str, mode: str,
                                      target: dict[str, Any], draft: dict[str, Any], *,
                                      connection: sqlite3.Connection) -> tuple[dict[str, Any], dict[str, object]]:
        """Project an already-sent original attempt in its outbox transaction.

        This does not authorize or dispatch a new send. Account revocation does
        not erase a genuine receipt from the retained original channel thread.
        """
        db = connection
        database = db.execute('PRAGMA database_list').fetchone()
        if (not db.in_transaction or database is None
                or Path(database[2]).resolve() != Path(self.database_path).resolve()
                or Path(database[2]).resolve() != Path(self.sessions.database_path).resolve()):
            raise ValueError('autonomous projection requires the original shared caller transaction')
        if mode not in {'active_decision', 'drift'}:
            raise ValueError('invalid autonomous delivery mode')
        attempt = self.delivery_receipt_for_source(role_id, source_kind='autonomy', source_tool_call_id=intent_id, connection=db)
        details = _loads(attempt['details_json']) if attempt else {}
        from .channels.delivery_projection import confirmed_parts
        if (attempt is None or (attempt['status'] != 'sent' and not confirmed_parts(details)) or attempt['source_run_id'] != tick_id
                or attempt['account_id'] != target['accountId'] or attempt['target_chat_id'] != target['chatId']
                or attempt['content'] != draft['message']):
            raise ValueError('autonomous projection has no matching original sent receipt')
        thread_row = db.execute('SELECT * FROM channel_threads WHERE thread_id=? AND role_id=?', (attempt['thread_id'], role_id)).fetchone()
        if thread_row is None:
            raise PermissionError('original autonomous channel thread is unavailable')
        thread = self._thread(thread_row)
        if (thread.account_id, thread.channel, thread.chat_id, thread.chat_type) != (
                target['accountId'], target['channel'], target['chatId'], 'private'):
            raise ValueError('autonomous projection conflicts with its original thread')
        message_id = f'{thread.session_key}:autonomy:{intent_id}'
        event_id = f'autonomy:{intent_id}'
        metadata = {'internalEvent': mode, 'autonomyTickId': tick_id, 'autonomyIntentId': intent_id,
            'channel': thread.channel, 'accountId': thread.account_id, 'threadId': thread.thread_id,
            'direction': 'outbound', 'chatId': thread.chat_id, 'groupName': '',
            'targetId': target['targetId'], 'targetKind': 'private', 'deliveryStatus': attempt['status'],
            **{key: details[key] for key in ('parts', 'partialDelivery', 'deliveredContent', 'receiptKind') if key in details},
            'deliveryAttemptId': attempt['attempt_id'], 'platformMessageId': attempt['platform_message_id'],
            'mood': draft['mood'], 'thought': draft['thought'], 'attachments': draft.get('attachments', []),
            'viaAccount': target.get('viaAccount', {'platform': target['channel'], 'accountId': target['accountId'],
                'platformAccountId': target['accountId'], 'displayName': target.get('accountDisplayName', '')})}
        stored = db.execute('SELECT sequence,content,status,message_type,metadata_json,created_at FROM messages WHERE message_id=? AND session_key=?',
            (message_id, thread.session_key)).fetchone()
        if stored is not None:
            if (stored['content'], stored['status'], stored['message_type'], _loads(stored['metadata_json'])) != (
                    draft['message'], 'completed', 'internal_completion', metadata):
                raise ValueError('original autonomous message identity conflicts')
            sequence, now = stored['sequence'], stored['created_at']
        else:
            now = _now()
            sequence = int(db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM messages WHERE session_key=?', (thread.session_key,)).fetchone()[0])
            # Both the event identity and platform ID keep their original
            # uniqueness. A conflicting existing row is not a success receipt.
            db.execute('INSERT INTO channel_events VALUES(?,?,?,?,?,?)',
                (event_id, thread.thread_id, 'outbound', attempt['platform_message_id'], now, _json(metadata)))
            db.execute('''INSERT INTO messages(message_id,session_key,sequence,role,content,status,created_at,message_type,metadata_json)
                VALUES(?,?,?,'assistant',?,'completed',?,'internal_completion',?)''',
                (message_id, thread.session_key, sequence, draft['message'], now, _json(metadata)))
            db.execute('UPDATE channel_threads SET last_activity=? WHERE thread_id=?', (now, thread.thread_id))
        message = Message(id=message_id, sessionKey=thread.session_key, sequence=sequence, role='assistant',
            content=draft['message'], status='completed', createdAt=datetime.fromisoformat(now),
            messageType='internal_completion', metadata=metadata)
        receipt = {'status': attempt['status'],
            **{key: details[key] for key in ('parts', 'partialDelivery', 'deliveredContent', 'receiptKind') if key in details}, 'intentId': intent_id, 'targetRef': target['ref'], 'messageId': message_id,
            'attemptId': attempt['attempt_id'], 'platformMessageId': attempt['platform_message_id']}
        event: dict[str, object] = {'type': 'message', 'roleId': role_id, 'thread': thread.public_dict(),
            'message': public_message(message).model_dump(mode='json')}
        return receipt, event

    async def subscribe(self, role_id: str):
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=128)
        self._subscribers.setdefault(role_id, set()).add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            subscribers = self._subscribers.get(role_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(role_id, None)

    def list_messages(self, role_id: str, thread_id: str, *, before: int | None = None, limit: int = 50, network_only: bool = False) -> tuple[ChannelThread, list[Any], bool, int | None]:
        thread = self.get_thread(role_id, thread_id)
        if thread is None or (network_only and thread.channel == "desktop"):
            raise KeyError(thread_id)
        rows = self.sessions.list_messages(thread.session_key)
        rows = [row for row in rows if row.messageType in {"text", "proactive_reminder", "internal_completion"}]
        if before is not None:
            rows = [row for row in rows if row.sequence < before]
        rows = sorted(rows, key=lambda item: item.sequence)
        has_more = len(rows) > max(1, min(limit, 200))
        selected = rows[-max(1, min(limit, 200)):] if has_more else rows
        next_before = selected[0].sequence if has_more and selected else None
        return thread, selected, has_more, next_before

    @staticmethod
    def _account(row: sqlite3.Row) -> ChannelAccount:
        blocked = _loads(row["blocked_senders_json"]).get("items", []) if row["blocked_senders_json"].startswith("{") else json.loads(row["blocked_senders_json"] or "[]")
        return ChannelAccount(str(row["account_id"]), str(row["role_id"]), str(row["channel"]), bool(row["enabled"]), bool(row["allow_private"]), bool(row["allow_group"]), tuple(str(x) for x in blocked if isinstance(x, str)), str(row["display_name"]), row["platform_id"], str(row["created_at"]), str(row["updated_at"]), row["last_error"])

    @staticmethod
    def _thread(row: sqlite3.Row) -> ChannelThread:
        return ChannelThread(str(row["thread_id"]), str(row["session_key"]), str(row["role_id"]), str(row["channel"]), str(row["account_id"]), str(row["chat_id"]), str(row["chat_type"]), str(row["title"]), str(row["last_activity"]), _loads(row["metadata_json"]))


class TelegramChannelService:
    """Normalize Telegram updates and persist isolated private/group threads."""

    def __init__(self, store: MultichannelStore, account_id: str | TelegramTransport = "", transport: TelegramTransport | MessageHandler | None = None, on_message: MessageHandler | None = None, persist_attachment: Callable[[str, str, str, bytes], dict[str, object]] | None = None, on_command: CommandHandler | None = None, on_admit: AdmissionHandler | None = None, on_defer: Callable[..., bool] | None = None, on_update: Callable[[str, Mapping[str, Any]], Awaitable[None]] | None = None) -> None:
        self.store = store
        self.on_command = on_command
        self.on_admit = on_admit
        self.on_defer = on_defer
        self.on_update = on_update
        self._message_tasks: set[asyncio.Task[None]] = set()
        self._connection: dict[str, object] = {"connection": "unknown", "online": None,
            "observedAt": None, "reason": "poll_not_observed"}
        self._connection_generation: tuple[str, str, str, str, tuple[int, int]] | None = None
        # Keep the small pre-account-id constructor usable for adapter tests:
        # ``Service(store, fake_transport, handler)``.
        if not isinstance(account_id, str):
            legacy_transport = account_id
            legacy_handler = transport if callable(transport) and not hasattr(transport, "get_me") else on_message
            self.account_id = ""
            self.transport = legacy_transport
            self.on_message = legacy_handler if callable(legacy_handler) else None
            self.persist_attachment = persist_attachment
        else:
            self.account_id = account_id
            self.transport = transport if transport is not None and hasattr(transport, "get_me") else TelegramBotApi()
            self.on_message = on_message
            self.persist_attachment = persist_attachment

    def connection_snapshot(self) -> dict[str, object]:
        if self._connection_generation is not None and self._poll_generation() != self._connection_generation:
            return {"connection": "unknown", "online": None, "observedAt": None, "reason": "configuration_changed"}
        return dict(self._connection)

    def _poll_generation(self) -> tuple[str, str, str, str, tuple[int, int]] | None:
        account = self.store.get_account(self.account_id)
        if account is None:
            return None
        return (account.created_at, account.role_id, account.channel, account.platform_id or "",
            self.store.directory_revision(self.account_id, account.channel))

    def _observe_poll(self, online: bool, reason: str, generation: tuple[str, str, str, str, tuple[int, int]] | None) -> None:
        self._connection_generation = generation
        self._connection = {"connection": "connected" if online else "disconnected", "online": online,
            "observedAt": _now(), "reason": reason}

    async def test_account(self, account_id: str) -> Mapping[str, Any]:
        token = self.store.token(account_id)
        if not token:
            raise RuntimeError("Telegram Bot Token 尚未配置")
        result = await self.transport.get_me(token)
        account = self.store.get_account(account_id)
        if account:
            self.store.set_platform_identity(account_id, str(result.get("id") or ""), display_name=str(result.get("username") or result.get("first_name") or ""))
        return result

    async def send_text(self, account_id: str, thread_id: str, text: str, *, reply_to: str | None = None) -> Any:
        account = self.store.get_account(account_id)
        if account is None:
            raise KeyError(account_id)
        thread = self.store.get_thread(account.role_id, thread_id)
        if thread is None or thread.account_id != account_id:
            raise KeyError(thread_id)
        attempt_id = self.store.begin_delivery(thread, text)
        try:
            result = await self.transport.send_message(self.store.token(account_id), thread.chat_id, text, reply_to=reply_to)
        except Exception as error:
            uncertain = isinstance(error, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException))
            self.store.finish_delivery(
                attempt_id,
                status="uncertain" if uncertain else "failed",
                error=type(error).__name__,
            )
            self.store.publish(thread.role_id, {"type": "delivery", "roleId": thread.role_id, "attemptId": attempt_id})
            raise
        message_id = str(result.get("message_id") or "") or None
        self.store.finish_delivery(attempt_id, status="sent", platform_message_id=message_id)
        self.store.append_event(
            thread,
            direction="outbound",
            content=text,
            external_message_id=message_id,
            role="assistant",
            metadata={
                "platformMessageId": message_id,
                "deliveryAttemptId": attempt_id,
                "deliveryStatus": "sent",
                "replyTo": reply_to,
            },
        )
        self.store.publish(thread.role_id, {"type": "delivery", "roleId": thread.role_id, "attemptId": attempt_id})
        return result

    async def handle_update(self, account_id: str, update: Mapping[str, Any], *, background: bool = False, received_at: float | None = None, history_intake=None) -> bool:
        received_at = datetime.now(timezone.utc).timestamp() if received_at is None else received_at
        account = self.store.get_account(account_id)
        if account is None or not account.enabled:
            return False
        from .conversation_history import ConversationHistoryView, freeze_channel_history, bind_channel_history
        if history_intake is None:
            history_intake = freeze_channel_history(self.store, account)
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, Mapping):
            return False
        chat = message.get("chat") if isinstance(message.get("chat"), Mapping) else {}
        sender = message.get("from") if isinstance(message.get("from"), Mapping) else {}
        sender_chat = message.get("sender_chat") if isinstance(message.get("sender_chat"), Mapping) else {}
        if sender_chat:
            sender = sender_chat
        chat_id = str(chat.get("id") or "")
        sender_id = (f"chat:{sender_chat['id']}" if sender_chat.get("id") is not None else str(sender.get("id") or ""))
        sender_name = str(sender.get("title") or " ".join(str(sender.get(key) or "") for key in ("first_name", "last_name")).strip() or sender.get("username") or "")
        from .channels.base import is_blocked_identity
        if not chat_id or not sender_id or is_blocked_identity(account.blocked_senders, sender_ids=(sender_id,), aliases=(sender.get("username"), sender_name)):
            return False
        chat_type = str(chat.get("type") or "private")
        if chat_type == "private" and not account.allow_private:
            return False
        if chat_type != "private" and not account.allow_group:
            return False
        raw_content = str(message.get("text") or message.get("caption") or "")
        content = raw_content.strip()
        message_thread_id = message.get("message_thread_id")
        topic_id = message_thread_id if isinstance(message_thread_id, int) and not isinstance(message_thread_id, bool) and message_thread_id > 0 and chat_type != "private" else None
        topic_kwargs = {"message_thread_id": topic_id} if topic_id is not None else {}
        title = str(chat.get("title") or sender_name).strip()
        thread = self.store.ensure_thread(account.role_id, account_id, chat_id, "private" if chat_type == "private" else "group", title=title, metadata={"senderId": sender_id, "username": sender.get("username")})
        from .channels.models import TargetKind, target_from_chat

        target = target_from_chat(
            account_id=account_id,
            chat_id=chat_id,
            kind=TargetKind.PRIVATE if chat_type == "private" else TargetKind.GROUP,
            display_name=title,
        )
        if chat_type != "private":
            known = self.store.get_target(account.role_id, account_id, target.id)
            known_metadata = known.metadata if known is not None else {}
            topics = set(known_metadata.get("topics", []))
            if topic_id is not None:
                topics.add(topic_id)
            members = set(str(item) for item in known_metadata.get("member_ids", []))
            members.add(sender_id)
            target = replace(target, metadata={
                **known_metadata, "topics": sorted(topics), "member_ids": sorted(members),
                **({"message_thread_id": topic_id} if topic_id is not None else {}),
            })
        self.store.remember_targets(account.role_id, [target])
        external_id = str(message.get("message_id") or update.get("update_id") or "")
        if not external_id:
            return False
        if self.store.has_external_event(thread.thread_id, external_id):
            return False
        from .channels.commands import control_command, identifier_reply, pairing_code

        command = control_command(content)
        if command is not None:
            if self.on_command is not None:
                response = self.on_command(thread, command, sender_id)
                if hasattr(response, "__await__"):
                    response = await response
            elif command == "stop":
                response = "当前未启用中断功能。"
            else:
                response = identifier_reply(command, channel="telegram", account_id=account_id, chat_id=chat_id, sender_id=sender_id)
            await self.transport.send_message(self.store.token(account_id), chat_id, str(response), reply_to=external_id, **topic_kwargs)
            return True
        reply = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), Mapping) else {}
        reply_chat = reply.get("chat") if isinstance(reply.get("chat"), Mapping) else {}
        if reply_chat.get("id") is not None and str(reply_chat["id"]) != chat_id:
            reply = {}  # Do not import another conversation through a malformed quote.
        reply_from = reply.get("from") if isinstance(reply.get("from"), Mapping) else {}
        reply_sender_chat = reply.get("sender_chat") if isinstance(reply.get("sender_chat"), Mapping) else {}
        reply_sender_id = (f"chat:{reply_sender_chat['id']}" if reply_sender_chat.get("id") is not None
            else str(reply_from.get("id") or ""))
        bot_id = str(account.platform_id or "")
        bot_username = account.display_name.lstrip("@").lower()
        entities = message.get("entities") if isinstance(message.get("entities"), list) else []
        if not entities and isinstance(message.get("caption_entities"), list):
            entities = message["caption_entities"]
        mentioned_ids: list[str] = []
        mentioned = False
        for entity in entities:
            if not isinstance(entity, Mapping):
                continue
            entity_type = entity.get("type")
            if entity_type == "text_mention":
                user = entity.get("user") if isinstance(entity.get("user"), Mapping) else {}
                mentioned_id = str(user.get("id") or "")
                if mentioned_id:
                    mentioned_ids.append(mentioned_id)
                    mentioned = mentioned or mentioned_id == bot_id
            elif entity_type == "mention" and bot_username:
                offset, length = entity.get("offset"), entity.get("length")
                if isinstance(offset, int) and isinstance(length, int) and offset >= 0 and length > 0:
                    # Telegram entity offsets are UTF-16 code-unit offsets,
                    # while Python indexes Unicode code points.
                    encoded = raw_content.encode("utf-16-le")
                    value = encoded[offset * 2:(offset + length) * 2].decode("utf-16-le", errors="ignore").lstrip("@").casefold()
                    mentioned = mentioned or value == bot_username.casefold()
        triggered = chat_type == "private" or bool(mentioned) or bool(bot_id and reply_sender_id == bot_id)
        # Telegram has no Shiori-style group listener in the baseline. Group
        # messages without an explicit mention/reply are therefore ignored.
        if chat_type != "private" and not triggered:
            return False
        if self.on_admit is not None and not self.on_admit(account_id):
            if self.on_defer is not None and self.on_defer(account_id, update, received_at=received_at, history_intake=history_intake):
                return False
            from .channels.host import CHANNEL_INTAKE_RETRY_NOTICE
            await self.transport.send_message(
                self.store.token(account_id), chat_id, CHANNEL_INTAKE_RETRY_NOTICE,
                reply_to=external_id,
                **topic_kwargs,
            )
            return False
        code = pairing_code(content)
        if code is not None:
            paired = chat_type == "private" and self.store.consume_pairing_code(code, role_id=account.role_id, channel="telegram", scope="platform", platform_user_id=sender_id, account_id=account_id, chat_id=chat_id)
            if paired:
                await self.transport.send_message(self.store.token(account_id), chat_id, "配对成功。")
            return paired
        owner = self.store.identity_is_owner(role_id=account.role_id, channel="telegram", scope="platform", platform_user_id=sender_id, account_id=account_id)
        snapshot = bind_channel_history(self.store, thread, sender_id, history_intake)
        history = ConversationHistoryView(self.store, role_id=account.role_id, thread_id=thread.thread_id, snapshot=snapshot) if snapshot else None
        attachment_items = await self._download_attachments(account, message, check=history.require_current if history else None)
        if history is not None:
            history.require_current()
        if not content and not attachment_items:
            return False
        if not content and attachment_items:
            content = "[收到附件]"
        quoted_attachments = await self._download_attachments(account, reply, check=history.require_current if history else None) if reply else []
        if history is not None:
            history.require_current()
        metadata = {"hostReceivedAt": received_at, "platformMessageId": external_id, "senderId": sender_id, "senderName": sender_name, "senderIsOwner": owner, "senderKind": "chat" if sender_chat else "user", "chatType": chat_type, "triggered": triggered, "mentioned": mentioned, "mentionedIds": mentioned_ids, "replyTo": str(reply.get("message_id") or "") if reply else None, "replyToSenderId": str(reply_from.get("id") or "") if reply else None, "replyToContent": str(reply.get("text") or reply.get("caption") or "") if reply else None, "replyToSenderName": str(reply_from.get("first_name") or reply_from.get("username") or "") if reply else None, "quotedAttachments": quoted_attachments, "attachments": attachment_items}
        from .channels.reply_identity import capture_reply_identity
        metadata.update(capture_reply_identity(self.store, account,
            sender_id=reply_sender_id, verified=bool(reply)))
        if reply_sender_chat:
            metadata["replyToSenderName"] = str(reply_sender_chat.get("title") or "")
        if topic_id is not None:
            metadata["messageThreadId"] = topic_id
        accepted, stored_message = self.store.append_event(thread, direction="inbound", content=content, external_message_id=external_id, metadata=metadata)
        if not accepted:
            return False
        if self.on_message is not None:
            metadata = {**metadata, "messageId": getattr(stored_message, "id", None)}
            if history is not None:
                history.include_current(stored_message.id)
                metadata["ownerHistory"] = history.snapshot()
            outcome = self.on_message(thread, content, metadata, owner)
            if hasattr(outcome, "__await__"):
                if background:
                    task = asyncio.create_task(self._complete_message(outcome))
                    self._message_tasks.add(task)
                    task.add_done_callback(self._message_tasks.discard)
                else:
                    await outcome
        return True

    async def _complete_message(self, outcome: Awaitable[None]) -> None:
        try:
            await outcome
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self.store.get_account(self.account_id) is not None:
                self.store.set_account_state(self.account_id, last_error=type(error).__name__)

    async def _download_attachments(self, account: ChannelAccount, message: Mapping[str, Any], *, check=None) -> list[dict[str, object]]:
        if self.persist_attachment is None or not hasattr(self.transport, "get_file"):
            return []
        candidates: list[tuple[str, str, str, int | None]] = []
        photos = message.get("photo")
        if isinstance(photos, list):
            photo = next((item for item in reversed(photos) if isinstance(item, Mapping)), None)
            if photo and isinstance(photo.get("file_id"), str):
                candidates.append((str(photo["file_id"]), "photo.jpg", "image/jpeg", int(photo["file_size"]) if isinstance(photo.get("file_size"), int) else None))
        document = message.get("document")
        if isinstance(document, Mapping) and isinstance(document.get("file_id"), str):
            candidates.append((str(document["file_id"]), str(document.get("file_name") or "attachment.bin"), str(document.get("mime_type") or "application/octet-stream"), int(document["file_size"]) if isinstance(document.get("file_size"), int) else None))
        attachments: list[dict[str, object]] = []
        for file_id, name, mime, size in candidates[:4]:
            if size is not None and (size < 0 or size > MAX_ATTACHMENT_BYTES):
                continue
            try:
                if check:
                    check()
                remote = await self.transport.get_file(self.store.token(account.account_id), file_id)
                if check:
                    check()
                file_path = remote.get("file_path")
                if not isinstance(file_path, str) or not file_path:
                    continue
                data, downloaded_mime = await self.transport.download_file(self.store.token(account.account_id), file_path)
                if check:
                    check()
                if len(data) > MAX_ATTACHMENT_BYTES:
                    continue
                attachments.append(self.persist_attachment(account.role_id, name, mime if mime != "application/octet-stream" else downloaded_mime, data))
            except (OSError, ValueError, RuntimeError, HTTPException, httpx.HTTPError):
                continue
        return attachments

    async def run_polling(self, stop: asyncio.Event) -> None:
        """Long-poll updates for one enabled bot until its task is stopped."""

        offset: int | None = None
        generation = None
        try:
            while not stop.is_set():
                account = self.store.get_account(self.account_id)
                if account is None or not self.store.account_effectively_enabled(account):
                    return
                generation = self._poll_generation()
                token = self.store.token(self.account_id)
                if not token:
                    await asyncio.sleep(1)
                    continue
                try:
                    updates = await self.transport.get_updates(token, offset=offset)
                    if stop.is_set() or self._poll_generation() != generation or self.store.token(self.account_id) != token:
                        return
                    self._observe_poll(True, "poll_succeeded", generation)
                    for update in updates:
                        update_id = update.get("update_id")
                        if isinstance(update_id, int):
                            offset = update_id + 1
                        if self.on_update is None:
                            await self.handle_update(self.account_id, update, background=True)
                        else:
                            task = asyncio.create_task(self.on_update(self.account_id, update))
                            self._message_tasks.add(task)
                            task.add_done_callback(self._message_tasks.discard)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if stop.is_set() or self._poll_generation() != generation or self.store.token(self.account_id) != token:
                        return
                    account = self.store.get_account(self.account_id)
                    if account is not None:
                        self.store.set_account_state(self.account_id, last_error=type(error).__name__)
                    self._observe_poll(False, "poll_failed", generation)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        pass
        finally:
            self._observe_poll(False, "polling_stopped", generation)
            if self._message_tasks:
                cleanup = asyncio.gather(*self._message_tasks, return_exceptions=True)
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise
