"""Owner target selection crosses HTTP, the real directory and localhost OneBot."""
import asyncio
import json
import threading
from contextlib import contextmanager

import pytest
from websockets.asyncio.server import serve

from backend.app import main
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.channels.transports import NapCatWebSocketTransport
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401
from test_proactive_mcp import source  # noqa: F401
from test_proactive_snapshot import grant_for
from backend.app.mcp_runtime import McpHost
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock

pytestmark = pytest.mark.usefixtures('local_platform_only')


@pytest.fixture
def platform():
    """Only the platform boundary is synthetic; use the production RPC transport."""
    ready, stop = threading.Event(), threading.Event()
    state = {'calls': [], 'sent': [], 'port': None}

    async def peer(socket):
        async for raw in socket:
            request = json.loads(raw)
            state['calls'].append(request)
            action = request['action']
            if action == 'get_friend_list':
                data = [{'user_id': value, 'nickname': f'Friend {value}'} for value in (101, 202, 303)]
            elif action == 'send_private_msg':
                state['sent'].append(request['params'])
                data = {'message_id': 8300 + len(state['sent'])}
            else:
                raise AssertionError(action)
            await socket.send(json.dumps({'status': 'ok', 'retcode': 0, 'echo': request['echo'], 'data': data}))

    async def run():
        async with serve(peer, '127.0.0.1', 0) as server:
            state['port'] = server.sockets[0].getsockname()[1]
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(.01)

    thread = threading.Thread(target=lambda: asyncio.run(run()), name='proactive-target-platform')
    thread.start()
    assert ready.wait(5)

    async def rpc(action, params):
        transport = NapCatWebSocketTransport(f"ws://127.0.0.1:{state['port']}", request_timeout=2)
        async def ingress(_):
            raise AssertionError('fixture has no inbound platform events')
        try:
            await transport.start(ingress)
            await transport.wait_connected()
            return await transport(action, params)
        finally:
            await transport.stop()

    yield state, rpc
    stop.set()
    thread.join(5)
    assert not thread.is_alive()


@contextmanager
def role_target(api, monkeypatch, platform):
    client, roles, sessions, tasks = api
    monkeypatch.setattr(main, '_proactive', None)
    role = client.post('/api/roles', json={'name': '自主目标', 'profile': {'profile': '只连接本地协议'}}).json()['role']
    channels = main.multichannel_store
    account = channels.register_channel_account(role['id'], 'qq', account_id='proactive-target-qq', enabled=True)
    code = channels.create_pairing_code()
    assert channels.consume_pairing_code(code, role_id=role['id'], channel='qq', scope='platform',
        platform_user_id='101', account_id=account.account_id, chat_id='101')
    main.channel_host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=platform[1]))
    try:
        yield client, role['id'], account, channels
    finally:
        if main._proactive is not None:
            asyncio.run(main._proactive.close())
        asyncio.run(main.channel_host.stop(account.account_id))


def test_owner_selects_one_observed_paired_private_target_without_sending(api, monkeypatch, platform):
    with role_target(api, monkeypatch, platform) as (client, role, account, channels):
        path = f'/api/roles/{role}/autonomy'
        accounts = client.get(path + '/target-accounts')
        assert accounts.status_code == 200, accounts.text
        facts = accounts.json()['accounts'][0]
        assert facts['accountId'] == account.account_id and facts['online'] is None
        assert facts['supportedKinds'] == ['known', 'friends', 'groups', 'members']
        assert platform[0]['calls'] == []

        observed = client.post(path + '/target-directory', json={'accountId': account.account_id, 'kind': 'friends', 'limit': 2})
        assert observed.status_code == 200, observed.text
        first = observed.json()
        assert first['coverage'] == 'platform' and first['complete'] is True and first['total'] == 3
        assert first['nextCursor'] and first['observedAt']
        selected = next(item for item in first['targets'] if item['chatId'] == '101')
        assert selected['ownerEligible'] is True and selected['selectionObservationId']
        assert all(not item['ownerEligible'] for item in first['targets'] if item['chatId'] != '101')
        saved = client.put(path, json={'expectedRevision': 0, 'config': {
            'defaultTargetRef': selected['targetRef'], 'allowedTargetRefs': [selected['targetRef']]},
            'targetSelections': [{'accountId': account.account_id, 'targetId': selected['id'],
                'selectionObservationId': selected['selectionObservationId'], 'expectedDirectoryRevision': first['directoryRevision']}]})
        assert saved.status_code == 200, saved.text
        grant = saved.json()['targetGrants'][0]
        assert grant['ref'] == selected['targetRef'] and grant['current'] is True
        assert grant['accountId'] == account.account_id and grant['targetId'] == selected['id']
        assert saved.json()['runs'] == [] and saved.json()['deliveries'] == []
        assert [item['action'] for item in platform[0]['calls']] == ['get_friend_list']
        assert platform[0]['sent'] == []


