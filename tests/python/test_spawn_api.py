import pytest
from backend.app.agent_runtime.types import ToolResultMessage
from backend.app.model_adapter import ModelResponseDone, ModelTextDelta, ModelToolCall
from test_background_shell_api import MemoryCredentials
from test_background_shell_api import api as shell_api  # noqa: F401


@pytest.fixture
def api(shell_api, tmp_path, monkeypatch):  # noqa: F811
    from backend.app import main
    from backend.app.channels.host import ChannelHost
    from backend.app.memory_config import MemorySettingsStore
    from backend.app.multichannel import MultichannelStore
    settings = MemorySettingsStore(tmp_path / 'memory-settings.json')
    settings.update(enabled=False)
    channels = MultichannelStore(shell_api[2].database_path, shell_api[2], MemoryCredentials())
    monkeypatch.setattr(main, 'memory_settings_store', settings)
    monkeypatch.setattr(main, 'multichannel_store', channels)
    monkeypatch.setattr(main, 'channel_host', ChannelHost(channels))
    monkeypatch.setattr(main, '_spawn_completion_worker', None)
    yield shell_api


@pytest.mark.parametrize('change', ['disabled', 'before_spawn', 'inside_child'])
def test_default_parent_shell_does_not_grant_or_restore_restricted_child_shell(api, monkeypatch, change):
    import sys
    from backend.app import main
    from backend.app.models import RoleUpdateInput
    client, roles, sessions, tasks = api
    child_schemas = []

    def revoke():
        roles.update(role['id'], RoleUpdateInput(name=role['name'], profile=role['profile'],
            agentConfig={'shell': {'enabled': False, 'allowedCommands': []}}))

    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            child = 'independent delegated task' in str(messages[0])
            results = [item for item in messages if item['role'] == 'tool']
            if child:
                child_schemas.append({item['function']['name'] for item in tools})
                if not results:
                    if change == 'inside_child':
                        revoke()
                    yield ModelToolCall('restricted-shell', 'shell', {'command': f'"{sys.executable}" -c "from pathlib import Path; Path(\'child-marker.txt\').write_text(\'should-not-execute\')"'})
                    yield ModelResponseDone("tool_use")
                else:
                    yield ModelTextDelta('child-stopped')
            elif not results:
                if change == 'before_spawn':
                    revoke()
                yield ModelToolCall('restricted-spawn', 'spawn', {'task': 'controlled restricted task', 'profile': 'scripting'})
                yield ModelResponseDone("tool_use")
            else:
                yield ModelTextDelta('parent-finished')

    monkeypatch.setattr(main, 'model_adapter', Adapter())
    role = client.post('/api/roles', json={'name': '受限子任务', 'profile': {'profile': '设定'},
        'agentConfig': {'shell': {'enabled': change != 'disabled', 'allowedCommands': [sys.executable]}}}).json()['role']
    response = client.post(f"/api/roles/{role['id']}/messages", json={'content': '运行受限任务'})
    assert 'assistant_completed' in response.text
    assert not list(tasks.root.rglob('child-marker.txt'))
    if change != 'inside_child':
        assert all('shell' not in names for names in child_schemas)


