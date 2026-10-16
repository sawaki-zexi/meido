"""Committed Drift progress recovers local projections without replaying its run."""
import asyncio
import json
import sqlite3
import threading
from contextlib import contextmanager

import pytest

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401
from test_proactive_drift_delivery import prepare, settled

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_projection_never_writes_or_clears_obligation_when_original_root_changes_after_preview_check(api, monkeypatch):
    client, roles, _, _ = api
    role, _, now = prepare(client, monkeypatch)
    app = main._proactive
    root = roles.workspace_path(role) / 'drift'
    held, foreign = root.with_name('held-original-drift'), root.with_name('retained-foreign-drift')
    original_project = app.drift_files.project
    def swapped(*args, **kwargs):
        root.rename(held)
        (root / 'state').mkdir(parents=True)
        (root / 'state/reading-notes.json').write_text('foreign state must survive', encoding='utf-8')
        return original_project(*args, **kwargs)
    class Finish:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            if self.requests == 1:
                name, args = 'activate_skill', {'skill_id': 'reading-notes'}
            else:
                assert self.requests == 2
                name, args = 'finish_drift', {'skill_used': 'reading-notes', 'one_line': '原根中的真实进度',
                    'next_action': '保留原目录责任', 'message_result': 'silent'}
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'projection-identity-{self.requests}', name, args),)))
    model = Finish()
    monkeypatch.setattr(main, 'model_adapter', model)
    try:
        with monkeypatch.context() as local:
            local.setattr(app.drift_files, 'project', swapped)
            now[0] += 1800
            state = asyncio.run(settled(app, role))
            assert model.requests == 2 and state['runs'][0]['status'] == 'finished_silent'
            assert (root / 'state/reading-notes.json').read_text(encoding='utf-8') == 'foreign state must survive'
            with app._transaction() as db:
                progress = dict(db.execute('SELECT * FROM autonomy_drift_progress WHERE role_id=?', (role,)).fetchone())
                original_run = dict(db.execute('SELECT * FROM autonomy_runs WHERE role_id=?', (role,)).fetchone())
                original_deliveries = [dict(row) for row in db.execute('SELECT * FROM autonomy_deliveries WHERE role_id=?', (role,))]
                assert progress['projection_pending'] == 1
        asyncio.run(app.close())
        monkeypatch.setattr(main, '_proactive', None)
        root.rename(foreign)
        held.rename(root)
        restored = main._proactive_application()
        asyncio.run(restored.recover())
        value = json.loads((root / 'state/reading-notes.json').read_text(encoding='utf-8'))
        assert value['runCount'] == 1 and value['oneLine'] == '原根中的真实进度'
        assert (foreign / 'state/reading-notes.json').read_text(encoding='utf-8') == 'foreign state must survive'
        assert model.requests == 2
        with restored._transaction() as db:
            recovered = dict(db.execute('SELECT * FROM autonomy_drift_progress WHERE role_id=?', (role,)).fetchone())
            assert recovered == {**progress, 'projection_pending': 0}
            assert dict(db.execute('SELECT * FROM autonomy_runs WHERE role_id=?', (role,)).fetchone()) == original_run
            assert [dict(row) for row in db.execute('SELECT * FROM autonomy_deliveries WHERE role_id=?', (role,))] == original_deliveries
        assert restored.query(role)['runs'][0]['runtimeAcknowledged']
    finally:
        if held.exists():
            root.rename(foreign)
            held.rename(root)
        if main._proactive is not None:
            asyncio.run(main._proactive.close())


@pytest.mark.parametrize('missing_original_identity', [False, True])
def test_committed_finish_survives_projection_failure_and_restart_recovers_only_same_local_version(api, monkeypatch, missing_original_identity):
    client, roles, _, _ = api
    role, path, now = prepare(client, monkeypatch)
    state_path = roles.workspace_path(role) / 'drift/state'
    state_path.write_text('real file blocks directory projection', encoding='utf-8')
    class Finish:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            if self.requests == 1:
                name, arguments = 'activate_skill', {'skill_id': 'reading-notes'}
            else:
                assert self.requests == 2
                name, arguments = 'finish_drift', {'skill_used': 'reading-notes', 'one_line': '实际提交进度',
                    'next_action': '恢复文件投影', 'message_result': 'silent'}
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'projection-{self.requests}', name, arguments),)))
    adapter = Finish()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    try:
        now[0] += 1800
        state = asyncio.run(settled(main._proactive, role))
        assert adapter.requests == 2 and state['runs'][0]['status'] == 'finished_silent'
        skill = next(item for item in client.get(path + '/drift-skills').json()['skills'] if item['id'] == 'reading-notes')
        assert skill['progress']['projectionPending'] and skill['progress']['runCount'] == 1
        if missing_original_identity:
            with main._proactive._transaction() as db:
                db.execute("UPDATE autonomy_drift_progress SET root_identity='' WHERE role_id=?", (role,))
        asyncio.run(main._proactive.close())
        monkeypatch.setattr(main, '_proactive', None)
        state_path.unlink()
        app = main._proactive_application()
        asyncio.run(app.recover())
        projection = state_path / 'reading-notes.json'
        if missing_original_identity:
            assert not projection.exists(), 'a legacy row without its original root identity cannot authorize a new projection'
        else:
            assert projection.is_file(), 'recovery must consume committed local progress, not rerun the model'
            value = json.loads(projection.read_text(encoding='utf-8'))
            assert value['runCount'] == 1 and value['nextAction'] == '恢复文件投影'
        assert adapter.requests == 2
        assert len(app.query(role)['runs']) == 1 and app.query(role)['runs'][0]['runtimeAcknowledged']
        skill = next(item for item in client.get(path + '/drift-skills').json()['skills'] if item['id'] == 'reading-notes')
        assert skill['progress']['projectionPending'] is missing_original_identity
    finally:
        asyncio.run(main._proactive.close())


