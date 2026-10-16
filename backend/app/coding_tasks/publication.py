"""Host-only reviewed Git operations, owned by the existing Coding module."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
import uuid

from ..task_runtime import TaskBudget, TaskOrigin, TaskOutcome
from ..task_runtime.runtime import TERMINAL
from .git_repository import git_environment, publication_remote
from .ledger import _json
from .owned_process import CleanupFailed, OwnedProcess, named_job_active


def fingerprint(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def operation_public(record):
    keys = {'id', 'taskId', 'attemptId', 'reviewId', 'action', 'status', 'runtimeStatus', 'runtimeTaskId',
            'runtimeAckDirty', 'cleanupState', 'deliveryStatus', 'commitSha', 'prUrl', 'blockers',
            'createdAt', 'endedAt', 'pushDispatched', 'prDispatched', 'pushConfirmed', 'observedAt'}
    return {key: record[key] for key in keys if key in record}


class OwnedGit:
    """A single owner holds every short-lived Git process and its pipes."""
    def __init__(self, root, identifier, journal, check, storage=lambda: None):
        self.root, self.identifier = Path(root), identifier
        self.journal, self.check = journal, check
        self.storage = storage
        self.process = None
        self._lock = threading.RLock()

    def run(self, root, *arguments, input=None, env=None, limit=1024 * 1024):
        self.check()
        self.storage()
        executable = shutil.which('git')
        if executable is None:
            raise ValueError('Git executable unavailable')
        with self._lock:
            self.check()
            process = OwnedProcess([executable, '--no-replace-objects', '-C', str(root), *arguments],
                Path(root), env or git_environment(), output_bytes=limit,
                job_name='Local\\Meido.Coding.' + self.identifier + '.git', journal=self.journal)
            self.process = process
        response = process.communicate(timeout=self.check(), input=input, check=self.check)
        self.storage()
        if response.returncode:
            raise ValueError('fixed Git operation failed')
        return response.stdout.decode('utf-8', errors='strict').strip()

    def close(self):
        with self._lock:
            process = self.process
        if process is not None:
            process.close()


class PublicationOperations:
    """One owner Interface for review, one-use approval, observation and stop."""
    def __init__(self, application, *, credentials=None, platform_factory=None, transport_factory=None):
        self.application = application
        self.ledger = application.ledger
        self.root = application.root / 'publication'
        self.root.mkdir(exist_ok=True)
        from .artifact_budget import identity
        self._root_identity = identity(self.root)
        self.credentials = credentials
        self.platform_factory, self.transport_factory = platform_factory, transport_factory
        self._owners = {}
        self._closed = False
        self._scan_cursor = ''
        with self.ledger.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS coding_reviews(
                id TEXT PRIMARY KEY,role_id TEXT NOT NULL,task_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,consumed_by TEXT,body TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS coding_owner_operations(
                id TEXT PRIMARY KEY,role_id TEXT NOT NULL,task_id TEXT NOT NULL,
                request_key TEXT NOT NULL UNIQUE,fingerprint TEXT NOT NULL,body TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS coding_publication_credentials(
                id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL,
                configured INTEGER NOT NULL,cleanup_pending INTEGER NOT NULL)''')
            db.execute('INSERT OR IGNORE INTO coding_publication_credentials VALUES(1,1,0,0)')

    def settings(self):
        with self.ledger._lock:
            db = self.ledger._db
            row = db.execute('SELECT revision,configured,cleanup_pending FROM coding_publication_credentials WHERE id=1').fetchone()
        return {'revision': row[0], 'configured': bool(row[1]), 'cleanupPending': bool(row[2])}

    def set_token(self, token):
        if self.credentials is None or not isinstance(token, str) or (token and (len(token) > 4096
                or not token.isascii() or any(char.isspace() for char in token) or '${' in token)):
            raise ValueError('dedicated publication credential unavailable or invalid')
        with self.application._owner_lock:
            with self.ledger.transaction() as db:
                db.execute('UPDATE coding_publication_credentials SET revision=revision+1,configured=0,cleanup_pending=1 WHERE id=1')
            errors = []
            for operation in self._records():
                if operation['action'] == 'publish' and (operation['status'] in {'accepted', 'running'}
                        or operation['cleanupState'] not in {'complete', 'not_started'}):
                    try:
                        self.cancel(operation['roleId'], operation['id'])
                    except Exception as error:
                        errors.append(error)
            if errors:
                raise errors[0] if len(errors) == 1 else ExceptionGroup('old broker credential resources remain pending', errors)
            self.credentials.set('token', token)
            with self.ledger.transaction() as db:
                db.execute('UPDATE coding_publication_credentials SET configured=?,cleanup_pending=0 WHERE id=1', (int(bool(token)),))
        return self.settings()

    def _native_record(self, role_id, task_id):
        with self.ledger._lock:
            db = self.ledger._db
            row = db.execute('SELECT current_attempt FROM coding_tasks WHERE id=? AND role_id=?', (task_id, role_id)).fetchone()
            if row is None:
                raise KeyError('Coding task does not belong to this role')
            return self.ledger.attempt(row[0])

    def _record(self, identifier, role_id=None):
        with self.ledger._lock:
            db = self.ledger._db
            row = db.execute('SELECT role_id,body FROM coding_owner_operations WHERE id=?', (identifier,)).fetchone()
            if row is None or role_id is not None and row['role_id'] != role_id:
                raise KeyError('Coding operation does not belong to this role')
            return json.loads(row['body'])

    def _update(self, identifier, **changes):
        with self.ledger.transaction() as db:
            row = db.execute('SELECT body FROM coding_owner_operations WHERE id=?', (identifier,)).fetchone()
            record = json.loads(row[0]) | changes
            db.execute('UPDATE coding_owner_operations SET body=? WHERE id=?', (_json(record), identifier))
        return record

    def _copy_runtime_result(self, identifier, snapshot):
        with self.ledger.transaction() as db:
            record = json.loads(db.execute('SELECT body FROM coding_owner_operations WHERE id=?', (identifier,)).fetchone()[0])
            if not record.get('runtimeResult'):
                status = record['status'] if record['status'] not in {'accepted', 'running'} else 'unknown'
                record.update(status=status, runtimeStatus=snapshot.status,
                    runtimeResult={'status': snapshot.status, 'exitCode': snapshot.exit_code, 'output': snapshot.output[:8000]},
                    runtimeAckDirty=True, endedAt=snapshot.ended_at)
                db.execute('UPDATE coding_owner_operations SET body=? WHERE id=?', (_json(record), identifier))
            return record

    def inspect(self, role_id, identifier):
        return operation_public(self._record(identifier, role_id))

    def list(self, role_id):
        return [operation_public(record) for record in self._records(role_id)]

    def task_summary(self, record):
        from .evidence import remaining_attempt_budget
        budget = False
        try:
            remaining_attempt_budget(record, business=True)
            budget = True
        except (ValueError, TimeoutError):
            pass
        allowed = self.application._allowed(record)
        return {'commitAllowed': bool(allowed and budget and record['repository'].get('localCommit')),
            'publicationAllowed': bool(allowed and record['repository'].get('push') and record['repository'].get('draftPr')),
            'hasEvidence': isinstance(record.get('validationEvidence'), dict),
            'businessDeadlineAt': record.get('businessDeadlineAt'), 'source': record['origin']['session_key']}

    def _records(self, role_id=None):
        with self.ledger._lock:
            db = self.ledger._db
            rows = db.execute('SELECT body FROM coding_owner_operations' + (' WHERE role_id=?' if role_id else ''),
                              (role_id,) if role_id else ()).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _allowed(self, operation):
        try:
            current = self._native_record(operation['roleId'], operation['taskId'])
            if (self._closed or operation.get('sealed') or current['attemptId'] != operation['attemptId']
                    or not self.application._allowed(current)):
                return False
            if operation['action'] == 'publish':
                setting = self.settings()
                if (not setting['configured'] or setting['cleanupPending']
                        or setting['revision'] != operation['candidate']['credentialRevision']):
                    return False
            return True
        except (KeyError, PermissionError, ValueError, sqlite3.Error):
            return False

    def _delivery_allowed(self, operation):
        # Continuing changes the current attempt, not the factual identity of
        # an earlier operation's result or its already sent receipt.
        return self.application._allowed(operation['candidate']['record'])

    def _current(self, role_id, task_id):
        record = self._native_record(role_id, task_id)
        if not self.application._allowed(record) or record.get('executionSealed'):
            raise PermissionError('original Coding authority was revoked')
        if record['cleanupState'] != 'complete' or record.get('resourceJobs') or record['runtimeStatus'] not in TERMINAL:
            raise ValueError('native resources or result handover have not completed')
        return record

    def _storage(self, record, deadline):
        from .artifact_budget import check
        from .evidence import task_paths
        check(self.root, self._root_identity, task_paths(self.application.root, record)[0], deadline)

    def _git(self, identifier, deadline, check=lambda: True, journal=lambda *_: None, record=None):
        last_storage_check = 0.0
        def remaining():
            nonlocal last_storage_check
            if self._closed or not check():
                raise PermissionError('Coding operation was cancelled or revoked')
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError('Coding operation business deadline ended')
            if record is not None and time.monotonic() - last_storage_check >= .5:
                self._storage(record, deadline)
                last_storage_check = time.monotonic()
            return value
        return OwnedGit(self.root, identifier, journal, remaining,
            (lambda: self._storage(record, deadline)) if record is not None else lambda: None)

    def prepare(self, role_id, task_id, *, action, message='', author_name='', author_email='', title='', body='', previous_operation_id=None):
        from .evidence import remaining_attempt_budget, task_paths, verify_attestation
        if action not in {'commit', 'publish'}:
            raise ValueError('unknown reviewed operation')
        with self.application._owner_lock:
            record = self._current(role_id, task_id)
            self._storage(record, time.monotonic() + 30)
            self._ensure_available(task_id, resolving=previous_operation_id)
            identifier = 'review_' + uuid.uuid4().hex
            task_root, worktree, _ = task_paths(self.application.root, record)
            candidate = {'id': identifier, 'roleId': role_id, 'taskId': task_id,
                'attemptId': record['attemptId'], 'action': action, 'nativeSession': record['nativeSession'],
                'branch': record['branch'], 'baseHead': record['baseHead'], 'record': record,
                'startHead': record.get('startHead'), 'source': record['origin'],
                'createdAt': time.time(),
                'attestedAt': record.get('validationEvidence', {}).get('attestedAt'),
                'validation': record.get('validationEvidence', {}).get('validation', [])}
            if action == 'commit':
                if record['status'] != 'validated_diff' or not record['repository'].get('localCommit'):
                    raise PermissionError('this attempt has no local commit permission or valid diff')
                remaining = remaining_attempt_budget(record, business=True)
                evidence = verify_attestation(worktree, record, time.monotonic() + remaining)
                if (not isinstance(message, str) or not 1 <= len(message) <= 8000 or '\x00' in message
                        or not re.fullmatch(r'[^<>\r\n\x00]{1,100}', author_name)
                        or not re.fullmatch(r'[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+', author_email)):
                    raise ValueError('review a concrete bounded commit message and identity')
                tree, diff = self._review_tree(identifier, worktree, record, evidence,
                    time.monotonic() + remaining_attempt_budget(record, business=True))
                timestamp = int(time.time())
                message = message.replace('\r\n', '\n').replace('\r', '\n').rstrip('\n') + '\n'
                payload = (f'tree {tree}\nparent {evidence["snapshot"]["head"]}\n'
                    f'author {author_name} <{author_email}> {timestamp} +0000\n'
                    f'committer {author_name} <{author_email}> {timestamp} +0000\n\n' + message).encode('utf-8')
                owner = self._git(identifier, time.monotonic() + remaining_attempt_budget(record, business=True), record=record)
                try:
                    sha = owner.run(worktree, 'hash-object', '-t', 'commit', '--stdin', input=payload)
                finally:
                    owner.close()
                candidate.update(head=evidence['snapshot']['head'], tree=tree, diff=diff,
                    commitSha=sha, commitPayload=payload.decode('utf-8'), message=message,
                    authorName=author_name, authorEmail=author_email)
            else:
                candidate.update(self._prepare_publication(record, title, body))
                candidate['pushRequired'] = True
                if previous_operation_id is not None:
                    previous = self._record(previous_operation_id, role_id)
                    if (previous['taskId'] != task_id or previous['attemptId'] != record['attemptId']
                            or previous['status'] != 'push_confirmed' or not previous.get('pushConfirmed')
                            or previous.get('prDispatched') or previous.get('resolvedBy')
                            or previous['candidate']['credentialRevision'] != candidate['credentialRevision']
                            or previous['candidate']['commitSha'] != candidate['commitSha']
                            or previous['candidate']['remote'] != candidate['remote']):
                        raise ValueError('remaining action requires a readback-confirmed original push without PR dispatch')
                    candidate.update(previousOperationId=previous_operation_id, pushRequired=False,
                        publishBranch=previous['candidate']['publishBranch'],
                        platformRepositoryId=previous['platformRepositoryId'])
            candidate['fingerprint'] = fingerprint(candidate)
            with self.ledger.transaction() as db:
                if db.execute('SELECT COUNT(*) FROM coding_reviews WHERE role_id=?', (role_id,)).fetchone()[0] >= 100:
                    raise ValueError('Coding review retention budget is full')
                db.execute('INSERT INTO coding_reviews VALUES(?,?,?,?,NULL,?)',
                    (identifier, role_id, task_id, candidate['fingerprint'], _json(candidate)))
            return self._public_review(candidate)

    @staticmethod
    def _public_review(candidate):
        return {key: value for key, value in candidate.items() if key not in {'record', 'commitPayload'}}

    def _review_tree(self, identifier, worktree, record, evidence, deadline):
        from .evidence import verify_attestation
        directory = self.root / identifier
        directory.mkdir(exist_ok=False)
        env = git_environment() | {'GIT_INDEX_FILE': str(directory / 'index')}
        owner = self._git(identifier, deadline, record=record)
        try:
            owner.run(worktree, 'read-tree', '--empty', env=env)
            files = evidence['snapshot']['files']
            present = [(name, item) for name, item in files.items() if item.get('status') != 'deleted']
            if present:
                paths = '\n'.join(json.dumps(str(Path(worktree) / name), ensure_ascii=False) for name, _ in present) + '\n'
                blobs = owner.run(worktree, 'hash-object', '-w', '--no-filters', '--stdin-paths', input=paths.encode('utf-8')).splitlines()
                if len(blobs) != len(present) or any(blob != item['gitBlob'] for blob, (_, item) in zip(blobs, present)):
                    raise ValueError('reviewed files changed during object capture')
                entries = ''.join(f'{item["mode"]} {blob}\t{name}\0' for blob, (name, item) in zip(blobs, present))
                owner.run(worktree, 'update-index', '-z', '--index-info', input=entries.encode('utf-8'), env=env)
            tree = owner.run(worktree, 'write-tree', env=env)
            if tree != evidence['snapshot']['tree']:
                raise ValueError('reviewed tree differs from attested files')
            diff = owner.run(worktree, 'diff', '--no-ext-diff', '--no-textconv', '--binary',
                evidence['snapshot']['head'], tree, '--', limit=256 * 1024)
            verify_attestation(worktree, record, deadline)
            return tree, diff
        finally:
            owner.close()
            # This host-created directory contains only Git's disposable
            # private index. Never recursively follow an altered artifact path.
            from .artifact_budget import identity
            if identity(self.root) != self._root_identity:
                raise CleanupFailed('private review index root changed')
            identity(directory)
            for name in ('index', 'index.lock'):
                path = directory / name
                if path.exists():
                    if path.is_symlink() or path.stat().st_nlink != 1:
                        raise CleanupFailed('private review index ownership changed')
                    path.unlink()
            directory.rmdir()

    def _ensure_available(self, task_id, resolving=None):
        for record in self._records():
            if (record['id'] == resolving and record['status'] == 'push_confirmed'
                    and record['cleanupState'] == 'complete' and not record.get('resolvedBy')):
                continue
            if record['taskId'] == task_id and (record['status'] in {'accepted', 'running'}
                    or self._cleanup_pending(record)
                    or record['status'] in {'unknown', 'push_confirmed'}):
                raise ValueError('previous Coding operation is active or has unresolved effects')

    def _operation_admission(self, record, resolving=None):
        self._ensure_available(record['id'], resolving=resolving)
        with self.ledger._lock:
            native = self.ledger._db.execute("""SELECT t.role_id,t.repo_id FROM coding_tasks t
                JOIN coding_attempts a ON a.task_id=t.id
                WHERE a.status IN ('accepted','running') OR a.cleanup NOT IN ('complete','not_started')""").fetchall()
        active = [item for item in self._records() if item['status'] in {'accepted', 'running'}
            or self._cleanup_pending(item)]
        if len(native) + len(active) >= 2 or any(item['role_id'] == record['origin']['role_id']
                or item['repo_id'] == record['repository']['id'] for item in native) or any(
                item['roleId'] == record['origin']['role_id']
                or item['candidate']['record']['repository']['id'] == record['repository']['id'] for item in active):
            raise ValueError('Coding role, repository or global operation capacity is occupied')

    def ensure_native_admission(self, role_id, repo_id, task_id=None):
        active = [record for record in self._records() if record['status'] in {'accepted', 'running'}
            or self._cleanup_pending(record)]
        if len(active) >= 2 or any(record['roleId'] == role_id
                or record['candidate']['record']['repository']['id'] == repo_id for record in active):
            raise ValueError('Coding owner-operation concurrency budget is occupied')
        if task_id is not None:
            self._ensure_available(task_id)

    def approve(self, role_id, review_id, *, request_id):
        from .evidence import remaining_attempt_budget
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
            raise ValueError('owner operation request identity is required')
        with self.application._owner_lock:
            with self.ledger.transaction() as db:
                row = db.execute('SELECT role_id,body,consumed_by FROM coding_reviews WHERE id=?', (review_id,)).fetchone()
                if row is None or row['role_id'] != role_id:
                    raise KeyError('review does not belong to this role')
                candidate = json.loads(row['body'])
                request_key = fingerprint([role_id, request_id])
                previous = db.execute('SELECT body,fingerprint FROM coding_owner_operations WHERE request_key=?', (request_key,)).fetchone()
                if previous:
                    if previous['fingerprint'] != candidate['fingerprint']:
                        raise ValueError('owner request conflicts with its original review')
                    return operation_public(json.loads(previous['body']))
                if row['consumed_by']:
                    raise ValueError('this exact approval has already been consumed')
            record = self._current(role_id, candidate['taskId'])
            if record['attemptId'] != candidate['attemptId']:
                raise ValueError('the reviewed attempt is no longer current')
            self._operation_admission(record, resolving=candidate.get('previousOperationId'))
            timeout = min(600, remaining_attempt_budget(record, business=False)) if candidate['action'] == 'commit' else 600
            if candidate['action'] == 'commit':
                remaining_attempt_budget(record, business=True)
            identifier = 'codingop_' + uuid.uuid4().hex
            operation = {'id': identifier, 'roleId': role_id, 'taskId': record['id'], 'attemptId': record['attemptId'], 'reviewId': review_id,
                'action': candidate['action'], 'candidate': candidate, 'origin': record['origin'],
                'status': 'accepted', 'runtimeStatus': 'queued', 'runtimeTaskId': None, 'runtimeAckDirty': False,
                'cleanupState': 'not_started', 'deliveryStatus': 'pending', 'messageSynced': False,
                'createdAt': time.time(), 'resourceJobs': [], 'blockers': [], 'sealed': False}
            if candidate.get('previousOperationId'):
                operation.update(pushConfirmed=True, platformRepositoryId=candidate['platformRepositoryId'],
                    commitSha=candidate['commitSha'], priorPushId=candidate['previousOperationId'])
            if not self._allowed(operation):
                raise PermissionError('reviewed authority is unavailable')
            with self.ledger.transaction() as db:
                changed = db.execute('UPDATE coding_reviews SET consumed_by=? WHERE id=? AND consumed_by IS NULL', (identifier, review_id)).rowcount
                if changed != 1:
                    raise ValueError('this exact approval has already been consumed')
                db.execute('INSERT INTO coding_owner_operations VALUES(?,?,?,?,?,?)',
                    (identifier, role_id, record['id'], request_key, candidate['fingerprint'], _json(operation)))
                if candidate.get('previousOperationId'):
                    previous = self._record(candidate['previousOperationId'], role_id)
                    if previous['status'] != 'push_confirmed' or previous.get('resolvedBy') or previous.get('prDispatched'):
                        raise ValueError('remaining action was already consumed or changed')
                    previous.update(status='superseded', resolvedBy=identifier, messageSynced=False)
                    db.execute('UPDATE coding_owner_operations SET body=? WHERE id=?', (_json(previous), previous['id']))
            executor = PublicationExecutor(self, operation)
            self._owners[identifier] = executor
            kind = self._kind(operation)
            self._update(identifier, launchClaimed=True)
            try:
                runtime = self.application.tasks.submit(TaskOrigin(**operation['origin']), executor, kind=kind,
                    description=identifier, submission_key=identifier, retain_until_ack=True, completion=False,
                    budget=TaskBudget(timeout_seconds=timeout, log_bytes=1024 * 1024, output_chars=8000),
                    authorized=lambda: self._allowed(self._record(identifier)))
                self._update(identifier, runtimeTaskId=runtime)
            except Exception:
                runtime = self.application.tasks.find_submission(TaskOrigin(**operation['origin']), kind=kind, submission_key=identifier)
                if runtime:
                    self._update(identifier, runtimeTaskId=runtime)
                else:
                    self._update(identifier, status='failed', runtimeStatus='interrupted', cleanupState='complete',
                        blockers=['runtime_handover_unavailable'], endedAt=time.time())
                    self._owners.pop(identifier, None)
                    raise
            return operation_public(self._record(identifier))

    @staticmethod
    def _kind(record):
        return 'coding_commit' if record['action'] == 'commit' else 'coding_publish'

    def resource_job(self, identifier, name, active):
        with self.ledger.transaction() as db:
            row = db.execute('SELECT body FROM coding_owner_operations WHERE id=?', (identifier,)).fetchone()
            record = json.loads(row[0])
            jobs = set(record.get('resourceJobs', []))
            jobs.add(name) if active else jobs.discard(name)
            record['resourceJobs'] = sorted(jobs)
            db.execute('UPDATE coding_owner_operations SET body=? WHERE id=?', (_json(record), identifier))

    def cancel(self, role_id, identifier):
        with self.application._owner_lock:
            record = self._record(identifier, role_id)
            self._update(identifier, sealed=True)
            owner = self._owners.get(identifier)
            if owner is not None:
                owner.cancel()
            runtime = record.get('runtimeTaskId') or self.application.tasks.find_submission(
                TaskOrigin(**record['origin']), kind=self._kind(record), submission_key=identifier)
            if runtime:
                self.application.tasks.stop(runtime, TaskOrigin(**record['origin']))
            return self.inspect(role_id, identifier)

    async def tick(self):
        # Unknown tombstones keep their facts but cannot starve newer work.
        with self.ledger._lock:
            condition = "(json_extract(body,'$.status') IN ('accepted','running') OR json_extract(body,'$.cleanupState') NOT IN ('complete','not_started') OR json_extract(body,'$.runtimeAckDirty')=1 OR json_extract(body,'$.messageSynced')=0 OR (json_extract(body,'$.deliveryDispatched')=1 AND json_extract(body,'$.deliveryStatus')!='sent'))"
            rows = self.ledger._db.execute('SELECT id,body FROM coding_owner_operations WHERE ' + condition
                + ' AND id>? ORDER BY id LIMIT 100', (self._scan_cursor,)).fetchall()
            if not rows and self._scan_cursor:
                self._scan_cursor = ''
                rows = self.ledger._db.execute('SELECT id,body FROM coding_owner_operations WHERE ' + condition
                    + ' ORDER BY id LIMIT 100').fetchall()
            if rows:
                self._scan_cursor = rows[-1]['id']
        for row in rows:
            try:
                await self._advance(json.loads(row['body']))
            except (CleanupFailed, sqlite3.Error, PermissionError, OSError, KeyError, ValueError):
                continue

    async def _advance(self, record):
        identifier = record['id']
        runtime = record.get('runtimeTaskId')
        origin = TaskOrigin(**record['origin'])
        if not runtime:
            runtime = await asyncio.to_thread(self.application.tasks.find_submission, origin,
                kind=self._kind(record), submission_key=identifier)
            if runtime:
                record = self._update(identifier, runtimeTaskId=runtime)
            elif record['status'] == 'accepted':
                record = self._update(identifier, status='interrupted', runtimeStatus='interrupted', cleanupState='complete')
        if runtime:
            if not record.get('runtimeResult'):
                snapshot = await asyncio.to_thread(self.application.tasks.query, runtime, origin)
                if snapshot.status not in TERMINAL:
                    return
                record = self._copy_runtime_result(identifier, snapshot)
            if record['runtimeAckDirty']:
                await asyncio.to_thread(self.application.tasks.acknowledge_result, runtime, origin)
                record = self._update(identifier, runtimeAckDirty=False)
        if record['cleanupState'] in {'running', 'cleanup_failed'}:
            owner = self._owners.get(identifier)
            if owner is not None and (record['cleanupState'] == 'cleanup_failed' or record['runtimeStatus'] in TERMINAL):
                await asyncio.to_thread(owner.cancel)
                record = self._update(identifier, cleanupState='complete')
            elif owner is None and record.get('resourceJobs') is not None:
                active = [await asyncio.to_thread(named_job_active, name) for name in record['resourceJobs']]
                if not any(active):
                    record = self._update(identifier, cleanupState='complete', resourceJobs=[])
        if record['cleanupState'] == 'complete':
            # A readback whose cleanup write failed can still own a live HTTP
            # execution. Never drop an in-memory owner on a stale SQL state.
            owner = self._owners.get(identifier)
            if owner is not None:
                await asyncio.to_thread(owner.cancel)
            self._owners.pop(identifier, None)
        if record['status'] in {'accepted', 'running'}:
            return
        kind = 'coding_commit' if record['action'] == 'commit' else 'coding_publication'
        if origin.session_key.startswith('thread:') and self.application.receipt:
            receipt = self.application.receipt(record | {'completionKind': kind, 'completionId': identifier})
            if receipt is not None and (receipt['status'] != record['deliveryStatus']
                    or receipt.get('attemptId') != record.get('deliveryAttemptId')):
                record = self._update(identifier, deliveryStatus=receipt['status'],
                    deliveryAttemptId=receipt.get('attemptId'), messageSynced=False)
        if self.application._closed:
            # Shutdown closes execution admission, not the durable source's
            # future delivery permission. A later host may finish this outbox.
            return
        if not self._delivery_allowed(record):
            if record['deliveryStatus'] != 'sent':
                self._update(identifier, deliveryStatus='suppressed', messageSynced=True)
            return
        if record.get('messageSynced'):
            return
        async def complete():
            if self.application._closed:
                return
            if not self._delivery_allowed(record):
                if record['deliveryStatus'] != 'sent':
                    self._update(identifier, deliveryStatus='suppressed', messageSynced=True)
                return
            content = record.get('completionContent') or ('编程操作 ' + identifier + '：' + record['status']
                + ('\n提交：' + record['commitSha'] if record.get('commitSha') else '')
                + ('\nDraft PR：' + record['prUrl'] if record.get('prUrl') else ''))
            current = self._update(identifier, completionContent=content)
            if origin.session_key.startswith('thread:'):
                delivery_record = current | {'completionKind': kind, 'completionId': identifier}
                receipt = self.application.receipt(delivery_record) if self.application.receipt else None
                if receipt is None and not current.get('deliveryDispatched') and self.application.deliver:
                    self._update(identifier, deliveryDispatched=True, deliveryStatus='sending')
                    try:
                        receipt = await self.application.deliver(delivery_record, content, lambda: self._delivery_allowed(self._record(identifier)))
                    except Exception:
                        receipt = None
                current = self._update(identifier, deliveryStatus=receipt['status'] if receipt else 'uncertain',
                    deliveryAttemptId=receipt.get('attemptId') if receipt else None)
            else:
                current = self._update(identifier, deliveryStatus='sent')
            with self.ledger.transaction() as db:
                self.application.sessions.append_coding_operation_once(origin.role_id, origin.session_key,
                    current['taskId'], current['attemptId'], identifier, kind, content,
                    connection=db, authorized=lambda: self._delivery_allowed(self._record(identifier)),
                    operation_status=current['status'], delivery_status=current['deliveryStatus'],
                    delivery_attempt_id=current.get('deliveryAttemptId'))
                current['messageSynced'] = True
                db.execute('UPDATE coding_owner_operations SET body=? WHERE id=?', (_json(current), identifier))
        if self.application.lock_for:
            async with self.application.lock_for(origin.role_id):
                await complete()
        else:
            await complete()

    async def invalidate_role(self, role_id):
        for record in self._records(role_id):
            self._update(record['id'], sealed=True)
        for kind in ('coding_commit', 'coding_publish'):
            await asyncio.to_thread(self.application.tasks.cancel_role_tasks, role_id, kind=kind)
        await self.tick()

    async def invalidate_delegation(self, role_id):
        for record in self._records(role_id):
            if record['candidate']['record']['executor'] == 'codex_exec':
                self._update(record['id'], sealed=True)
                await asyncio.to_thread(self.cancel, role_id, record['id'])
        await self.tick()

    def role_resources_pending(self, role_id):
        return any(self._cleanup_pending(record) for record in self._records(role_id))

    def _cleanup_pending(self, record):
        return record['cleanupState'] not in {'complete', 'not_started'} or record['id'] in self._owners

    def _approved_publication(self, operation):
        """Read the immutable, consumed owner approval, independent of Runtime."""
        candidate = operation['candidate']
        with self.ledger._lock:
            row = self.ledger._db.execute('''SELECT r.body,r.fingerprint,r.consumed_by,r.role_id,r.task_id,
                o.role_id AS operation_role,o.task_id AS operation_task,o.fingerprint AS operation_fingerprint
                FROM coding_reviews r JOIN coding_owner_operations o ON o.id=? WHERE r.id=?''',
                (operation['id'], operation['reviewId'])).fetchone()
        payload = dict(candidate)
        digest = payload.pop('fingerprint', None)
        if (row is None or candidate['id'] != operation['reviewId'] or candidate['action'] != 'publish'
                or row['role_id'] != operation['roleId'] or row['operation_role'] != operation['roleId']
                or row['task_id'] != operation['taskId'] or row['operation_task'] != operation['taskId']
                or row['consumed_by'] != operation['id'] or row['fingerprint'] != digest
                or row['operation_fingerprint'] != digest or fingerprint(payload) != digest
                or json.loads(row['body']) != candidate):
            raise PermissionError('original publication approval is unavailable for readback')
        return candidate

    def _readback_push(self, operation, current, check):
        """A remaining-only approval inherits one verified push, never a POST."""
        target = operation['candidate']
        if target.get('pushRequired', True) is not False:
            if not operation.get('pushDispatched'):
                raise PermissionError('original push dispatch is unavailable for readback')
            return
        publication_keys = ('roleId', 'taskId', 'attemptId', 'nativeSession', 'branch', 'baseHead', 'startHead',
            'source', 'head', 'tree', 'commitSha', 'remote', 'baseRef', 'publishBranch', 'credentialRevision')
        record_keys = ('id', 'attemptId', 'executor', 'nativeSession', 'branch', 'baseHead', 'startHead',
            'expectedHead', 'origin', 'repository', 'profile', 'nativeBuild', 'authorityGeneration', 'delegationGeneration')
        seen: set[str] = set()
        value = operation
        try:
            with self.ledger._lock:
                # The role retains at most 100 reviews; reject cycles or an
                # unavailable chain rather than following unbounded history.
                while len(seen) < 100:
                    check()
                    if value['id'] in seen:
                        break
                    seen.add(value['id'])
                    candidate = self._approved_publication(value)
                    frozen = candidate['record']
                    if (value['action'] != 'publish' or value['roleId'] != target['roleId']
                            or value['taskId'] != target['taskId'] or value['attemptId'] != target['attemptId']
                            or value['origin'] != target['source'] or candidate['source'] != current['origin']
                            or frozen['origin'] != current['origin']
                            or any(candidate.get(key) != target.get(key) for key in publication_keys)
                            or any(frozen.get(key) != current.get(key) for key in record_keys)
                            or current.get('expectedHead') != target['commitSha']
                            or current.get('hostCommit', {}).get('sha') != target['commitSha']
                            or current['hostCommit'].get('tree') != target['tree']
                            or self._closed or not self.application._allowed(frozen)
                            or value['cleanupState'] != 'complete' or value.get('pushConfirmed') is not True
                            or type(value.get('platformRepositoryId')) is not int
                            or value['platformRepositoryId'] != operation['platformRepositoryId']):
                        break
                    if candidate.get('pushRequired') is True:
                        if (value.get('pushDispatched') is True and value.get('pushAcknowledged') is True
                                and not value.get('prDispatched') and not value.get('priorPushId')):
                            return
                        break
                    prior_id = value.get('priorPushId')
                    if (candidate.get('pushRequired') is not False or value.get('pushDispatched')
                            or not prior_id or candidate.get('previousOperationId') != prior_id
                            or candidate.get('platformRepositoryId') != operation['platformRepositoryId']):
                        break
                    prior = self._record(prior_id, operation['roleId'])
                    if (prior['status'] != 'superseded' or prior.get('resolvedBy') != value['id']
                            or prior.get('prDispatched')):
                        break
                    value = prior
        except (KeyError, TypeError, ValueError):
            raise PermissionError('original confirmed push chain is unavailable for readback') from None
        raise PermissionError('original confirmed push chain is unavailable for readback')

    def observe(self, role_id, identifier):
        """Explicit owner readback. This method never launches Git writes or POST."""
        from .evidence import snapshot_worktree, task_paths
        from .github_publication import GithubPlatform
        # The explicit readback gets one budget including admission, identity
        # checks and cold client preparation, rather than a fresh network clock.
        deadline = time.monotonic() + 30
        if not self.application._owner_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise TimeoutError('bounded Coding readback admission ended')
        try:
            operation = self._record(identifier, role_id)
            current = self._native_record(role_id, operation['taskId'])
            if current['attemptId'] != operation['attemptId'] or not self.application._allowed(current):
                raise PermissionError('original Coding readback authority was revoked')
            def remaining():
                if not self.application._allowed(current):
                    raise PermissionError('bounded Coding readback authority ended')
                value = deadline - time.monotonic()
                if value <= 0:
                    raise TimeoutError('bounded Coding readback deadline ended')
                return value
            remaining()
            if operation['cleanupState'] != 'complete':
                raise ValueError('owned resources must be confirmed stopped before readback')
            candidate = operation['candidate']
            _, worktree, _ = task_paths(self.application.root, current)
            if operation['action'] == 'commit':
                after = snapshot_worktree(worktree, current['repository']['commonDir'], current['branch'], deadline)
                remaining()
                if (not operation.get('commitDispatched') or after['head'] != candidate['commitSha']
                        or after['headTree'] != candidate['tree'] or after['tree'] != candidate['tree'] or after['status']):
                    return self.inspect(role_id, identifier)
                self._attest_commit(current, operation, after)
                self._update(identifier, status='validated_commit', commitSha=candidate['commitSha'], observedAt=time.time())
                return self.inspect(role_id, identifier)
            setting = self.settings()
            if (not setting['configured'] or setting['cleanupPending'] or setting['revision'] != candidate['credentialRevision']
                    or operation.get('platformRepositoryId') is None):
                raise PermissionError('original broker identity is unavailable for readback')
            self._readback_push(operation, current, remaining)
            token = self.credentials.get('token') if self.credentials else ''
            if not token:
                raise PermissionError('original broker credential is unavailable')
            remaining()
            platform = (self.platform_factory or GithubPlatform)(token, candidate['remote'])
            platform.check = remaining
            try:
                remaining()
                repository = platform.repository()
                if repository['id'] != operation['platformRepositoryId']:
                    raise ValueError('original platform repository was replaced')
                if platform.reference(candidate['publishBranch']) != candidate['commitSha']:
                    return self.inspect(role_id, identifier)
                self._update(identifier, branchObserved=True, observedAt=time.time())
                if operation.get('pushAcknowledged') and not operation.get('pushConfirmed'):
                    operation = self._update(identifier, pushConfirmed=True)
                if not operation.get('prDispatched'):
                    if operation.get('pushConfirmed') and not platform.pulls(candidate['publishBranch'], candidate['baseRef']):
                        self._update(identifier, status='push_confirmed', observedAt=time.time(), messageSynced=False)
                    return self.inspect(role_id, identifier)
                pulls = platform.pulls(candidate['publishBranch'], candidate['baseRef'])
                if len(pulls) != 1:
                    return self.inspect(role_id, identifier)
                proof = platform.verify(platform.pull(pulls[0].get('number')), candidate, repository)
                self._update(identifier, status='delivered_draft', prUrl=proof['url'], platformProof=proof,
                    observedAt=time.time(), messageSynced=False)
                return self.inspect(role_id, identifier)
            finally:
                try:
                    platform.close()
                except BaseException:
                    # Reuse the existing operation cleanup owner and its
                    # cancel/tick/close paths; no new Runtime or HTTP retry.
                    owner = PublicationExecutor(self, operation)
                    owner.platform = platform
                    self._owners[identifier] = owner
                    self._update(identifier, cleanupState='cleanup_failed')
                    raise
        finally:
            self.application._owner_lock.release()

    def _attest_commit(self, current, operation, after):
        candidate = operation['candidate']
        with self.ledger.transaction() as db:
            task = db.execute('SELECT body,current_attempt FROM coding_tasks WHERE id=?', (current['id'],)).fetchone()
            if task['current_attempt'] != current['attemptId'] or not self.application._allowed(current):
                raise PermissionError('current Coding attempt or authority changed')
            body = json.loads(task['body']) | {'expectedHead': candidate['commitSha']}
            db.execute('UPDATE coding_tasks SET body=? WHERE id=?', (_json(body), current['id']))
            attempt = db.execute('SELECT body FROM coding_attempts WHERE id=?', (current['attemptId'],)).fetchone()
            value = json.loads(attempt[0]) | {'status': 'validated_commit', 'expectedHead': candidate['commitSha'],
                'hostCommit': {'sha': candidate['commitSha'], 'tree': candidate['tree'], 'parent': candidate['head'],
                    'snapshot': after, 'operationId': operation['id']}}
            db.execute('UPDATE coding_attempts SET status=?,body=? WHERE id=?', ('validated_commit', _json(value), current['attemptId']))

    async def close(self):
        self._closed = True
        errors = []
        for role_id in {record['roleId'] for record in self._records()}:
            for kind in ('coding_commit', 'coding_publish'):
                try:
                    await asyncio.to_thread(self.application.tasks.cancel_role_tasks, role_id, kind=kind)
                except Exception as error:
                    errors.append(error)
        try:
            await self.tick()
        except Exception as error:
            errors.append(error)
        for identifier, owner in list(self._owners.items()):
            try:
                await asyncio.to_thread(owner.cancel)
                self._update(identifier, cleanupState='complete')
                self._owners.pop(identifier, None)
            except Exception as error:
                errors.append(error)
        if any(record['cleanupState'] not in {'complete', 'not_started'} for record in self._records()):
            errors.append(CleanupFailed('Coding owner-operation resources remain unconfirmed'))
        if errors:
            raise errors[0] if len(errors) == 1 else ExceptionGroup('Coding owner-operation cleanup failed', errors)

    def _prepare_publication(self, record, title, body):
        from .evidence import snapshot_worktree, task_paths
        if (record['status'] != 'validated_commit' or not record.get('hostCommit')
                or not record['repository'].get('push') or not record['repository'].get('draftPr')):
            raise PermissionError('a host-attested commit and separate push/Draft PR grants are required')
        if (not isinstance(title, str) or not 1 <= len(title) <= 200 or any(char in title for char in '\r\n\x00')
                or not isinstance(body, str) or len(body.encode('utf-8')) > 32768 or '\x00' in body):
            raise ValueError('review a bounded concrete Draft PR title and body')
        setting = self.settings()
        if not setting['configured'] or setting['cleanupPending']:
            raise PermissionError('dedicated broker credential is unavailable')
        remote = publication_remote(Path(record['repository']['root']), record['repository']['remote'])
        if remote != record['repository'].get('publicationRemote'):
            raise PermissionError('approved remote identity changed')
        _, worktree, _ = task_paths(self.application.root, record)
        snapshot = snapshot_worktree(worktree, record['repository']['commonDir'], record['branch'], time.monotonic() + 30)
        if snapshot != record['hostCommit']['snapshot'] or snapshot['status'] or snapshot['head'] != record['expectedHead']:
            raise ValueError('host-committed evidence changed')
        commit_operation = self._record(record['hostCommit']['operationId'])
        if (commit_operation['status'] != 'validated_commit' or commit_operation['cleanupState'] != 'complete'
                or commit_operation['runtimeStatus'] not in TERMINAL):
            raise ValueError('host commit resources or outcome are not confirmed')
        owner = self._git('review_' + uuid.uuid4().hex, time.monotonic() + 30)
        try:
            diff = owner.run(worktree, 'diff', '--no-ext-diff', '--no-textconv', '--binary', record['baseHead'], snapshot['head'], '--', limit=256 * 1024)
        finally:
            owner.close()
        return {'commitSha': snapshot['head'], 'head': snapshot['head'], 'tree': snapshot['headTree'],
            'remote': remote, 'baseRef': record['repository']['baseRef'],
            'publishBranch': 'codex/meido-pub-' + uuid.uuid4().hex,
            'credentialRevision': setting['revision'], 'title': title, 'diff': diff,
            'body': body.replace('\r\n', '\n').replace('\r', '\n')}


class PublicationExecutor:
    def __init__(self, service, record):
        self.service, self.record = service, record
        self._cancelled = threading.Event()
        self.git = None
        self.platform = None

    def cancel(self):
        self._cancelled.set()
        self._close()

    def _close(self):
        errors = []
        for owner in (self.git, self.platform):
            if owner is not None:
                try:
                    owner.close()
                except Exception as error:
                    errors.append(error)
        if errors:
            raise errors[0] if len(errors) == 1 else ExceptionGroup('Coding broker resource cleanup failed', errors)

    def run(self, execution):
        from .evidence import remaining_attempt_budget, snapshot_worktree, task_paths, verify_attestation
        operation = self.record
        candidate, identifier = operation['candidate'], operation['id']
        service = self.service
        business, blockers, cleanup = 'failed', [], 'running'
        dispatched = False
        def allowed():
            return not self._cancelled.is_set() and service._allowed(service._record(identifier))
        try:
            service._update(identifier, status='running', cleanupState='running')
            deadline = execution.deadline - 60
            if operation['action'] == 'commit':
                deadline = min(deadline, time.monotonic() + remaining_attempt_budget(candidate['record'], business=True))
            self.git = service._git(identifier, deadline, allowed,
                lambda name, active: service.resource_job(identifier, name, active), record=candidate['record'])
            current = service._current(operation['roleId'], operation['taskId'])
            _, worktree, _ = task_paths(service.application.root, current)
            if operation['action'] == 'publish':
                self._publish(current, worktree)
                business = 'delivered_draft'
            else:
                evidence = verify_attestation(worktree, current, deadline)
                if evidence['snapshot']['head'] != candidate['head'] or evidence['snapshot']['tree'] != candidate['tree']:
                    raise ValueError('reviewed evidence changed')
                service._update(identifier, commitDispatched=True)
                dispatched = True
                commit = self.git.run(worktree, 'hash-object', '-w', '-t', 'commit', '--stdin',
                    input=candidate['commitPayload'].encode('utf-8'))
                if commit != candidate['commitSha']:
                    raise ValueError('approved commit object identity changed')
                self.git.run(worktree, 'update-ref', 'refs/heads/' + current['branch'], commit, candidate['head'])
                self.git.run(worktree, 'read-tree', commit)
                after = snapshot_worktree(worktree, current['repository']['commonDir'], current['branch'], deadline)
                if after['head'] != commit or after['headTree'] != candidate['tree'] or after['status']:
                    raise ValueError('committed tree or clean state differs from review')
                # Git's tree identity is independently tied to all reviewed content
                # and modes. No native executable is still alive at this point.
                if after['tree'] != candidate['tree'] or not allowed():
                    raise PermissionError('committed evidence or permission changed')
                service._attest_commit(current, operation, after)
                service._update(identifier, commitSha=commit)
                business = 'validated_commit'
        except BaseException as error:
            business = 'unknown' if (dispatched or any(service._record(identifier).get(key) for key in ('commitDispatched', 'pushDispatched', 'prDispatched'))) else 'cancelled' if self._cancelled.is_set() else 'failed'
            blockers = [getattr(error, 'code', type(error).__name__)]
        finally:
            try:
                self._close()
                cleanup = 'complete'
            except BaseException:
                cleanup, business, blockers = 'cleanup_failed', 'unknown', ['broker_cleanup_failed']
            service._update(identifier, status=business, cleanupState=cleanup, blockers=blockers, endedAt=time.time())
        return TaskOutcome('succeeded' if business in {'validated_commit', 'delivered_draft'} else 'failed',
            0 if business in {'validated_commit', 'delivered_draft'} else None,
            _json({'status': business, 'cleanupState': cleanup, 'blockers': blockers}))

    def _publish(self, current, worktree):
        from .github_publication import GithubPlatform, GitTransport
        from .evidence import snapshot_worktree
        service, identifier, candidate = self.service, self.record['id'], self.record['candidate']
        assert self.git is not None
        if (current['status'] != 'validated_commit' or current.get('hostCommit', {}).get('sha') != candidate['commitSha']
                or publication_remote(Path(current['repository']['root']), current['repository']['remote']) != candidate['remote']):
            raise ValueError('reviewed committed source or remote changed')
        before = snapshot_worktree(worktree, current['repository']['commonDir'], current['branch'], time.monotonic() + self.git.check())
        if before != current['hostCommit']['snapshot'] or before['status'] or before['head'] != candidate['commitSha']:
            raise ValueError('reviewed committed files changed')
        token = service.credentials.get('token') if service.credentials is not None else ''
        if not token or not token.isascii() or any(char.isspace() for char in token):
            raise PermissionError('dedicated broker token unavailable')
        self.git.check()
        self.platform = (service.platform_factory or GithubPlatform)(token, candidate['remote'])
        self.platform.check = self.git.check
        repository = self.platform.repository()
        service._update(identifier, platformRepositoryId=repository['id'])
        if self.platform.reference(candidate['baseRef']) != candidate['baseHead']:
            raise ValueError('approved remote base changed')
        if candidate.get('pushRequired', True):
            if self.platform.reference(candidate['publishBranch']) is not None:
                raise ValueError('approved publication branch already exists')
            transport = (service.transport_factory or GitTransport)(service.root / identifier,
                self.git, candidate, token)
            self.git.check()
            service._update(identifier, pushDispatched=True, commitSha=candidate['commitSha'])
            transport.push(worktree)
            service._update(identifier, pushAcknowledged=True)
            if self.platform.reference(candidate['publishBranch']) != candidate['commitSha']:
                raise ValueError('pushed exact SHA is not confirmed')
            service._update(identifier, pushConfirmed=True)
        elif (repository['id'] != candidate['platformRepositoryId']
                or self.platform.reference(candidate['publishBranch']) != candidate['commitSha']):
            raise ValueError('previously confirmed owned push changed')
        if self.platform.pulls(candidate['publishBranch'], candidate['baseRef']):
            raise ValueError('an existing PR prevents a new publication dispatch')
        if self.platform.reference(candidate['baseRef']) != candidate['baseHead']:
            raise ValueError('approved remote base changed before PR dispatch')
        self.git.check()
        service._update(identifier, prDispatched=True)
        pull = self.platform.create(candidate)
        proof = self.platform.verify(pull, candidate, repository)
        service._update(identifier, prUrl=proof['url'], platformProof=proof, observedAt=time.time())
