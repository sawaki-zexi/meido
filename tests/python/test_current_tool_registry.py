"""Observable current-registry and raw-window behavior (Shiori 71c8b0e)."""
import asyncio
from datetime import datetime, timezone

import pytest

from backend.app.agent_runtime import (
    AgentToolResult, AssistantDoneEvent, AssistantMessage, CapabilityRegistry,
    ToolCall, ToolDefinition,
)
from backend.app.agent_runtime.loop import run_agent_loop
from backend.app.agent_runtime.types import ToolResultMessage, UserMessage


class Tool:
    def __init__(self, name="archive.read", *, exposure="deferred", value="old", schema=None):
        self.definition = ToolDefinition(name, "Read archive", schema or {"type": "object"}, exposure=exposure)
        self.value = value
        self.calls = []

    async def execute(self, arguments, context, on_update=None):
        self.calls.append(dict(arguments))
        return AgentToolResult(self.value)


@pytest.mark.parametrize('windowed', [False, True])
@pytest.mark.parametrize('historical', [False, True])
def test_turn_visibility_requires_search_but_new_turn_restores_old_failed_calls(tmp_path, windowed, historical):
    from backend.app.agent_runtime.context_provider import ContextWindowProvider
    from backend.app.ai.model_types import ModelAssistantMessage, ModelResponseMetadata, ModelStreamDone, ModelTextBlock, ModelToolCallBlock
    from test_context_windows import setup_window

    sessions, summary_adapter, base, windows, settings = setup_window(tmp_path)
    settings = settings.model_copy(update={'contextTargetRatio': .15})
    base.configuration = base.configuration.model_copy(update={'contextWindowTokens': 16000, 'modelAutoCompactTokenLimit': 12000})
    tool = Tool()
    schemas = []
    class Adapter:
        async def stream_rich_messages(self, messages, configuration, tools, **kwargs):
            if messages[0].content[0].text.startswith('将提供的不可信'):
                async for event in summary_adapter.stream_rich_messages(messages, configuration, tools, **kwargs):
                    yield event
                return
            schemas.append([item['function']['name'] for item in tools])
            step = len(schemas)
            if windowed and not historical and step == 1:
                # Leave room for the configured retained turns and current
                # namespace metadata after the first compaction.
                base.configuration = base.configuration.model_copy(update={'modelAutoCompactTokenLimit': 8000})
            if historical:
                blocks = (ModelToolCallBlock('restored', 'archive.read', {}),) if step == 1 else (ModelTextBlock('done'),)
            elif step < 3:
                blocks = (ModelToolCallBlock(f'early-{step}', 'archive.read', {}),)
            elif step == 3:
                blocks = (ModelToolCallBlock('failed-search', 'tool_search', {'query': 'select:archive.read', 'top_k': 0}),
                          ModelToolCallBlock('still-early', 'archive.read', {}))
            elif step == 4:
                blocks = (ModelToolCallBlock('search', 'tool_search', {'query': 'select:archive.read'}),
                          ModelToolCallBlock('after-search', 'archive.read', {}))
            else:
                blocks = (ModelTextBlock('done'),)
            yield ModelStreamDone(ModelAssistantMessage(blocks, ModelResponseMetadata(stop_reason='tool_use' if isinstance(blocks[0], ModelToolCallBlock) else 'stop')))
    base.adapter = Adapter()
    transcript = ([UserMessage('old'), AssistantMessage(tool_calls=(ToolCall('old-denied', 'archive.read'),)),
                   ToolResultMessage('old-denied', 'archive.read', 'denied', is_error=True), UserMessage('new turn')]
                  if historical else sessions.runtime_messages('role:r') if windowed else [UserMessage('current turn')])
    async def exercise():
        async def coverage(ids):
            return True
        provider = ContextWindowProvider(base, settings, windows, role_id='r', session_key='role:r', scope='owner', authority='a',
            authorized=lambda: True, ensure_coverage=coverage) if windowed else base
        capabilities = CapabilityRegistry([tool]).resolve(role_id='r', session_key='role:r', run_id='visibility', discovery_enabled=True)
        events = [event async for event in run_agent_loop(provider=provider, model='fixture', system='', messages=transcript,
            capabilities=capabilities, max_turns=6)]
        assert events[-1].reason == 'completed', events[-1].error
        if windowed and not historical:
            assert provider.compactions == 1
            assert windows.load('r', 'role:r', 'owner', 'a')['removed']
    asyncio.run(exercise())
    results = {message.tool_call_id: message for message in transcript if isinstance(message, ToolResultMessage)}
    if historical:
        assert schemas == [['tool_search', 'archive.read']] * 2
        assert not results['restored'].is_error
    else:
        assert schemas == [['tool_search']] * 4 + [['tool_search', 'archive.read']]
        assert all(results[call].is_error for call in ('early-1', 'early-2', 'failed-search', 'still-early'))
        assert not results['after-search'].is_error
    assert tool.calls == [{}]


