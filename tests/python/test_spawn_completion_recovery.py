"""Real persisted task/message boundaries, without a platform account."""
import asyncio
import json
import sqlite3
import sys
import threading
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


def test_expired_task_still_repairs_its_unsynchronized_confirmed_message(channel_task):
    tasks, sessions, origin, task_id = channel_task
    message = sessions.append_task_completion('r', origin.session_key, task_id, 'expired-confirmed', task_status='succeeded')
    tasks.update_completion(task_id, status='sending', content=message.content, message_id=message.id)
    tasks.update_completion(task_id, status='sent', content=message.content, delivery_id='confirmed-before-crash')
    tasks.close()
    restarted = TaskRuntime(tasks.root, retention_seconds=-1)
    async def forbidden(*args):
        raise AssertionError('Confirmed delivery cannot be resent during recovery')
    async def run():
        worker = TaskCompletionWorker(restarted, sessions, lock_for=lambda _: asyncio.Lock(), authorized=lambda _: True,
                                      summarize=forbidden, deliver=forbidden)
        worker.start()
        try:
            await asyncio.sleep(.1)
        finally:
            await worker.close()
    try:
        asyncio.run(run())
        assert [item.content for item in sessions.runtime_messages(origin.session_key)] == ['expired-confirmed']
        # Repaired terminal events may now expire normally; no unbounded pin.
        assert restarted.list(origin, kind='spawn') == ()
    finally:
        restarted.close()


def test_desktop_completed_message_is_sent_even_when_sync_ack_encounters_sqlite_busy(tmp_path):
    class BusyOnceRuntime(TaskRuntime):
        failed_once = False
        def acknowledge_completion_message(self, task_id):
            if self.failed_once:
                return super().acknowledge_completion_message(task_id)
            self.failed_once = True
            with self._lock:
                self._db.execute('PRAGMA busy_timeout=1')
                blocker = sqlite3.connect(self.root / 'tasks.db')
                blocker.execute('BEGIN EXCLUSIVE')
                try:
                    return super().acknowledge_completion_message(task_id)
                finally:
                    blocker.rollback()
                    blocker.close()
                    self._db.execute('PRAGMA busy_timeout=5000')
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data' / 'sessions.db')
    sessions.open_role_session('r')
    tasks = BusyOnceRuntime(tmp_path / 'tasks')
    origin = TaskOrigin('r', 'role:r')
    task_id = tasks.submit(origin, ProcessExecutor([sys.executable, '-c', 'print("real-child")'], tmp_path, {}),
                           budget=TaskBudget(), kind='spawn', completion=True)
    deadline = time.monotonic() + 3
    while tasks.query(task_id, origin).ended_at is None:
        assert time.monotonic() < deadline
        time.sleep(.01)
    async def summarize(_snapshot):
        return 'desktop-completed-marker'
    async def forbidden(*_args):
        raise AssertionError('Desktop completion cannot send externally')
    async def run():
        worker = TaskCompletionWorker(tasks, sessions, lock_for=lambda _: asyncio.Lock(), authorized=lambda _: True,
                                      summarize=summarize, deliver=forbidden)
        worker.start()
        try:
            await asyncio.sleep(.15)
        finally:
            await worker.close()
    try:
        asyncio.run(run())
        assert tasks.failed_once
        assert tasks.completion(task_id, origin)['status'] == 'sent'
        assert 'desktop-completed-marker' in sessions.runtime_messages(origin.session_key)[0].content
    finally:
        tasks.close()


def test_transient_sqlite_busy_during_recovery_does_not_stop_other_completions(channel_task, tmp_path):
    tasks, sessions, origin, task_id = channel_task
    message = sessions.append_task_completion('r', origin.session_key, task_id, 'confirmed-before-lock', task_status='succeeded')
    tasks.update_completion(task_id, status='sending', content=message.content, message_id=message.id)
    tasks.update_completion(task_id, status='sent', content=message.content, delivery_id='confirmed-attempt')
    another = tasks.submit(origin, ProcessExecutor([sys.executable, '-c', 'print("another-real-child")'], tmp_path, {}),
                           budget=TaskBudget(), kind='spawn', completion=True)
    deadline = time.monotonic() + 3
    while tasks.query(another, origin).ended_at is None:
        assert time.monotonic() < deadline
        time.sleep(.01)
    locked, release = threading.Event(), threading.Event()
    def block_database():
        with sqlite3.connect(sessions.database_path) as connection:
            connection.execute('BEGIN EXCLUSIVE')
            locked.set()
            release.wait(6.5)
            connection.rollback()
    blocker = threading.Thread(target=block_database)
    blocker.start()
    assert locked.wait(3)
    sends = []
    async def summarize(_snapshot):
        return 'another-completion-marker'
    async def deliver(snapshot, _content):
        sends.append(snapshot.task_id)
        return 'sent', 'another-confirmed-attempt'
    async def run():
        worker = TaskCompletionWorker(tasks, sessions, lock_for=lambda _: asyncio.Lock(), authorized=lambda _: True,
                                      summarize=summarize, deliver=deliver)
        worker.start()
        try:
            async with asyncio.timeout(10):
                while tasks.completion(another, origin)['status'] != 'sent':
                    await asyncio.sleep(.02)
        finally:
            await worker.close()
    try:
        asyncio.run(run())
        assert sends == [another]
        assert {item.content for item in sessions.runtime_messages(origin.session_key)} == {
            'confirmed-before-lock', f'[内部子任务完成 / {another} / succeeded]\nanother-completion-marker'}
    finally:
        release.set()
        blocker.join(2)
