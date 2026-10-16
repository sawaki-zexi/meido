import asyncio
from pathlib import Path
import time

import pytest

from backend.app.coding_tasks.application import CodingApplication
from backend.app.coding_tasks.config import CodingRoleConfig
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskOrigin, TaskRuntime

pytestmark = pytest.mark.usefixtures('local_platform_only')


class GrantedConfiguration:
    def resolve(self, role_id, role, repository, **kwargs):
        return ({'id': 'repo-a', 'revision': 'repo-revision', 'root': repository},
                {'id': 'profile-a', 'revision': 'profile-revision', 'executor': 'herdr', 'kind': 'codex'},
                {'executable': 'fixture-only'})

    def close(self):
        pass


class ControlledExecutor:
    def __init__(self, record, ledger):
        self.record, self.ledger = record, ledger

    def run(self, execution):
        from backend.app.task_runtime import TaskOutcome
        self.ledger.update_attempt(self.record['attemptId'], status='validated_diff', summary='真实结果副本',
                                   cleanupState='complete', nativeSession='fixture-native-id')
        return TaskOutcome('succeeded', 0, 'bounded runtime result')

    def cancel(self):
        pass


def test_background_attempt_survives_parent_and_copies_result_before_runtime_ack(tmp_path):
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    sessions.open_role_session('role-a')
    tasks = TaskRuntime(tmp_path / 'tasks')
    calls = []

    def factory(record, ledger):
        calls.append(record['attemptId'])
        return ControlledExecutor(record, ledger)

    application = CodingApplication(tmp_path / 'coding', tasks, sessions,
        role_config=lambda role_id: CodingRoleConfig(enabled=True), authorized=lambda origin: True,
        config=GrantedConfiguration(), executor_factory=factory)
    origin = TaskOrigin('role-a', 'role:role-a', 'parent-run', 'call-one')
    try:
        first = application.start_task(origin, repository=str(tmp_path), requirement='完整需求')
        assert first['status'] == 'accepted'
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            asyncio.run(application.tick())
            result = application.inspect(first['id'], origin)
            if result['deliveryStatus'] == 'sent':
                break
            time.sleep(.01)
        assert result['status'] == 'validated_diff'
        assert result['summary'] == '真实结果副本'
        assert result['runtimeAckDirty'] is False
        assert result['deliveryStatus'] == 'sent'
        assert len(sessions.list_messages('role:role-a')) == 1
        again = application.start_task(origin, repository=str(tmp_path), requirement='完整需求')
        assert again['id'] == first['id']
        assert len(calls) == 1
        with pytest.raises(KeyError):
            application.inspect(first['id'], TaskOrigin('role-b', 'role:role-b'))
    finally:
        asyncio.run(application.close())
        tasks.close()


def test_revocation_between_frozen_check_and_atomic_admission_cannot_capture_new_authority(tmp_path, monkeypatch):
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    sessions.open_role_session('role-a')
    tasks = TaskRuntime(tmp_path / 'tasks')
    class Configuration(GrantedConfiguration):
        epoch = 0
        def role_generation(self, role_id): return self.epoch
    configuration = Configuration()
    calls = []
    def factory(record, ledger):
        calls.append(record)
        return ControlledExecutor(record, ledger)
    application = CodingApplication(tmp_path / 'coding', tasks, sessions,
        role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True,
        config=configuration, executor_factory=factory)
    access = {'enabled': True, 'roleId': 'role-a', 'generation': 0,
        'repositories': {'repo-a': 'repo-revision'}, 'profiles': {'profile-a': 'profile-revision'},
        'nativeBuild': {'executable': 'fixture-only'}}
    original = application.ledger.admit
    def revoke_at_transaction(*args, **kwargs):
        configuration.epoch += 1  # owner closes/reopens after the outer check
        return original(*args, **kwargs)
    monkeypatch.setattr(application.ledger, 'admit', revoke_at_transaction)
    origin = TaskOrigin('role-a', 'role:role-a', 'parent', 'frozen-call')
    try:
        with pytest.raises(PermissionError):
            application.start_task(origin, repository=str(tmp_path), requirement='old frozen authority', frozen_access=access)
        assert not calls and not application.ledger.list(origin)
    finally:
        asyncio.run(application.close())
        tasks.close()


