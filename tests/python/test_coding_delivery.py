import asyncio
import time

import pytest

from backend.app.coding_tasks.application import CodingApplication
from backend.app.coding_tasks.config import CodingRoleConfig
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskOrigin, TaskRuntime
from test_coding_application import ControlledExecutor, GrantedConfiguration

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_real_sent_receipt_repairs_an_already_synced_uncertain_projection_without_resending(tmp_path):
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    channels = MultichannelStore(sessions.database_path, sessions)
    account = channels.register_channel_account('role-a', 'qq', account_id='fixture-account', enabled=True, allow_group=True)
    thread = channels.ensure_channel_thread('role-a', 'qq', account.account_id, 'fixture-group', 'group')
    tasks = TaskRuntime(tmp_path / 'tasks')
    sends = []
    async def deliver(record, content, authorized):
        assert authorized()
        sends.append(record['attemptId'])
        receipt = channels.begin_delivery(thread, content, source_kind='coding_completion', source_tool_call_id=record['attemptId'])
        channels.finish_delivery(receipt, status='sent', platform_message_id='fixture-message')
        raise OSError('fixture lost acknowledgement after durable platform receipt')
    def receipt(record):
        value = channels.delivery_for_source('role-a', source_kind='coding_completion', source_tool_call_id=record['attemptId'])
        return {'status': value[0], 'attemptId': value[1]} if value else None
    application = CodingApplication(tmp_path / 'coding', tasks, sessions,
        role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True,
        config=GrantedConfiguration(), executor_factory=ControlledExecutor, deliver=deliver, receipt=receipt)
    origin = TaskOrigin('role-a', thread.session_key, 'channel-parent', 'channel-call',
        source_thread_id=thread.thread_id, source_account_id=account.account_id, source_scope_id='frozen-fixture')
    try:
        accepted = application.start_task(origin, repository=str(tmp_path), requirement='channel result')
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            asyncio.run(application.tick())
            if sessions.list_messages(thread.session_key): break
            time.sleep(.01)
        assert sessions.list_messages(thread.session_key)[0].metadata['deliveryStatus'] == 'uncertain'
        asyncio.run(application.tick())
        observed = application.inspect(accepted['id'], origin)
        assert observed['deliveryStatus'] == 'sent'
        assert len(sends) == 1
        messages = sessions.list_messages(thread.session_key)
        assert len(messages) == 1 and messages[0].metadata['deliveryStatus'] == 'sent'
        assert messages[0].metadata['channel'] == 'qq' and messages[0].runId is None
        assert observed['runtimeAckDirty'] is False
        application.ledger.update_attempt(accepted['attemptId'], messageSynced=False)
        application.authorized = lambda _: False
        asyncio.run(application.tick())
        assert application.ledger.attempt(accepted['attemptId'])['deliveryStatus'] == 'sent'
    finally:
        asyncio.run(application.close())
        tasks.close()


def test_external_completion_waits_for_original_role_lock_before_send(tmp_path):
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    channels = MultichannelStore(sessions.database_path, sessions)
    account = channels.register_channel_account('role-a', 'qq', account_id='fixture-account', enabled=True)
    thread = channels.ensure_channel_thread('role-a', 'qq', account.account_id, 'owner-chat', 'private')
    tasks = TaskRuntime(tmp_path / 'tasks')
    sends = []
    lock = asyncio.Lock()
    async def deliver(record, content, authorized):
        assert authorized()
        sends.append(record['attemptId'])
        return {'status': 'sent', 'attemptId': 'real-fixture-receipt'}
    application = CodingApplication(tmp_path / 'coding', tasks, sessions,
        role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True,
        config=GrantedConfiguration(), executor_factory=ControlledExecutor, deliver=deliver,
        lock_for=lambda _: lock)
    origin = TaskOrigin('role-a', thread.session_key, 'channel-parent', 'channel-call',
        source_thread_id=thread.thread_id, source_account_id=account.account_id)
    try:
        accepted = application.start_task(origin, repository=str(tmp_path), requirement='channel result')
        tasks.query(application.ledger.attempt(accepted['attemptId'])['runtimeTaskId'], origin, wait_seconds=2)
        async def scenario():
            await lock.acquire()
            pending = asyncio.create_task(application.tick())
            try:
                await asyncio.sleep(.1)
                assert not sends
                assert not sessions.list_messages(thread.session_key)
            finally:
                lock.release()
                await pending
            assert len(sends) == 1
            assert len(sessions.list_messages(thread.session_key)) == 1
        asyncio.run(scenario())
    finally:
        asyncio.run(application.close())
        tasks.close()
