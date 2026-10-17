"""Read-only resource IDs and original-message evidence stay inside real grants."""
import asyncio
import json

import pytest

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401
from test_proactive_drift_authoring_api import NotesAuthor
from test_proactive_drift_delivery import prepare, settled

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_real_resource_is_readonly_and_history_reuses_original_evidence_without_foreign_or_autonomy_messages(api, monkeypatch):
    client, roles, sessions, _ = api
    class Author(NotesAuthor):
        async def stream_structured_messages(self, messages, configuration, tools):
            async for event in super().stream_structured_messages(messages, configuration, tools):
                if getattr(event, 'name', None) == 'write_file':
                    if event.arguments['path'].endswith('drift.json'):
                        value = json.loads(event.arguments['content'])
                        value['readResourceIds'] = ['role-profile']
                        event.arguments['content'] = json.dumps(value)
                    elif event.arguments['path'].endswith('SKILL.md'):
                        event.arguments['content'] = event.arguments['content'].replace('finish_drift]', 'fetch_messages, finish_drift]')
                yield event
    role, _, now = prepare(client, monkeypatch, extra_tools=('fetch_messages',), author=Author(),
        authoring_extra={'allowedResourceIds': ['role-profile']}, config_extra={'driftReadResourceIds': ['role-profile']})
    other = client.post('/api/roles', json={'name': 'foreign', 'profile': {'profile': 'foreign'}}).json()['role']['id']
    assert 'assistant_completed' in client.post(f'/api/roles/{other}/messages', json={'content': 'foreign-private-evidence'}).text
    foreign = next(item for item in sessions.list_messages(f'role:{other}') if item.role == 'user')
    assert 'assistant_completed' in client.post(f'/api/roles/{role}/messages', json={'content': 'own-original-evidence'}).text
    own = next(item for item in sessions.list_messages(f'role:{role}') if item.content == 'own-original-evidence')
    class Reader:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            if self.requests == 1:
                name, arguments = 'activate_skill', {'skill_id': 'reading-notes'}
            elif self.requests == 2:
                name, arguments = 'read_file', {'path': '@resource/role-profile'}
            elif self.requests == 3:
                assert '仅合成夹具' in json.loads(messages[-1].content[0].text)['content']
                name, arguments = 'write_file', {'path': '@resource/role-profile', 'content': 'never mutate persona'}
            elif self.requests == 4:
                assert messages[-1].is_error
                name, arguments = 'fetch_messages', {'ids': [foreign.id, own.id]}
            else:
                assert self.requests == 5
                result = json.loads(json.loads(messages[-1].content[0].text)['content'])
                assert result['matched_count'] == 1 and result['messages'][0]['source_ref'] == own.id
                assert 'foreign-private-evidence' not in str(result) and result['messages'][0]['content'] == 'own-original-evidence'
                name, arguments = 'finish_drift', {'skill_used': 'reading-notes', 'one_line': '核验自己获准的真实原文',
                    'next_action': '继续下一条', 'message_result': 'silent'}
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'resources-{self.requests}', name, arguments),)))
    adapter = Reader()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    try:
        now[0] += 1800
        state = asyncio.run(settled(main._proactive, role))
        assert adapter.requests == 5 and state['runs'][0]['status'] == 'finished_silent', state
        assert not (roles.workspace_path(role) / 'drift/@resource').exists()
    finally:
        asyncio.run(main._proactive.close())


