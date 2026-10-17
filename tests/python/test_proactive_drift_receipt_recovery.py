"""Recover original desktop facts without replaying a completed Drift run."""
import asyncio
import hashlib
import json

import pytest

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401
from test_proactive_drift_delivery import prepare, settled

pytestmark = pytest.mark.usefixtures('local_platform_only')


def original_sent(api, monkeypatch):
    client, _, _, _ = api
    role, _, now = prepare(client, monkeypatch, extra_tools=('message_push',))
    app = main._proactive
    class Send:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            if self.requests == 1:
                name, arguments = 'activate_skill', {'skill_id': 'reading-notes'}
            elif self.requests == 2:
                name, arguments = 'message_push', {'message': '唯一原桌面事实', 'mood': '', 'thought': ''}
            else:
                assert self.requests == 3
                name, arguments = 'finish_drift', {'skill_used': 'reading-notes', 'one_line': '已实际发送',
                    'next_action': '只恢复原回执', 'message_result': 'sent'}
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'receipt-{self.requests}', name, arguments),)))
    model = Send()
    monkeypatch.setattr(main, 'model_adapter', model)
    now[0] += 1800
    state = asyncio.run(settled(app, role))
    assert state['runs'][0]['status'] == 'finished_sent' and state['runs'][0]['runtimeAcknowledged']
    with app._transaction() as db:
        claim = dict(db.execute('SELECT * FROM autonomy_deliveries WHERE role_id=?', (role,)).fetchone())
        run = dict(db.execute('SELECT * FROM autonomy_runs WHERE tick_id=?', (claim['tick_id'],)).fetchone())
        finish = dict(db.execute('SELECT * FROM autonomy_drift_turns WHERE tick_id=?', (claim['tick_id'],)).fetchone())
        progress = dict(db.execute('SELECT * FROM autonomy_drift_progress WHERE role_id=?', (role,)).fetchone())
    return app, model, claim, run, finish, progress


def test_lost_receipt_recovers_original_canonical_message_and_close_without_replaying_run(api, monkeypatch):
    _, _, sessions, _ = api
    app, model, original, run, finish, progress = original_sent(api, monkeypatch)
    try:
        with app._transaction() as db:
            db.execute("UPDATE autonomy_deliveries SET status='uncertain',receipt=NULL,message_id=NULL WHERE intent_id=?", (original['intent_id'],))
        asyncio.run(app.recover())
        asyncio.run(app.close())
        with app._transaction() as db:
            restored = dict(db.execute('SELECT * FROM autonomy_deliveries WHERE intent_id=?', (original['intent_id'],)).fetchone())
            assert restored == original
            assert dict(db.execute('SELECT * FROM autonomy_runs WHERE tick_id=?', (original['tick_id'],)).fetchone()) == run
            assert dict(db.execute('SELECT * FROM autonomy_drift_turns WHERE tick_id=?', (original['tick_id'],)).fetchone()) == finish
            assert dict(db.execute('SELECT * FROM autonomy_drift_progress WHERE role_id=?', (original['role_id'],)).fetchone()) == progress
        assert model.requests == 3
        assert len([item for item in sessions.list_messages(f"role:{original['role_id']}") if item.id == original['message_id']]) == 1
    finally:
        # Restore only injected receipt loss on RED, so the original owned
        # resources can close without masking the recovery exception.
        with app._transaction() as db:
            db.execute('UPDATE autonomy_deliveries SET status=?,receipt=?,message_id=?,error=? WHERE intent_id=?',
                (original['status'], original['receipt'], original['message_id'], original['error'], original['intent_id']))
        asyncio.run(app.close())


