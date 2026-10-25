"""Current-source sticker authority is checked at the actual QQ handoff."""
import asyncio
import io
import json

import pytest
from PIL import Image

from backend.app.channels.host import ChannelHost
from backend.app.channels.models import DeliveryStatus, TargetKind, target_from_chat
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.channels.transports import NapCatWebSocketTransport
from backend.app.chat_attachments import save_attachment, resolve_attachment
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore


def setup(tmp_path, transport, fmt="PNG"):
    extension, mime = ("png", "image/png") if fmt == "PNG" else ("gif", "image/gif")
    stream = io.BytesIO()
    Image.new('RGB', (2, 2), 'red').save(stream, fmt, **(
        {'save_all': True, 'append_images': [Image.new('RGB', (2, 2), 'blue')], 'duration': 50, 'loop': 0}
        if fmt == 'GIF' else {}))
    raw = stream.getvalue()
    item = save_attachment(tmp_path / 'role', 'meme.' + extension, mime, raw)
    sessions = SessionStore(tmp_path / 'sessions.db')
    channels = MultichannelStore(sessions.database_path, sessions)
    account = channels.register_channel_account('role', 'qq', account_id='meme-qq', enabled=True, allow_group=True)
    thread = channels.ensure_channel_thread('role', 'qq', account.account_id, '100', 'group')
    target = target_from_chat(account_id=account.account_id, chat_id='100', kind=TargetKind.GROUP)
    channels.remember_targets('role', [target])
    host = ChannelHost(channels, attachment_resolver=lambda role, identifier: resolve_attachment(tmp_path / role, identifier))
    host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=transport))
    async def send(authorized, content=''):
        return await host.account_send('role', account.account_id, target.id, content, actor='reply',
            source_kind='chat_meme', source_thread_id=thread.thread_id,
            attachment_ids=(item['id'],), frozen_attachment_bytes={item['id']: raw}, authorize_source=authorized)
    return send, item, raw


@pytest.mark.parametrize('fmt', ['PNG', 'GIF'])
@pytest.mark.parametrize('revoke', ['source', 'material'])
def test_sticker_rechecks_after_forward_login_preparation(tmp_path, revoke, fmt):
    allowed = True
    calls = []
    async def transport(action, params):
        nonlocal allowed
        calls.append(action)
        if action == 'get_login_info':
            if revoke == 'source':
                allowed = False
            else:
                from pathlib import Path
                Path(item['path']).write_bytes(b'replaced')
            await asyncio.sleep(0)
            return {'user_id': 99, 'nickname': 'role'}
        return {'message_id': '101'}
    send, item, raw = setup(tmp_path, transport, fmt)
    from pathlib import Path
    result = asyncio.run(send(lambda: allowed and Path(item['path']).read_bytes() == raw, 'x' * 301))
    assert result.status is DeliveryStatus.FAILED
    assert calls == ['get_login_info']


@pytest.mark.parametrize('fmt', ['PNG', 'GIF'])
def test_sticker_rechecks_after_socket_lock_and_preserves_real_receipt(tmp_path, fmt):
    async def scenario():
        allowed = True
        calls = []
        transport = NapCatWebSocketTransport('ws://unused.invalid')
        class Socket:
            async def send(self, raw):
                nonlocal allowed
                request = json.loads(raw)
                calls.append(request['action'])
                allowed = False  # Once dispatched, the real receipt remains authoritative.
                transport._pending[request['echo']].set_result({'message_id': '101'})
        transport._ws, transport.connected = Socket(), True
        send, _, _ = setup(tmp_path, transport, fmt)
        await transport._send_lock.acquire()
        pending = asyncio.create_task(send(lambda: allowed))
        async def entered():
            while not transport._pending:
                await asyncio.sleep(0)
        await asyncio.wait_for(entered(), 1)
        allowed = False
        transport._send_lock.release()
        result = await asyncio.wait_for(pending, 1)
        assert result.status is DeliveryStatus.FAILED and not calls
        allowed = True
        result = await send(lambda: allowed)
        assert result.status is DeliveryStatus.SENT and calls == ['send_group_msg']
    asyncio.run(scenario())
