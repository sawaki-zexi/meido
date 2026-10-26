"""Maintain owner memory and isolated external profiles from channel activity.

Owner statements enter the existing role memory engine. External conversations
maintain thread notes and verified, role-owned member profiles. Listening uses
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

    def profile(self, role_id: str, thread_id: str, sender_id: str) -> dict:
        store = self._initialize()
        thread = store.get_thread(role_id, thread_id)
        if thread is None:
            raise KeyError(thread_id)
        return store.external_memory.members.profile(thread, sender_id)

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
        try:
            store.external_memory.external_thread(thread.role_id, thread_id, write=True)
        except (KeyError, PermissionError):
            return False
        bindings = store.list_identities()
        account = store.get_account(thread.account_id)
        account_identity = (account.created_at, account.platform_id)
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
        context_revision = store.external_memory.revision(thread.role_id, "context", thread_id)
        with store._connect() as db:
            context = store.external_memory.context_in(db, thread)
        snapshot = {"groupNote": context["note"], "summary": context["activity"]["text"],
            "summaryUpdatedAt": context["activity"]["updatedAt"]}
        owner_runs = set()
        if thread.chat_type == "private":
            owner_runs = {item.runId for item in store.sessions.list_messages(thread.session_key)
                if item.role == "user" and item.metadata.get("senderIsOwner") is True and item.runId}
        conversation = [message for message in batch
            if message["metadata"].get("channelTurnState") != "invalidated"
            and (message["role"] == "user" or (message["role"] == "assistant"
                and message["metadata"].get("deliveryStatus") == "sent"))
            and (thread.chat_type == "group" or (message["metadata"].get("senderIsOwner") is not True
                and not message["metadata"].get("ownerContext") and message.get("runId") not in owner_runs))]
        external = [message for message in conversation if message["role"] == "user"
            and store.external_memory.members.allowed(thread, str(message["metadata"].get("senderId") or ""), message["metadata"])]
        profiles = {}
        ambiguous = set()
        for message in external:
            sender_id = str(message["metadata"].get("senderId") or "")
            profile = store.external_memory.members.profile(thread, sender_id, message["metadata"])
            if sender_id in profiles and profiles[sender_id]["key"] != profile["key"]:
                ambiguous.add(sender_id)
            profiles[sender_id] = profile
        for sender_id in ambiguous:
            del profiles[sender_id]
        result = await self.summarize(thread, conversation, snapshot, profiles) if conversation else {"members": []}
        if not isinstance(result, dict) or not isinstance(result.get("members", []), list):
            raise ValueError("渠道记忆整理结果无效")
        valid_senders = set(profiles)
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
            try:
                store.external_memory.external_thread(thread.role_id, thread_id, write=True)
            except (KeyError, PermissionError):
                return False
            if store.list_identities() != bindings:
                return False
            account = store.get_account(thread.account_id)
            if account is None or (account.created_at, account.platform_id) != account_identity:
                return False
            row = db.execute(f"SELECT sequence FROM {cursor_table} WHERE thread_id=?", (thread_id,)).fetchone()
            if (int(row[0]) if row else 0) != cursor:
                return False
            if store.external_memory.revision_in(db, thread.role_id, "context", thread_id) != context_revision:
                return False
            for sender_id in valid_senders:
                expected = profiles[sender_id]
                if (not store.external_memory.members.allowed(thread, sender_id)
                        or store.external_memory.revision_in(db, thread.role_id, "member", expected["key"]) != expected["revision"]):
                    return False
            if conversation:
                note = str(result.get("groupNote", snapshot.get("groupNote", "")))[:12000]
                summary = str(result.get("summary", snapshot.get("summary", "")))[:12000]
                store.external_memory.write_context_in(db, thread, note=note, summary=summary)
            for item in members:
                expected = profiles[str(item["senderId"])]
                fields = {"profile": str(item.get("profile") or "")[:8000], "shorthand": str(item.get("shorthand") or "")[:200]}
                if expected.get("state") == "conflict":
                    store.external_memory.members.add_candidate_in(db, thread, expected["key"], **fields,
                        updated_at=datetime.now().astimezone().isoformat(), origin="learning")
                    store.external_memory.advance_revision(db, thread.role_id, "member", expected["key"])
                else:
                    store.external_memory.members.write_in(db, thread, expected["key"], **fields)
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
