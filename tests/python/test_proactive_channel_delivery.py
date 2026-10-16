"""Drift and owner reconciliation retain the original real channel attempt."""
import asyncio
import json
import sqlite3
import uuid

import pytest

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from test_proactive_drift_delivery import prepare, settled
from test_proactive_targets_api import api, shell_api, platform, save_external  # noqa: F401

pytestmark = pytest.mark.usefixtures('local_platform_only')


def channel_for(channels, role, rpc):
    account = channels.register_channel_account(role, 'qq', account_id='drift-original-qq', enabled=True)
    code = channels.create_pairing_code()
    assert channels.consume_pairing_code(code, role_id=role, channel='qq', scope='platform',
        platform_user_id='101', account_id=account.account_id, chat_id='101')
    main.channel_host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=rpc))
    return account


class DriftSend:
    def __init__(self, selected, account, *, outcome='sent', stale_call=False):
        self.selected, self.account, self.outcome, self.stale_call = selected, account, outcome, stale_call
        self.requests = 0

    async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
        self.requests += 1
        if self.requests == 1:
            name, arguments = 'activate_skill', {'skill_id': 'reading-notes'}
        elif self.requests == 2:
            name, arguments = 'message_push', {'message': '长期技能原账号消息', 'mood': '平静', 'thought': '一次机会',
                'account_id': self.account.account_id, 'target_id': self.selected['id']}
        elif self.requests == 3 and self.stale_call:
            assert {item['function']['name'] for item in tools} == {'write_file', 'edit_file', 'finish_drift'}
            assert json.loads(messages[-1].content[0].text)['status'] == 'sent'
            name, arguments = 'message_push', {'message': '不能重复投递', 'mood': '', 'thought': ''}
        else:
            assert self.requests == (4 if self.stale_call else 3)
            if self.stale_call:
                assert messages[-1].is_error
            name, arguments = 'finish_drift', {'skill_used': 'reading-notes', 'one_line': '本次真实投递状态',
                'next_action': '下次继续原工作', 'message_result': self.outcome}
        yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'drift-channel-{self.requests}', name, arguments),)))


def test_trusted_drift_uses_original_channel_claim_and_shrinks_actual_dispatch(api, monkeypatch, platform):
    client, _, sessions, _ = api
    role, path, now = prepare(client, monkeypatch, extra_tools=('message_push',))
    account = channel_for(main.multichannel_store, role, platform[1])
    config = client.get(path).json()['config']
    _, selected = save_external(client, path, account, config, expected_revision=config['revision'])
    model = DriftSend(selected, account, stale_call=True)
    monkeypatch.setattr(main, 'model_adapter', model)
    try:
        now[0] += 1800
        final = asyncio.run(settled(main._proactive, role))
        assert final['runs'][0]['status'] == 'finished_sent', final
        receipt = final['deliveries'][0]
        assert receipt['status'] == 'sent' and receipt['attemptId'] and receipt['platformMessageId'] == '8301'
        assert receipt['target']['ref'] == selected['targetRef']
        assert len(platform[0]['sent']) == 1 and model.requests == 4
        thread = main.multichannel_store.list_threads(role)[0]
        _, messages, _, _ = main.multichannel_store.list_messages(role, thread.thread_id)
        assert len(messages) == 1 and messages[0].metadata['internalEvent'] == 'drift'
        assert messages[0].metadata['autonomyIntentId'] == receipt['intentId']
        assert not any(message.content == '长期技能原账号消息' for message in sessions.list_messages(f'role:{role}'))
    finally:
        asyncio.run(main._proactive.close())
        asyncio.run(main.channel_host.stop(account.account_id))