@pytest.mark.parametrize('web_tool', ['web_fetch', 'web_search'])
def test_drift_actual_memory_is_readonly_current_role_and_real_web_ssrf_never_connects(api, tmp_path, monkeypatch, web_tool):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from backend.app.memory_engine import DefaultMemoryEngine, MemoryMutation, MemoryScope
    from backend.app.memory_events import MemoryEventBus, RetrievalCompleted
    from backend.app.memory_service import MemoryService
    from backend.app.memory_store import MemoryStore
    from backend.app.network_tools import NetworkToolsConfig
    client, roles, _, _ = api
    role, path, now = prepare(client, monkeypatch, extra_tools=('recall_memory', web_tool),
        agent_extra={'memoryRecall': {'enabled': True}})
    foreign = client.post('/api/roles', json={'name': 'foreign-memory', 'profile': {'profile': 'other'}}).json()['role']['id']
    memories, bus = MemoryStore(tmp_path / 'drift-memory.db'), MemoryEventBus()
    service = MemoryService(memories, implicit_extraction_enabled=False, memory_enabled=lambda: True, event_bus=bus)
    engine = DefaultMemoryEngine(service, role_exists=lambda key: roles.get(key) is not None, event_bus=bus)
    for name, value in {'memory_store': memories, 'memory_service': service, 'memory_engine': engine,
        'memory_event_bus': bus}.items():
        monkeypatch.setattr(main, name, value)
    main.memory_settings_store.update(enabled=True, embedding_enabled=False)
    async def seed():
        for owner, suffix in [(role, 'first'), (role, 'second'), (role, 'third'), (foreign, 'foreign')]:
            await engine.mutate(MemoryMutation(kind='remember', scope=MemoryScope(owner, f'role:{owner}'),
                summary='drift731 ' + suffix, memory_kind='fact', source_ref='local-' + suffix))
    asyncio.run(seed())
    before = {owner: [item.model_dump(mode='json') for item in memories.list_all(owner)] for owner in (role, foreign)}
    requests, connections = [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
        do_POST = do_GET
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    url = f'http://127.0.0.1:{server.server_port}/private'
    import socket
    original_connect = socket.socket.connect
    def connect(sock, address):
        if isinstance(address, tuple) and len(address) >= 2 and address[1] == server.server_port:
            connections.append(address)
        return original_connect(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(main, 'network_tools_config', NetworkToolsConfig(search_provider='exa_api', search_endpoint=url))
    class Reader:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            if self.requests == 1:
                name, args = 'activate_skill', {'skill_id': 'reading-notes'}
            elif self.requests == 2:
                name, args = 'recall_memory', {'query': 'drift731'}
            elif self.requests == 3:
                records = json.loads(messages[-1].content[0].text)
                assert len(records) == 2 and 'foreign' not in str(records)
                assert all(item['source']['session_key'] == f'role:{role}' for item in records)
                name, args = web_tool, {'url': url} if web_tool == 'web_fetch' else {'query': 'local synthetic fixture'}
            else:
                assert self.requests == 4 and messages[-1].is_error
                name, args = 'finish_drift', {'skill_used': 'reading-notes', 'one_line': '只读自己的记忆并拒绝私网请求',
                    'next_action': '继续合法素材', 'message_result': 'silent'}
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'memory-web-{self.requests}', name, args),)))
    model = Reader()
    monkeypatch.setattr(main, 'model_adapter', model)
    try:
        options = client.get(path + '/drift-grant-options').json()
        assert next(item for item in options['tools'] if item['name'] == 'recall_memory')['available']
        now[0] += 1800
        state = asyncio.run(settled(main._proactive, role))
        assert model.requests == 4 and state['runs'][0]['status'] == 'finished_silent', state
        assert requests == [] and connections == []
        retrievals = [item for item in bus.events if isinstance(item, RetrievalCompleted)]
        assert len(retrievals) == 1 and retrievals[0].trace['effect'] == 'read_only' and retrievals[0].result_count == 2
        assert before == {owner: [item.model_dump(mode='json') for item in memories.list_all(owner)] for owner in (role, foreign)}
    finally:
        asyncio.run(main._proactive.close())
        server.shutdown()
        server.server_close()
        worker.join(5)
        assert not worker.is_alive()


def test_actual_file_failure_is_an_error_in_model_protocol_and_persisted_call_result(api, monkeypatch):
    client, _, _, _ = api
    role, _, now = prepare(client, monkeypatch)
    class Reader:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            if self.requests == 1:
                name, args = 'activate_skill', {'skill_id': 'reading-notes'}
            elif self.requests == 2:
                name, args = 'read_file', {'path': 'work/reading-notes/missing-file.txt'}
            else:
                assert self.requests == 3 and messages[-1].is_error
                assert json.loads(messages[-1].content[0].text)['isError']
                name, args = 'finish_drift', {'skill_used': 'reading-notes', 'one_line': '如实记录读取失败',
                    'next_action': '等待真实素材', 'message_result': 'silent'}
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'file-error-{self.requests}', name, args),)))
    model = Reader()
    monkeypatch.setattr(main, 'model_adapter', model)
    try:
        now[0] += 1800
        state = asyncio.run(settled(main._proactive, role))
        assert state['runs'][0]['status'] == 'finished_silent' and model.requests == 3, state
        with main._proactive._transaction() as db:
            row = db.execute("SELECT is_error,result FROM autonomy_drift_tool_calls WHERE call_id='file-error-2'").fetchone()
            assert row['is_error'] == 1 and json.loads(row['result'])['isError']
    finally:
        asyncio.run(main._proactive.close())
