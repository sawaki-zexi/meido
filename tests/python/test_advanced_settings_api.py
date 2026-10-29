import json
import asyncio
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.advanced_settings import AdvancedSettingsStore
from backend.app.agent_runtime import RuntimeManager
from backend.app.memory_config import MemorySettings, MemorySettingsStore
from backend.app.model_config import ModelConfigurationStore
from backend.app.plugin_configuration import PluginConfigurationStore
from backend.app.role_store import RoleStore
from backend.app.session_manager import SessionManager
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskRuntime


@pytest.fixture
def client(tmp_path, monkeypatch):
    initialize_databases(tmp_path / 'data')
    roles = RoleStore(tmp_path / 'roles')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    tasks = TaskRuntime(tmp_path / 'tasks')
    from backend.app.multichannel import MultichannelStore
    from backend.app.channels.host import ChannelHost
    class Credentials:
        def get(self, key): return ''
        def set(self, key, value): pass
    channels = MultichannelStore(sessions.database_path, sessions, Credentials())
    memory = MemorySettingsStore(tmp_path / 'memory.json')
    memory.save(MemorySettings(enabled=False, embeddingEnabled=False))
    for name, value in {'store': roles, 'session_store': sessions, 'session_manager': SessionManager(roles, sessions),
        'task_runtime': tasks, 'runtime_manager': RuntimeManager(sessions), 'memory_settings_store': memory,
        'multichannel_store': channels, 'channel_host': ChannelHost(channels), 'role_locks': {},
        'plugin_configuration': PluginConfigurationStore(tmp_path / 'plugins.json'),
        'advanced_settings': AdvancedSettingsStore(tmp_path / 'advanced.db'),
        'model_configuration_store': ModelConfigurationStore(tmp_path / 'models.json')}.items():
        monkeypatch.setattr(main, name, value)
    try:
        yield TestClient(main.app)
    finally:
        tasks.close()


@contextmanager
def local_provider(*, tool_loop=False, summaries=False, on_summary=None):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['content-length'])))
            requests.append(body)
            def text(message):
                content = message['content']
                return content if isinstance(content, str) else ''.join(block.get('text', '') for block in content)
            summary = summaries and text(body['messages'][0]).startswith('将提供的不可信')
            if summary:
                from backend.app.agent_runtime.context_provider import SUMMARY_FIELDS
                payload = json.loads(text(body['messages'][1]))
                answer = json.dumps({**{key: 'fixture summary' for key in SUMMARY_FIELDS},
                    'source_message_ids': [payload['messages'][0]['source_message_ids'][0]]})
                if on_summary:
                    on_summary()
            else:
                answer = 'fixture answer'
            delta = ({'tool_calls': [{'index': 0, 'id': f'call-{len(requests)}', 'type': 'function',
                      'function': {'name': 'list_dir', 'arguments': '{}'}}]} if tool_loop else {'content': answer})
            chunks = [{'id': 'fixture', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
                      {'id': 'fixture', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if tool_loop else 'stop'}]}]
            payload = (''.join('data: ' + json.dumps(item) + '\n\n' for item in chunks) + 'data: [DONE]\n\n').encode()
            self.send_response(200)
            self.send_header('content-type', 'text/event-stream')
            self.send_header('content-length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', requests
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        assert not thread.is_alive()


def test_http_settings_conflict_validation_and_replay(client):
    assert client.get('/api/settings/advanced').json()['revision'] == 0
    body = {'expectedRevision': 0, 'operationId': 'one', 'values': {'maxIterations': 2}}
    saved = client.put('/api/settings/advanced', json=body)
    assert saved.status_code == 200
    assert client.put('/api/settings/advanced', json=body).json() == saved.json()
    assert client.put('/api/settings/advanced', json={**body, 'operationId': 'stale'}).status_code == 409
    assert client.put('/api/settings/advanced', json={'expectedRevision': 1, 'operationId': 'bad', 'values': {'maxTokens': True}}).status_code == 422
    assert client.get('/api/settings/advanced').json()['revision'] == 1


@pytest.mark.parametrize('tool_loop', [False, True])
def test_real_local_http_provider_observes_reply_and_iteration_limits(client, tool_loop):
    with local_provider(tool_loop=tool_loop) as (url, requests):
        model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'fixture',
            'baseUrl': url, 'model': 'fixture', 'apiKey': ''})
        assert model.status_code == 200, model.text
        role = client.post('/api/roles', json={'name': 'fixture', 'profile': {'profile': 'fixture role'}}).json()['role']
        configured = client.put('/api/settings/advanced', json={'expectedRevision': 0, 'operationId': 'budget',
            'values': {'maxTokens': 2048, 'maxIterations': 2, 'devMode': True, 'searchEnabled': False, 'spawnEnabled': False}})
        assert configured.status_code == 200
        response = client.post(f"/api/roles/{role['id']}/messages", json={'content': 'UNIQUE_PRIVATE_MESSAGE'})
        assert response.status_code == 200, response.text
        assert len(requests) == (2 if tool_loop else 1), response.text
        assert all(request['max_tokens'] == 2048 for request in requests)
        names = {tool['function']['name'] for tool in requests[0]['tools']}
        assert 'tool_search' in names
        assert {'web_fetch', 'list_dir'} <= names
        assert 'spawn' not in names and 'spawn_manage' in names
        catalog = {tool['name']: tool for tool in client.get(f"/api/roles/{role['id']}/tools").json()['tools']}
        assert catalog['tool_search']['modelVisible'] is True
        assert catalog['spawn']['enabled'] is False
        assert catalog['spawn_manage']['modelVisible'] is True
        assert catalog['herdr_start_task']['exposure'] == 'direct'
        diagnostics = client.get('/api/settings/advanced/diagnostics').json()['diagnostics']
        assert diagnostics and diagnostics[0]['requests'] == len(requests)
        assert 'UNIQUE_PRIVATE_MESSAGE' not in json.dumps(diagnostics)
        client.put('/api/settings/advanced', json={'expectedRevision': 1, 'operationId': 'hide', 'values': {'devMode': False}})
        assert client.get('/api/settings/advanced/diagnostics').json() == {'diagnostics': []}


