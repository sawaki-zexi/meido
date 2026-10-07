import asyncio
from collections.abc import Mapping

import pytest

from backend.app.agent_runtime import (
    AgentToolResult,
    AssistantDoneEvent,
    AssistantMessage,
    ToolCall,
    ToolCallEndEvent,
    ToolContext,
    ToolDefinition,
    UserMessage,
    run_agent_loop,
)
from backend.app.agent_runtime.capabilities import CapabilityRegistry


class ReadTool:
    definition = ToolDefinition(
        name="memory.read",
        description="Read a memory item",
        input_schema={"type": "object"},
        source="builtin",
        version="1.0.0",
        risk="read_only",
        exposure="direct",
    )

    def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del arguments, on_update
        return AgentToolResult(f"{context.role_id}:{context.session_key}:{context.run_id}")


class WriteTool:
    definition = ToolDefinition(
        name="memory.write",
        description="Write a memory item",
        input_schema={"type": "object"},
        source="role",
        version="1.0.0",
        risk="mutating",
        exposure="direct",
    )

    def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del arguments, context, on_update
        return AgentToolResult("ok")


def test_capability_registry_rejects_duplicate_ids_and_freezes_snapshot():
    registry = CapabilityRegistry([ReadTool()])

    with pytest.raises(ValueError, match="duplicate tool"):
        registry.register(ReadTool())

    first = registry.resolve(
        role_id="role-1",
        session_key="role:role-1",
        run_id="run-1",
    )
    registry.register(WriteTool())

    assert [tool.name for tool in first.snapshot.tools] == ["memory.read"]
    assert [tool.definition.name for tool in first.tools] == ["memory.read"]
    assert first.snapshot.to_dict()["tools"][0]["source"] == "builtin"
    assert first.snapshot.to_dict()["tools"][0]["inputSchema"] == {"type": "object"}

    second = registry.resolve(
        role_id="role-1",
        session_key="role:role-1",
        run_id="run-2",
    )
    assert [tool.name for tool in second.snapshot.tools] == ["memory.read", "memory.write"]


def test_capability_registry_filters_tools_and_records_deterministic_reasons():
    registry = CapabilityRegistry([ReadTool(), WriteTool()])

    resolution = registry.resolve(
        role_id="role-1",
        session_key="role:role-1",
        run_id="run-1",
        enabled_tools={"memory.read"},
        allowed_risks={"read_only"},
    )

    assert [tool.name for tool in resolution.snapshot.tools] == ["memory.read"]
    assert resolution.snapshot.provider_schemas()[0]["function"]["name"] == "memory.read"
    assert resolution.denied_tools == {
        "memory.write": "tool is disabled for this run",
    }


def test_agent_loop_uses_capability_resolution_for_schema_and_denied_calls():
    class Provider:
        def __init__(self):
            self.schemas = []

        def stream_response(self, *, model, system, messages, tools, signal, session_id=None):
            del model, system, messages, signal, session_id
            self.schemas.append(tuple(tools))
            call = ToolCall("call-1", "memory.write", {})

            async def stream():
                yield ToolCallEndEvent(call)
                yield AssistantDoneEvent(AssistantMessage(tool_calls=(call,), stop_reason="tool_use"))

            return stream()

    provider = Provider()
    resolution = CapabilityRegistry([ReadTool(), WriteTool()]).resolve(
        role_id="role-1",
        session_key="role:role-1",
        run_id="run-1",
        enabled_tools={"memory.read"},
        allowed_risks={"read_only"},
    )

    async def collect():
        return [
            event
            async for event in run_agent_loop(
                provider=provider,
                model="fake",
                system="",
                messages=[UserMessage("写入")],
                capabilities=resolution,
                max_turns=1,
            )
        ]

    events = asyncio.run(collect())
    assert [item["function"]["name"] for item in provider.schemas[0]] == ["memory.read"]
    result = next(event.result for event in events if event.type == "tool_execution_end")
    assert result.is_error is True
    assert result.details == {"tool": "memory.write", "reason": "tool is disabled for this run", "status": "denied"}


def test_agent_loop_injects_resolution_scope_into_tool_context():
    class Provider:
        def stream_response(self, *, model, system, messages, tools, signal, session_id=None):
            del model, system, messages, tools, signal, session_id
            call = ToolCall("call-1", "memory.read", {})

            async def stream():
                yield ToolCallEndEvent(call)
                yield AssistantDoneEvent(AssistantMessage(tool_calls=(call,), stop_reason="tool_use"))

            return stream()

    resolution = CapabilityRegistry([ReadTool()]).resolve(
        role_id="bound-role",
        session_key="bound-session",
        run_id="bound-run",
    )

    async def collect():
        return [
            event
            async for event in run_agent_loop(
                provider=Provider(),
                model="fake",
                system="",
                messages=[UserMessage("读取")],
                role_id="caller-role",
                session_key="caller-session",
                run_id="caller-run",
                capabilities=resolution,
                max_turns=1,
            )
        ]

    events = asyncio.run(collect())
    result = next(event.result for event in events if event.type == "tool_execution_end")
    assert result.content == "bound-role:bound-session:bound-run"
