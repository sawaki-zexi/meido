"""Crash recovery keeps native resource duties separate from business results."""
import asyncio
import os
import sys
import time
import uuid
import sqlite3

import pytest

from backend.app.coding_tasks.application import CodingApplication
from backend.app.coding_tasks.config import CodingRoleConfig
from backend.app.coding_tasks.owned_process import OwnedProcess, named_job_active
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskOrigin, TaskRuntime, TaskOutcome
from test_coding_application import GrantedConfiguration
from test_coding_application import ControlledExecutor


pytestmark = pytest.mark.usefixtures('local_platform_only')


@pytest.mark.skipif(os.name != 'nt', reason='Windows named Job recovery acceptance')
def test_recovered_terminal_attempt_with_live_job_keeps_role_repo_profile_frozen(tmp_path):
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    sessions.open_role_session('role-a')
    tasks = TaskRuntime(tmp_path / 'runtime')
    owners = []
    job = 'Local\\Meido.Coding.recovery-' + uuid.uuid4().hex

    class RetainedResource:
        def __init__(self, record, ledger):
            self.record, self.ledger, self.process = record, ledger, None
            owners.append(self)

        def run(self, execution):
            self.process = OwnedProcess([sys.executable, '-I', '-c', 'import time; time.sleep(60)'], tmp_path, {},
                job_name=job, journal=lambda name, active: self.ledger.resource_job(self.record['attemptId'], name, active))
            self.ledger.update_attempt(self.record['attemptId'], status='unknown', cleanupState='running')
            return TaskOutcome('failed', None, 'native state remains unknown')

        def cancel(self):
            if self.process is not None:
                self.process.close()

    first = CodingApplication(tmp_path / 'coding', tasks, sessions, config=GrantedConfiguration(),
        role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True,
        executor_factory=RetainedResource)
    reopened = None
    try:
        origin = TaskOrigin('role-a', 'role:role-a', 'parent', 'original')
        task = first.start_task(origin, repository=str(tmp_path), requirement='owned resource')
        tasks.query(first.ledger.attempt(task['attemptId'])['runtimeTaskId'], origin, wait_seconds=3)
        assert named_job_active(job) and owners[0].process.process.poll() is None
        reopened = CodingApplication(tmp_path / 'coding', tasks, sessions, config=GrantedConfiguration(),
            role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True,
            executor_factory=RetainedResource)
        asyncio.run(reopened.tick())
        assert reopened.ledger.attempt(task['attemptId'])['cleanupState'] == 'running'
        with pytest.raises(ValueError, match='concurrency|cleanup|resource'):
            reopened.start_task(TaskOrigin('role-a', 'role:role-a', 'later', 'new-call'),
                repository=str(tmp_path), requirement='must wait for original Job exit')
        with pytest.raises(ValueError, match='profile|cleanup|resource'):
            reopened.ledger.admit(TaskOrigin('role-b', 'role:role-b', 'later', 'other-role'),
                requirement='same frozen profile', repository={'id': 'other-repo', 'revision': 'r'},
                profile={'id': 'profile-a', 'revision': 'p', 'executor': 'herdr'}, max_daily=5)
        owners[0].cancel()
        asyncio.run(reopened.tick())
        assert reopened.ledger.attempt(task['attemptId'])['cleanupState'] == 'complete'
    finally:
        for owner in owners:
            owner.cancel()
        if reopened is not None:
            asyncio.run(reopened.close())
        asyncio.run(first.close())
        tasks.close()


