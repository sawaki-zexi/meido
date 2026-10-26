import asyncio
import json

from backend.app.channels.feishu import FeishuAdapter, FeishuConfig
from backend.app.channels.models import DeliveryStatus, OutboundMessage, TargetKind, target_from_chat
from backend.app.channels.telegram import TelegramAccountAdapter


def test_telegram_long_markdown_preserves_text_entities_and_utf16_boundaries():
    calls = []
    class Transport:
        async def send_message(self, token, chat, text, **kwargs):
            calls.append((chat, text, kwargs))
            return {'message_id': len(calls)}
    adapter = TelegramAccountAdapter('tg', lambda: 'fixture', Transport())
    target = target_from_chat(account_id='tg', chat_id='1', kind=TargetKind.GROUP, message_thread_id=7)
    adapter.remember_target(target)
    text = '**中文😀** ' + '长😀' * 2500 + '\n[链接](https://example.com)\n```python\nprint("😀")\n```'
    result = asyncio.run(adapter.send(OutboundMessage(target, text, reply_to_message_id='9')))
    assert result.status is DeliveryStatus.SENT
    assert len(calls) >= 2
    visible = ''.join(call[1] for call in calls)
    assert '中文😀' in visible and '长😀' * 2500 in visible and 'print("😀")' in visible
    assert '**' not in visible and '```' not in visible
    kinds = set()
    for chat, content, options in calls:
        encoded = content.encode('utf-16-le')
        assert len(encoded) // 2 <= 4096 and options['message_thread_id'] == 7 and chat == '1'
        for entity in options.get('entities', []):
            start, end = entity['offset'] * 2, (entity['offset'] + entity['length']) * 2
            assert 0 <= start < end <= len(encoded)
            encoded[start:end].decode('utf-16-le')
            kinds.add(entity['type'])
    assert {'bold', 'text_link', 'pre'} <= kinds


def test_feishu_long_markdown_is_multiple_cards_with_same_target():
    calls = []
    async def transport(operation, params):
        calls.append((operation, params))
        return {'message_id': f'm{len(calls)}'}
    adapter = FeishuAdapter(FeishuConfig('fs', 'app'), transport=transport)
    target = target_from_chat(account_id='fs', chat_id='oc_fixture', kind=TargetKind.PRIVATE)
    adapter.remember_target(target)
    text = '**中文😀**\n' + '长文😀' * 3000
    result = asyncio.run(adapter.send(OutboundMessage(target, text)))
    assert result.status is DeliveryStatus.SENT and len(calls) >= 2
    pieces = []
    for operation, params in calls:
        assert operation == 'im.v1.message.create' and params['receive_id'] == target.chat_id
        assert params['msg_type'] == 'interactive'
        assert len(params['content'].encode('utf-8')) < 30 * 1024
        card = json.loads(params['content'])
        pieces.append(card['body']['elements'][0]['content'])
    assert ''.join(pieces) == text


def test_feishu_code_fence_survives_chunk_boundary_without_changing_source_text():
    cards = []
    async def transport(operation, params):
        cards.append(json.loads(params['content'])['body']['elements'][0]['content'])
        return {'message_id': str(len(cards))}
    adapter = FeishuAdapter(FeishuConfig('fs', 'app'), transport=transport)
    target = target_from_chat(account_id='fs', chat_id='chat', kind=TargetKind.PRIVATE)
    adapter.remember_target(target)
    original = '```python\n' + 'print("中文😀")\n' * 700 + '```\n'
    result = asyncio.run(adapter.send(OutboundMessage(target, original)))
    assert len(cards) > 1 and all(card.count('```') == 2 for card in cards)
    assert ''.join(part['content'] for part in result.raw['parts']) == original
