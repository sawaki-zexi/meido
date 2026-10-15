"""A real owner conversation creates and receives an owned time task."""
import asyncio
import json
import pytest

from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401


def test_http_create_list_due_delivery_next_history_and_cancel(api, monkeypatch):  # noqa: F811
    from backend.app import main
    client, _roles, sessions, _tasks = api
    now = [1780000000.0]
    monkeypatch.setattr(main, '_schedule_clock', lambda: now[0])
    monkeypatch.setattr(main, '_schedules', None)
    receipts = []
    action = ['schedule']
    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            last_user = max(i for i, item in enumerate(messages) if item['role'] == 'user')
            results = [item for item in messages[last_user + 1:] if item['role'] == 'tool']
            if action[0] == 'history':
                assert 'due-known-158' in str(messages)
                yield ModelTextDelta('看到到时消息')
            elif not results:
                assert action[0] in {item['function']['name'] for item in tools}
                arguments = {'tier': 'instant', 'trigger': 'after', 'when': '10s', 'message': 'due-known-158'} if action[0] == 'schedule' else {} if action[0] == 'list_schedules' else {'id': receipts[0]['id']}
                yield ModelToolCall('time-call', action[0], arguments)
            else:
                receipts.append(json.loads(results[-1]['content']))
                yield ModelTextDelta('完成')
    monkeypatch.setattr(main, 'model_adapter', Adapter())
    role = client.post('/api/roles', json={'name': '时间任务', 'profile': {'profile': '测试'},
        'agentConfig': {'enabledTools': ['schedule', 'list_schedules', 'cancel_schedule']}}).json()['role']
    path = f"/api/roles/{role['id']}/messages"
    response = client.post(path, json={'content': '十秒后提醒'})
    assert 'assistant_completed' in response.text, response.text
    assert receipts[0]['nextAt'] == now[0] + 10
    action[0] = 'list_schedules'
    assert 'assistant_completed' in client.post(path, json={'content': '查询时间任务'}).text
    assert receipts[-1]['schedules'][0]['id'] == receipts[0]['id']
    now[0] += 10
    asyncio.run(main._schedule_application().tick())
    messages = sessions.list_messages(f"role:{role['id']}")
    delivered = [item for item in messages if item.metadata.get('internalEvent') == 'schedule']
    assert len(delivered) == 1 and delivered[0].content == 'due-known-158'
    assert delivered[0].metadata['deliveryStatus'] == 'sent' and delivered[0].runId is None
    action[0] = 'history'
    assert 'assistant_completed' in client.post(path, json={'content': '读到时消息'}).text
    action[0] = 'cancel_schedule'
    assert 'assistant_completed' in client.post(path, json={'content': '取消'}).text
    assert receipts[-1]['cancelled'] == 1


