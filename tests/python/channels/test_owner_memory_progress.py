import asyncio
from types import SimpleNamespace

import pytest

from backend.app import main
from backend.app.multichannel import MultichannelStore
from backend.app.owner_channel_memory import OwnerChannelMemory
from backend.app.role_store import RoleStore
from backend.app.models import RoleInput, RoleProfile
from backend.app.session_store import SessionStore


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
@pytest.mark.parametrize('listening', [False, True])
async def test_owner_channel_documents_have_independent_retryable_progress(tmp_path, monkeypatch, listening):
    roles = RoleStore(tmp_path / 'roles')
    role = roles.create(RoleInput(name='角色', profile=RoleProfile(profile='设定')))
    sessions = SessionStore(tmp_path / 'sessions.db')
    channels = MultichannelStore(sessions.database_path, sessions)
    channels.register_channel_account(role.id, 'qq', account_id='qq', enabled=True, allow_group=True)
    thread = channels.ensure_channel_thread(role.id, 'qq', 'qq', '100', 'group')
    channels.set_group_listening(role.id, 'qq', '100', True)
    code = channels.create_pairing_code()
    assert channels.consume_pairing_code(code, role_id=role.id, channel='qq', scope='platform',
        platform_user_id='owner', account_id='qq', chat_id='100')
    for name, value in {'store': roles, 'session_store': sessions, 'multichannel_store': channels,
                       'memory_worker': SimpleNamespace(role_deletion_blocked=lambda _: False)}.items():
        monkeypatch.setattr(main, name, value)
    metadata = {'senderId': 'owner', 'senderIsOwner': True, 'channelTurnState': 'running'}
    if listening:
        channels.listening.hear(thread, '主人事实', 'source', metadata)
        with channels._connect() as db:
            messages = [channels.listening._message(row) for row in db.execute('SELECT * FROM channel_listening_records')]
    else:
        _, message = channels.append_event(thread, direction='inbound', content='主人事实', external_message_id='source', metadata=metadata)
        messages = [message.model_dump(mode='json')]
    extracted, published = [], []
    fail = True
    def extract(role_id, source):
        extracted.append(source)
        return {'history': '我们讨论了主人事实', 'pending': '- [identity] 主人事实',
            'history_entries': [('我们讨论了主人事实', 0)], 'conversation': '主人事实'}
    def publish(event):
        published.append(event.source_key)
        if fail:
            raise RuntimeError('controlled embedding outage')
    owner = OwnerChannelMemory(sessions, roles.root, extract, publish, main._verify_owner_channel_memory)
    folder = roles.root / role.id / 'memory'
    recent = (folder / 'RECENT_CONTEXT.md').read_bytes()
    await owner.commit(thread, messages, listening=listening)
    await owner.drain()
    key = ('listening:' if listening else 'group-owner:') + thread.thread_id
    assert sessions.maintenance_progress(role.id, key)['pendingEvents']
    assert sessions.maintenance_progress(role.id, 'role:' + role.id) == {'lastSequence': 0}
    assert (folder / 'RECENT_CONTEXT.md').read_bytes() == recent
    assert '主人事实' in (folder / 'PENDING.md').read_text(encoding='utf-8')
    # The normal channel completion checkpoint is not a source edit.
    if listening:
        import json
        with channels._connect() as db:
            db.execute('UPDATE channel_listening_records SET metadata_json=? WHERE id=?',
                (json.dumps({**messages[0]['metadata'], 'channelTurnState': 'completed'}), messages[0]['id']))
    else:
        sessions.update_message(message.id, message.content, message.status, metadata={**message.metadata, 'channelTurnState': 'completed'})
    fail = False
    await owner.retry(role.id, key)
    saved = sessions.maintenance_progress(role.id, key)
    assert not saved['pendingEvents'] and not saved.get('invalidatedEvents')
    assert len(published) == 2 and len(extracted) == 1
    await owner.commit(thread, messages, listening=listening)
    await owner.drain()
    assert len(extracted) == 1 and len(published) == 2
    channels.unbind_identity(str(channels.list_identities()[0]['identityId']))
    with pytest.raises(PermissionError):
        await owner.commit(thread, messages, listening=listening)


@pytest.mark.anyio
async def test_owner_channel_revocation_prevents_pending_replay(tmp_path):
    sessions = SessionStore(tmp_path / 'sessions.db')
    thread = SimpleNamespace(thread_id='t', role_id='r', session_key='channel:t')
    proof = 'bound'
    called = []
    def publish(event):
        called.append(event)
        raise RuntimeError('retry later')
    owner = OwnerChannelMemory(sessions, tmp_path / 'roles',
        lambda *_: {'history': '历史', 'pending': '候选', 'history_entries': [], 'conversation': '主人事实'},
        publish, lambda *_: proof)
    await owner.commit(thread, [{'id': 'm', 'sequence': 1, 'createdAt': '2026-11-02T00:00:00+00:00'}])
    await owner.drain()
    proof = None
    await owner.retry('r', 'listening:t')
    saved = sessions.maintenance_progress('r', 'listening:t')
    assert len(called) == 1 and not saved['pendingEvents'] and len(saved['invalidatedEvents']) == 1