def test_prelaunch_snapshot_storage_failure_rolls_back_admission(tmp_path):
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    sessions.open_role_session('role-a')
    tasks, launched = TaskRuntime(tmp_path / 'runtime'), []

    def factory(record, ledger):
        launched.append(record['attemptId'])
        assert record['nativeBuild'] == {'executable': 'fixture-only'}
        assert record['authorityGeneration'] == 0
        return ControlledExecutor(record, ledger)

    app = CodingApplication(tmp_path / 'coding', tasks, sessions, config=GrantedConfiguration(),
        role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True, executor_factory=factory)
    origin = TaskOrigin('role-a', 'role:role-a', 'parent', 'prelaunch-fault')
    try:
        with sqlite3.connect(sessions.database_path) as db:
            for operation in ('INSERT', 'UPDATE'):
                db.execute(f"""CREATE TRIGGER fail_prelaunch_{operation} BEFORE {operation} ON coding_attempts
                    WHEN json_extract(NEW.body,'$.nativeBuild') IS NOT NULL
                    BEGIN SELECT RAISE(ABORT,'controlled snapshot storage failure'); END""")
        with pytest.raises(sqlite3.IntegrityError):
            app.start_task(origin, repository=str(tmp_path), requirement='never dispatched')
        assert launched == [] and not tasks.list(origin, kind='coding_herdr')
        assert app.list_tasks(origin) == [], 'partial prelaunch admission permanently consumed the role slot'
        with sqlite3.connect(sessions.database_path) as db:
            for operation in ('INSERT', 'UPDATE'):
                db.execute(f'DROP TRIGGER fail_prelaunch_{operation}')
        record = app.start_task(origin, repository=str(tmp_path), requirement='never dispatched')
        assert launched == [record['attemptId']]
    finally:
        asyncio.run(app.close())
        tasks.close()


@pytest.mark.parametrize('action', ['recover', 'owner_cancel', 'source_cancel'])
@pytest.mark.parametrize('claimed', [False, True], ids=['unclaimed', 'claimed'])
def test_legacy_unsubmitted_admission_finishes_without_late_execution(tmp_path, action, claimed):
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    sessions.open_role_session('role-a')
    tasks, launched = TaskRuntime(tmp_path / 'runtime'), []

    def forbidden_factory(*args):
        launched.append(args)
        raise AssertionError('recovery cannot replay an unsubmitted native operation')

    app = CodingApplication(tmp_path / 'coding', tasks, sessions, config=GrantedConfiguration(),
        role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True, executor_factory=forbidden_factory)
    origin = TaskOrigin('role-a', 'role:role-a', 'parent', 'legacy')
    record, _ = app.ledger.admit(origin, requirement='legacy partial admission',
        repository={'id': 'repo-a', 'revision': 'r'},
        profile={'id': 'profile-a', 'revision': 'p', 'executor': 'herdr'}, max_daily=5)
    if claimed:
        assert app.ledger.claim_launch(record['attemptId'])
    try:
        if action == 'recover':
            asyncio.run(app.tick())
            assert app.inspect(record['id'], origin)['status'] == 'interrupted'
        else:
            result = (app.cancel_owned_task('role-a', record['id']) if action == 'owner_cancel'
                      else app.cancel_task(record['id'], origin))
            assert result['status'] == 'cancelled'
        assert not app.ledger.claim_launch(record['attemptId'])
        for _ in range(3):
            asyncio.run(app.tick())
        assert launched == [] and not tasks.list(origin, kind='coding_herdr')
        assert app.inspect(record['id'], origin)['cleanupState'] == 'complete'
    finally:
        asyncio.run(app.close())
        tasks.close()


def test_role_generation_revoked_at_final_sequence_read_blocks_completion_insert(tmp_path):
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    sessions.open_role_session('role-a')
    tasks = TaskRuntime(tmp_path / 'runtime')

    class Configuration(GrantedConfiguration):
        epoch = 0

        def role_generation(self, role_id):
            return self.epoch

    config = Configuration()
    app = CodingApplication(tmp_path / 'coding', tasks, sessions, config=config,
        role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True, executor_factory=ControlledExecutor)
    origin, revoked = TaskOrigin('role-a', 'role:role-a', 'parent', 'late-revoke'), []

    def revoke_at_final_sequence_read(sql):
        if 'COALESCE(MAX(sequence),0)+1 FROM messages' in sql and not revoked:
            config.epoch += 1
            revoked.append(True)

    try:
        record = app.start_task(origin, repository=str(tmp_path), requirement='one completion')
        tasks.query(app.ledger.attempt(record['attemptId'])['runtimeTaskId'], origin, wait_seconds=2)
        app.ledger._db.set_trace_callback(revoke_at_final_sequence_read)
        asyncio.run(app.tick())
        assert revoked and not sessions.list_messages('role:role-a')
        assert app.inspect(record['id'], origin)['deliveryStatus'] != 'sent'
        app.ledger._db.set_trace_callback(None)
        asyncio.run(app.tick())
        assert app.inspect(record['id'], origin)['deliveryStatus'] == 'suppressed'
    finally:
        app.ledger._db.set_trace_callback(None)
        asyncio.run(app.close())
        tasks.close()