def test_http_sync_child_reads_real_file_while_parent_owns_role_lock(api, monkeypatch):
    from backend.app import main

    client, roles, sessions, tasks = api
    class Adapter:
        requests = []
        async def stream_structured_messages(self, messages, configuration, tools):
            self.requests.append((messages, tools))
            child = '独立子任务' in str(messages[0])
            tool_results = [item for item in messages if item['role'] == 'tool']
            if child:
                assert 'parent-private-marker' not in str(messages)
                assert {item['function']['name'] for item in tools} == {'read_file', 'list_dir', 'web_search', 'web_fetch'}
                if not tool_results:
                    yield ModelToolCall('child-read', 'read_file', {'path': 'evidence.txt'})
                    yield ModelResponseDone("tool_use")
                else:
                    assert 'tracer-known-731' in str(tool_results[-1])
                    yield ModelTextDelta('child-confirmed-731')
            elif not tool_results:
                yield ModelToolCall('delegate', 'spawn', {'task': 'Read evidence.txt and return its marker.'})
                yield ModelResponseDone("tool_use")
            else:
                assert 'child-confirmed-731' in str(tool_results[-1])
                yield ModelTextDelta('parent-completed')
    adapter = Adapter()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    role = client.post('/api/roles', json={'name': '委托角色', 'profile': {'profile': '测试角色'}, 'agentConfig': {
        'spawn': {'enabled': True}, 'enabledTools': ['read_file'],
    }}).json()['role']
    workspace = roles.workspace_path(role['id'])
    (workspace / 'evidence.txt').write_text('tracer-known-731', encoding='utf-8')
    response = client.post(f"/api/roles/{role['id']}/messages", json={'content': 'parent-private-marker'})
    assert 'assistant_completed' in response.text
    results = [item for item in sessions.runtime_messages(f"role:{role['id']}") if isinstance(item, ToolResultMessage)]
    assert results[-1].is_error is False
    assert results[-1].details['status'] == 'succeeded'
    assert results[-1].details['taskId'].startswith('spawn_')
    assert results[-1].details['origin']['parentRunId']
    assert len([item for item in sessions.list_messages(f"role:{role['id']}") if item.role == 'user']) == 1


def test_background_completion_survives_parent_and_is_appended_once(api, monkeypatch):
    import asyncio
    import threading
    import time
    from backend.app import main

    client, roles, sessions, tasks = api
    release = threading.Event()
    started = threading.Event()
    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            system = str(messages[0])
            results = [item for item in messages if item['role'] == 'tool']
            if 'independent delegated task' in system:
                started.set()
                while not release.is_set():
                    await asyncio.sleep(.01)
                if not results:
                    yield ModelToolCall('read-background', 'read_file', {'path': 'background.txt'})
                    yield ModelResponseDone("tool_use")
                else:
                    yield ModelTextDelta('actual-background-923')
            elif 'internal spawn completion' in system:
                assert tools == []
                assert 'actual-background-923' in str(messages)
                yield ModelTextDelta('整理完成923')
            elif not results:
                yield ModelToolCall('background-spawn', 'spawn', {'task': 'Read background.txt', 'run_in_background': True})
                yield ModelResponseDone("tool_use")
            else:
                yield ModelTextDelta('parent-finished-first')
    monkeypatch.setattr(main, 'model_adapter', Adapter())
    role = client.post('/api/roles', json={'name': '后台角色', 'profile': {'profile': '测试'}, 'agentConfig': {
        'spawn': {'enabled': True}, 'enabledTools': ['read_file'],
    }}).json()['role']
    (roles.workspace_path(role['id']) / 'background.txt').write_text('actual-background-923')
    # A connected application loop outlives the individual HTTP request.
    with client:
        response = client.post(f"/api/roles/{role['id']}/messages", json={'content': '后台委托'})
        assert 'assistant_completed' in response.text
        assert started.wait(2)
        assert not any(item.messageType == 'internal_completion' for item in sessions.list_messages(f"role:{role['id']}"))
        result = next(item for item in sessions.runtime_messages(f"role:{role['id']}") if isinstance(item, ToolResultMessage))
        assert result.details['background'] is True
        release.set()
        deadline = time.monotonic() + 4
        while not any(item.messageType == 'internal_completion' for item in sessions.list_messages(f"role:{role['id']}")):
            assert time.monotonic() < deadline
            time.sleep(.02)
        messages = sessions.list_messages(f"role:{role['id']}")
        completion = [item for item in messages if item.messageType == 'internal_completion']
        assert len(completion) == 1
        assert '整理完成923' in completion[0].content
        assert completion[0].role == 'assistant'
        assert completion[0].metadata['taskStatus'] == 'succeeded'
        assert len([item for item in messages if item.role == 'user']) == 1
        # Replaying a pending outbox after the session append is idempotent.
        tasks.update_completion(result.details['taskId'], status='pending', content=completion[0].content)
        time.sleep(.2)
        assert len([item for item in sessions.list_messages(f"role:{role['id']}") if item.messageType == 'internal_completion']) == 1



