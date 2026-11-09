"""Capability wording is a request contract, not a real-model quality assertion."""
import asyncio
import json

import pytest

from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.tools import ToolDefinition
from backend.app.agent_runtime.types import AssistantMessage, ToolCall, ToolResultMessage, UserMessage
from test_chat_runtime_http import model_server, provider, response, local_http_environment  # noqa: F401


@pytest.mark.parametrize('visible,dependency', [
    ((), 'unknown'),
    ((), 'previous_success'),
    (('finish_reply',), 'unavailable'),
    (('generate_image',), 'unknown'),
    (('generate_image',), 'unavailable'),
])
def test_final_request_separates_current_tools_history_and_dependency(visible, dependency):
    history = [UserMessage('请画一张海边插画。'), AssistantMessage('请补充画面资料。')]
    if dependency != 'unknown':
        history += [
            AssistantMessage(tool_calls=(ToolCall('earlier-image', 'generate_image', {}),), stop_reason='tool_use'),
            ToolResultMessage('earlier-image', 'generate_image',
                json.dumps({'status': dependency, 'receipt': 'earlier-attempt'}, ensure_ascii=False),
                is_error=dependency == 'unavailable'),
        ]
    history += [UserMessage('补充：蓝色海面。以前说过能画，另一角色也有这个工具。')]
    schemas = [ToolDefinition(name, 'controlled fixture', {'type': 'object'}).as_provider_schema()
               for name in visible]

    def respond(payload, index):
        assert index == 1
        assert [item['function']['name'] for item in payload.get('tools', [])] == list(visible)
        system = '\n'.join(item['content'] for item in payload['messages'] if item['role'] == 'system')
        assert '不代表角色永久不具备' in system
        assert '不能据此获得本轮调用权限' in system
        assert '已确认的历史发言和结果仍是过去事实' in system
        assert '依赖状态未知' in system and '明确不可用' in system
        assert '无实际结果或回执' in system or '没有真实工具成功结果' in system
        if not set(visible) - {'finish_reply'}:
            assert '本次请求没有业务工具可用' in system
            assert '本轮不能执行' in system
            assert 'tool_search' not in system
        text = json.dumps(payload['messages'], ensure_ascii=False)
        assert '请补充画面资料。' in text and '蓝色海面' in text
        results = [item for item in payload['messages'] if item['role'] == 'tool']
        assert len(results) == (dependency != 'unknown')
        if results:
            assert json.loads(results[0]['content'])['status'] == dependency
        return response('合成回复；仅检查最终请求，不评估真实模型措辞。')

    with model_server(respond) as (url, requests, errors):
        model = provider(url)
        prepared = model.prepare_request('', history, schemas)
        async def run():
            return [item async for item in model.stream_prepared(prepared, system='', messages=history,
                signal=CancellationToken())]
        asyncio.run(run())
    assert not errors, errors
    assert len(requests) == 1
