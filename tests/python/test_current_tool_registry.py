"""Observable current-registry and raw-window behavior (Shiori 71c8b0e)."""
import asyncio

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
