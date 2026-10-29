"""A role can push to its own desktop without configuring a channel."""
import json
import sqlite3
from io import BytesIO

import pytest
from PIL import Image

from backend.app.model_adapter import ModelResponseDone, ModelTextDelta, ModelToolCall
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401


def test_desktop_only_http_push_reaches_saved_role_session_and_next_model_request(api, monkeypatch):  # noqa: F811
    from backend.app import main
    client, _roles, sessions, _tasks = api
    requests = []
    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append((messages, tools))
            if not any(item['role'] == 'tool' for item in messages):
                assert 'message_push' in {tool['function']['name'] for tool in tools}
                yield ModelToolCall('desktop-push-call', 'message_push', {
                    'channel': 'desktop', 'chat_id': 'owner', 'message': '桌面独立推送证据-167'})
                yield ModelResponseDone("tool_use")
            else:
                receipt = json.loads(messages[-1]['content'])
                assert receipt['status'] == 'sent' and receipt['messageId']
                yield ModelTextDelta('推送已保存在桌面')
    monkeypatch.setattr(main, 'model_adapter', Adapter())
    role = client.post('/api/roles', json={'name': '桌面推送', 'profile': {'profile': '测试设定'},
        'agentConfig': {'enabledTools': ['message_push']}}).json()['role']
    assert main.multichannel_store.list_role_accounts(role['id']) == []
    catalog = client.get(f"/api/roles/{role['id']}/tools").json()
    declaration = next(item for item in catalog['tools'] if item['name'] == 'message_push')
    assert declaration['enabled'] and declaration['modelVisible']
    response = client.post(f"/api/roles/{role['id']}/messages", json={'content': '推送一条桌面消息'})
    assert 'assistant_completed' in response.text, response.text
    assert len(requests) == 2
    stored = sessions.list_messages(f"role:{role['id']}")
    pushes = [item for item in stored if item.content == '桌面独立推送证据-167']
    assert len(pushes) == 1 and pushes[0].role == 'assistant'
    assert pushes[0].messageType == 'desktop_push' and pushes[0].runId is None
    assert len([item for item in stored if item.role == 'user']) == 1
    with sqlite3.connect(sessions.database_path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM agent_runs WHERE role_id=?', (role['id'],)).fetchone()[0] == 1
    public = client.get(f"/api/roles/{role['id']}/session").json()
    assert any(item['id'] == pushes[0].id for item in public['messages'])


@pytest.fixture
def desktop_peer(api, monkeypatch):  # noqa: F811
    from backend.app import main
    client, roles, sessions, _tasks = api
    arguments = {'channel': 'desktop', 'chat_id': 'owner'}
    replies = []
    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            if not any(item['role'] == 'tool' for item in messages):
                yield ModelToolCall('desktop-media', 'message_push', dict(arguments))
                yield ModelResponseDone("tool_use")
            else:
                replies.append(messages[-1]['content'])
                yield ModelTextDelta('已处理桌面推送请求')
    monkeypatch.setattr(main, 'model_adapter', Adapter())
    role = client.post('/api/roles', json={'name': '桌面媒体', 'profile': {'profile': '测试设定'},
        'agentConfig': {'enabledTools': ['message_push']}}).json()['role']
    image = BytesIO()
    Image.new('RGB', (3, 2), '#167abc').save(image, format='PNG')
    picture = client.post(f"/api/roles/{role['id']}/attachments", content=image.getvalue(),
        headers={'content-type': 'image/png', 'x-attachment-filename': 'push.png'}).json()
    document = client.post(f"/api/roles/{role['id']}/attachments", content=b'desktop-file-known-167',
        headers={'content-type': 'text/plain', 'x-attachment-filename': 'push.txt'}).json()
    return client, roles, sessions, role, arguments, replies, picture, document


@pytest.mark.parametrize('mode', ['image', 'file', 'mixed', 'deduplicate'])
def test_real_uploaded_media_is_pushed_once_and_publicly_downloadable(desktop_peer, mode):
    client, _roles, sessions, role, arguments, replies, picture, document = desktop_peer
    expected = [picture, document] if mode == 'mixed' else [document] if mode == 'file' else [picture]
    if mode == 'mixed':
        arguments.update({'message': '图片与文件', 'attachments': [picture['id'], document['id']]})
    elif mode == 'deduplicate':
        arguments.update({'image': picture['id'], 'attachments': [picture['id'], picture['id']]})
    else:
        arguments[mode] = expected[0]['id']
    response = client.post(f"/api/roles/{role['id']}/messages", json={'content': '推到桌面'})
    assert 'assistant_completed' in response.text, response.text
    receipt = json.loads(replies[0])
    assert receipt['status'] == 'sent'
    public = client.get(f"/api/roles/{role['id']}/session").json()
    message = next(item for item in public['messages'] if item['id'] == receipt['messageId'])
    assert message['content'] == ('图片与文件' if mode == 'mixed' else '')
    assert message['metadata']['attachments'] == expected
    assert not any(field in json.dumps(message) for field in ('"path"', 'desktop-file-known-167', 'data:image'))
    for item in expected:
        download = client.get(f"/api/roles/{role['id']}/attachments/{item['id']}")
        assert download.status_code == 200 and len(download.content) == item['size']
    assert len([item for item in sessions.list_messages(f"role:{role['id']}") if item.messageType == 'desktop_push']) == 1


@pytest.mark.parametrize('mode', ['foreign', 'missing', 'wrong_type', 'conflicting_type', 'external', 'other_chat', 'empty'])
def test_invalid_media_and_destinations_never_partially_write(desktop_peer, mode):
    client, _roles, sessions, role, arguments, _replies, picture, document = desktop_peer
    if mode == 'foreign':
        another = client.post('/api/roles', json={'name': '另一角色', 'profile': {'profile': '其他设定'}}).json()['role']
        foreign = client.post(f"/api/roles/{another['id']}/attachments", content=b'foreign-private-file',
            headers={'content-type': 'text/plain', 'x-attachment-filename': 'foreign.txt'}).json()
        arguments['attachments'] = [picture['id'], foreign['id']]
    elif mode == 'missing':
        arguments['attachments'] = [picture['id'], 'a' * 32]
    elif mode == 'wrong_type':
        arguments['image'] = document['id']
    elif mode == 'conflicting_type':
        arguments.update({'image': picture['id'], 'file': picture['id']})
    elif mode == 'external':
        arguments.update({'channel': 'qq', 'message': '不得外发'})
    elif mode == 'other_chat':
        arguments.update({'chat_id': 'other-role', 'message': '不得改投'})
    response = client.post(f"/api/roles/{role['id']}/messages", json={'content': '尝试非法推送'})
    assert 'assistant_completed' in response.text, response.text
    messages = sessions.list_messages(f"role:{role['id']}")
    assert not any(item.messageType == 'desktop_push' for item in messages)
    result = next(item for item in messages if item.messageType == 'tool_result')
    assert result.isError
    assert 'foreign-private-file' not in result.content


@pytest.mark.parametrize('terminal', ['failed', 'stopped'])
def test_committed_push_survives_a_later_parent_failure_or_real_http_stop(api, monkeypatch, terminal):  # noqa: F811
    import asyncio
    import threading
    import httpx
    from backend.app import main

    client, _roles, sessions, _tasks = api
    ready = threading.Event()
    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            if not any(item['role'] == 'tool' for item in messages):
                yield ModelToolCall('committed-push', 'message_push', {
                    'channel': 'desktop', 'chat_id': 'owner', 'message': 'durable-before-parent-terminal'})
                yield ModelResponseDone("tool_use")
            else:
                assert json.loads(messages[-1]['content'])['status'] == 'sent'
                ready.set()
                if terminal == 'failed':
                    raise RuntimeError('controlled parent failure')
                await asyncio.Event().wait()
    monkeypatch.setattr(main, 'model_adapter', Adapter())
    role = client.post('/api/roles', json={'name': '终态保留', 'profile': {'profile': '测试'},
        'agentConfig': {'enabledTools': ['message_push']}}).json()['role']

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://fixture') as http:
            request = asyncio.create_task(http.post(f"/api/roles/{role['id']}/messages", json={'content': '先投递再等待'}))
            assert await asyncio.to_thread(ready.wait, 3)
            if terminal == 'stopped':
                with sqlite3.connect(sessions.database_path) as connection:
                    run_id = connection.execute('SELECT run_id FROM agent_runs WHERE role_id=?', (role['id'],)).fetchone()[0]
                stopped = await http.post(f"/api/roles/{role['id']}/runs/{run_id}/stop")
                assert stopped.status_code == 200, stopped.text
            if terminal == 'failed':
                response = await asyncio.wait_for(request, 4)
                assert 'assistant_failed' in response.text
            else:
                # The in-process ASGI transport asserts when Stop terminates
                # an already open response; the public Stop receipt and saved
                # run, rather than a fabricated SSE completion, are the proof.
                response = (await asyncio.wait_for(asyncio.gather(request, return_exceptions=True), 4))[0]
                assert isinstance(response, (httpx.Response, AssertionError))
                assert sessions.get_run(run_id).status == 'cancelled'
    asyncio.run(run())
    messages = sessions.list_messages(f"role:{role['id']}")
    pushed = [message for message in messages if message.messageType == 'desktop_push']
    assert len(pushed) == 1 and pushed[0].status == 'completed' and pushed[0].runId is None
    assert pushed[0].content == 'durable-before-parent-terminal'
    assert len([item for item in messages if item.role == 'user']) == 1


@pytest.mark.parametrize('call_count', [1, 2])
def test_next_owner_turn_and_reopened_store_preserve_real_push_receipts(api, monkeypatch, call_count):  # noqa: F811
    from backend.app import main
    from backend.app.agent_runtime.types import ToolResultMessage
    from backend.app.session_store import SessionStore

    client, _roles, sessions, _tasks = api
    requests = []
    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            receipts = [item for item in messages if item['role'] == 'tool']
            if not receipts:
                for index in range(call_count):
                    yield ModelToolCall(f'push-{index}', 'message_push', {
                        'channel': 'desktop', 'chat_id': 'owner', 'message': f'confirmed-push-{index}'})
                yield ModelResponseDone("tool_use")
            else:
                assert len(receipts) == call_count
                assert all(json.loads(item['content'])['status'] == 'sent' for item in receipts)
                yield ModelTextDelta('真实回执保留')
    monkeypatch.setattr(main, 'model_adapter', Adapter())
    role = client.post('/api/roles', json={'name': '回执重放', 'profile': {'profile': '测试'},
        'agentConfig': {'enabledTools': ['message_push']}}).json()['role']
    first = client.post(f"/api/roles/{role['id']}/messages", json={'content': '投递'})
    assert 'assistant_completed' in first.text
    reopened = SessionStore(sessions.database_path)
    replay = reopened.runtime_messages(f"role:{role['id']}")
    results = [item for item in replay if isinstance(item, ToolResultMessage)]
    assert len(results) == call_count and all(not item.is_error and json.loads(item.content)['status'] == 'sent' for item in results)
    # Provider protocol keeps every result directly after its full call group;
    # independent push messages follow it, without changing SQLite chronology.
    call_index = next(index for index, item in enumerate(replay) if getattr(item, 'tool_calls', ()))
    assert all(isinstance(item, ToolResultMessage) for item in replay[call_index + 1:call_index + 1 + call_count])
    following = client.post(f"/api/roles/{role['id']}/messages", json={'content': '继续读取历史'})
    assert 'assistant_completed' in following.text, following.text
    assert len(requests) == 3
    assert len([item for item in sessions.list_messages(f"role:{role['id']}") if item.messageType == 'desktop_push']) == call_count


def test_workspace_file_tool_output_is_sent_without_attachment_id_and_streamed_immediately(api, monkeypatch):  # noqa: F811
    from backend.app import main
    client, _roles, sessions, _tasks = api
    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            results = [item for item in messages if item['role'] == 'tool']
            if not results:
                assert f"attachment_id={uploaded['id']}" in str(messages[-1]['content'])
                yield ModelToolCall('write', 'write_file', {'path': 'report.html', 'content': '<h1>owned-report</h1>'})
                yield ModelResponseDone('tool_use')
            elif len(results) == 1:
                yield ModelToolCall('send', 'message_push', {'channel': 'desktop', 'chat_id': 'owner', 'file': 'report.html'})
                yield ModelResponseDone('tool_use')
            else:
                assert json.loads(results[-1]['content'])['status'] == 'sent'
                yield ModelTextDelta('报告已附上')
    monkeypatch.setattr(main, 'model_adapter', Adapter())
    role = client.post('/api/roles', json={'name': '文件输出', 'profile': {'profile': '测试'}}).json()['role']
    uploaded = client.post(f"/api/roles/{role['id']}/attachments", content=b'input notes',
        headers={'content-type': 'text/plain', 'x-attachment-filename': 'input.txt'}).json()
    response = client.post(f"/api/roles/{role['id']}/messages", json={'content': '创建并发送报告', 'attachments': [uploaded['id']]})
    assert 'event: assistant_message_committed' in response.text, response.text
    assert response.text.index('event: assistant_message_committed') < response.text.index('报告已附上')
    saved = next(item for item in sessions.list_messages(f"role:{role['id']}") if item.messageType == 'desktop_push')
    attachment = saved.metadata['attachments'][0]
    (_roles.workspace_path(role['id']) / 'report.html').unlink()
    download = client.get(f"/api/roles/{role['id']}/attachments/{attachment['id']}")
    assert download.content == b'<h1>owned-report</h1>'
    assert download.headers['content-disposition'].startswith('attachment;')
    assert download.headers['x-content-type-options'] == 'nosniff'
    assert '"path"' not in json.dumps(attachment)
