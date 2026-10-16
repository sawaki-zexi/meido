"""Real desktop delivery is single-use and shrinks this tick's capabilities."""
import asyncio
import json
import time
import sqlite3

import pytest

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401
from test_proactive_drift_authoring_api import NotesAuthor

pytestmark = pytest.mark.usefixtures('local_platform_only')


def prepare(client, monkeypatch, *, extra_tools=(), author=None, authoring_extra=None, agent_extra=None, config_extra=None, clock_start=None):
    monkeypatch.setattr(main, '_proactive', None)
    now = [time.time() if clock_start is None else clock_start]
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    tools = ['read_file', 'write_file', 'edit_file', 'list_dir', *extra_tools]
    role = client.post('/api/roles', json={'name': '真实Drift闭环', 'profile': {'profile': '仅合成夹具'},
        'agentConfig': {'enabledTools': tools, **(agent_extra or {})}}).json()['role']
    path = f"/api/roles/{role['id']}/autonomy"
    response = client.put(path, json={'expectedRevision': 0, 'config': {'driftAuthoring': {'enabled': True,
        'allowedTools': [*tools, 'finish_drift'], **(authoring_extra or {})}}})
    assert response.status_code == 200, response.text
    if author is None:
        class Author(NotesAuthor):
            async def stream_structured_messages(self, messages, configuration, tools):
                async for event in super().stream_structured_messages(messages, configuration, tools):
                    if getattr(event, 'name', None) == 'write_file' and event.arguments['path'].endswith('SKILL.md'):
                        event.arguments['content'] = event.arguments['content'].replace('finish_drift]',
                            ', '.join([*extra_tools, 'finish_drift']) + ']')
                    yield event
        author = Author()
    monkeypatch.setattr(main, 'model_adapter', author)
    assert 'assistant_completed' in client.post(f"/api/roles/{role['id']}/messages", json={'content': '$create-drift-skill 创建长期小任务'}).text
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-drift-only', 'apiKey': 'fixture'}).json()['configuration']
    config = client.get(path).json()['config']
    response = client.put(path, json={'expectedRevision': config['revision'], 'config': {**config,
        'driftEnabled': True, 'driftModelConfigurationId': model['id'], 'consentRevision': 1,
        'driftToolGrants': tools, **(config_extra or {})}})
    assert response.status_code == 200, response.text
    return role['id'], path, now


async def settled(app, identifier, *, clock=True):
    if clock:
        await app.on_clock()
    async with asyncio.timeout(20):
        while True:
            await app.recover()
            state = app.query(identifier)
            if state['runs'] and all(run['runtimeAcknowledged'] for run in state['runs']):
                return state
            await asyncio.sleep(.01)


def test_real_single_send_shrinks_next_schema_and_stale_dispatch_then_commits_exact_progress(api, monkeypatch):
    client, roles, sessions, _ = api
    identifier, path, now = prepare(client, monkeypatch, extra_tools=('message_push',))

    class Send:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            names = {item['function']['name'] for item in tools}
            if self.requests == 1:
                assert 'message_push' in names
                name, arguments = 'activate_skill', {'skill_id': 'reading-notes'}
            elif self.requests == 2:
                name, arguments = 'message_push', {'message': '实际发送的单条长期进度', 'mood': '平静', 'thought': '一条有限进度'}
            elif self.requests == 3:
                receipt = json.loads(messages[-1].content[0].text)
                assert receipt['status'] == 'sent' and receipt['messageId']
                assert names == {'write_file', 'edit_file', 'finish_drift'}
                # A model can still fabricate an old schema call; dispatch must reject it.
                name, arguments = 'message_push', {'message': '不得重发', 'mood': '', 'thought': ''}
            elif self.requests == 4:
                assert messages[-1].is_error
                name, arguments = 'finish_drift', {'skill_used': 'reading-notes', 'one_line': '发送真实进度',
                    'next_action': '继续下一条', 'message_result': 'silent'}
            else:
                assert self.requests == 5 and messages[-1].is_error
                name, arguments = 'finish_drift', {'skill_used': 'reading-notes', 'one_line': '发送真实进度',
                    'next_action': '继续下一条', 'message_result': 'sent'}
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'send-{self.requests}', name, arguments),)))

    adapter = Send()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    try:
        now[0] += 1800
        state = asyncio.run(settled(main._proactive, identifier))
        assert adapter.requests == 5, state
        assert state['runs'][0]['status'] == 'finished_sent'
        messages = [item for item in sessions.list_messages(f'role:{identifier}') if item.content == '实际发送的单条长期进度']
        assert len(messages) == 1
        with main._proactive._transaction() as db:
            deliveries = db.execute('SELECT * FROM autonomy_deliveries WHERE role_id=?', (identifier,)).fetchall()
            assert len(deliveries) == 1 and deliveries[0]['dispatched'] == 1 and deliveries[0]['status'] == 'sent'
            assert json.loads(deliveries[0]['receipt'])['messageId'] == messages[0].id
        skill = client.get(path + '/drift-skills').json()['skills'][0]
        assert skill['progress']['runCount'] == 1 and skill['progress']['messageResult'] == 'sent'
        assert skill['progress']['nextAction'] == '继续下一条'
    finally:
        asyncio.run(main._proactive.close())


