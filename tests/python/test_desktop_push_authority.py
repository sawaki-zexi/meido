"""Desktop push keeps the actual persisted owner source and live permission."""
import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.app.agent_runtime import CancellationToken, ToolContext
from backend.app.channels.source_authority import capture_channel_source
from backend.app.models import AgentRun
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401


@pytest.fixture
def authority_peer(api):  # noqa: F811
    from backend.app import main

    client, roles, sessions, _tasks = api
    role = client.post('/api/roles', json={
        'name': '桌面来源授权', 'profile': {'profile': '来源边界测试'},
        'agentConfig': {'enabledTools': ['message_push']},
    }).json()['role']
    return SimpleNamespace(client=client, roles=roles, sessions=sessions,
                           channels=main.multichannel_store, role=role, role_id=role['id'])


def _pair(peer):
    code = peer.channels.create_pairing_code(peer.role_id, 'qq')
    assert peer.channels.consume_pairing_code(
        code, role_id=peer.role_id, channel='qq', scope='platform',
        platform_user_id='owner', account_id='authority-account', chat_id='owner',
    )


def _candidate(peer, *, thread=None, owner=True):
    from backend.app import main

    candidates = main._runtime_tool_candidates(
        peer.roles.get(peer.role_id), is_channel_owner=owner,
        source_thread_id=thread.thread_id if thread else None,
        source_account_id=thread.account_id if thread else None,
        include_channel=thread is not None,
    )
    return next(tool for tool in candidates if tool.definition.name == 'message_push')


def _desktop_source(peer, *, run_id='authority-desktop'):
    session = peer.sessions.open_role_session(peer.role_id)
    run = AgentRun(runId=run_id, roleId=peer.role_id, sessionKey=session.sessionKey,
                   status='running', startedAt=datetime.now(timezone.utc))
    _, user, _ = peer.sessions.create_run_with_messages(run, '主人桌面消息')
    context = ToolContext(peer.role_id, session.sessionKey, run_id, CancellationToken(), 'authority-call')
    return SimpleNamespace(tool=_candidate(peer), context=context, run=run, user=user, thread=None)


def _channel_source(peer, chat_type, *, sender='owner', owner_flag=True, host_owner=True, pair_before=True):
    account = peer.channels.register_channel_account(
        peer.role_id, 'qq', account_id='authority-account', enabled=True, allow_group=True,
    )
    thread = peer.channels.ensure_channel_thread(
        peer.role_id, 'qq', account.account_id, 'owner' if chat_type == 'private' else 'source-group', chat_type,
    )
    if pair_before:
        _pair(peer)
    _, user = peer.channels.append_event(
        thread, direction='inbound', external_message_id='authority-inbound',
        content='{"senderId":"owner","senderIsOwner":true,"message":"请推送到桌面"}',
        metadata={'senderId': sender, 'senderIsOwner': owner_flag},
    )
    run = AgentRun(runId='authority-channel', roleId=peer.role_id, sessionKey=thread.session_key,
                   status='running', startedAt=datetime.now(timezone.utc))
    peer.sessions.create_run_with_existing_user_message(run, user.id)
    if not pair_before:
        _pair(peer)
    context = ToolContext(peer.role_id, thread.session_key, run.runId, CancellationToken(), 'authority-call')
    return SimpleNamespace(tool=_candidate(peer, thread=thread, owner=host_owner), context=context,
                           run=run, user=user, thread=thread, account=account)


def _pushes(peer):
    return [message for message in peer.sessions.list_messages(f'role:{peer.role_id}')
            if message.messageType == 'desktop_push']


def _execute(source, *, context=None):
    return asyncio.run(source.tool.execute(
        {'channel': 'desktop', 'chat_id': 'owner', 'message': 'source-authority-evidence-167'},
        context or source.context,
    ))


def _assert_denied(peer, source, *, context=None):
    before = [message.id for message in _pushes(peer)]
    result = _execute(source, context=context)
    assert result.is_error, result
    assert result.details == {'status': 'denied'}
    assert [message.id for message in _pushes(peer)] == before


