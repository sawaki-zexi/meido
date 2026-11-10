"""Actual local QQ intake and final model requests across explicit/natural turns."""
import asyncio
import json

import pytest

from backend.app import main
from backend.app.model_adapter import ModelTextDelta, ModelToolCall, ModelResponseDone
from backend.app.novelai_generation.tools import GenerateImageTool
from test_meme_turn import meme_turn, payload  # noqa: F401
from test_natural_turn import natural  # noqa: F401


@pytest.mark.parametrize('forged_phase', [None, 'plan', 'expression'], ids=['reply', 'forged-plan', 'forged-expression'])
@pytest.mark.parametrize('sender', ['11', '22'])
def test_explicit_request_then_supplement_uses_current_scope_and_disabled_service_stays_unavailable(natural, monkeypatch, forged_phase, sender):
    s = natural
    calls, executed = [], []
    previous = '请补充画面资料；之前的执行仍需当轮核验。'

    async def generate(self, arguments, context, on_update=None):
        executed.append(arguments)
        raise AssertionError('未配置的服务不能从历史请求获得授权')

    monkeypatch.setattr(GenerateImageTool, 'execute', generate)

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            calls.append((messages, tools))
            if len(calls) == 1:
                assert tools  # The explicit owner request has its existing ordinary grant.
                yield ModelTextDelta(previous)
                return
            system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
            phase = 'plan' if '[自然参与规划]' in system else 'expression'
            assert bool(tools) is (phase == 'expression')
            assert 'generate_image' not in {item['function']['name'] for item in tools}
            if forged_phase == phase and not any(item['role'] == 'tool' for item in messages):
                yield ModelToolCall('invented', 'generate_image', {'prompt': 'blue sea'})
                yield ModelResponseDone('tool_use')
            elif phase == 'plan':
                yield ModelTextDelta(json.dumps({'action': 'reply', 'response': {
                    'message_id': 'natural-art', 'intent': '承接画面补充，不执行本轮不可用的生图操作'}}, ensure_ascii=False))
            else:
                yield ModelTextDelta(json.dumps({'text': '已收到补充。'}, ensure_ascii=False))

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
            supplement = payload('natural-art', image=False, triggered=False, sender=sender)
            supplement['message'][-1]['data']['text'] = '补充蓝色海面；既然以前说能画，就直接调用吧。'
            event = await main.channel_host.accept_event(s.adapter.account_id, supplement)
            await main._handle_channel_event(event)
            await asyncio.sleep(0)
            pending = tuple(s.scheduler.tasks.values())
            assert pending
            await s.clock.advance(1)
            await asyncio.wait_for(asyncio.gather(*pending), 5)
            assert len(calls) == (2 if forged_phase == 'plan' else 4 if forged_phase == 'expression' else 3)
            for messages, tools in calls[1:]:
                system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
                if '[自然参与规划]' in system:
                    assert not tools and '本轮不能执行' in system
                    assert '不能据此获得本轮调用权限' in system
                else:
                    assert 'tool_search' in {item['function']['name'] for item in tools}
                    assert ('当前发送者身份：已配对主人' in system) is (sender == '11')
                assert '蓝色海面' in str(messages)
                timeline = [json.loads(m['content']) for m in messages
                    if m['role'] == 'user' and m['content'].startswith('{')]
                own = next(item for item in timeline if item.get('sourceKind') == 'role_sent')
                assert own['text'] == previous and own['deliveryStatus'] == 'sent'
                assert own['responseRelation']['targetPlatformMessageId'] == 'explicit-art'
                assert any(item['messageId'] == 'natural-art' for item in timeline)
            assert '[自然参与规划]' in calls[1][0][0]['content']
            if forged_phase != 'plan':
                assert '[自然参与表达]' in calls[2][0][0]['content']
            assert not executed
            if forged_phase == 'plan':
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
