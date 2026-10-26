import asyncio
from types import SimpleNamespace

import pytest

from backend.app import main
from backend.app.agent_runtime import RuntimeManager, TextDeltaEvent
from backend.app.agent_runtime.capabilities import CapabilityRegistry
from backend.app.channels.delivery import OutboundScheduler
from backend.app.channels.group_tools import MultichannelGroupRepository
from backend.app.channels.host import ChannelHost
from backend.app.channels.models import TargetKind, target_from_chat
from backend.app.channels.telegram import TelegramAccountAdapter
from backend.app.model_config import ModelConfigurationStore
from backend.app.models import RoleInput, RoleProfile
from backend.app.multichannel import MultichannelStore
from backend.app.role_store import RoleStore
from backend.app.session_store import SessionStore
from test_runtime_delivery import MemoryCredentials, owned_tasks  # noqa: F401


def test_cancelled_real_runtime_retains_stream_receipt_in_original_message(tmp_path, monkeypatch, owned_tasks):
    roles = RoleStore(tmp_path / 'roles')
    role = roles.create(RoleInput(name='fixture', profile=RoleProfile(profile='fixture')))
    sessions = SessionStore(tmp_path / 'sessions.db')
    channels = MultichannelStore(sessions.database_path, sessions, MemoryCredentials())
    account = channels.save_account(role.id, token='synthetic', enabled=True)
    thread = channels.ensure_thread(role.id, account.account_id, '99', 'private')
    code = channels.create_pairing_code(role.id)
    assert channels.consume_pairing_code(code, role_id=role.id, channel='telegram', scope='platform',
        platform_user_id='99', account_id=account.account_id, chat_id='99')
    _, user = channels.append_event(thread, direction='inbound', content='hello', external_message_id='1', metadata={'senderId': '99', 'senderIsOwner': True})
    target = target_from_chat(account_id=account.account_id, chat_id='99', kind=TargetKind.PRIVATE)
    channels.remember_targets(role.id, [target])
    shown = asyncio.Event()
    class Transport:
        async def send_message(self, *args, **kwargs):
            return {'message_id': 'real-message'}
        async def edit_message_text(self, token, chat, message, text, **kwargs):
            assert text == 'CONFIRMED'
            shown.set()
            return {'message_id': message}
    adapter = TelegramAccountAdapter(account.account_id, lambda: 'synthetic', Transport(), scheduler=OutboundScheduler())
    host = ChannelHost(channels)
    host.register(adapter)
    class Provider:
        def __init__(self, adapter, role, configuration, *args, **kwargs):
            self.adapter, self.role, self.configuration = adapter, role, configuration
        async def stream_response(self, **kwargs):
            yield TextDeltaEvent('CONFIRMED')
            await asyncio.Event().wait()
    async def resolve(_role, *, session_key, run_id, **kwargs):
        return CapabilityRegistry(()).resolve(role_id=role.id, session_key=session_key, run_id=run_id)
    async def commit(*args):
        pass
    for name, value in {
        'store': roles, 'session_store': sessions, 'multichannel_store': channels,
        'runtime_manager': RuntimeManager(sessions), 'role_locks': {},
        'model_configuration_store': ModelConfigurationStore(tmp_path / 'models.json'),
        'group_repository': MultichannelGroupRepository(channels), 'channel_host': host,
        'channel_adapters': {account.account_id: adapter}, 'MeidoProvider': Provider,
        '_runtime_tool_candidates': lambda *args, **kwargs: (), '_runtime_capabilities': resolve,
        '_role_memory_context': lambda *args, **kwargs: '',
        'memory_worker': SimpleNamespace(role_deletion_blocked=lambda *args: False, role_lock_for=lambda *args: asyncio.Lock()),
        'channel_memory_maintenance': SimpleNamespace(commit_turn=commit),
    }.items():
        monkeypatch.setattr(main, name, value)
    async def scenario():
        task = asyncio.create_task(main._handle_channel_message(thread, 'hello',
            {'messageId': user.id, 'senderId': '99', 'platformMessageId': '1'}, True))
        await asyncio.wait_for(shown.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    ledger = channels.list_deliveries(role.id)[0]
    reply = sessions.list_messages(thread.session_key)[-1]
    assert reply.metadata.get('deliveryAttemptId') == ledger['attemptId']
    assert reply.metadata['platformMessageId'] == 'real-message'
    assert reply.metadata['partialDelivery'] and reply.metadata['deliveredContent'] == 'CONFIRMED'
    assert sessions.runtime_messages(thread.session_key)[-1].content == 'CONFIRMED'
    assert channels.owner_context(role.id, exclude_thread_id='desktop')[-1][2] == 'CONFIRMED'
    assert channels.owner_context_messages(role.id)[-1].content == 'CONFIRMED'
