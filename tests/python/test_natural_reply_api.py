"""Desktop natural messages traverse the real API and durable session path."""
import json

from backend.app import main
from backend.app.model_adapter import ModelResponseDone, ModelToolCall
from backend.app.models import RoleUpdateInput
from test_api import configure_session_api


def test_desktop_natural_reply_commits_each_message_without_persisting_suffix(tmp_path, monkeypatch, local_platform_only):
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            assert '即时聊天' in messages[0]['content']
            assert 'finish_reply' in {tool['function']['name'] for tool in tools}
            yield ModelToolCall('finish', 'finish_reply', {'messages': ['第一段\n正常换行', '第二段']})
            yield ModelResponseDone('tool_use')
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, Model())
    main.store.update(role.id, RoleUpdateInput(name=role.name, profile=role.profile,
        agentConfig=role.agentConfig.model_copy(update={'naturalChatEnabled': True})))
    pauses = []
    async def pause(previous):
        rows = [item for item in sessions.list_messages(f'role:{role.id}') if item.role == 'assistant' and item.messageType == 'text']
        pauses.append(previous)
        assert [item.content for item in rows] == ['第一段\n正常换行']
        assert all('replySequence' not in item.metadata for item in rows)
    monkeypatch.setattr(main, 'wait_between_messages', pause)
    response = client.post(f'/api/roles/{role.id}/messages', json={'content': '你好'})
    assert response.status_code == 200
    blocks = response.text.strip().split('\n\n')
    events = [(block.split('\n')[0].removeprefix('event: '),
               json.loads(next(line.removeprefix('data: ') for line in block.split('\n') if line.startswith('data: ')))) for block in blocks]
    spoken = [(name, data['message']) for name, data in events if name in {'assistant_message_committed', 'assistant_completed'}]
    assert [name for name, _ in spoken] == ['assistant_message_committed', 'assistant_completed']
    assert [message['content'] for _, message in spoken] == ['第一段\n正常换行', '第二段']
    assert len({message['id'] for _, message in spoken}) == 2
    assert pauses == ['第一段\n正常换行']
    assert not any(name in {'tool_execution_start', 'assistant_delta', 'assistant_failed'} for name, _ in events)
    history = sessions.runtime_messages(f'role:{role.id}')
    assert [item.content for item in history if item.role == 'assistant'] == ['第一段\n正常换行', '第二段']


def test_desktop_accepted_supplement_replaces_only_unsent_tail(tmp_path, monkeypatch, local_platform_only):
    import asyncio
    from types import SimpleNamespace
    from backend.app.models import SendMessageInput
    from backend.app.agent_runtime.runtime import RuntimeManager

    class Model:
        calls = 0
        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            words = ['旧首段', '旧尾段'] if self.calls == 1 else ['新回复']
            yield ModelToolCall(f'finish-{self.calls}', 'finish_reply', {'messages': words})
            yield ModelResponseDone('tool_use')
    role, sessions, _ = configure_session_api(tmp_path, monkeypatch, Model())
    main.store.update(role.id, RoleUpdateInput(name=role.name, profile=role.profile,
        agentConfig=role.agentConfig.model_copy(update={'naturalChatEnabled': True})))
    monkeypatch.setattr(main, 'runtime_manager', RuntimeManager(sessions))
    monkeypatch.setattr(main, '_desktop_reply_plans', {})
    monkeypatch.setattr(main, '_ordinary_reply_runs', {})
    waiting, release, invalidated = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = main.ReplyPlan.invalidate
    def invalidate(plan):
        original(plan)
        invalidated.set()
    monkeypatch.setattr(main.ReplyPlan, 'invalidate', invalidate)
    async def pause(_):
        waiting.set()
        await release.wait()
    async def connected():
        return False
    monkeypatch.setattr(main, 'wait_between_messages', pause)
    async def consume(response):
        return [chunk async for chunk in response.body_iterator]
    async def scenario():
        try:
            first = await main.send_role_message(role.id, SendMessageInput(content='第一条输入'), SimpleNamespace(is_disconnected=connected))
            consuming = asyncio.create_task(consume(first))
            await waiting.wait()
            second = asyncio.create_task(main.send_role_message(role.id, SendMessageInput(content='补充输入'), SimpleNamespace(is_disconnected=connected)))
            await invalidated.wait()
            assert [row.content for row in sessions.list_messages(f'role:{role.id}') if row.role == 'user'] == ['第一条输入', '补充输入']
            release.set()
            old_events = await consuming
            assert any('assistant_completed' in item for item in old_events)
            await consume(await second)
        finally:
            release.set()
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(asyncio.wait_for(scenario(), 10))
    assert [row.content for row in sessions.list_messages(f'role:{role.id}') if row.role == 'assistant' and row.messageType == 'text'] == ['旧首段', '新回复']
