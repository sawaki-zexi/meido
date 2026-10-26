import asyncio
import json

import httpx
import pytest

from backend.app.channels.models import DeliveryStatus, OutboundMessage, TargetKind, target_from_chat
from backend.app.channels.telegram import TelegramAccountAdapter
from backend.app.multichannel import TelegramBotApi
from test_delivery_recovery import Clock


@pytest.mark.parametrize('error,expected,calls', [
    ({'error_code': 429, 'description': 'Too Many Requests', 'parameters': {'retry_after': 3}}, DeliveryStatus.SENT, 2),
    ({'error_code': 400, 'description': 'Bad Request: message to be replied not found'}, DeliveryStatus.SENT, 2),
    ({'error_code': 400, 'description': 'Bad Request: message thread not found'}, DeliveryStatus.FAILED, 1),
    ({'error_code': 403, 'description': 'Forbidden: bot was blocked'}, DeliveryStatus.FAILED, 1),
    ({'error_code': 500, 'description': 'Internal error'}, DeliveryStatus.UNCERTAIN, 1),
])
def test_real_telegram_transport_classifies_only_safe_recovery(monkeypatch, error, expected, calls):
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(error['error_code'], json={'ok': False, **error})
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 42}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    clock = Clock()
    adapter = TelegramAccountAdapter('a', lambda: 'synthetic', TelegramBotApi(), scheduler=clock.scheduler())
    target = target_from_chat(account_id='a', chat_id='chat', kind=TargetKind.GROUP, message_thread_id=7)
    adapter.remember_target(target)
    result = asyncio.run(adapter.send(OutboundMessage(target, '**中文😀**', reply_to_message_id='9')))
    assert result.status is expected and len(requests) == calls
    assert all(body['chat_id'] == 'chat' and body['message_thread_id'] == 7 for body in requests)
    if error['error_code'] == 429:
        assert clock.waits == [3] and requests[0] == requests[1]
    if 'replied not found' in error['description']:
        assert 'reply_parameters' not in requests[1]
        assert result.raw['parts'][0]['withoutReply']


def test_telegram_media_caption_entities_reach_real_transport(monkeypatch, tmp_path):
    from backend.app.channels.models import Attachment
    requests = []
    def handle(request):
        requests.append(request.content)
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 1}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    target = target_from_chat(account_id='a', chat_id='1', kind=TargetKind.PRIVATE)
    adapter = TelegramAccountAdapter('a', lambda: 'synthetic', TelegramBotApi())
    adapter.remember_target(target)
    file = tmp_path / 'x.png'
    file.write_bytes(b'fixture')
    result = asyncio.run(adapter.send(OutboundMessage(target, '**caption**', attachments=(Attachment('image', str(file)),))))
    assert result.status is DeliveryStatus.SENT
    assert b'caption_entities' in requests[0] and b'"bold"' in requests[0]
