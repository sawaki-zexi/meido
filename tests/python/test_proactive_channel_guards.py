"""Real autonomous origins keep target, source and human authority separate."""
import asyncio
from contextlib import contextmanager
import json

import pytest

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.mcp_runtime import McpHost
from test_proactive_targets_api import api, shell_api, platform, source, role_target, save_external  # noqa: F401
from test_proactive_snapshot import grant_for

pytestmark = pytest.mark.usefixtures('local_platform_only')


@contextmanager
def decision(api, monkeypatch, platform, source):
    now, host = [1781000000.0], McpHost(source[1])
    monkeypatch.setattr(main, 'mcp_host', host)
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    try:
        with role_target(api, monkeypatch, platform) as (client, role, account, channels):
            server = source[1].save(source[2].model_copy(update={'roleIds': [role], 'timeoutSeconds': 10}))
            grant = grant_for(source).model_copy(update={'configRevision': server.revision, 'grantRevision': server.grant_id})
            model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
                'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-only', 'apiKey': 'fixture'}).json()['configuration']
            path = f'/api/roles/{role}/autonomy'
            state, selected = save_external(client, path, account, {'activeDecisionEnabled': True,
                'decisionModelConfigurationId': model['id'], 'consentRevision': 1, 'sourceGrants': [grant.model_dump(mode='json')]})
            yield {'client': client, 'role': role, 'account': account, 'channels': channels, 'now': now,
                'selected': selected, 'app': main._proactive, 'path': path, 'config': state['config']}
    finally:
        asyncio.run(host.close())


async def run(ctx):
    app = ctx['app']
    ctx['now'][0] += 1800
    assert await app.on_clock() == 1
    async with asyncio.timeout(20):
        while True:
            await app.recover()
            state = app.query(ctx['role'])
            if state['runs'] and all(row['runtimeAcknowledged'] for row in state['runs']):
                await app.recover()
                return app.query(ctx['role'])
            await asyncio.sleep(.01)


def emit(step, name, arguments):
    return ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'guard-{step}', name, arguments),)))


def test_model_retarget_requires_complete_original_observed_pair_before_single_draft(api, monkeypatch, platform, source):
    with decision(api, monkeypatch, platform, source) as ctx:
        invalid = [{'account_id': ctx['account'].account_id}, {'target_id': ctx['selected']['id']},
            {'account_id': '', 'target_id': ''}, {'account_channel': ''}, {'target_kind': ''},
            {'account_id': 'foreign-account', 'target_id': ctx['selected']['id']}]
        class Model:
            requests = 0
            item = None
            async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
                self.requests += 1
                if self.requests == 1:
                    yield emit(self.requests, 'get_content_events', {})
                    return
                if self.requests == 2:
                    self.item = json.loads(messages[-1].content[0].text)[0]['id']
                else:
                    assert messages[-1].is_error is (self.requests <= len(invalid) + 2)
                if self.requests <= len(invalid) + 2:
                    extra = invalid[self.requests - 2] if self.requests <= len(invalid) + 1 else {
                        'account_id': ctx['account'].account_id, 'target_id': ctx['selected']['id']}
                    yield emit(self.requests, 'message_push', {'message': '合法原二元组', 'mood': '', 'thought': '',
                        'evidence': [self.item], **extra})
                else:
                    assert self.requests == len(invalid) + 3
                    yield emit(self.requests, 'finish_turn', {'decision': 'reply'})
        model = Model()
        monkeypatch.setattr(main, 'model_adapter', model)
        state = asyncio.run(run(ctx))
        assert state['deliveries'][0]['status'] == 'sent' and model.requests == 9
        assert len(platform[0]['sent']) == 1 and platform[0]['sent'][0]['user_id'] == 101


def test_account_disable_enable_during_model_response_cannot_restore_old_tick(api, monkeypatch, platform, source):
    with decision(api, monkeypatch, platform, source) as ctx:
        class Model:
            requests = 0
            async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
                self.requests += 1
                if self.requests == 1:
                    yield emit(1, 'get_content_events', {})
                else:
                    assert self.requests == 2
                    ctx['channels'].set_account_state(ctx['account'].account_id, enabled=False)
                    ctx['channels'].set_account_state(ctx['account'].account_id, enabled=True)
                    yield emit(2, 'message_push', {'message': '撤权不能复活', 'mood': '', 'thought': ''})
        model = Model()
        monkeypatch.setattr(main, 'model_adapter', model)
        state = asyncio.run(run(ctx))
        assert state['runs'][0]['status'] == 'aborted'
        assert state['deliveries'] == [] and platform[0]['sent'] == [] and model.requests == 2
        assert state['targetGrants'][0]['current'] is False


@pytest.mark.parametrize('withdraw', ['target', 'source'])
def test_revocation_after_real_platform_acceptance_keeps_original_receipt_without_another_action(api, monkeypatch, platform, source, withdraw):
    from test_proactive_channel_human_priority import owner_http
    with decision(api, monkeypatch, platform, source) as ctx:
        observed = []
        async def rpc(action, params):
            result = await platform[1](action, params)
            if action == 'send_private_msg':
                observed.append(result)
            return result
        persist = ctx['channels'].finish_delivery
        def withdraw_after_original_receipt(*args, **kwargs):
            result = persist(*args, **kwargs)
            if kwargs['status'] == 'sent':
                # This case asserts retention of an already durable receipt.
                # The separate held-response owner test covers acceptance with
                # no published local receipt, which must remain unknown.
                if withdraw == 'target':
                    identity = ctx['channels'].list_identities()[0]['identityId']
                    ctx['channels'].unbind_identity(identity)
                else:
                    response = http_client.put(ctx['path'], json={'expectedRevision': ctx['config']['revision'],
                        'config': {**ctx['config'], 'sourceGrants': [{**ctx['config']['sourceGrants'][0], 'enabled': False}]}})
                    assert response.status_code == 200, response.text
            return result
        monkeypatch.setattr(ctx['channels'], 'finish_delivery', withdraw_after_original_receipt)
        main.channel_host.register(NapCatAdapter(NapCatConfig(ctx['account'].account_id), transport=rpc))
        # The new connection is selected by a new run; the old saved owner
        # grant is unchanged, while every already-frozen adapter would revoke.
        class Model:
            requests = 0
            async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
                self.requests += 1
                if self.requests == 1:
                    yield emit(1, 'get_content_events', {})
                elif self.requests == 2:
                    item = json.loads(messages[-1].content[0].text)[0]['id']
                    yield emit(2, 'message_push', {'message': '已发生的原平台事实', 'mood': '', 'thought': '', 'evidence': [item]})
                else:
                    assert self.requests == 3
                    yield emit(3, 'finish_turn', {'decision': 'reply'})
        model = Model()
        monkeypatch.setattr(main, 'model_adapter', model)
        with owner_http() as (http_client, invoke):
            try:
                state = invoke(run(ctx))
            finally:
                invoke(ctx['app'].close())
        assert len(platform[0]['sent']) == 1 and observed == [{'message_id': 8301}]
        receipt = state['deliveries'][0]
        assert receipt['status'] == 'sent' and receipt['platformMessageId'] == '8301'
        assert receipt['attemptId'] and receipt['messageId'] and not receipt['projectionPending']
        assert model.requests == 3
        assert len(ctx['channels'].list_messages(ctx['role'], ctx['channels'].list_threads(ctx['role'])[0].thread_id)[1]) == 1