def save_external(client, path, account, config, *, expected_revision=0):
    observation = client.post(path + '/target-directory', json={'accountId': account.account_id, 'kind': 'friends'})
    assert observation.status_code == 200, observation.text
    payload = observation.json()
    selected = next(item for item in payload['targets'] if item['chatId'] == '101')
    response = client.put(path, json={'expectedRevision': expected_revision, 'config': {**config,
        'defaultTargetRef': selected['targetRef'], 'allowedTargetRefs': [selected['targetRef']]},
        'targetSelections': [{'accountId': account.account_id, 'targetId': selected['id'],
            'selectionObservationId': selected['selectionObservationId'], 'expectedDirectoryRevision': payload['directoryRevision']}]})
    assert response.status_code == 200, response.text
    return response.json(), selected


def test_real_a01_tick_sends_once_to_original_owner_channel_and_projects_receipt(api, monkeypatch, platform, source):
    now = [1781000000.0]
    host = McpHost(source[1])
    monkeypatch.setattr(main, 'mcp_host', host)
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    try:
        with role_target(api, monkeypatch, platform) as (client, role, account, channels):
            saved_server = source[1].save(source[2].model_copy(update={'roleIds': [role]}))
            grant = grant_for(source).model_copy(update={'configRevision': saved_server.revision, 'grantRevision': saved_server.grant_id})
            model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
                'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-only', 'apiKey': 'fixture'}).json()['configuration']
            path = f'/api/roles/{role}/autonomy'
            state, selected = save_external(client, path, account, {'activeDecisionEnabled': True,
                'decisionModelConfigurationId': model['id'], 'consentRevision': 1, 'sourceGrants': [grant.model_dump(mode='json')]})

            class Model:
                requests = 0
                async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
                    self.requests += 1
                    if self.requests == 1:
                        name, arguments = 'get_content_events', {}
                    elif self.requests == 2:
                        item = json.loads(messages[-1].content[0].text)[0]['id']
                        name, arguments = 'message_push', {'message': '原账号自主消息8301', 'mood': '平静', 'thought': '本地协议', 'evidence': [item]}
                    else:
                        assert self.requests == 3
                        name, arguments = 'finish_turn', {'decision': 'reply'}
                    yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'channel-{self.requests}', name, arguments),)))

            model_adapter = Model()
            monkeypatch.setattr(main, 'model_adapter', model_adapter)

            async def run():
                application = main._proactive_application()
                now[0] += 1800
                assert await application.on_clock() == 1
                async with asyncio.timeout(20):
                    while True:
                        await application.recover()
                        current = application.query(role)
                        if current['runs'] and all(item['runtimeAcknowledged'] for item in current['runs']):
                            return current
                        await asyncio.sleep(.01)

            final = asyncio.run(run())
            assert len(platform[0]['sent']) == 1, final
            assert platform[0]['sent'][0]['user_id'] == 101
            assert final['runs'][0]['status'] == 'sent'
            receipt = final['deliveries'][0]
            assert receipt['status'] == 'sent' and receipt['target']['ref'] == selected['targetRef']
            assert receipt['attemptId'] and receipt['platformMessageId'] == '8301' and receipt['messageId']
            assert receipt['projectionPending'] is False
            thread = channels.list_threads(role)[0]
            _, messages, _, _ = channels.list_messages(role, thread.thread_id)
            assert len(messages) == 1 and messages[0].content == '原账号自主消息8301'
            assert messages[0].metadata['autonomyIntentId'] == receipt['intentId']
            assert messages[0].metadata['deliveryAttemptId'] == receipt['attemptId']
            assert main.session_store.list_messages(f'role:{role}') == []
            assert model_adapter.requests == 3
    finally:
        asyncio.run(host.close())