@pytest.mark.parametrize('outcome', ['failed', 'uncertain'])
def test_real_local_delivery_failure_keeps_single_claim_and_exact_non_success_finish_after_restart(api, monkeypatch, outcome):
    client, roles, sessions, _ = api
    role, path, now = prepare(client, monkeypatch, extra_tools=('message_push',))
    app = main._proactive
    if outcome == 'failed':
        with app._transaction() as db:
            db.execute("""CREATE TRIGGER reject_fixture_drift BEFORE INSERT ON messages
                WHEN json_extract(NEW.metadata_json,'$.internalEvent')='drift'
                BEGIN SELECT RAISE(ABORT,'fixture rejects canonical desktop insert'); END""")
    else:
        class DisconnectedTransport:
            async def deliver(self, turn, intent_id, draft):
                # A real synthetic localhost peer accepts the connection and
                # closes without a delivery receipt. No success is injected.
                closed = asyncio.Event()
                async def peer(reader, writer):
                    try:
                        await reader.read(1)
                    finally:
                        writer.close()
                        await writer.wait_closed()
                        closed.set()
                server = await asyncio.start_server(peer, '127.0.0.1', 0)
                writer = None
                try:
                    reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
                    writer.write(b'?')
                    await writer.drain()
                    await reader.readexactly(1)
                finally:
                    if writer is not None:
                        writer.close()
                        await writer.wait_closed()
                    server.close()
                    await server.wait_closed()
                    await closed.wait()
        app.drift_delivery = DisconnectedTransport()
    class Send:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            if self.requests == 1:
                name, arguments = 'activate_skill', {'skill_id': 'reading-notes'}
            elif self.requests == 2:
                name, arguments = 'message_push', {'message': 'only original claim', 'mood': '', 'thought': ''}
            elif self.requests == 3:
                assert json.loads(messages[-1].content[0].text)['status'] == outcome
                name, arguments = 'message_push', {'message': 'second attempt forbidden', 'mood': '', 'thought': ''}
            else:
                assert self.requests == 4 and messages[-1].is_error
                name, arguments = 'finish_drift', {'skill_used': 'reading-notes', 'one_line': '按真实失败事实收尾',
                    'next_action': '继续静默工作', 'message_result': outcome}
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'negative-{self.requests}', name, arguments),)))
    adapter = Send()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    try:
        now[0] += 1800
        state = asyncio.run(settled(app, role))
        assert adapter.requests == 4 and state['runs'][0]['status'] == 'finish_' + outcome, state
        assert not any(item.content in {'only original claim', 'second attempt forbidden'} for item in sessions.list_messages(f'role:{role}'))
        with app._transaction() as db:
            rows = db.execute('SELECT * FROM autonomy_deliveries WHERE role_id=?', (role,)).fetchall()
            assert len(rows) == 1 and rows[0]['dispatched'] == 1 and rows[0]['status'] == outcome
            reservation = db.execute('SELECT * FROM autonomy_storage_reservations WHERE operation_id=?', (state['runs'][0]['id'],)).fetchone()
            assert reservation['settled'] == 1
            assert (reservation['remaining_bytes'] == 0) == (outcome == 'failed')
        asyncio.run(app.close())
        monkeypatch.setattr(main, '_proactive', None)
        restored = main._proactive_application()
        asyncio.run(restored.recover())
        assert adapter.requests == 4
        assert restored.query(role)['runs'][0]['runtimeAcknowledged']
        progress = next(item for item in client.get(path + '/drift-skills').json()['skills'] if item['id'] == 'reading-notes')['progress']
        assert progress['runCount'] == 1 and progress['messageResult'] == outcome
    finally:
        asyncio.run(main._proactive.close())


