"""Current saved context is an independent, read-only HTTP operation."""
import asyncio
import copy
import hashlib
import io
import json
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image

from backend.app import main
from backend.app.agent_runtime.usage_anchor import UsageAnchors
from backend.app.agent_runtime.meido_provider import MeidoProvider
from backend.app.agent_runtime.types import AssistantMessage, ToolCall, ToolResultMessage, ImageContent
from backend.app.chat_attachments import save_attachment
from backend.app.context_windows import ContextWindowStore
from backend.app.models import ModelConfigurationInput
from test_chat_stop import chat_server  # noqa: F401


def configure():
    return main.model_configuration_store.create(ModelConfigurationInput(
        providerId='custom', provider='fixture', baseUrl='http://127.0.0.1:1',
        model='fixture', contextWindowTokens=64000))


def database(path=None):
    with sqlite3.connect(path or main.session_store.database_path) as connection:
        return list(connection.iterdump())


def test_saved_context_is_measured_without_a_turn_or_any_writes(chat_server, monkeypatch):
    client, role, _ = chat_server
    configuration = configure()
    # chat_server omits lifespan; initialize the service that normal startup
    # creates before accepting any HTTP requests.
    main._coding_application()
    main._novelai_application()
    main._desktop_pet_application()
    monkeypatch.setattr(main, 'roles_root', main.store.root)
    main.memory_settings_store.update(enabled=True)
    memory_dir = main.store.root / role.id / 'memory'
    memory_dir.mkdir(exist_ok=True)
    (memory_dir / 'MEMORY.md').write_text('stable saved memory', encoding='utf-8')
    main.session_store.open_role_session(role.id)
    main.session_store.append_message(f'role:{role.id}', 'user', '已经保存的输入', 'completed')
    anchors = UsageAnchors()
    monkeypatch.setattr(main, '_ordinary_usage_anchors', anchors)
    def forbidden(*args, **kwargs):
        raise AssertionError('inspection must not execute or maintain anything')
    monkeypatch.setattr(main.model_adapter, 'stream_rich_messages', forbidden)
    monkeypatch.setattr(main.memory_maintenance, 'refresh_recent_context', forbidden)
    monkeypatch.setattr(main, '_role_reaction_turn', forbidden)
    monkeypatch.setattr(main._current_memory_engine(), 'query', forbidden)
    monkeypatch.setattr(main._current_memory_engine(), 'ensure_coverage', forbidden)
    monkeypatch.setattr(main.todo_plugin, 'context_block', forbidden)
    monkeypatch.setattr(main.owner_knowledge_plugin, 'search', forbidden)
    from backend.app.agent_runtime.capabilities import CapabilityResolution
    monkeypatch.setattr(CapabilityResolution, 'publish_window', forbidden)
    monkeypatch.setattr(main.runtime_manager, 'run', forbidden)
    files_before = {path: path.read_bytes() for path in memory_dir.rglob('*') if path.is_file()}
    before, anchor_before = database(), copy.deepcopy(vars(anchors))
    response = client.get(f'/api/roles/{role.id}/context-status')
    assert response.status_code == 200, response.text
    status = response.json()
    assert status['status'] == 'ready', status
    assert status['roleId'] == role.id and status['sessionKey'] == f'role:{role.id}'
    assert status['inputTokens'] > 0 and status['source'] == 'local'
    assert status['configurationId'] == configuration.id and status['capacity'] == 64000
    assert status['inputLimit'] == 64000 - status['outputReserve'] - status['safetyMargin']
    assert response.headers['cache-control'] == 'no-store'
    assert database() == before
    assert vars(anchors) == anchor_before
    assert {path: path.read_bytes() for path in memory_dir.rglob('*') if path.is_file()} == files_before


