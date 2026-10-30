"""Shiori call intent at provider, hook and business-function boundaries."""
import asyncio
from copy import deepcopy

import pytest

from backend.app.agent_runtime import (
    AgentToolResult, AssistantDoneEvent, AssistantMessage, CapabilityRegistry,
    ToolCall, ToolDefinition, ToolExecutionEndEvent, run_agent_loop,
)
from backend.app.agent_runtime.plugins import (
    HookDefinition, HookOutcome, PluginContribution, PluginManifest, PluginRegistry,
)


INTENT_SCHEMA = {
    'type': 'string',
    'description': '用 5-12 个字说明这次工具调用的意图，只写给用户看的短语。'
                   '不要复述工具名，不要粘贴长参数。例如：查看目录、读取配置、搜索健康数据。',
}


@pytest.mark.parametrize('route', ['plain', 'direct', 'deferred', 'loaded'])
def test_provider_requests_call_intent_and_strict_business_function_receives_only_its_arguments(route):
    seen = []
    original = {
        'type': 'object', 'properties': {'path': {'type': 'string'}},
        'required': ['path'], 'additionalProperties': False,
    }

    class Tool:
        definition = ToolDefinition('read_config', 'Read config', deepcopy(original),
            exposure='deferred' if route in {'deferred', 'loaded'} else 'direct')

        async def execute(self, arguments, context, on_update=None):
            def read(*, path):
                seen.append(path)
                return AgentToolResult('config: ' + path)
            return read(**arguments)

    class Provider:
        requests = 0

        async def stream_response(self, *, tools, messages, **kwargs):
            self.requests += 1
            schemas = {item['function']['name']: item['function']['parameters'] for item in tools}
            for schema in schemas.values():
                assert schema['properties']['description'] == INTENT_SCHEMA
                assert schema['required'].count('description') == 1
            if route == 'deferred' and self.requests == 1:
                assert set(schemas) == {'tool_search'}
                call = ToolCall('search', 'tool_search', {'query': 'select:read_config', 'description': '查找配置读取工具'})
            elif not seen:
                assert schemas['read_config']['required'] == ['path', 'description']
                call = ToolCall('read', 'read_config', {'path': 'config.json', 'description': '读取运行配置'})
            else:
                assert messages[-1].content == 'config: config.json'
                yield AssistantDoneEvent(AssistantMessage('done'))
                return
            yield AssistantDoneEvent(AssistantMessage(tool_calls=(call,)))

    async def exercise():
        tool = Tool()
        capabilities = None if route == 'plain' else CapabilityRegistry([tool]).resolve(
            role_id='r', session_key='role:r', run_id='intent',
            discovery_enabled=route in {'deferred', 'loaded'},
            activated_tools={'read_config'} if route == 'loaded' else set(),
        )
        events = [event async for event in run_agent_loop(provider=Provider(), model='fake', system='',
            messages=[], tools=[tool], capabilities=capabilities, max_turns=3)]
        assert events[-1].reason == 'completed', events[-1]
        assert seen == ['config.json']
        assert tool.definition.input_schema == original
        if capabilities:
            # Snapshot consumers must see the same declaration as actual requests.
            assert capabilities.current().snapshot.provider_schemas() == capabilities.current().provider_schemas()
        calls = [call for message in events[-1].messages if isinstance(message, AssistantMessage) for call in message.tool_calls]
        assert calls[-1].arguments == {'path': 'config.json', 'description': '读取运行配置'}

    asyncio.run(exercise())


