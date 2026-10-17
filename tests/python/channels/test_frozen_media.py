"""Actual channel payloads consume the host's immutable attachment bytes."""
import asyncio
import base64
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from backend.app.channels.base import UnsupportedTargetError
from backend.app.channels.feishu import FeishuAdapter, FeishuConfig
from backend.app.channels.host import ChannelHost
from backend.app.channels.models import Attachment, DeliveryStatus, OutboundMessage, TargetKind, target_from_chat
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.channels.qqbot import QQBotAdapter, QQBotConfig
from backend.app.channels.telegram import TelegramAccountAdapter
from backend.app.chat_attachments import MAX_ATTACHMENT_BYTES, save_attachment
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore
from test_proactive_targets_api import platform  # noqa: F401

pytestmark = pytest.mark.usefixtures('local_platform_only')
ORIGINAL = b'\x89PNG\r\n\x1a\noriginal-image'
CHANGED = b'\x89PNG\r\n\x1a\nreplaced-image'


def sender(channel, received):
    async def transport(operation, params):
        if operation == 'send_private_msg':
            encoded = params['message'][-1]['data']['file'].removeprefix('base64://')
            received.append(base64.b64decode(encoded))
            return {'message_id': 101}
        if operation == 'upload_c2c_file':
            received.append(base64.b64decode(params['body']['file_data']))
            return {'file_info': 'frozen-upload'}
        if operation == 'send_c2c_image':
            return {'id': 'frozen-message'}
        if operation == 'im.v1.media.create':
            received.append(params['data'])
            return {'image_key': 'frozen-image', 'file_key': 'frozen-file'}
        if operation == 'im.v1.message.create':
            return {'message_id': 'frozen-message'}
        raise AssertionError(operation)

    async def telegram_media(_token, _chat_id, data, _name, **_options):
        received.append(data)
        return {'message_id': 101}

    if channel == 'qq':
        return NapCatAdapter(NapCatConfig('frozen-account'), transport=transport)
    if channel == 'qqbot':
        return QQBotAdapter(QQBotConfig('frozen-account', 'fixture'), transport=transport)
    if channel == 'feishu':
        return FeishuAdapter(FeishuConfig('frozen-account', 'fixture'), transport=transport)
    return TelegramAccountAdapter('frozen-account', lambda: 'fixture',
        SimpleNamespace(send_photo=telegram_media, send_document=telegram_media))


@pytest.mark.parametrize('channel', ['qq', 'qqbot', 'feishu', 'telegram'])
@pytest.mark.parametrize('frozen', [False, True], ids=['ordinary-path', 'frozen-bytes'])
def test_platform_payload_uses_frozen_bytes_after_the_original_file_changes(tmp_path, channel, frozen):
    received = []
    adapter = sender(channel, received)
    target = target_from_chat(account_id=adapter.account_id, chat_id='101', kind=TargetKind.PRIVATE)
    adapter.remember_target(target)
    path = tmp_path / 'image.png'
    path.write_bytes(ORIGINAL)
    attachment = Attachment('image', str(path), mime_type='image/png', size=len(ORIGINAL),
        **({'frozen_data': ORIGINAL} if frozen else {}))
    path.write_bytes(CHANGED)

    result = asyncio.run(adapter.send(OutboundMessage(target, '', attachments=(attachment,))))
    assert result.status is DeliveryStatus.SENT, result
    assert received == [ORIGINAL if frozen else CHANGED]


@pytest.mark.parametrize('channel', ['qq', 'qqbot', 'feishu', 'telegram'])
def test_frozen_media_still_obeys_platform_size_limits(tmp_path, channel):
    received = []
    adapter = sender(channel, received)
    target = target_from_chat(account_id=adapter.account_id, chat_id='101', kind=TargetKind.PRIVATE)
    adapter.remember_target(target)
    path = tmp_path / 'small.png'
    path.write_bytes(ORIGINAL)
    attachment = Attachment('image', str(path), mime_type='image/png',
        frozen_data=b'\x89PNG\r\n\x1a\n' + b'x' * MAX_ATTACHMENT_BYTES)

    result = asyncio.run(adapter.send(OutboundMessage(target, '', attachments=(attachment,))))
    assert result.status is DeliveryStatus.FAILED
    assert received == []


@pytest.mark.parametrize('channel', ['qq', 'qqbot'])
def test_frozen_media_does_not_enable_unsupported_files_or_invalid_image_signatures(tmp_path, channel):
    received = []
    adapter = sender(channel, received)
    target = target_from_chat(account_id=adapter.account_id, chat_id='101', kind=TargetKind.PRIVATE)
    adapter.remember_target(target)
    path = tmp_path / 'image.png'
    path.write_bytes(ORIGINAL)
    for kind, data in [('file', ORIGINAL), ('image', b'invalid-image')]:
        attachment = Attachment(kind, str(path), mime_type='image/png', frozen_data=data)
        result = asyncio.run(adapter.send(OutboundMessage(target, '', attachments=(attachment,))))
        assert result.status is DeliveryStatus.FAILED
    assert received == []