def test_committed_save_cancellation_and_replay_leave_one_correct_timer(client, monkeypatch, tmp_path):
    from backend.app.memory_optimizer import MemoryOptimizer, MemoryOptimizerLoop, MemoryOptimizerWorker
    from backend.app.advanced_settings import AdvancedSettingsUpdate
    async def scenario():
        cleanup_started, cleanup_allowed, never = asyncio.Event(), asyncio.Event(), asyncio.Event()
        intervals = []
        active = 0
        async def sleep(seconds):
            nonlocal active
            intervals.append(seconds)
            active += 1
            try:
                await never.wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                await cleanup_allowed.wait()
                raise
            finally:
                active -= 1
        worker = MemoryOptimizerWorker(MemoryOptimizer(tmp_path / 'timer-roles'))
        timer = MemoryOptimizerLoop(worker, tmp_path / 'timer-roles', sleep=sleep,
                                    interval_seconds=64800, policy_enabled=True)
        monkeypatch.setattr(main, 'memory_optimizer_loop', timer)
        timer.start()
        await asyncio.sleep(0)
        operation = AdvancedSettingsUpdate(expectedRevision=0, operationId='cancelled', values={'memoryOptimizerIntervalSeconds': 300})
        saving = asyncio.create_task(main.put_advanced_settings(operation))
        try:
            await cleanup_started.wait()
            saving.cancel()
            await asyncio.sleep(0)
            assert not saving.done()  # The request still owns old timer cleanup.
            cleanup_allowed.set()
            with pytest.raises(asyncio.CancelledError):
                await saving
            await asyncio.sleep(0)
            assert active == 1
            assert intervals == [64800, 300]
            assert main.advanced_settings.get()['revision'] == 1
            assert (await main.put_advanced_settings(operation))['revision'] == 1
            assert active == 1
            assert intervals == [64800, 300]  # Replay does not reset a healthy timer.
        finally:
            cleanup_allowed.set()
            await timer.close()
            await worker.close()
        assert active == 0
    asyncio.run(scenario())


