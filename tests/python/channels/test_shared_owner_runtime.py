import asyncio
import pytest

from backend.app import main
from backend.app.model_adapter import ModelTextDelta
from test_meme_turn import meme_turn


def private_payload(identifier, content):
    return {'post_type': 'message', 'message_type': 'private', 'self_id': 99, 'user_id': 11,
        'message_id': identifier, 'sender': {'user_id': 11, 'nickname': '主人'},
        'message': [{'type': 'text', 'data': {'text': content}}]}


def test_private_runtime_reads_desktop_dialogue_and_two_queued_messages_keep_frozen_boundaries(meme_turn, monkeypatch):
    state = meme_turn
    sessions = state.sessions
    sessions.open_role_session(state.role.id)
    sessions.append_message(f'role:{state.role.id}', 'user', '桌面制定的计划', run_id='desktop')
    sessions.append_message(f'role:{state.role.id}', 'assistant', '桌面回答的细节', run_id='desktop')
    requests, delivered = [], []
    async def transport(operation, params):
        assert operation == 'send_private_msg'
        delivered.append(params)
        return {'message_id': str(1000 + len(delivered))}
    monkeypatch.setattr(state.adapter, '_transport', transport)
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        class Model:
            async def stream_structured_messages(self, messages, configuration, tools):
                requests.append(messages)
                if len(requests) == 1:
                    started.set()
                    await release.wait()
                yield ModelTextDelta('第一回合答案' if len(requests) == 1 else '第二回合答案')
        monkeypatch.setattr(main, 'model_adapter', Model())
        first = asyncio.create_task(main._dispatch_channel_payload(state.adapter.account_id,
            private_payload('first', '手机第一问题'), source_adapter=state.adapter))
        await asyncio.wait_for(started.wait(), 5)
        second = asyncio.create_task(main._dispatch_channel_payload(state.adapter.account_id,
            private_payload('second', '手机第二问题'), source_adapter=state.adapter))
        try:
            for _ in range(100):
                thread = state.channels.get_thread_by_chat(state.role.id, state.adapter.account_id, '11', chat_type='private')
                if state.channels.has_external_event(thread.thread_id, 'second'):
                    break
                await asyncio.sleep(.01)
            else:
                raise AssertionError('second input did not reach the queue')
            sessions.append_message(f'role:{state.role.id}', 'user', '排队以后的未来桌面消息')
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 8)
            # The next input, after both platform receipts are committed,
            # must replay both answers. A concurrently queued input may start
            # before the previous answer has a confirmed delivery receipt.
            await main._dispatch_channel_payload(state.adapter.account_id,
                private_payload('third', '确认两条已送达的回复'), source_adapter=state.adapter)
        finally:
            release.set()
            for task in (first, second):
                if not task.done():
                    task.cancel()
            await asyncio.gather(first, second, return_exceptions=True)
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(scenario())
    with state.channels._connect() as db:
        errors = [tuple(row) for row in db.execute('SELECT status,error FROM agent_runs')]
    assert len(requests) == 3, errors
    assert '桌面制定的计划' in str(requests[0]) and '桌面回答的细节' in str(requests[0])
    assert '手机第二问题' not in str(requests[0])
    second = str(requests[1])
    assert second.count('手机第二问题') == 1
    assert '排队以后的未来桌面消息' not in second
    assert '第一回合答案' in str(requests[2]) and '第二回合答案' in str(requests[2])
    thread = state.channels.get_thread_by_chat(state.role.id, state.adapter.account_id, '11', chat_type='private')
    assert len(sessions.list_messages(thread.session_key)) == 6
    assert len(sessions.list_messages(f'role:{state.role.id}')) == 3


@pytest.mark.parametrize('initial_owner', [False, True])
def test_account_queue_cannot_promote_old_guest_or_expand_owner_history(meme_turn, monkeypatch, initial_owner):
    state = meme_turn
    if not initial_owner:
        state.channels.unbind_identity(state.channels.list_identities()[0]['identityId'])
    state.sessions.open_role_session(state.role.id)
    state.sessions.append_message(f'role:{state.role.id}', 'user', '原桌面秘密')
    requests = []
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            yield ModelTextDelta('结束')
    monkeypatch.setattr(main, 'model_adapter', Model())
    async def send(operation, params):
        return {'message_id': 'sent'}
    monkeypatch.setattr(state.adapter, '_transport', send)
    async def scenario():
        lock = main.channel_event_locks.setdefault(state.adapter.account_id, asyncio.Lock())
        await lock.acquire()
        pending = asyncio.create_task(main._dispatch_channel_payload(state.adapter.account_id,
            private_payload('queued', '配对变化前到达的输入'), source_adapter=state.adapter))
        try:
            await asyncio.sleep(.05)
            assert not pending.done()
            sender = '33' if initial_owner else '11'
            thread = state.channels.ensure_channel_thread(state.role.id, 'qq', state.adapter.account_id, sender, 'private')
            code = state.channels.create_pairing_code()
            assert state.channels.consume_pairing_code(code, role_id=state.role.id, channel='qq', scope='platform',
                platform_user_id=sender, account_id=state.adapter.account_id, chat_id=sender)
            state.sessions.append_message(thread.session_key, 'user', '后配对私聊秘密')
            state.sessions.append_message(f'role:{state.role.id}', 'user', '等待中的未来桌面消息')
            lock.release()
            await asyncio.wait_for(pending, 8)
        finally:
            if lock.locked():
                lock.release()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(scenario())
    assert bool(requests) is initial_owner
    actual = str(requests)
    assert ('原桌面秘密' in actual) is initial_owner
    assert '后配对私聊秘密' not in actual and '等待中的未来桌面消息' not in actual
