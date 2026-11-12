import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.conversation_history import ConversationHistoryView
from backend.app.multichannel import MultichannelStore
from test_message_history_tools import setup_history


def group_history(tmp_path):
    sessions, channels, _, _ = setup_history(tmp_path)
    channels.register_channel_account('r1', 'qq', account_id='qq', enabled=True, allow_group=True)
    thread = channels.ensure_channel_thread('r1', 'qq', 'qq', 'group', 'group', title='茶友群')
    channels.set_group_listening('r1', 'qq', 'group', True)
    code = channels.create_pairing_code()
    channels.consume_pairing_code(code, role_id='r1', channel='qq', scope='platform',
        platform_user_id='owner', account_id='qq', chat_id='owner')
    with channels._connect() as db:
        old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        db.execute('UPDATE channel_identities SET paired_at=?', (old,))
        db.execute('UPDATE channel_accounts SET created_at=?', (old,))
    return sessions, channels, thread


def hear(channels, thread, identifier, text, *, timestamp=None, owner=True):
    return channels.listening.hear(thread, text, identifier,
        {'senderId': 'owner' if owner else 'friend', 'senderIsOwner': owner}, timestamp=timestamp)


def test_owner_recent_group_speech_is_bounded_and_keeps_original_scope(tmp_path):
    sessions, channels, thread = group_history(tmp_path)
    now = datetime.now(timezone.utc)
    hear(channels, thread, 'expired', 'EXPIRED', timestamp=now - timedelta(hours=6, seconds=1))
    for index in range(11):
        hear(channels, thread, str(index), f'OWNER_{index} ' + '茶' * 300,
            timestamp=now - timedelta(minutes=11-index))
    hear(channels, thread, 'friend', 'FRIEND_SECRET', owner=False)
    view = ConversationHistoryView(channels, role_id='r1', group_speech=True)
    blocks = view.group_speech_blocks()
    assert len(blocks) == 1
    rows = json.loads(blocks[0].split('\n', 2)[2])
    assert len(rows) == 10
    assert rows[0]['text'].startswith('OWNER_1 ') and rows[-1]['text'].startswith('OWNER_10 ')
    assert all(len(row['text']) <= 200 and row['group'] == '茶友群' for row in rows)
    assert 'EXPIRED' not in blocks[0] and 'FRIEND_SECRET' not in blocks[0]
    assert view.read() == []
    assert 'OWNER_' not in json.dumps(view.snapshot())
    assert ConversationHistoryView(channels, role_id='r1').group_speech_blocks() == []


@pytest.mark.parametrize('change', ['clear', 'toggle', 'unbind', 'edit'])
def test_owner_group_speech_revalidates_frozen_sources(tmp_path, change):
    _, channels, thread = group_history(tmp_path)
    source = hear(channels, thread, 'source', 'RECENT_OWNER')
    view = ConversationHistoryView(channels, role_id='r1', group_speech=True)
    assert 'RECENT_OWNER' in str(view.group_speech_blocks())
    if change == 'clear':
        channels.listening.clear('r1', thread.thread_id)
    elif change == 'toggle':
        channels.set_group_listening('r1', 'qq', 'group', False)
        channels.set_group_listening('r1', 'qq', 'group', True)
    elif change == 'unbind':
        channels.unbind_identity(channels.list_identities()[0]['identityId'])
    else:
        with channels._connect() as db:
            db.execute('UPDATE channel_listening_records SET content=? WHERE id=?', ('CHANGED', source['id']))
    with pytest.raises(PermissionError):
        view.require_snapshot_current()


def test_temporary_owner_speech_expires_with_window_and_restart(tmp_path):
    sessions, channels, thread = group_history(tmp_path)
    channels.listening.set_cap('r1', thread.thread_id, 1)
    hear(channels, thread, 'saved', 'SAVED_OWNER')
    temporary = hear(channels, thread, 'temporary', 'TEMPORARY_OWNER')
    sessions.append_message(thread.session_key, 'user', temporary['content'], metadata=temporary['metadata'])
    frozen = ConversationHistoryView(channels, role_id='r1', group_speech=True)
    assert 'TEMPORARY_OWNER' in str(frozen.group_speech_blocks())
    for index in range(30):
        hear(channels, thread, 'later-' + str(index), 'FRIEND', owner=False)
    with pytest.raises(PermissionError, match='缺失'):
        frozen.group_speech_blocks()
    current = ConversationHistoryView(channels, role_id='r1', group_speech=True)
    assert 'TEMPORARY_OWNER' not in str(current.group_speech_blocks())
    restarted = MultichannelStore(sessions.database_path, sessions)
    restored = ConversationHistoryView(restarted, role_id='r1', group_speech=True)
    assert 'SAVED_OWNER' in str(restored.group_speech_blocks())
    assert 'TEMPORARY_OWNER' not in str(restored.group_speech_blocks())


