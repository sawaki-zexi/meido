"""Canonical member facts; observations prove the role, scope and source chat."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .external_memory import ExternalMemoryConflict


def identity_scope(channel: str, thread_id: str, metadata: dict) -> str:
    if channel in {"qq", "telegram"}:
        return "platform"
    account = metadata.get("viaAccount")
    if isinstance(account, dict):
        platform_id = account.get("platformAccountId")
        if isinstance(platform_id, str) and platform_id and platform_id != account.get("accountId"):
            return "application:" + platform_id
    return "legacy:" + thread_id


def member_key(channel: str, scope: str, sender_id: str) -> str:
    return hashlib.sha256(json.dumps([channel, scope, sender_id], ensure_ascii=False).encode()).hexdigest()


class ExternalMemberProfiles:
    def __init__(self, memory):
        self.memory, self.store = memory, memory.store
        with self.store._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS channel_external_members (
                    role_id TEXT NOT NULL, member_key TEXT NOT NULL, channel TEXT NOT NULL,
                    scope TEXT NOT NULL, sender_id TEXT NOT NULL, profile TEXT NOT NULL DEFAULT '',
                    shorthand TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'empty',
                    updated_at TEXT NOT NULL, PRIMARY KEY(role_id,member_key));
                CREATE TABLE IF NOT EXISTS channel_external_member_sources (
                    role_id TEXT NOT NULL, member_key TEXT NOT NULL, thread_id TEXT NOT NULL,
                    names_json TEXT NOT NULL DEFAULT '[]', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    PRIMARY KEY(role_id,member_key,thread_id));
                CREATE TABLE IF NOT EXISTS channel_external_member_candidates (
                    role_id TEXT NOT NULL, member_key TEXT NOT NULL, candidate_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL, profile TEXT NOT NULL, shorthand TEXT NOT NULL,
                    updated_at TEXT NOT NULL, origin TEXT NOT NULL,
                    PRIMARY KEY(role_id,member_key,candidate_id));
            """)

    def allowed(self, thread, sender_id: str, metadata: dict | None = None) -> bool:
        if not sender_id or self.memory.is_owner(thread, sender_id):
            return False
        metadata = metadata or {}
        if metadata.get("senderIsOwner") is True:
            return False
        source = metadata.get("viaAccount") or thread.metadata.get("viaAccount") or {}
        if thread.channel in {"qq", "telegram"} and sender_id == source.get("platformAccountId"):
            return False
        return True

    def observe_in(self, db, thread, metadata: dict, observed_at: str) -> str | None:
        metadata.pop("externalMemberKey", None)
        sender_id = str(metadata.get("senderId") or "")
        if not self.allowed(thread, sender_id, metadata):
            return None
        scope = identity_scope(thread.channel, thread.thread_id, metadata)
        key = member_key(thread.channel, scope, sender_id)
        stamp = datetime.fromisoformat(observed_at).astimezone(timezone.utc).isoformat()
        db.execute("INSERT OR IGNORE INTO channel_external_members(role_id,member_key,channel,scope,sender_id,updated_at) VALUES(?,?,?,?,?,?)",
            (thread.role_id, key, thread.channel, scope, sender_id, stamp))
        row = db.execute("SELECT names_json,first_seen FROM channel_external_member_sources WHERE role_id=? AND member_key=? AND thread_id=?",
            (thread.role_id, key, thread.thread_id)).fetchone()
        names = json.loads(row[0]) if row else []
        name = str(metadata.get("senderName") or "").strip()
        if name:
            stamp_for_name = max([stamp, *(item["lastSeen"] for item in names if item["name"] == name)])
            names = [item for item in names if item["name"] != name]
            names.append({"name": name, "lastSeen": stamp_for_name})
        db.execute("INSERT INTO channel_external_member_sources VALUES(?,?,?,?,?,?) ON CONFLICT(role_id,member_key,thread_id) "
            "DO UPDATE SET names_json=excluded.names_json,first_seen=MIN(first_seen,excluded.first_seen),last_seen=MAX(last_seen,excluded.last_seen)",
            (thread.role_id, key, thread.thread_id, json.dumps(names, ensure_ascii=False), stamp, stamp))
        metadata["externalMemberKey"] = key
        return key

    def _row(self, db, role_id: str, thread_id: str, key: str):
        return db.execute("SELECT m.* FROM channel_external_members m JOIN channel_external_member_sources s "
            "ON s.role_id=m.role_id AND s.member_key=m.member_key WHERE m.role_id=? AND m.member_key=? AND s.thread_id=?",
            (role_id, key, thread_id)).fetchone()

    def _detail(self, db, thread, row) -> dict:
        sources = db.execute("SELECT s.*,t.title,t.channel FROM channel_external_member_sources s "
            "JOIN channel_threads t ON t.thread_id=s.thread_id AND t.role_id=s.role_id "
            "WHERE s.role_id=? AND s.member_key=? ORDER BY s.last_seen,s.thread_id",
            (thread.role_id, row["member_key"])).fetchall()
        names = {}
        for source in sources:
            for item in json.loads(source["names_json"]):
                names[item["name"]] = max(item["lastSeen"], names.get(item["name"], ""))
        nicknames = sorted(names, key=lambda name: names[name])
        account = self.store.get_account(thread.account_id)
        return {"key": str(row["member_key"]), "channel": str(row["channel"]), "scope": str(row["scope"]),
            "senderId": str(row["sender_id"]), "name": nicknames[-1] if nicknames else str(row["sender_id"]),
            "nicknames": nicknames, "profile": str(row["profile"]), "shorthand": str(row["shorthand"]),
            "state": str(row["state"]), "updatedAt": str(row["updated_at"]),
            "revision": self.memory.revision_in(db, thread.role_id, "member", row["member_key"]),
            "readOnly": account is None or bool(thread.metadata.get("accountRemoved")),
            "candidates": [{"id": item["candidate_id"], "threadId": item["thread_id"],
                "profile": item["profile"], "shorthand": item["shorthand"], "updatedAt": item["updated_at"]}
                for item in db.execute("SELECT * FROM channel_external_member_candidates WHERE role_id=? AND member_key=? ORDER BY updated_at,candidate_id",
                    (thread.role_id, row["member_key"]))] if row["state"] == "conflict" else [],
            "sources": [{"threadId": str(source["thread_id"]), "title": str(source["title"]),
                "firstSeen": str(source["first_seen"]), "lastSeen": str(source["last_seen"])} for source in sources]}

    def list_in(self, db, thread) -> list[dict]:
        rows = db.execute("SELECT m.* FROM channel_external_members m JOIN channel_external_member_sources s "
            "ON s.role_id=m.role_id AND s.member_key=m.member_key WHERE s.role_id=? AND s.thread_id=? "
            "AND m.state IN ('published','conflict') ORDER BY m.updated_at DESC,m.member_key",
            (thread.role_id, thread.thread_id)).fetchall()
        return [self._detail(db, thread, row) for row in rows if self.allowed(thread, str(row["sender_id"]))]

    def read(self, role_id: str, thread_id: str, key: str) -> dict:
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            thread = self.memory.external_thread(role_id, thread_id)
            row = self._row(db, role_id, thread_id, key)
            if row is None or row["state"] in {"empty", "deleted"}:
                raise KeyError(key)
            if not self.allowed(thread, str(row["sender_id"])):
                raise PermissionError("主人及角色自身不提供成员档案。")
            return self._detail(db, thread, row)

    def profile(self, thread, sender_id: str, metadata: dict | None = None) -> dict:
        scope = identity_scope(thread.channel, thread.thread_id, thread.metadata if metadata is None else metadata)
        key = member_key(thread.channel, scope, sender_id)
        with self.store._connect() as db:
            row = self._row(db, thread.role_id, thread.thread_id, key)
            revision = self.memory.revision_in(db, thread.role_id, "member", key)
            if row is None or row["state"] != "published" or not self.allowed(thread, sender_id):
                return {"key": key, "profile": "", "shorthand": "", "revision": revision,
                    "state": str(row["state"]) if row else "empty"}
            return self._detail(db, thread, row)

    def add_candidate_in(self, db, thread, key: str, *, profile: str, shorthand: str, updated_at: str, origin: str) -> None:
        candidate_id = hashlib.sha256(json.dumps([thread.thread_id, profile, shorthand, updated_at, origin], ensure_ascii=False).encode()).hexdigest()
        db.execute("INSERT OR IGNORE INTO channel_external_member_candidates VALUES(?,?,?,?,?,?,?,?)",
            (thread.role_id, key, candidate_id, thread.thread_id, profile, shorthand, updated_at, origin))

    def write_in(self, db, thread, key: str, *, profile: str, shorthand: str) -> dict:
        db.execute("UPDATE channel_external_members SET profile=?,shorthand=?,state='published',updated_at=? WHERE role_id=? AND member_key=?",
            (profile, shorthand, datetime.now(timezone.utc).isoformat(), thread.role_id, key))
        self.memory.advance_revision(db, thread.role_id, "member", key)
        return self._detail(db, thread, self._row(db, thread.role_id, thread.thread_id, key))

    def save(self, role_id: str, thread_id: str, key: str, *, profile: str, shorthand: str, expected_revision: int) -> dict:
        if not isinstance(profile, str) or len(profile) > 8000 or not isinstance(shorthand, str) or len(shorthand) > 200:
            raise ValueError("档案最多 8000 字，速记最多 200 字。")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("请提供读取资料时的版本。")
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            thread = self.memory.external_thread(role_id, thread_id, write=True)
            row = self._row(db, role_id, thread_id, key)
            if row is None or row["state"] in {"empty", "deleted"}:
                raise KeyError(key)
            if not self.allowed(thread, str(row["sender_id"])):
                raise PermissionError("主人及角色自身不提供成员档案。")
            if self.memory.revision_in(db, role_id, "member", key) != expected_revision:
                raise ExternalMemoryConflict("成员档案已更新，请重新读取并合并草稿。")
            result = self.write_in(db, thread, key, profile=profile, shorthand=shorthand)
        self.store.publish(role_id, {"type": "external_memory", "roleId": role_id, "memberKey": key})
        return result

    def delete(self, role_id: str, thread_id: str, key: str, expected_revision: int) -> None:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("请提供读取资料时的版本。")
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            thread = self.memory.external_thread(role_id, thread_id, write=True)
            row = self._row(db, role_id, thread_id, key)
            if row is None or row["state"] in {"empty", "deleted"}:
                raise KeyError(key)
            if not self.allowed(thread, str(row["sender_id"])):
                raise PermissionError("主人及角色自身不提供成员档案。")
            if self.memory.revision_in(db, role_id, "member", key) != expected_revision:
                raise ExternalMemoryConflict("成员档案已更新，请重新读取后再删除。")
            db.execute("UPDATE channel_external_members SET state='deleted',profile='',shorthand='',updated_at=? "
                "WHERE role_id=? AND member_key=?", (datetime.now(timezone.utc).isoformat(), role_id, key))
            self.memory.advance_revision(db, role_id, "member", key)
        self.store.publish(role_id, {"type": "external_memory", "roleId": role_id, "memberKey": key, "deleted": True})