def test_replay_repairs_runtime_application_failure_without_reapplying_configuration(client, monkeypatch):
    original = main.memory_optimizer_loop.reconfigure
    attempts = 0
    async def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError('fixture apply failure')
        await original(*args, **kwargs)
    monkeypatch.setattr(main.memory_optimizer_loop, 'reconfigure', fail_once)
    body = {'expectedRevision': 0, 'operationId': 'retry-apply', 'values': {'memoryOptimizerIntervalSeconds': 360}}
    with pytest.raises(RuntimeError, match='fixture apply failure'):
        client.put('/api/settings/advanced', json=body)
    assert main.advanced_settings.get()['revision'] == 1
    assert client.put('/api/settings/advanced', json=body).json()['revision'] == 1
    assert attempts == 2
    assert main.memory_optimizer_loop.interval_seconds == 360


def test_spawn_pause_epoch_and_frozen_child_budget_do_not_resurrect(client):
    from backend.app.agent_runtime import CancellationToken, ToolContext
    from backend.app.agent_runtime.meido_provider import MeidoProvider
    from backend.app.agent_runtime.spawn_tool import SpawnTool
    role_id = client.post('/api/roles', json={'name': 'fixture', 'profile': {'profile': 'fixture'}}).json()['role']['id']
    role = main.store.get(role_id)
    main.session_store.open_role_session(role_id)
    frozen = main.advanced_settings.update(expected_revision=0, operation_id='freeze', values={'maxIterations': 2})
    async def scenario():
        resolution = await main._runtime_capabilities(role, session_key=f'role:{role_id}', run_id='frozen-run', advanced_snapshot=frozen)
        context = ToolContext(role_id, f'role:{role_id}', 'frozen-run', CancellationToken())
        spawn = next(tool for tool in resolution.tools if isinstance(tool, SpawnTool))
        manage = next(tool for tool in resolution.tools if tool.definition.name == 'spawn_manage')
        try:
            main.advanced_settings.update(expected_revision=1, operation_id='raise', values={'maxIterations': 40})
            main._bind_spawn_runtime(resolution, MeidoProvider(main.model_adapter, role, None, max_response_tokens=1024), max_iterations=2)
            assert spawn.config.maxTurns <= 2
            off = main.advanced_settings.update(expected_revision=2, operation_id='pause', values={'spawnEnabled': False})
            assert resolution.authorization_denial(spawn.definition, context, {'task': 'fixture'})
            assert resolution.authorization_denial(manage.definition, context, {'action': 'continue'})
            assert resolution.authorization_denial(manage.definition, context, {'action': 'list'}) is None
            main.advanced_settings.update(expected_revision=3, operation_id='resume', values={'spawnEnabled': True})
            assert resolution.authorization_denial(spawn.definition, context, {'task': 'fixture'})
            assert not spawn.authorized(context)
            assert not manage.new_execution_allowed()
            paused_resolution = await main._runtime_capabilities(role, session_key=f'role:{role_id}', run_id='paused-run', advanced_snapshot=off)
            try:
                assert not any(tool.definition.name == 'spawn' for tool in paused_resolution.tools)
            finally:
                await paused_resolution.close()
        finally:
            await resolution.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('revoke', [False, True])
