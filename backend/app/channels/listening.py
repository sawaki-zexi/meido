"""Group listening records, separate from addressed conversation messages.

Like Shiori, only the first daily-cap records are durable. Later messages
remain in a bounded recent window for the next group turn, never in history.
"""
from __future__ import annotations

import json
import uuid
from collections import deque
from datetime import datetime
from typing import Any


class GroupListeningStore:
    DEFAULT_DAILY_CAP = 200
    RECENT_LIMIT = 30

    def __init__(self, store) -> None:
        self.store = store
        self._windows: dict[str, deque[dict[str, Any]]] = {}
        with store._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS channel_listening_records (
                    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    content TEXT NOT NULL, metadata_json TEXT NOT NULL,
                    external_message_id TEXT, created_at TEXT NOT NULL, day TEXT NOT NULL,
                    UNIQUE(thread_id,sequence), UNIQUE(thread_id,external_message_id)
                );
                CREATE INDEX IF NOT EXISTS channel_listening_day ON channel_listening_records(thread_id,day);
                CREATE TABLE IF NOT EXISTS channel_listening_config (
                    key TEXT PRIMARY KEY, value INTEGER
                );
                CREATE TABLE IF NOT EXISTS channel_listening_toggles (
                    thread_id TEXT NOT NULL, enabled INTEGER NOT NULL, operator TEXT NOT NULL, at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS channel_listening_cursors (
                    thread_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL DEFAULT 0
                );
            """)

    @staticmethod
    def _cap(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("每日入库上限必须是不小于 1 的整数")
        return value

    def default_cap(self) -> int:
        with self.store._connect() as db:
            row = db.execute("SELECT value FROM channel_listening_config WHERE key='default'").fetchone()
        return int(row[0]) if row else self.DEFAULT_DAILY_CAP

    def set_default_cap(self, cap: object) -> int:
        cap = self._cap(cap)
        with self.store._connect() as db:
            db.execute("INSERT INTO channel_listening_config VALUES('default',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (cap,))
        return cap

    def group(self, role_id: str, thread_id: str):
        thread = self.store.get_thread(role_id, thread_id)
        if thread is None or thread.channel != "qq" or thread.chat_type != "group":
            raise KeyError(thread_id)
        return thread

    def state(self, role_id: str, thread_id: str) -> dict[str, Any]:
        thread = self.group(role_id, thread_id)
        with self.store._connect() as db:
            cap = db.execute("SELECT value FROM channel_listening_config WHERE key=?", (thread_id,)).fetchone()
            toggles = db.execute("SELECT enabled,operator,at FROM channel_listening_toggles WHERE thread_id=? ORDER BY rowid DESC LIMIT 50", (thread_id,)).fetchall()
        return {
            "threadId": thread_id,
            "enabled": self.store.group_listening(role_id, thread.account_id, thread.chat_id),
            "dailyCap": int(cap[0]) if cap else None,
            "defaultDailyCap": self.default_cap(),
            "toggles": [{"enabled": bool(row[0]), "operator": row[1], "at": row[2]} for row in toggles],
        }

    def set_cap(self, role_id: str, thread_id: str, cap: object) -> dict[str, Any]:
        self.group(role_id, thread_id)
        with self.store._connect() as db:
            if cap is None:
                db.execute("DELETE FROM channel_listening_config WHERE key=?", (thread_id,))
            else:
                db.execute("INSERT INTO channel_listening_config VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (thread_id, self._cap(cap)))
        return self.state(role_id, thread_id)

    def log_toggle(self, thread_id: str, enabled: bool, operator: str) -> None:
        if operator not in {"user", "role"}:
            raise ValueError("未知旁听操作方")
        with self.store._connect() as db:
            db.execute("INSERT INTO channel_listening_toggles VALUES(?,?,?,?)", (thread_id, int(enabled), operator, datetime.now().astimezone().isoformat()))

    def hear(self, thread, content: str, external_message_id: str, metadata: dict[str, Any], *, timestamp: datetime | None = None) -> dict[str, Any] | None:
        self.group(thread.role_id, thread.thread_id)
        if not self.store.group_listening(thread.role_id, thread.account_id, thread.chat_id):
            return None
        moment = (timestamp or datetime.now().astimezone()).astimezone()
        state = self.state(thread.role_id, thread.thread_id)
        cap = state["dailyCap"] or state["defaultDailyCap"]
        window = self._windows.setdefault(thread.thread_id, deque(maxlen=self.RECENT_LIMIT))
        if any(item["metadata"].get("platformMessageId") == external_message_id for item in window):
            return None
        account = self.store.get_account(thread.account_id)
        source = {**metadata, "channel": thread.channel, "threadId": thread.thread_id, "accountId": thread.account_id, "direction": "inbound", "platformMessageId": external_message_id, "listening": True}
        source.pop("viaAccount", None)
        if account:
            source["viaAccount"] = account.source_snapshot()
        message = {"id": f"listen:{uuid.uuid4().hex}", "sequence": None, "role": "user", "content": content, "status": "completed", "createdAt": moment.isoformat(), "metadata": source}
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if external_message_id and db.execute("SELECT 1 FROM channel_listening_records WHERE thread_id=? AND external_message_id=?", (thread.thread_id, external_message_id)).fetchone():
                return None
            count = db.execute("SELECT COUNT(*) FROM channel_listening_records WHERE thread_id=? AND day=?", (thread.thread_id, moment.date().isoformat())).fetchone()[0]
            if count >= cap:
                window.append(message)
                return message
            sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM channel_listening_records WHERE thread_id=?", (thread.thread_id,)).fetchone()[0]
            message["sequence"] = int(sequence)
            db.execute("INSERT INTO channel_listening_records VALUES(?,?,?,?,?,?,?,?)", (message["id"], thread.thread_id, sequence, content, json.dumps(source, ensure_ascii=False), external_message_id or None, moment.isoformat(), moment.date().isoformat()))
            db.execute("UPDATE channel_threads SET last_activity=? WHERE thread_id=?", (moment.isoformat(), thread.thread_id))
        self.store.publish(thread.role_id, {"type": "listening_heard", "roleId": thread.role_id, "threadId": thread.thread_id, "message": message})
        return message

    @staticmethod
    def _message(row) -> dict[str, Any]:
        return {"id": row["id"], "sequence": int(row["sequence"]), "role": "user", "content": row["content"], "status": "completed", "createdAt": row["created_at"], "metadata": json.loads(row["metadata_json"])}

    def page(self, role_id: str, thread_id: str, *, before: int | None = None, limit: int = 50) -> dict[str, Any]:
        self.group(role_id, thread_id)
        limit = max(1, min(limit, 100))
        where, params = "thread_id=?", [thread_id]
        if before is not None:
            where += " AND sequence<?"
            params.append(before)
        with self.store._connect() as db:
            rows = db.execute(f"SELECT * FROM channel_listening_records WHERE {where} ORDER BY sequence DESC LIMIT ?", (*params, limit + 1)).fetchall()
        messages = [self._message(row) for row in reversed(rows[:limit])]
        return {"messages": messages, "hasMore": len(rows) > limit, "nextBefore": messages[0]["sequence"] if messages else None}

    def recent(self, role_id: str, thread_id: str) -> list[dict[str, Any]]:
        records = self.page(role_id, thread_id, limit=self.RECENT_LIMIT)["messages"]
        merged = [*records, *self._windows.get(thread_id, ())]
        merged.sort(key=lambda item: datetime.fromisoformat(item["createdAt"]))
        return merged[-self.RECENT_LIMIT:]

    def context(self, role_id: str, thread_id: str) -> str:
        lines, budget = [], 3000
        for item in reversed(self.recent(role_id, thread_id)):
            meta = item["metadata"]
            speaker = "主人" if meta.get("senderIsOwner") else meta.get("senderName") or meta.get("senderId") or "成员"
            line = f"[{item['createdAt']}] {speaker}（ID {meta.get('senderId', '')}）：{item['content'][:200]}"
            if len(line) + 1 > budget:
                break
            budget -= len(line) + 1
            lines.append(line)
        return "群里最近的其他消息（旁听，未触发角色）：\n" + "\n".join(reversed(lines)) if lines else ""

    def clear(self, role_id: str, thread_id: str) -> int:
        self.group(role_id, thread_id)
        with self.store._connect() as db:
            count = db.execute("SELECT COUNT(*) FROM channel_listening_records WHERE thread_id=?", (thread_id,)).fetchone()[0]
            db.execute("DELETE FROM channel_listening_records WHERE thread_id=?", (thread_id,))
            db.execute("DELETE FROM channel_listening_cursors WHERE thread_id=?", (thread_id,))
        self._windows.pop(thread_id, None)
        self.store.publish(role_id, {"type": "listening_cleared", "roleId": role_id, "threadId": thread_id})
        return int(count)

    def delete_role(self, role_id: str) -> None:
        with self.store._connect() as db:
            threads = [row[0] for row in db.execute("SELECT thread_id FROM channel_threads WHERE role_id=?", (role_id,))]
            for thread_id in threads:
                for table in ("channel_listening_records", "channel_listening_toggles", "channel_listening_cursors"):
                    db.execute(f"DELETE FROM {table} WHERE thread_id=?", (thread_id,))
                db.execute("DELETE FROM channel_listening_config WHERE key=?", (thread_id,))
                self._windows.pop(thread_id, None)