def test_disabling_listening_keeps_saved_speech_available_to_new_turns(tmp_path):
    _, channels, thread = group_history(tmp_path)
    channels.listening.set_cap('r1', thread.thread_id, 1)
    hear(channels, thread, 'saved', 'SAVED_OWNER')
    hear(channels, thread, 'temporary', 'TEMPORARY_OWNER')
    frozen = ConversationHistoryView(channels, role_id='r1', group_speech=True)
    assert 'TEMPORARY_OWNER' in str(frozen.group_speech_blocks())
    channels.set_group_listening('r1', 'qq', 'group', False)
    with pytest.raises(PermissionError):
        frozen.require_snapshot_current()
    current = ConversationHistoryView(channels, role_id='r1', group_speech=True)
    assert 'SAVED_OWNER' in str(current.group_speech_blocks())
    assert 'TEMPORARY_OWNER' not in str(current.group_speech_blocks())


@pytest.mark.parametrize('change', ['content', 'identity', 'reply'])
def test_binding_run_preserves_frozen_speech_but_source_edits_invalidate_it(tmp_path, change):
    from backend.app.models import AgentRun
    sessions, channels, thread = group_history(tmp_path)
    _, original = channels.append_event(thread, direction='inbound', content='OWNER_SOURCE',
        external_message_id='direct', metadata={'senderId': 'owner', 'senderIsOwner': True})
    reply = sessions.append_message(thread.session_key, 'assistant', 'CONFIRMED_REPLY',
        metadata={'channel': 'qq', 'deliveryStatus': 'sent',
            'responseRelation': {'triggerSourceMessageId': original.id}})
    view = ConversationHistoryView(channels, role_id='r1', group_speech=True)
    before = view.group_speech_blocks()
    sessions.create_run_with_existing_user_message(AgentRun(runId='bound-run', roleId='r1',
        sessionKey=thread.session_key, status='created', startedAt=datetime.now(timezone.utc)), original.id)
    view.require_snapshot_current()
    assert view.group_speech_blocks() == before
    with channels._connect() as db:
        if change == 'identity':
            metadata = {**original.metadata, 'senderIsOwner': False}
            db.execute('UPDATE messages SET metadata_json=? WHERE message_id=?', (json.dumps(metadata), original.id))
        else:
            identifier = reply.id if change == 'reply' else original.id
            db.execute('UPDATE messages SET content=? WHERE message_id=?', ('CHANGED', identifier))
    with pytest.raises(PermissionError):
        view.require_snapshot_current()


def test_direct_owner_speech_preserves_quote_and_confirmed_reply(tmp_path):
    sessions, channels, thread = group_history(tmp_path)
    _, original = channels.append_event(thread, direction='inbound', content='请回答我引用的群友',
        external_message_id='direct', metadata={'senderId': 'owner', 'senderIsOwner': True,
            'replyTo': 'friend-question', 'replyContextVerified': True, 'replyToSenderId': 'friend',
            'replyToSenderName': '群友', 'replyToSenderIsOwner': False, 'replyToContent': '群友的问题'})
    sessions.append_message(thread.session_key, 'assistant', 'CONFIRMED' + '答' * 300 + '不应出现' * 300,
        status='failed', metadata={'channel': 'qq', 'deliveryStatus': 'uncertain', 'partialDelivery': True,
            'responseRelation': {'triggerSourceMessageId': original.id, 'targetSenderId': 'friend'},
            'parts': [{'kind': 'text', 'content': 'CONFIRMED' + '答' * 300, 'status': 'sent', 'platformMessageId': 'sent'},
                {'kind': 'text', 'content': '不应出现' * 300, 'status': 'uncertain'}]})
    view = ConversationHistoryView(channels, role_id='r1', group_speech=True)
    row = json.loads(view.group_speech_blocks()[0].split('\n', 2)[2])[0]
    assert row['text'] == original.content and row['quote']['senderId'] == 'friend'
    assert row['quote']['identity'] == '未配对成员'
    assert row['reply'].startswith('CONFIRMED') and len(row['reply']) == 200
    assert row['replySources'][0]['responseRelation']['targetSenderId'] == 'friend'
    assert '不应出现' not in str(row)
    assert view.read() == []


def test_missing_historical_owner_evidence_and_later_binding_do_not_expand_view(tmp_path):
    _, channels, thread = group_history(tmp_path)
    channels.listening.hear(thread, 'UNKNOWN_OLD_IDENTITY', 'unknown', {'senderId': 'owner'})
    frozen = ConversationHistoryView(channels, role_id='r1', group_speech=True)
    channels.unbind_identity(channels.list_identities()[0]['identityId'])
    hear(channels, thread, 'before', 'BEFORE_PAIRING')
    code = channels.create_pairing_code()
    channels.consume_pairing_code(code, role_id='r1', channel='qq', scope='platform',
        platform_user_id='owner', account_id='qq', chat_id='owner')
    hear(channels, thread, 'after', 'AFTER_PAIRING')
    assert frozen.group_speech_blocks() == []
    current = ConversationHistoryView(channels, role_id='r1', group_speech=True)
    assert 'AFTER_PAIRING' in str(current.group_speech_blocks())
    assert 'BEFORE_PAIRING' not in str(current.group_speech_blocks())
    assert 'UNKNOWN_OLD_IDENTITY' not in str(current.group_speech_blocks())