def test_transient_native_job_observation_retries_without_stopping_worker_or_close(tmp_path, monkeypatch):
    from backend.app.coding_tasks import owned_process

    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    sessions.open_role_session('role-a')
    tasks = TaskRuntime(tmp_path / 'runtime')
    app = CodingApplication(tmp_path / 'coding', tasks, sessions, config=GrantedConfiguration(),
        role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True)
    origin = TaskOrigin('role-a', 'role:role-a', 'original', 'call')
    record, _ = app.ledger.admit(origin, requirement='recover resources',
        repository={'id': 'repo-a', 'revision': 'r'},
        profile={'id': 'profile-a', 'revision': 'p', 'executor': 'herdr'}, max_daily=5)
    app.ledger.update_attempt(record['attemptId'], status='unknown', cleanupState='running',
        resourceJobs=['Local\\Meido.Coding.test-transient'])
    calls = []

    def transient_failure(name):
        calls.append(name)
        if len(calls) == 1:
            raise owned_process.CleanupFailed('controlled transient Job query failure')
        return False

    monkeypatch.setattr(owned_process, 'named_job_active', transient_failure)

    async def scenario():
        app.start()
        try:
            async with asyncio.timeout(2):
                while len(calls) < 2 and not app._worker.done():
                    await asyncio.sleep(.01)
            assert len(calls) >= 2, 'transient resource observation stopped the global worker'
            assert not app._worker.done()
            assert app.ledger.attempt(record['attemptId'])['cleanupState'] == 'complete'
        finally:
            try:
                await app.close()
            except owned_process.CleanupFailed:
                # Only the RED run has a failed worker; consume it before close
                # retries so all databases/resources stay fixture-owned.
                app._worker = None
                await app.close()
            tasks.close()

    asyncio.run(scenario())


def test_unconfirmed_native_job_keeps_close_retryable_and_databases_open(tmp_path, monkeypatch):
    from backend.app.coding_tasks import owned_process

    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    sessions.open_role_session('role-a')
    tasks = TaskRuntime(tmp_path / 'runtime')
    app = CodingApplication(tmp_path / 'coding', tasks, sessions, config=GrantedConfiguration(),
        role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True)
    origin = TaskOrigin('role-a', 'role:role-a', 'original', 'close-query')
    record, _ = app.ledger.admit(origin, requirement='unconfirmed resources',
        repository={'id': 'repo-a', 'revision': 'r'},
        profile={'id': 'profile-a', 'revision': 'p', 'executor': 'herdr'}, max_daily=5)
    app.ledger.update_attempt(record['attemptId'], status='unknown', cleanupState='running',
        resourceJobs=['Local\\Meido.Coding.test-unconfirmed'])
    released = []

    def observation(name):
        if not released:
            raise owned_process.CleanupFailed('controlled persistent Job query failure')
        return False

    monkeypatch.setattr(owned_process, 'named_job_active', observation)
    try:
        with pytest.raises(owned_process.CleanupFailed):
            asyncio.run(app.close())
        assert app.inspect(record['id'], origin)['cleanupState'] == 'running'
    finally:
        released.append(True)
        asyncio.run(app.close())
        tasks.close()


def test_close_consumes_previous_worker_resource_failure_and_still_finishes_cleanup(tmp_path):
    from backend.app.coding_tasks.owned_process import CleanupFailed

    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    tasks = TaskRuntime(tmp_path / 'runtime')
    app = CodingApplication(tmp_path / 'coding', tasks, sessions, config=GrantedConfiguration(),
        role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True)

    async def previous_worker():
        raise CleanupFailed('controlled already failed worker')

    async def scenario():
        app._worker = asyncio.create_task(previous_worker())
        await asyncio.sleep(0)
        try:
            await app.close()
            await app.close()
        finally:
            if not app._resources_closed:
                app._worker = None
                await app.close()

    try:
        asyncio.run(scenario())
    finally:
        tasks.close()
