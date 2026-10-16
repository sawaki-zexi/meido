"""Durable payment boundary, immutable request claims and one media outbox."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

RESERVATION_BYTES = 96 * 1024 * 1024
ROLE_MEDIA_BYTES = 1024 * 1024 * 1024
GLOBAL_MEDIA_BYTES = 5 * ROLE_MEDIA_BYTES
IN_FLIGHT = ('prepared', 'dispatching', 'received', 'local_pending')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


class GenerationLedger:
    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)
        with self.connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS novelai_generations(generation_id TEXT PRIMARY KEY, role_id TEXT NOT NULL, source_scope TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS novelai_attempts(
                    attempt_id TEXT PRIMARY KEY, generation_id TEXT NOT NULL, role_id TEXT NOT NULL,
                    source_scope TEXT NOT NULL, human_key TEXT NOT NULL, request_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, parameters TEXT NOT NULL, origin TEXT NOT NULL,
                    service_revision INTEGER NOT NULL, role_revision TEXT NOT NULL, day TEXT NOT NULL,
                    status TEXT NOT NULL, dispatched INTEGER NOT NULL DEFAULT 0, budget_released INTEGER NOT NULL DEFAULT 0,
                    reserved_bytes INTEGER NOT NULL, media_bytes INTEGER NOT NULL DEFAULT 0,
                    task_id TEXT, runtime_ack_pending INTEGER NOT NULL DEFAULT 0,
                    media_type TEXT, spool_size INTEGER NOT NULL DEFAULT 0,
                    attachments TEXT NOT NULL DEFAULT '[]', error_code TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(role_id,source_scope,request_key));
                CREATE INDEX IF NOT EXISTS novelai_history ON novelai_attempts(role_id,source_scope,created_at DESC,attempt_id DESC);
                CREATE TABLE IF NOT EXISTS novelai_human_claims(role_id TEXT NOT NULL,source_scope TEXT NOT NULL,human_key TEXT NOT NULL,attempt_id TEXT NOT NULL,PRIMARY KEY(role_id,source_scope,human_key));
                CREATE TABLE IF NOT EXISTS novelai_daily(day TEXT NOT NULL,role_id TEXT NOT NULL,attempts INTEGER NOT NULL,PRIMARY KEY(day,role_id));
                CREATE TABLE IF NOT EXISTS novelai_role_epochs(role_id TEXT PRIMARY KEY,revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS novelai_cancelled_requests(role_id TEXT NOT NULL,source_scope TEXT NOT NULL,request_key TEXT NOT NULL,PRIMARY KEY(role_id,source_scope,request_key));
                CREATE TABLE IF NOT EXISTS novelai_recovery_cursor(singleton INTEGER PRIMARY KEY CHECK(singleton=1),after_created REAL,after_attempt TEXT NOT NULL);
                INSERT OR IGNORE INTO novelai_recovery_cursor VALUES(1,NULL,'');
                CREATE TABLE IF NOT EXISTS novelai_media_outbox(attempt_id TEXT PRIMARY KEY,state TEXT NOT NULL DEFAULT 'pending',message_id TEXT,consumed INTEGER NOT NULL DEFAULT 0,receipt TEXT,message_synced INTEGER NOT NULL DEFAULT 0);
                CREATE VIEW IF NOT EXISTS novelai_generation_views AS SELECT a.*,o.state AS delivery_status FROM novelai_attempts a LEFT JOIN novelai_media_outbox o ON o.attempt_id=a.attempt_id;
            ''')
            # Recovery never launches a previous paid attempt, including prepared ones.
            for row in connection.execute("SELECT attempt_id,dispatched FROM novelai_attempts WHERE status IN ('prepared','dispatching')").fetchall():
                self._finish(connection, row['attempt_id'], 'unknown' if row['dispatched'] else 'cancelled_before_dispatch')
            connection.execute("UPDATE novelai_media_outbox SET state='uncertain' WHERE state='sending'")

    def connect(self):
        connection = sqlite3.connect(self.database_path, timeout=.2)
        connection.row_factory = sqlite3.Row
        return connection

    def _day(self, connection):
        today = datetime.now(timezone.utc).date().isoformat()
        previous = connection.execute('SELECT MAX(day) FROM novelai_daily').fetchone()[0]
        return max(today, previous or today)

    def prepare(self, *, role_id, source_scope, human_key, request_key, parameters, origin,
                service_revision, role_revision, account_limit, role_limit, fingerprint=None):
        fingerprint = fingerprint or hashlib.sha256(canonical(parameters).encode()).hexdigest()
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            previous = connection.execute('SELECT * FROM novelai_attempts WHERE role_id=? AND source_scope=? AND request_key=?', (role_id, source_scope, request_key)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise ValueError('request parameters conflict with saved request')
                return self._view(previous)
            if connection.execute('SELECT 1 FROM novelai_human_claims WHERE role_id=? AND source_scope=? AND human_key=?', (role_id, source_scope, human_key)).fetchone():
                raise ValueError('this input turn already owns a generation intent')
            if connection.execute("SELECT 1 FROM novelai_attempts WHERE status IN ('prepared','dispatching','received','local_pending') LIMIT 1").fetchone():
                raise ValueError('NovelAI account is busy')
            day = self._day(connection)
            used = {row['role_id']: row['attempts'] for row in connection.execute('SELECT role_id,attempts FROM novelai_daily WHERE day=? AND role_id IN (?,?)', (day, '*', role_id))}
            if used.get('*', 0) >= account_limit or used.get(role_id, 0) >= role_limit:
                raise ValueError('daily generation attempt budget exhausted')
            rows = connection.execute('SELECT role_id,SUM(reserved_bytes+media_bytes) AS bytes FROM novelai_attempts GROUP BY role_id').fetchall()
            if sum(row['bytes'] for row in rows) + RESERVATION_BYTES > GLOBAL_MEDIA_BYTES or sum(row['bytes'] for row in rows if row['role_id'] == role_id) + RESERVATION_BYTES > ROLE_MEDIA_BYTES:
                raise ValueError('generation media storage budget exhausted')
            generation_id, attempt_id, now = uuid.uuid4().hex, uuid.uuid4().hex, time.time()
            connection.execute('INSERT INTO novelai_generations VALUES(?,?,?,?)', (generation_id, role_id, source_scope, now))
            connection.execute('''INSERT INTO novelai_attempts(attempt_id,generation_id,role_id,source_scope,human_key,request_key,fingerprint,parameters,origin,service_revision,role_revision,day,status,reserved_bytes,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'prepared',?,?,?)''', (attempt_id, generation_id, role_id, source_scope, human_key, request_key, fingerprint, canonical(parameters), canonical(origin), service_revision, role_revision, day, RESERVATION_BYTES, now, now))
            connection.execute('INSERT INTO novelai_human_claims VALUES(?,?,?,?)', (role_id, source_scope, human_key, attempt_id))
            for owner in ('*', role_id):
                connection.execute('INSERT INTO novelai_daily VALUES(?,?,1) ON CONFLICT(day,role_id) DO UPDATE SET attempts=attempts+1', (day, owner))
            connection.execute('INSERT INTO novelai_media_outbox(attempt_id) VALUES(?)', (attempt_id,))
            # This obligation precedes Runtime admission; losing task_id cannot
            # erase the responsibility to find/copy/ACK the owned handover.
            connection.execute('UPDATE novelai_attempts SET runtime_ack_pending=1 WHERE attempt_id=?', (attempt_id,))
            return self._view(connection.execute('SELECT * FROM novelai_attempts WHERE attempt_id=?', (attempt_id,)).fetchone())

    def mark_dispatching(self, attempt_id):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute("UPDATE novelai_attempts SET status='dispatching',dispatched=1,updated_at=? WHERE attempt_id=? AND status='prepared' AND dispatched=0", (time.time(), attempt_id)).rowcount != 1:
                raise ValueError('generation dispatch is no longer admissible')

    def set_task(self, attempt_id, task_id):
        with self.connect() as connection:
            connection.execute('UPDATE novelai_attempts SET task_id=?,runtime_ack_pending=1 WHERE attempt_id=?', (task_id, attempt_id))

    def seal(self, role_id, source_scope, request_key):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('INSERT OR IGNORE INTO novelai_cancelled_requests VALUES(?,?,?)', (role_id, source_scope, request_key))
            connection.execute("UPDATE novelai_media_outbox SET state='suppressed',consumed=1,message_synced=1 WHERE state='pending' AND consumed=0 AND attempt_id IN (SELECT attempt_id FROM novelai_attempts WHERE role_id=? AND source_scope=? AND request_key=?)", (role_id, source_scope, request_key))

    def sealed(self, role_id, source_scope, request_key):
        with self.connect() as connection:
            return connection.execute('SELECT 1 FROM novelai_cancelled_requests WHERE role_id=? AND source_scope=? AND request_key=?', (role_id, source_scope, request_key)).fetchone() is not None

    def received(self, attempt_id, media_type, spool_size):
        with self.connect() as connection:
            connection.execute("UPDATE novelai_attempts SET status='received',media_type=?,spool_size=?,updated_at=? WHERE attempt_id=? AND status='dispatching'", (media_type, spool_size, time.time(), attempt_id))

    def imported(self, attempt_id, attachments):
        with self.connect() as connection:
            connection.execute("UPDATE novelai_attempts SET status='succeeded',attachments=?,reserved_bytes=0,media_bytes=?,error_code=?,updated_at=? WHERE attempt_id=? AND status IN ('received','local_pending')", (canonical(attachments), sum(item['size'] for item in attachments), 'unexpected_multiple_images' if len(attachments) > 1 else '', time.time(), attempt_id))

    def staged_images(self, attempt_id, attachments):
        """Durable import references precede deletion of the complete raw response."""
        with self.connect() as connection:
            connection.execute("UPDATE novelai_attempts SET attachments=?,updated_at=? WHERE attempt_id=? AND status IN ('received','local_pending')", (canonical(attachments), time.time(), attempt_id))

    def settled_failure(self, attempt_id, attachments, size):
        with self.connect() as connection:
            connection.execute("UPDATE novelai_attempts SET attachments=?,reserved_bytes=0,media_bytes=?,updated_at=? WHERE attempt_id=? AND status IN ('unknown','failed_response','cancelled_before_dispatch','rejected')", (canonical(attachments), size, time.time(), attempt_id))

    def finish(self, attempt_id, status, *, error_code=''):
        if status not in {'cancelled_before_dispatch', 'failed_response', 'unknown', 'local_pending', 'rejected'}:
            raise ValueError('invalid generation terminal status')
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._finish(connection, attempt_id, status, error_code=error_code)

    def _finish(self, connection, attempt_id, status, *, error_code=''):
        row = connection.execute('SELECT * FROM novelai_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
        if row is None or row['status'] == 'succeeded':
            return
        release = status in {'cancelled_before_dispatch', 'rejected'} and not row['dispatched'] and not row['budget_released']
        if release:
            for owner in ('*', row['role_id']):
                connection.execute('UPDATE novelai_daily SET attempts=MAX(0,attempts-1) WHERE day=? AND role_id=?', (row['day'], owner))
        # received/local_pending keep their full reservation until local recovery finishes.
        reserved = row['reserved_bytes'] if status in {'local_pending', 'unknown'} or row['spool_size'] else 0
        connection.execute('UPDATE novelai_attempts SET status=?,budget_released=MAX(budget_released,?),reserved_bytes=?,error_code=?,updated_at=? WHERE attempt_id=?', (status, int(release), reserved, error_code, time.time(), attempt_id))

    def get(self, role_id, source_scope, generation_id):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM novelai_generation_views WHERE role_id=? AND source_scope=? AND generation_id=? ORDER BY created_at DESC LIMIT 1', (role_id, source_scope, generation_id)).fetchone()
        if row is None:
            raise KeyError('generation not found in current source')
        return self._view(row)

    def attempt(self, attempt_id):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM novelai_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
        if row is None:
            raise KeyError('generation attempt missing')
        return dict(row)

    def request(self, role_id, source_scope, request_key):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM novelai_attempts WHERE role_id=? AND source_scope=? AND request_key=?', (role_id, source_scope, request_key)).fetchone()
        return dict(row) if row is not None else None

    def recovery_batch(self):
        """Claim one bounded page, rotating past duties that cannot finish yet."""
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            cursor = connection.execute('SELECT after_created,after_attempt FROM novelai_recovery_cursor WHERE singleton=1').fetchone()
            query = '''SELECT a.*,o.state AS outbox_state,o.message_synced FROM novelai_attempts a
                JOIN novelai_media_outbox o ON o.attempt_id=a.attempt_id
                WHERE (a.status IN ('prepared','dispatching','received','local_pending') OR a.runtime_ack_pending=1
                   OR (a.status='unknown' AND a.reserved_bytes>0)
                   OR (o.consumed=1 AND o.message_synced=0 AND o.message_id IS NOT NULL
                       AND o.state IN ('pending','sending','sent','failed','uncertain'))
                   OR (a.status='succeeded' AND o.state='pending' AND o.consumed=0))'''
            order = ' ORDER BY a.created_at,a.attempt_id LIMIT 100'
            rows = []
            if cursor['after_created'] is not None:
                rows = connection.execute(query + ' AND (a.created_at>? OR (a.created_at=? AND a.attempt_id>?))' + order,
                    (cursor['after_created'], cursor['after_created'], cursor['after_attempt'])).fetchall()
            if not rows:
                rows = connection.execute(query + order).fetchall()
            if rows:
                # Commit advancement before repairing. A blocked/failed row
                # keeps its obligation and gets another turn after wraparound.
                connection.execute('UPDATE novelai_recovery_cursor SET after_created=?,after_attempt=? WHERE singleton=1',
                    (rows[-1]['created_at'], rows[-1]['attempt_id']))
            return rows

    def list(self, role_id, source_scope=None, *, limit=20, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('history page limit must be 1..50')
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) != 32 or any(c not in '0123456789abcdef' for c in cursor)):
            raise ValueError('invalid history cursor')
        with self.connect() as connection:
            where, params = 'role_id=?', [role_id]
            if source_scope is not None:
                where += ' AND source_scope=?'
                params.append(source_scope)
            if cursor:
                old = connection.execute('SELECT created_at FROM novelai_attempts WHERE attempt_id=? AND ' + where, [cursor, *params]).fetchone()
                if old is None:
                    raise ValueError('invalid history cursor')
                where += ' AND (created_at<? OR (created_at=? AND attempt_id<?))'
                params += [old[0], old[0], cursor]
            rows = connection.execute('SELECT * FROM novelai_generation_views WHERE ' + where + ' ORDER BY created_at DESC,attempt_id DESC LIMIT ?', [*params, limit + 1]).fetchall()
        return {'generations': [self._view(row) for row in rows[:limit]], 'nextCursor': rows[limit - 1]['attempt_id'] if len(rows) > limit else None}

    def usage(self, role_id):
        with self.connect() as connection:
            day = self._day(connection)
            used = {row['role_id']: row['attempts'] for row in connection.execute('SELECT * FROM novelai_daily WHERE day=? AND role_id IN (?,?)', (day, '*', role_id))}
            sizes = connection.execute('SELECT role_id,SUM(media_bytes+reserved_bytes) AS total,SUM(reserved_bytes) AS reserved FROM novelai_attempts GROUP BY role_id').fetchall()
        return {'day': day, 'accountAttempts': used.get('*', 0), 'manualAttempts': used.get(role_id, 0),
            'roleMediaBytes': sum(row['total'] for row in sizes if row['role_id'] == role_id),
            'globalMediaBytes': sum(row['total'] for row in sizes),
            'reservedMediaBytes': sum(row['reserved'] for row in sizes if row['role_id'] == role_id)}

    @staticmethod
    def _view(row):
        return {'generationId': row['generation_id'], 'attemptId': row['attempt_id'], 'roleId': row['role_id'],
                'status': row['status'], 'parameters': json.loads(row['parameters']),
                'attachments': json.loads(row['attachments']), 'taskId': row['task_id'],
                'createdAt': datetime.fromtimestamp(row['created_at'], timezone.utc).isoformat(),
                'errorCode': row['error_code'], 'consumption': 'possible' if row['dispatched'] else 'not_dispatched',
                'deliveryStatus': row['delivery_status'] if 'delivery_status' in row.keys() else 'pending'}