def test_unknown_channel_report_is_cas_idempotent_and_late_real_receipt_prevails(api, monkeypatch, platform):
    client, _, sessions, tasks = api
    role, path, now = prepare(client, monkeypatch, extra_tools=('message_push',))
    actual_responses = []
    async def observed_rpc(action, params):
        result = await platform[1](action, params)
        if action == 'send_private_msg':
            actual_responses.append(result)
        return result
    account = channel_for(main.multichannel_store, role, observed_rpc)
    config = client.get(path).json()['config']
    _, selected = save_external(client, path, account, config, expected_revision=config['revision'])
    model = DriftSend(selected, account, outcome='uncertain')
    monkeypatch.setattr(main, 'model_adapter', model)
    with sqlite3.connect(sessions.database_path) as db:
        db.execute('''CREATE TRIGGER lose_original_platform_receipt BEFORE UPDATE ON channel_delivery_attempts
            WHEN NEW.status='sent' BEGIN SELECT RAISE(ABORT,'real original receipt write failed'); END''')
    try:
        now[0] += 1800
        initial = asyncio.run(settled(main._proactive, role))
        assert initial['runs'][0]['status'] == 'finish_uncertain', initial
        original_run = initial['runs'][0]
        delivery = initial['deliveries'][0]
        assert delivery['status'] in {'pending', 'uncertain'} and delivery['projectionPending']
        assert len(platform[0]['sent']) == 1 and actual_responses[0]['message_id'] == 8301
        assert model.requests == 3 and initial['acknowledgements'] == []
        with main._proactive._transaction() as db:
            claim_before = dict(db.execute('SELECT * FROM autonomy_deliveries WHERE intent_id=?', (delivery['intentId'],)).fetchone())
            keys_before = [tuple(row) for row in db.execute('SELECT * FROM autonomy_delivery_keys WHERE intent_id=? ORDER BY key', (delivery['intentId'],))]
            progress_before = tuple(db.execute('SELECT * FROM autonomy_drift_progress WHERE role_id=?', (role,)).fetchone())

        request = {'requestId': str(uuid.uuid4()), 'expectedResolutionRevision': 0, 'expectedStatus': delivery['status'],
            'decision': 'reported_sent', 'allowFuture': True, 'note': '主人报告收到，平台回执仍待核对'}
        endpoint = path + '/deliveries/' + delivery['intentId'] + '/reconciliation'
        first = client.post(endpoint, json=request)
        assert first.status_code == 200, first.text
        reported = first.json()['deliveries'][0]
        assert reported['status'] == delivery['status'] and reported['resolutionRevision'] == 1
        assert reported['resolution']['decision'] == 'reported_sent' and reported['resolution']['allowFuture'] is True
        assert reported['messageId'] is None and first.json()['acknowledgements'] == []
        assert client.post(endpoint, json=request).json()['deliveries'][0] == reported
        assert client.post(endpoint, json={**request, 'note': '同请求不能改正文'}).status_code == 409
        assert client.post(endpoint, json={**request, 'requestId': str(uuid.uuid4())}).status_code == 409
        assert client.post(endpoint, json={**request, 'requestId': str(uuid.uuid4()), 'decision': 'keep_pending'}).status_code == 422
        with main._proactive._transaction() as db:
            assert dict(db.execute('SELECT * FROM autonomy_deliveries WHERE intent_id=?', (delivery['intentId'],)).fetchone()) == claim_before
            assert [tuple(row) for row in db.execute('SELECT * FROM autonomy_delivery_keys WHERE intent_id=? ORDER BY key', (delivery['intentId'],))] == keys_before
            assert tuple(db.execute('SELECT * FROM autonomy_drift_progress WHERE role_id=?', (role,)).fetchone()) == progress_before

        attempt = main.multichannel_store.list_deliveries(role)[0]
        with sqlite3.connect(sessions.database_path) as db:
            db.execute('DROP TRIGGER lose_original_platform_receipt')
        # This is the genuine first transport response, copied through the
        # existing receipt persistence interface; no second platform request.
        main.multichannel_store.finish_delivery(attempt['attemptId'], status='sent',
            platform_message_id=str(actual_responses[0]['message_id']))
        asyncio.run(main._proactive.recover())
        final = client.get(path).json()
        repaired = final['deliveries'][0]
        assert repaired['status'] == 'sent' and repaired['platformMessageId'] == '8301'
        assert repaired['resolution'] == reported['resolution'] and not repaired['projectionPending']
        assert final['runs'][0]['status'] == original_run['status'] and final['runs'][0]['runtimeAcknowledged']
        assert len(platform[0]['sent']) == 1 and model.requests == 3
        assert client.post(endpoint, json={**request, 'requestId': str(uuid.uuid4()), 'expectedResolutionRevision': 1}).status_code == 409
        asyncio.run(main._proactive.recover())
        thread = main.multichannel_store.list_threads(role)[0]
        assert len(main.multichannel_store.list_messages(role, thread.thread_id)[1]) == 1
    finally:
        with sqlite3.connect(sessions.database_path) as db:
            db.execute('DROP TRIGGER IF EXISTS lose_original_platform_receipt')
        asyncio.run(main._proactive.close())
        asyncio.run(main.channel_host.stop(account.account_id))