def test_missing_model_and_unknown_capacity_are_explicit(chat_server):
    client, role, _ = chat_server
    path = f'/api/roles/{role.id}/context-status'
    missing = client.get(path).json()
    assert missing['status'] == 'unavailable' and missing['inputTokens'] is None
    assert missing['reason']
    main.model_configuration_store.create(ModelConfigurationInput(
        providerId='custom', provider='fixture', baseUrl='http://127.0.0.1:1', model='unknown'))
    unknown = client.get(path).json()
    assert unknown['status'] == 'ready' and unknown['capacity'] is None
    assert unknown['inputTokens'] > 0 and unknown['inputLimit'] is None and unknown['reason']


def test_readonly_anchor_estimation_preserves_live_calibration():
    anchors = UsageAnchors()
    context = ('role', 'session', 'scope', 'authority')
    request = {'messages': [{'role': 'user', 'content': 'hello'}], 'tools': []}
    anchors.finish(anchors.begin(request, context), 150)
    before = copy.deepcopy(vars(anchors))
    assert anchors.estimate(request, context, readonly=True).source == 'actual'
    assert anchors.estimate({'messages': [{'role': 'user', 'content': 'changed'}]}, context, readonly=True).source == 'local'
    assert anchors.estimate(request, (*context[:3], 'other'), readonly=True).source == 'local'
    assert vars(anchors) == before


def capture_input(monkeypatch):
    captured = []
    original = MeidoProvider.estimate_prepared
    def estimate(provider, prepared):
        result = original(provider, prepared)
        if provider.readonly_usage:
            captured.append((provider, prepared, provider._input_request(prepared.messages, prepared.tools,
                configuration=prepared.configuration, max_tokens=prepared.max_tokens)))
        return result
    monkeypatch.setattr(MeidoProvider, 'estimate_prepared', estimate)
    original_factory = main._desktop_provider
    def factory(*args, **kwargs):
        provider = original_factory(*args, **kwargs)
        provider._request_time = '2026-11-01T12:00:00+08:00'
        return provider
    monkeypatch.setattr(main, '_desktop_provider', factory)
    return captured


def test_missing_attachment_workspace_stays_missing_during_inspection(chat_server, monkeypatch, tmp_path):
    client, role, _ = chat_server
    configure()
    # Match normal startup before taking the read-only operation's snapshots.
    main._coding_application()
    main._novelai_application()
    main._desktop_pet_application()
    main.session_store.open_role_session(role.id)
    workspace = main.store.workspace_path(role.id)
    attachment = save_attachment(workspace, 'saved.txt', 'text/plain', b'previously saved attachment')
    main.session_store.append_message(f'role:{role.id}', 'user', 'saved input', metadata={'attachments': [attachment]})
    retained = workspace.with_name('retained-workspace')
    assert workspace.is_relative_to(tmp_path.resolve()) and retained.is_relative_to(tmp_path.resolve())
    workspace.rename(retained)
    assert not workspace.exists()
    anchors = UsageAnchors()
    monkeypatch.setattr(main, '_ordinary_usage_anchors', anchors)
    captured = capture_input(monkeypatch)
    before, anchor_before = database(), copy.deepcopy(vars(anchors))
    files_before = {path: path.read_bytes() for path in retained.rglob('*') if path.is_file()}

    response = client.get(f'/api/roles/{role.id}/context-status')

    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'ready', response.text
    assert '历史附件已缺失或不属于当前角色' in json.dumps(captured[-1][2], ensure_ascii=False)
    assert not workspace.exists(), 'read-only inspection must not recreate a missing role workspace'
    assert database() == before
    assert vars(anchors) == anchor_before
    assert {path: path.read_bytes() for path in retained.rglob('*') if path.is_file()} == files_before