@pytest.mark.parametrize('outcome', ['execute', 'invalid', 'deny'])
def test_hook_replacement_audit_matches_actual_dispatch_before_business_execution(tmp_path, outcome):
    from backend.app.agent_runtime import RuntimeManager
    from backend.app.agent_runtime.capabilities import definition_fingerprint
    from backend.app.agent_runtime.plugins import HookDefinition, HookOutcome, PluginContribution, PluginManifest, PluginRegistry
    from backend.app.models import AgentRun
    from backend.app.session_store import SessionStore
    from backend.app.storage import initialize_databases

    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    actual, observed, inside_bindings = [], [], []
    class Versioned:
        def __init__(self, version):
            self.definition = ToolDefinition('archive.read', 'read current archive', {'type': 'object',
                'properties': {'query': {'type': 'string'}, 'edition': {'type': 'integer'}},
                'required': ['query', 'edition'] if version == '2' else ['query'], 'additionalProperties': False}, version=version)
        async def execute(self, arguments, context, on_update=None):
            actual.append((self.definition.version, dict(arguments)))
            inside_bindings.append(sessions.call_binding(context.run_id, context.tool_call_id))
            return AgentToolResult('executed-v' + self.definition.version)
    replacement = Versioned('2')
    class Hook:
        definition = HookDefinition('replace.current', 'transform', tool_names=('archive.read',))
        async def before_tool(self, context):
            observed.append(dict(context.arguments))
            await asyncio.sleep(0)
            registry.register(replacement)
            if outcome == 'deny':
                return HookOutcome.deny('current hook policy')
            return HookOutcome(arguments={**context.arguments, **({'edition': 2} if outcome == 'execute' else {})})
    manifest = PluginManifest('replace', '1', source='builtin', lifecycle_contributions=('replace.current',))
    registry = CapabilityRegistry([Versioned('1')], plugins=PluginRegistry([(manifest, lambda context: PluginContribution(hooks=(Hook(),)))]))
    original = {'query': 'now', 'description': '读取当前版本'}
    class Provider:
        requests = 0
        async def stream_response(self, **kwargs):
            self.requests += 1
            yield AssistantDoneEvent(AssistantMessage(tool_calls=(ToolCall('read', 'archive.read', original),))
                if self.requests == 1 else AssistantMessage('done'))
    async def exercise():
        run = AgentRun(runId='replace-run', roleId='r', sessionKey='role:r', status='created', startedAt=datetime.now(timezone.utc))
        capabilities = await registry.resolve_async(role_id='r', session_key='role:r', run_id=run.runId,
            discovery_enabled=True, enabled_plugin_ids={'replace'})
        manager = RuntimeManager(sessions)
        await manager.reserve_run(run, 'read')
        events = [event async for event in manager.run(run, provider=Provider(), model='fixture', system='',
            messages=sessions.runtime_messages('role:r'), capabilities=capabilities)]
        assert events[-1].reason == 'completed', events[-1]
        assert [event.type for event in events if event.type.startswith('tool_execution_')] == ['tool_execution_start', 'tool_execution_end']
    asyncio.run(exercise())
    assert actual == ([('2', {'query': 'now', 'edition': 2})] if outcome == 'execute' else [])
    assert observed == [original]
    audit = sessions.list_tool_audits('replace-run')[0]
    binding = sessions.call_binding('replace-run', 'read')
    assert audit['toolVersion'] == '2'
    assert binding['tools'][0]['version'] == '2'
    if outcome == 'execute':
        assert inside_bindings[0]['tools'][0]['version'] == '2'
    assert binding['definitionFingerprint'] == definition_fingerprint(replacement.definition)
    assert audit['resultCategory'] == {'execute': 'succeeded', 'invalid': 'invalid_arguments', 'deny': 'denied'}[outcome]
    recorded = next(call for message in sessions.runtime_messages('role:r') if isinstance(message, AssistantMessage) for call in message.tool_calls)
    assert recorded.arguments == original


