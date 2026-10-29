"""Ordinary QQ admission/model/tools/delivery, with only remote I/O replaced."""
import asyncio
import pytest

from backend.app import main
from backend.app.channels.base import PlatformRejectedError
from backend.app.model_adapter import ModelResponseDone, ModelToolCall
from backend.app.models import RoleUpdateInput
from test_meme_turn import meme_turn, payload


def enable(monkeypatch, state):
    role = state.role
    state.roles.update(role.id, RoleUpdateInput(name=role.name, profile=role.profile,
        agentConfig=role.agentConfig.model_copy(update={"naturalChatEnabled": True})))
    monkeypatch.setattr(main, '_channel_reply_plans', {})
    monkeypatch.setattr(main, '_channel_reply_locks', {})
    monkeypatch.setattr(main, '_ordinary_reply_runs', {})
    async def no_wait(_):
        pass
    monkeypatch.setattr(main, 'wait_between_messages', no_wait)


@pytest.mark.parametrize('failure', [None, 'failed', 'uncertain'])
def test_qq_reply_sequences_commit_only_confirmed_prefix(meme_turn, monkeypatch, failure):
    state = meme_turn
    enable(monkeypatch, state)
    delivered = []
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            assert 'finish_reply' in {item['function']['name'] for item in tools}
            assert '即时聊天' in messages[0]['content']
            yield ModelToolCall('finish', 'finish_reply', {'messages': ['第一条', '第二条', '第三条']})
            yield ModelResponseDone('tool_use')
    async def transport(operation, params):
        if operation in {'get_friend_list', 'get_group_list', 'get_group_member_list'}:
            return {'data': []}
        assert operation == 'send_group_msg'
        text = ''.join(item['data']['text'] for item in params['message'] if item['type'] == 'text')
        delivered.append(text)
        # The unstarted suffix must not be durable while the platform is sending.
        thread = state.channels.get_thread_by_chat(state.role.id, state.adapter.account_id, '100', chat_type='group')
        if len(delivered) < 3:
            assert all('第三条' not in item.content for item in state.sessions.list_messages(thread.session_key))
        if len(delivered) == 2 and failure == 'failed':
            raise PlatformRejectedError('reject second')
        if len(delivered) == 2 and failure == 'uncertain':
            return {}
        return {'message_id': str(100 + len(delivered))}
    monkeypatch.setattr(main, 'model_adapter', Model())
    monkeypatch.setattr(state.adapter, '_transport', transport)
    async def scenario():
        try:
            await main._dispatch_channel_payload(state.adapter.account_id, payload('natural', image=False), source_adapter=state.adapter)
        finally:
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(scenario())
    assert delivered == (['第一条', '第二条', '第三条'] if failure is None else ['第一条', '第二条'])
    thread = state.channels.get_thread_by_chat(state.role.id, state.adapter.account_id, '100', chat_type='group')
    replies = [item for item in state.sessions.list_messages(thread.session_key) if item.role == 'assistant' and item.messageType == 'text']
    assert [item.content for item in replies] == delivered
    assert [item.metadata['deliveryStatus'] for item in replies] == (['sent'] * 3 if failure is None else ['sent', failure])
    history = state.sessions.runtime_messages(thread.session_key)
    assert [item.content for item in history if item.role == 'assistant'] == (delivered if failure is None else ['第一条'])
    assert all('replySequence' not in item.metadata for item in replies)


def test_same_sender_supplement_discards_only_unsent_suffix(meme_turn, monkeypatch):
    state = meme_turn
    enable(monkeypatch, state)
    waiting, release, invalidated = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_invalidate = main.ReplyPlan.invalidate
    def invalidate(plan):
        original_invalidate(plan)
        invalidated.set()
    monkeypatch.setattr(main.ReplyPlan, "invalidate", invalidate)
    class Model:
        calls = 0
        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            words = ['旧第一条', '旧第二条'] if self.calls == 1 else ['新回复']
            yield ModelToolCall(f'finish-{self.calls}', 'finish_reply', {'messages': words})
            yield ModelResponseDone('tool_use')
    async def pause(_):
        waiting.set()
        await release.wait()
    monkeypatch.setattr(main, 'model_adapter', Model())
    monkeypatch.setattr(main, 'wait_between_messages', pause)
    async def scenario():
        try:
            first = asyncio.create_task(main._dispatch_channel_payload(state.adapter.account_id, payload('old', image=False), source_adapter=state.adapter))
            await waiting.wait()
            second = asyncio.create_task(main._dispatch_channel_payload(state.adapter.account_id, payload('new', image=False), source_adapter=state.adapter))
            await invalidated.wait()
            release.set()
            await asyncio.gather(first, second)
        finally:
            release.set()
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(asyncio.wait_for(scenario(), 10))
    sent = [''.join(part['data']['text'] for part in params['message'] if part['type'] == 'text')
        for operation, params in state.operations if operation == 'send_group_msg']
    assert sent == ['旧第一条', '新回复']