def test_cleanup_failure_keeps_owner_and_acks_terminal_result_before_retry(tmp_path):
    import threading
    from backend.app.coding_tasks.owned_process import CleanupFailed, OwnedProcess
    from backend.app.task_runtime import TaskOutcome
    import sys
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    sessions.open_role_session('role-a')
    tasks = TaskRuntime(tmp_path / 'tasks')
    release = threading.Event()
    class ResourceExecutor:
        process = None
        def __init__(self, record, ledger):
            self.record, self.ledger = record, ledger
        def run(self, execution):
            self.process = OwnedProcess([sys.executable, '-I', '-c', 'import time; time.sleep(600)'], tmp_path, {})
            self.ledger.update_attempt(self.record['attemptId'], status='unknown', cleanupState='cleanup_failed', summary='cleanup pending')
            return TaskOutcome('failed', None, 'resource cleanup pending')
        def cancel(self):
            if not release.is_set():
                raise CleanupFailed('fixture transient cleanup refusal')
            self.process.close()
    application = CodingApplication(tmp_path / 'coding', tasks, sessions,
        role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True,
        config=GrantedConfiguration(), executor_factory=ResourceExecutor)
    origin = TaskOrigin('role-a', 'role:role-a', 'parent', 'cleanup-call')
    try:
        task = application.start_task(origin, repository=str(tmp_path), requirement='owned resource')
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            asyncio.run(application.tick())
            observed = application.inspect(task['id'], origin)
            if observed['runtimeStatus'] == 'failed': break
            time.sleep(.01)
        assert observed['cleanupState'] == 'cleanup_failed'
        assert observed['runtimeAckDirty'] is False
        owner = application._owners[task['attemptId']]
        assert owner.process.process.poll() is None
        with pytest.raises(ValueError):
            application.start_task(TaskOrigin('role-a', 'role:role-a', 'later', 'other-call'), repository=str(tmp_path), requirement='must stay frozen')
        release.set()
        asyncio.run(application.tick())
        assert owner.process.process.poll() is not None
        assert application.inspect(task['id'], origin)['cleanupState'] == 'complete'
        assert task['attemptId'] not in application._owners
    finally:
        release.set()
        asyncio.run(application.close())
        tasks.close()


def test_ack_recovery_after_acknowledged_runtime_was_pruned(tmp_path, monkeypatch):
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    sessions.open_role_session('role-a')
    tasks = TaskRuntime(tmp_path / 'tasks', retention_seconds=0)
    application = CodingApplication(tmp_path / 'coding', tasks, sessions,
        role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True,
        config=GrantedConfiguration(), executor_factory=ControlledExecutor)
    origin = TaskOrigin('role-a', 'role:role-a', 'parent', 'call')
    original_update = application.ledger.update_attempt
    failed = False

    def lose_dirty_clear(attempt_id, **changes):
        nonlocal failed
        if changes == {'runtimeAckDirty': False} and not failed:
            failed = True
            import sqlite3
            raise sqlite3.OperationalError('fixture crash after successful ACK')
        return original_update(attempt_id, **changes)

    monkeypatch.setattr(application.ledger, 'update_attempt', lose_dirty_clear)
    try:
        task = application.start_task(origin, repository=str(tmp_path), requirement='one dispatch')
        deadline = time.monotonic() + 5
        while not failed and time.monotonic() < deadline:
            asyncio.run(application.tick())
            time.sleep(.01)
        assert failed
        copied = application.ledger.attempt(task['attemptId'])
        assert copied['runtimeAckDirty'] and copied['runtimeResult']['status'] == 'succeeded'
        with pytest.raises(KeyError):
            tasks.query(copied['runtimeTaskId'], origin)
        asyncio.run(application.tick())
        restored = application.inspect(task['id'], origin)
        assert restored['runtimeAckDirty'] is False
        assert restored['deliveryStatus'] == 'sent'
        assert len(sessions.list_messages('role:role-a')) == 1
        assert application.start_task(origin, repository=str(tmp_path), requirement='one dispatch')['id'] == task['id']
        assert not tasks.list(origin, kind='coding_herdr')
    finally:
        asyncio.run(application.close())
        tasks.close()