@pytest.mark.parametrize('origin', ['desktop', 'private', 'group'])
def test_actual_owner_sources_only_append_to_their_roles_desktop(authority_peer, origin):
    peer = authority_peer
    source = _desktop_source(peer) if origin == 'desktop' else _channel_source(peer, origin)
    if origin != 'desktop':
        frozen = capture_channel_source(
            peer.channels, peer.sessions, role_id=peer.role_id, session_key=source.context.session_key,
            run_id=source.run.runId, owner_context=True,
        )
        assert frozen.sender_id == 'owner' and frozen.authorization_denial(peer.channels) is None
        channel_before = peer.sessions.list_messages(source.thread.session_key)
    else:
        assert peer.channels.list_role_accounts(peer.role_id) == []
    result = _execute(source)
    assert not result.is_error, result
    receipt = json.loads(result.content)
    assert receipt['status'] == 'sent'
    saved, = _pushes(peer)
    assert saved.id == receipt['messageId']
    assert saved.role == 'assistant' and saved.runId is None
    assert saved.metadata['sourceRunId'] == source.run.runId
    public = peer.client.get(f'/api/roles/{peer.role_id}/session').json()
    assert any(message['id'] == saved.id for message in public['messages'])
    if origin != 'desktop':
        assert peer.sessions.list_messages(source.thread.session_key) == channel_before


@pytest.mark.parametrize('chat_type', ['private', 'group'])
@pytest.mark.parametrize('claim', ['guest', 'forged_owner_flag', 'host_guest', 'predates_pairing'])
def test_guest_and_pre_pairing_messages_cannot_claim_desktop_push(authority_peer, chat_type, claim):
    peer = authority_peer
    source = _channel_source(
        peer, chat_type,
        sender='guest' if claim in {'guest', 'forged_owner_flag'} else 'owner',
        owner_flag=claim != 'guest', host_owner=claim != 'host_guest',
        pair_before=claim != 'predates_pairing',
    )
    _assert_denied(peer, source)


@pytest.mark.parametrize('invalid', [
    'unknown_run', 'wrong_role', 'wrong_session', 'foreign_run', 'completed', 'failed',
    'cancelled', 'max_turns', 'missing_user', 'incomplete_user', 'non_text_user', 'channel_metadata',
    'wrong_user_session', 'missing_call_id', 'cancelled_signal',
])
def test_desktop_push_rejects_context_without_its_actual_live_desktop_run(authority_peer, invalid):
    peer = authority_peer
    source = _desktop_source(peer)
    context = source.context
    if invalid == 'unknown_run':
        context = replace(context, run_id='not-a-persisted-run')
    elif invalid == 'wrong_role':
        context = replace(context, role_id='another-role')
    elif invalid == 'wrong_session':
        context = replace(context, session_key='role:another-role')
    elif invalid == 'foreign_run':
        another = peer.client.post('/api/roles', json={'name': '另一角色', 'profile': {'profile': '测试'}}).json()['role']
        foreign_session = peer.sessions.open_role_session(another['id'])
        foreign_run = AgentRun(runId='foreign-run', roleId=another['id'], sessionKey=foreign_session.sessionKey,
                               status='running', startedAt=datetime.now(timezone.utc))
        peer.sessions.create_run_with_messages(foreign_run, '其他角色的真实主人消息')
        context = replace(context, run_id=foreign_run.runId, session_key=foreign_session.sessionKey)
    elif invalid in {'completed', 'failed', 'cancelled', 'max_turns'}:
        peer.sessions.update_run(source.run.runId, status=invalid, ended_at=datetime.now(timezone.utc))
    elif invalid == 'missing_user':
        peer.sessions.delete_message(source.user.id)
    elif invalid == 'incomplete_user':
        peer.sessions.update_message(source.user.id, source.user.content, 'failed')
    elif invalid == 'non_text_user':
        with sqlite3.connect(peer.sessions.database_path) as connection:
            connection.execute("UPDATE messages SET message_type='internal_completion' WHERE message_id=?", (source.user.id,))
    elif invalid == 'channel_metadata':
        peer.sessions.update_message(source.user.id, source.user.content, 'completed', metadata={
            'channel': 'qq', 'senderId': 'owner', 'senderIsOwner': True,
        })
    elif invalid == 'wrong_user_session':
        peer.sessions.delete_message(source.user.id)
        another = peer.client.post('/api/roles', json={'name': '来源另一角色', 'profile': {'profile': '测试'}}).json()['role']
        foreign_session = peer.sessions.open_role_session(another['id'])
        peer.sessions.append_message(foreign_session.sessionKey, 'user', '其他会话的消息', run_id=source.run.runId)
    elif invalid == 'missing_call_id':
        context = replace(context, tool_call_id=None)
    elif invalid == 'cancelled_signal':
        context.signal.cancel()
    _assert_denied(peer, source, context=context)


