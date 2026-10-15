from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
import builtins
from contextlib import contextmanager
from dataclasses import asdict
from typing import Callable

from ..session_store import SessionStore
from ..task_runtime import TaskOrigin, TaskRuntime, TaskBudget
from .time_rules import Trigger, prepare_trigger, due_occurrences


FINAL = frozenset({'sent', 'failed', 'expired', 'revoked', 'cancelled', 'interrupted', 'uncertain'})
GRACE = 300


class DeliveryDeferred(Exception):
    """The real transport proved it never entered an external attempt."""


async def _owned_call(function, *args, **kwargs):
    """Cancellation cannot detach an in-progress task handover or stop."""
    pending = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    interrupted = None
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError as error:
            interrupted = interrupted or error
    if interrupted is not None:
        # Retrieve an error as well as waiting for ownership to be settled.
        try:
            pending.result()
        except Exception:
            pass
        raise interrupted
    return pending.result()


class ScheduleApplication:
    """Durable plans and delivery outbox; no ordinary chat runs are created.

    Plan cancellation, occurrence claims and local delivery share sessions.db.
    Cross-database task submission uses an owned key and retained result ACK.
    """

    def __init__(self, sessions: SessionStore, tasks: TaskRuntime, *, clock=time.time,
                 authorized: Callable[[dict], bool], target_resolver: Callable,
                 execute: Callable, deliver: Callable, receipt: Callable, offline: Callable = lambda _: False):
        self.sessions, self.tasks, self.clock = sessions, tasks, clock
        self.authorized, self.target_resolver = authorized, target_resolver
        self.execute, self.deliver, self.receipt = execute, deliver, receipt
        self.offline = offline
        self._worker: asyncio.Task | None = None
        with self._transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY, role_id TEXT NOT NULL, scope TEXT NOT NULL,
                create_key TEXT NOT NULL UNIQUE, next_at REAL, status TEXT NOT NULL,
                remaining INTEGER, started INTEGER NOT NULL DEFAULT 0,
                delivered INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, data TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS schedule_occurrences (
                id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL, nominal REAL NOT NULL,
                execution TEXT NOT NULL, delivery TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
                task_id TEXT, attempt_id TEXT, message_id TEXT, error TEXT NOT NULL DEFAULT '',
                retry_at REAL NOT NULL DEFAULT 0, retries INTEGER NOT NULL DEFAULT 0,
                dispatched INTEGER NOT NULL DEFAULT 0, synced INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0, skipped_from REAL,
                UNIQUE(schedule_id,nominal))''')
            columns = {r[1] for r in db.execute('PRAGMA table_info(schedule_occurrences)')}
            if 'skipped_count' not in columns:
                db.execute('ALTER TABLE schedule_occurrences ADD COLUMN skipped_count INTEGER NOT NULL DEFAULT 0')
                db.execute('ALTER TABLE schedule_occurrences ADD COLUMN skipped_from REAL')
            # A process loss beyond dispatch cannot prove the platform did not send.
            db.execute("UPDATE schedule_occurrences SET delivery='uncertain',error='process interrupted during dispatch' WHERE delivery='dispatching'")
            self._recovered_plans = {row[0] for row in db.execute("SELECT id FROM schedules WHERE status='active'")}

    @contextmanager
    def _transaction(self):
        with sqlite3.connect(self.sessions.database_path, timeout=.05) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            yield db

    @staticmethod
    def _scope(origin):
        return json.dumps(origin.scope(), separators=(',', ':'))

    def _plan(self, row):
        return {**dict(row), 'data': json.loads(row['data'])}

    def _public(self, db, row):
        data = json.loads(row['data'])
        last = db.execute('SELECT * FROM schedule_occurrences WHERE schedule_id=? ORDER BY nominal DESC LIMIT 1', (row['id'],)).fetchone()
        return {'id': row['id'], 'name': data.get('name', ''), 'tier': data['tier'],
                'trigger': data['originalTrigger'], 'when': data['when'], 'timezone': data['timezone'],
                'nextAt': row['next_at'], 'remainingRuns': row['remaining'], 'planStatus': row['status'],
                'startedRuns': row['started'], 'deliveredRuns': row['delivered'], 'skippedRuns': row['skipped'],
                'receivedAt': data['receivedAt'], 'receivedTimeSource': data['receivedTimeSource'],
                'latestOccurrence': ({'id': last['id'], 'scheduledAt': last['nominal'],
                    'executionStatus': last['execution'], 'deliveryStatus': last['delivery'],
                    'taskId': last['task_id'], 'attemptId': last['attempt_id'], 'error': last['error'],
                    'skippedRuns': last['skipped_count'], 'skippedFrom': last['skipped_from']} if last else None)}

    def create(self, origin: TaskOrigin, arguments: dict, *, received_at: float,
               received_source: str, frozen: dict, admitted: Callable[[], bool] | None = None) -> dict:
        if not origin.role_id or not origin.session_key or not origin.parent_run_id or not origin.tool_call_id:
            raise PermissionError('a real host run and tool call are required')
        key = hashlib.sha256(json.dumps([origin.scope(), origin.parent_run_id, origin.tool_call_id]).encode()).hexdigest()
        with self._transaction() as db:
            existing = db.execute('SELECT * FROM schedules WHERE create_key=?', (key,)).fetchone()
            if existing is not None:
                if (admitted is not None and not admitted()) or not self.authorized(self._plan(existing)):
                    raise PermissionError('schedule permission is unavailable')
                return self._public(db, existing)
        if arguments.get('tier') not in {'instant', 'soft'}:
            raise ValueError('tier must be instant or soft')
        for field, maximum in {'name': 100, 'when': 200, 'message': 4000, 'prompt': 20000}.items():
            if field in arguments and (not isinstance(arguments[field], str) or len(arguments[field]) > maximum):
                raise ValueError(field + ' exceeds the host text budget')
        count = arguments.get('count', 1 if arguments['trigger'] in {'at', 'after'} else None)
        if count is not None and (type(count) is not int or not 1 <= count <= 1000):
            raise ValueError('count must be an integer from 1 to 1000')
        if arguments['trigger'] in {'at', 'after'} and count != 1:
            raise ValueError('single occurrence count must be 1')
        tier = arguments['tier']
        field = 'message' if tier == 'instant' else 'prompt'
        if not str(arguments.get(field, '')).strip() or ('prompt' if tier == 'instant' else 'message') in arguments:
            raise ValueError('instant requires only message; soft requires only prompt')
        trigger = prepare_trigger(arguments['trigger'], arguments['when'], arguments.get('timezone', 'Asia/Shanghai'), received_at)
        if trigger.next_at + GRACE < self.clock():
            raise ValueError('schedule expired before registration')
        data = {**arguments, 'origin': asdict(origin), 'trigger': asdict(trigger),
                'originalTrigger': arguments['trigger'], 'timezone': trigger.timezone,
                'receivedAt': received_at, 'receivedTimeSource': received_source, 'frozen': frozen}
        data['target'] = self.target_resolver(origin, arguments)
        with self._transaction() as db:
            row = db.execute('SELECT * FROM schedules WHERE create_key=?', (key,)).fetchone()
            if row:
                if (admitted is not None and not admitted()) or not self.authorized(self._plan(row)):
                    raise PermissionError('schedule permission is unavailable')
                return self._public(db, row)
            if (admitted is not None and not admitted()) or not self.authorized({'data': data, 'role_id': origin.role_id}):
                raise PermissionError('schedule permission is unavailable')
            if db.execute("SELECT COUNT(*) FROM schedules WHERE role_id=? AND status IN ('active','blocked_uncertain')", (origin.role_id,)).fetchone()[0] >= 100:
                raise ValueError('active schedule budget reached')
            if db.execute("SELECT COUNT(*) FROM schedule_occurrences o JOIN schedules s ON s.id=o.schedule_id WHERE s.role_id=? AND o.delivery NOT IN ('sent','failed','expired','revoked','cancelled','interrupted')", (origin.role_id,)).fetchone()[0] >= 1000:
                raise ValueError('unarchived occurrence budget reached')
            identifier = uuid.uuid4().hex
            db.execute('INSERT INTO schedules(id,role_id,scope,create_key,next_at,status,remaining,created_at,data) VALUES(?,?,?,?,?,?,?,?,?)',
                (identifier, origin.role_id, self._scope(origin), key, trigger.next_at, 'active', count, self.clock(), json.dumps(data)))
            return self._public(db, db.execute('SELECT * FROM schedules WHERE id=?', (identifier,)).fetchone())

    def list(self, origin: TaskOrigin) -> list[dict]:
        with self._transaction() as db:
            return [self._public(db, row) for row in db.execute('SELECT * FROM schedules WHERE role_id=? AND scope=? ORDER BY created_at DESC',
                    (origin.role_id, self._scope(origin))).fetchall()]

    def cancel(self, origin: TaskOrigin, *, identifier: str = '', name: str = '') -> dict:
        if not identifier and not name or identifier and len(identifier) < 8:
            raise ValueError('provide an unambiguous id prefix of at least 8 characters or exact name')
        with self._transaction() as db:
            rows = db.execute('SELECT * FROM schedules WHERE role_id=? AND scope=?', (origin.role_id, self._scope(origin))).fetchall()
            ids = [r for r in rows if r['id'].startswith(identifier)] if identifier else []
            names = [r for r in rows if json.loads(r['data']).get('name') == name] if name else []
            if identifier and len(ids) != 1 or name and not names or identifier and name and {r['id'] for r in ids} != {r['id'] for r in names}:
                raise ValueError('schedule is unavailable in this source')
            chosen = ids if identifier else names
            tasks: builtins.list[tuple[str, TaskOrigin]] = []
            for row in chosen:
                db.execute("UPDATE schedules SET status='cancelled',next_at=NULL WHERE id=?", (row['id'],))
                db.execute("UPDATE schedule_occurrences SET delivery='cancelled',error='plan cancelled' WHERE schedule_id=? AND dispatched=0 AND delivery NOT IN ('sent','failed','expired','revoked','interrupted','uncertain')", (row['id'],))
                tasks.extend((r[0], TaskOrigin(**json.loads(row['data'])['origin'])) for r in db.execute('SELECT task_id FROM schedule_occurrences WHERE schedule_id=? AND task_id IS NOT NULL', (row['id'],)))
        for task, task_origin in tasks:
            try:
                snapshot = self.tasks.stop(task, task_origin)
                self._update_task_status(task, snapshot.status)
                self.tasks.acknowledge_result(task, task_origin)
            except KeyError:
                pass
        return {'cancelled': len(chosen), 'ids': [r['id'] for r in chosen]}

    def _claim(self):
        now = self.clock()
        with self._transaction() as db:
            for row in db.execute("SELECT * FROM schedules WHERE status='active' AND next_at<=?", (now,)).fetchall():
                if db.execute("SELECT 1 FROM schedule_occurrences WHERE schedule_id=? AND delivery NOT IN ('sent','failed','expired','revoked','cancelled','interrupted') LIMIT 1", (row['id'],)).fetchone():
                    continue
                data = json.loads(row['data'])
                trigger = Trigger(**data['trigger'])
                remaining = row['remaining']
                consumed, nominal, next_at = due_occurrences(trigger, row['next_at'], now, limit=remaining)
                skipped = consumed - 1
                # Only the latest nominal slot may run. Startup repeat slots never burst.
                recovered_repeat = row['id'] in self._recovered_plans and trigger.kind in {'every', 'cron'}
                if recovered_repeat:
                    delivery = 'expired'
                else:
                    delivery = 'expired' if nominal + GRACE < now else 'waiting'
                if remaining is not None:
                    remaining = max(0, remaining - consumed)
                if remaining == 0:
                    next_at = None
                if db.execute('SELECT COUNT(*) FROM schedule_occurrences o JOIN schedules s ON s.id=o.schedule_id WHERE s.role_id=? AND o.delivery NOT IN (\'sent\',\'failed\',\'expired\',\'revoked\',\'cancelled\',\'interrupted\')', (row['role_id'],)).fetchone()[0] >= 1000:
                    continue
                missed = skipped + int(delivery == 'expired')
                db.execute('INSERT OR IGNORE INTO schedule_occurrences(id,schedule_id,nominal,execution,delivery,content,skipped_count,skipped_from,error) VALUES(?,?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, row['id'], nominal, 'ready' if data['tier'] == 'instant' else 'claimed', delivery, data.get('message', ''),
                     missed, row['next_at'] if missed else None, 'missed repeating slots on restart' if recovered_repeat else 'nominal time expired' if delivery == 'expired' else ''))
                db.execute('UPDATE schedules SET next_at=?,remaining=?,started=started+?,skipped=skipped+?,status=? WHERE id=?',
                    (next_at, remaining, int(delivery == 'waiting'), skipped + int(delivery == 'expired'), 'active' if next_at is not None else 'completed', row['id']))
            self._prune(db, now)
        self._recovered_plans.clear()

    def _prune(self, db, now):
        """Never prune an unacknowledged or ambiguous occurrence."""
        archived = "delivery IN ('sent','failed','expired','revoked','cancelled','interrupted') AND synced=1"
        db.execute(f'DELETE FROM schedule_occurrences WHERE {archived} AND nominal<?', (now - 30 * 86400,))
        for row in db.execute('SELECT DISTINCT role_id FROM schedules').fetchall():
            db.execute(f'''DELETE FROM schedule_occurrences WHERE id IN (
                SELECT o.id FROM schedule_occurrences o JOIN schedules s ON s.id=o.schedule_id
                WHERE s.role_id=? AND o.{archived} ORDER BY nominal DESC LIMIT -1 OFFSET 1000)''', (row[0],))
        db.execute("DELETE FROM schedules WHERE status!='active' AND status!='blocked_uncertain' AND created_at<? AND NOT EXISTS (SELECT 1 FROM schedule_occurrences WHERE schedule_id=schedules.id)", (now - 30 * 86400,))

    def _rows(self):
        with self._transaction() as db:
            return [(self._plan(row), dict(occ)) for row in db.execute('SELECT * FROM schedules').fetchall()
                    for occ in db.execute("SELECT * FROM schedule_occurrences WHERE schedule_id=? AND (delivery NOT IN ('sent','failed','expired','revoked','cancelled','interrupted','uncertain') OR synced=0)", (row['id'],)).fetchall()]

    def _update(self, identifier, **changes):
        with self._transaction() as db:
            gate = " AND delivery NOT IN ('cancelled','sent','revoked')" if 'delivery' in changes else ''
            db.execute('UPDATE schedule_occurrences SET ' + ','.join(f'{key}=?' for key in changes) + ' WHERE id=?' + gate, (*changes.values(), identifier))
            if changes.get('delivery') == 'revoked':
                db.execute("UPDATE schedules SET status='revoked',next_at=NULL WHERE id=(SELECT schedule_id FROM schedule_occurrences WHERE id=?) AND status!='cancelled'", (identifier,))

    def _update_task_status(self, task_id, status):
        with self._transaction() as db:
            db.execute('UPDATE schedule_occurrences SET execution=? WHERE task_id=?', (status, task_id))

    def _allowed(self, plan, occurrence, db=None):
        if not self.authorized(plan):
            return False
        if db is not None:
            row = db.execute('SELECT status FROM schedules WHERE id=?', (plan['id'],)).fetchone()
            occ = db.execute('SELECT delivery FROM schedule_occurrences WHERE id=?', (occurrence['id'],)).fetchone()
            return row is not None and row[0] != 'cancelled' and occ is not None and occ[0] not in {'cancelled', 'revoked'}
        with self._transaction() as connection:
            return self._allowed(plan, occurrence, connection)

    async def tick(self):
        try:
            self._claim()
            rows = self._rows()
        except sqlite3.OperationalError:
            return
        for plan, occurrence in rows:
            try:
                await self._advance(plan, occurrence)
            except PermissionError:
                if occurrence['dispatched']:
                    self._update(occurrence['id'], error='original session was removed', synced=1)
                else:
                    self._update(occurrence['id'], delivery='revoked', error='original session was removed', synced=1)
            except (sqlite3.OperationalError, OSError):
                continue

    async def _advance(self, plan, occurrence):
        data, oid = plan['data'], occurrence['id']
        origin = TaskOrigin(**data['origin'])
        if occurrence['dispatched']:
            result = self.receipt(plan, occurrence)
            if result:
                await self._record_receipt(plan, occurrence, *result)
                return
            elif occurrence['delivery'] == 'dispatching':
                # Another live worker may currently own this attempt.
                return
        if occurrence['delivery'] in FINAL or occurrence['delivery'] == 'pending':
            if occurrence['delivery'] == 'uncertain' and occurrence['dispatched']:
                await self._record_receipt(plan, occurrence, 'uncertain', occurrence['attempt_id'])
            elif occurrence['delivery'] != 'pending':
                self._update(oid, synced=1)
            return
        if occurrence['nominal'] + GRACE < self.clock():
            self._update(oid, delivery='expired', error='occurrence grace expired', synced=1)
            if occurrence['task_id']:
                snapshot = await _owned_call(self.tasks.stop, occurrence['task_id'], origin)
                self._update_task_status(occurrence['task_id'], snapshot.status)
                self.tasks.acknowledge_result(occurrence['task_id'], origin)
            return
        if occurrence['retry_at'] > self.clock():
            return
        if self.offline(plan):
            self._retry(occurrence, 'channel is temporarily offline')
            return
        if not self._allowed(plan, occurrence):
            self._update(oid, delivery='revoked', error='original permission or source revoked', synced=1)
            if occurrence['task_id']:
                snapshot = await _owned_call(self.tasks.stop, occurrence['task_id'], origin)
                self._update_task_status(occurrence['task_id'], snapshot.status)
                self.tasks.acknowledge_result(occurrence['task_id'], origin)
            return
        if data['tier'] == 'soft' and occurrence['execution'] != 'ready':
            try:
                # Both databases can safely retry this handover key; never the execution.
                task_id = occurrence['task_id']
                if task_id is None:
                    marker = 'schedule occurrence ' + oid
                    previous = next((task for task in self.tasks.list(origin, kind='schedule') if task.description == marker), None)
                    if previous is not None:
                        task_id = previous.task_id
                    else:
                        executor = await self.execute(plan)
                        task_id = await _owned_call(self.tasks.submit, origin, executor,
                            budget=TaskBudget(120, 20000), kind='schedule', description=marker,
                            authorized=lambda: self._allowed(plan, occurrence) and executor.is_authorized(), completion=False,
                            submission_key=oid, retain_until_ack=True)
                    self._update(oid, task_id=task_id, execution='running')
                    if not self._allowed(plan, occurrence):
                        await _owned_call(self.tasks.stop, task_id, origin)
                        self.tasks.acknowledge_result(task_id, origin)
                        return
                snapshot = self.tasks.query(task_id, origin)
                if snapshot.ended_at is None:
                    return
                if snapshot.status != 'succeeded' or not snapshot.output.strip():
                    self._update(oid, execution=snapshot.status, delivery=snapshot.status if snapshot.status in FINAL else 'failed',
                                 error='soft execution ' + snapshot.status, synced=1)
                    self.tasks.acknowledge_result(task_id, origin)
                    return
                self._update(oid, content=snapshot.output, execution='ready')
                self.tasks.acknowledge_result(task_id, origin)
                occurrence.update(content=snapshot.output, execution='ready', task_id=task_id)
            except (PermissionError, KeyError) as error:
                self._update(oid, execution='failed', delivery='revoked', error=str(error)[:300], synced=1)
                return
            except ValueError as error:
                self._retry(occurrence, str(error))
                return
        if data['target']['channel'] == 'desktop':
            with self._transaction() as db:
                current = db.execute('SELECT delivery FROM schedule_occurrences WHERE id=?', (oid,)).fetchone()
                if current is None or current[0] in FINAL:
                    return
                if not self._allowed(plan, occurrence, db):
                    db.execute("UPDATE schedule_occurrences SET delivery='cancelled',synced=1 WHERE id=?", (oid,))
                    return
                message = self.sessions.append_schedule_message_once(origin.role_id, data['target']['sessionKey'], plan['id'], oid,
                    occurrence['content'], authorized=lambda: self._allowed(plan, occurrence, db), connection=db)
                db.execute("UPDATE schedule_occurrences SET delivery='sent',message_id=?,synced=1 WHERE id=?", (message.id, oid))
                db.execute('UPDATE schedules SET delivered=delivered+1 WHERE id=?', (plan['id'],))
            return
        # Persist the dispatch gate before entering a network send lease.
        with self._transaction() as db:
            if not self._allowed(plan, occurrence, db):
                return
            won = db.execute("UPDATE schedule_occurrences SET delivery='dispatching',dispatched=1 WHERE id=? AND dispatched=0 AND delivery='waiting'", (oid,)).rowcount
            if won:
                message = self.sessions.append_schedule_message_once(origin.role_id, data['target']['sessionKey'], plan['id'], oid,
                    occurrence['content'], authorized=lambda: self._allowed(plan, occurrence, db), connection=db,
                    channel=data['target']['channel'], delivery_status='pending')
                db.execute('UPDATE schedule_occurrences SET message_id=? WHERE id=?', (message.id, oid))
        if not won:
            return
        try:
            status, attempt = await self.deliver(plan, occurrence, lambda: self._allowed(plan, occurrence))
        except DeliveryDeferred as error:
            self._update(oid, dispatched=0, delivery='waiting')
            self._retry(occurrence, str(error))
            return
        except PermissionError:
            with self._transaction() as db:
                row = db.execute('SELECT status FROM schedules WHERE id=?', (plan['id'],)).fetchone()
                status = 'cancelled' if row is not None and row[0] == 'cancelled' else 'revoked'
                db.execute('UPDATE schedule_occurrences SET delivery=?,error=?,synced=1 WHERE id=?',
                    (status, 'permission withdrawn before network attempt', oid))
                self.sessions.append_schedule_message_once(origin.role_id, data['target']['sessionKey'], plan['id'], oid,
                    occurrence['content'], authorized=lambda: True, connection=db, channel=data['target']['channel'], delivery_status='failed')
            return
        except BaseException as error:
            self._update(oid, delivery='uncertain', error=type(error).__name__)
            if isinstance(error, asyncio.CancelledError):
                raise
            return
        await self._record_receipt(plan, occurrence, status, attempt)

    def _retry(self, occurrence, error):
        retries = occurrence['retries'] + 1
        delay = (1, 5, 30)[min(retries - 1, 2)]
        self._update(occurrence['id'], retry_at=self.clock() + delay, retries=retries, error=error[:300])

    async def _record_receipt(self, plan, occurrence, status, attempt):
        if status not in {'sent', 'pending', 'failed', 'uncertain'}:
            status = 'uncertain'
        origin, target = TaskOrigin(**plan['data']['origin']), plan['data']['target']
        with self._transaction() as db:
            before = db.execute('SELECT delivery FROM schedule_occurrences WHERE id=?', (occurrence['id'],)).fetchone()[0]
            db.execute('UPDATE schedule_occurrences SET delivery=?,attempt_id=?,synced=0 WHERE id=?', (status, attempt, occurrence['id']))
            if status == 'sent' and before != 'sent':
                db.execute('UPDATE schedules SET delivered=delivered+1 WHERE id=?', (plan['id'],))
            if status == 'uncertain':
                db.execute("UPDATE schedules SET status='blocked_uncertain',next_at=NULL WHERE id=? AND status!='cancelled'", (plan['id'],))
        # The authoritative receipt is committed first. A local message failure
        # only leaves a dirty reconciliation; it never grants another send.
        with self._transaction() as db:
            message = self.sessions.append_schedule_message_once(origin.role_id, target['sessionKey'], plan['id'], occurrence['id'],
                occurrence['content'], authorized=lambda: True, connection=db, channel=target['channel'],
                delivery_status=status, attempt_id=attempt)
            db.execute('UPDATE schedule_occurrences SET message_id=?,synced=1 WHERE id=?', (message.id, occurrence['id']))

    def delete_role(self, role_id):
        with self._transaction() as db:
            db.execute('DELETE FROM schedule_occurrences WHERE schedule_id IN (SELECT id FROM schedules WHERE role_id=?)', (role_id,))
            db.execute('DELETE FROM schedules WHERE role_id=?', (role_id,))

    def start(self):
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            await self.tick()
            await asyncio.sleep(.25)

    async def close(self):
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