def test_soft_outlives_parent_with_spawn_disabled_and_reads_a_real_file_once(api, monkeypatch):  # noqa: F811
    import sqlite3
    import time
    from backend.app import main
    client, roles, sessions, tasks = api
    now = [1780000000.0]
    monkeypatch.setattr(main, '_schedule_clock', lambda: now[0])
    monkeypatch.setattr(main, '_schedules', None)
    child_requests = []
    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            child = 'independent delegated task' in str(messages[0])
            results = [item for item in messages if item['role'] == 'tool']
            if child:
                child_requests.append(messages)
                assert 'parent-secret-158' not in str(messages)
                assert {item['function']['name'] for item in tools} == {'read_file'}
                if not results:
                    yield ModelToolCall('soft-read', 'read_file', {'path': 'time-evidence.txt'})
                else:
                    assert 'file-proof-158' in str(results[-1])
                    yield ModelTextDelta('soft-delivered-158')
            elif not results:
                yield ModelToolCall('schedule-soft', 'schedule', {'tier': 'soft', 'trigger': 'after',
                    'when': '1s', 'prompt': 'Read time-evidence.txt and return its proof.'})
            else:
                yield ModelTextDelta('父回合已结束')
    monkeypatch.setattr(main, 'model_adapter', Adapter())
    role = client.post('/api/roles', json={'name': '到时执行', 'profile': {'profile': '测试'},
        'agentConfig': {'spawn': {'enabled': False}, 'enabledTools': ['schedule', 'read_file']}}).json()['role']
    (roles.workspace_path(role['id']) / 'time-evidence.txt').write_text('file-proof-158', encoding='utf-8')
    response = client.post(f"/api/roles/{role['id']}/messages", json={'content': 'parent-secret-158'})
    assert 'assistant_completed' in response.text
    now[0] += 1
    async def run():
        deadline = time.monotonic() + 4
        while not any(item.content == 'soft-delivered-158' for item in sessions.list_messages(f"role:{role['id']}")):
            await main._schedule_application().tick()
            assert time.monotonic() < deadline
            await asyncio.sleep(.01)
        await main._schedule_application().tick()
    asyncio.run(run())
    messages = sessions.list_messages(f"role:{role['id']}")
    assert len([item for item in messages if item.content == 'soft-delivered-158']) == 1
    assert len([item for item in messages if item.role == 'user']) == 1
    assert len(child_requests) == 2
    with sqlite3.connect(sessions.database_path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM agent_runs WHERE role_id=?', (role['id'],)).fetchone()[0] == 1
    assert tasks.pending_completions() == ()


@pytest.mark.parametrize('field', ['request_time', 'received_at', 'role_id', 'run_id', 'source'])
def test_model_cannot_inject_host_schedule_authority_or_time(api, monkeypatch, field):  # noqa: F811
    from backend.app import main
    client, _, sessions, _ = api
    receipts = []
    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            if not any(item['role'] == 'tool' for item in messages):
                yield ModelToolCall('forged', 'schedule', {'tier': 'instant', 'trigger': 'after', 'when': '1s',
                    'message': 'must-not-deliver', field: 'forged-host-fact'})
            else:
                receipts.append(messages[-1])
                yield ModelTextDelta('已拒绝')
    monkeypatch.setattr(main, 'model_adapter', Adapter())
    role = client.post('/api/roles', json={'name': '输入边界', 'profile': {'profile': '测试'},
        'agentConfig': {'enabledTools': ['schedule']}}).json()['role']
    response = client.post(f"/api/roles/{role['id']}/messages", json={'content': '输入边界'})
    assert 'assistant_completed' in response.text
    stored = [m for m in sessions.list_messages(f"role:{role['id']}") if m.messageType == 'tool_result']
    assert len(stored) == 1 and stored[0].isError
    assert not any(m.metadata.get('internalEvent') == 'schedule' for m in sessions.list_messages(f"role:{role['id']}"))


def test_revoke_schedule_stops_existing_plan_while_management_remains_available(api, monkeypatch):  # noqa: F811
    from backend.app import main
    client, _, sessions, _ = api
    now, receipts, action = [1780000000.0], [], ['schedule']
    monkeypatch.setattr(main, '_schedule_clock', lambda: now[0])
    monkeypatch.setattr(main, '_schedules', None)
    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            last_user = max(i for i, item in enumerate(messages) if item['role'] == 'user')
            results = [item for item in messages[last_user + 1:] if item['role'] == 'tool']
            if not results:
                args = {'tier': 'instant', 'trigger': 'every', 'when': '10s', 'message': 'revoked'} if action[0] == 'schedule' else {} if action[0] == 'list_schedules' else {'id': receipts[0]['id']}
                yield ModelToolCall('call', action[0], args)
            else:
                receipts.append(json.loads(results[-1]['content']))
                yield ModelTextDelta('完成')
    monkeypatch.setattr(main, 'model_adapter', Adapter())
    role = client.post('/api/roles', json={'name': '关闭许可', 'profile': {'profile': '测试'},
        'agentConfig': {'enabledTools': ['schedule', 'list_schedules', 'cancel_schedule']}}).json()['role']
    path = f"/api/roles/{role['id']}/messages"
    assert 'assistant_completed' in client.post(path, json={'content': '创建'}).text
    changed = client.put(f"/api/roles/{role['id']}", json={'name': role['name'], 'profile': role['profile'], 'agentConfig': {'enabledTools': ['list_schedules', 'cancel_schedule']}})
    assert changed.status_code == 200
    now[0] += 10
    asyncio.run(main._schedule_application().tick())
    action[0] = 'list_schedules'
    assert 'assistant_completed' in client.post(path, json={'content': '查询'}).text
    assert receipts[-1]['schedules'][0]['planStatus'] == 'revoked'
    assert receipts[-1]['schedules'][0]['nextAt'] is None
    action[0] = 'cancel_schedule'
    assert 'assistant_completed' in client.post(path, json={'content': '取消'}).text
    assert receipts[-1]['cancelled'] == 1
    assert not any(m.metadata.get('internalEvent') == 'schedule' for m in sessions.list_messages(f"role:{role['id']}"))


def test_relative_time_is_captured_before_history_preparation_and_model_delay(api, monkeypatch):  # noqa: F811
    from backend.app import main
    client, _, sessions, _ = api
    now, receipts = [1780000000.0], []
    received = now[0]
    monkeypatch.setattr(main, '_schedule_clock', lambda: now[0])
    monkeypatch.setattr(main, '_schedules', None)
    original = main.session_manager.role_and_history
    def delayed(*args):
        now[0] += 20
        return original(*args)
    monkeypatch.setattr(main.session_manager, 'role_and_history', delayed)
    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            if not any(m['role'] == 'tool' for m in messages):
                now[0] += 20
                yield ModelToolCall('delay', 'schedule', {'tier': 'instant', 'trigger': 'after', 'when': '10s', 'message': 'received-based'})
            else:
                receipts.append(json.loads(messages[-1]['content']))
                yield ModelTextDelta('创建')
    monkeypatch.setattr(main, 'model_adapter', Adapter())
    role = client.post('/api/roles', json={'name': '接收时间', 'profile': {'profile': '测试'},
        'agentConfig': {'enabledTools': ['schedule']}}).json()['role']
    response = client.post(f"/api/roles/{role['id']}/messages", json={'content': '十秒后'})
    assert 'assistant_completed' in response.text
    assert receipts[0]['nextAt'] == received + 10
    assert receipts[0]['receivedAt'] == received and receipts[0]['receivedTimeSource'] == 'hostReceivedAt'
    asyncio.run(main._schedule_application().tick())
    assert any(m.content == 'received-based' for m in sessions.list_messages(f"role:{role['id']}"))
