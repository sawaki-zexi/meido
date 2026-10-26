import asyncio

import httpx
import pytest

from backend.app.channels.delivery import DeliveryRejected, OutboundScheduler
from backend.app.channels.feishu import FeishuAdapter, FeishuConfig
from backend.app.channels.host import ChannelHost
from backend.app.channels.models import DeliveryStatus, OutboundMessage, TargetKind, target_from_chat
from backend.app.channels.qqbot import QQBotAdapter, QQBotConfig
from backend.app.channels.telegram import TelegramAccountAdapter
from backend.app.channels.transports import FeishuWebhookTransport
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore
from test_delivery_recovery import Clock
from test_host import MemoryCredentials


def test_account_disable_then_reenable_during_wait_does_not_revive_old_tail(tmp_path):
    sessions = SessionStore(tmp_path / 'sessions.db')
    store = MultichannelStore(sessions.database_path, sessions, MemoryCredentials())
    account = store.save_account('r', token='synthetic', enabled=True)
    target = target_from_chat(account_id=account.account_id, chat_id='1', kind=TargetKind.PRIVATE)
    store.remember_targets('r', [target])
    clock, calls = Clock(), []
    def toggle():
        store.set_account_state(account.account_id, enabled=False)
        store.set_account_state(account.account_id, enabled=True)
    clock.after_wait = toggle
    class Transport:
        async def send_message(self, token, chat, text, **options):
            calls.append(text)
            return {'message_id': str(len(calls))}
    scheduler = OutboundScheduler(send_interval=2, clock=lambda: clock.now, sleep=clock.sleep)
    adapter = TelegramAccountAdapter(account.account_id, lambda: 'synthetic', Transport(), scheduler=scheduler)
    host = ChannelHost(store)
    host.register(adapter)
    result = asyncio.run(host.account_send('r', account.account_id, target.id, 'x' * 9000, actor='owner'))
    assert result.status is DeliveryStatus.FAILED and result.raw['partialDelivery']
    assert len(calls) == 1 and result.error == 'source_revoked'


def test_feishu_rechecks_authorization_after_token_wait_before_visible_post():
    allowed, requests = [True], []
    async def scenario():
        def handle(request):
            requests.append(request.url.path)
            allowed[0] = False
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'synthetic', 'expire': 7200})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            adapter = FeishuAdapter(FeishuConfig('a', 'app'), transport=FeishuWebhookTransport('app', 'secret', http_client=client))
            target = target_from_chat(account_id='a', chat_id='chat', kind=TargetKind.PRIVATE)
            adapter.remember_target(target)
            return await adapter.send(OutboundMessage(target, 'text', authorize_dispatch=lambda: allowed[0]))
    result = asyncio.run(scenario())
    assert result.status is DeliveryStatus.FAILED and result.error == 'source_revoked'
    assert len(requests) == 1 and '/auth/' in requests[0]


@pytest.mark.parametrize('channel', ['telegram', 'feishu', 'qqbot'])
def test_stream_revocation_preserves_receipt_but_never_posts_another_update(channel):
    allowed, calls, snapshots = [True], [], []
    class Telegram:
        async def send_message(self, *args, **kwargs):
            calls.append('create')
            return {'message_id': 'real'}
        async def edit_message_text(self, *args, **kwargs):
            calls.append('edit')
            return {'message_id': 'real'}
    async def transport(operation, params):
        calls.append(operation)
        return {'card_id': 'card'} if operation == 'cardkit.create' else {'message_id': 'real'}
    adapter = (TelegramAccountAdapter('a', lambda: 'synthetic', Telegram(), scheduler=OutboundScheduler()) if channel == 'telegram'
        else FeishuAdapter(FeishuConfig('a', 'app'), transport=transport, scheduler=OutboundScheduler()) if channel == 'feishu'
        else QQBotAdapter(QQBotConfig('a', 'app'), transport=transport))
    target = target_from_chat(account_id='a', chat_id='chat', kind=TargetKind.PRIVATE, external_message_id='source')
    adapter.remember_target(target)
    async def scenario():
        session = await adapter.stream_start(target, outbound=OutboundMessage(target, '',
            authorize_dispatch=lambda: allowed[0], record_delivery=snapshots.append))
        await adapter.stream_update(session, 'confirmed preview')
        if getattr(session, 'task', None) is not None:
            await session.task
        before = len(calls)
        allowed[0] = False
        result = await adapter.stream_finalize(session, 'unconfirmed final')
        assert len(calls) == before
        assert result.status is DeliveryStatus.FAILED
        assert result.raw['deliveredContent'] == 'confirmed preview'
        assert snapshots[-1]['parts'][0]['platformMessageId'] == 'real'
    asyncio.run(scenario())


def test_queue_capacity_and_unknown_error_are_explicit_without_retry():
    class Transport:
        calls = 0
        async def send_message(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError('opaque outcome')
    target = target_from_chat(account_id='a', chat_id='1', kind=TargetKind.PRIVATE)
    transport = Transport()
    scheduler = OutboundScheduler(capacity=0)
    adapter = TelegramAccountAdapter('a', lambda: 'synthetic', transport, scheduler=scheduler)
    adapter.remember_target(target)
    result = asyncio.run(adapter.send(OutboundMessage(target, 'text')))
    assert result.status is DeliveryStatus.FAILED and result.error == 'delivery_queue_full' and transport.calls == 0
    scheduler.capacity = 1
    result = asyncio.run(adapter.send(OutboundMessage(target, 'text')))
    assert result.status is DeliveryStatus.UNCERTAIN and transport.calls == 1