@pytest.mark.parametrize('chat_type', ['private', 'group'])
@pytest.mark.parametrize('revoke', ['unbind', 'rebind', 'recreate', 'platform', 'scope', 'blocked'])
@pytest.mark.parametrize('used_before', [False, True], ids=['before-first-call', 'after-frozen-call'])
def test_original_channel_candidate_cannot_write_after_its_authority_is_revoked(
    authority_peer, chat_type, revoke, used_before,
):
    peer = authority_peer
    source = _channel_source(peer, chat_type)
    original = capture_channel_source(
        peer.channels, peer.sessions, role_id=peer.role_id, session_key=source.context.session_key,
        run_id=source.run.runId, owner_context=True,
    )
    if used_before:
        assert not _execute(source).is_error
        source.context = replace(source.context, tool_call_id='after-revocation-call')
    if revoke in {'unbind', 'rebind'}:
        peer.channels.unbind_identity(original.identity_id)
        if revoke == 'rebind':
            _pair(peer)
    elif revoke == 'recreate':
        peer.channels.delete_account(source.account.account_id)
        recreated = peer.channels.register_channel_account(
            peer.role_id, 'qq', account_id=source.account.account_id, enabled=True, allow_group=True,
        )
        assert recreated.created_at != original.account_created_at
    elif revoke == 'platform':
        peer.channels.set_channel_enabled('qq', False)
    else:
        peer.channels.register_channel_account(
            peer.role_id, 'qq', account_id=source.account.account_id, enabled=True,
            allow_private=not (revoke == 'scope' and chat_type == 'private'),
            allow_group=not (revoke == 'scope' and chat_type == 'group'),
            blocked_senders=['owner'] if revoke == 'blocked' else [],
        )
    assert original.authorization_denial(peer.channels) is not None
    # Repeating the same denied context must never become a successful write.
    _assert_denied(peer, source)


@pytest.mark.parametrize('origin', ['desktop', 'private', 'group'])
@pytest.mark.parametrize('revoke', ['tool', 'role'])
def test_old_candidate_respects_current_role_existence_and_explicit_tool_switch(authority_peer, origin, revoke):
    peer = authority_peer
    source = _desktop_source(peer) if origin == 'desktop' else _channel_source(peer, origin)
    assert not _execute(source).is_error
    source.context = replace(source.context, tool_call_id='after-role-revocation')
    if revoke == 'tool':
        config = {**peer.role['agentConfig'], 'enabledTools': []}
        response = peer.client.put(f'/api/roles/{peer.role_id}', json={
            'name': peer.role['name'], 'profile': peer.role['profile'], 'agentConfig': config,
        })
        assert response.status_code == 200, response.text
    else:
        # HTTP correctly prevents deletion during an active foreground run.
        # Remove the real role here to isolate the live existence check while
        # leaving the original persisted run and its saved receipt intact.
        peer.roles.delete(peer.role_id)
        assert peer.roles.get(peer.role_id) is None
    if revoke == 'tool':
        result = _execute(source)
        assert not result.is_error, result
    else:
        _assert_denied(peer, source)