@pytest.mark.parametrize('change', ['unbind', 'rebind', 'platform', 'recreate', 'recreate_before', 'role', 'sender', 'thread'])
def test_channel_management_and_watchdog_preserve_frozen_actual_source(api, monkeypatch, change):
    import asyncio
    import time
    from datetime import datetime, timezone
    from backend.app import main
    from backend.app.agent_runtime import CapabilityRegistry, CancellationToken, ToolContext
    from backend.app.agent_runtime.provider import AssistantDoneEvent
    from backend.app.agent_runtime.types import AssistantMessage
    from backend.app.models import AgentRun
    from backend.app.multichannel import MultichannelStore
    from backend.app.task_runtime import TaskOrigin

    client, roles, sessions, tasks = api
    channels = MultichannelStore(sessions.database_path, sessions, MemoryCredentials())
    monkeypatch.setattr(main, 'multichannel_store', channels)
    role = client.post('/api/roles', json={'name': '渠道委托', 'profile': {'profile': '测试'},
        'agentConfig': {'spawn': {'enabled': True}, 'enabledTools': ['read_file']}}).json()['role']
    role_id = role['id']
    account = channels.register_channel_account(role_id, 'qq', account_id='source-account', enabled=True, allow_group=True)
    thread = channels.ensure_channel_thread(role_id, 'qq', account.account_id, 'source-group', 'group')
    def pair():
        code = channels.create_pairing_code(role_id, 'qq')
        assert channels.consume_pairing_code(code, role_id=role_id, channel='qq', scope='platform',
            platform_user_id='owner', account_id=account.account_id, chat_id='owner')
    pair()
    def turn(run_id, sender='owner', target=thread):
        previous = sessions.active_run(role_id)
        if previous:
            sessions.update_run(previous.runId, status='completed', ended_at=datetime.now(timezone.utc))
        _, message = channels.append_event(target, direction='inbound', content='delegate', external_message_id=run_id,
            metadata={'senderId': sender, 'senderIsOwner': True})
        sessions.create_run_with_existing_user_message(AgentRun(runId=run_id, roleId=role_id, sessionKey=target.session_key,
            status='running', startedAt=datetime.now(timezone.utc)), message.id)
        candidates = main._runtime_tool_candidates(roles.get(role_id), is_channel_owner=True,
            source_thread_id=target.thread_id, source_account_id=account.account_id, include_channel=True)
        tools = {item.definition.name: item for item in candidates}
        context = ToolContext(role_id, target.session_key, run_id, CancellationToken(), 'host-spawn-call')
        return candidates, tools, context
    class Provider:
        calls = 0
        async def stream_response(self, **kwargs):
            self.calls += 1
            await asyncio.sleep(10)
            yield AssistantDoneEvent(AssistantMessage('private-source-log'))
    async def run():
        candidates, tools, context = turn('first-parent')
        frozen = CapabilityRegistry(candidates).resolve(role_id=role_id, session_key=context.session_key,
            run_id=context.run_id, enabled_tools={'spawn', 'spawn_manage', 'read_file'}, allowed_risks={'read_only', 'mutating', 'external'})
        provider = Provider()
        tools['spawn'].bind(frozen, lambda: provider)
        if change == 'recreate_before':
            channels.delete_account(account.account_id)
            channels.register_channel_account(role_id, 'qq', account_id=account.account_id, enabled=True, allow_group=True)
        result = await tools['spawn'].execute({'task': 'controlled work', 'run_in_background': True}, context)
        if change == 'recreate_before':
            assert result.is_error
            assert 'logPath' not in result.details
            assert provider.calls == 0
            assert not tuple(tasks.root.glob('logs/*/*.log'))
            return
        assert not result.is_error
        task_id = result.details['taskId']
        origin = TaskOrigin(role_id, context.session_key, source_thread_id=thread.thread_id,
            source_account_id=account.account_id, source_scope_id=result.details['origin']['sourceScopeId'])
        context.signal.cancel()  # Explicit background does not inherit parent cancellation.
        await asyncio.sleep(.08)
        assert tasks.query(task_id, origin).status == 'running'
        _, later_tools, later = turn('same-source-later')
        own = await later_tools['spawn_manage'].execute({'action': 'get', 'job_id': task_id}, later)
        assert not own.is_error
        if change in {'unbind', 'rebind'}:
            channels.unbind_identity(str(channels.list_identities()[0]['identityId']))
            if change == 'rebind':
                pair()
        elif change == 'platform':
            channels.set_channel_enabled('qq', False)
        elif change == 'recreate':
            channels.delete_account(account.account_id)
            channels.register_channel_account(role_id, 'qq', account_id=account.account_id, enabled=True, allow_group=True)
        elif change == 'role':
            later = ToolContext('other-role', later.session_key, later.run_id, CancellationToken())
        elif change == 'sender':
            _, later_tools, later = turn('other-sender', sender='another')
        elif change == 'thread':
            another = channels.ensure_channel_thread(role_id, 'qq', account.account_id, 'another-group', 'group')
            _, later_tools, later = turn('other-group', target=another)
        for action in ['get', 'cancel']:
            denied = await later_tools['spawn_manage'].execute({'action': action, 'job_id': task_id}, later)
            assert denied.is_error
            assert 'logPath' not in denied.details
            assert 'private-source-log' not in denied.content
        if change in {'unbind', 'rebind', 'platform', 'recreate'}:
            deadline = time.monotonic() + 3
            while tasks.query(task_id, origin).ended_at is None:
                assert time.monotonic() < deadline
                await asyncio.sleep(.02)
            assert tasks.query(task_id, origin).status == 'cancelled'
    asyncio.run(run())


