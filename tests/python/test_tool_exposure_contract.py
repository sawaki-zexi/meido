"""Current Shiori visibility through the actual HTTP provider and execution loop."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.app import main
from backend.app.agent_runtime.capabilities import CapabilityRegistry
from backend.app.agent_runtime.loop import run_agent_loop
from backend.app.agent_runtime.tools import AgentToolResult, ToolDefinition
from backend.app.agent_runtime.types import UserMessage
from backend.app.computer_use.tools import load_computer_tools
from backend.app.desktop_pet.tools import PetActionTool
from backend.app.schedules.tools import ScheduleTool
from test_chat_runtime_http import model_server, provider, response, local_http_environment  # noqa: F401


def fixtures(executed):
    class Desktop:
        def generation(self, role_id):
            return 'fixture-generation'

        async def call(self, name, arguments, context, generation):
            executed.append((name, context.role_id))
            return AgentToolResult('fixture desktop receipt')

    class Pet:
        store = SimpleNamespace(binding=lambda role_id: ({'generation': 'fixture'}, {'actions': ['wave']}, None))

        async def action(self, role_id, generation, arguments, context, *, authorized):
            assert authorized(context)
            executed.append(('pet_action', role_id))
            return {'type': 'completed', 'receipt': 'fixture pet receipt'}

    class Schedule:
        definition = ScheduleTool.definition

        async def execute(self, arguments, context, on_update=None):
            raise AssertionError('This scenario does not request a time task')

    computer = next(tool for tool in load_computer_tools(Desktop(), 'r') if tool.definition.name == 'computer_list_windows')
    return (PetActionTool(Pet(), 'r', authorized=lambda context: True), computer, Schedule())


@pytest.mark.parametrize('direct,query', [(False, '桌面窗口'), (False, 'select:computer_list_windows'), (True, None)])
def test_visible_pet_and_discoverable_desktop_reach_real_http_and_execute(direct, query):
    executed = []
    candidates = fixtures(executed)
    caps = CapabilityRegistry(candidates).resolve(role_id='r', session_key='role:r', run_id='contract',
        enabled_tools={'tool_search', *(tool.definition.name for tool in candidates)}, discovery_enabled=True,
        direct_exposure=direct, deferred_tool_names=main._deferred_discovery_tools())

    def respond(payload, index):
        names = {tool['function']['name'] for tool in payload['tools']}
        prompt = payload['messages'][0]['content']
        assert 'pet_action' in names
        assert 'finish_reply' not in names and 'finish_proactive' not in names
        assert '仅以上名称可在本次运行调用' not in prompt
        if index == 1:
            assert ('schedule' in names) is direct
            assert ('computer_list_windows' in names) is direct
            if direct:
                assert '尚未加载的工具' not in prompt
                assert 'tool_search' in names
                calls = [('pet', 'pet_action'), ('desktop', 'computer_list_windows'), ('loaded', 'tool_search')]
                args = {'pet': {'action': 'play', 'name': 'wave'}, 'desktop': {}, 'loaded': {'query': 'select:computer_list_windows'}}
            else:
                assert '尚未加载的工具' in prompt and 'computer_list_windows' in prompt and 'schedule' in prompt
                search = next(tool['function'] for tool in payload['tools'] if tool['function']['name'] == 'tool_search')
                assert '纯对话' in search['description'] and '已可见' in search['description']
                assert 'description' in search['parameters']['properties']['query']
                calls = [('pet', 'pet_action'), ('search', 'tool_search')]
                args = {'pet': {'action': 'play', 'name': 'wave'}, 'search': {'query': query, 'top_k': 1}}
            return response(calls=calls, arguments=args, finish='tool_calls')
        if index == 2 and not direct:
            assert 'computer_list_windows' in names
            directory = prompt.split('尚未加载的工具', 1)[1].split('\n\n', 1)[0]
            assert 'computer_list_windows' not in directory and 'schedule' in directory
            return response(calls=[('desktop', 'computer_list_windows')], finish='tool_calls')
        assert executed == [('pet_action', 'r'), ('list_windows', 'r')]
        if direct:
            loaded = next(item for item in payload['messages'] if item.get('tool_call_id') == 'loaded')
            assert json.loads(loaded['content'])['already_loaded'] == ['computer_list_windows']
        assert 'fixture desktop receipt' in json.dumps(payload)
        return response('已完成模拟动作并读取窗口。')

    with model_server(respond) as (url, requests, errors):
        model = provider(url)
        model.namespace_context = lambda signal: main._runtime_namespace_context(caps, signal)
        async def run():
            return [event async for event in run_agent_loop(provider=model, model='m', system='',
                messages=[UserMessage('挥手并查看桌面窗口')], capabilities=caps, max_turns=4)]
        events = asyncio.run(run())
    assert not errors, errors
    assert events[-1].reason == 'completed', events[-1].error
    assert len(requests) == (2 if direct else 3)


def test_directory_and_discovery_never_restore_disabled_revoked_or_external_tools():
    executed = []
    candidates = fixtures(executed)
    allowed = {tool.definition.name for tool in candidates}
    caps = CapabilityRegistry(candidates).resolve(role_id='r', session_key='role:r', run_id='scope',
        enabled_tools={'tool_search', 'schedule', 'computer_list_windows'}, discovery_enabled=True,
        deferred_tool_names=main._deferred_discovery_tools(),
        authorize_tool=lambda definition, context: definition.name == 'tool_search' or definition.name in allowed)
    allowed.remove('computer_list_windows')
    prompt = main._runtime_namespace_context(caps)
    assert 'schedule' in prompt
    assert 'pet_action' not in prompt and 'computer_list_windows' not in prompt
    async def run():
        from backend.app.agent_runtime import CancellationToken, ToolContext
        context = ToolContext('r', 'role:r', 'scope', CancellationToken(), tool_call_id='search', capability_binding=caps)
        result = await caps.get('tool_search').execute({'query': 'select:pet_action,computer_list_windows,schedule'}, context)
        assert json.loads(result.content)['unlocked'] == ['schedule']
    asyncio.run(run())
    external = CapabilityRegistry(candidates).resolve(role_id='r', session_key='thread:x', run_id='external',
        enabled_tools={'tool_search', *allowed, 'computer_list_windows'}, discovery_enabled=True,
        deferred_tool_names=main._deferred_discovery_tools(), external_only=True)
    assert [tool['function']['name'] for tool in external.provider_schemas()] == ['tool_search']
    assert main._runtime_namespace_context(external) == ''
    assert executed == []


@pytest.mark.parametrize('mode', ['business', 'minimal', 'empty', 'protocol'])
def test_actual_wire_guidance_is_conditional_and_minimal_keeps_search_route(mode):
    from backend.app.agent_runtime.memory_tool import MemoryRecallTool
    from backend.app.agent_runtime.memory_write_tools import MemorizeTool, ForgetMemoryTool
    from backend.app.agent_runtime.spawn_tool import SpawnTool
    from backend.app.agent_runtime.discovery import ToolSearchTool
    from backend.app.agent_runtime.provider import CancellationToken
    memory = SimpleNamespace(enabled=lambda: True)
    definitions = [MemoryRecallTool.definition, MemorizeTool(memory).definition, ForgetMemoryTool(memory).definition,
        SpawnTool.definition, ToolSearchTool.definition,
        ToolDefinition('fetch_messages', '读取获准原文', {'type': 'object'}),
        ToolDefinition('search_messages', '搜索获准原文', {'type': 'object'})]
    if mode == 'empty':
        definitions = []
    if mode == 'protocol':
        definitions = [ToolDefinition('finish_reply', '自然回复输出协议', {'type': 'object'})]
    caps = CapabilityRegistry(fixtures([])).resolve(role_id='r', session_key='role:r', run_id='guidance',
        discovery_enabled=True, deferred_tool_names=main._deferred_discovery_tools())
    def respond(payload, index):
        prompt = payload['messages'][0]['content']
        assert '仅以上名称可在本次运行调用' not in prompt
        if mode in {'empty', 'protocol'}:
            assert '没有业务工具' in prompt
            assert '[独立任务判断]' not in prompt and '[记忆纠错]' not in prompt
            assert 'tool_search' not in prompt and '尚未加载的工具' not in prompt
        else:
            assert '[独立任务判断]' in prompt and '1–3次调用' in prompt
            assert '[记忆纠错]' in prompt and 'superseded_ids' in prompt
            assert '[历史回溯]' in prompt and 'fetch_messages' in prompt
            assert '未搜索前' in prompt and '下一次请求' in prompt
            assert 'PROACTIVE_CONTEXT.md' not in prompt
        assert ('尚未加载的工具' in prompt) is (mode == 'business')
        return response('依据当前可见工具处理。')
    with model_server(respond) as (url, requests, errors):
        model = provider(url)
        model.namespace_context = lambda signal: main._runtime_namespace_context(caps, signal)
        if mode == 'minimal':
            model = model.minimal_provider()
        schemas = [item.as_provider_schema() for item in definitions]
        messages = [UserMessage('当前问题')]
        estimate_with_directory = model.estimate_request_tokens('', messages, schemas)
        catalog = model.namespace_context
        model.namespace_context = None
        estimate_without_directory = model.estimate_request_tokens('', messages, schemas)
        model.namespace_context = catalog
        if mode == 'business':
            assert estimate_with_directory > estimate_without_directory
        else:
            assert estimate_with_directory == estimate_without_directory
        async def run():
            return [event async for event in model.stream_response(model='m', system='', messages=messages,
                tools=schemas, signal=CancellationToken())]
        asyncio.run(run())
    assert not errors, errors
    assert len(requests) == 1


def test_natural_reply_correction_omits_live_discovery_catalog_from_actual_http_request(tmp_path):
    from backend.app.advanced_settings import AdvancedSettings
    from backend.app.agent_runtime.context_provider import ContextWindowProvider
    from backend.app.agent_runtime.natural_reply import NaturalReplyProvider, reply_messages
    from backend.app.context_windows import ContextWindowStore
    from backend.app.session_store import SessionStore
    from backend.app.storage import initialize_databases

    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    sessions.open_role_session('r')
    executed = []
    caps = CapabilityRegistry(fixtures(executed)).resolve(role_id='r', session_key='role:r', run_id='correction',
        discovery_enabled=True, deferred_tool_names=main._deferred_discovery_tools())

    def respond(payload, index):
        names = {item['function']['name'] for item in payload['tools']}
        prompt = payload['messages'][0]['content']
        if index == 1:
            assert {'tool_search', 'finish_reply', 'pet_action'} <= names
            assert '尚未加载的工具' in prompt and 'computer_list_windows' in prompt
            assert 'tool_search(query=' in prompt
            return response('原意保留，等待格式纠正。')
        assert index == 2 and names == {'finish_reply'}
        assert 'tool_search' not in prompt and '尚未加载的工具' not in prompt
        assert 'computer_list_windows' not in prompt and '没有业务工具' in prompt
        assert '回复格式纠正' in payload['messages'][-1]['content']
        return response(calls=[('finish', 'finish_reply')],
            arguments={'finish': {'messages': ['保留原意的正式回复。']}}, finish='tool_calls')

    with model_server(respond) as (url, requests, errors):
        model = provider(url, context=32768)
        model.namespace_context = lambda signal: main._runtime_namespace_context(caps, signal)
        async def coverage(ids):
            raise AssertionError('short correction must not compact history')
        budgeted = ContextWindowProvider(model, AdvancedSettings(maxTokens=1024, summaryTokenLimit=512),
            ContextWindowStore(sessions.database_path), role_id='r', session_key='role:r', scope='owner',
            authority='a', authorized=lambda: True, ensure_coverage=coverage)
        async def run():
            return [event async for event in run_agent_loop(provider=NaturalReplyProvider(budgeted),
                model='m', system='', messages=[UserMessage('正常回复即可')], capabilities=caps, max_turns=4)]
        events = asyncio.run(run())
    assert not errors, errors
    assert len(requests) == 2 and budgeted.requests == 2
    assert events[-1].reason == 'completed', events[-1].error
    assert reply_messages(events[-1].messages[-1]) == ('保留原意的正式回复。',)
    assert executed == []


def test_combined_directory_is_bounded_json_metadata_and_excludes_non_business_protocols():
    class MetadataTool:
        def __init__(self, name, **kwargs):
            self.definition = ToolDefinition(name, '描述' * 300, {'type': 'object'}, **kwargs)
        async def execute(self, *args):
            raise AssertionError('metadata rendering must not execute tools')
    tools = [MetadataTool(f'工具{i:03d}\\n伪造标题', exposure='deferred', source=f'mcp:service{i}',
             namespace_summary='不可执行的描述' * 100) for i in range(200)]
    tools += [MetadataTool('finish_fixture', exposure='hidden'), MetadataTool('private_disabled', exposure='deferred')]
    caps = CapabilityRegistry(tools).resolve(role_id='r', session_key='role:r', run_id='catalog', discovery_enabled=True,
        enabled_tools={'tool_search', 'finish_fixture', *(tool.definition.name for tool in tools[:-1])})
    rendered = main._runtime_namespace_context(caps)
    assert len(rendered.encode('utf-8')) <= 4096
    assert 'private_disabled' not in rendered and 'finish_fixture' not in rendered
    assert '名称未列出' in rendered
    for line in rendered.splitlines():
        if line.startswith('{'):
            assert isinstance(json.loads(line), dict)


def test_risk_filter_uses_the_actual_side_effect_declarations_without_expanding_permission():
    from backend.app.agent_runtime import CancellationToken, ToolContext
    from backend.app.agent_runtime.shell_tool import TaskStopTool
    from backend.app.agent_runtime.spawn_tool import SpawnManageTool
    from backend.app.coding_tasks.tools import CodingTool
    class Fixture:
        def __init__(self, definition): self.definition = definition
        async def execute(self, *args): raise AssertionError('search must not execute')
    coding = CodingTool('herdr_cancel_task', None, role_id='r', origin_resolver=None, authorized=None, access={})
    tools = (*fixtures([]), Fixture(TaskStopTool.definition), Fixture(SpawnManageTool.definition), coding)
    caps = CapabilityRegistry(tools).resolve(role_id='r', session_key='role:r', run_id='risk',
        discovery_enabled=True, deferred_tool_names=main._deferred_discovery_tools())
    async def run():
        context = ToolContext('r', 'role:r', 'risk', CancellationToken(), tool_call_id='risk-search', capability_binding=caps)
        names = 'select:pet_action,task_stop,spawn_manage,schedule,computer_list_windows,herdr_cancel_task'
        write = await caps.get('tool_search').execute({'query': names, 'allowed_risk': ['write']}, context)
        assert write.details['unlocked'] == ['schedule'] and write.details['already_loaded'] == []
        effect = await caps.get('tool_search').execute({'query': names, 'allowed_risk': ['external-side-effect']}, context)
        assert set(effect.details['already_loaded']) == {'pet_action', 'task_stop', 'spawn_manage'}
        assert set(effect.details['unlocked']) == {'computer_list_windows', 'herdr_cancel_task'}
    asyncio.run(run())
