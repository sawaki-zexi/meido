"""Real persisted task/message boundaries, without a platform account."""
import asyncio
import json
import sqlite3
import sys
import time

import pytest

from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskBudget, TaskOrigin, TaskRuntime
from backend.app.task_runtime.completion import TaskCompletionWorker
from backend.app.task_runtime.process import ProcessExecutor


@pytest.fixture
def channel_task(tmp_path):
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data' / 'sessions.db')
    channels = MultichannelStore(sessions.database_path, sessions)
    account = channels.register_channel_account('r', 'qq', account_id='local-proof', enabled=True, allow_group=True)
    thread = channels.ensure_channel_thread('r', 'qq', account.account_id, 'controlled-group', 'group')
    origin = TaskOrigin('r', thread.session_key, source_thread_id=thread.thread_id, source_account_id=account.account_id,
                        source_authority_json=json.dumps({'channel': 'qq'}))
    tasks = TaskRuntime(tmp_path / 'tasks')
    task_id = tasks.submit(origin, ProcessExecutor([sys.executable, '-c', 'print("real-completion")'], tmp_path, {}),
                           budget=TaskBudget(), kind='spawn', completion=True)
    deadline = time.monotonic() + 3
    while tasks.query(task_id, origin).ended_at is None:
        assert time.monotonic() < deadline
        time.sleep(.01)
    yield tasks, sessions, origin, task_id
    tasks.close()


def test_channel_completion_is_never_replayed_before_delivery_metadata_is_added(channel_task):
    _tasks, sessions, origin, task_id = channel_task
    message = sessions.append_task_completion('r', origin.session_key, task_id, 'not-delivered', task_status='succeeded')
    assert message.metadata['channel'] == 'qq'
    assert message.metadata['deliveryStatus'] == 'pending'
    assert sessions.runtime_messages(origin.session_key) == []


@pytest.mark.parametrize('status', ['sending', 'sent', 'failed', 'uncertain', 'pending'])
@pytest.mark.parametrize('legacy_metadata', [False, True])
def test_restart_reconciles_authoritative_delivery_without_resending(channel_task, status, legacy_metadata):
    tasks, sessions, origin, task_id = channel_task
    message = sessions.append_task_completion('r', origin.session_key, task_id, 'completion-marker', task_status='succeeded')
    metadata = {} if legacy_metadata else {**message.metadata, 'channel': 'qq', 'deliveryStatus': 'pending'}
    sessions.update_message(message.id, message.content, 'completed', metadata=metadata)
    # The external attempt is durably entered before a receipt can be saved.
    tasks.update_completion(task_id, status='sending', content=message.content, message_id=message.id)
    tasks.update_completion(task_id, status=status, content=message.content, delivery_id='local-attempt')
    tasks.close()
    restarted = TaskRuntime(tasks.root)
    async def forbidden(*args):
        raise AssertionError('A recorded external attempt must never be automatically resent')
    async def run():
        worker = TaskCompletionWorker(restarted, sessions, lock_for=lambda _: asyncio.Lock(), authorized=lambda _: True,
                                      summarize=forbidden, deliver=forbidden)
        worker.start()
        try:
            await asyncio.sleep(.15)
        finally:
            await worker.close()
    try:
        asyncio.run(run())
        expected = 'uncertain' if status == 'sending' else status
        restored = sessions.list_messages(origin.session_key)
        assert len(restored) == 1
        assert restored[0].metadata['channel'] == 'qq'
        assert restored[0].metadata['deliveryStatus'] == expected
        assert restored[0].metadata['deliveryAttemptId'] == 'local-attempt'
        assert restarted.completion(task_id, origin)['status'] == expected
        assert restarted.pending_completions() == ()
        assert [item.content for item in sessions.runtime_messages(origin.session_key)] == (
            ['completion-marker'] if status == 'sent' else [])
    finally:
        restarted.close()


def test_legacy_pending_receipt_migrates_without_repeating_external_attempt(channel_task):
    tasks, sessions, origin, task_id = channel_task
    message = sessions.append_task_completion('r', origin.session_key, task_id, 'legacy-pending', task_status='succeeded')
    tasks.update_completion(task_id, status='pending', content=message.content, message_id=message.id, delivery_id='already-submitted')
    tasks.close()
    with sqlite3.connect(tasks.root / 'tasks.db') as connection:
        connection.execute('ALTER TABLE task_completions DROP COLUMN dispatched')
    restarted = TaskRuntime(tasks.root)
    try:
        assert restarted.completion(task_id, origin)['status'] == 'pending'
        assert restarted.pending_completions() == ()
    finally:
        restarted.close()


def test_confirmed_receipt_survives_a_local_message_ack_error(channel_task, monkeypatch):
    tasks, sessions, origin, task_id = channel_task
    update = sessions.update_message
    failed_once = False
    def interrupted_ack(*args, **kwargs):
        nonlocal failed_once
        if kwargs.get('metadata', {}).get('deliveryStatus') == 'sent' and not failed_once:
            failed_once = True
            raise OSError('controlled message database acknowledgement failure')
        return update(*args, **kwargs)
    monkeypatch.setattr(sessions, 'update_message', interrupted_ack)
    sends = []
    async def summarize(_snapshot):
        return 'confirmed-content'
    async def deliver(_snapshot, _content):
        sends.append(True)
        return 'sent', 'confirmed-attempt'
    async def run():
        worker = TaskCompletionWorker(tasks, sessions, lock_for=lambda _: asyncio.Lock(), authorized=lambda _: True,
                                      summarize=summarize, deliver=deliver)
        worker.start()
        try:
            async with asyncio.timeout(3):
                while not sends:
                    await asyncio.sleep(.01)
            await asyncio.sleep(.1)
        finally:
            await worker.close()
    asyncio.run(run())
    assert failed_once
    assert len(sends) == 1
    assert tasks.completion(task_id, origin)['status'] == 'sent'
    message = sessions.list_messages(origin.session_key)[0]
    assert message.metadata['deliveryStatus'] == 'sent'
    assert message.metadata['deliveryAttemptId'] == 'confirmed-attempt'