def test_supplement_finishes_started_tool_but_never_starts_next_batch_call(meme_turn, monkeypatch):
    state = meme_turn
    enable(monkeypatch, state)
    started, invalidated, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_invalidate = main.ReplyPlan.invalidate
    def invalidate(plan):
        original_invalidate(plan)
        invalidated.set()
    monkeypatch.setattr(main.ReplyPlan, 'invalidate', invalidate)
    calls = []
    from backend.app.agent_runtime.tools import AgentToolResult
    async def search(_tool, arguments, context, on_update=None):
        calls.append(arguments['query'])
        started.set()
        await release.wait()
        return AgentToolResult('completed actual search')
    monkeypatch.setattr(main.WebSearchTool, 'execute', search)
    class Model:
        requests = 0
        async def stream_structured_messages(self, messages, configuration, tools):
            self.requests += 1
            if self.requests == 1:
                yield ModelToolCall('first', 'web_search', {'query': 'first'})
                yield ModelToolCall('second', 'web_search', {'query': 'must not run'})
            else:
                yield ModelToolCall('finish', 'finish_reply', {'messages': ['已按补充回复']})
            yield ModelResponseDone('tool_use')
    monkeypatch.setattr(main, 'model_adapter', Model())
    async def scenario():
        try:
            first = asyncio.create_task(main._dispatch_channel_payload(state.adapter.account_id, payload('tool-old', image=False), source_adapter=state.adapter))
            await started.wait()
            second = asyncio.create_task(main._dispatch_channel_payload(state.adapter.account_id, payload('tool-new', image=False), source_adapter=state.adapter))
            await invalidated.wait()
            release.set()
            await asyncio.gather(first, second, return_exceptions=True)
        finally:
            release.set()
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(asyncio.wait_for(scenario(), 10))
    assert calls == ['first']
    thread = state.channels.get_thread_by_chat(state.role.id, state.adapter.account_id, '100', chat_type='group')
    messages = state.sessions.list_messages(thread.session_key)
    assert any(item.role == 'tool' and item.content == 'completed actual search' for item in messages)
    assert len([item for item in messages if item.role == 'user']) == 2
    assert [item.content for item in messages if item.role == 'assistant' and item.messageType == 'text' and item.status == 'completed'] == ['已按补充回复']


@pytest.mark.parametrize('tail', [False, True])
def test_stop_during_platform_request_keeps_receipt_and_never_sends_tail(meme_turn, monkeypatch, tail):
    state = meme_turn
    enable(monkeypatch, state)
    entered, release = asyncio.Event(), asyncio.Event()
    sent, memory, scenes = [], [], []
    commit = main.channel_memory_maintenance.commit_turn
    async def record_memory(thread, owner, user, assistant):
        memory.append(assistant.content)
        await commit(thread, owner, user, assistant)
    monkeypatch.setattr(main.channel_memory_maintenance, 'commit_turn', record_memory)
    application = main._novelai_application()
    complete = application.scenes.complete
    def record_scene(*args):
        scenes.append(args)
        return complete(*args)
    monkeypatch.setattr(application.scenes, 'complete', record_scene)
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            yield ModelToolCall('finish', 'finish_reply', {'messages': ['正在发送', '不发送尾段'] if tail else ['正在发送']})
            yield ModelResponseDone('tool_use')
    async def transport(operation, params):
        if operation in {'get_friend_list', 'get_group_list', 'get_group_member_list'}:
            return {'data': []}
        assert operation == 'send_group_msg'
        sent.append(params)
        entered.set()
        await release.wait()
        return {'message_id': '123'}
    monkeypatch.setattr(main, 'model_adapter', Model())
    monkeypatch.setattr(state.adapter, '_transport', transport)
    async def scenario():
        try:
            first = asyncio.create_task(main._dispatch_channel_payload(state.adapter.account_id, payload('pending-send', image=False), source_adapter=state.adapter))
            await entered.wait()
            thread = state.channels.get_thread_by_chat(state.role.id, state.adapter.account_id, '100', chat_type='group')
            stopping = asyncio.create_task(main._channel_command_reply(thread, 'stop', '11'))
            await asyncio.sleep(0)
            assert not stopping.done()
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            assert await stopping == '已停止当前回复。'
        finally:
            release.set()
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(asyncio.wait_for(scenario(), 10))
    assert len(sent) == 1
    thread = state.channels.get_thread_by_chat(state.role.id, state.adapter.account_id, '100', chat_type='group')
    replies = [item for item in state.sessions.list_messages(thread.session_key) if item.role == 'assistant' and item.messageType == 'text']
    assert len(replies) == 1 and replies[0].content == '正在发送'
    assert replies[0].metadata['deliveryStatus'] == 'sent'
    assert memory == ['正在发送']
    assert bool(scenes) is (not tail)


@pytest.mark.parametrize('image_only', [False, True])
def test_natural_reply_reaction_keeps_native_image_and_no_protocol_draft(meme_turn, monkeypatch, image_only):
    import base64
    import re
    from test_role_reactions import illustration
    state = meme_turn
    enable(monkeypatch, state)
    _, raw = illustration(state)
    class Model:
        calls = 0
        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            token = re.search(r'role-[a-f0-9]{16}:', messages[0]['content']).group()[:-1]
            words = [f'<meme:{token}>'] if image_only else ['首段', f'尾段<meme:{token}>']
            yield ModelToolCall('finish', 'finish_reply', {'messages': words})
            yield ModelResponseDone('tool_use')
    model = Model()
    monkeypatch.setattr(main, 'model_adapter', model)
    async def scenario():
        try:
            await main._dispatch_channel_payload(state.adapter.account_id, payload('natural-image', sender='22', image=False), source_adapter=state.adapter)
        finally:
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(scenario())
    sends = [params for operation, params in state.operations if operation == 'send_group_msg']
    assert model.calls == 1
    assert len(sends) == (1 if image_only else 2)
    images = [part for send in sends for part in send['message'] if part['type'] == 'image']
    assert len(images) == 1 and base64.b64decode(images[0]['data']['file'].removeprefix('base64://')) == raw
    text = ''.join(part['data']['text'] for send in sends for part in send['message'] if part['type'] == 'text')
    assert text == ('' if image_only else '首段尾段')
