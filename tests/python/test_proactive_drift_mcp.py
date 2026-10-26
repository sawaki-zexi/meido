"""The same S06 bundle waits for an owner grant, then really mounts stdio schemas."""
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import time

import pytest

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
from backend.app.mcp_config import McpConfigStore, McpServerInput
from backend.app.mcp_runtime import McpHost
from backend.app.model_adapter import ModelTextDelta, ModelToolCall, ModelResponseDone
from test_background_shell_api import MemoryCredentials
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401
from test_proactive_drift_authoring_api import NotesAuthor
from test_proactive_mcp import FIXTURE


pytestmark = pytest.mark.usefixtures('local_platform_only')


@pytest.fixture
def owned_mcp(api, tmp_path, monkeypatch):
    host = McpHost(McpConfigStore(tmp_path / 'drift-mcp.db', MemoryCredentials()))
    monkeypatch.setattr(main, 'mcp_host', host)
    monkeypatch.setattr(main, '_proactive', None)
    try:
        yield host
    finally:
        if main._proactive is not None:
            asyncio.run(main._proactive.close())
        asyncio.run(host.close())


@pytest.mark.parametrize('fault', ['success', 'catalog_changed', 'persist_failed', 'revoked'])
def test_owner_adds_missing_permission_to_the_same_hash_then_mount_publishes_next_real_model_schema(api, owned_mcp, tmp_path, monkeypatch, fault):
    client, roles, sessions, _ = api
    host = owned_mcp
    now = [time.time()]
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    role = client.post('/api/roles', json={'name': '有依赖的小任务', 'profile': {'profile': '离线MCP'},
        'agentConfig': {'enabledTools': ['read_file', 'write_file', 'edit_file']}}).json()['role']
    identifier, remote_name = role['id'], 'mcp_source__get_events'
    path = f'/api/roles/{identifier}/autonomy'
    saved = client.put(path, json={'expectedRevision': 0, 'config': {'driftAuthoring': {'enabled': True,
        'allowedTools': ['read_file', 'write_file', 'edit_file', 'finish_drift'], 'allowMcp': True}}})
    assert saved.status_code == 200, saved.text

    class Author(NotesAuthor):
        async def stream_structured_messages(self, messages, configuration, tools):
            async for event in super().stream_structured_messages(messages, configuration, tools):
                if isinstance(event, ModelToolCall) and event.name == 'write_file':
                    arguments = dict(event.arguments)
                    if arguments['path'].endswith('references/drift.json'):
                        value = json.loads(arguments['content'])
                        value['requiresMCP'] = [{'serverId': 'source', 'tools': [{'name': 'get_events'}]}]
                        arguments['content'] = json.dumps(value)
                    elif arguments['path'].endswith('SKILL.md'):
                        arguments['content'] = arguments['content'].replace('tools: [read_file,', f'tools: [{remote_name}, read_file,')
                    event = ModelToolCall(event.id, event.name, arguments)
                yield event

    monkeypatch.setattr(main, 'model_adapter', Author())
    assert 'assistant_completed' in client.post(f'/api/roles/{identifier}/messages', json={'content': '$create-drift-skill 创建有依赖的任务'}).text
    pending = client.get(path + '/drift-skills').json()['skills'][0]
    assert pending['provenance']['status'] == 'verified'
    assert pending['status'] == 'pending_dependency' and pending['missingMcp'] == ['source:get_events']
    bundle = pending['bundleSha256']
    root = tmp_path / 'service'
    root.mkdir()
    declaration = {'name': 'get_events', 'description': 'local business evidence',
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}
    unrelated = {**declaration, 'name': 'poll', 'description': 'not declared by the active skill'}
    (root / 'catalog.json').write_text(json.dumps([declaration, unrelated]), encoding='utf-8')
    (root / 'events.json').write_text(json.dumps([{'id': 'fixture', 'kind': 'content', 'title': 'actual-mount-evidence'}]), encoding='utf-8')
    server_body = {'serverId': 'source', 'name': '离线依赖',
        'command': [str(Path(sys.executable).resolve()), '-I', str(FIXTURE), str(root)], 'cwd': str(root),
        'roleIds': [identifier], 'enabled': True, 'risk': 'external', 'timeoutSeconds': 10, 'enableDiscovery': True}
    configured = client.post('/api/mcp/servers', json=server_body)
    assert configured.status_code == 201, configured.text

    class Connect:
        requests = 0
        async def stream_structured_messages(self, messages, configuration, tools):
            self.requests += 1
            if self.requests == 1:
                yield ModelToolCall('load-owner-connect', 'tool_search', {'query': 'select:mcp_add'})
                yield ModelResponseDone('tool_use')
            elif self.requests == 2:
                assert 'mcp_add' in {item['function']['name'] for item in tools}
                yield ModelToolCall('actual-owner-connect', 'mcp_add', {'server_id': 'source'})
                yield ModelResponseDone('tool_use')
            else:
                assert 'connected' in str(messages[-1])
                yield ModelTextDelta('真实目录已观察，尚未授权Drift调用')
                yield ModelResponseDone('stop')

    monkeypatch.setattr(main, 'model_adapter', Connect())
    assert 'assistant_completed' in client.post(f'/api/roles/{identifier}/messages', json={'content': '连接已保存的离线MCP'}).text
    options = client.get(path + '/drift-grant-options')
    assert options.status_code == 200, options.text
    option = next(item for item in options.json()['mcpServers'] if item['serverId'] == 'source')
    assert option['status'] == 'observed' and option['operations'][0]['name'] == 'get_events'
    assert not (root / 'calls.jsonl').exists()
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'fixture-only', 'apiKey': 'fixture'}).json()['configuration']
    config = client.get(path).json()['config']
    saved = client.put(path, json={'expectedRevision': config['revision'], 'config': {**config,
        'driftEnabled': True, 'driftModelConfigurationId': model['id'], 'consentRevision': 1,
        'driftToolGrants': ['read_file', 'write_file', 'edit_file'],
        'driftMcpGrants': [{**{key: option[key] for key in ('serverId', 'configRevision', 'grantRevision', 'risk')},
            'operations': [{key: operation[key] for key in ('name', 'definitionHash')} for operation in option['operations']]}]}})
    assert saved.status_code == 200, saved.text
    if fault == 'catalog_changed':
        (root / 'catalog.json').write_text(json.dumps([{**declaration, 'description': 'actual changed contract'}, unrelated]), encoding='utf-8')

    class Drift:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            names = {item['function']['name'] for item in tools}
            if self.requests == 1:
                assert remote_name not in names
                name, arguments = 'activate_skill', {'skill_id': 'reading-notes'}
            elif self.requests == 2:
                assert remote_name not in names
                if fault == 'catalog_changed':
                    assert messages[-1].is_error
                    raise RuntimeError('fixture provider ends after the actual activation dependency rejection')
                if fault == 'persist_failed':
                    with main._proactive._transaction() as db:
                        db.execute('''CREATE TRIGGER reject_mount_call BEFORE INSERT ON autonomy_drift_tool_calls
                            WHEN NEW.tool_name='mount_server' BEGIN SELECT RAISE(ABORT,'fixture rejects call evidence'); END''')
                name, arguments = 'mount_server', {'server_id': 'source'}
            elif self.requests == 3:
                assert remote_name in names, 'mount must publish the actual schema before this provider request'
                assert 'mcp_source__poll' not in names, 'owner upper ceiling must not expand the skill declaration'
                if fault == 'revoked':
                    host.store.save(McpServerInput.model_validate({**server_body, 'enabled': False}))
                name, arguments = remote_name, {}
            else:
                assert 'actual-mount-evidence' in messages[-1].content[0].text
                name, arguments = 'finish_drift', {'skill_used': 'reading-notes', 'one_line': '真实调用已挂载依赖',
                    'next_action': '下一次仍重验相同版本', 'message_result': 'silent'}
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'drift-mount-{self.requests}', name, arguments),)))

    adapter = Drift()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    async def exercise():
        now[0] += 1800
        await main._proactive.on_clock()
        async with asyncio.timeout(20):
            while True:
                await main._proactive.recover()
                state = main._proactive.query(identifier)
                if state['runs'] and all(item['runtimeAcknowledged'] for item in state['runs']):
                    return state
                await asyncio.sleep(.01)
    try:
        state = asyncio.run(exercise())
        if fault != 'success':
            assert adapter.requests == (3 if fault == 'revoked' else 2) and state['runs'][0]['status'] == 'aborted', state
            assert not (root / 'calls.jsonl').exists()
            with sqlite3.connect(sessions.database_path) as db:
                assert db.execute('SELECT COUNT(*) FROM autonomy_capability_revisions').fetchone()[0] == 0
                assert db.execute('SELECT COUNT(*) FROM autonomy_drift_progress').fetchone()[0] == 0
            assert host.public()[0]['activeProcesses'] == 0
            return
        assert adapter.requests == 4 and state['runs'][0]['status'] == 'finished_silent', state
        current = client.get(path + '/drift-skills').json()['skills'][0]
        assert current['bundleSha256'] == bundle and current['provenance']['runId'] == pending['provenance']['runId']
        calls = [json.loads(line) for line in (root / 'calls.jsonl').read_text().splitlines()]
        assert [item['name'] for item in calls] == ['get_events']
        with sqlite3.connect(sessions.database_path) as db:
            assert db.execute('SELECT COUNT(*) FROM autonomy_capability_revisions').fetchone()[0] == 0
            binding = json.loads(db.execute('SELECT binding_json FROM autonomy_drift_tool_calls WHERE tool_name=?', (remote_name,)).fetchone()[0])
        assert [item['name'] for item in binding['tools']] == [remote_name]
        assert host.public()[0]['activeProcesses'] == 0
    finally:
        if main._proactive is not None:
            asyncio.run(main._proactive.close())
        asyncio.run(host.close())
