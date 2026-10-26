import asyncio
import hashlib
import json
import time

import pytest

from backend.app.channels.source_authority import capture_channel_message_source
from backend.app.coding_tasks.application import CodingApplication
from backend.app.coding_tasks.config import CodingRoleConfig
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskOrigin, TaskRuntime
from test_coding_application import ControlledExecutor, GrantedConfiguration

pytestmark = pytest.mark.usefixtures('local_platform_only')


def frozen_channel_origin(channels, thread, parent_run_id, tool_call_id):
    # These tests start new effects, so retain the original local inbound
    # evidence rather than giving a legacy management-only origin new authority.
    sender = 'fixture-owner'
    code = channels.create_pairing_code(thread.role_id, thread.channel)
    assert channels.consume_pairing_code(code, role_id=thread.role_id, channel=thread.channel,
        scope='platform', platform_user_id=sender, account_id=thread.account_id, chat_id=thread.chat_id)
    created, message = channels.append_event(thread, direction='inbound', content='start coding',
        external_message_id='fixture-inbound',
        metadata={'senderId': sender, 'senderIsOwner': True, 'chatType': thread.chat_type})
    assert created
    payload = capture_channel_message_source(channels, thread, message, owner_context=True).to_dict()
    scope = {key: value for key, value in payload.items() if key != 'message_created_at'}
    return TaskOrigin(thread.role_id, thread.session_key, parent_run_id, tool_call_id,
        source_thread_id=thread.thread_id, source_account_id=thread.account_id,
        source_scope_id=hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest(),
        source_authority_json=json.dumps(payload, sort_keys=True, separators=(',', ':')))


def coding_completions(sessions, thread):
    return [message for message in sessions.list_messages(thread.session_key)
        if message.metadata.get('internalEvent') == 'coding_completion']


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
    origin = frozen_channel_origin(channels, thread, 'channel-parent', 'channel-call')
    try:
        accepted = application.start_task(origin, repository=str(tmp_path), requirement='channel result')
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            asyncio.run(application.tick())
            if coding_completions(sessions, thread): break
            time.sleep(.01)
        assert coding_completions(sessions, thread)[0].metadata['deliveryStatus'] == 'uncertain'
        asyncio.run(application.tick())
        observed = application.inspect(accepted['id'], origin)
        assert observed['deliveryStatus'] == 'sent'
        assert len(sends) == 1
        messages = coding_completions(sessions, thread)
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
    origin = frozen_channel_origin(channels, thread, 'channel-parent', 'channel-call')
    try:
        accepted = application.start_task(origin, repository=str(tmp_path), requirement='channel result')
        tasks.query(application.ledger.attempt(accepted['attemptId'])['runtimeTaskId'], origin, wait_seconds=2)
        async def scenario():
            await lock.acquire()
            pending = asyncio.create_task(application.tick())
            try:
                await asyncio.sleep(.1)
                assert not sends
                assert not coding_completions(sessions, thread)
            finally:
                lock.release()
                await pending
            assert len(sends) == 1
            assert len(coding_completions(sessions, thread)) == 1
        asyncio.run(scenario())
    finally:
        asyncio.run(application.close())
        tasks.close()
