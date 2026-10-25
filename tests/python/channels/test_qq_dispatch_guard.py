import asyncio
import pytest

from backend.app.channels import DeliveryStatus, NapCatAdapter, NapCatConfig, OutboundMessage, TargetKind, target_from_chat
from backend.app.channels.models import Attachment
from backend.app.channels.transports import NapCatWebSocketTransport
from backend.app.channels.base import UnsupportedTargetError


def test_revocation_during_image_preparation_never_sends(monkeypatch):
    async def scenario():
        calls = []
        allowed = True
        async def transport(action, params):
            calls.append(action)
            return {'message_id': 12}
        adapter = NapCatAdapter(NapCatConfig('qq'), transport=transport)
        target = target_from_chat(account_id='qq', chat_id='100', kind=TargetKind.GROUP)
        adapter.remember_target(target)
        async def prepare(item):
            nonlocal allowed
            await asyncio.sleep(0)
            allowed = False
            return {'type': 'image', 'data': {'file': 'fixture'}}
        monkeypatch.setattr(adapter, '_image_segment', prepare)
        result = await adapter.send(OutboundMessage(target, 'hello', attachments=(Attachment('image', 'fixture'),),
            authorize_dispatch=lambda: allowed))
        assert result.status is DeliveryStatus.FAILED
        assert not calls
    asyncio.run(scenario())


def test_partial_long_reply_stops_new_dispatch_but_preserves_uncertainty():
    async def scenario():
        calls = []
        allowed = True
        async def transport(action, params):
            nonlocal allowed
            calls.append(action)
            if action == 'get_login_info':
                return {'user_id': 99, 'nickname': 'bot'}
            allowed = False
            return {'message_id': 12}
        adapter = NapCatAdapter(NapCatConfig('qq'), transport=transport)
        target = target_from_chat(account_id='qq', chat_id='100', kind=TargetKind.GROUP)
        adapter.remember_target(target)
        result = await adapter.send(OutboundMessage(target, 'long' * 100,
            reply_to_message_id='1', authorize_dispatch=lambda: allowed))
        assert calls == ['get_login_info', 'send_group_msg']
        assert result.status is DeliveryStatus.UNCERTAIN
    asyncio.run(scenario())


def test_socket_lock_wait_rechecks_authority_before_writing():
    async def scenario():
        transport = NapCatWebSocketTransport('ws://fixture.invalid')
        sent = []
        class Socket:
            async def send(self, data):
                sent.append(data)
        transport._ws = Socket()
        transport.connected = True
        allowed = True
        await transport._send_lock.acquire()
        task = asyncio.create_task(transport.send_authorized('send_group_msg', {}, authorize=lambda: allowed))
        await asyncio.sleep(0)
        allowed = False
        transport._send_lock.release()
        with pytest.raises(UnsupportedTargetError):
            await task
        assert sent == [] and transport._pending == {}
    asyncio.run(scenario())
