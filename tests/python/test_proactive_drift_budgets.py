"""Original tick budget and persistent Drift cooldown/day slots are never reset."""
import asyncio
import json
import time

import pytest

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401
from test_proactive_drift_delivery import prepare, settled

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_multi_call_response_consumes_one_step_but_executes_neither_write_nor_send(api, monkeypatch):
    client, roles, sessions, _ = api
    role, _, now = prepare(client, monkeypatch, extra_tools=('message_push',))
    class Multiple:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            if self.requests == 1:
                calls = (ModelToolCallBlock('multi-activate', 'activate_skill', {'skill_id': 'reading-notes'}),)
            elif self.requests == 2:
                calls = (ModelToolCallBlock('multi-write', 'write_file', {'path': 'work/must-not-exist.txt', 'content': 'not executed'}),
                    ModelToolCallBlock('multi-send', 'message_push', {'message': 'must not be delivered', 'mood': '', 'thought': ''}))
            else:
                assert self.requests == 3 and messages[-1].is_error and messages[-2].is_error
                calls = (ModelToolCallBlock('multi-finish', 'finish_drift', {'skill_used': 'reading-notes',
                    'one_line': '拒绝同一步的多个动作', 'next_action': '按单步协议继续', 'message_result': 'silent'}),)
            yield ModelStreamDone(ModelAssistantMessage(content=calls))
    model = Multiple()
    monkeypatch.setattr(main, 'model_adapter', model)
    try:
        now[0] += 1800
        state = asyncio.run(settled(main._proactive, role))
        assert model.requests == 3 and state['runs'][0]['status'] == 'finished_silent', state
        assert not (roles.workspace_path(role) / 'drift/work/must-not-exist.txt').exists()
        assert not any(item.content == 'must not be delivered' for item in sessions.list_messages(f'role:{role}'))
        with main._proactive._transaction() as db:
            assert db.execute('SELECT COUNT(*) FROM autonomy_deliveries').fetchone()[0] == 0
            assert tuple(db.execute('SELECT steps,tokens FROM autonomy_drift_turns').fetchone()) == (3, 1536)
    finally:
        asyncio.run(main._proactive.close())


def test_missing_finish_consumes_original_token_reservation_and_cooldown_without_fake_progress(api, monkeypatch):
    client, _, _, _ = api
    role, path, now = prepare(client, monkeypatch)
    class NeverFinish:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            assert max_tokens == 512
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'budget-{self.requests}', 'activate_skill', {'skill_id': 'reading-notes'}),)))
    adapter = NeverFinish()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    app = main._proactive
    try:
        now[0] += 1800
        first = asyncio.run(settled(app, role))
        assert adapter.requests == 16 and first['runs'][0]['status'] == 'aborted'
        assert first['runs'][0]['mode'] == 'drift' and first['runs'][0]['timeoutSeconds'] == 600
        skill = next(item for item in client.get(path + '/drift-skills').json()['skills'] if item['id'] == 'reading-notes')
        assert skill['progress'] is None
        with app._transaction() as db:
            budget = db.execute('SELECT steps,tokens FROM autonomy_drift_turns').fetchone()
            assert tuple(budget) == (16, 8192)
            started = db.execute('SELECT last_drift_at FROM autonomy_roles WHERE role_id=?', (role,)).fetchone()[0]
        now[0] += 1800
        second = asyncio.run(settled(app, role))
        assert len(second['runs']) == 2 and adapter.requests == 16
        with app._transaction() as db:
            row = db.execute('SELECT SUM(drifts),SUM(decisions) FROM autonomy_budgets WHERE role_id=?', (role,)).fetchone()
            assert tuple(row) == (1, 2)
            assert db.execute('SELECT last_drift_at FROM autonomy_roles WHERE role_id=?', (role,)).fetchone()[0] == started
        asyncio.run(app.close())
        monkeypatch.setattr(main, '_proactive', None)
        restored = main._proactive_application()
        asyncio.run(restored.recover())
        assert adapter.requests == 16 and len(restored.query(role)['runs']) == 2
    finally:
        asyncio.run(main._proactive.close())


def test_four_persistent_drift_slots_are_part_of_original_twenty_four_daily_tick_limit(api, monkeypatch):
    client, _, _, _ = api
    midnight = (int(time.time() // 86400) + 1) * 86400
    role, _, now = prepare(client, monkeypatch, clock_start=midnight + 1200)
    class Finish:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            if len(messages) == 2:
                name, arguments = 'activate_skill', {'skill_id': 'reading-notes'}
            else:
                name, arguments = 'finish_drift', {'skill_used': 'reading-notes', 'one_line': '当天真实有限进度',
                    'next_action': '等待下一次合格准入', 'message_result': 'silent'}
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'day-{self.requests}', name, arguments),)))
    adapter = Finish()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    app = main._proactive
    try:
        for index in range(24):
            now[0] += 1800
            state = asyncio.run(settled(app, role))
            assert len(state['runs']) == index + 1
        assert adapter.requests == 8
        assert sum(run['status'] == 'finished_silent' for run in state['runs']) == 4
        with app._transaction() as db:
            assert tuple(db.execute('SELECT SUM(drifts),SUM(decisions) FROM autonomy_budgets WHERE role_id=?', (role,)).fetchone()) == (4, 24)
        now[0] += 1800
        assert asyncio.run(app.on_clock()) == 0
        assert len(app.query(role)['runs']) == 24 and adapter.requests == 8
        assert app.query(role)['admissionError']
    finally:
        asyncio.run(app.close())
