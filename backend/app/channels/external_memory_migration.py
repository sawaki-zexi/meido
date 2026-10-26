"""One recoverable upgrade from thread profiles to verified identity profiles."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path

from .external_members import identity_scope


def prepare_backup(store) -> str:
    """Run before creating new memory tables; never replace a prior backup."""
    with store._connect() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "channel_external_migrations" in tables and db.execute(
                "SELECT 1 FROM channel_external_migrations WHERE version=1").fetchone():
            return ""
        if not db.execute("SELECT 1 FROM channel_threads LIMIT 1").fetchone():
            return ""
    backup = Path(store.database_path).with_suffix(".before-external-memory-v1.sqlite3")
    if not backup.exists():
        temporary = backup.with_name(backup.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            # The reserved write lock prevents migration/ingestion racing the
            # snapshot. A separate reader can back up under that lock in WAL.
            with store._connect() as lock, store._connect() as source:
                lock.execute("BEGIN IMMEDIATE")
                with closing(sqlite3.connect(temporary)) as destination:
                    source.backup(destination)
                    if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RuntimeError("外部记忆升级前的备份校验失败。")
                os.replace(temporary, backup)
        finally:
            temporary.unlink(missing_ok=True)
    with closing(sqlite3.connect(backup)) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("外部记忆备份损坏，请先恢复备份再升级。")
    return str(backup)


def migrate(memory, backup_path: str) -> None:
    store = memory.store
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS channel_external_migrations (version INTEGER PRIMARY KEY, backup_path TEXT NOT NULL, completed_at TEXT NOT NULL)")
        if db.execute("SELECT 1 FROM channel_external_migrations WHERE version=1").fetchone():
            return
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        threads = db.execute("SELECT role_id,thread_id FROM channel_threads WHERE channel<>'desktop'").fetchall()
        for ref in threads:
            thread = store.get_thread(ref[0], ref[1])
            observations = [dict(row) for row in db.execute(
                "SELECT metadata_json,created_at FROM messages WHERE session_key=? AND role='user' ORDER BY sequence", (thread.session_key,))]
            observations += [dict(row) for row in db.execute(
                "SELECT metadata_json,created_at FROM channel_listening_records WHERE thread_id=? ORDER BY sequence", (thread.thread_id,))]
            for observed in observations:
                observed["instant"] = datetime.fromisoformat(observed["created_at"]).astimezone(timezone.utc)
                observed["metadata"] = json.loads(observed["metadata_json"])
            observations.sort(key=lambda row: row["instant"])
            for observed in observations:
                metadata = json.loads(observed["metadata_json"])
                if metadata.get("channelTurnState") != "invalidated":
                    memory.members.observe_in(db, thread, metadata, observed["created_at"])
            if "channel_member_profiles" not in tables:
                continue
            for old in db.execute("SELECT * FROM channel_member_profiles WHERE role_id=? AND thread_id=?",
                    (thread.role_id, thread.thread_id)).fetchall():
                if not memory.members.allowed(thread, old["sender_id"]):
                    continue
                updated = datetime.fromisoformat(old["updated_at"]).astimezone(timezone.utc)
                evidence = [row for row in observations
                    if row["metadata"].get("senderId") == old["sender_id"]
                    and row["metadata"].get("channelTurnState") != "invalidated" and row["instant"] <= updated]
                if any(item["metadata"].get("senderIsOwner") is True for item in evidence):
                    continue
                scopes = {identity_scope(thread.channel, thread.thread_id, item["metadata"]) for item in evidence}
                # The current account or thread snapshot cannot prove the app
                # that owned an old open_id when its profile was generated.
                proven = len(scopes) == 1 and bool(evidence)
                metadata = dict(evidence[-1]["metadata"]) if proven else {}
                metadata["senderId"] = old["sender_id"]
                key = memory.members.observe_in(db, thread, metadata, evidence[-1]["created_at"] if proven else old["updated_at"])
                names = db.execute("SELECT name,last_seen FROM channel_member_names WHERE role_id=? AND channel=? "
                    "AND account_id=? AND chat_id=? AND chat_type=? AND sender_id=? ORDER BY last_seen,rowid",
                    (thread.role_id, thread.channel, thread.account_id, thread.chat_id, thread.chat_type, old["sender_id"])).fetchall()
                if thread.channel in {"qq", "telegram"} or not evidence:
                    for name in names:
                        memory.members.observe_in(db, thread, {**metadata, "senderName": name[0]}, name[1])
                memory.members.add_candidate_in(db, thread, key, profile=old["profile"], shorthand=old["shorthand"],
                    updated_at=old["updated_at"], origin="migration")
        keys = db.execute("SELECT DISTINCT role_id,member_key FROM channel_external_member_candidates WHERE origin='migration'").fetchall()
        for role_id, key in keys:
            if memory.revision_in(db, role_id, "member", key):
                continue
            candidates = db.execute("SELECT profile,shorthand,updated_at FROM channel_external_member_candidates WHERE role_id=? AND member_key=?",
                (role_id, key)).fetchall()
            versions = {(row[0], row[1]) for row in candidates}
            profile, shorthand = next(iter(versions)) if len(versions) == 1 else ("", "")
            db.execute("UPDATE channel_external_members SET profile=?,shorthand=?,state=?,updated_at=? WHERE role_id=? AND member_key=?",
                (profile, shorthand, "published" if len(versions) == 1 else "conflict", max(row[2] for row in candidates), role_id, key))
            memory.advance_revision(db, role_id, "member", key)
        db.execute("INSERT INTO channel_external_migrations VALUES(1,?,?)", (backup_path, datetime.now(timezone.utc).isoformat()))