@pytest.mark.parametrize('change', ['todo_disable', 'todo_enable', 'novelai_availability', 'novelai_quota', 'novelai_settings', 'coding_disable'])
def test_effective_tool_changes_during_inspection_discard_stale_status(chat_server, monkeypatch, tmp_path, change):
    from backend.app.agent_runtime.context_provider import ContextWindowProvider
    from backend.app.todo.plugin import TodoPlugin
    from backend.app.todo.store import TodoStore

    client, role, _ = chat_server
    configure()
    monkeypatch.setattr(main, 'data_root', tmp_path / 'data')
    todos = TodoStore(tmp_path / 'todos.db')
    todos.update_settings(enabled=change != 'todo_enable', reminders_enabled=False)
    monkeypatch.setattr(main, 'todo_store', todos)
    monkeypatch.setattr(main, 'todo_plugin', TodoPlugin(todos, main.session_store))
    coding = main._coding_application()
    coding.config.set_enabled(True)
    novelai = main._novelai_application()
    # This fake token never leaves the fixture or touches the system vault.
    class Credentials:
        def get(self, key):
            raise AssertionError('inspection must not read a generation credential')
        def set(self, key, value):
            pass
    monkeypatch.setattr(novelai.settings, 'credentials', Credentials())
    novelai.settings.set_token('fixture-token')
    novelai.settings.save({'enabled': True}, expected_revision=novelai.settings.public()['revision'])
    main._desktop_pet_application()
    main.session_store.open_role_session(role.id)
    main.session_store.append_message(f'role:{role.id}', 'user', 'saved input')
    anchors = UsageAnchors()
    monkeypatch.setattr(main, '_ordinary_usage_anchors', anchors)
    captured = capture_input(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = ContextWindowProvider.inspect
    async def held(window, *args, **kwargs):
        result = await original(window, *args, **kwargs)
        if not entered.is_set():
            entered.set()
            assert await asyncio.to_thread(release.wait, 5)
        return result
    monkeypatch.setattr(ContextWindowProvider, 'inspect', held)

    with ThreadPoolExecutor() as pool:
        reading = pool.submit(client.get, f'/api/roles/{role.id}/context-status')
        try:
            assert entered.wait(3)
            previous_request = json.dumps(captured[-1][2], ensure_ascii=False)
            if change.startswith('todo_'):
                assert ('todo.search' in previous_request) == (change == 'todo_disable')
                updated = client.put('/api/todos/settings', json={'enabled': change == 'todo_enable'})
                assert updated.status_code == 200, updated.text
            elif change.startswith('novelai_'):
                assert '本地配置已就绪' in previous_request
                if change == 'novelai_quota':
                    # A paid operation elsewhere can change declaration readiness
                    # without changing any service settings revision.
                    with novelai.ledger.connect() as connection:
                        connection.execute('INSERT INTO novelai_daily VALUES(?,?,?)',
                            (novelai.ledger.usage(role.id)['day'], role.id, 10))
                else:
                    values = {'allowTxt2Img': False, 'allowImg2Img': False} if change == 'novelai_availability' else {'maxSteps': 27}
                    novelai.settings.save(values, expected_revision=novelai.settings.public()['revision'])
            else:
                assert 'herdr_start_task' in previous_request
                coding.config.set_enabled(False)
            before = {path: database(path) for path in tmp_path.rglob('*.db')}
            anchor_before = copy.deepcopy(vars(anchors))
            release.set()
            response = reading.result(5)
            assert response.status_code == 200, response.text
            unchanged_input = change == 'novelai_settings'
            assert response.json()['status'] == ('ready' if unchanged_input else 'unavailable'), response.text
            if not unchanged_input:
                assert response.json()['inputTokens'] is None
            assert {path: database(path) for path in tmp_path.rglob('*.db')} == before
            assert vars(anchors) == anchor_before
        finally:
            release.set()

    fresh = client.get(f'/api/roles/{role.id}/context-status')
    assert fresh.status_code == 200 and fresh.json()['status'] == 'ready', fresh.text
    current_request = json.dumps(captured[-1][2], ensure_ascii=False)
    if change.startswith('todo_'):
        assert ('todo.search' in current_request) == (change == 'todo_enable')
    elif change == 'novelai_availability':
        assert '文生图和图生图均已关闭' in current_request
    elif change == 'novelai_quota':
        assert '今日生图尝试额度已用完' in current_request
    elif change == 'novelai_settings':
        assert current_request == previous_request
    elif change == 'coding_disable':
        assert 'herdr_start_task' not in current_request
    if change == 'todo_enable':
        assert client.put('/api/todos/settings', json={'enabled': False}).status_code == 200


def test_summary_tools_and_saved_images_share_normal_request_estimation(chat_server, monkeypatch):
    client, role, _ = chat_server
    configure()
    session = f'role:{role.id}'
    main.session_store.open_role_session(role.id)
    old = main.session_store.append_message(session, 'user', 'old raw text that was summarized')
    old_reply = main.session_store.append_message(session, 'assistant', 'old reply')
    buffer = io.BytesIO()
    Image.new('RGB', (12, 12), 'blue').save(buffer, format='PNG')
    attachment = save_attachment(main.store.workspace_path(role.id), 'saved.png', 'image/png', buffer.getvalue())
    unsent = save_attachment(main.store.workspace_path(role.id), 'draft.txt', 'text/plain', b'UNSENT_DRAFT_ATTACHMENT')
    main.session_store.append_message(session, 'user', 'look at saved picture', metadata={'attachments': [attachment]})
    main.session_store.append_agent_message(session, AssistantMessage('', tool_calls=(ToolCall('fetch', 'web_fetch', {'url': 'https://example.test'}),), stop_reason='tool_use'))
    main.session_store.append_agent_message(session, ToolResultMessage('fetch', 'web_fetch', 'SAVED_TOOL_RESULT',
        images=(ImageContent('image/png', 'aW1hZ2U='),)))
    main.session_store.append_message(session, 'assistant', 'saved answer')
    history = main._owner_history(role.id)
    windows = history.windows()
    authority = json.dumps({'role': role.id}, sort_keys=True) + history.authority
    prepared = windows.prepare(role.id, session, 'shared_owner', authority, [old.id, old_reply.id])
    windows.commit(prepared, {'tasks': 'SAVED_WORK_SUMMARY', 'source_message_ids': [old.id, old_reply.id]}, authorized=lambda: True, covered=True)
    window_before = windows.snapshot_role(role.id)
    captured = capture_input(monkeypatch)
    result = client.get(f'/api/roles/{role.id}/context-status').json()
    assert result['status'] == 'ready', result
    provider, request, normalized = captured[-1]
    text = json.dumps(normalized, ensure_ascii=False)
    assert 'SAVED_WORK_SUMMARY' in text and 'old raw text that was summarized' not in text
    assert 'SAVED_TOOL_RESULT' in text and 'saved answer' in text
    assert 'UNSENT_DRAFT_ATTACHMENT' not in text and unsent['id'] not in text
    assert text.count('image_url') >= 2
    assert 'web_fetch' in [tool['function']['name'] for tool in request.tools]
    # The regular provider uses precisely this prepared input and adapter
    # normalization; the read-only path only changes ownership of calibration.
    provider.readonly_usage = False
    provider.usage_anchors = UsageAnchors()
    assert provider.estimate_prepared(request).tokens == result['inputTokens']
    assert windows.snapshot_role(role.id) == window_before


def test_api_uses_actual_and_delta_without_changing_anchors_and_follows_model(chat_server, monkeypatch):
    client, role, _ = chat_server
    first = configure()
    main.session_store.open_role_session(role.id)
    main.session_store.append_message(f'role:{role.id}', 'user', 'saved')
    anchors = UsageAnchors()
    monkeypatch.setattr(main, '_ordinary_usage_anchors', anchors)
    captured = capture_input(monkeypatch)
    path = f'/api/roles/{role.id}/context-status'
    assert client.get(path).json()['source'] == 'local'
    provider, _, request = captured[-1]
    anchors.finish(anchors.begin(request, provider.usage_context), 444)
    before = copy.deepcopy(vars(anchors))
    actual = client.get(path).json()
    assert (actual['source'], actual['inputTokens']) == ('actual', 444)
    main.session_store.append_message(f'role:{role.id}', 'assistant', 'another saved reply')
    delta = client.get(path).json()
    assert delta['source'] == 'anchor_delta' and delta['inputTokens'] > 444
    assert vars(anchors) == before
    second = main.model_configuration_store.create(ModelConfigurationInput(
        providerId='custom', provider='fixture', baseUrl='http://127.0.0.1:1', model='second', contextWindowTokens=120000))
    assert client.put(f'/api/roles/{role.id}/model-configuration', json={'configurationId': second.id}).status_code == 200
    switched = client.get(path).json()
    assert switched['configurationId'] == second.id and switched['model'] == 'second'
    assert switched['capacity'] == 120000 and switched['source'] == 'local'
    assert vars(anchors) == before, 'inspection of a new model must preserve the old live calibration'
    assert client.put(f'/api/roles/{role.id}/model-configuration', json={'configurationId': first.id}).status_code == 200
    assert client.get(path).json()['source'] == 'anchor_delta'


def test_invalid_window_is_projected_without_writing_tombstone(chat_server):
    client, role, _ = chat_server
    configure()
    session = f'role:{role.id}'
    main.session_store.open_role_session(role.id)
    main.session_store.append_message(session, 'user', 'keep saved history')
    windows = ContextWindowStore(main.session_store.database_path)
    with windows.connect() as db:
        db.execute('INSERT INTO context_windows VALUES (?,?,?,?,?,?,?,?)',
            (role.id, session, 'shared_owner', 8, 'revoked-authority', '["gone"]', 'bad', '{"tasks":"stale summary"}'))
    before = windows.snapshot_role(role.id)
    result = client.get(f'/api/roles/{role.id}/context-status').json()
    assert result['status'] == 'ready'
    assert windows.snapshot_role(role.id) == before


@pytest.mark.parametrize('change', ['append', 'role', 'model', 'other_role'])
def test_stale_reads_are_discarded_without_blocking_other_pages(chat_server, monkeypatch, change):
    from backend.app import context_inspection
    from backend.app.models import RoleUpdateInput
    client, role, other = chat_server
    configure()
    for target in (role, other):
        main.session_store.open_role_session(target.id)
        main.session_store.append_message(f'role:{target.id}', 'user', 'saved')
    entered, release = threading.Event(), threading.Event()
    original = context_inspection._read
    def held(host, role_id):
        result = original(host, role_id)
        if role_id == role.id:
            entered.set()
            assert release.wait(5)
        return result
    monkeypatch.setattr(context_inspection, '_read', held)
    with ThreadPoolExecutor() as pool:
        reading = pool.submit(client.get, f'/api/roles/{role.id}/context-status')
        try:
            assert entered.wait(3)
            assert client.get('/api/health').status_code == 200
            if change in {'append', 'other_role'}:
                target = other if change == 'other_role' else role
                main.session_store.append_message(f'role:{target.id}', 'user', 'new saved message')
            elif change == 'role':
                main.store.update(role.id, RoleUpdateInput(name='updated role', profile=role.profile))
            else:
                main.model_configuration_store.create(ModelConfigurationInput(
                    providerId='custom', provider='fixture', baseUrl='http://127.0.0.1:1', model='changed'))
            release.set()
            result = reading.result(5).json()
            assert result['status'] == ('ready' if change == 'other_role' else 'unavailable'), result
            if change != 'other_role':
                assert result['inputTokens'] is None
        finally:
            release.set()


def test_runtime_and_maintenance_busy_are_role_scoped(chat_server, monkeypatch):
    client, role, other = chat_server
    configure()
    lock = threading.Lock()
    lock.acquire()
    main.role_locks[role.id] = lock
    path = f'/api/roles/{role.id}/context-status'
    assert client.get(path).json()['status'] == 'busy'
    assert client.get(f'/api/roles/{other.id}/context-status').json()['status'] == 'ready'
    lock.release()
    maintenance = threading.Lock()
    maintenance.acquire()
    monkeypatch.setattr(main.memory_worker, 'role_lock_for', lambda target: maintenance if target == role.id else lock)
    assert client.get(path).json()['status'] == 'busy'
    maintenance.release()
    assert client.get(path).json()['status'] == 'ready'


def test_inspection_failure_is_explicit_and_recoverable(chat_server, monkeypatch):
    from backend.app import context_inspection
    client, role, _ = chat_server
    configure()
    original = context_inspection._read
    def fail(*args):
        raise OSError('fixture read error')
    monkeypatch.setattr(context_inspection, '_read', fail)
    path = f'/api/roles/{role.id}/context-status'
    result = client.get(path).json()
    assert result['status'] == 'unavailable' and result['inputTokens'] is None and result['reason']
    monkeypatch.setattr(context_inspection, '_read', original)
    assert client.get(path).json()['status'] == 'ready'


def test_mcp_observed_catalog_is_reused_without_connecting_and_stale_catalog_is_unknown(chat_server, monkeypatch, tmp_path):
    from backend.app.mcp_config import McpConfigStore, McpServerInput
    from backend.app.mcp_runtime import McpHost, McpRun
    from test_mcp_http import Credentials
    client, role, _ = chat_server
    configure()
    configs = McpConfigStore(tmp_path / 'mcp.db', Credentials())
    host = McpHost(configs)
    monkeypatch.setattr(main, 'mcp_host', host)
    source = {'role_id': role.id, 'session_key': f'role:{role.id}', 'kind': 'desktop'}
    scope_id = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    data = McpServerInput(serverId='notes', name='Notes vault-secret', env={'FIXTURE_SECRET': 'vault-secret'},
        command=[sys.executable], cwd=str(tmp_path), roleIds=[role.id])
    config = configs.save(data)
    configs.set_intent(scope_id, role.id, source['session_key'], config)
    def forbidden(*args, **kwargs):
        raise AssertionError('inspection must never connect an MCP peer')
    monkeypatch.setattr(McpRun, 'prepare', forbidden)
    monkeypatch.setattr(host, 'freeze', forbidden)
    path = f'/api/roles/{role.id}/context-status'
    assert client.get(path).json()['status'] == 'unavailable'
    declaration = {'name': 'read', 'description': 'KNOWN_MCP_DESCRIPTOR', 'inputSchema': {'type': 'object'}}
    host.observe_catalog(config, [declaration])
    main.session_store.open_role_session(role.id)
    main.session_store.append_agent_message(source['session_key'],
        AssistantMessage('', tool_calls=(ToolCall('read', 'mcp_notes__read', {}),), stop_reason='tool_use'))
    main.session_store.append_agent_message(source['session_key'], ToolResultMessage('read', 'mcp_notes__read', 'saved response'))
    captured = capture_input(monkeypatch)
    with sqlite3.connect(configs.path) as db:
        before = list(db.iterdump())
    assert client.get(path).json()['status'] == 'ready'
    assert 'KNOWN_MCP_DESCRIPTOR' in json.dumps(captured[-1][2])
    assert 'vault-secret' not in json.dumps(captured[-1][2]), 'metadata uses the same redaction as real declarations'
    assert host._runs == {} and host._autonomy_runs == {}
    with sqlite3.connect(configs.path) as db:
        assert list(db.iterdump()) == before
    changed = configs.save(data.model_copy(update={'name': 'Changed Notes'}))
    configs.set_intent(scope_id, role.id, source['session_key'], changed)
    assert host.observed_catalog(role.id, 'notes')[0] == 'stale'
    assert client.get(path).json()['status'] == 'unavailable'
