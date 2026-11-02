import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from backend.app.agent_runtime.meido_provider import MeidoProvider
from backend.app.agent_runtime import ToolDefinition, UserMessage, run_agent_loop
from backend.app.agent_runtime.tools import AgentToolResult
from backend.app.ai.model_adapter import ModelResponseDone, ModelTextDelta, ModelToolCall, OpenAICompatibleAdapter
from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
from backend.app.models import ModelConfiguration, Role, RoleProfile


def role(**updates):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    values = dict(id='role-1', name='爱丽丝', description='{{char}}的简介',
        profile=RoleProfile(profile='{{char}}陪伴{{user}}', personality='温柔',
            behaviorRules='帮助{{ USER }}', responseConstraints='{{CHAR}}简短回答', nickname='小林'),
        createdAt=now, updatedAt=now)
    values.update(updates)
    return Role(**values)


def configuration():
    return ModelConfiguration(providerId='custom', provider='custom',
        baseUrl='https://unused.test', model='fixture', apiKey='synthetic')


def test_compiler_has_fixed_order_and_does_not_read_or_mutate_private_fields():
    from backend.app.role_prompt import compile_role_prompt
    selected = role().model_copy(update={
        'provenance': {'creator_notes': 'SOURCE_SECRET'},
        'modelConfig': {'apiKey': 'CONFIG_SECRET'},
        'memory': 'MEMORY_SECRET', 'workspace': 'WORKSPACE_SECRET'})
    before = selected.model_dump()
    compiled = compile_role_prompt(selected)
    assert compiled.text == '\n\n'.join([
        '你正在扮演 爱丽丝。', '简介：爱丽丝的简介', '角色设定：爱丽丝陪伴小林',
        '性格：温柔', '行为规则：帮助小林', '回复约束：爱丽丝简短回答', '称呼用户：小林'])
    assert compiled.diagnostics == ()
    assert compile_role_prompt(selected) == compiled
    assert selected.model_dump() == before


def test_compiler_omits_whitespace_fields_without_empty_labels():
    from backend.app.role_prompt import compile_role_prompt
    # Persisted/API roles require a setting. The pure compiler also handles an
    # empty draft without inventing labels or whitespace.
    selected = role().model_copy(update={'name': ' \n ', 'description': '\t',
        'profile': RoleProfile(profile=' ', personality='\t', behaviorRules='\n')})
    assert compile_role_prompt(selected).text == ''
    selected = role(description=' ', profile=RoleProfile(profile='  第一行\n第二行  '))
    assert compile_role_prompt(selected).text == '你正在扮演 爱丽丝。\n\n角色设定：第一行\n第二行'


def test_macros_are_case_insensitive_and_use_neutral_user_when_nickname_is_empty():
    from backend.app.role_prompt import compile_role_prompt
    selected = role(description='', profile=RoleProfile(profile='{{ CHAR }} / {{UsEr}}', nickname=' \t'))
    assert compile_role_prompt(selected).text == '你正在扮演 爱丽丝。\n\n角色设定：爱丽丝 / 用户'


def test_inserted_names_are_literal_and_never_recursively_expanded():
    from backend.app.role_prompt import compile_role_prompt
    selected = role(name='{{user}}', description='',
        profile=RoleProfile(profile='{{char}} / {{user}}', nickname='{{char}}'))
    compiled = compile_role_prompt(selected)
    assert compiled.text == '你正在扮演 {{char}}。\n\n角色设定：{{user}} / {{char}}\n\n称呼用户：{{user}}'
    assert compiled.diagnostics == ()