def test_drift_attaches_only_original_granted_current_role_public_attachment(api, monkeypatch):
    client, _, sessions, _ = api
    role, path, now = prepare(client, monkeypatch, extra_tools=('message_push',))
    own = client.post(f'/api/roles/{role}/attachments', content=b'local owned attachment',
        headers={'x-attachment-filename': 'notes.txt', 'content-type': 'text/plain'}).json()
    foreign = client.post('/api/roles', json={'name': 'other', 'profile': {'profile': 'other'}}).json()['role']['id']
    other = client.post(f'/api/roles/{foreign}/attachments', content=b'other private attachment',
        headers={'x-attachment-filename': 'other.txt', 'content-type': 'text/plain'}).json()
    config = client.get(path).json()['config']
    saved = client.put(path, json={'expectedRevision': config['revision'], 'config': {**config, 'driftAttachmentIds': [own['id']]}})
    assert saved.status_code == 200, saved.text
    class Send:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            if self.requests == 1:
                name, arguments = 'activate_skill', {'skill_id': 'reading-notes'}
            elif self.requests in {2, 3}:
                if self.requests == 3:
                    assert messages[-1].is_error
                name, arguments = 'message_push', {'message': '本角色真实附件', 'mood': '', 'thought': '',
                    'attachment_ids': [other['id'] if self.requests == 2 else own['id']]}
            else:
                assert self.requests == 4 and json.loads(messages[-1].content[0].text)['status'] == 'sent'
                name, arguments = 'finish_drift', {'skill_used': 'reading-notes', 'one_line': '发送自己的附件',
                    'next_action': '下一项', 'message_result': 'sent'}
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'attachment-{self.requests}', name, arguments),)))
    adapter = Send()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    try:
        now[0] += 1800
        state = asyncio.run(settled(main._proactive, role))
        assert adapter.requests == 4 and state['runs'][0]['status'] == 'finished_sent', state
        message = next(item for item in sessions.list_messages(f'role:{role}') if item.content == '本角色真实附件')
        assert message.metadata['attachments'] == [own]
        assert all('path' not in item for item in message.metadata['attachments'])
    finally:
        asyncio.run(main._proactive.close())


def test_interrupted_receipt_write_recovery_uses_original_claim_without_replaying_send_or_finish(api, monkeypatch):
    client, _, sessions, _ = api
    role, _, now = prepare(client, monkeypatch, extra_tools=('message_push',))
    app = main._proactive
    with app._transaction() as db:
        db.execute("""CREATE TRIGGER reject_uncertain_receipt BEFORE UPDATE OF status ON autonomy_deliveries
            WHEN NEW.status='uncertain' BEGIN SELECT RAISE(ABORT,'receipt persistence interrupted'); END""")
    class LostReceipt:
        calls = 0
        async def deliver(self, turn, intent_id, draft):
            self.calls += 1
            raise ConnectionError('transport returned no receipt')
    delivery = LostReceipt()
    app.drift_delivery = delivery
    class Send:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            if self.requests == 1:
                name, arguments = 'activate_skill', {'skill_id': 'reading-notes'}
            else:
                assert self.requests == 2
                name, arguments = 'message_push', {'message': 'original interrupted claim', 'mood': '', 'thought': ''}
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'interrupted-{self.requests}', name, arguments),)))
    model = Send()
    monkeypatch.setattr(main, 'model_adapter', model)
    try:
        now[0] += 1800
        state = asyncio.run(settled(app, role))
        assert state['runs'][0]['status'] == 'aborted' and model.requests == 2 and delivery.calls == 1
        with app._transaction() as db:
            original = dict(db.execute('SELECT * FROM autonomy_deliveries WHERE role_id=?', (role,)).fetchone())
            assert original['status'] == 'dispatching' and original['dispatched'] == 1
            db.execute('DROP TRIGGER reject_uncertain_receipt')
        asyncio.run(app.close())
        monkeypatch.setattr(main, '_proactive', None)
        restored = main._proactive_application()
        asyncio.run(restored.recover())
        with restored._transaction() as db:
            recovered = dict(db.execute('SELECT * FROM autonomy_deliveries WHERE role_id=?', (role,)).fetchone())
            assert recovered['status'] == 'uncertain'
            assert recovered['intent_id'] == original['intent_id'] and recovered['dispatched'] == 1
            assert db.execute('SELECT COUNT(*) FROM autonomy_drift_progress WHERE role_id=?', (role,)).fetchone()[0] == 0
        assert model.requests == 2 and delivery.calls == 1
        assert not any(item.content == 'original interrupted claim' for item in sessions.list_messages(f'role:{role}'))
    finally:
        asyncio.run(main._proactive.close())