@pytest.mark.parametrize('reader_raises', [False, True])
def test_bounded_receipt_scan_survives_rebuild_and_reaches_later_original_fact(api, monkeypatch, reader_raises):
    _, _, sessions, _ = api
    app, model, original, run, finish, progress = original_sent(api, monkeypatch)
    asyncio.run(app.close())
    # Only the target is an actual sent/Runtime-ACK run. The 32 older ledger
    # rows are explicitly synthetic historical backlog, with no live tasks.
    def insert(db, table, record):
        names = list(record)
        db.execute('INSERT INTO ' + table + '(' + ','.join(names) + ') VALUES('
            + ','.join('?' for _ in names) + ')', [record[name] for name in names])
    historical = []
    active = app
    try:
        with app._transaction() as db:
            db.execute('DELETE FROM autonomy_deliveries WHERE intent_id=?', (original['intent_id'],))
            db.execute('DELETE FROM autonomy_runs WHERE tick_id=?', (original['tick_id'],))
            for index in range(32):
                tick = f'synthetic-old-receipt-{index:02d}'
                intent = hashlib.sha256(f"drift:{original['role_id']}:{tick}".encode()).hexdigest()
                origin = {**json.loads(run['origin']), 'tick_id': tick}
                insert(db, 'autonomy_runs', {**run, 'tick_id': tick, 'origin': json.dumps(origin), 'task_id': None})
                claim = {**original, 'tick_id': tick, 'intent_id': intent, 'status': 'uncertain', 'message_id': None, 'receipt': None}
                insert(db, 'autonomy_deliveries', claim)
                historical.append(claim)
            insert(db, 'autonomy_runs', run)
            insert(db, 'autonomy_deliveries', {**original, 'status': 'uncertain', 'message_id': None, 'receipt': None})
        lookups = []
        def observe(owner):
            reader = owner.drift_delivery.receipt_for
            async def observed(origin, claim):
                lookups.append(claim['intent_id'])
                if reader_raises and len(lookups) == 1:
                    raise OSError('original receipt reader temporarily failed')
                return await reader(origin, claim)
            monkeypatch.setattr(owner.drift_delivery, 'receipt_for', observed)
        observe(app)
        if reader_raises:
            with pytest.raises(OSError, match='temporarily failed'):
                asyncio.run(app.recover())
        else:
            asyncio.run(app.recover())
        assert len(lookups) == (1 if reader_raises else 32) and original['intent_id'] not in lookups
        monkeypatch.setattr(main, '_proactive', None)
        active = main._proactive_application()
        observe(active)
        prior = len(lookups)
        asyncio.run(active.recover())
        assert len(lookups) - prior <= 32
        with active._transaction() as db:
            recovered = dict(db.execute('SELECT * FROM autonomy_deliveries WHERE intent_id=?', (original['intent_id'],)).fetchone())
            assert recovered == original, 'A new application must resume after the old unknown batch'
            unknown = [dict(row) for row in db.execute("SELECT * FROM autonomy_deliveries WHERE tick_id LIKE 'synthetic-old-receipt-%' ORDER BY tick_id")]
            assert len(unknown) == 32
            assert all(row['status'] == 'uncertain' and row['dispatched'] == 1 and row['receipt'] is None for row in unknown)
            assert dict(db.execute('SELECT * FROM autonomy_runs WHERE tick_id=?', (original['tick_id'],)).fetchone()) == run
            assert dict(db.execute('SELECT * FROM autonomy_drift_turns WHERE tick_id=?', (original['tick_id'],)).fetchone()) == finish
            assert dict(db.execute('SELECT * FROM autonomy_drift_progress WHERE role_id=?', (original['role_id'],)).fetchone()) == progress
        assert model.requests == 3
        assert len([item for item in sessions.list_messages(f"role:{original['role_id']}") if item.id == original['message_id']]) == 1
    finally:
        with active._transaction() as db:
            db.execute("DELETE FROM autonomy_deliveries WHERE tick_id LIKE 'synthetic-old-receipt-%'")
            db.execute("DELETE FROM autonomy_runs WHERE tick_id LIKE 'synthetic-old-receipt-%'")
            db.execute('UPDATE autonomy_deliveries SET status=?,receipt=?,message_id=?,error=? WHERE intent_id=?',
                (original['status'], original['receipt'], original['message_id'], original['error'], original['intent_id']))
        asyncio.run(active.close())
