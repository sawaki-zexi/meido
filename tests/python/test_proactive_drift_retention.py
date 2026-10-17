"""Expire Drift output while preserving its durable recovery obligations."""
import asyncio
import json

import pytest

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
from backend.app.proactive_runtime import retention
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401
from test_proactive_drift_delivery import prepare, settled

pytestmark = pytest.mark.usefixtures('local_platform_only')
DAY = 86400


def silent_model(monkeypatch):
    class Silent:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            if self.requests == 1:
                name, args = 'activate_skill', {'skill_id': 'reading-notes'}
            else:
                assert self.requests == 2
                name, args = 'finish_drift', {'skill_used': 'reading-notes', 'one_line': '真实静默进度',
                    'next_action': '下一步保留在技能状态', 'message_result': 'silent'}
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'retention-{self.requests}', name, args),)))
    model = Silent()
    monkeypatch.setattr(main, 'model_adapter', model)
    return model


@pytest.mark.parametrize('already_compacted', [False, True])
def test_completed_tool_payload_expires_after_seven_days_and_finish_details_after_ninety(api, monkeypatch, already_compacted):
    client, _, _, _ = api
    role, _, now = prepare(client, monkeypatch)
    app = main._proactive
    model = silent_model(monkeypatch)
    try:
        now[0] += 1800
        state = asyncio.run(settled(app, role))
        assert state['runs'][0]['status'] == 'finished_silent' and state['runs'][0]['runtimeAcknowledged']
        tick = state['runs'][0]['id']
        with app._transaction() as db:
            calls = [dict(row) for row in db.execute('SELECT * FROM autonomy_drift_tool_calls WHERE tick_id=? ORDER BY call_id', (tick,))]
            activation = next(row for row in calls if row['tool_name'] == 'activate_skill')
            assert json.loads(activation['result'])['instructions']
            progress = dict(db.execute('SELECT * FROM autonomy_drift_progress WHERE role_id=?', (role,)).fetchone())
            assert db.execute('SELECT finish FROM autonomy_drift_turns WHERE tick_id=?', (tick,)).fetchone()[0]
            before = retention.usage(db, role)['globalBytes']
            retention.prune(db, now[0] + 8 * DAY)
            assert db.execute('SELECT COUNT(*) FROM autonomy_transcripts WHERE tick_id=?', (tick,)).fetchone()[0] == 0
            expired = [dict(row) for row in db.execute('SELECT * FROM autonomy_drift_tool_calls WHERE tick_id=? ORDER BY call_id', (tick,))]
            assert expired == [{**row, 'result': None} for row in calls]
            assert db.execute('SELECT finish FROM autonomy_drift_turns WHERE tick_id=?', (tick,)).fetchone()[0]
            assert retention.usage(db, role)['globalBytes'] < before
            if already_compacted:
                # Upgrade fixture: the previous implementation could set this
                # marker while leaving the full Drift finish columns behind.
                db.execute('UPDATE autonomy_storage_reservations SET compacted=1 WHERE operation_id=?', (tick,))
            assert retention.prune(db, now[0] + 91 * DAY)['compactedRuns'] == (0 if already_compacted else 1)
            turn = db.execute('SELECT finish,receipt FROM autonomy_drift_turns WHERE tick_id=?', (tick,)).fetchone()
            assert tuple(turn) == (None, None)
            assert db.execute('SELECT compacted FROM autonomy_storage_reservations WHERE operation_id=?', (tick,)).fetchone()[0] == 1
            assert dict(db.execute('SELECT * FROM autonomy_drift_progress WHERE role_id=?', (role,)).fetchone()) == progress
            assert db.execute('SELECT COUNT(*) FROM autonomy_runs WHERE tick_id=?', (tick,)).fetchone()[0] == 1
        asyncio.run(app.recover())
        assert model.requests == 2
    finally:
        asyncio.run(app.close())


def test_unacknowledged_real_runtime_keeps_tool_results_until_copy_and_ack(api, monkeypatch):
    client, _, _, _ = api
    role, _, now = prepare(client, monkeypatch)
    app = main._proactive
    model = silent_model(monkeypatch)
    async def exercise():
        now[0] += 1800
        await app.on_clock()
        async with asyncio.timeout(20):
            while True:
                state = app.query(role)
                if state['runs']:
                    tick = state['runs'][0]['id']
                    executor = app._executors.get(tick)
                    if executor is not None and state['runs'][0]['taskId']:
                        snapshot = app.tasks.query(state['runs'][0]['taskId'], executor.origin.task_origin())
                        if snapshot.ended_at is not None:
                            break
                await asyncio.sleep(.01)
        assert not state['runs'][0]['runtimeAcknowledged']
        with app._transaction() as db:
            result = db.execute("SELECT result FROM autonomy_drift_tool_calls WHERE tick_id=? AND tool_name='activate_skill'", (tick,)).fetchone()[0]
            assert json.loads(result)['instructions']
            assert retention.prune(db, now[0] + 91 * DAY)['protectedRuns'] == 1
            assert db.execute("SELECT result FROM autonomy_drift_tool_calls WHERE tick_id=? AND tool_name='activate_skill'", (tick,)).fetchone()[0] == result
            assert db.execute('SELECT compacted FROM autonomy_storage_reservations WHERE operation_id=?', (tick,)).fetchone()[0] == 0
        await app.recover()
        assert app.query(role)['runs'][0]['runtimeAcknowledged']
        with app._transaction() as db:
            retention.prune(db, now[0] + 8 * DAY)
            assert db.execute("SELECT result FROM autonomy_drift_tool_calls WHERE tick_id=? AND tool_name='activate_skill'", (tick,)).fetchone()[0] is None
        assert model.requests == 2
    try:
        asyncio.run(exercise())
    finally:
        asyncio.run(app.close())


def test_pending_original_projection_keeps_finish_and_progress_until_recovered(api, monkeypatch):
    client, roles, _, _ = api
    role, _, now = prepare(client, monkeypatch)
    app = main._proactive
    blocker = roles.workspace_path(role) / 'drift/state'
    blocker.write_text('original projection is blocked', encoding='utf-8')
    model = silent_model(monkeypatch)
    try:
        now[0] += 1800
        state = asyncio.run(settled(app, role))
        tick = state['runs'][0]['id']
        assert state['runs'][0]['runtimeAcknowledged']
        with app._transaction() as db:
            progress = dict(db.execute('SELECT * FROM autonomy_drift_progress WHERE role_id=?', (role,)).fetchone())
            assert progress['projection_pending'] == 1
            finish = tuple(db.execute('SELECT finish,receipt FROM autonomy_drift_turns WHERE tick_id=?', (tick,)).fetchone())
            result = retention.prune(db, now[0] + 91 * DAY)
            assert result['protectedRuns'] == 1 and result['compactedRuns'] == 0
            assert tuple(db.execute('SELECT finish,receipt FROM autonomy_drift_turns WHERE tick_id=?', (tick,)).fetchone()) == finish
            assert dict(db.execute('SELECT * FROM autonomy_drift_progress WHERE role_id=?', (role,)).fetchone()) == progress
        blocker.unlink()
        asyncio.run(app.recover())
        with app._transaction() as db:
            assert dict(db.execute('SELECT * FROM autonomy_drift_progress WHERE role_id=?', (role,)).fetchone()) == {**progress, 'projection_pending': 0}
            assert retention.prune(db, now[0] + 91 * DAY)['compactedRuns'] == 1
        assert model.requests == 2
    finally:
        asyncio.run(app.close())
