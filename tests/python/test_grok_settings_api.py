from test_channel_api import channel_api as channel_api

import asyncio
import json
from types import SimpleNamespace

import httpx

from backend.app import main
from backend.app.agent_runtime import AssistantDoneEvent, AssistantMessage, RuntimeManager
from backend.app.agent_runtime.capabilities import CapabilityRegistry
from backend.app.channels.grok import adapter as grok_module
from backend.app.channels.group_tools import MultichannelGroupRepository
from backend.app.model_config import ModelConfigurationStore
from backend.app.task_runtime import TaskRuntime


def test_grok_settings_are_local_only_and_keep_the_webhook_key_private(channel_api):
    role, channels, credentials, client = channel_api
    platforms = client.get('/api/channels/settings').json()['channels']
    assert next(item for item in platforms if item['channel'] == 'grok_bot')['enabled'] is False
    response = client.put(f'/api/roles/{role.id}/channels/grok_bot', json={
        'endpoint': 'https://example.invalid/routine', 'webhookKey': 'test-private-key',
        'maxMessages': 12, 'enabled': False,
    })
    assert response.status_code == 200, response.text
    account = response.json()['account']
    assert account['credentialsConfigured'] and account['maxMessages'] == 12
    assert 'test-private-key' not in response.text
    assert 'token' not in account.get('routine', '').lower()
    saved = channels.channel_config(account['accountId'])
    assert saved['peerId'] and 'test-private-key' not in str(saved)
    assert 'test-private-key' in credentials.values.values()
    changed = client.put(f'/api/roles/{role.id}/channels/grok_bot', json={
        'endpoint': 'https://example.invalid/other-routine', 'enabled': False,
    })
    assert changed.status_code == 200, changed.text
    assert channels.channel_config(account['accountId'])['peerId'] != saved['peerId']


def test_grok_callback_runs_the_role_as_visitor_and_records_real_submission(channel_api, monkeypatch, tmp_path):
    role, channels, _, _ = channel_api
    submitted, model_inputs = [], []
    replied = asyncio.Event()
    tasks = TaskRuntime(tmp_path / 'tasks')

    async def remote(request):
        submitted.append(json.loads(request.content))
        replied.set()
        return httpx.Response(200, json={'task_id': 'only-an-acceptance'})

    adapter_type = grok_module.GrokAdapter
    monkeypatch.setattr(grok_module, 'GrokAdapter', lambda *args, **kwargs:
        adapter_type(*args, **kwargs, http_transport=httpx.MockTransport(remote)))

    class Provider:
        def __init__(self, adapter, role, configuration, *_args, **_kwargs):
            self.adapter, self.role, self.configuration = adapter, role, configuration
        async def stream_response(self, **kwargs):
            model_inputs.append(kwargs)
            yield AssistantDoneEvent(AssistantMessage('角色的真实运行回复'))

    async def capabilities(_role, *, session_key, run_id, **kwargs):
        return CapabilityRegistry(()).resolve(role_id=role.id, session_key=session_key, run_id=run_id)

    async def commit(*_args):
        pass

    monkeypatch.setattr(main, 'task_runtime', tasks)
    monkeypatch.setattr(main, 'runtime_manager', RuntimeManager(channels.sessions))
    monkeypatch.setattr(main, 'role_locks', {})
    monkeypatch.setattr(main, 'channel_lifecycle_locks', {})
    monkeypatch.setattr(main, 'channel_platform_locks', {})
    monkeypatch.setattr(main, 'model_configuration_store', ModelConfigurationStore(tmp_path / 'models.json'))
    monkeypatch.setattr(main, 'group_repository', MultichannelGroupRepository(channels))
    monkeypatch.setattr(main, 'MeidoProvider', Provider)
    monkeypatch.setattr(main, '_runtime_tool_candidates', lambda *args, **kwargs: ())
    monkeypatch.setattr(main, '_runtime_capabilities', capabilities)
    monkeypatch.setattr(main, '_role_memory_context', lambda *_args: '')
    monkeypatch.setattr(main, 'memory_worker', SimpleNamespace(role_deletion_blocked=lambda *_: False, role_lock_for=lambda *_: asyncio.Lock()))
    monkeypatch.setattr(main, 'channel_memory_maintenance', SimpleNamespace(commit_turn=commit))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as api:
            response = await api.post('/api/channels/settings', json={'channel': 'grok_bot', 'enabled': True})
            assert response.status_code == 200, response.text
            response = await api.put(f'/api/roles/{role.id}/channels/grok_bot', json={
                'endpoint': 'https://example.invalid/routine', 'webhookKey': 'private-test', 'enabled': True})
            assert response.status_code == 200, response.text
            account = response.json()['account']
            assert account['connectionStatus'] == 'connected' and not submitted
            adapter = main.channel_adapters[account['accountId']]
            try:
                directory = await api.get(f'/api/roles/{role.id}/channels/accounts/{account["accountId"]}/targets')
                assert len(directory.json()['targets']) == 1
                connection = json.loads(adapter.client_file.read_text())
                async with httpx.AsyncClient(trust_env=False) as callback:
                    response = await callback.post(connection['callback_url'] + 'messages',
                        headers={'Authorization': 'Bearer ' + connection['token']},
                        json={'message_id': 'bot-first', 'text': '你好'})
                    assert response.status_code == 202, response.text
                await asyncio.wait_for(replied.wait(), 8)
                # Stop drains the actual admitted role run and persists its receipt.
                response = await api.post(f'/api/roles/{role.id}/channels/accounts/{account["accountId"]}/state', json={'enabled': False})
                assert response.status_code == 200, response.text
                thread = channels.get_thread_by_chat(role.id, account['accountId'], adapter.target.chat_id, chat_type='private')
                messages = channels.sessions.list_messages(thread.session_key)
                assert messages[0].metadata['senderIsOwner'] is False
                assert messages[-1].content == '角色的真实运行回复'
                assert messages[-1].metadata['receiptKind'] == 'transport_acceptance'
                assert messages[-1].metadata.get('platformMessageId') is None
                assert channels.sessions.runtime_messages(thread.session_key)[-1].content == '角色的真实运行回复'
                assert submitted[0]['reply_to'] == 'bot-first' and len(model_inputs) == 1
            finally:
                await adapter.stop()
    try:
        asyncio.run(scenario())
    finally:
        tasks.close()
