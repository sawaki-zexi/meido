"""Maintain owner memory and isolated external profiles from channel activity.

Owner statements enter the existing role memory engine. Other speakers only
update the current thread's environment and member profiles. Listening uses
its own cursor and never advances the conversation maintenance cursor.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime


async def finish_io(work):
    """Complete a thread's writes before propagating coroutine cancellation."""
    pending = asyncio.create_task(work)
    cancellation = None
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError as error:
            cancellation = error
    if cancellation is not None:
        if not pending.cancelled():
            pending.exception()
        raise cancellation
    return pending.result()


class ChannelMemoryMaintenance:
    BATCH_SIZE = 50
    SWEEP_SECONDS = 30 * 60

    def __init__(self, store_provider: Callable, group_provider: Callable, summarize: Callable, owner_ingest: Callable, *, owner_statements: Callable | None = None, enabled: Callable = lambda: True) -> None:
        self.store_provider, self.group_provider = store_provider, group_provider
        self.summarize, self.owner_ingest, self.enabled = summarize, owner_ingest, enabled
        self.owner_statements = owner_statements
        self._tasks: dict[str, asyncio.Task] = {}
        self._requested: set[str] = set()
        self._sweeper: asyncio.Task | None = None
        self.errors: list[str] = []
        self.closed = False
        self._deleting: set[str] = set()

    def _initialize(self):
        store = self.store_provider()
        with store._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS channel_member_profiles (
                    role_id TEXT NOT NULL, thread_id TEXT NOT NULL, sender_id TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '', profile TEXT NOT NULL DEFAULT '', shorthand TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL, PRIMARY KEY(role_id,thread_id,sender_id)
                );
                CREATE TABLE IF NOT EXISTS channel_memory_cursors (
                    thread_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL DEFAULT 0
                );
            """)
        return store

    def profile(self, role_id: str, thread_id: str, sender_id: str) -> dict[str, str]:
        store = self._initialize()
        if store.get_thread(role_id, thread_id) is None:
            raise KeyError(thread_id)
        with store._connect() as db:
            row = db.execute("SELECT name,profile,shorthand,updated_at FROM channel_member_profiles WHERE role_id=? AND thread_id=? AND sender_id=?", (role_id, thread_id, sender_id)).fetchone()
        return {"name": row[0], "profile": row[1], "shorthand": row[2], "updatedAt": row[3]} if row else {"profile": "", "shorthand": ""}

    def heard(self, thread) -> None:
        self.request(thread.thread_id)

    def request(self, thread_id: str) -> None:
        if self.closed or not self.enabled():
            return
        with self.store_provider()._connect() as db:
            row = db.execute("SELECT role_id FROM channel_threads WHERE thread_id=?", (thread_id,)).fetchone()
        if row is None or row[0] in self._deleting:
            return
        self._requested.add(thread_id)
        if thread_id not in self._tasks:
            task = asyncio.create_task(self._run_requested(thread_id), name=f"channel-memory:{thread_id}")
            self._tasks[thread_id] = task
            task.add_done_callback(lambda completed: self._done(thread_id, completed))

    def _done(self, thread_id, task) -> None:
        if self._tasks.get(thread_id) is task:
            self._tasks.pop(thread_id, None)
        if not task.cancelled() and task.exception():
            self.errors.append(f"{thread_id}: {type(task.exception()).__name__}")
            del self.errors[:-100]
        if thread_id in self._requested and not self.closed:
            self.request(thread_id)

    async def _run_requested(self, thread_id):
        while thread_id in self._requested and not self.closed:
            self._requested.discard(thread_id)
            try:
                while await self.consolidate(thread_id, listening=True):
                    pass
                while await self.consolidate(thread_id, listening=False):
                    pass
            except Exception as error:
                self.errors.append(f"{thread_id}: {type(error).__name__}")
                del self.errors[:-100]
                self._requested.discard(thread_id)

    async def commit_turn(self, thread, owner: bool, user_message, assistant_message) -> None:
        if self.closed or not self.enabled() or assistant_message is None or assistant_message.status != "completed":
            return
        if owner:
            await finish_io(asyncio.to_thread(self.owner_ingest, thread, user_message, assistant_message))
        self.request(thread.thread_id)

    async def consolidate(self, thread_id: str, *, listening: bool, today: str | None = None) -> bool:
        if not self.enabled():
            return False
        store = self._initialize()
        with store._connect() as db:
            row = db.execute("SELECT role_id FROM channel_threads WHERE thread_id=?", (thread_id,)).fetchone()
        if row is None:
            return False
        thread = store.get_thread(row[0], thread_id)
        if thread is None or thread.channel == "desktop":
            return False
        if thread.role_id in self._deleting:
            return False
        cursor_table = "channel_listening_cursors" if listening else "channel_memory_cursors"
        with store._connect() as db:
            row = db.execute(f"SELECT sequence FROM {cursor_table} WHERE thread_id=?", (thread_id,)).fetchone()
            cursor = int(row[0]) if row else 0
            if listening:
                rows = db.execute("SELECT * FROM channel_listening_records WHERE thread_id=? AND sequence>? ORDER BY sequence LIMIT ?", (thread_id, cursor, self.BATCH_SIZE)).fetchall()
                batch = [store.listening._message(row) for row in rows]
                day = today or datetime.now().astimezone().date().isoformat()
                if len(batch) < self.BATCH_SIZE:
                    current_day = max(day, rows[-1]["day"]) if rows else day
                    batch = [message for message, row in zip(batch, rows) if row["day"] < current_day]
            else:
                messages = [item for item in store.sessions.list_messages(thread.session_key) if item.sequence > cursor and item.status == "completed" and item.messageType == "text"]
                # Preserve a stable recent transcript, as the desktop maintenance does.
                batch = [item.model_dump(mode="json") for item in messages[:-12][:self.BATCH_SIZE]] if len(messages) >= 18 else []
        if not batch:
            return False
        groups = self.group_provider()
        snapshot = dict(groups.read_context(thread.role_id, thread_id)) if thread.chat_type == "group" else {}
        profiles = {}
        for message in batch:
            sender_id = str(message["metadata"].get("senderId") or "")
            if sender_id:
                profiles[sender_id] = self.profile(thread.role_id, thread_id, sender_id)
        external = [message for message in batch if not message["metadata"].get("senderIsOwner") and message["role"] == "user"]
        result = await self.summarize(thread, external, snapshot, profiles) if external else {"members": []}
        if not isinstance(result, dict) or not isinstance(result.get("members", []), list):
            raise ValueError("渠道记忆整理结果无效")
        valid_senders = {str(message["metadata"].get("senderId")) for message in external}
        members = [item for item in result.get("members", []) if isinstance(item, dict) and str(item.get("senderId")) in valid_senders]
        owner_messages = [message for message in batch if message["metadata"].get("senderIsOwner") is True and message["role"] == "user"]
        if listening and owner_messages and self.owner_statements is not None:
            await self.owner_statements(thread, owner_messages)
        up_to = batch[-1]["sequence"]
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM channel_threads WHERE role_id=? AND thread_id=?", (thread.role_id, thread_id)).fetchone() is None:
                return False
            if thread.role_id in self._deleting:
                return False
            row = db.execute(f"SELECT sequence FROM {cursor_table} WHERE thread_id=?", (thread_id,)).fetchone()
            if (int(row[0]) if row else 0) != cursor:
                return False
            if thread.chat_type == "group":
                current = db.execute("SELECT group_note,summary,summary_updated_at FROM channel_group_context WHERE role_id=? AND thread_id=?", (thread.role_id, thread_id)).fetchone()
                actual = {"groupNote": str(current[0]), "summary": str(current[1]), "summaryUpdatedAt": str(current[2])} if current else {"groupNote": "", "summary": "", "summaryUpdatedAt": ""}
                if actual != snapshot:
                    return False
            for sender_id, expected in profiles.items():
                row = db.execute("SELECT profile,shorthand,updated_at FROM channel_member_profiles WHERE role_id=? AND thread_id=? AND sender_id=?", (thread.role_id, thread_id, sender_id)).fetchone()
                if row and row[2] != expected.get("updatedAt"):
                    return False
            now = datetime.now().astimezone().isoformat()
            if thread.chat_type == "group" and external:
                note = str(result.get("groupNote", snapshot.get("groupNote", "")))[:12000]
                summary = str(result.get("summary", snapshot.get("summary", "")))[:12000]
                db.execute("INSERT INTO channel_group_context VALUES(?,?,?,?,?,?) ON CONFLICT(role_id,thread_id) DO UPDATE SET group_note=excluded.group_note,summary=excluded.summary,summary_updated_at=excluded.summary_updated_at,updated_at=excluded.updated_at", (thread.role_id, thread_id, note, summary, now, now))
            for item in members:
                observed = db.execute("SELECT name FROM channel_member_names WHERE role_id=? AND channel=? AND account_id=? AND chat_id=? AND chat_type=? AND sender_id=? ORDER BY last_seen DESC,rowid DESC LIMIT 1", (thread.role_id, thread.channel, thread.account_id, thread.chat_id, thread.chat_type, str(item["senderId"]))).fetchone()
                db.execute("INSERT INTO channel_member_profiles VALUES(?,?,?,?,?,?,?) ON CONFLICT(role_id,thread_id,sender_id) DO UPDATE SET name=excluded.name,profile=excluded.profile,shorthand=excluded.shorthand,updated_at=excluded.updated_at", (thread.role_id, thread_id, str(item["senderId"]), str(observed[0]) if observed else "", str(item.get("profile") or "")[:8000], str(item.get("shorthand") or "")[:200], now))
            db.execute(f"INSERT INTO {cursor_table} VALUES(?,?) ON CONFLICT(thread_id) DO UPDATE SET sequence=excluded.sequence", (thread_id, up_to))
        store.publish(thread.role_id, {"type": "group_memory", "roleId": thread.role_id, "threadId": thread_id})
        return True

    async def start(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            self.closed = False
            self._sweeper = asyncio.create_task(self._sweep())

    async def _sweep(self) -> None:
        while not self.closed:
            store = self._initialize()
            for account in store.list_role_accounts_for_all_roles():
                for thread in store.list_threads(account.role_id, limit=500):
                    if thread.channel != "desktop":
                        self.request(thread.thread_id)
            await asyncio.sleep(self.SWEEP_SECONDS)

    async def close(self) -> None:
        self.closed = True
        if self._sweeper:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
        if self._tasks:
            for task in tuple(self._tasks.values()):
                task.cancel()
            await asyncio.gather(*tuple(self._tasks.values()), return_exceptions=True)
        self._sweeper = None

    async def begin_role_deletion(self, role_id: str) -> None:
        self._deleting.add(role_id)
        with self.store_provider()._connect() as db:
            threads = {row[0] for row in db.execute("SELECT thread_id FROM channel_threads WHERE role_id=?", (role_id,))}
        tasks = [task for thread_id, task in tuple(self._tasks.items()) if thread_id in threads]
        self._requested.difference_update(threads)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for thread_id in threads:
            if self._tasks.get(thread_id) in tasks:
                self._tasks.pop(thread_id, None)

    def end_role_deletion(self, role_id: str, *, deleted: bool) -> None:
        if not deleted:
            self._deleting.discard(role_id)