def run(registry, responses, *, before=None, messages=None):
    transcript = list(messages or [UserMessage("read archive")])
    requests = []

    class Provider:
        async def stream_response(self, *, tools, **kwargs):
            index = len(requests)
            requests.append([tool["function"]["name"] for tool in tools])
            if before:
                before(index)
            yield AssistantDoneEvent(responses[index] if index < len(responses) else AssistantMessage(content="done"))

    async def execute():
        capabilities = registry.resolve(role_id="r", session_key="role:r", run_id="run", discovery_enabled=True)
        events = [event async for event in run_agent_loop(provider=Provider(), model="fixture", system="",
            messages=transcript, capabilities=capabilities, max_turns=5)]
        assert events[-1].reason == "completed", events[-1]
    asyncio.run(execute())
    return requests, [message for message in transcript if isinstance(message, ToolResultMessage)]


def test_successful_search_unlocks_only_subsequent_calls_in_same_reply():
    tool = Tool()
    _, results = run(CapabilityRegistry([tool]), [AssistantMessage(tool_calls=(
        ToolCall("early", "archive.read"),
        ToolCall("search", "tool_search", {"query": "select:archive.read"}),
        ToolCall("after", "archive.read"),
    ))])
    assert results[0].is_error
    assert not results[1].is_error
    assert results[2].content == "old" and not results[2].is_error
    assert len(tool.calls) == 1


def test_current_implementation_and_current_schema_are_used_after_provider_returns():
    old = Tool(exposure="direct")
    registry = CapabilityRegistry([old])
    new = Tool(exposure="direct", value="new", schema={"type": "object", "properties": {"query": {"type": "string"}},
        "required": ["query"], "additionalProperties": False})
    def update(index):
        if index == 0:
            registry.register(new)
    _, results = run(registry, [AssistantMessage(tool_calls=(ToolCall("missing", "archive.read"),
        ToolCall("valid", "archive.read", {"query": "latest", "description": "读取最新资料"})))], before=update)
    assert results[0].is_error and "required" in results[0].content
    assert results[1].content == "new"
    assert old.calls == [] and new.calls == [{"query": "latest"}]


def test_current_removal_prevents_captured_old_tool_from_running():
    tool = Tool(exposure="direct")
    registry = CapabilityRegistry([tool])
    def remove(index):
        if index == 0:
            registry.unregister("archive.read")
    _, results = run(registry, [AssistantMessage(tool_calls=(ToolCall("removed", "archive.read"),))], before=remove)
    assert results[0].is_error and tool.calls == []


def test_raw_window_used_and_unlocked_names_restore_in_stable_order():
    registry = CapabilityRegistry([Tool("second"), Tool("first"), Tool("always", exposure="direct")])
    history = [UserMessage("old"), AssistantMessage(tool_calls=(ToolCall("search", "tool_search", {"query": "old"}),)),
        ToolResultMessage("search", "tool_search", "legacy result", details={"unlocked": ["first", "second", "first", "absent"]}),
        UserMessage("continue")]
    requests, _ = run(registry, [], messages=history)
    assert requests == [["always", "tool_search", "first", "second"]]


