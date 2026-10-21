from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import DraftStatus, MailConnection, MailDraft, MailItem


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _account_scope(account: str) -> str:
    normalized = account.strip().casefold()
    return f"acct-{_hash(normalized)[:20]}" if normalized else ""


def _thread_scope(thread_id: str, account: str) -> str:
    scope = _account_scope(account)
    return _hash(f"{scope}\0{thread_id}")[:32] if scope else thread_id


class MailStore:
    """Durable, idempotent state for the owner-level mailbox service."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self) -> None:
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS mail_connection (
                id INTEGER PRIMARY KEY CHECK(id=1), host TEXT NOT NULL DEFAULT 'imap.qq.com',
                port INTEGER NOT NULL DEFAULT 993, username TEXT NOT NULL DEFAULT '',
                use_ssl INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 0,
                paused INTEGER NOT NULL DEFAULT 0, verified INTEGER NOT NULL DEFAULT 0,
                auto_draft_ready INTEGER NOT NULL DEFAULT 0, inbox_folder TEXT NOT NULL DEFAULT '',
                sent_folder TEXT NOT NULL DEFAULT '', drafts_folder TEXT NOT NULL DEFAULT '',
                last_error TEXT, last_sync_at TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mail_sync_state (
                folder TEXT PRIMARY KEY, last_uid INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mail_items (
                id TEXT PRIMARY KEY, source_uid TEXT NOT NULL UNIQUE, mailbox TEXT NOT NULL,
                message_id TEXT NOT NULL, thread_id TEXT NOT NULL, sender TEXT NOT NULL,
                reply_to TEXT NOT NULL DEFAULT '', recipients TEXT NOT NULL, subject TEXT NOT NULL, received_at TEXT NOT NULL,
                body_text TEXT NOT NULL, in_reply_to TEXT NOT NULL DEFAULT '', references_text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new', needs_reply INTEGER, draft_id TEXT,
                last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS mail_items_thread ON mail_items(thread_id);
            CREATE TABLE IF NOT EXISTS mail_drafts (
                id TEXT PRIMARY KEY, mail_id TEXT NOT NULL, task_key TEXT NOT NULL DEFAULT '', remote_uid TEXT,
                folder TEXT NOT NULL, body TEXT NOT NULL, subject TEXT NOT NULL,
                recipients TEXT NOT NULL, message_id TEXT NOT NULL, generated_hash TEXT NOT NULL,
                remote_hash TEXT NOT NULL, status TEXT NOT NULL, stale INTEGER NOT NULL DEFAULT 0,
                remote_missing INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                last_error TEXT, FOREIGN KEY(mail_id) REFERENCES mail_items(id)
            );
            CREATE TABLE IF NOT EXISTS mail_tasks (
                task_key TEXT PRIMARY KEY, mail_id TEXT, account_scope TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                error TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mail_jobs (
                id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
                target_id TEXT NOT NULL DEFAULT '', account_scope TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL, result_json TEXT, error TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mail_test_drafts (
                marker TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL,
                sent_at TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS mail_connection_candidate (
                id INTEGER PRIMARY KEY CHECK(id=1), host TEXT NOT NULL, port INTEGER NOT NULL,
                username TEXT NOT NULL, inbox_folder TEXT NOT NULL, sent_folder TEXT NOT NULL,
                drafts_folder TEXT NOT NULL, marker TEXT NOT NULL, connection_tested INTEGER NOT NULL,
                test_draft_present INTEGER NOT NULL, test_sent INTEGER NOT NULL DEFAULT 0,
                tested_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mail_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mail_preparations (
                id TEXT PRIMARY KEY, mail_id TEXT NOT NULL, task_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mail_audit (
                id TEXT PRIMARY KEY, operation TEXT NOT NULL, source_role_id TEXT NOT NULL,
                run_id TEXT NOT NULL, mail_id TEXT, status TEXT NOT NULL, error TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mail_reminder_batches (
                batch_id TEXT PRIMARY KEY, account_scope TEXT NOT NULL DEFAULT '', draft_ids_json TEXT NOT NULL, status TEXT NOT NULL,
                failure_count INTEGER NOT NULL DEFAULT 0, failure_summary_json TEXT NOT NULL DEFAULT '[]',
                role_id TEXT, last_error TEXT,
                created_at TEXT NOT NULL, delivered_at TEXT
            );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(mail_items)")}
            if "reply_to" not in columns:
                db.execute("ALTER TABLE mail_items ADD COLUMN reply_to TEXT NOT NULL DEFAULT ''")
            if "direction" not in columns:
                db.execute("ALTER TABLE mail_items ADD COLUMN direction TEXT NOT NULL DEFAULT 'inbound'")
            draft_columns = {row[1] for row in db.execute("PRAGMA table_info(mail_drafts)")}
            if "task_key" not in draft_columns:
                db.execute("ALTER TABLE mail_drafts ADD COLUMN task_key TEXT NOT NULL DEFAULT ''")
            if "preparation_id" not in draft_columns:
                db.execute("ALTER TABLE mail_drafts ADD COLUMN preparation_id TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS mail_drafts_task_key ON mail_drafts(task_key) WHERE task_key != ''")
            task_columns = {row[1] for row in db.execute("PRAGMA table_info(mail_tasks)")}
            if "account_scope" not in task_columns:
                db.execute("ALTER TABLE mail_tasks ADD COLUMN account_scope TEXT NOT NULL DEFAULT ''")
            job_columns = {row[1] for row in db.execute("PRAGMA table_info(mail_jobs)")}
            if "account_scope" not in job_columns:
                db.execute("ALTER TABLE mail_jobs ADD COLUMN account_scope TEXT NOT NULL DEFAULT ''")
            batch_columns = {row[1] for row in db.execute("PRAGMA table_info(mail_reminder_batches)")}
            if "failure_count" not in batch_columns:
                db.execute("ALTER TABLE mail_reminder_batches ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0")
            if "failure_summary_json" not in batch_columns:
                db.execute("ALTER TABLE mail_reminder_batches ADD COLUMN failure_summary_json TEXT NOT NULL DEFAULT '[]'")
            if "question_ids_json" not in batch_columns:
                db.execute("ALTER TABLE mail_reminder_batches ADD COLUMN question_ids_json TEXT NOT NULL DEFAULT '[]'")
            active = db.execute("SELECT username FROM mail_connection WHERE id=1").fetchone()
            scope = _account_scope(str(active[0])) if active and active[0] else ""
            if scope:
                legacy_items = db.execute("SELECT id,source_uid,thread_id,message_id,in_reply_to,references_text FROM mail_items WHERE source_uid NOT LIKE 'acct-%::%'").fetchall()
                for row in legacy_items:
                    references = str(row["references_text"] or "").split()
                    root = references[0] if references else str(row["in_reply_to"] or row["message_id"] or "")
                    account_thread = _hash(f"{str(active[0]).strip().casefold()}\0{root}")[:32]
                    thread_id = account_thread
                    db.execute("UPDATE mail_items SET source_uid=?,thread_id=? WHERE id=?", (f"{scope}::{row['source_uid']}", thread_id, row["id"]))
                    db.execute("UPDATE mail_jobs SET target_id=? WHERE kind='process_thread' AND target_id=?", (thread_id, row["thread_id"]))
                legacy_states = db.execute("SELECT folder,last_uid,updated_at FROM mail_sync_state WHERE folder NOT LIKE 'acct-%::%'").fetchall()
                for row in legacy_states:
                    db.execute("UPDATE OR IGNORE mail_sync_state SET folder=? WHERE folder=?", (f"{scope}::{row['folder']}", row["folder"]))
            db.execute("UPDATE mail_tasks SET account_scope=(SELECT substr(i.source_uid,1,instr(i.source_uid,'::')-1) FROM mail_items i WHERE i.id=mail_tasks.mail_id) WHERE account_scope='' AND mail_id IS NOT NULL")
            db.execute("UPDATE mail_jobs SET account_scope=COALESCE((SELECT substr(i.source_uid,1,instr(i.source_uid,'::')-1) FROM mail_items i WHERE i.id=mail_jobs.target_id), ?) WHERE account_scope='' AND target_id IS NOT NULL", (scope,))
            if scope:
                db.execute("UPDATE mail_jobs SET account_scope=? WHERE account_scope='' AND (target_id='' OR target_id IS NULL)", (scope,))
            batch_columns = {row[1] for row in db.execute("PRAGMA table_info(mail_reminder_batches)")}
            if "account_scope" not in batch_columns:
                db.execute("ALTER TABLE mail_reminder_batches ADD COLUMN account_scope TEXT NOT NULL DEFAULT ''")
            for batch in db.execute("SELECT batch_id,draft_ids_json FROM mail_reminder_batches WHERE account_scope=''").fetchall():
                draft_ids = json.loads(batch["draft_ids_json"] or "[]")
                batch_scope = ""
                if draft_ids:
                    row = db.execute("SELECT i.source_uid FROM mail_drafts d JOIN mail_items i ON i.id=d.mail_id WHERE d.id=?", (str(draft_ids[0]),)).fetchone()
                    if row and "::" in str(row["source_uid"]):
                        batch_scope = str(row["source_uid"]).split("::", 1)[0]
                if not batch_scope:
                    batch_scope = scope
                if batch_scope:
                    db.execute("UPDATE mail_reminder_batches SET account_scope=? WHERE batch_id=?", (batch_scope, batch["batch_id"]))

    def connection(self) -> MailConnection:
        with self._connect() as db:
            row = db.execute("SELECT * FROM mail_connection WHERE id=1").fetchone()
        if row is None:
            return MailConnection(host="imap.qq.com", port=993, username="")
        return MailConnection(
            host=row["host"], port=int(row["port"]), username=row["username"], use_ssl=bool(row["use_ssl"]),
            enabled=bool(row["enabled"]), paused=bool(row["paused"]), verified=bool(row["verified"]),
            auto_draft_ready=bool(row["auto_draft_ready"]), inbox_folder=row["inbox_folder"],
            sent_folder=row["sent_folder"], drafts_folder=row["drafts_folder"],
            last_error=row["last_error"], last_sync_at=row["last_sync_at"], updated_at=row["updated_at"],
        )

    def save_connection(self, connection: MailConnection, *, confirmed_test_marker: str = "") -> MailConnection:
        now = _now()
        with self._connect() as db:
            if confirmed_test_marker:
                db.execute("BEGIN IMMEDIATE")
                candidate = db.execute(
                    "SELECT test_draft_present FROM mail_connection_candidate WHERE id=1 AND marker=?",
                    (confirmed_test_marker,),
                ).fetchone()
                if candidate is None or not candidate["test_draft_present"]:
                    raise ValueError("QQ 测试草稿状态已变化，请重新验证")
            db.execute("""INSERT INTO mail_connection
                (id,host,port,username,use_ssl,enabled,paused,verified,auto_draft_ready,inbox_folder,sent_folder,drafts_folder,last_error,last_sync_at,updated_at)
                VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET host=excluded.host,port=excluded.port,username=excluded.username,
                use_ssl=excluded.use_ssl,enabled=excluded.enabled,paused=excluded.paused,verified=excluded.verified,
                auto_draft_ready=excluded.auto_draft_ready,inbox_folder=excluded.inbox_folder,sent_folder=excluded.sent_folder,
                drafts_folder=excluded.drafts_folder,last_error=excluded.last_error,last_sync_at=excluded.last_sync_at,updated_at=excluded.updated_at""",
                (connection.host, connection.port, connection.username, int(connection.use_ssl), int(connection.enabled), int(connection.paused), int(connection.verified), int(connection.auto_draft_ready), connection.inbox_folder, connection.sent_folder, connection.drafts_folder, connection.last_error, connection.last_sync_at, now))
            if confirmed_test_marker:
                db.execute(
                    "UPDATE mail_connection_candidate SET test_sent=1,tested_at=? WHERE id=1 AND marker=?",
                    (now, confirmed_test_marker),
                )
                db.execute(
                    "UPDATE mail_test_drafts SET status='sent',sent_at=?,error=NULL WHERE marker=?",
                    (now, confirmed_test_marker),
                )
        return MailConnection(
            host=connection.host, port=connection.port, username=connection.username, use_ssl=connection.use_ssl,
            enabled=connection.enabled, paused=connection.paused, verified=connection.verified,
            auto_draft_ready=connection.auto_draft_ready, inbox_folder=connection.inbox_folder,
            sent_folder=connection.sent_folder, drafts_folder=connection.drafts_folder,
            last_error=connection.last_error, last_sync_at=connection.last_sync_at, updated_at=now,
        )

    def update_connection(self, **fields: object) -> MailConnection:
        current = self.connection()
        values = {
            "host": current.host, "port": current.port, "username": current.username, "use_ssl": current.use_ssl,
            "enabled": current.enabled, "paused": current.paused, "verified": current.verified,
            "auto_draft_ready": current.auto_draft_ready, "inbox_folder": current.inbox_folder,
            "sent_folder": current.sent_folder, "drafts_folder": current.drafts_folder,
            "last_error": current.last_error, "last_sync_at": current.last_sync_at,
        }
        values.update({key: value for key, value in fields.items() if key in values})
        return self.save_connection(MailConnection(**values))

    @staticmethod
    def _scoped_folder(folder: str, account: str = "") -> str:
        scope = _account_scope(account)
        return f"{scope}::{folder}" if scope else folder

    @staticmethod
    def account_scope(account: str) -> str:
        return _account_scope(account)

    def checkpoint(self, folder: str, *, account: str = "") -> int:
        folder = self._scoped_folder(folder, account)
        with self._connect() as db:
            row = db.execute("SELECT last_uid FROM mail_sync_state WHERE folder=?", (folder,)).fetchone()
        return int(row[0]) if row else 0

    def set_checkpoint(self, folder: str, uid: int, *, account: str = "") -> None:
        folder = self._scoped_folder(folder, account)
        with self._connect() as db:
            db.execute("INSERT INTO mail_sync_state(folder,last_uid,updated_at) VALUES(?,?,?) ON CONFLICT(folder) DO UPDATE SET last_uid=excluded.last_uid,updated_at=excluded.updated_at", (folder, uid, _now()))

    @staticmethod
    def _item(row: sqlite3.Row) -> MailItem:
        return MailItem(id=row["id"], source_uid=row["source_uid"], mailbox=row["mailbox"], message_id=row["message_id"], thread_id=row["thread_id"], sender=row["sender"], reply_to=row["reply_to"], recipients=row["recipients"], subject=row["subject"], received_at=row["received_at"], body_text=row["body_text"], in_reply_to=row["in_reply_to"], references=row["references_text"], status=row["status"], needs_reply=None if row["needs_reply"] is None else bool(row["needs_reply"]), draft_id=row["draft_id"], last_error=row["last_error"], created_at=row["created_at"], updated_at=row["updated_at"], direction=row["direction"])

    def upsert_item(self, remote: Any, *, account: str = "", direction: str = "inbound") -> tuple[MailItem, bool]:
        now = _now()
        with self._connect() as db:
            scope = _account_scope(account)
            source_uid = f"{scope}::{remote.folder}/{remote.uid}" if scope else f"{remote.folder}/{remote.uid}"
            remote_thread_id = str(remote.thread_id)
            references = [value.strip() for value in str(remote.references or "").split() if value.strip()]
            parent_ids = [*references, str(remote.in_reply_to or "").strip()]
            thread_id = ""
            for parent_id in parent_ids:
                if not parent_id:
                    continue
                sql = "SELECT thread_id FROM mail_items WHERE message_id=?"
                args: tuple[object, ...] = (parent_id,)
                if scope:
                    sql += " AND source_uid LIKE ?"
                    args += (f"{scope}::%",)
                sql += " ORDER BY created_at LIMIT 1"
                parent = db.execute(sql, args).fetchone()
                if parent is not None:
                    thread_id = str(parent["thread_id"])
                    break
            if not thread_id:
                thread_id = remote_thread_id if scope and len(remote_thread_id) == 32 and all(char in "0123456789abcdef" for char in remote_thread_id.casefold()) else _thread_scope(remote_thread_id, account)
            existing = db.execute("SELECT * FROM mail_items WHERE source_uid=?", (source_uid,)).fetchone()
            if existing is None and scope:
                existing = db.execute("SELECT * FROM mail_items WHERE message_id=? AND source_uid LIKE ?", (remote.message_id, f"{scope}::%")).fetchone()
            if existing:
                db.execute("UPDATE mail_items SET mailbox=?,message_id=?,thread_id=?,sender=?,reply_to=?,recipients=?,subject=?,received_at=?,body_text=?,in_reply_to=?,references_text=?,updated_at=? WHERE id=?", (remote.folder, remote.message_id, thread_id, remote.sender, remote.reply_to, remote.recipients, remote.subject, remote.received_at, remote.body_text, remote.in_reply_to, remote.references, now, existing["id"]))
                if direction == "outbound" or existing["direction"] == "outbound":
                    db.execute("UPDATE mail_items SET direction='outbound',status='sent' WHERE id=?", (existing["id"],))
                row = db.execute("SELECT * FROM mail_items WHERE id=?", (existing["id"],)).fetchone()
                return self._item(row), False
            item_id = f"mail-{uuid.uuid4().hex}"
            db.execute("INSERT INTO mail_items(id,source_uid,mailbox,message_id,thread_id,sender,reply_to,recipients,subject,received_at,body_text,in_reply_to,references_text,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (item_id, source_uid, remote.folder, remote.message_id, thread_id, remote.sender, remote.reply_to, remote.recipients, remote.subject, remote.received_at, remote.body_text, remote.in_reply_to, remote.references, "new", now, now))
            if direction == "outbound":
                db.execute("UPDATE mail_items SET direction='outbound',status='sent' WHERE id=?", (item_id,))
            row = db.execute("SELECT * FROM mail_items WHERE id=?", (item_id,)).fetchone()
            return self._item(row), True

    def get_item(self, item_id: str, *, account: str = "") -> MailItem | None:
        with self._connect() as db:
            if account:
                row = db.execute("SELECT * FROM mail_items WHERE id=? AND source_uid LIKE ?", (item_id, f"{_account_scope(account)}::%")).fetchone()
            else:
                row = db.execute("SELECT * FROM mail_items WHERE id=?", (item_id,)).fetchone()
        return self._item(row) if row else None

    @staticmethod
    def item_belongs_to_account(item: MailItem, account: str) -> bool:
        scope = _account_scope(account)
        if not item.source_uid.startswith("acct-"):
            return True
        return bool(scope) and item.source_uid.startswith(f"{scope}::")

    def list_items(self, *, status: str | None = None, limit: int = 100, offset: int = 0, account: str = "") -> list[MailItem]:
        sql = "SELECT * FROM mail_items"
        args: list[object] = []
        filters = ["direction='inbound'"]
        if status:
            filters.append("status=?")
            args.append(status)
        scope = _account_scope(account)
        if scope:
            filters.append("source_uid LIKE ?")
            args.append(f"{scope}::%")
        if filters:
            sql += " WHERE " + " AND ".join(filters)
        sql += " ORDER BY received_at DESC, created_at DESC LIMIT ? OFFSET ?"
        args.extend((max(1, min(limit, 500)), max(0, int(offset))))
        with self._connect() as db:
            return [self._item(row) for row in db.execute(sql, args).fetchall()]

    def pending_items(self, *, statuses: tuple[str, ...] = ("new", "failed"), account: str = "") -> list[MailItem]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        scope = _account_scope(account)
        account_clause = " AND source_uid LIKE ?" if scope else ""
        args: tuple[object, ...] = (*statuses, f"{scope}::%") if scope else statuses
        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM mail_items WHERE direction='inbound' AND status IN ({placeholders}){account_clause} ORDER BY received_at,created_at",
                args,
            ).fetchall()
        return [self._item(row) for row in rows]

    def list_thread_ids(self, *, status: str | None = None, account: str = "") -> list[dict[str, object]]:
        sql = """SELECT thread_id, COUNT(*) AS item_count, MAX(received_at) AS latest_at
            FROM mail_items"""
        args: tuple[object, ...] = ()
        filters: list[str] = []
        if status:
            filters.append("status=?")
            args += (status,)
        if account:
            filters.append("source_uid LIKE ?")
            args += (f"{_account_scope(account)}::%",)
        if filters:
            sql += " WHERE " + " AND ".join(filters)
        sql += " GROUP BY thread_id ORDER BY latest_at DESC"
        with self._connect() as db:
            return [dict(row) for row in db.execute(sql, args).fetchall()]

    def thread_items(self, thread_id: str, *, account: str = "") -> list[MailItem]:
        with self._connect() as db:
            if account:
                rows = db.execute("SELECT * FROM mail_items WHERE thread_id=? AND source_uid LIKE ? ORDER BY received_at,created_at", (thread_id, f"{_account_scope(account)}::%")).fetchall()
            else:
                rows = db.execute("SELECT * FROM mail_items WHERE thread_id=? ORDER BY received_at,created_at", (thread_id,)).fetchall()
            return [self._item(row) for row in rows]

    def update_item(self, item_id: str, **fields: object) -> MailItem:
        allowed = {"status", "needs_reply", "draft_id", "last_error"}
        changes = {key: value for key, value in fields.items() if key in allowed}
        if not changes:
            item = self.get_item(item_id)
            if item is None:
                raise KeyError(item_id)
            return item
        assignments = ",".join(f"{key if key != 'needs_reply' else 'needs_reply'}=?" for key in changes)
        values = [int(value) if key == "needs_reply" and value is not None else value for key, value in changes.items()]
        with self._connect() as db:
            db.execute(f"UPDATE mail_items SET {assignments},updated_at=? WHERE id=?", (*values, _now(), item_id))
        item = self.get_item(item_id)
        if item is None:
            raise KeyError(item_id)
        return item

    @staticmethod
    def _draft(row: sqlite3.Row) -> MailDraft:
        return MailDraft(id=row["id"], mail_id=row["mail_id"], remote_uid=row["remote_uid"], folder=row["folder"], body=row["body"], subject=row["subject"], recipients=row["recipients"], message_id=row["message_id"], generated_hash=row["generated_hash"], remote_hash=row["remote_hash"], status=row["status"], stale=bool(row["stale"]), remote_missing=bool(row["remote_missing"]), created_at=row["created_at"], updated_at=row["updated_at"], last_error=row["last_error"])

    def get_draft_for_mail(self, mail_id: str, *, account: str = "") -> MailDraft | None:
        with self._connect() as db:
            if account:
                row = db.execute("SELECT d.* FROM mail_drafts d JOIN mail_items i ON i.id=d.mail_id WHERE d.mail_id=? AND i.source_uid LIKE ? ORDER BY d.updated_at DESC LIMIT 1", (mail_id, f"{_account_scope(account)}::%")).fetchone()
            else:
                row = db.execute("SELECT * FROM mail_drafts WHERE mail_id=? ORDER BY updated_at DESC LIMIT 1", (mail_id,)).fetchone()
        return self._draft(row) if row else None

    def save_draft(self, mail_id: str, *, task_key: str, remote_uid: str | None, folder: str, body: str, subject: str, recipients: str, message_id: str, generated_hash: str, remote_hash: str, status: DraftStatus = "created", draft_id: str | None = None) -> MailDraft:
        draft_id = draft_id or f"draft-{uuid.uuid4().hex}"
        now = _now()
        with self._connect() as db:
            # The task key is protected by a partial unique index, which SQLite
            # cannot name in an ON CONFLICT target. INSERT OR IGNORE keeps the
            # operation idempotent for both the task key and generated id.
            db.execute("""INSERT OR IGNORE INTO mail_drafts(id,mail_id,task_key,remote_uid,folder,body,subject,recipients,message_id,generated_hash,remote_hash,status,stale,remote_missing,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (draft_id, mail_id, task_key, remote_uid, folder, body, subject, recipients, message_id, generated_hash, remote_hash, status, int(status == "stale"), 0, now, now))
            row = db.execute("SELECT * FROM mail_drafts WHERE task_key=?", (task_key,)).fetchone()
        return self._draft(row)

    def draft_for_task(self, task_key: str, *, account: str = "") -> MailDraft | None:
        with self._connect() as db:
            if account:
                row = db.execute("SELECT d.* FROM mail_drafts d JOIN mail_items i ON i.id=d.mail_id WHERE d.task_key=? AND i.source_uid LIKE ?", (task_key, f"{_account_scope(account)}::%")).fetchone()
            else:
                row = db.execute("SELECT * FROM mail_drafts WHERE task_key=?", (task_key,)).fetchone()
        return self._draft(row) if row else None

    def update_draft(self, draft_id: str, **fields: object) -> MailDraft:
        allowed = {"remote_uid", "remote_hash", "status", "stale", "remote_missing", "last_error"}
        changes = {key: value for key, value in fields.items() if key in allowed}
        if changes:
            assignments = ",".join(f"{key}=?" for key in changes)
            values = [int(value) if key in {"stale", "remote_missing"} else value for key, value in changes.items()]
            with self._connect() as db:
                db.execute(f"UPDATE mail_drafts SET {assignments},updated_at=? WHERE id=?", (*values, _now(), draft_id))
        with self._connect() as db:
            row = db.execute("SELECT * FROM mail_drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise KeyError(draft_id)
        return self._draft(row)

    def list_drafts(self, limit: int = 100) -> list[MailDraft]:
        with self._connect() as db:
            return [self._draft(row) for row in db.execute("SELECT * FROM mail_drafts ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()]

    def all_drafts(self) -> list[MailDraft]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM mail_drafts ORDER BY updated_at DESC").fetchall()
        return [self._draft(row) for row in rows]

    def all_drafts_for_account(self, account: str) -> list[MailDraft]:
        scope = _account_scope(account)
        if not scope:
            return self.all_drafts()
        with self._connect() as db:
            rows = db.execute(
                """SELECT d.* FROM mail_drafts d JOIN mail_items i ON i.id=d.mail_id
                WHERE i.source_uid LIKE ? ORDER BY d.updated_at DESC""",
                (f"{scope}::%",),
            ).fetchall()
        return [self._draft(row) for row in rows]

    def list_drafts_for_mail(self, mail_id: str, *, account: str = "") -> list[MailDraft]:
        with self._connect() as db:
            if account:
                rows = db.execute("SELECT d.* FROM mail_drafts d JOIN mail_items i ON i.id=d.mail_id WHERE d.mail_id=? AND i.source_uid LIKE ? ORDER BY d.updated_at DESC", (mail_id, f"{_account_scope(account)}::%")).fetchall()
            else:
                rows = db.execute("SELECT * FROM mail_drafts WHERE mail_id=? ORDER BY updated_at DESC", (mail_id,)).fetchall()
        return [self._draft(row) for row in rows]

    def count_items(self, *, account: str = "") -> int:
        scope = _account_scope(account)
        sql, args = ("SELECT COUNT(*) FROM mail_items WHERE direction='inbound' AND source_uid LIKE ?", (f"{scope}::%",)) if scope else ("SELECT COUNT(*) FROM mail_items WHERE direction='inbound'", ())
        with self._connect() as db:
            row = db.execute(sql, args).fetchone()
        return int(row[0]) if row else 0

    def count_drafts(self, *, account: str = "") -> int:
        scope = _account_scope(account)
        sql = "SELECT COUNT(*) FROM mail_drafts"
        args: tuple[object, ...] = ()
        if scope:
            sql += " WHERE mail_id IN (SELECT id FROM mail_items WHERE source_uid LIKE ?)"
            args = (f"{scope}::%",)
        with self._connect() as db:
            row = db.execute(sql, args).fetchone()
        return int(row[0]) if row else 0

    def mark_thread_drafts_stale(self, thread_id: str, *, exclude_mail_id: str | None = None) -> int:
        """Mark existing drafts in a thread as possibly stale after new mail arrives."""
        sql = """UPDATE mail_drafts SET
            stale=1,
            status=CASE WHEN status='manual_modified' THEN 'manual_modified' ELSE 'stale' END,
            updated_at=?
            WHERE remote_missing=0 AND status NOT IN ('missing','uncertain')
            AND mail_id IN (SELECT id FROM mail_items WHERE thread_id=?)"""
        args: list[object] = [_now(), thread_id]
        if exclude_mail_id:
            sql += " AND mail_id != ?"
            args.append(exclude_mail_id)
        with self._connect() as db:
            cursor = db.execute(sql, args)
            return int(cursor.rowcount)

    def create_test_draft(self, marker: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """INSERT INTO mail_test_drafts(marker,status,created_at) VALUES(?,?,?)
                ON CONFLICT(marker) DO UPDATE SET status=CASE WHEN status='sent' THEN status ELSE 'pending' END,error=NULL""",
                (marker, "pending", _now()),
            )
            return cursor.rowcount > 0

    def mark_test_draft_created(self, marker: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE mail_test_drafts SET status='created',error=NULL WHERE marker=?", (marker,))

    def test_draft(self) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute("SELECT marker,status,created_at,sent_at,error FROM mail_test_drafts ORDER BY created_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def mark_test_draft_error(self, marker: str, error: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE mail_test_drafts SET status='failed',error=? WHERE marker=?", (error[:300], marker))

    def save_candidate(self, host: str, port: int, username: str, folders: dict[str, str], marker: str, *, present: bool) -> None:
        with self._connect() as db:
            db.execute("""INSERT INTO mail_connection_candidate(id,host,port,username,inbox_folder,sent_folder,drafts_folder,marker,connection_tested,test_draft_present,test_sent,tested_at)
                VALUES(1,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET host=excluded.host,port=excluded.port,username=excluded.username,
                inbox_folder=excluded.inbox_folder,sent_folder=excluded.sent_folder,drafts_folder=excluded.drafts_folder,marker=excluded.marker,
                connection_tested=excluded.connection_tested,test_draft_present=excluded.test_draft_present,test_sent=0,tested_at=excluded.tested_at""",
                (host, port, username, folders["inbox"], folders["sent"], folders["drafts"], marker, 1, int(present), 0, _now()))

    def begin_candidate(self, host: str, port: int, username: str) -> None:
        with self._connect() as db:
            db.execute("""INSERT INTO mail_connection_candidate(id,host,port,username,inbox_folder,sent_folder,drafts_folder,marker,connection_tested,test_draft_present,test_sent,tested_at)
                VALUES(1,?,?,?,'','','','',0,0,0,?) ON CONFLICT(id) DO UPDATE SET host=excluded.host,port=excluded.port,username=excluded.username,
                inbox_folder='',sent_folder='',drafts_folder='',marker='',connection_tested=0,test_draft_present=0,test_sent=0,tested_at=excluded.tested_at""", (host, port, username, _now()))

    def set_candidate_marker(self, marker: str, folders: dict[str, str], *, present: bool) -> None:
        with self._connect() as db:
            db.execute("UPDATE mail_connection_candidate SET inbox_folder=?,sent_folder=?,drafts_folder=?,marker=?,connection_tested=1,test_draft_present=?,tested_at=? WHERE id=1", (folders["inbox"], folders["sent"], folders["drafts"], marker, int(present), _now()))

    def candidate(self) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM mail_connection_candidate WHERE id=1").fetchone()
        return dict(row) if row else None

    def setting(self, key: str, default: str = "") -> str:
        with self._connect() as db:
            row = db.execute("SELECT value FROM mail_settings WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def thread_fingerprint(self, item: MailItem, *, account: str) -> str:
        rows = self.thread_items(item.thread_id, account=account)
        value = [(row.id, row.message_id, row.sender, row.recipients, row.subject, row.body_text,
                  row.in_reply_to, row.references, row.direction) for row in sorted(rows, key=lambda row: row.id)]
        return _hash(json.dumps(value, ensure_ascii=False))

    def save_preparation(self, mail_id: str, task_key: str, payload: dict[str, object]) -> dict[str, object]:
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO mail_preparations(id,mail_id,task_key,payload_json,created_at) VALUES(?,?,?,?,?)",
                       (f"prep-{uuid.uuid4().hex}", mail_id, task_key, json.dumps(payload, ensure_ascii=False), _now()))
            row = db.execute("SELECT * FROM mail_preparations WHERE task_key=?", (task_key,)).fetchone()
        return self._preparation(row)

    @staticmethod
    def _preparation(row) -> dict[str, object] | None:
        if row is None:
            return None
        return {**json.loads(row["payload_json"]), "id": row["id"], "mailId": row["mail_id"],
                "taskKey": row["task_key"], "createdAt": row["created_at"]}

    def preparation(self, *, account: str, preparation_id: str = "", task_key: str = "", mail_id: str = "") -> dict[str, object] | None:
        scope = _account_scope(account)
        column, value = ("p.id", preparation_id) if preparation_id else (("p.task_key", task_key) if task_key else ("p.mail_id", mail_id))
        with self._connect() as db:
            row = db.execute(f"SELECT p.* FROM mail_preparations p JOIN mail_items i ON i.id=p.mail_id WHERE {column}=? AND i.source_uid LIKE ? ORDER BY p.rowid DESC LIMIT 1",
                             (value, f"{scope}::%")).fetchone()
        return self._preparation(row)

    def attach_preparation(self, draft_id: str, preparation_id: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE mail_drafts SET preparation_id=? WHERE id=?", (preparation_id, draft_id))

    def draft_preparation(self, draft_id: str, *, account: str) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute("SELECT preparation_id FROM mail_drafts WHERE id=?", (draft_id,)).fetchone()
        return self.preparation(account=account, preparation_id=row[0]) if row and row[0] else None

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO mail_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (key, value, _now()))

    def audit(self, operation: str, source_role_id: str, run_id: str, mail_id: str | None, status: str, error: str | None = None) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO mail_audit(id,operation,source_role_id,run_id,mail_id,status,error,created_at) VALUES(?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, operation, source_role_id, run_id, mail_id, status, error[:300] if error else None, _now()))

    def create_reminder_batch(
        self,
        batch_id: str,
        draft_ids: list[str],
        *,
        failure_count: int = 0,
        failure_summaries: list[str] | None = None,
        account: str = "",
        question_ids: list[str] | None = None,
    ) -> bool:
        account = account or self.connection().username
        with self._connect() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO mail_reminder_batches
                (batch_id,account_scope,draft_ids_json,status,failure_count,failure_summary_json,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                (batch_id, _account_scope(account), json.dumps(draft_ids), "pending", max(0, int(failure_count)), json.dumps(failure_summaries or [], ensure_ascii=False), _now()),
            )
            if cursor.rowcount:
                db.execute("UPDATE mail_reminder_batches SET question_ids_json=? WHERE batch_id=?", (json.dumps(question_ids or []), batch_id))
            return cursor.rowcount > 0

    def pending_reminder_batches(self, limit: int = 50, *, account: str = "") -> list[dict[str, object]]:
        if not account:
            return []
        with self._connect() as db:
            rows = db.execute("SELECT * FROM mail_reminder_batches WHERE account_scope=? AND status IN ('pending','failed') ORDER BY created_at LIMIT ?", (_account_scope(account), max(1, min(limit, 100)))).fetchall()
        return [self._reminder_batch(row) for row in rows]

    @staticmethod
    def _reminder_batch(row: sqlite3.Row) -> dict[str, object]:
        return {
            "batchId": row["batch_id"], "draftIds": json.loads(row["draft_ids_json"]),
            "status": row["status"], "failureCount": int(row["failure_count"] or 0),
            "failureSummaries": json.loads(row["failure_summary_json"] or "[]"),
            "questionIds": json.loads(row["question_ids_json"] or "[]"),
            "roleId": row["role_id"], "lastError": row["last_error"],
            "createdAt": row["created_at"], "deliveredAt": row["delivered_at"],
        }

    def reminder_batches(self, limit: int = 20, *, account: str) -> list[dict[str, object]]:
        scope = _account_scope(account)
        if not scope:
            return []
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM mail_reminder_batches WHERE account_scope=? ORDER BY created_at DESC LIMIT ?",
                (scope, max(1, min(limit, 100))),
            ).fetchall()
        return [self._reminder_batch(row) for row in rows]

    def update_reminder_batch(self, batch_id: str, *, status: str, role_id: str | None = None, error: str | None = None, delivered: bool = False) -> None:
        with self._connect() as db:
            db.execute("UPDATE mail_reminder_batches SET status=?,role_id=?,last_error=?,delivered_at=? WHERE batch_id=?", (status, role_id, error[:300] if error else None, _now() if delivered else None, batch_id))

    def task_status(self, task_key: str, *, account: str = "") -> str | None:
        with self._connect() as db:
            if account:
                row = db.execute("SELECT status FROM mail_tasks WHERE task_key=? AND account_scope=?", (task_key, _account_scope(account))).fetchone()
            else:
                row = db.execute("SELECT status FROM mail_tasks WHERE task_key=?", (task_key,)).fetchone()
        return str(row[0]) if row else None

    def recover_interrupted_tasks(self) -> None:
        with self._connect() as db:
            db.execute("UPDATE mail_tasks SET status='uncertain',error='服务停止时任务结果待核实',updated_at=? WHERE status='running'", (_now(),))

    def list_audits(self, limit: int = 100) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM mail_audit ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
        return [dict(row) for row in rows]

    def task_claim(self, task_key: str, mail_id: str, *, account: str = "") -> bool:
        scope = _account_scope(account)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM mail_tasks WHERE task_key=? AND account_scope=?", (task_key, scope)).fetchone()
            if row and row["status"] in {"running", "completed"}:
                db.rollback()
                return False
            db.execute("""INSERT INTO mail_tasks(task_key,mail_id,account_scope,status,error,updated_at) VALUES(?,?,?,?,?,?)
                ON CONFLICT(task_key) DO UPDATE SET mail_id=excluded.mail_id,account_scope=excluded.account_scope,status='running',error=NULL,updated_at=excluded.updated_at""", (task_key, mail_id, scope, "running", None, _now()))
            db.commit()
            return True

    def task_finish(self, task_key: str, status: str, error: str | None = None, *, account: str = "") -> None:
        with self._connect() as db:
            if account:
                db.execute("UPDATE mail_tasks SET status=?,error=?,updated_at=? WHERE task_key=? AND account_scope=?", (status, error, _now(), task_key, _account_scope(account)))
            else:
                db.execute("UPDATE mail_tasks SET status=?,error=?,updated_at=? WHERE task_key=?", (status, error, _now(), task_key))

    def enqueue_job(self, *, job_id: str, request_key: str, kind: str, target_id: str = "", payload: dict[str, object] | None = None, account: str = "") -> dict[str, object]:
        now = _now()
        with self._connect() as db:
            existing = db.execute("SELECT * FROM mail_jobs WHERE request_key=?", (request_key,)).fetchone()
            if existing is not None and json.loads(existing["payload_json"]) != (payload or {}):
                raise ValueError("同一请求标识不能提交不同内容")
            db.execute(
                """INSERT OR IGNORE INTO mail_jobs
                (id,request_key,kind,target_id,account_scope,payload_json,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,'queued',?,?)""",
                (job_id, request_key, kind, target_id, _account_scope(account), json.dumps(payload or {}, ensure_ascii=False), now, now),
            )
            row = db.execute("SELECT * FROM mail_jobs WHERE request_key=?", (request_key,)).fetchone()
        return self._job(row)

    @staticmethod
    def _job(row: sqlite3.Row) -> dict[str, object]:
        return {
            "jobId": row["id"], "kind": row["kind"], "targetId": row["target_id"],
            "accountScope": row["account_scope"],
            "payload": json.loads(row["payload_json"] or "{}"), "status": row["status"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": row["error"], "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        }

    def get_job(self, job_id: str, *, account: str = "") -> dict[str, object] | None:
        with self._connect() as db:
            if account:
                row = db.execute("SELECT * FROM mail_jobs WHERE id=? AND account_scope=?", (job_id, _account_scope(account))).fetchone()
            else:
                row = db.execute("SELECT * FROM mail_jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def recover_interrupted_jobs(self) -> None:
        with self._connect() as db:
            db.execute("UPDATE mail_jobs SET status='queued',error='服务重启后恢复排队',updated_at=? WHERE status='running'", (_now(),))

    def claim_next_job(self) -> dict[str, object] | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM mail_jobs WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                db.commit()
                return None
            db.execute("UPDATE mail_jobs SET status='running',error=NULL,updated_at=? WHERE id=?", (_now(), row["id"]))
            claimed = db.execute("SELECT * FROM mail_jobs WHERE id=?", (row["id"],)).fetchone()
            db.commit()
        return self._job(claimed)

    def finish_job(self, job_id: str, *, status: str, result: dict[str, object] | None = None, error: str | None = None) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE mail_jobs SET status=?,result_json=?,error=?,updated_at=? WHERE id=?",
                (status, json.dumps(result, ensure_ascii=False) if result is not None else None, error[:500] if error else None, _now(), job_id),
            )

    def public_status(self) -> dict[str, object]:
        connection = self.connection()
        return {
            "host": connection.host, "port": connection.port, "username": connection.username,
            "useSsl": connection.use_ssl, "enabled": connection.enabled, "paused": connection.paused,
            "verified": connection.verified, "autoDraftReady": connection.auto_draft_ready,
            "folders": {"inbox": connection.inbox_folder, "sent": connection.sent_folder, "drafts": connection.drafts_folder},
            "lastError": connection.last_error, "lastSyncAt": connection.last_sync_at,
            "items": self.count_items(account=connection.username), "drafts": self.count_drafts(account=connection.username),
        }
