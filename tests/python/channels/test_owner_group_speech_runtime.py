import asyncio

from backend.app import main
from backend.app.model_adapter import ModelTextDelta
from test_meme_turn import meme_turn
from test_shared_owner_runtime import private_payload


def test_paired_private_runtime_receives_owner_group_speech(meme_turn, monkeypatch):
    state = meme_turn
    requests = []
    thread = state.channels.ensure_channel_thread(state.role.id, 'qq', state.adapter.account_id, '100', 'group', title='近期群')
    state.channels.set_group_listening(state.role.id, state.adapter.account_id, '100', True)
    state.channels.listening.hear(thread, 'RECENT_OWNER_IN_GROUP', 'heard', {'senderId': '11', 'senderIsOwner': True})
    async def transport(operation, params):
        assert operation == 'send_private_msg'
        return {'message_id': 'private-reply'}
    monkeypatch.setattr(state.adapter, '_transport', transport)
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            yield ModelTextDelta('接上群里的话题')
    monkeypatch.setattr(main, 'model_adapter', Model())
    async def scenario():
        try:
            await main._dispatch_channel_payload(state.adapter.account_id,
                private_payload('private', '继续刚才群里的话题'), source_adapter=state.adapter)
        finally:
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(scenario())
    assert len(requests) == 1
    assert 'RECENT_OWNER_IN_GROUP' in str(requests)
    private = state.channels.get_thread_by_chat(state.role.id, state.adapter.account_id, '11', chat_type='private')
    assert 'RECENT_OWNER_IN_GROUP' not in str(state.sessions.list_messages(private.session_key))