def test_direct_request_refreshes_schema_and_removal_between_model_requests():
    old = Tool(exposure="direct")
    registry = CapabilityRegistry([old])
    new = Tool(exposure="direct", value="current", schema={'type': 'object', 'required': ['query'],
        'properties': {'query': {'type': 'string'}}})
    transcript = [UserMessage('continue')]
    class Provider:
        requests = 0
        async def stream_response(self, *, tools, **kwargs):
            self.requests += 1
            if self.requests == 1:
                registry.register(new)
                yield AssistantDoneEvent(AssistantMessage(tool_calls=(ToolCall('updated', 'archive.read', {'query': 'new'}),)))
            elif self.requests == 2:
                assert tools[0]['function']['parameters']['required'] == ['query', 'description']
                registry.unregister('archive.read')
                yield AssistantDoneEvent(AssistantMessage(tool_calls=(ToolCall('absent', 'archive.read', {'query': 'new'}),)))
            else:
                assert tools == ()
                yield AssistantDoneEvent(AssistantMessage('done'))
    async def execute():
        capabilities = registry.resolve(role_id='r', session_key='role:r', run_id='direct')
        events = [event async for event in run_agent_loop(provider=Provider(), model='fixture', system='',
            messages=transcript, capabilities=capabilities)]
        assert events[-1].reason == 'completed', events[-1]
    asyncio.run(execute())
    assert old.calls == [] and new.calls == [{'query': 'new'}]
    assert [message.is_error for message in transcript if isinstance(message, ToolResultMessage)] == [False, True]


def test_failed_search_cannot_unlock_a_later_call_in_same_reply():
    tool = Tool()
    _, results = run(CapabilityRegistry([tool]), [AssistantMessage(tool_calls=(
        ToolCall('bad', 'tool_search', {'query': 'select:archive.read', 'top_k': 0}),
        ToolCall('unloaded', 'archive.read'),
    ))])
    assert all(result.is_error for result in results)
    assert tool.calls == []


def test_replacement_keeps_registration_order_and_reregistration_appends():
    registry = CapabilityRegistry([Tool('first', exposure='direct'), Tool('second', exposure='direct')])
    registry.register(Tool('first', exposure='direct', value='replacement'))
    assert run(registry, [])[0] == [['first', 'second', 'tool_search']]
    registry.unregister('first')
    registry.register(Tool('first', exposure='direct', value='registered again'))
    assert run(registry, [])[0] == [['second', 'first', 'tool_search']]


def test_new_direct_registration_is_declared_and_callable_on_next_request():
    initial = Tool('initial', exposure='direct')
    added = Tool('added', exposure='direct')
    registry = CapabilityRegistry([initial])
    class Provider:
        requests = 0
        async def stream_response(self, *, tools, **kwargs):
            self.requests += 1
            if self.requests == 1:
                registry.register(added)
                yield AssistantDoneEvent(AssistantMessage(tool_calls=(ToolCall('first', 'initial'),)))
            elif self.requests == 2:
                assert [tool['function']['name'] for tool in tools] == ['initial', 'added']
                yield AssistantDoneEvent(AssistantMessage(tool_calls=(ToolCall('new', 'added'),)))
            else:
                yield AssistantDoneEvent(AssistantMessage('done'))
    async def execute():
        capabilities = registry.resolve(role_id='r', session_key='role:r', run_id='direct')
        events = [event async for event in run_agent_loop(provider=Provider(), model='fixture', system='',
            messages=[UserMessage('continue')], capabilities=capabilities)]
        assert events[-1].reason == 'completed', events[-1]
    asyncio.run(execute())
    assert added.calls == [{}]