@pytest.mark.parametrize('business_description', [False, True])
def test_hooks_keep_original_call_intent_and_business_description_keeps_its_contract(business_description):
    observed, executed = [], []
    original = {'type': 'object', 'properties': {'value': {'type': 'integer'}},
                'required': ['value'], 'additionalProperties': False}
    if business_description:
        original['properties']['description'] = {'type': 'string', 'enum': ['invoice']}
        original['required'].append('description')
    description = 'invoice' if business_description else '读取当前配置'

    class Tool:
        definition = ToolDefinition('plugin.read', 'Read', deepcopy(original), source='plugin:intent')

        async def execute(self, arguments, context, on_update=None):
            executed.append(dict(arguments))
            return AgentToolResult('receipt')

    class Hook:
        definition = HookDefinition('intent.transform', 'transform', tool_names=('plugin.read',))

        def before_tool(self, context):
            observed.append(dict(context.arguments))
            # The execution seam must also strip intent preserved by a hook.
            return HookOutcome(arguments={**context.arguments, 'value': 2})

    class Provider:
        requests = 0

        async def stream_response(self, *, tools, **kwargs):
            self.requests += 1
            if business_description:
                assert tools[0]['function']['parameters'] == original
            yield AssistantDoneEvent(AssistantMessage(tool_calls=(
                ToolCall('one', 'plugin.read', {'value': 1, 'description': description}),
            )) if self.requests == 1 else AssistantMessage('done'))

    async def exercise():
        manifest = PluginManifest('intent', '1.0.0', requested_capabilities=('runtime.tool', 'runtime.hook'),
            declared_tools=('plugin.read',), lifecycle_contributions=('intent.transform',))
        plugins = PluginRegistry([(manifest, lambda _: PluginContribution(tools=(Tool(),), hooks=(Hook(),)))])
        capabilities = await CapabilityRegistry(plugins=plugins).resolve_async(
            role_id='r', session_key='role:r', run_id='intent', granted_capabilities={'runtime.tool', 'runtime.hook'})
        assert capabilities.get('plugin.read') is not None, capabilities.snapshot.plugin_diagnostics
        try:
            events = [event async for event in run_agent_loop(provider=Provider(), model='fake', system='',
                messages=[], capabilities=capabilities)]
            result = next(event.result for event in events if isinstance(event, ToolExecutionEndEvent))
            assert not result.is_error, result
        finally:
            await capabilities.close()
        assert observed == [{'value': 1, 'description': description}]
        assert executed == [{'value': 2, **({'description': description} if business_description else {})}]
        assert Tool.definition.input_schema == original

    asyncio.run(exercise())


@pytest.mark.parametrize('arguments,expected_status', [
    ({'value': 1}, 'succeeded'),
    ({'value': 1, 'description': '短'}, 'succeeded'),
    ({'value': 'wrong', 'description': '读取配置'}, 'invalid_arguments'),
    ({'value': 1, 'extra': True, 'description': '读取配置'}, 'invalid_arguments'),
])
def test_intent_is_a_model_instruction_not_an_extra_host_rejection_rule(arguments, expected_status):
    received = []

    class Tool:
        definition = ToolDefinition('read', 'Read', {'type': 'object', 'properties': {'value': {'type': 'integer'}},
            'required': ['value'], 'additionalProperties': False})

        async def execute(self, arguments, context, on_update=None):
            received.append(dict(arguments))
            return AgentToolResult('receipt')

    class Provider:
        requests = 0

        async def stream_response(self, **kwargs):
            self.requests += 1
            yield AssistantDoneEvent(AssistantMessage(tool_calls=(ToolCall('one', 'read', arguments),))
                if self.requests == 1 else AssistantMessage('done'))

    async def exercise():
        events = [event async for event in run_agent_loop(provider=Provider(), model='fake', system='',
            messages=[], tools=[Tool()])]
        result = next(event.result for event in events if isinstance(event, ToolExecutionEndEvent))
        assert result.details['status'] == expected_status
        assert received == ([{'value': 1}] if expected_status == 'succeeded' else [])
        call = next(message.tool_calls[0] for message in events[-1].messages
                    if isinstance(message, AssistantMessage) and message.tool_calls)
        assert call.arguments == arguments

    asyncio.run(exercise())