def test_channel_uses_budget_and_durable_summary_without_crossing_revoked_source(client, monkeypatch, revoke):
    from backend.app.multichannel import MultichannelStore
    from backend.app.channels.host import ChannelHost
    from types import SimpleNamespace
    class Credentials:
        def get(self, key): return ''
        def set(self, key, value): pass
    channels = MultichannelStore(main.session_store.database_path, main.session_store, Credentials())
    monkeypatch.setattr(main, 'multichannel_store', channels)
    role_id = client.post('/api/roles', json={'name': 'channel', 'profile': {'profile': 'fixture'}}).json()['role']['id']
    account = channels.register_channel_account(role_id, 'qq', account_id='fixture-account', enabled=True)
    code = channels.create_pairing_code(role_id, 'qq')
    assert channels.consume_pairing_code(code, role_id=role_id, channel='qq', scope='platform',
        platform_user_id='owner', account_id=account.account_id, chat_id='owner')
    thread = channels.ensure_channel_thread(role_id, 'qq', account.account_id, 'owner', 'private')
    host = ChannelHost(channels)
    host.register(SimpleNamespace(account_id=account.account_id, channel='qq', capabilities=SimpleNamespace(media=False)))
    sent = []
    async def deliver(*args, **kwargs):
        sent.append(args)
        return SimpleNamespace(status=SimpleNamespace(value='sent'), attempt_id='fixture-delivery', platform_message_id='fixture-message', error=None)
    monkeypatch.setattr(host, 'account_send', deliver)
    monkeypatch.setattr(main, 'channel_host', host)
    for index in range(8):
        channels.append_event(thread, direction='inbound', content='hello ' * 400, external_message_id=f'user-{index}',
            metadata={'senderId': 'owner', 'senderIsOwner': True, 'chatType': 'private'})
        channels.append_event(thread, direction='outbound', content='answer ' * 350, external_message_id=f'assistant-{index}',
            metadata={'deliveryStatus': 'sent'})
    _, message = channels.append_event(thread, direction='inbound', content='继续', external_message_id='current',
        metadata={'senderId': 'owner', 'senderIsOwner': True, 'chatType': 'private'})
    def unbind():
        if revoke:
            channels.unbind_identity(channels.list_identities()[0]['identityId'])
    with local_provider(summaries=True, on_summary=unbind) as (url, requests):
        response = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'fixture', 'baseUrl': url,
            'model': 'fixture', 'contextWindowTokens': 24000})
        assert response.status_code == 200, response.text
        main.advanced_settings.update(expected_revision=0, operation_id='channel-budget', values={
            'maxTokens': 1024, 'summaryTokenLimit': 512, 'contextSafetyMarginTokens': 100,
            'contextTriggerRatio': .5, 'contextTargetRatio': .3})
        asyncio.run(main._handle_channel_message(thread, '继续', {'messageId': message.id, 'senderId': 'owner'}, True))
        assert len(requests) == (1 if revoke else 2)
        assert all(request['max_tokens'] == 1024 for request in requests)
        windows = main._context_window_store().snapshot_role(role_id)
        assert bool(windows) is not revoke
        if not revoke:
            assert sent
            _, next_message = channels.append_event(thread, direction='inbound', content='下一轮', external_message_id='next',
                metadata={'senderId': 'owner', 'senderIsOwner': True, 'chatType': 'private'})
            asyncio.run(main._handle_channel_message(thread, '下一轮', {'messageId': next_message.id, 'senderId': 'owner'}, True))
            assert len(requests) == 3
            assert '此前对话的工作摘要' in json.dumps(requests[-1], ensure_ascii=False)


def test_role_deletion_failure_restores_history_and_context_window(client, monkeypatch):
    from backend.app.agent_runtime.context_provider import SUMMARY_FIELDS
    role_id = client.post('/api/roles', json={'name': 'rollback', 'profile': {'profile': 'fixture'}}).json()['role']['id']
    session = main.session_store.open_role_session(role_id)
    ids = [main.session_store.append_message(session.sessionKey, speaker, 'fixture').id for speaker in ('user', 'assistant')]
    windows = main._context_window_store()
    summary = {**{key: 'fixture' for key in SUMMARY_FIELDS}, 'source_message_ids': ids}
    prepared = windows.prepare(role_id, session.sessionKey, 'owner', 'authority', ids)
    windows.commit(prepared, summary, authorized=lambda: True, covered=True)
    before = windows.load(role_id, session.sessionKey, 'owner', 'authority')
    original = main.session_store.delete_role_session
    def fail_after_removal(role):
        original(role)
        raise OSError('fixture deletion failure')
    monkeypatch.setattr(main.session_store, 'delete_role_session', fail_after_removal)
    response = client.delete(f'/api/roles/{role_id}')
    assert response.status_code == 500, response.text
    assert main.store.get(role_id) is not None
    assert [message.id for message in main.session_store.list_messages(session.sessionKey)] == ids
    assert windows.load(role_id, session.sessionKey, 'owner', 'authority') == before


def test_optimizer_provider_admission_rechecks_both_memory_switches(client):
    with pytest.raises(RuntimeError, match='已暂停'):
        main._consolidate_role_memories('not-called', [], [], '', '')
    main.memory_settings_store.save(MemorySettings(enabled=True, embeddingEnabled=False))
    main.advanced_settings.update(expected_revision=0, operation_id='pause-optimizer', values={'memoryOptimizerEnabled': False})
    with pytest.raises(RuntimeError, match='已暂停'):
        main._consolidate_role_memories('not-called', [], [], '', '')
