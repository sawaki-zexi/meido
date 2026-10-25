"""Finite, low-priority QQ participation, independent of durable listening quota.

Only accepted input creates work. A group owns one coalescing batch; unrelated
traffic cannot extend its original deadline or replenish its call allowance.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class NaturalLimits:
    coalesce: float = 1.5
    wait: float = 3.0
    lifetime: float = 45.0
    cooldown: float = 20.0
    max_calls: int = 3
    daily_calls: int = 100
    min_role_interval: float = 3.0
    max_inputs: int = 30
    max_groups: int = 32
    context_chars: int = 8000
    text_chars: int = 280

@dataclass(frozen=True)
class NaturalInput:
    message_id: str
    sender_id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    event: Any = None

@dataclass(frozen=True)
class NaturalDecision:
    action: str
    text: str = ''
    candidate_id: str | None = None

@dataclass
class NaturalBatch:
    id: str
    thread: Any
    inputs: list[NaturalInput]
    started_at: float
    deadline: float
    calls: int = 0
    revision: int = 0
    run_id: str | None = None
    phase: str = 'waiting'
    invalidated: bool = False
    snapshot: tuple[NaturalInput, ...] = ()
    authority: Any = None
    media: Any = None
    media_units: int = 0

class NaturalQuota:
    """Role cumulative request accounting survives process restart."""
    def __init__(self, store):
        self.store = store
        with store._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS channel_natural_quota (role_id TEXT PRIMARY KEY, day TEXT NOT NULL, calls INTEGER NOT NULL, last_call REAL NOT NULL)')

    def take(self, role_id, now, *, daily, interval):
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT day,calls,last_call FROM channel_natural_quota WHERE role_id=?',(role_id,)).fetchone()
            calls = int(row[1]) if row and row[0] == day else 0
            if calls >= daily or row and now - float(row[2]) < interval:
                return False
            db.execute('INSERT INTO channel_natural_quota VALUES(?,?,?,?) ON CONFLICT(role_id) DO UPDATE SET day=excluded.day,calls=excluded.calls,last_call=excluded.last_call', (role_id,day,calls+1,now))
        return True

class NaturalScheduler:
    def __init__(self, *, decide, send, authorized, busy, quota, clock=time.time,
                 sleep=asyncio.sleep, limits=NaturalLimits(), cleanup=None):
        self.decide, self.send, self.authorized, self.busy = decide, send, authorized, busy
        self.quota, self.clock, self.sleep, self.limits = quota, clock, sleep, limits
        self.cleanup = cleanup
        self.batches: dict[str, NaturalBatch] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.cooldowns: dict[str, float] = {}

    def accept(self, thread, message: NaturalInput, *, authority=None):
        key = thread.thread_id
        batch = self.batches.get(key)
        if batch is not None:
            if batch.invalidated or any(item.message_id == message.message_id for item in batch.inputs):
                return
            received = min(self.clock(),float(message.metadata.get("hostReceivedAt",self.clock())))
            batch.started_at = min(batch.started_at,received)
            batch.deadline = min(batch.deadline,received+self.limits.lifetime)
            if self._related(batch.snapshot, message):
                batch.revision += 1
            batch.inputs.append(message)
            del batch.inputs[:-self.limits.max_inputs]
            return
        now = self.clock()
        if now < self.cooldowns.get(key, 0) or len(self.batches) >= self.limits.max_groups:
            return
        started = min(now, float(message.metadata.get("hostReceivedAt", now)))
        if now >= started + self.limits.lifetime:
            return
        batch = NaturalBatch(uuid.uuid4().hex, thread, [message], started, started+self.limits.lifetime, authority=authority)
        self.batches[key] = batch
        # Never inherit the socket intake lease; detached work must reauthorize.
        task = contextvars.Context().run(asyncio.create_task, self._run(batch))
        self.tasks[key] = task
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)

    @staticmethod
    def _related(snapshot, message):
        if not snapshot:
            return False
        ids = {item.message_id for item in snapshot}
        ids.update(str(item.metadata['replyTo']) for item in snapshot if item.metadata.get('replyTo'))
        if message.metadata.get('replyTo') in ids:
            return True
        # A sender explicitly addressing another person is not a supplement.
        if message.metadata.get('replyTo') or message.metadata.get('mentionedIds'):
            return False
        return message.sender_id in {item.sender_id for item in snapshot}

    def valid(self, batch):
        return not batch.invalidated and self.clock() < batch.deadline and self.authorized(batch)

    def invalidate(self, thread_id):
        batch = self.batches.get(thread_id)
        if batch is None:
            return
        batch.invalidated = True
        task = self.tasks.get(thread_id)
        # Once handed to platform, preserve its actual receipt. The last-safe
        # socket guard still sees invalidation and rejects not-yet-sent work.
        if task and batch.phase != 'sending':
            task.get_loop().call_soon_threadsafe(task.cancel)

    async def preempt(self, thread_id):
        task = self.tasks.get(thread_id)
        self.invalidate(thread_id)
        if task is not None and task is not asyncio.current_task():
            await self._drain_owned((task,))

    @staticmethod
    async def _drain_owned(tasks):
        from .feishu_connection import wait_owned
        async def drain():
            await asyncio.gather(*tasks, return_exceptions=True)
        await wait_owned(asyncio.create_task(drain()))

    async def close(self):
        for key in tuple(self.batches): self.invalidate(key)
        await self._drain_owned(tuple(self.tasks.values()))

    async def _run(self, batch):
        key = batch.thread.thread_id
        try:
            await self.sleep(self.limits.coalesce)
            while self.valid(batch) and batch.calls < self.limits.max_calls:
                # Do not join the ordinary role lock queue: explicit turns own
                # that queue. At most one bounded timer per participating group.
                if self.busy(batch.thread.role_id):
                    await self.sleep(min(.25, max(0.,batch.deadline-self.clock())))
                    continue
                if not self.quota.take(batch.thread.role_id,self.clock(),daily=self.limits.daily_calls,interval=self.limits.min_role_interval):
                    break
                batch.calls += 1
                batch.snapshot = tuple(batch.inputs)
                revision = batch.revision
                batch.phase = 'generating'
                decision = await self.decide(batch,batch.snapshot,lambda: self.valid(batch))
                if not self.valid(batch): break
                if batch.revision != revision:
                    continue
                if decision.action == 'wait':
                    batch.phase = 'waiting'
                    await self.sleep(min(self.limits.wait,max(0.,batch.deadline-self.clock())))
                    continue
                if decision.action != 'reply': break
                text = "\n".join(decision.text.strip().splitlines()[:8])[:self.limits.text_chars]
                if not text and not decision.candidate_id: break
                decision = NaturalDecision('reply',text,decision.candidate_id)
                batch.phase = 'sending'
                # An attempt (even uncertain/failed) cools down; never retry it.
                self.cooldowns[key] = self.clock()+self.limits.cooldown
                await self.send(batch,decision,lambda: self.valid(batch) and batch.revision == revision)
                break
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # No error spam or retry loop in the public group.
            logger.info('Natural batch ended thread=%s error=%s',key,type(error).__name__)
        finally:
            try:
                if self.cleanup is not None:
                    self.cleanup(batch)
            except Exception as error:
                logger.warning('Natural cleanup failed thread=%s error=%s',key,type(error).__name__)
            finally:
                if self.batches.get(key) is batch:
                    self.batches.pop(key,None)
                    self.tasks.pop(key,None)
            # Bound bookkeeping even for a very large observed group catalog.
            now = self.clock()
            self.cooldowns = {k:v for k,v in self.cooldowns.items() if v > now}