def test_unknown_macros_remain_literal_with_deterministic_field_diagnostics():
    from backend.app.role_prompt import RolePromptDiagnostic, compile_role_prompt
    selected = role(description='{{unknown}}', profile=RoleProfile(
        profile='{{date}} {{date}} {{unknown}} {{random::a::b}}', personality='{{date}}'))
    compiled = compile_role_prompt(selected)
    assert '角色设定：{{date}} {{date}} {{unknown}} {{random::a::b}}' in compiled.text
    assert compiled.diagnostics == (
        RolePromptDiagnostic('description', '{{unknown}}'), RolePromptDiagnostic('profile', '{{date}}'),
        RolePromptDiagnostic('profile', '{{unknown}}'), RolePromptDiagnostic('profile', '{{random::a::b}}'),
        RolePromptDiagnostic('personality', '{{date}}'))
    assert compile_role_prompt(selected) == compiled


def test_import_preview_and_compiler_agree_on_macros_and_ignore_provenance():
    from backend.app.role_card_import import parse_role_card
    from backend.app.role_prompt import compile_role_prompt
    preview = parse_role_card('synthetic.json', json.dumps({'data': {
        'name': '爱丽丝', 'description': '{{ CHAR }}陪伴{{User}} {{date}}',
        'creator_notes': 'PRIVATE_CREATOR_NOTE', 'extensions': {'provenance': 'PRIVATE_SOURCE'}}}).encode())
    assert preview['compatibility']['unsupportedMacros'] == ['{{date}}']
    draft = preview['draft']
    selected = role(name=draft['name'], description=draft['description'], profile=RoleProfile(
        **{field: draft[field] for field in RoleProfile.model_fields}))
    prompt = compile_role_prompt(selected)
    assert '角色设定：爱丽丝陪伴用户 {{date}}' in prompt.text
    assert 'PRIVATE_' not in prompt.text


def test_rich_request_reuses_the_measured_compiled_identity_and_separate_dynamic_frame():
    from backend.app.agent_runtime import CancellationToken
    from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelTextBlock
    from backend.app.role_prompt import compile_role_prompt
    projected, sent = [], []

    class Adapter(OpenAICompatibleAdapter):
        def prepare_input(self, messages, config, tools=(), **kwargs):
            projected.append(messages)
            return super().prepare_input(messages, config, tools, **kwargs)

        async def stream_rich_messages(self, messages, config, tools, **kwargs):
            sent.append(messages)
            yield ModelStreamDone(ModelAssistantMessage((ModelTextBlock('完成'),)))

    selected = role()
    provider = MeidoProvider(Adapter(), selected, configuration(),
        stable_memory='[稳定记忆]\n原文 {{user}}', memory_context='[动态记忆]\n原文 {{char}}')
    history = [UserMessage('当前输入 {{user}}')]
    request = provider.prepare_request('', history, [])
    assert provider.estimate_prepared(request).tokens > 0

    async def collect():
        return [event async for event in provider.stream_prepared(request,
            system='', messages=history, signal=CancellationToken())]

    events = asyncio.run(collect())
    assert sent == [request.messages]
    assert projected and all(messages == request.messages for messages in projected)
    assert sent[0][0].content[0].text.startswith(compile_role_prompt(selected).text)
    assert '[稳定记忆]\n原文 {{user}}' in sent[0][0].content[0].text
    assert '[动态记忆]\n原文 {{char}}' in sent[0][-2].content[0].text
    assert sent[0][-1].content[0].text == '当前输入 {{user}}'
    assert any(getattr(event, 'message', None) and event.message.content == '完成' for event in events)


