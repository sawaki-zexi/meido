"""Coding exec shares durable handover/ACK duties without replaying the CLI."""
import asyncio
import json
from pathlib import Path
import sqlite3
import time

import pytest

from backend.app.coding_tasks.application import CodingApplication
from backend.app.coding_tasks.config import CodingRoleConfig
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskOrigin, TaskRuntime
from test_coding_spawn import Configuration, Executor

pytestmark = pytest.mark.usefixtures('local_platform_only')


@pytest.mark.parametrize('fault', ['submit-return', 'runtime-id-write', 'ack-dirty-clear'])
def test_exec_handover_and_ack_faults_keep_original_attempt_without_replay(tmp_path, monkeypatch, fault):
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    sessions.open_role_session('r')
    tasks = TaskRuntime(tmp_path / 'tasks', retention_seconds=0 if fault == 'ack-dirty-clear' else 86400)
    calls = []
    def factory(record, ledger):
        calls.append(record['attemptId'])
        return Executor(record, ledger)
    app = CodingApplication(tmp_path / 'coding', tasks, sessions,
        role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True,
        config=Configuration(), executor_factory=factory)
    origin = TaskOrigin('r', 'role:r', 'parent', 'call')
    fired = []
    original_submit, original_ack = tasks.submit, tasks.acknowledge_result
    if fault == 'submit-return':
        def lost_return(*args, **kwargs):
            identifier = original_submit(*args, **kwargs)
            if not fired:
                fired.append(identifier)
                raise OSError('accepted return lost')
            return identifier
        monkeypatch.setattr(tasks, 'submit', lost_return)
    if fault == 'runtime-id-write':
        with sqlite3.connect(sessions.database_path) as db:
            db.execute("""CREATE TRIGGER fail_task_id BEFORE UPDATE ON coding_attempts
                WHEN NEW.runtime_id IS NOT NULL AND OLD.runtime_id IS NULL
                BEGIN SELECT RAISE(ABORT,'real taskId write failure'); END""")
    if fault == 'ack-dirty-clear':
        def ack_then_db_failure(identifier, source):
            result = original_ack(identifier, source)
            if not fired:
                fired.append(identifier)
                with sqlite3.connect(sessions.database_path) as db:
                    db.execute("""CREATE TRIGGER fail_ack_clear BEFORE UPDATE ON coding_attempts
                        WHEN NEW.ack_dirty=0 AND OLD.ack_dirty=1
                        BEGIN SELECT RAISE(ABORT,'real dirty clear failure'); END""")
            return result
        monkeypatch.setattr(tasks, 'acknowledge_result', ack_then_db_failure)
    try:
        if fault == 'runtime-id-write':
            with pytest.raises(sqlite3.IntegrityError):
                app.start_task(origin, repository=str(tmp_path), requirement='one native dispatch', executor='codex_exec')
            with sqlite3.connect(sessions.database_path) as db:
                db.execute('DROP TRIGGER fail_task_id')
            record = app.list_tasks(origin, executor='codex_exec')[0]
        else:
            record = app.start_task(origin, repository=str(tmp_path), requirement='one native dispatch', executor='codex_exec')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            asyncio.run(app.tick())
            current = app.inspect(record['id'], origin, executor='codex_exec')
            if fault == 'ack-dirty-clear' and fired:
                assert current['runtimeAckDirty']
                with pytest.raises(KeyError):
                    tasks.query(fired[0], origin)
                with sqlite3.connect(sessions.database_path) as db:
                    db.execute('DROP TRIGGER fail_ack_clear')
                fault = 'resolved'
            if current['deliveryStatus'] == 'sent' and not current['runtimeAckDirty']:
                break
            time.sleep(.01)
        assert current['status'] == 'validated_diff' and not current['runtimeAckDirty']
        assert current['deliveryStatus'] == 'sent'
        assert len(sessions.list_messages('role:r')) == 1
        again = app.start_task(origin, repository=str(tmp_path), requirement='one native dispatch', executor='codex_exec')
        assert again['id'] == record['id'] and again['attemptId'] == record['attemptId']
        assert calls == [record['attemptId']]
    finally:
        with sqlite3.connect(sessions.database_path) as db:
            db.execute('DROP TRIGGER IF EXISTS fail_task_id')
            db.execute('DROP TRIGGER IF EXISTS fail_ack_clear')
        asyncio.run(app.close())
        tasks.close()


def test_revoked_delegate_finishes_ack_without_retargeting_desktop(tmp_path):
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    sessions.open_role_session('r')
    tasks = TaskRuntime(tmp_path / 'tasks')
    allowed = [True]
    app = CodingApplication(tmp_path / 'coding', tasks, sessions,
        role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: allowed[0],
        config=Configuration(), executor_factory=Executor)
    from backend.app.channels.source_authority import FrozenChannelSource
    source = FrozenChannelSource('r', 'thread:original-source', 'original-source', 'account',
        '2026-01-01T00:00:00+00:00', 'qq', 'chat', 'private', 'sender', True, 'identity',
        '2026-01-01T01:00:00+00:00', '2026-01-01T02:00:00+00:00', '2026-01-01T01:00:00+00:00')
    origin = TaskOrigin('r', source.session_key, 'parent', 'call', source_thread_id=source.thread_id,
        source_account_id=source.account_id, source_scope_id='binding', source_authority_json=json.dumps(source.to_dict()))
    try:
        record = app.start_task(origin, repository=str(tmp_path), requirement='only original source', executor='codex_exec')
        allowed[0] = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            asyncio.run(app.tick())
            current = app.ledger.attempt(record['attemptId'])
            if current['deliveryStatus'] == 'suppressed' and not current['runtimeAckDirty'] and current.get('runtimeResult'):
                break
            time.sleep(.01)
        assert current['deliveryStatus'] == 'suppressed' and not current['runtimeAckDirty']
        assert not sessions.list_messages('role:r')
        with pytest.raises(KeyError):
            app.ledger.inspect(record['id'], TaskOrigin('other', 'thread:original-source'))
    finally:
        asyncio.run(app.close())
        tasks.close()
