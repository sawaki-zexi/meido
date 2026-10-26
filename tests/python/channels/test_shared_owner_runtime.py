import asyncio

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