def test_real_http_model_parent_and_child_use_independent_transcripts_and_frozen_config(api, monkeypatch):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from backend.app import main
    from backend.app.model_adapter import OpenAICompatibleAdapter
    from backend.app.models import ModelConfiguration

    client, roles, sessions, _tasks = api
    requests, errors = [], []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                assert self.path == '/v1/chat/completions'
                system = str(body['messages'][0])
                results = [item for item in body['messages'] if item['role'] == 'tool']
                if 'independent delegated task' in system:
                    assert 'parent-private-http-111' not in str(body)
                    assert body['model'] == 'frozen-local-model'
                    assert body['max_tokens'] <= 4096
                    assert {item['function']['name'] for item in body['tools']} == {'read_file', 'list_dir', 'web_search', 'web_fetch'}
                    if not results:
                        # A model connection edit must not affect this child.
                        current = main.model_configuration_store.get(configuration.id)
                        main.model_configuration_store.update(configuration.id, current.model_copy(update={'model': 'later-model'}))
                        delta = {'tool_calls': [{'index': 0, 'id': 'http-read', 'type': 'function',
                            'function': {'name': 'read_file', 'arguments': json.dumps({'path': 'http-evidence.txt'})}}]}
                    else:
                        assert 'http-known-111' in str(results[-1])
                        delta = {'content': 'http-child-result-111'}
                elif not results:
                    delta = {'tool_calls': [{'index': 0, 'id': 'http-spawn', 'type': 'function',
                        'function': {'name': 'spawn', 'arguments': json.dumps({'task': 'Read http-evidence.txt'})}}]}
                else:
                    assert 'http-child-result-111' in str(results[-1])
                    delta = {'content': 'http-parent-result-111'}
                chunks = [{'id': 'controlled-http', 'object': 'chat.completion.chunk', 'model': body['model'],
                    'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
                    {'id': 'controlled-http', 'object': 'chat.completion.chunk', 'model': body['model'],
                    'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if 'tool_calls' in delta else 'stop'}]}]
                payload = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n'
                raw = payload.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except Exception as error:
                errors.append(str(error))
                self.send_error(500)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
    worker.start()
    try:
        configuration = main.model_configuration_store.create(ModelConfiguration(providerId='custom', provider='openai',
            baseUrl=f'http://127.0.0.1:{server.server_port}/v1', model='frozen-local-model'))
        monkeypatch.setattr(main, 'model_adapter', OpenAICompatibleAdapter(timeout=3))
        role = client.post('/api/roles', json={'name': 'HTTP委托', 'profile': {'profile': '测试'},
            'agentConfig': {'spawn': {'enabled': True}, 'enabledTools': ['read_file']}}).json()['role']
        assert client.put(f"/api/roles/{role['id']}/model-configuration", json={'configurationId': configuration.id}).status_code == 200
        (roles.workspace_path(role['id']) / 'http-evidence.txt').write_text('http-known-111')
        response = client.post(f"/api/roles/{role['id']}/messages", json={'content': 'parent-private-http-111'})
        assert 'assistant_completed' in response.text, response.text
        assert not errors
        assert len(requests) == 4
        assert all(item['model'] == 'frozen-local-model' for item in requests)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


@pytest.mark.parametrize('delivery', ['sent', 'pending', 'failed', 'uncertain', 'revoked', 'recover'])
def test_background_channel_completion_uses_original_delivery_ledger_without_replay(api, monkeypatch, delivery):
    import asyncio
    from datetime import datetime, timezone
    from backend.app import main
    from backend.app.agent_runtime import CancellationToken, ToolContext
    from backend.app.agent_runtime.meido_provider import MeidoProvider
    from backend.app.channels.base import ChannelAdapter, PlatformRejectedError
    from backend.app.channels.models import ChannelCapabilities, DeliveryResult, DeliveryStatus, TargetKind, target_from_chat
    from backend.app.models import AgentRun
    from backend.app.task_runtime import TaskOrigin, TaskRuntime

    client, roles, sessions, tasks = api
    expected = 'sent' if delivery == 'recover' else 'failed' if delivery == 'revoked' else delivery
    channels = main.multichannel_store
    role = client.post('/api/roles', json={'name': '完成投递', 'profile': {'profile': '测试'},
        'agentConfig': {'spawn': {'enabled': True}, 'enabledTools': ['read_file']}}).json()['role']
    role_id = role['id']
    (roles.workspace_path(role_id) / 'channel-result.txt').write_text('channel-proof-881')
    account = channels.register_channel_account(role_id, 'qq', account_id='completion-source', enabled=True, allow_group=True)
    code = channels.create_pairing_code(role_id, 'qq')
    assert channels.consume_pairing_code(code, role_id=role_id, channel='qq', scope='platform', platform_user_id='owner', account_id=account.account_id, chat_id='owner')
    thread = channels.ensure_channel_thread(role_id, 'qq', account.account_id, 'original-group', 'group')
    target = target_from_chat(account_id=account.account_id, chat_id=thread.chat_id, kind=TargetKind.GROUP)
    class Adapter(ChannelAdapter):
        channel = 'qq'
        calls = 0
        def __init__(self):
            super().__init__()
            self.account_id = account.account_id
        @property
        def capabilities(self):
            return ChannelCapabilities(group=True, targets=frozenset({TargetKind.GROUP}))
        def parse_event(self, payload):
            return None
        async def list_targets(self):
            return [target]
        async def send(self, message):
            self.calls += 1
            assert message.target.chat_id == 'original-group'
            assert 'channel-proof-881' in message.content
            if delivery == 'uncertain':
                raise TimeoutError('controlled fixture timeout')
            if delivery == 'failed':
                raise PlatformRejectedError('controlled fixture rejection')
            if delivery == 'pending':
                return DeliveryResult(DeliveryStatus.PENDING, message.target, 'platform-completion')
            return DeliveryResult(DeliveryStatus.SENT, message.target, 'platform-completion')
    adapter = Adapter()
    main.channel_host.register(adapter)
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            if 'internal spawn completion' in str(messages[0]):
                assert not tools
                assert 'channel-proof-881' in str(messages)
                yield ModelTextDelta('channel-proof-881 summary')
            elif not any(item['role'] == 'tool' for item in messages):
                yield ModelToolCall('channel-read', 'read_file', {'path': 'channel-result.txt'})
                yield ModelResponseDone("tool_use")
            else:
                yield ModelTextDelta('channel-proof-881 result')
    model = Model()
    monkeypatch.setattr(main, 'model_adapter', model)
    async def run():
        await main.channel_host.account_targets(role_id, account.account_id)
        _, inbound = channels.append_event(thread, direction='inbound', content='delegate', external_message_id='initial', metadata={'senderId': 'owner', 'senderIsOwner': True})
        parent = AgentRun(runId='channel-parent', roleId=role_id, sessionKey=thread.session_key, status='running', startedAt=datetime.now(timezone.utc))
        sessions.create_run_with_existing_user_message(parent, inbound.id)
        lock = main._runtime_role_lock(role_id)
        await lock.acquire()
        candidates = main._runtime_tool_candidates(roles.get(role_id), is_channel_owner=True, source_thread_id=thread.thread_id, source_account_id=account.account_id, include_channel=True)
        resolution = await main._runtime_capabilities(roles.get(role_id), session_key=thread.session_key, run_id=parent.runId, available_tools=candidates)
        main._bind_spawn_runtime(resolution, MeidoProvider(model, roles.get(role_id), None))
        context = ToolContext(role_id, thread.session_key, parent.runId, CancellationToken(), 'spawn-channel', resolution.snapshot.snapshot_id)
        result = await resolution.get('spawn').execute({'task': 'Read channel-result.txt', 'run_in_background': True}, context)
        assert not result.is_error
        task_id = result.details['taskId']
        origin = TaskOrigin(role_id, thread.session_key, source_thread_id=thread.thread_id, source_account_id=account.account_id,
            source_scope_id=result.details['origin']['sourceScopeId'])
        deadline = asyncio.get_running_loop().time() + 3
        while tasks.query(task_id, origin).ended_at is None:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(.02)
        assert not any(item.messageType == 'internal_completion' for item in sessions.list_messages(thread.session_key))
        if delivery == 'revoked':
            channels.unbind_identity(str(channels.list_identities()[0]['identityId']))
        sessions.update_run(parent.runId, status='completed', ended_at=datetime.now(timezone.utc))
        lock.release()
        deadline = asyncio.get_running_loop().time() + 3
        while tasks.completion(task_id, origin)['status'] in {'pending', 'sending'} and not (delivery == 'pending' and adapter.calls):
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(.02)
        if delivery == 'pending':
            await asyncio.sleep(.2)
        assert tasks.completion(task_id, origin)['status'] == expected, str(tasks.completion(task_id, origin))
        messages = [item for item in sessions.list_messages(thread.session_key) if item.messageType == 'internal_completion']
        if delivery == 'revoked':
            assert not messages
            assert adapter.calls == 0
        else:
            assert len(messages) == 1
            assert messages[0].metadata['deliveryStatus'] == expected
            assert adapter.calls == 1
            attempts = channels.list_deliveries(role_id)
            assert len(attempts) == 1 and attempts[0]['sourceKind'] == 'spawn_completion'
            assert attempts[0]['status'] == expected
            if expected != 'sent':
                assert not any('summary' in item.content for item in sessions.runtime_messages(thread.session_key))
        await main._spawn_completion_worker.close()
        if delivery == 'recover':
            # Crash window: the send may have reached the platform, but the
            # task outbox and local message have not recorded the receipt.
            tasks.update_completion(task_id, status='sending', content=messages[0].content)
            sessions.update_message(messages[0].id, messages[0].content, 'completed', metadata={**messages[0].metadata,
                'deliveryStatus': 'pending'})
        # Reopening persistence must preserve sent/failed/uncertain without a second send.
        restarted = TaskRuntime(tasks.root)
        try:
            assert restarted.completion(task_id, origin)['status'] == ('uncertain' if delivery == 'recover' else expected)
            assert restarted.pending_completions() == ()
            if delivery == 'recover':
                monkeypatch.setattr(main, 'task_runtime', restarted)
                main._ensure_spawn_completion_worker()
                await asyncio.sleep(.1)
                await main._spawn_completion_worker.close()
                restored = [item for item in sessions.list_messages(thread.session_key) if item.messageType == 'internal_completion']
                assert len(restored) == 1 and restored[0].metadata['deliveryStatus'] == 'uncertain'
                assert adapter.calls == 1
        finally:
            restarted.close()
        await resolution.close()
    asyncio.run(run())


def test_legacy_spawn_switch_does_not_hide_default_delegation_and_invalid_budgets_are_rejected(api, monkeypatch):
    from backend.app import main
    client, _roles, sessions, _tasks = api
    class Adapter:
        schemas = []
        async def stream_structured_messages(self, messages, configuration, tools):
            self.schemas.append({item['function']['name'] for item in tools})
            if 'independent delegated task' in str(messages[0]):
                yield ModelTextDelta('default-child-result')
            elif not any(item['role'] == 'tool' for item in messages):
                yield ModelToolCall('forged-spawn', 'spawn', {'task': 'should be unavailable'})
                yield ModelResponseDone("tool_use")
            else:
                yield ModelTextDelta('permission-result')
    adapter = Adapter()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    role = client.post('/api/roles', json={'name': '关闭委托', 'profile': {'profile': '测试'}, 'agentConfig': {
        'spawn': {'enabled': False}, 'enabledTools': ['read_file', 'spawn', 'spawn_manage']}}).json()['role']
    response = client.post(f"/api/roles/{role['id']}/messages", json={'content': 'attempt'})
    assert 'assistant_completed' in response.text
    assert {'spawn', 'spawn_manage'} <= adapter.schemas[0]
    result = next(item for item in sessions.runtime_messages(f"role:{role['id']}") if isinstance(item, ToolResultMessage))
    assert not result.is_error and 'default-child-result' in result.content
    for budget in [{'timeoutSeconds': 601}, {'maxTurns': 0}, {'maxOutputChars': 100001}, {'maxModelOutputChars': 0}]:
        invalid = client.put(f"/api/roles/{role['id']}", json={'name': role['name'], 'profile': role['profile'],
            'agentConfig': {'spawn': {'enabled': True, **budget}}})
        assert invalid.status_code == 422


def test_restart_preserves_interrupted_child_and_delivers_internal_completion_once(api, monkeypatch):
    import asyncio
    from backend.app import main
    from backend.app.task_runtime import TaskOrigin, TaskRuntime
    client, _roles, sessions, tasks = api
    class Model:
        children = 0
        async def stream_structured_messages(self, messages, configuration, tools):
            system = str(messages[0])
            if 'internal spawn completion' in system:
                assert not tools and 'status=interrupted' in str(messages)
                yield ModelTextDelta('任务因重启中断，尚未完成')
            elif 'independent delegated task' in system:
                self.children += 1
                await asyncio.sleep(30)
                yield ModelTextDelta('should never resume')
            elif not any(item['role'] == 'tool' for item in messages):
                yield ModelToolCall('restart-child', 'spawn', {'task': 'controlled pending child', 'run_in_background': True})
                yield ModelResponseDone("tool_use")
            else:
                yield ModelTextDelta('parent-finished')
    model = Model()
    monkeypatch.setattr(main, 'model_adapter', model)
    role = client.post('/api/roles', json={'name': '重启恢复', 'profile': {'profile': '测试'},
        'agentConfig': {'spawn': {'enabled': True}, 'enabledTools': ['read_file']}}).json()['role']
    assert 'assistant_completed' in client.post(f"/api/roles/{role['id']}/messages", json={'content': 'delegate'}).text
    result = next(item for item in sessions.runtime_messages(f"role:{role['id']}") if isinstance(item, ToolResultMessage))
    origin = TaskOrigin(role['id'], f"role:{role['id']}", source_thread_id=f"desktop:{role['id']}")
    tasks.close()
    restarted = TaskRuntime(tasks.root)
    monkeypatch.setattr(main, 'task_runtime', restarted)
    async def run():
        assert restarted.query(result.details['taskId'], origin).status == 'interrupted'
        main._ensure_spawn_completion_worker()
        deadline = asyncio.get_running_loop().time() + 3
        while restarted.completion(result.details['taskId'], origin)['status'] == 'pending':
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(.02)
        await main._spawn_completion_worker.close()
        messages = [item for item in sessions.list_messages(origin.session_key) if item.messageType == 'internal_completion']
        assert len(messages) == 1 and messages[0].metadata['taskStatus'] == 'interrupted'
        assert 'should never resume' not in messages[0].content
        assert model.children == 1
        assert restarted.completion(result.details['taskId'], origin)['status'] == 'sent'
        assert len([item for item in sessions.list_messages(origin.session_key) if item.role == 'user']) == 1
    try:
        asyncio.run(run())
    finally:
        restarted.close()


def test_http_later_turn_can_list_get_and_cancel_the_same_background_child(api, monkeypatch):
    import asyncio
    from backend.app import main
    client, _roles, sessions, _tasks = api
    class Adapter:
        task_id = None
        async def stream_structured_messages(self, messages, configuration, tools):
            system = str(messages[0])
            if 'independent delegated task' in system:
                await asyncio.sleep(20)
                yield ModelTextDelta('late-child')
                return
            if 'internal spawn completion' in system:
                yield ModelTextDelta('child-cancelled')
                return
            last_user = max(index for index, item in enumerate(messages) if item['role'] == 'user')
            action = messages[last_user]['content'].rsplit('\n', 1)[-1]
            if any(item['role'] == 'tool' for item in messages[last_user + 1:]):
                yield ModelTextDelta('observed-current-task')
            elif action == 'start':
                yield ModelToolCall('start', 'spawn', {'task': 'wait for cancellation', 'run_in_background': True})
                yield ModelResponseDone("tool_use")
            else:
                yield ModelToolCall('manage', 'spawn_manage', {'action': action, **({'job_id': self.task_id} if action != 'list' else {})})
                yield ModelResponseDone("tool_use")
    adapter = Adapter()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    role = client.post('/api/roles', json={'name': '跨回合管理', 'profile': {'profile': '测试'},
        'agentConfig': {'spawn': {'enabled': True}, 'enabledTools': ['read_file']}}).json()['role']
    key = f"role:{role['id']}"
    with client:
        assert 'assistant_completed' in client.post(f"/api/roles/{role['id']}/messages", json={'content': 'start'}).text
        adapter.task_id = next(item.details['taskId'] for item in sessions.runtime_messages(key) if isinstance(item, ToolResultMessage))
        for action in ['list', 'get', 'cancel']:
            assert 'assistant_completed' in client.post(f"/api/roles/{role['id']}/messages", json={'content': action}).text
            result = [item for item in sessions.runtime_messages(key) if isinstance(item, ToolResultMessage)][-1]
            assert not result.is_error or action == 'cancel'
            if action == 'list':
                assert result.details['tasks'][0]['taskId'] == adapter.task_id
            else:
                assert result.details['taskId'] == adapter.task_id
                assert result.details['status'] == ('cancelled' if action == 'cancel' else 'running')
