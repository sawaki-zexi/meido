"""A role can push to its own desktop without configuring a channel."""
import json
import sqlite3

from backend.app.model_adapter import ModelTextDelta, ModelToolCall
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
