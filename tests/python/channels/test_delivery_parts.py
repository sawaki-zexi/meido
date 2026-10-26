import asyncio

import pytest

from backend.app.channels.host import ChannelHost
from backend.app.channels.models import DeliveryStatus, TargetKind, target_from_chat
from backend.app.channels.telegram import TelegramAccountAdapter
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore
from test_host import MemoryCredentials


@pytest.mark.parametrize('failure', ['timeout', 'missing_id'])
def test_each_confirmed_part_is_durable_before_next_send_and_unknown_stops_tail(tmp_path, failure):
    sessions = SessionStore(tmp_path / 'sessions.db')
    store = MultichannelStore(sessions.database_path, sessions, MemoryCredentials())
    account = store.save_account('role', token='synthetic', enabled=True)
    target = target_from_chat(account_id=account.account_id, chat_id='1', kind=TargetKind.PRIVATE)
    store.remember_targets('role', [target])
    calls = []
    class Transport:
        async def send_message(self, token, chat, text, **options):
            calls.append(text)
            if len(calls) == 2:
                durable = store.list_deliveries('role')[0]
                assert durable['parts'][0]['platformMessageId'] == 'first-receipt'
                assert durable['parts'][0]['status'] == 'sent'
                if failure == 'timeout':
                    raise TimeoutError('synthetic ambiguous send')
                return {}
            return {'message_id': 'first-receipt'}
    adapter = TelegramAccountAdapter(account.account_id, lambda: 'synthetic', Transport())
    host = ChannelHost(store)
    host.register(adapter)
    async def scenario():
        return await host.account_send('role', account.account_id, target.id, 'x' * 10000, actor='owner')
    result = asyncio.run(scenario())
    assert result.status is DeliveryStatus.UNCERTAIN and len(calls) == 2
    saved = store.list_deliveries('role')[0]
    assert [part['status'] for part in saved['parts']] == ['sent', 'uncertain', 'pending']
    assert saved['partialDelivery'] and saved['deliveredContent'] == calls[0]
    thread = store.get_thread('role', saved['threadId'])
    original = sessions.list_messages(thread.session_key)[0]
    assert original.content == 'x' * 10000
    context = sessions.runtime_messages(thread.session_key)
    assert len(context) == 1 and context[0].content == calls[0]
    store.mark_interrupted_deliveries_uncertain()
    assert len(calls) == 2


def test_restart_preserves_confirmed_part_and_marks_only_inflight_unknown(tmp_path):
    sessions = SessionStore(tmp_path / 'sessions.db')
    store = MultichannelStore(sessions.database_path, sessions, MemoryCredentials())
    account = store.save_account('role', token='synthetic', enabled=True)
    thread = store.ensure_thread('role', account.account_id, '1', 'private')
    attempt = store.begin_delivery(thread, 'confirmedunknownunsent')
    store.append_event(thread, direction='outbound', external_message_id=None, role='assistant', content='confirmedunknownunsent', status='pending',
        metadata={'naturalReply': True, 'deliveryAttemptId': attempt, 'deliveryStatus': 'pending'})
    parts = [{'kind': 'text', 'content': content, 'status': status, 'platformMessageId': identifier}
        for content, status, identifier in [('confirmed', 'sent', 'actual'), ('unknown', 'sending', None), ('unsent', 'pending', None)]]
    store.record_delivery_parts(attempt, {'parts': parts, 'partialDelivery': True, 'deliveredContent': 'confirmed', 'platform_message_ids': ['actual']})
    store.mark_interrupted_deliveries_uncertain()
    saved = store.list_deliveries('role')[0]
    assert saved['status'] == 'uncertain'
    assert [part['status'] for part in saved['parts']] == ['sent', 'uncertain', 'pending']
    assert saved['platformMessageId'] == 'actual'
    assert sessions.runtime_messages(thread.session_key)[0].content == 'confirmed'