@pytest.mark.parametrize('with_tool', [False, True])
def test_role_macros_reach_ordinary_and_tool_model_requests(with_tool):
    calls = []

    class Adapter:
        async def stream_structured_messages(self, messages, config, tools):
            calls.append(messages)
            if with_tool and len(calls) == 1:
                yield ModelToolCall('call-1', 'read', {})
                yield ModelResponseDone('tool_use')
            else:
                yield ModelTextDelta('完成')
                yield ModelResponseDone('stop')

    class ReadTool:
        definition = ToolDefinition('read', 'Read synthetic data', {'type': 'object', 'properties': {}})

        def execute(self, arguments, context, on_update=None):
            return AgentToolResult('工具原文 {{char}}')

    provider = MeidoProvider(Adapter(), role(), configuration(),
        structured_without_tools=True, stable_memory='[长期记忆]\n原文 {{user}}',
        memory_context='[动态记忆]\n原文 {{char}}', capability_prompt='[能力]\n原文 {{user}}',
        required_context='[来源]\n访客身份 {{char}}')

    async def collect():
        return [event async for event in run_agent_loop(provider=provider, model='fixture', system='',
            messages=[UserMessage('你好 {{user}}')], tools=[ReadTool()] if with_tool else [],
            role_id='role-1', session_key='role:role-1', run_id='run-1')]

    asyncio.run(collect())
    assert len(calls) == (2 if with_tool else 1)
    for messages in calls:
        prompt = messages[0]['content']
        assert '角色设定：爱丽丝陪伴小林' in prompt
        assert '行为规则：帮助小林' in prompt
        assert '回复约束：爱丽丝简短回答' in prompt
        assert '[长期记忆]\n原文 {{user}}' in prompt
        assert '[能力]\n原文 {{user}}' in prompt
        assert '[来源]\n访客身份 {{char}}' in prompt
        assert '[动态记忆]' not in prompt
        assert any('[动态记忆]\n原文 {{char}}' in str(message['content']) for message in messages[1:])
        assert any(message['content'] == '你好 {{user}}' for message in messages)
    if with_tool:
        assert calls[0][0] == calls[1][0]
        assert any(message.get('role') == 'tool' and message['content'] == '工具原文 {{char}}'
            for message in calls[1])


@pytest.mark.parametrize('adapter_type', [OpenAICompatibleAdapter, RoutedModelAdapter])
def test_compatibility_adapters_compile_role_before_appending_memory(adapter_type, monkeypatch):
    adapter = RoutedModelAdapter(ModelAdapterRegistry()) if adapter_type is RoutedModelAdapter else adapter_type()
    calls = []

    async def stream(messages, config):
        calls.append(messages)
        yield '完成'

    monkeypatch.setattr(adapter, 'stream_messages', stream)

    async def collect():
        return [text async for text in adapter.stream_reply(role(), [], configuration(),
            memory_context='[记忆]\n保留 {{char}} 和 {{user}}')]

    assert asyncio.run(collect()) == ['完成']
    prompt = calls[0][0]['content']
    assert '角色设定：爱丽丝陪伴小林' in prompt
    assert '回复约束：爱丽丝简短回答' in prompt
    assert prompt.endswith('[记忆]\n保留 {{char}} 和 {{user}}')


@pytest.mark.parametrize('completion', [True, False])
def test_todo_reminder_uses_same_role_fields_without_expanding_facts_or_memory(monkeypatch, completion):
    from backend.app import main
    calls = []
    selected = role()

    class Memory:
        async def query(self, query):
            assert query.effect == 'read_only'
            return SimpleNamespace(text_block='记忆 {{char}}')

    async def complete(messages, config, **kwargs):
        calls.append(messages)
        return '提醒'

    async def stream(messages, config, **kwargs):
        calls.append(messages)
        yield '提醒'

    adapter = SimpleNamespace(complete_messages=complete) if completion else SimpleNamespace(stream_messages=stream)
    monkeypatch.setattr(main, 'store', SimpleNamespace(get=lambda _: selected))
    monkeypatch.setattr(main, '_role_model_configuration', lambda _: configuration())
    monkeypatch.setattr(main, 'model_adapter', adapter)
    monkeypatch.setattr(main, '_current_memory_engine', lambda: Memory())
    assert main._generate_todo_reminder(selected.id, 'event-1', 'exact', [{'title': '事实 {{user}}'}]) == '提醒'
    prompt = calls[0][0]['content']
    assert '你正在扮演 爱丽丝。' in prompt
    assert '角色设定：爱丽丝陪伴小林' in prompt
    assert '回复约束：爱丽丝简短回答' in prompt
    assert '称呼用户：小林' in prompt
    assert '记忆 {{char}}' in prompt and '事实 {{user}}' in prompt
