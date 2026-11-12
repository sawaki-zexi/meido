"""Maintain owner memory and isolated external profiles from channel activity.

Owner statements enter the existing role memory engine. External conversations
maintain thread notes and verified, role-owned member profiles. Listening uses
its own cursor and never advances the conversation maintenance cursor.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from collections.abc import Callable
from datetime import datetime
from .source_authority import source_messages


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
    # Coalesce brief pauses. A sweep bounds idle/retry latency to 30 seconds.
    IDLE_SECONDS = 120
    SWEEP_SECONDS = 30
    RETRY_SECONDS = 30

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
        self._locks: dict[str, asyncio.Lock] = {}
        self.owner_documents = None
        self.clock, self.sleep = time.time, asyncio.sleep
        self._retry_at: dict[str, float] = {}

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
                    thread_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL DEFAULT 0,
                    source_stamp TEXT NOT NULL DEFAULT ''
                );
            """)
            for cursor_table in ('channel_memory_cursors', 'channel_listening_cursors'):
                if 'source_stamp' not in {row[1] for row in db.execute(f'PRAGMA table_info({cursor_table})')}:
                    db.execute(f"ALTER TABLE {cursor_table} ADD COLUMN source_stamp TEXT NOT NULL DEFAULT ''")
        return store

    @staticmethod
    def _source_stamp(store, thread, through, *, listening=False):
        from .source_authority import channel_owner_binding
        from .conversation_context import PROJECTION_VERSION
        if listening:
            with store._connect() as db:
                rows = db.execute('SELECT * FROM channel_listening_records WHERE thread_id=? AND sequence<=? ORDER BY sequence', (thread.thread_id, through)).fetchall()
            messages = [store.listening._message(row) for row in rows]
        else:
            messages = [item.model_dump(mode='json') for item in store.sessions.list_messages(thread.session_key) if item.sequence <= through]
        senders = {str(item['metadata'].get('senderId') or '') for item in messages if item['role'] == 'user'}
        owners = {}
        for sender in senders:
            binding = channel_owner_binding(store, thread.channel, thread.account_id, sender)
            owners[sender] = {key: binding[key] for key in ('identityId', 'pairedAt')} if binding else None
        account = store.get_account(thread.account_id)
        messages = source_messages(messages)
        return hashlib.sha256(json.dumps([PROJECTION_VERSION, messages, owners, account.created_at if account else None],
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def profile(self, role_id: str, thread_id: str, sender_id: str) -> dict:
        store = self._initialize()
        thread = store.get_thread(role_id, thread_id)
        if thread is None:
            raise KeyError(thread_id)
        return store.external_memory.members.profile(thread, sender_id)

    def heard(self, thread) -> None:
        self.request(thread.thread_id)

    def request(self, thread_id: str) -> None:
        if self.closed or not self.enabled() or self.clock() < self._retry_at.get(thread_id, 0):
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
            if self.clock() < self._retry_at.get(thread_id, 0):
                return
            try:
                if self.owner_documents is not None:
                    store = self.store_provider()
                    with store._connect() as db:
                        row = db.execute('SELECT role_id FROM channel_threads WHERE thread_id=?', (thread_id,)).fetchone()
                    if row:
                        self.owner_documents.resume(row[0])
                while await self.consolidate(thread_id, listening=True):
                    pass
                if self.clock() < self._retry_at.get(thread_id, 0):
                    return
                while await self.consolidate(thread_id, listening=False):
                    pass
            except Exception as error:
                self._retry_at[thread_id] = self.clock() + self.RETRY_SECONDS
                self.errors.append(f"{thread_id}: {type(error).__name__}")
                del self.errors[:-100]
                self._requested.discard(thread_id)

    def _listening_ready(self, store, thread, rows, today):
        if not rows:
            return False
        if len(rows) >= self.BATCH_SIZE or rows[0]['day'] < today:
            return True
        from .conversation_context import current_timeline
        recent = current_timeline(store, thread, limit=store.listening.RECENT_LIMIT)
        if rows[0]['id'] not in {message['id'] for message in recent}:
            return True
        # Include addressed messages and temporary overflow in the idle clock.
        latest = max(datetime.fromisoformat(message['createdAt']).timestamp() for message in recent)
        return self.clock() - latest >= self.IDLE_SECONDS

    @staticmethod
    def _listening_active(store, thread):
        return (thread.channel == 'qq' and thread.chat_type == 'group'
            and store.group_listening(thread.role_id, thread.account_id, thread.chat_id))

    async def commit_turn(self, thread, owner: bool, user_message, assistant_message) -> None:
        if self.closed or not self.enabled() or assistant_message is None or assistant_message.status != "completed":
            return
        if owner:
            if inspect.iscoroutinefunction(self.owner_ingest):
                await self.owner_ingest(thread, user_message, assistant_message)
            else:
                await finish_io(asyncio.to_thread(self.owner_ingest, thread, user_message, assistant_message))
        self.request(thread.thread_id)

    async def ensure_coverage(self, role_id, session_key, message_ids):
        store = self._initialize()
        thread = store.get_thread(role_id, session_key.removeprefix('thread:'))
        if thread is None or thread.session_key != session_key:
            return False
        wanted = set(message_ids)
        selected = [item for item in store.sessions.list_messages(session_key) if item.id in wanted]
        if {item.id for item in selected} != wanted:
            return False
        through = max((item.sequence for item in selected), default=0)
        await self.consolidate(thread.thread_id, listening=False, through_sequence=through)
        with store._connect() as db:
            row = db.execute('SELECT sequence FROM channel_memory_cursors WHERE thread_id=?', (thread.thread_id,)).fetchone()
        return row is not None and row[0] >= through

    async def consolidate(self, thread_id: str, *, listening: bool, today: str | None = None, through_sequence=None) -> bool:
        async with self._locks.setdefault(thread_id, asyncio.Lock()):
            try:
                return await self._consolidate(thread_id, listening=listening, today=today, through_sequence=through_sequence)
            finally:
                if thread_id in self._retry_at:
                    self._retry_at[thread_id] = self.clock() + self.RETRY_SECONDS

    async def _consolidate(self, thread_id: str, *, listening: bool, today: str | None = None, through_sequence=None) -> bool:
        if self.closed or not self.enabled():
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
        listening_revision = store.listening.revision(thread_id)
        if listening and not self._listening_active(store, thread):
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
            if cursor:
                stamp = db.execute(f'SELECT source_stamp FROM {cursor_table} WHERE thread_id=?', (thread_id,)).fetchone()[0]
                if stamp != self._source_stamp(store, thread, cursor, listening=listening):
                    db.execute(f"UPDATE {cursor_table} SET sequence=0,source_stamp='' WHERE thread_id=?", (thread_id,))
                    cursor = 0
            if listening:
                rows = db.execute("SELECT * FROM channel_listening_records WHERE thread_id=? AND sequence>? ORDER BY sequence LIMIT ?", (thread_id, cursor, self.BATCH_SIZE)).fetchall()
                day = today or datetime.fromtimestamp(self.clock()).astimezone().date().isoformat()
                batch = [store.listening._message(row) for row in rows] if self._listening_ready(store, thread, rows, day) else []
            else:
                messages = [item for item in store.sessions.list_messages(thread.session_key) if item.sequence > cursor and item.status == "completed" and item.messageType == "text"]
                # Preserve a stable recent transcript, as the desktop maintenance does.
                selected = ([item for item in messages if item.sequence <= through_sequence] if through_sequence is not None else
                            messages[:-20][:self.BATCH_SIZE] if len(messages) >= 30 else [])
                batch = [item.model_dump(mode='json') for item in selected]
        if not batch:
            return False
        up_to = batch[-1]['sequence']
        frozen = json.dumps(source_messages(batch), sort_keys=True, ensure_ascii=False)

        def source_current():
            current_account = store.get_account(thread.account_id)
            if (self.closed or thread.role_id in self._deleting or current_account is None
                    or (current_account.created_at, current_account.platform_id) != account_identity
                    or store.list_identities() != bindings
                    or listening and (store.listening.revision(thread_id) != listening_revision
                        or not self._listening_active(store, thread))):
                return False
            if listening:
                with store._connect() as db:
                    current_rows = db.execute('SELECT * FROM channel_listening_records WHERE thread_id=? AND sequence>? AND sequence<=? ORDER BY sequence',
                        (thread_id, cursor, up_to)).fetchall()
                current_batch = [store.listening._message(row) for row in current_rows]
            else:
                current_batch = [item.model_dump(mode='json') for item in store.sessions.list_messages(thread.session_key)
                    if cursor < item.sequence <= up_to and item.status == 'completed' and item.messageType == 'text']
            return json.dumps(source_messages(current_batch), sort_keys=True, ensure_ascii=False) == frozen

        # Failed/invalidated batches remain durable. Automatic arrivals and the
        # periodic sweep retry after one bounded cooldown, never in a hot loop.
        self._retry_at[thread_id] = self.clock() + self.RETRY_SECONDS
        with store._connect() as db:
            db.execute("BEGIN")
            context = store.external_memory.context_in(db, thread)
        context_revision = context["revision"]
        snapshot = {"groupNote": context["note"], "summary": context["activity"]["text"],
            "summaryUpdatedAt": context["activity"]["updatedAt"]}
        owner_messages = [message for message in batch if message['metadata'].get('senderIsOwner') is True and message['role'] == 'user']
        if self.owner_documents is not None:
            from .source_authority import select_owner_binding
            admitted_owners = []
            for message in owner_messages:
                binding = select_owner_binding(bindings, thread.channel, thread.account_id, str(message['metadata'].get('senderId') or ''))
                if binding and datetime.fromisoformat(str(binding['pairedAt'])) <= datetime.fromisoformat(message['createdAt']):
                    admitted_owners.append(message)
            # Historical owner markings remain excluded from member profiles.
            # A new batch may retire revoked private-memory candidates while
            # still consolidating its authorized current-group conversation.
            owner_messages = admitted_owners
        owner_runs = set()
        if thread.chat_type == "private":
            owner_runs = {item.runId for item in store.sessions.list_messages(thread.session_key)
                if item.role == "user" and item.metadata.get("senderIsOwner") is True and item.runId}
        from .delivery_projection import delivered_message
        from ..models import Message
        projected = [delivered_message(Message.model_validate(message)) if message['role'] == 'assistant' else message for message in batch]
        projected = [item.model_dump(mode='json') if isinstance(item, Message) else item for item in projected if item is not None]
        conversation = [message for message in projected
            if message["metadata"].get("channelTurnState") != "invalidated"
            and (message["role"] == "user" or (message["role"] == "assistant"
                and (message["metadata"].get("deliveryStatus") == "sent" or message['metadata'].get('deliveryProjection') is True)))
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
        if not source_current():
            return False
        if not isinstance(result, dict) or not isinstance(result.get("members", []), list):
            raise ValueError("渠道记忆整理结果无效")
        valid_senders = set(profiles)
        members = [item for item in result.get("members", []) if isinstance(item, dict) and str(item.get("senderId")) in valid_senders]
        if listening and owner_messages and self.owner_statements is not None:
            await self.owner_statements(thread, owner_messages, authorized=source_current)
        elif not listening and thread.chat_type == 'group' and owner_messages and self.owner_documents is not None:
            await self.owner_documents.commit(thread, owner_messages, listening=False, authorized=source_current)
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM channel_threads WHERE role_id=? AND thread_id=?", (thread.role_id, thread_id)).fetchone() is None:
                return False
            if not source_current():
                return False
            try:
                store.external_memory.external_thread(thread.role_id, thread_id, write=True)
            except (KeyError, PermissionError):
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
                # Empty automatic updates mean no new fact, as in Shiori's
                # merge_member_profile. Explicit owner edits may still clear.
                fields = {"profile": str(item.get("profile") or expected["profile"])[:8000],
                    "shorthand": str(item.get("shorthand") or expected["shorthand"])[:200]}
                if expected.get("state") == "conflict":
                    store.external_memory.members.add_candidate_in(db, thread, expected["key"], **fields,
                        updated_at=datetime.now().astimezone().isoformat(), origin="learning")
                    store.external_memory.advance_revision(db, thread.role_id, "member", expected["key"])
                else:
                    store.external_memory.members.write_in(db, thread, expected["key"], **fields)
            db.execute(f"INSERT INTO {cursor_table}(thread_id,sequence) VALUES(?,?) ON CONFLICT(thread_id) DO UPDATE SET sequence=excluded.sequence", (thread_id, up_to))
            db.execute(f'UPDATE {cursor_table} SET source_stamp=? WHERE thread_id=?',
                (self._source_stamp(store, thread, up_to, listening=listening), thread_id))
        store.publish(thread.role_id, {"type": "group_memory", "roleId": thread.role_id, "threadId": thread_id})
        self._retry_at.pop(thread_id, None)
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
            await self.sleep(self.SWEEP_SECONDS)

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
        self._requested.clear()
        self._retry_at.clear()
        if self.owner_documents is not None:
            await self.owner_documents.drain()

    async def begin_role_deletion(self, role_id: str) -> None:
        self._deleting.add(role_id)
        if self.owner_documents is not None:
            await self.owner_documents.drain(role_id)
        with self.store_provider()._connect() as db:
            threads = {row[0] for row in db.execute("SELECT thread_id FROM channel_threads WHERE role_id=?", (role_id,))}
        tasks = [task for thread_id, task in tuple(self._tasks.items()) if thread_id in threads]
        self._requested.difference_update(threads)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for thread_id in threads:
            self._retry_at.pop(thread_id, None)
            if self._tasks.get(thread_id) in tasks:
                self._tasks.pop(thread_id, None)

    def end_role_deletion(self, role_id: str, *, deleted: bool) -> None:
        if not deleted:
            self._deleting.discard(role_id)
