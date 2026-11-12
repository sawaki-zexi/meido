"""Natural admission keeps provenance without promoting temporary hearing."""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from backend.app import main
from backend.app.channels.consolidation import ChannelMemoryMaintenance
from backend.app.channels.group_tools import MultichannelGroupRepository
from backend.app.channels.conversation_context import current_timeline
from backend.app.conversation_history import ConversationHistoryView
from backend.app.models import AgentRun
from backend.app.session_store import SessionStore
from test_listening_maintenance import owner_setup


def admit(channels, thread, record, name):
    run = AgentRun(runId=name, roleId=thread.role_id, sessionKey=thread.session_key,
        status='running', startedAt=datetime.now(timezone.utc), modelSnapshot={'batchId': name})
    return channels.sessions.admit_natural_run(run, record)


def test_natural_admission_does_not_route_hearing_through_ordinary_owner_memory(tmp_path, monkeypatch):
    extracted = []

    def extract(_role, messages):
        extracted.extend(message['content'] for message in messages)
        text = ' '.join(message['content'] for message in messages)
        return {'history': text, 'pending': text, 'history_entries': [(text, 0)], 'conversation': text}

    channels, thread, owner, folder = owner_setup(tmp_path, monkeypatch, extract)
    channels.listening.set_cap(thread.role_id, thread.thread_id, 1)
    records = [channels.listening.hear(thread, text, str(number),
        {'senderId': 'owner', 'senderIsOwner': True, 'triggered': False})
        for number, text in enumerate(['DURABLE_FACT', 'OVERFLOW_SECRET'])]
    for number, record in enumerate(records):
        run = admit(channels, thread, record, f'run-{number}')
        channels.sessions.update_run(run.runId, status='completed')
    reply = channels.sessions.append_message(thread.session_key, 'assistant', '已确认的角色回复',
        metadata={'channel': 'qq', 'direction': 'outbound', 'deliveryStatus': 'sent'})
    summarized = []

    async def summarize(_thread, messages, *_):
        summarized.extend(message['content'] for message in messages)
        return {'members': []}

    worker = ChannelMemoryMaintenance(lambda: channels, lambda: MultichannelGroupRepository(channels),
        summarize, lambda *_: None, owner_statements=main._ingest_owner_listening_memory)
    worker.owner_documents = owner

    async def run():
        assert await worker.ensure_coverage(thread.role_id, thread.session_key, [reply.id])
        await owner.drain()
        assert extracted == [] and summarized == ['已确认的角色回复']
        worker.clock = lambda: datetime.now().timestamp() + worker.IDLE_SECONDS + 1
        assert await worker.consolidate(thread.thread_id, listening=True)
        await owner.drain()
        assert extracted == ['DURABLE_FACT']
        assert 'OVERFLOW_SECRET' not in (folder / 'PENDING.md').read_text(encoding='utf-8')

    asyncio.run(run())


def test_overflow_receipt_preserves_replay_claim_without_persisting_original(tmp_path, monkeypatch):
    channels, thread, _, _ = owner_setup(tmp_path, monkeypatch, lambda *_: None)
    channels.listening.set_cap(thread.role_id, thread.thread_id, 1)
    channels.listening.hear(thread, 'durable', 'first', {'senderId': 'owner', 'triggered': False})
    record = channels.listening.hear(thread, 'OVERFLOW_SECRET', 'overflow',
        {'senderId': 'owner', 'senderIsOwner': True, 'triggered': False,
         'replyToContent': 'QUOTED_SECRET', 'attachments': [{'id': 'attachment', 'kind': 'image', 'text': 'ATTACHMENT_SECRET'}]})
    run = admit(channels, thread, record, 'overflow-run')
    assert channels.sessions.user_message_for_run(run.runId).content == 'OVERFLOW_SECRET'
    with channels._connect() as db:
        saved = db.execute('SELECT content,metadata_json FROM messages WHERE message_id=?', (record['id'],)).fetchone()
        event = db.execute('SELECT metadata_json FROM channel_events WHERE external_message_id=?', ('overflow',)).fetchone()
    assert all(secret not in str((tuple(saved), tuple(event))) for secret in ('OVERFLOW_SECRET', 'QUOTED_SECRET', 'ATTACHMENT_SECRET'))
    channels.sessions.update_run(run.runId, status='completed')
    assert channels.sessions.user_message_for_run(run.runId).content == ''
    assert not channels.sessions._natural_inputs
    restarted = SessionStore(channels.sessions.database_path)
    with pytest.raises(PermissionError):
        restarted.admit_natural_run(run.model_copy(update={'runId': 'replayed'}), record)
    assert restarted.get_run('replayed') is None


