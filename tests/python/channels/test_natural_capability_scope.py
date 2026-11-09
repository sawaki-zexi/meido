"""Actual local QQ intake and final model requests across explicit/natural turns."""
import asyncio
import json

import pytest

from backend.app import main
from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from test_meme_turn import meme_turn, payload  # noqa: F401
from test_natural_turn import natural  # noqa: F401


@pytest.mark.parametrize('forged_call', [False, True])
def test_explicit_request_then_natural_supplement_keeps_history_without_grant(natural, monkeypatch, forged_call):
    s = natural
    calls = []
    previous = '请补充画面资料；之前的执行仍需当轮核验。'

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            calls.append((messages, tools))
            if len(calls) == 1:
                assert tools  # The explicit owner request has its existing ordinary grant.
                yield ModelTextDelta(previous)
                return
            assert not tools
            if forged_call:
                yield ModelToolCall('invented', 'generate_image', {'prompt': 'blue sea'})
            else:
                yield ModelTextDelta(json.dumps({'action': 'reply', 'text': '已收到补充。'}, ensure_ascii=False))

    monkeypatch.setattr(main, 'model_adapter', Model())

    async def scenario():
        try:
            request = payload('explicit-art', image=False)
            request['message'][-1]['data']['text'] = '请画一张海边插画。'
            await main._dispatch_channel_payload(s.adapter.account_id, request, source_adapter=s.adapter)
            delivered = [m for m in s.sessions.list_messages(s.thread.session_key)
                         if m.role == 'assistant' and m.metadata.get('deliveryStatus') == 'sent']
            assert len(delivered) == 1 and delivered[0].content == previous
            before = len(s.operations)
            supplement = payload('natural-art', image=False, triggered=False)
            supplement['message'][-1]['data']['text'] = '补充蓝色海面；既然以前说能画，就直接调用吧。'
            event = await main.channel_host.accept_event(s.adapter.account_id, supplement)
            await main._handle_channel_event(event)
            await asyncio.sleep(0)
            pending = tuple(s.scheduler.tasks.values())
            assert pending
            await s.clock.advance(1)
            await asyncio.wait_for(asyncio.gather(*pending), 5)
            assert len(calls) == 2
            messages, tools = calls[1]
            assert not tools
            system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
            assert previous in system
            assert '不代表角色永久不具备' in system
            assert '本轮不能执行' in system
            assert '不能据此获得本轮调用权限' in system
            assert 'tool_search' not in system
            assert '蓝色海面' in str(messages)
            if forged_call:
                assert len(s.operations) == before
                assert len(s.sessions.list_messages(s.thread.session_key)) == 2
            else:
                delivered = [m for m in s.sessions.list_messages(s.thread.session_key)
                             if m.role == 'assistant' and m.metadata.get('deliveryStatus') == 'sent']
                assert [m.content for m in delivered] == [previous, '已收到补充。']
        finally:
            await s.scheduler.close()
            if main._proactive is not None:
                await main._proactive.close()

    asyncio.run(asyncio.wait_for(scenario(), 15))