def test_recovery_defers_real_native_sqlite_busy_without_losing_work_and_rethrows_nonbusy_sqlite(api, monkeypatch):
    client, _, sessions, _ = api
    role, _, _ = prepare(client, monkeypatch)
    app = main._proactive
    try:
        with sqlite3.connect(sessions.database_path) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            asyncio.run(app.recover())
            blocker.rollback()
        assert app.query(role)['runs'] == []
        original = app._transaction
        @contextmanager
        def actual_nonbusy_sql_error():
            with original() as db:
                db.execute('SELECT definitely_missing_179_column')
                yield db
        with monkeypatch.context() as local:
            local.setattr(app, '_transaction', actual_nonbusy_sql_error)
            with pytest.raises(sqlite3.OperationalError) as failure:
                asyncio.run(app.recover())
            assert failure.value.sqlite_errorcode == sqlite3.SQLITE_ERROR
    finally:
        asyncio.run(app.close())


def test_real_busy_after_send_claim_keeps_owned_cancel_and_original_runtime_result_recovery(api, monkeypatch):
    client, _, sessions, _ = api
    role, _, now = prepare(client, monkeypatch, extra_tools=('message_push',))
    app = main._proactive
    entered, released = threading.Event(), threading.Event()
    class HeldDesktop:
        async def deliver(self, turn, intent_id, draft):
            with sqlite3.connect(sessions.database_path) as blocker:
                blocker.execute('BEGIN IMMEDIATE')
                entered.set()
                try:
                    while True:
                        await asyncio.sleep(.01)
                finally:
                    blocker.rollback()
                    released.set()
    app.drift_delivery = HeldDesktop()
    class Model:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            if self.requests == 1:
                name, arguments = 'activate_skill', {'skill_id': 'reading-notes'}
            else:
                assert self.requests == 2
                name, arguments = 'message_push', {'message': 'claimed before native busy', 'mood': '', 'thought': ''}
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'busy-claim-{self.requests}', name, arguments),)))
    model = Model()
    monkeypatch.setattr(main, 'model_adapter', model)
    async def exercise():
        now[0] += 1800
        await app.on_clock()
        async with asyncio.timeout(10):
            while not entered.is_set():
                await asyncio.sleep(.01)
        # Busy is not evidence of an absent task; native authorization monitoring
        # conservatively seals/cancels while its executor still owns cleanup.
        await app.recover()
        state = await settled(app, role, clock=False)
        assert released.is_set() and model.requests == 2
        assert state['runs'][0]['status'] == 'aborted' and state['runs'][0]['runtimeAcknowledged']
        assert app._drift_owners == {}
        with app._transaction() as db:
            row = db.execute('SELECT * FROM autonomy_deliveries WHERE role_id=?', (role,)).fetchone()
            assert row['dispatched'] == 1 and row['status'] == 'uncertain'
            run = db.execute('SELECT * FROM autonomy_runs WHERE role_id=?', (role,)).fetchone()
            assert run['runtime_result'] is not None and not run['result_ack_pending'] and run['runtime_acknowledged']
        assert not any(item.content == 'claimed before native busy' for item in sessions.list_messages(f'role:{role}'))
        await app.close()
        monkeypatch.setattr(main, '_proactive', None)
        restored = main._proactive_application()
        await restored.recover()
        assert model.requests == 2 and len(restored.query(role)['runs']) == 1
    try:
        asyncio.run(exercise())
    finally:
        asyncio.run(main._proactive.close())


def test_concurrent_identical_finish_returns_original_receipt_once_and_conflict_cannot_increment_progress(api, monkeypatch):
    client, _, _, _ = api
    role, path, now = prepare(client, monkeypatch)
    app = main._proactive
    class Finish:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            if self.requests == 1:
                name, arguments = 'activate_skill', {'skill_id': 'reading-notes'}
            else:
                assert self.requests == 2
                arguments = {'skill_used': 'reading-notes', 'one_line': '同一真实完成', 'next_action': '后续一项', 'message_result': 'silent'}
                turn = next(iter(app._drift_owners.values()))
                receipts = await asyncio.gather(*(asyncio.to_thread(turn.finish, dict(arguments)) for _ in range(2)))
                assert receipts[0] == receipts[1]
                with pytest.raises(ValueError):
                    turn.finish({**arguments, 'one_line': 'conflicting completion'})
                name = 'finish_drift'
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'finish-cas-{self.requests}', name, arguments),)))
    adapter = Finish()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    try:
        now[0] += 1800
        state = asyncio.run(settled(app, role))
        assert adapter.requests == 2 and state['runs'][0]['status'] == 'finished_silent', state
        progress = next(item for item in client.get(path + '/drift-skills').json()['skills'] if item['id'] == 'reading-notes')['progress']
        assert progress['runCount'] == 1 and progress['oneLine'] == '同一真实完成'
    finally:
        asyncio.run(app.close())