def test_legacy_listening_copy_never_revives_an_evicted_original(tmp_path, monkeypatch):
    channels, thread, _, _ = owner_setup(tmp_path, monkeypatch, lambda *_: None)
    channels.listening.set_cap(thread.role_id, thread.thread_id, 1)
    channels.listening.hear(thread, 'durable', 'first', {'senderId': 'owner', 'triggered': False})
    record = channels.listening.hear(thread, 'EPHEMERAL', 'overflow', {'senderId': 'owner', 'triggered': False})
    channels.sessions.append_message(thread.session_key, 'user', record['content'], metadata=record['metadata'])
    for index in range(30):
        channels.listening.hear(thread, 'newer', f'new-{index}', {'senderId': 'visitor'})
    channels.listening._windows.clear()
    assert all(message['content'] != 'EPHEMERAL' for message in current_timeline(channels, thread))
    view = ConversationHistoryView(channels, role_id=thread.role_id, thread_id=thread.thread_id)
    assert all(message.content != 'EPHEMERAL' for message in view.read())


@pytest.mark.parametrize('terminal', ['completed', 'failed', 'cancelled', 'max_turns'])
@pytest.mark.parametrize('finish', [False, True])
def test_active_original_is_released_on_every_terminal_path(tmp_path, monkeypatch, terminal, finish):
    channels, thread, _, _ = owner_setup(tmp_path, monkeypatch, lambda *_: None)
    channels.listening.set_cap(thread.role_id, thread.thread_id, 1)
    channels.listening.hear(thread, 'durable', 'first', {'senderId': 'owner'})
    record = channels.listening.hear(thread, 'overflow', 'second',
        {'senderId': 'owner', 'triggered': False, 'attachments': [{'kind': 'image', 'id': 'active-image'}]})
    run = admit(channels, thread, record, 'active')
    for index in range(30):
        channels.listening.hear(thread, 'later', str(index), {'senderId': 'visitor'})
    assert channels.sessions.user_message_for_run(run.runId).metadata['attachments'] == record['metadata']['attachments']
    if finish:
        channels.sessions.finish_run(run.runId, status=terminal, assistant_message_id=None,
            assistant_content='', turn_count=1)
    else:
        channels.sessions.update_run(run.runId, status=terminal)
    assert not channels.sessions._natural_inputs
    missing = channels.sessions.user_message_for_run(run.runId)
    assert missing.content == '' and missing.metadata['originalUnavailable'] is True
    assert 'attachments' not in missing.metadata


def test_legacy_coverage_uses_durable_original_and_rejects_missing_source(tmp_path, monkeypatch):
    channels, thread, _, _ = owner_setup(tmp_path, monkeypatch, lambda *_: None)
    channels.listening.set_cap(thread.role_id, thread.thread_id, 1)
    durable = channels.listening.hear(thread, 'durable', 'first', {'senderId': 'visitor', 'triggered': False})
    overflow = channels.listening.hear(thread, 'overflow', 'second', {'senderId': 'visitor', 'triggered': False})
    for index, record in enumerate((durable, overflow)):
        run = admit(channels, thread, record, str(index))
        channels.sessions.update_run(run.runId, status='completed')
        # Existing releases stored a full copy in the ordinary message table.
        with channels._connect() as db:
            db.execute("UPDATE messages SET content=?,metadata_json=?,message_type='text' WHERE message_id=?",
                (record['content'], json.dumps(record['metadata']), record['id']))
    summarized = []

    async def summarize(_thread, messages, *_):
        summarized.extend(message['content'] for message in messages)
        return {'members': []}

    worker = ChannelMemoryMaintenance(lambda: channels, lambda: MultichannelGroupRepository(channels),
        summarize, lambda *_: None)

    async def run():
        assert await worker.ensure_coverage(thread.role_id, thread.session_key, [durable['id']])
        assert summarized == ['durable']
        assert not await worker.ensure_coverage(thread.role_id, thread.session_key, [overflow['id']])
        restored = SessionStore(channels.sessions.database_path)
        assert restored.user_message_for_run('0').content == 'durable'
        assert restored.user_message_for_run('1').content == ''
        channels.listening.clear(thread.role_id, thread.thread_id)
        assert restored.user_message_for_run('0') is None
        assert not await worker.ensure_coverage(thread.role_id, thread.session_key, [durable['id']])

    asyncio.run(run())