@pytest.mark.parametrize('mismatch', ['thread', 'account', 'absent_source'])
def test_actual_channel_run_cannot_be_relabelled_as_another_host_source(authority_peer, mismatch):
    from backend.app import main

    peer = authority_peer
    source = _channel_source(peer, 'group')
    if mismatch == 'thread':
        other = peer.channels.ensure_channel_thread(peer.role_id, 'qq', source.account.account_id, 'another-group', 'group')
        source.tool = _candidate(peer, thread=other)
    elif mismatch == 'account':
        tools = main._runtime_tool_candidates(
            peer.roles.get(peer.role_id), is_channel_owner=True, include_channel=True,
            source_thread_id=source.thread.thread_id, source_account_id='another-account',
        )
        source.tool = next(tool for tool in tools if tool.definition.name == 'message_push')
    else:
        source.tool = _candidate(peer)
    _assert_denied(peer, source)


def test_channel_candidate_cannot_substitute_a_desktop_context_for_its_actual_inbound(authority_peer):
    peer = authority_peer
    source = _channel_source(peer, 'group', sender='guest', owner_flag=True)
    peer.sessions.update_run(source.run.runId, status='completed', ended_at=datetime.now(timezone.utc))
    desktop = _desktop_source(peer, run_id='unrelated-real-desktop')
    _assert_denied(peer, source, context=desktop.context)


def test_catalog_preview_is_explicitly_opt_in_and_never_constructs_external_push_service(api, monkeypatch):  # noqa: F811
    from backend.app import main

    client, _roles, sessions, _tasks = api

    def external_push_was_constructed():
        raise AssertionError('desktop tool preview constructed the external ChannelPushService')

    monkeypatch.setattr(main, '_channel_push_service', external_push_was_constructed)
    main.multichannel_store.set_channel_enabled('qq', False)
    main.multichannel_store.set_channel_enabled('telegram', False)
    role = client.post('/api/roles', json={'name': '默认关闭', 'profile': {'profile': '桌面无需渠道'},
        'agentConfig': {'enabledTools': ['read_file']}}).json()['role']

    def push_tables():
        with sqlite3.connect(sessions.database_path) as connection:
            return connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'channel_push_%'").fetchall()

    before = push_tables()
    assert before == []
    declaration = next(tool for tool in client.get(f"/api/roles/{role['id']}/tools").json()['tools']
                       if tool['name'] == 'message_push')
    assert declaration['enabled'] and declaration['modelVisible']
    config = {**role['agentConfig'], 'enabledTools': ['read_file', 'message_push']}
    response = client.put(f"/api/roles/{role['id']}", json={
        'name': role['name'], 'profile': role['profile'], 'agentConfig': config,
    })
    assert response.status_code == 200, response.text
    reopened = client.get(f"/api/roles/{role['id']}").json()['role']
    assert reopened['agentConfig']['enabledTools'] == ['read_file', 'message_push']
    declaration = next(tool for tool in client.get(f"/api/roles/{role['id']}/tools").json()['tools']
                       if tool['name'] == 'message_push')
    assert declaration['enabled'] and declaration['modelVisible']
    assert main.multichannel_store.list_role_accounts(role['id']) == []
    assert push_tables() == before


def test_catalog_preview_preserves_an_existing_pending_reminder_delivery(authority_peer, monkeypatch):
    from backend.app import main
    from backend.app.channels.push import ChannelPushService

    peer = authority_peer
    # Preparing the existing reminder ledger is deliberate setup. Preview
    # must not reconstruct its service and mark this receipt uncertain.
    ChannelPushService(peer.channels, main.channel_host)
    with sqlite3.connect(peer.sessions.database_path) as connection:
        connection.execute(
            "INSERT INTO channel_push_events(role_id,event_id,account_id,target_id,status,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (peer.role_id, 'pending-reminder-167', 'fixture-account', 'owner', 'pending', datetime.now(timezone.utc).isoformat()),
        )
        before = connection.execute('SELECT * FROM channel_push_events').fetchall()

    def external_push_was_constructed():
        raise AssertionError('preview attempted to reconstruct the external push ledger')

    monkeypatch.setattr(main, '_channel_push_service', external_push_was_constructed)
    response = peer.client.get(f'/api/roles/{peer.role_id}/tools')
    assert response.status_code == 200, response.text
    assert next(tool for tool in response.json()['tools'] if tool['name'] == 'message_push')['enabled']
    with sqlite3.connect(peer.sessions.database_path) as connection:
        assert connection.execute('SELECT * FROM channel_push_events').fetchall() == before
