from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore


def test_restart_marks_only_pending_qq_turns_interrupted_and_retains_dedupe(tmp_path):
    path = tmp_path / 'sessions.db'
    sessions = SessionStore(path)
    channels = MultichannelStore(path, sessions)
    channels.register_channel_account('role', 'qq', account_id='qq', enabled=True)
    thread = channels.ensure_channel_thread('role', 'qq', 'qq', 'group', 'group')
    ids = {}
    for state in ('queued', 'running', 'completed'):
        _, message = channels.append_event(thread, direction='inbound', content=state,
            external_message_id=state, metadata={'channelTurnState': state})
        ids[state] = message.id
    assert channels.mark_interrupted_channel_turns() == 2
    messages = {message.id: message for message in sessions.list_messages(thread.session_key)}
    for state in ('queued', 'running'):
        assert messages[ids[state]].metadata['channelTurnState'] == 'interrupted'
        assert messages[ids[state]].content == state
        assert channels.has_external_event(thread.thread_id, state)
    assert messages[ids['completed']].metadata['channelTurnState'] == 'completed'
    assert channels.mark_interrupted_channel_turns() == 0


def test_turn_state_update_preserves_message_and_publishes(tmp_path):
    sessions = SessionStore(tmp_path / 'sessions.db')
    channels = MultichannelStore(tmp_path / 'sessions.db', sessions)
    channels.register_channel_account('role', 'qq', account_id='qq', enabled=True)
    thread = channels.ensure_channel_thread('role', 'qq', 'qq', 'group', 'group')
    _, message = channels.append_event(thread, direction='inbound', content='hello',
        external_message_id='m', metadata={'channelTurnState': 'queued', 'senderId': 'visitor'})
    import asyncio
    queue = asyncio.Queue()
    channels._subscribers['role'] = {queue}
    channels.set_channel_turn_state(thread, message.id, 'invalidated')
    updated = sessions.list_messages(thread.session_key)[0]
    assert updated.metadata['channelTurnState'] == 'invalidated'
    assert updated.metadata['senderId'] == 'visitor'
    assert updated.content == 'hello' and updated.id == message.id
    assert queue.get_nowait()['message']['metadata']['channelTurnState'] == 'invalidated'
