import asyncio

import pytest

from backend.app.conversation_history import ConversationHistoryView
from backend.app.multichannel import TelegramChannelService
from test_message_history_tools import setup_history


@pytest.mark.parametrize('change', ['revoke', 'repair', 'account', 'later_binding'])
def test_telegram_freezes_owner_history_before_attachment_wait(tmp_path, change):
    sessions, channels, _, _ = setup_history(tmp_path)
    account = channels.save_account('r1', token='synthetic', enabled=True)
    thread = channels.ensure_thread('r1', account.account_id, '22', 'private')
    def pair(sender='22', chat='22'):
        code = channels.create_pairing_code()
        assert channels.consume_pairing_code(code, role_id='r1', channel='telegram', scope='platform',
            platform_user_id=sender, account_id=account.account_id, chat_id=chat)
    pair()
    sessions.append_message('role:r1', 'user', '原来可见的桌面消息')
    calls, download = [], []
    class Transport:
        async def get_me(self, token):
            return {'id': 42}
        async def get_file(self, token, key):
            await asyncio.sleep(0)
            if change == 'revoke':
                channels.unbind_identity(channels.list_identities()[0]['identityId'])
            elif change == 'repair':
                pair()
            elif change == 'account':
                channels.set_platform_identity(account.account_id, 'replacement')
            else:
                other = channels.ensure_thread('r1', account.account_id, '33', 'private')
                pair('33', '33')
                sessions.append_message(other.session_key, 'user', '后来配对的秘密')
                sessions.append_message('role:r1', 'user', '等待期间的未来消息')
            return {'file_path': 'synthetic.txt'}
        async def download_file(self, token, path):
            download.append(path)
            return b'hello', 'text/plain'
    service = TelegramChannelService(channels, account.account_id, Transport(), on_message=lambda *args: calls.append(args),
        persist_attachment=lambda *args: {'id': 'synthetic', 'name': 'hello.txt'})
    update = {'message': {'message_id': 11, 'chat': {'id': 22, 'type': 'private'}, 'from': {'id': 22},
        'document': {'file_id': 'file', 'file_name': 'hello.txt', 'mime_type': 'text/plain'}}}
    if change != 'later_binding':
        with pytest.raises(PermissionError):
            asyncio.run(service.handle_update(account.account_id, update))
        assert not calls and not download
    else:
        assert asyncio.run(service.handle_update(account.account_id, update))
        metadata = calls[0][2]
        restored = ConversationHistoryView(channels, role_id='r1', thread_id=thread.thread_id, snapshot=metadata['ownerHistory'])
        text = str(restored.read())
        assert '原来可见的桌面消息' in text and '收到附件' in text
        assert '后来配对的秘密' not in text and '等待期间的未来消息' not in text
        assert 'ownerHistory' not in sessions.list_messages(thread.session_key)[0].metadata
