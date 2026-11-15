"""Stable material operations use the real ChannelHost ledger, never a replay queue."""
import asyncio
import pytest

from backend.app.channels.host import ChannelHost
from backend.app.channels.models import DeliveryStatus, TargetKind, target_from_chat
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore


def setup(tmp_path, transport):
    sessions = SessionStore(tmp_path / 'sessions.db')
    store = MultichannelStore(sessions.database_path, sessions)
    account = store.register_channel_account('role', 'qq', account_id='qq', enabled=True, allow_group=True)
    thread = store.ensure_channel_thread('role', 'qq', account.account_id, '100', 'group')
    target = target_from_chat(account_id=account.account_id, chat_id='100', kind=TargetKind.GROUP)
    store.remember_targets('role', [target])
    host = ChannelHost(store)
    host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=transport))
    async def send(content='hello'):
        return await host.account_send('role', 'qq', target.id, content, actor='reply',
            source_thread_id=thread.thread_id, source_session_key=thread.session_key, source_run_id='run',
            source_kind='chat_meme', operation_key='stable-op', authorize_source=lambda: True,
            material_usage={'assetId': 'asset', 'sha256': 'a' * 64, 'semanticRevision': 1})
    return host, store, send


def test_same_operation_reuses_durable_receipt_and_changed_parameters_are_rejected(tmp_path):
    calls = []
    async def transport(action, params):
        calls.append(action)
        return {'message_id': '1'}
    host, store, send = setup(tmp_path, transport)
    async def scenario():
        first = await send()
        replay = await send()
        assert first.status is replay.status is DeliveryStatus.SENT
        assert first.attempt_id == replay.attempt_id
        assert calls == ['send_group_msg']
        with pytest.raises(ValueError, match='operation'):
            await send('different')
        assert store.material_usage('role')['a' * 64]['successes'] == 1
        assert store.material_usage('role')['a' * 64]['attempts'] == 1
    asyncio.run(scenario())


def test_concurrent_pending_operation_never_dispatches_twice(tmp_path):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def transport(action, params):
            calls.append(action)
            entered.set()
            await release.wait()
            return {'message_id': '1'}
        _, store, send = setup(tmp_path, transport)
        first = asyncio.create_task(send())
        await asyncio.wait_for(entered.wait(), 2)
        replay = await send()
        assert replay.status is DeliveryStatus.UNCERTAIN
        assert store.material_usage('role')['a' * 64]['successes'] == 0
        release.set()
        await first
        assert calls == ['send_group_msg']
    asyncio.run(scenario())
