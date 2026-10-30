"""Namespace hints are a live, scoped projection, never a second authority."""
import asyncio
from types import SimpleNamespace

from backend.app.agent_runtime import CancellationToken, MeidoProvider, ToolDefinition, UserMessage
from backend.app.agent_runtime.discovery import ToolSearchTool
from backend.app.ai.model_types import ModelAssistantMessage, ModelResponseMetadata, ModelStreamDone, ModelTextBlock
from backend.app.models import RoleInput, RoleProfile


def test_namespace_hint_filters_before_render_and_obeys_search_revocation():
    from backend.app.main import _runtime_namespace_context
    search = ToolDefinition('tool_search', 'Search')
    allowed = ToolDefinition('invoice', 'Read', source='mcp:finance', namespace_summary='发票服务')
    denied = ToolDefinition('secret', 'Read', source='mcp:private-service', namespace_summary='不得泄露')
    revoked = set()
    seen = []
    def authorize(definition, context):
        seen.append((context.role_id, context.session_key, context.run_id))
        return 'denied' if definition.name in revoked or definition is denied else None
    current = SimpleNamespace(snapshot=SimpleNamespace(role_id='r1', session_key='thread:t1', run_id='run1',
        candidates=(search, allowed, denied), tools=(search,)),
        definition=lambda name: search if name == 'tool_search' else None,
        authorization_denial=authorize)
    capabilities = SimpleNamespace(current=lambda: current)
    text = _runtime_namespace_context(capabilities, CancellationToken())
    assert '发票服务' in text and 'mcp:finance' in text
    assert 'private-service' not in text and '不得泄露' not in text and 'omitted' not in text
    assert set(seen) == {('r1', 'thread:t1', 'run1')}
    revoked.add('invoice')
    assert _runtime_namespace_context(capabilities) == ''
    revoked.add('tool_search')
    assert _runtime_namespace_context(capabilities) == ''


def test_namespace_projection_uses_current_request_and_is_counted_in_prompt():
    class Adapter:
        def __init__(self):
            self.prompts = []
        async def stream_rich_messages(self, messages, *args, **kwargs):
            self.prompts.append(messages[0].content[0].text)
            yield ModelStreamDone(ModelAssistantMessage((ModelTextBlock('ok'),), ModelResponseMetadata(stop_reason='stop')))
    adapter = Adapter()
    role = RoleInput(name='test', profile=RoleProfile(profile='角色设定必须保留')).model_copy(update={'id': 'r1'})
    current = ['已授权命名空间 ' + '资料' * 200]
    signals = []
    def namespace_context(signal):
        signals.append(signal)
        return current[0]
    provider = MeidoProvider(adapter, role, None, namespace_context=namespace_context)
    plain = MeidoProvider(Adapter(), role, None)
    messages = [UserMessage('你好')]
    tools = [ToolSearchTool.definition.as_provider_schema()]
    assert provider.estimate_request_tokens('', messages, tools) > plain.estimate_request_tokens('', messages, tools)
    signal = CancellationToken()
    async def read():
        return [event async for event in provider.stream_response(model='fake', system='', messages=messages, tools=tools, signal=signal)]
    asyncio.run(read())
    assert '角色设定必须保留' in adapter.prompts[-1] and current[0] in adapter.prompts[-1]
    assert signals[-1] is signal
    current[0] = ''
    asyncio.run(read())
    assert '已授权命名空间' not in adapter.prompts[-1]
    # A newly constructed child/natural provider does not inherit parent hints.
    assert '已授权命名空间' not in plain.role_system_prompt()
