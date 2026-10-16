from __future__ import annotations

import asyncio
import copy
from pathlib import Path
import sqlite3
import threading
import time

from ..session_store import SessionStore
from ..task_runtime import TaskBudget, TaskOrigin, TaskRuntime
from ..task_runtime.runtime import TERMINAL, TaskExecutor
from .config import CodingConfigStore, CodingRoleConfig
from .ledger import CodingLedger, _json
from .probes import NativeProfileProbes
from .owned_process import CleanupFailed


def public_task(record: dict) -> dict:
    fields = {'id', 'attemptId', 'executor', 'status', 'runtimeStatus', 'nativeTurnStatus', 'cleanupState',
              'runtimeTaskId', 'nativeSession', 'summary', 'validation', 'blockers', 'deliveryStatus',
              'runtimeAckDirty', 'createdAt', 'endedAt', 'branch', 'baseHead', 'validatedHead', 'observedAt'}
    return {key: record[key] for key in fields if key in record}


class CodingApplication:
    """Own CodingTask policy, attempts, native resources and result delivery."""
    def __init__(self, root: Path, tasks: TaskRuntime, sessions: SessionStore, *, role_config,
                 authorized, config=None, executor_factory=None, deliver=None, receipt=None, lock_for=None, delegation_authorized=None):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.tasks, self.sessions = tasks, sessions
        self.config = config if config is not None else CodingConfigStore(self.root / 'configuration.db')
        # Business result copy, ACK dirty and canonical completion outbox live
        # in the same database as their idempotent original-session message.
        self.ledger = CodingLedger(Path(sessions.database_path))
        self.role_config, self.authorized = role_config, authorized
        self.delegation_authorized = delegation_authorized or authorized
        self.executor_factory = executor_factory or self._native_executor
        self.deliver, self.receipt, self.lock_for = deliver, receipt, lock_for
        self._tick_lock = threading.Lock()
        self._owner_lock = threading.RLock()
        self._owners: dict[str, TaskExecutor] = {}
        self._worker = None
        self._closed = False
        self._resources_closed = False
        self.probes = NativeProfileProbes(self.root, tasks, self.config,
            role_exists=lambda role_id: not self._closed and self.role_config(role_id) is not None)

    def _native_executor(self, record, ledger):
        if record['executor'] == 'codex_exec':
            from .codex_exec import CodexExecutor
            return CodexExecutor(self.root, record, ledger, authorized=lambda: self._execution_allowed(record))
        from .executor import HerdrExecutor
        return HerdrExecutor(self.root, record, ledger, authorized=lambda: self._execution_allowed(record))

    def _execution_allowed(self, record):
        return self._allowed(record) and not self.ledger.attempt(record['attemptId']).get('executionSealed')

    def _allowed(self, record: dict) -> bool:
        try:
            origin = TaskOrigin(**record['origin'])
            role = self.role_config(origin.role_id)
            if (self._closed or role is None or not role.enabled or not self.authorized(origin)
                    or self.ledger.attempt(record['attemptId']).get('delegationSealed')):
                return False
            if record['executor'] == 'codex_exec' and not self.delegation_authorized(origin):
                return False
            if record['executor'] == 'codex_exec' and record.get('delegationGeneration', 0) != self.delegation_generation(origin.role_id):
                return False
            if record.get('authorityGeneration', 0) != self.generation(origin.role_id):
                return False
            check = getattr(self.config, 'still_authorized', None)
            return check is None or check(origin.role_id, role, record)
        except Exception:
            return False

    def generation(self, role_id):
        getter = getattr(self.config, 'role_generation', None)
        return getter(role_id) if getter else 0

    def freeze_access(self, role_id: str, role: CodingRoleConfig) -> dict:
        setting = self.config.public()
        return {'roleId': role_id, 'enabled': bool(role.enabled and setting['enabled']), 'generation': self.generation(role_id),
                'delegationGeneration': self.delegation_generation(role_id),
                'repositories': {item['id']: item['revision'] for item in setting['repositories']
                                 if item['id'] in role.repoGrantIds and role_id in item['allowedRoleIds']},
                'profiles': {item['id']: item['revision'] for item in setting['profiles']
                             if item['id'] in role.profileIds and role_id in item['allowedRoleIds']
                             and item.get('verification', {}).get('status') == 'verified'
                             and item['verification'].get('revision') == item['revision']},
                'nativeBuild': copy.deepcopy(setting['nativeBuild'])}

    def _admit(self, origin, repository, requirement, kind=None, task_id=None, request_id=None, admitted=lambda: True, frozen_access=None, executor='herdr'):
        authority_generation = self.generation(origin.role_id)
        delegation_generation = self.delegation_generation(origin.role_id)
        role = self.role_config(origin.role_id)
        if self._closed or role is None or not role.enabled or not self.authorized(origin):
            raise PermissionError('original coding owner permission is unavailable')
        if executor == 'codex_exec' and not self.delegation_authorized(origin):
            raise PermissionError('original spawn coding permission is unavailable')
        grant, profile, build = self.config.resolve(origin.role_id, role, repository, kind=kind, executor=executor)
        if frozen_access is not None and (not frozen_access.get('enabled') or frozen_access.get('roleId') != origin.role_id
                or frozen_access.get('generation') != authority_generation
                or executor == 'codex_exec' and frozen_access.get('delegationGeneration', 0) != delegation_generation
                or frozen_access['repositories'].get(grant['id']) != grant['revision']
                or frozen_access['profiles'].get(profile['id']) != profile['revision']
                or executor == 'herdr' and frozen_access['nativeBuild'] != build):
            raise PermissionError('coding grant is outside the frozen run authority')
        with self._owner_lock:
            if self._closed:
                raise PermissionError('coding application has closed admission')
            record, created = self.ledger.admit(origin, requirement=requirement, repository=grant, profile=profile,
                max_daily=role.maxAttemptsPerDay, task_id=task_id, request_id=request_id,
                admitted=lambda: not self._closed and admitted() and self.authorized(origin)
                and self.generation(origin.role_id) == authority_generation
                and (executor != 'codex_exec' or self.delegation_authorized(origin)
                     and self.delegation_generation(origin.role_id) == delegation_generation),
                native_build=build, authority_generation=authority_generation, delegation_generation=delegation_generation)
            if created:
                self._submit(record)
        return public_task(record)

    def start_task(self, origin: TaskOrigin, *, repository: str, requirement: str, agent: str | None = None,
                   request_id: str | None = None, admitted=lambda: True, frozen_access=None, executor='herdr') -> dict:
        return self._admit(origin, repository, requirement, kind=agent, request_id=request_id, admitted=admitted, frozen_access=frozen_access, executor=executor)

    def continue_task(self, task_id: str, origin: TaskOrigin, *, requirement: str, request_id: str | None = None, admitted=lambda: True, frozen_access=None, executor='herdr') -> dict:
        task = self.ledger.inspect(task_id, origin)
        if task['executor'] != executor:
            raise KeyError('task executor differs from this interface')
        return self._admit(origin, task['repository']['id'], requirement, kind=task['profile']['kind'],
                           task_id=task_id, request_id=request_id, admitted=admitted, frozen_access=frozen_access, executor=executor)

    def _submit(self, record):
        if not self.ledger.claim_launch(record['attemptId']):
            return
        origin = TaskOrigin(**record['origin'])
        try:
            executor = self.executor_factory(record, self.ledger)
            with self._owner_lock:
                self._owners[record['attemptId']] = executor
            identifier = self.tasks.submit(origin, executor, kind=self._runtime_kind(record),
                budget=TaskBudget(timeout_seconds=600, log_bytes=1024 * 1024, output_chars=8000),
                description=record['attemptId'], authorized=lambda: self._execution_allowed(record),
                submission_key=record['attemptId'], retain_until_ack=True, completion=False)
            self.ledger.update_attempt(record['attemptId'], runtimeTaskId=identifier)
        except Exception:
            # submit may already have committed and started the same keyed
            # task. Recovery observes kind/scope/attempt; it never swaps keys.
            previous = self._find_runtime(record)
            if previous is not None:
                self.ledger.update_attempt(record['attemptId'], runtimeTaskId=previous.task_id)
                return
            self.ledger.update_attempt(record['attemptId'], status='failed', nativeTurnStatus='not_started',
                                       cleanupState='complete', blockers=['runtime_handover_unavailable'])
            raise

    def _find_runtime(self, record):
        origin = TaskOrigin(**record['origin'])
        matches = [item for item in self.tasks.list(origin, kind=self._runtime_kind(record)) if item.description == record['attemptId']]
        if len(matches) > 1:
            raise RuntimeError('ambiguous CodingTask runtime handover')
        return matches[0] if matches else None

    @staticmethod
    def _runtime_kind(record):
        return {'herdr': 'coding_herdr', 'codex_exec': 'coding_codex'}[record['executor']]

    def inspect(self, task_id: str, origin: TaskOrigin, *, executor='herdr') -> dict:
        if not self.authorized(origin):
            raise PermissionError('original coding source permission was revoked')
        record = self.ledger.inspect(task_id, origin)
        if record['executor'] != executor:
            raise KeyError('task executor differs from this interface')
        return public_task(record)

    def list_tasks(self, origin: TaskOrigin, *, executor='herdr') -> list[dict]:
        if not self.authorized(origin):
            raise PermissionError('original coding source permission was revoked')
        return [public_task(item) for item in self.ledger.list(origin, executor=executor)]

    def cancel_task(self, task_id: str, origin: TaskOrigin, *, executor='herdr') -> dict:
        record = self.ledger.inspect(task_id, origin)
        if record['executor'] != executor or not self.authorized(origin):
            raise PermissionError('original coding cancellation permission was revoked')
        return self._cancel_record(record)

    def cancel_owned_task(self, role_id: str, task_id: str) -> dict:
        """Trusted local owner UI may stop resources after source revocation."""
        matches = [item for item in self.ledger.role_records(role_id) if item['id'] == task_id]
        if not matches:
            raise KeyError('coding task does not belong to this role')
        record = matches[-1]
        return self._cancel_record(record)

    def _cancel_record(self, record):
        with self._owner_lock:
            record = self.ledger.attempt(record['attemptId'])
            owner = self._owners.get(record['attemptId'])
            self.ledger.update_attempt(record['attemptId'], executionSealed=True)
            runtime = record.get('runtimeTaskId')
            if not runtime and record.get('launchClaimed'):
                snapshot = self._find_runtime(record)
                if snapshot is not None:
                    runtime = snapshot.task_id
                    self.ledger.update_attempt(record['attemptId'], runtimeTaskId=runtime)
            if owner is not None:
                owner.cancel()
            if runtime:
                self.tasks.stop(runtime, TaskOrigin(**record['origin']))
            elif owner is None:
                self.ledger.finish_unsubmitted(record['attemptId'], status='cancelled')
            if owner is not None:
                self.ledger.update_attempt(record['attemptId'], cleanupState='complete')
            return public_task(self.ledger.attempt(record['attemptId']))

    async def tick(self):
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            await asyncio.to_thread(self.probes.tick)
            for record in self.ledger.unsettled():
                try:
                    await self._advance(record)
                except (CleanupFailed, sqlite3.Error, PermissionError, KeyError, OSError, ValueError):
                    # Temporary storage, role disable or lost local projection
                    # leaves the durable copy/dirty/receipt for the next tick.
                    continue
        finally:
            self._tick_lock.release()

    async def _advance(self, record):
        origin = TaskOrigin(**record['origin'])
        with self._owner_lock:
            record = self.ledger.attempt(record['attemptId'])
            owner = self._owners.get(record['attemptId'])
            if not record.get('runtimeTaskId') and record.get('launchClaimed'):
                snapshot = self._find_runtime(record)
                if snapshot is not None:
                    self.ledger.update_attempt(record['attemptId'], runtimeTaskId=snapshot.task_id)
                    record['runtimeTaskId'] = snapshot.task_id
            if owner is None and not record.get('runtimeTaskId'):
                self.ledger.finish_unsubmitted(record['attemptId'], status='interrupted')
                record = self.ledger.attempt(record['attemptId'])
        if owner is None and record['cleanupState'] in {'running', 'cleanup_failed'}:
            from .owned_process import named_job_active
            jobs = record.get('resourceJobs')
            active_jobs = False
            for name in jobs or ():
                active_jobs = await asyncio.to_thread(named_job_active, name) or active_jobs
            if jobs is not None and not active_jobs:
                self.ledger.update_attempt(record['attemptId'], cleanupState='complete', resourceJobs=[])
                record['cleanupState'] = 'complete'
        if record['cleanupState'] == 'cleanup_failed':
            with self._owner_lock:
                owner = self._owners.get(record['attemptId'])
            if owner is not None:
                try:
                    await asyncio.to_thread(owner.cancel)
                except (CleanupFailed, OSError):
                    # Resource responsibility and result ACK are independent.
                    # Retain the live owner, freeze admission and still copy
                    # the terminal result for the normal ACK path below.
                    pass
                else:
                    self.ledger.update_attempt(record['attemptId'], cleanupState='complete')
                    record['cleanupState'] = 'complete'
        identifier = record.get('runtimeTaskId')
        if identifier:
            # The copied result is authoritative for ACK recovery. Once ACK
            # succeeded the generic runtime may legitimately prune its result;
            # its immutable submission tombstone still accepts this same ACK.
            if record.get('runtimeResult') and record['runtimeStatus'] in TERMINAL:
                snapshot = None
            else:
                snapshot = await asyncio.to_thread(self.tasks.query, identifier, origin)
                if snapshot.status not in TERMINAL:
                    return
            if snapshot is not None and record['runtimeStatus'] != snapshot.status:
                business = record['status']
                if business in {'accepted', 'running'}:
                    business = snapshot.status if snapshot.status != 'succeeded' else 'unknown'
                self.ledger.update_attempt(record['attemptId'], status=business, runtimeStatus=snapshot.status,
                    runtimeResult={'status': snapshot.status, 'exitCode': snapshot.exit_code, 'output': snapshot.output[:8000]},
                    runtimeAckDirty=True, endedAt=snapshot.ended_at)
            record = self.ledger.attempt(record['attemptId'])
            if record['runtimeAckDirty']:
                await asyncio.to_thread(self.tasks.acknowledge_result, identifier, origin)
                self.ledger.update_attempt(record['attemptId'], runtimeAckDirty=False)
                record['runtimeAckDirty'] = False
            if record['cleanupState'] == 'complete':
                with self._owner_lock:
                    self._owners.pop(record['attemptId'], None)
        if record['status'] in {'accepted', 'running'}:
            return
        if not self._allowed(record):
            self.ledger.update_attempt(record['attemptId'], deliveryStatus='suppressed', messageSynced=True)
            return
        if origin.session_key.startswith('thread:') and self.receipt is not None:
            receipt = self.receipt(record)
            if receipt is not None and (receipt['status'] != record['deliveryStatus']
                    or receipt.get('attemptId') != record.get('deliveryAttemptId')):
                self.ledger.update_attempt(record['attemptId'], deliveryStatus=receipt['status'],
                    deliveryAttemptId=receipt.get('attemptId'), messageSynced=False)
                record = self.ledger.attempt(record['attemptId'])
        if record.get('messageSynced'):
            return
        content = '编程任务 ' + record['id'] + '：' + record['status'] + '\n' + record.get('summary', '')[:8000]
        if self.lock_for is None:
            await self._complete(record, origin, content)
        else:
            async with self.lock_for(origin.role_id):
                await self._complete(record, origin, content)

    async def _complete(self, record, origin, content):
        if not self._allowed(record):
            self.ledger.update_attempt(record['attemptId'], deliveryStatus='suppressed', messageSynced=True)
            return
        if origin.session_key.startswith('thread:'):
            if self.deliver is None:
                return
            current_receipt = self.receipt(record) if self.receipt else None
            if current_receipt is not None:
                self.ledger.update_attempt(record['attemptId'], deliveryStatus=current_receipt['status'],
                                           deliveryAttemptId=current_receipt.get('attemptId'))
            elif record.get('deliveryDispatched'):
                self.ledger.update_attempt(record['attemptId'], deliveryStatus='uncertain')
            else:
                self.ledger.update_attempt(record['attemptId'], deliveryStatus='sending', deliveryDispatched=True)
                try:
                    receipt = await self.deliver(record, content, lambda: self._allowed(record))
                    self.ledger.update_attempt(record['attemptId'], deliveryStatus=receipt['status'],
                                               deliveryAttemptId=receipt.get('attemptId'))
                except Exception:
                    self.ledger.update_attempt(record['attemptId'], deliveryStatus='uncertain')
            record = self.ledger.attempt(record['attemptId'])
        else:
            record['deliveryStatus'] = 'sent'
        await asyncio.to_thread(self._sync_message, record, content)

    def _sync_message(self, record, content):
        origin = TaskOrigin(**record['origin'])
        with self.ledger.transaction() as db:
            row = db.execute('SELECT body FROM coding_attempts WHERE id=?', (record['attemptId'],)).fetchone()
            import json
            current = json.loads(row['body'])
            self.sessions.append_coding_completion_once(origin.role_id, origin.session_key, record['id'], record['attemptId'],
                content, authorized=lambda: self._allowed(record), connection=db,
                task_status=record['status'], delivery_status=record['deliveryStatus'],
                delivery_attempt_id=record.get('deliveryAttemptId'))
            current.update({'messageSynced': True, 'deliveryStatus': record['deliveryStatus']})
            db.execute('UPDATE coding_attempts SET body=? WHERE id=?', (_json(current), record['attemptId']))

    def start(self):
        if self._worker is None or self._worker.done():
            self._closed = False
            self._worker = asyncio.create_task(self._run())

    async def _run(self):
        while not self._closed:
            try:
                await self.tick()
            except (CleanupFailed, sqlite3.Error, OSError):
                pass
            await asyncio.sleep(.1)

    async def close(self):
        if self._resources_closed:
            return
        self._closed = True
        # Business validation can precede the runtime's finally block. Stop by
        # owned runtime kind instead of treating a business status as proof of
        # process exit. Keep all databases/owners alive if cleanup raises.
        for role_id in self.ledger.role_ids():
            await asyncio.to_thread(self.tasks.cancel_role_tasks, role_id, kind='coding_herdr')
            await asyncio.to_thread(self.tasks.cancel_role_tasks, role_id, kind='coding_codex')
        if self._worker is not None:
            try:
                await asyncio.shield(self._worker)
            except asyncio.CancelledError:
                # A terminal cancelled worker is already joined. Cancelling
                # this close caller must retain the live worker for retry.
                caller = asyncio.current_task()
                if not self._worker.cancelled() or caller is not None and caller.cancelling():
                    raise
            except (CleanupFailed, sqlite3.Error, OSError):
                # Consume an earlier recoverable worker failure, then confirm
                # every resource below before any database can be closed.
                pass
        await self.tick()
        with self._owner_lock:
            remaining_owners = list(self._owners.items())
        for attempt_id, owner in remaining_owners:
            await asyncio.to_thread(owner.cancel)
            self.ledger.update_attempt(attempt_id, cleanupState='complete')
            with self._owner_lock:
                self._owners.pop(attempt_id, None)
        if any(record['cleanupState'] not in {'complete', 'not_started'}
               for role_id in self.ledger.role_ids() for record in self.ledger.role_records(role_id)):
            raise CleanupFailed('owned Coding resources have not been confirmed stopped')
        await asyncio.to_thread(self.probes.close)
        self.ledger.close()
        self.config.close()
        self._resources_closed = True

    async def invalidate_role(self, role_id: str):
        revoke = getattr(self.config, 'revoke_role', None)
        if revoke:
            await asyncio.to_thread(revoke, role_id)
        await asyncio.to_thread(self.tasks.cancel_role_tasks, role_id, kind='coding_qualification')
        await asyncio.to_thread(self.tasks.cancel_role_tasks, role_id, kind='coding_herdr')
        await asyncio.to_thread(self.tasks.cancel_role_tasks, role_id, kind='coding_codex')
        # The caller closes admission before awaiting this method. Recovery
        # still copies/ACKs terminal results even when delivery is suppressed.
        await self.tick()

    def seal_delegation(self, role_id: str):
        """Seal before any awaited cleanup can expose a subsequent re-enable."""
        revoke = getattr(self.config, 'revoke_delegation', None)
        if revoke:
            revoke(role_id)
        with self._owner_lock:
            for record in self.ledger.role_records(role_id):
                if record['executor'] == 'codex_exec':
                    self.ledger.update_attempt(record['attemptId'], delegationSealed=True, executionSealed=True)

    async def invalidate_delegation(self, role_id: str, *, seal=True):
        """Closing spawn seals existing Codex delegates permanently, even on re-enable."""
        if seal:
            self.seal_delegation(role_id)
        await asyncio.to_thread(self.tasks.cancel_role_tasks, role_id, kind='coding_codex')
        await self.tick()

    def delegation_generation(self, role_id):
        getter = getattr(self.config, 'delegation_generation', None)
        return getter(role_id) if getter else 0

    async def delete_role(self, role_id: str):
        await self.prepare_role_deletion(role_id)
        # Retain worktrees and their host ownership metadata. A role deletion
        # never force-removes uncommitted artifacts.
        self.ledger.delete_role(role_id)

    async def prepare_role_deletion(self, role_id: str):
        await self.invalidate_role(role_id)
        records = self.ledger.role_records(role_id)
        if any(item['cleanupState'] not in {'complete', 'not_started'} for item in records):
            raise RuntimeError('Coding resources have not been confirmed stopped')
        if self.probes.role_resources_pending(role_id):
            raise RuntimeError('native CLI qualification resources have not been confirmed stopped')
