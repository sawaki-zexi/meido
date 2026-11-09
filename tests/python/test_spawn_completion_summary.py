"""A failed auxiliary summary must not hide a real owned task's terminal state."""
import asyncio
import json
import sqlite3
import time

import pytest

from backend.app.agent_runtime import CancellationToken, ToolContext
from backend.app.agent_runtime.spawn_tool import SpawnManageTool
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskBudget, TaskOrigin, TaskOutcome, TaskRuntime
from backend.app.task_runtime.completion import TaskCompletionWorker


@pytest.fixture
def completed_task(tmp_path, request):
    status = getattr(request, 'param', 'succeeded')
    class Executor:
        def run(self, execution):
            return TaskOutcome(status, output_tail='PRIVATE_TASK_RESULT')
        def cancel(self):
            pass
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data' / 'sessions.db')
    channels = MultichannelStore(sessions.database_path, sessions)
    account = channels.register_channel_account('r', 'qq', account_id='summary-test', enabled=True, allow_group=True)
    thread = channels.ensure_channel_thread('r', 'qq', account.account_id, 'original-group', 'group')
    origin = TaskOrigin('r', thread.session_key, source_thread_id=thread.thread_id, source_account_id=account.account_id,
                        source_authority_json=json.dumps({'channel': 'qq'}))
    tasks = TaskRuntime(tmp_path / 'tasks')
    task_id = tasks.submit(origin, Executor(), budget=TaskBudget(), kind='spawn', completion=True)
    deadline = time.monotonic() + 3
    while tasks.query(task_id, origin).ended_at is None:
        assert time.monotonic() < deadline
        time.sleep(.01)
    yield tasks, sessions, origin, task_id
    tasks.close()


@pytest.mark.parametrize('completed_task', ['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'], indirect=True)
@pytest.mark.parametrize('failure', [RuntimeError, TimeoutError])
def test_summary_failure_reports_real_terminal_once_and_keeps_result_queryable(completed_task, failure):
    tasks, sessions, origin, task_id = completed_task
    status = tasks.query(task_id, origin).status
    summaries, sends = [], []
    async def summarize(snapshot):
        summaries.append(snapshot.task_id)
        raise failure('PRIVATE_PROVIDER_ERROR')
    async def deliver(snapshot, content):
        sends.append((snapshot.task_id, content))
        return 'sent', 'one-real-receipt'
    async def run():
        worker = TaskCompletionWorker(tasks, sessions, lock_for=lambda _: asyncio.Lock(), authorized=lambda _: True,
                                      summarize=summarize, deliver=deliver)
        worker.start()
        try:
            async with asyncio.timeout(3):
                while tasks.completion(task_id, origin)['status'] in {'pending', 'sending'}:
                    await asyncio.sleep(.01)
        finally:
            await worker.close()
        assert tasks.completion(task_id, origin)['status'] == 'sent'
        assert len(sends) == len(summaries) == 1
        message = sessions.list_messages(origin.session_key)[0]
        assert task_id in message.content and f'/ {status}]' in message.content
        assert f'查询任务 {task_id}' in message.content and 'spawn_manage' not in message.content
        assert 'PRIVATE_TASK_RESULT' not in message.content and 'PRIVATE_PROVIDER_ERROR' not in message.content
        assert message.metadata['taskStatus'] == status
        assert message.metadata['deliveryStatus'] == 'sent'
        manage = SpawnManageTool(tasks, origin_resolver=lambda _: origin, authorized=lambda _: True)
        query = await manage.execute({'action': 'get', 'job_id': task_id},
            ToolContext('r', origin.session_key, 'later-turn', CancellationToken()))
        assert query.details['status'] == status and query.content == 'PRIVATE_TASK_RESULT'
        tasks.close()
        restarted = TaskRuntime(tasks.root)
        restored_worker = TaskCompletionWorker(restarted, sessions, lock_for=lambda _: asyncio.Lock(),
            authorized=lambda _: True, summarize=summarize, deliver=deliver)
        try:
            restored_worker.start()
            await asyncio.sleep(.1)
            assert restarted.pending_completions() == ()
            assert len(summaries) == len(sends) == len(sessions.list_messages(origin.session_key)) == 1
        finally:
            await restored_worker.close()
            restarted.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure,expected', [(PermissionError, 'failed'), (sqlite3.OperationalError, 'pending'),
                                            (asyncio.CancelledError, 'pending')])
def test_summary_authority_storage_and_cancellation_keep_existing_semantics(completed_task, failure, expected):
    tasks, sessions, origin, task_id = completed_task
    calls = []
    async def summarize(_):
        raise failure('PRIVATE_FAILURE')
    async def deliver(*_):
        calls.append(True)
        return 'sent', 'must-not-send'
    async def run():
        worker = TaskCompletionWorker(tasks, sessions, lock_for=lambda _: asyncio.Lock(), authorized=lambda _: True,
                                      summarize=summarize, deliver=deliver)
        if failure is asyncio.CancelledError:
            with pytest.raises(asyncio.CancelledError):
                await worker._complete(tasks.query(task_id, origin))
        else:
            await worker._complete(tasks.query(task_id, origin))
        assert tasks.completion(task_id, origin)['status'] == expected
        assert not sessions.list_messages(origin.session_key) and not calls
        if failure is sqlite3.OperationalError:
            async def recovered(_):
                return 'summary-after-storage-recovery'
            worker.summarize = recovered
            await worker._complete(tasks.query(task_id, origin))
            assert tasks.completion(task_id, origin)['status'] == 'sent'
            assert 'summary-after-storage-recovery' in sessions.list_messages(origin.session_key)[0].content
    asyncio.run(run())


def test_summary_failure_cannot_send_after_origin_is_revoked(completed_task):
    tasks, sessions, origin, task_id = completed_task
    allowed = True
    async def summarize(_):
        nonlocal allowed
        allowed = False
        raise RuntimeError('PRIVATE_FAILURE')
    async def forbidden(*_):
        raise AssertionError('Revoked source must not receive a fallback')
    async def run():
        worker = TaskCompletionWorker(tasks, sessions, lock_for=lambda _: asyncio.Lock(), authorized=lambda _: allowed,
                                      summarize=summarize, deliver=forbidden)
        await worker._complete(tasks.query(task_id, origin))
        assert tasks.completion(task_id, origin)['status'] == 'failed'
        assert not sessions.list_messages(origin.session_key)
    asyncio.run(run())