def test_natural_reply_uses_run_input_without_requiring_a_persisted_input_copy(tmp_path):
    from backend.app.models import AgentRun
    sessions, channels, thread = group_history(tmp_path)
    original = hear(channels, thread, 'natural-owner', '请帮群友回答问题')
    sessions.create_run(AgentRun(runId='natural-run', roleId='r1', sessionKey=thread.session_key,
        status='completed', startedAt=datetime.now(timezone.utc),
        modelSnapshot={'currentInputId': original['id']}))
    sessions.append_message(thread.session_key, 'assistant', '给群友的已确认回答', run_id='natural-run',
        metadata={'channel': 'qq', 'deliveryStatus': 'sent',
            'responseRelation': {'targetSourceMessageId': 'friend-question', 'targetSenderId': 'friend'}})
    view = ConversationHistoryView(channels, role_id='r1', group_speech=True)
    row = json.loads(view.group_speech_blocks()[0].split('\n', 2)[2])[0]
    assert row['sourceMessageId'] == original['id']
    assert row['reply'] == '给群友的已确认回答'
    assert row['replySources'][0]['responseRelation']['targetSenderId'] == 'friend'


def test_untrusted_old_timestamps_are_skipped_without_breaking_group_context(tmp_path):
    _, channels, thread = group_history(tmp_path)
    for index, value in enumerate(['', 'invalid', datetime.now().isoformat()]):
        record = hear(channels, thread, str(index), 'UNTRUSTED_TIME')
        with channels._connect() as db:
            db.execute('UPDATE channel_listening_records SET created_at=? WHERE id=?', (value, record['id']))
    hear(channels, thread, 'valid', 'VALID_RECENT')
    view = ConversationHistoryView(channels, role_id='r1', group_speech=True)
    assert 'VALID_RECENT' in str(view.group_speech_blocks())
    assert 'UNTRUSTED_TIME' not in str(view.group_speech_blocks())


def test_six_hour_boundary_and_snapshot_replay(tmp_path, monkeypatch):
    from backend.app import conversation_history
    _, channels, thread = group_history(tmp_path)
    moment = datetime.now(timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment
    monkeypatch.setattr(conversation_history, 'datetime', Clock)
    hear(channels, thread, 'at', 'AT_SIX_HOURS', timestamp=moment - timedelta(hours=6))
    hear(channels, thread, 'outside', 'OUTSIDE_SIX_HOURS', timestamp=moment - timedelta(hours=6, microseconds=1))
    view = ConversationHistoryView(channels, role_id='r1', group_speech=True)
    restored = ConversationHistoryView(channels, role_id='r1', snapshot=view.snapshot())
    assert 'AT_SIX_HOURS' in str(restored.group_speech_blocks())
    assert 'OUTSIDE_SIX_HOURS' not in str(restored.group_speech_blocks())


def test_group_background_obeys_full_request_budget_and_does_not_grant_history_tools(tmp_path):
    from backend.app.agent_runtime import CancellationToken, ToolContext
    from backend.app.agent_runtime.meido_provider import MeidoProvider, RequestContext
    from backend.app.agent_runtime.message_history_tools import build_message_history_tools
    from backend.app.agent_runtime.types import UserMessage
    from backend.app.models import ModelConfiguration, Role
    from test_context_windows import Adapter
    from test_message_history_tools import execute
    sessions, channels, thread = group_history(tmp_path)
    record = hear(channels, thread, 'owner', 'OWNER_FACT' * 100)
    view = ConversationHistoryView(channels, role_id='r1', group_speech=True)
    config = ModelConfiguration(providerId='custom', provider='fixture', baseUrl='http://localhost',
        model='fixture', contextWindowTokens=8000)
    now = datetime.now(timezone.utc)
    role = Role(id='r1', name='R', profile={'profile': '测试角色'}, createdAt=now, updatedAt=now)
    provider = MeidoProvider(Adapter(), role, config, max_response_tokens=100)
    messages = [UserMessage('本轮当前问题')]
    bare = provider.estimate_request_tokens('', messages, ())
    provider.configuration = config.model_copy(update={'contextWindowTokens': bare + 120})
    prepared = provider.prepare_request('', messages, (), context=RequestContext(blocks=tuple(view.group_speech_blocks())))
    assert prepared.context == ''
    assert provider.estimate_prepared(prepared).tokens + 100 < provider.configuration.contextWindowTokens
    fetch = build_message_history_tools(sessions, channels, role_id='r1', view=view)[1]
    result = execute(fetch, {'ids': [record['id']]}, ToolContext('r1', 'role:r1', 'run', CancellationToken()))
    assert result['count'] == 0
