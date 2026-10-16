from __future__ import annotations

from contextlib import contextmanager
import builtins
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from ..task_runtime import TaskOrigin


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class CodingLedger:
    """Durable CodingTask identity, admission and attempt handover."""
    def __init__(self, path: Path, *, clock=time.time):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._scan_cursor = ''
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute('''CREATE TABLE IF NOT EXISTS coding_tasks (
            id TEXT PRIMARY KEY, role_id TEXT NOT NULL, scope TEXT NOT NULL,
            executor TEXT NOT NULL, repo_id TEXT NOT NULL, profile_id TEXT NOT NULL,
            created REAL NOT NULL, current_attempt TEXT NOT NULL, body TEXT NOT NULL)''')
        self._db.execute('''CREATE TABLE IF NOT EXISTS coding_attempts (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, role_id TEXT NOT NULL,
            request_key TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
            day TEXT NOT NULL, created REAL NOT NULL, status TEXT NOT NULL,
            cleanup TEXT NOT NULL DEFAULT 'not_started', runtime_id TEXT,
            ack_dirty INTEGER NOT NULL DEFAULT 0, body TEXT NOT NULL)''')
        self._db.execute('CREATE INDEX IF NOT EXISTS coding_scope ON coding_tasks(scope,created)')
        self._db.execute('CREATE INDEX IF NOT EXISTS coding_daily ON coding_attempts(role_id,day)')
        self._db.execute('CREATE TABLE IF NOT EXISTS coding_retained_artifacts(task_id TEXT PRIMARY KEY,role_id TEXT NOT NULL,body TEXT NOT NULL)')
        self._db.commit()

    @contextmanager
    def transaction(self):
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def admit(self, origin: TaskOrigin, *, requirement: str, repository: dict, profile: dict,
              max_daily: int, task_id: str | None = None, request_id: str | None = None, admitted=lambda: True,
              native_build: dict | None = None, authority_generation: int = 0, delegation_generation: int = 0) -> tuple[dict, bool]:
        if not isinstance(requirement, str) or not 1 <= len(requirement) <= 20000 or not requirement.strip():
            raise ValueError('requirement must contain 1 to 20000 characters')
        if not origin.role_id or not origin.session_key or not (origin.tool_call_id or request_id):
            raise PermissionError('coding requires a host-bound call identity')
        scope = _json(origin.scope())
        operation = 'continue:' + task_id if task_id else 'start'
        key = hashlib.sha256(_json([scope, origin.parent_run_id, origin.tool_call_id or request_id, operation]).encode()).hexdigest()
        fingerprint = hashlib.sha256(_json([requirement, repository['id'], repository['revision'], profile['id'], profile['revision']]).encode()).hexdigest()
        with self.transaction() as db:
            if not admitted():
                raise PermissionError('coding admission cancelled or revoked')
            previous = db.execute('SELECT task_id,fingerprint,body FROM coding_attempts WHERE request_key=?', (key,)).fetchone()
            if previous is not None:
                if previous['fingerprint'] != fingerprint:
                    raise ValueError('coding request conflict')
                original = self._inspect(db, previous['task_id'], scope)
                return original | json.loads(previous['body']), False
            if task_id:
                task = self._inspect(db, task_id, scope)
                if task['status'] not in {'validated_diff', 'validated_commit', 'needs_input', 'failed', 'unknown', 'cancelled', 'timed_out', 'interrupted'}:
                    raise ValueError('coding task already has an active attempt')
                if task['cleanupState'] != 'complete' or not task.get('nativeSession'):
                    raise ValueError('coding continuation requires a verified session and complete cleanup')
                if task['repository']['revision'] != repository['revision'] or task['profile']['revision'] != profile['revision']:
                    raise PermissionError('coding grant or profile changed')
                if db.execute('SELECT COUNT(*) FROM coding_attempts WHERE task_id=?', (task_id,)).fetchone()[0] >= 10:
                    raise ValueError('coding task attempt budget reached')
            else:
                if db.execute('SELECT COUNT(*) FROM coding_tasks WHERE role_id=?', (origin.role_id,)).fetchone()[0] >= 10:
                    raise ValueError('coding retained task budget reached')
                task_id = 'coding_' + uuid.uuid4().hex
                task = {'id': task_id, 'origin': asdict(origin), 'executor': profile['executor'],
                        'repository': repository, 'profile': profile, 'createdAt': self.clock(),
                        'branch': 'codex/meido-' + uuid.uuid4().hex[:16], 'nativeSession': None}
            day = datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()
            if db.execute('SELECT COUNT(*) FROM coding_attempts WHERE role_id=? AND day=?', (origin.role_id, day)).fetchone()[0] >= max_daily:
                raise ValueError('coding daily attempt budget reached')
            active = db.execute("""SELECT t.role_id,t.repo_id,t.profile_id,a.cleanup,a.status
                FROM coding_tasks t JOIN coding_attempts a ON a.task_id=t.id
                WHERE a.status IN ('accepted','running') OR a.cleanup NOT IN ('complete','not_started')""").fetchall()
            if len(active) >= 2 or any(row['role_id'] == origin.role_id or row['repo_id'] == repository['id'] for row in active):
                raise ValueError('coding concurrency budget reached')
            if any(row['profile_id'] == profile['id'] and (row['cleanup'] == 'cleanup_failed'
                    or row['status'] not in {'accepted', 'running'} and row['cleanup'] not in {'complete', 'not_started'})
                    for row in active):
                raise ValueError('coding profile is frozen until cleanup is confirmed')
            attempt_id = 'attempt_' + uuid.uuid4().hex
            attempt = {'attemptId': attempt_id, 'origin': asdict(origin), 'requirement': requirement,
                       'runtimeTaskId': None, 'status': 'accepted', 'runtimeStatus': 'queued',
                       'nativeTurnStatus': 'not_started', 'cleanupState': 'not_started', 'dispatch': False,
                       'nativeSession': task.get('nativeSession'), 'runtimeAckDirty': False,
                       'deliveryStatus': 'pending', 'deliveryDispatched': False, 'messageSynced': False,
                       'createdAt': self.clock(), 'summary': '', 'validation': [], 'blockers': []}
            if native_build is not None:
                attempt.update(nativeBuild=json.loads(_json(native_build)), authorityGeneration=authority_generation)
            if profile['executor'] == 'codex_exec':
                attempt['delegationGeneration'] = delegation_generation
            db.execute('INSERT INTO coding_attempts(id,task_id,role_id,request_key,fingerprint,day,created,status,body) VALUES(?,?,?,?,?,?,?,?,?)',
                       (attempt_id, task_id, origin.role_id, key, fingerprint, day, self.clock(), 'accepted', _json(attempt)))
            if db.execute('SELECT 1 FROM coding_tasks WHERE id=?', (task_id,)).fetchone() is None:
                db.execute('INSERT INTO coding_tasks(id,role_id,scope,executor,repo_id,profile_id,created,current_attempt,body) VALUES(?,?,?,?,?,?,?,?,?)',
                           (task_id, origin.role_id, scope, profile['executor'], repository['id'], profile['id'], self.clock(), attempt_id, _json(task)))
            else:
                db.execute('UPDATE coding_tasks SET current_attempt=? WHERE id=?', (attempt_id, task_id))
            return task | attempt, True

    def _inspect(self, db, task_id, scope=None):
        row = db.execute('SELECT * FROM coding_tasks WHERE id=?', (task_id,)).fetchone()
        if row is None or scope is not None and row['scope'] != scope:
            raise KeyError('coding task not found in this role and original context')
        attempt = db.execute('SELECT body FROM coding_attempts WHERE id=?', (row['current_attempt'],)).fetchone()
        return json.loads(row['body']) | json.loads(attempt['body'])

    def inspect(self, task_id: str, origin: TaskOrigin) -> dict:
        with self._lock:
            return self._inspect(self._db, task_id, _json(origin.scope()))

    def list(self, origin: TaskOrigin, *, executor: str = 'herdr') -> list[dict]:
        with self._lock:
            return [self._inspect(self._db, row['id']) for row in self._db.execute(
                'SELECT id FROM coding_tasks WHERE scope=? AND executor=? ORDER BY created DESC LIMIT 10', (_json(origin.scope()), executor)).fetchall()]

    def update_attempt(self, attempt_id: str, **changes) -> dict:
        with self.transaction() as db:
            row = db.execute('SELECT task_id,body FROM coding_attempts WHERE id=?', (attempt_id,)).fetchone()
            if row is None:
                raise KeyError('coding attempt not found')
            value = json.loads(row['body']) | changes
            db.execute('UPDATE coding_attempts SET status=?,cleanup=?,runtime_id=?,ack_dirty=?,body=? WHERE id=?',
                       (value['status'], value['cleanupState'], value['runtimeTaskId'], int(value['runtimeAckDirty']), _json(value), attempt_id))
            if value.get('nativeSession'):
                task = db.execute('SELECT current_attempt,body FROM coding_tasks WHERE id=?', (row['task_id'],)).fetchone()
                if task['current_attempt'] == attempt_id:
                    data = json.loads(task['body']) | {'nativeSession': value['nativeSession']}
                    db.execute('UPDATE coding_tasks SET body=? WHERE id=?', (_json(data), row['task_id']))
            return value

    def attempt(self, attempt_id: str) -> dict:
        """Host-only observer: tools must first resolve role/source via inspect."""
        with self._lock:
            row = self._db.execute('SELECT task_id,body FROM coding_attempts WHERE id=?', (attempt_id,)).fetchone()
            if row is None:
                raise KeyError('coding attempt not found')
            task = self._db.execute('SELECT body FROM coding_tasks WHERE id=?', (row['task_id'],)).fetchone()
            return json.loads(task['body']) | json.loads(row['body'])

    def unsettled(self) -> builtins.list[dict]:
        with self._lock:
            condition = "(status IN ('accepted','running') OR cleanup NOT IN ('complete','not_started') OR ack_dirty=1 OR json_extract(body,'$.messageSynced')=0 OR (json_extract(body,'$.deliveryDispatched')=1 AND json_extract(body,'$.deliveryStatus')!='sent'))"
            rows = self._db.execute('SELECT id FROM coding_attempts WHERE ' + condition + ' AND id>? ORDER BY id LIMIT 100', (self._scan_cursor,)).fetchall()
            if not rows and self._scan_cursor:
                self._scan_cursor = ''
                rows = self._db.execute('SELECT id FROM coding_attempts WHERE ' + condition + ' ORDER BY id LIMIT 100').fetchall()
            if rows:
                self._scan_cursor = rows[-1]['id']
            return [self.attempt(row['id']) for row in rows]

    def claim_launch(self, attempt_id: str) -> bool:
        with self.transaction() as db:
            row = db.execute('SELECT body FROM coding_attempts WHERE id=?', (attempt_id,)).fetchone()
            value = json.loads(row['body'])
            if value.get('launchClaimed') or value.get('executionSealed') or value['status'] != 'accepted':
                return False
            value['launchClaimed'] = True
            db.execute('UPDATE coding_attempts SET body=? WHERE id=?', (_json(value), attempt_id))
            return True

    def finish_unsubmitted(self, attempt_id: str, *, status: str) -> bool:
        if status not in {'cancelled', 'interrupted'}:
            raise ValueError('invalid unsubmitted terminal status')
        with self.transaction() as db:
            row = db.execute('SELECT body FROM coding_attempts WHERE id=?', (attempt_id,)).fetchone()
            value = json.loads(row['body'])
            if (value['status'] not in {'accepted', 'running'} or value.get('runtimeTaskId')
                    or value['cleanupState'] != 'not_started' or value.get('resourceJobs')):
                return False
            value.update(status=status, runtimeStatus=status, nativeTurnStatus='not_started',
                         cleanupState='complete', executionSealed=True, endedAt=self.clock(),
                         blockers=['cancelled_before_dispatch' if status == 'cancelled' else 'interrupted_before_runtime_handover'])
            db.execute('UPDATE coding_attempts SET status=?,cleanup=?,body=? WHERE id=?',
                       (status, 'complete', _json(value), attempt_id))
            return True

    def update_task(self, task_id: str, **changes) -> None:
        with self.transaction() as db:
            row = db.execute('SELECT body FROM coding_tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                raise KeyError('coding task not found')
            value = json.loads(row['body']) | changes
            db.execute('UPDATE coding_tasks SET body=? WHERE id=?', (_json(value), task_id))

    def resource_job(self, attempt_id: str, name: str, active: bool):
        with self.transaction() as db:
            row = db.execute('SELECT body FROM coding_attempts WHERE id=?', (attempt_id,)).fetchone()
            value = json.loads(row['body'])
            jobs = set(value.get('resourceJobs', []))
            jobs.add(name) if active else jobs.discard(name)
            value['resourceJobs'] = sorted(jobs)
            db.execute('UPDATE coding_attempts SET body=? WHERE id=?', (_json(value), attempt_id))

    def close(self):
        self._db.close()

    def role_ids(self) -> set[str]:
        with self._lock:
            return {row[0] for row in self._db.execute('SELECT DISTINCT role_id FROM coding_tasks')}

    def role_records(self, role_id: str) -> builtins.list[dict]:
        with self._lock:
            return [self.attempt(row[0]) for row in self._db.execute(
                'SELECT id FROM coding_attempts WHERE role_id=? ORDER BY created', (role_id,)).fetchall()]

    def delete_role(self, role_id: str):
        with self.transaction() as db:
            for row in db.execute('SELECT id,body FROM coding_tasks WHERE role_id=?', (role_id,)).fetchall():
                db.execute('INSERT OR REPLACE INTO coding_retained_artifacts VALUES(?,?,?)', (row['id'], role_id, row['body']))
            db.execute('DELETE FROM coding_attempts WHERE role_id=?', (role_id,))
            db.execute('DELETE FROM coding_tasks WHERE role_id=?', (role_id,))