class MemoryCredentials:
    def get(self, _key):
        return ''

    def set(self, _key, _value):
        pass


@contextmanager
def owned_media(tmp_path, platform):
    sessions = SessionStore(tmp_path / 'sessions.db')
    channels = MultichannelStore(sessions.database_path, sessions, MemoryCredentials())
    account = channels.register_channel_account('role', 'qq', account_id='frozen-account', enabled=True)
    target = target_from_chat(account_id=account.account_id, chat_id='101', kind=TargetKind.PRIVATE)
    channels.remember_targets('role', [target])
    code = channels.create_pairing_code()
    assert channels.consume_pairing_code(code, role_id='role', channel='qq', scope='platform',
        platform_user_id='101', account_id=account.account_id, chat_id='101')
    item = save_attachment(tmp_path / 'role', 'image.png', 'image/png', ORIGINAL)
    host = ChannelHost(channels, attachment_resolver=lambda role, identifier:
        item if role == 'role' and identifier == item['id'] else None)
    host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=platform[1]))
    try:
        yield host, channels, target, item
    finally:
        asyncio.run(host.stop(account.account_id))


def test_host_resolves_original_role_id_and_sends_frozen_media_through_real_onebot(tmp_path, platform):
    with owned_media(tmp_path, platform) as (host, channels, target, item):
        from pathlib import Path
        Path(item['path']).write_bytes(CHANGED)
        result = asyncio.run(host.account_send('role', target.account_id, target.id, '', actor='push',
            attachment_ids=(item['id'],), frozen_attachment_bytes={item['id']: ORIGINAL},
            authorize_source=lambda: True, record_message=False, source_kind='autonomy',
            source_tool_call_id='original-intent', source_run_id='original-tick'))

        assert result.status is DeliveryStatus.SENT and result.platform_message_id == '8301'
        assert len(platform[0]['sent']) == 1
        image = platform[0]['sent'][0]['message'][-1]['data']['file']
        assert base64.b64decode(image.removeprefix('base64://')) == ORIGINAL
        receipt = channels.delivery_receipt_for_source('role', source_kind='autonomy', source_tool_call_id='original-intent')
        assert receipt['attempt_id'] == result.attempt_id and receipt['platform_message_id'] == '8301'
        assert channels.sessions.list_messages(channels.list_threads('role')[0].session_key) == []


@pytest.mark.parametrize('invalid', ['extra-id', 'duplicate-id', 'mutable', 'empty', 'oversize',
    'wrong-role', 'wrong-size', 'owner-actor', 'no-authorization', 'revoked', 'raw-attachment'])
def test_host_rejects_unfrozen_or_out_of_scope_media_before_platform_dispatch(tmp_path, platform, invalid):
    with owned_media(tmp_path, platform) as (host, channels, target, item):
        role = 'role'
        options = {'actor': 'push', 'attachment_ids': (item['id'],),
            'frozen_attachment_bytes': {item['id']: ORIGINAL}, 'authorize_source': lambda: True}
        if invalid == 'extra-id':
            options['frozen_attachment_bytes']['not-original'] = ORIGINAL
        elif invalid == 'duplicate-id':
            options['attachment_ids'] = (item['id'], item['id'])
        elif invalid == 'mutable':
            options['frozen_attachment_bytes'][item['id']] = bytearray(ORIGINAL)
        elif invalid == 'empty':
            options['frozen_attachment_bytes'][item['id']] = b''
        elif invalid == 'oversize':
            options['frozen_attachment_bytes'][item['id']] = b'x' * (MAX_ATTACHMENT_BYTES + 1)
        elif invalid == 'wrong-role':
            host.attachment_resolver = lambda _role, _identifier: None
        elif invalid == 'wrong-size':
            item['size'] += 1
        elif invalid == 'owner-actor':
            options['actor'] = 'owner'
        elif invalid == 'no-authorization':
            options['authorize_source'] = None
        elif invalid == 'revoked':
            options['authorize_source'] = lambda: False
        elif invalid == 'raw-attachment':
            options['attachments'] = (Attachment('image', item['path']),)

        with pytest.raises(UnsupportedTargetError):
            asyncio.run(host.account_send(role, target.account_id, target.id, '', **options))
        assert platform[0]['calls'] == [] and platform[0]['sent'] == []
        assert channels.list_deliveries('role') == []
        asyncio.run(asyncio.wait_for(host.pause_and_drain(target.account_id), 1))
