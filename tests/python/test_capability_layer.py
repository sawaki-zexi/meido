import asyncio
from collections.abc import Mapping
from dataclasses import replace

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


def test_tool_definition_validates_policy_metadata_and_limits():
    with pytest.raises(ValueError, match="unsupported tool risk"):
        ToolDefinition("bad-risk", "Invalid", risk="write")
    with pytest.raises(ValueError, match="unsupported tool exposure"):
        ToolDefinition("bad-exposure", "Invalid", exposure="always")
    with pytest.raises(ValueError, match="unsupported tool approval"):
        ToolDefinition("bad-approval", "Invalid", approval="silent")
    with pytest.raises(ValueError, match="tool timeout must be positive"):
        ToolDefinition("bad-timeout", "Invalid", timeout_seconds=0)
    with pytest.raises(ValueError, match="tool timeout must be positive"):
        ToolDefinition("bad-nan-timeout", "Invalid", timeout_seconds=float("nan"))
    with pytest.raises(ValueError, match="tool timeout must be positive"):
        ToolDefinition("bad-bool-timeout", "Invalid", timeout_seconds=True)
    with pytest.raises(ValueError, match="tool timeout must be positive"):
        ToolDefinition("bad-huge-timeout", "Invalid", timeout_seconds=10**1000)
    with pytest.raises(ValueError, match="tool output limit must be positive"):
        ToolDefinition("bad-output", "Invalid", output_limit=0)
    with pytest.raises(ValueError, match="tool output limit must be positive integer"):
        ToolDefinition("bad-bool-output", "Invalid", output_limit=True)


def test_provider_schema_does_not_expose_mutable_tool_definition():
    definition = ToolDefinition(
        "nested",
        "Nested schema",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
    )

    schema = definition.as_provider_schema()
    parameters = schema["function"]["parameters"]
    assert isinstance(parameters, dict)
    parameters["properties"] = {}

    assert definition.input_schema["properties"] == {"value": {"type": "string"}}


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


class DeniedTool:
    definition = ToolDefinition(
        name="memory.denied",
        description="Never available",
        input_schema={"type": "object"},
        approval="deny",
    )

    def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del arguments, context, on_update
        return AgentToolResult("should not execute")


class DeferredTool:
    definition = ToolDefinition(
        name="memory.deferred",
        description="Deferred memory search",
        input_schema={"type": "object"},
        exposure="deferred",
    )

    def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del arguments, context, on_update
        return AgentToolResult("ok")


class ApprovalTool:
    definition = ToolDefinition(
        name="memory.write-approval",
        description="Write memory after approval",
        input_schema={"type": "object"},
        risk="mutating",
        approval="writes",
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
    first_generation = first.snapshot.generation
    assert len(first_generation) == 24

    second = registry.resolve(
        role_id="role-1",
        session_key="role:role-1",
        run_id="run-2",
    )
    assert [tool.name for tool in second.snapshot.tools] == ["memory.read", "memory.write"]
    assert second.snapshot.generation != first_generation


def test_capability_generation_changes_when_tool_contract_changes():
    first = CapabilityRegistry([ReadTool()]).resolve(
        role_id="role-1",
        session_key="role:role-1",
        run_id="run-1",
    )

    class ChangedReadTool:
        definition = replace(
            ReadTool.definition,
            description="Read a memory item with a bounded query",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "maxLength": 100}},
                "required": ["query"],
            },
            timeout_seconds=2.0,
            output_limit=1000,
        )

        def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
            del arguments, context, on_update
            return AgentToolResult("ok")

    changed = CapabilityRegistry([ChangedReadTool()]).resolve(
        role_id="role-1",
        session_key="role:role-1",
        run_id="run-2",
    )

    assert changed.snapshot.generation != first.snapshot.generation


def test_capability_generation_is_independent_of_registration_order():
    first = CapabilityRegistry([ReadTool(), WriteTool()]).resolve(
        role_id="role-1",
        session_key="role:role-1",
        run_id="run-1",
    )
    reversed_order = CapabilityRegistry([WriteTool(), ReadTool()]).resolve(
        role_id="role-1",
        session_key="role:role-1",
        run_id="run-2",
    )

    assert first.snapshot.generation == reversed_order.snapshot.generation


def test_capability_resolution_keeps_execution_policy_after_tool_mutation():
    class MutableTool:
        definition = ToolDefinition(
            "mutable",
            "Uses a frozen execution policy",
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
            output_limit=4,
        )

        def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
            del context, on_update
            return AgentToolResult(f"value:{arguments['value']}")

    tool = MutableTool()
    resolution = CapabilityRegistry([tool]).resolve(
        role_id="role-1",
        session_key="role:role-1",
        run_id="run-frozen-policy",
        enabled_tools={"mutable"},
    )
    tool.definition = replace(
        tool.definition,
        input_schema={
            "type": "object",
            "properties": {"other": {"type": "string"}},
            "required": ["other"],
        },
        output_limit=100,
    )
    resolution.snapshot.tools[0].input_schema["required"] = ["other"]

    class Provider:
        def __init__(self) -> None:
            self.turn = 0

        def stream_response(self, *, model, system, messages, tools, signal, session_id=None):
            del model, system, messages, tools, signal, session_id
            self.turn += 1
            call = ToolCall("call-mutable", "mutable", {"value": "ok"})

            async def stream():
                if self.turn == 1:
                    yield ToolCallEndEvent(call)
                    yield AssistantDoneEvent(AssistantMessage(tool_calls=(call,), stop_reason="tool_use"))
                else:
                    yield AssistantDoneEvent(AssistantMessage(content="完成"))

            return stream()

    async def collect():
        return [
            event
            async for event in run_agent_loop(
                provider=Provider(),
                model="fake",
                system="",
                messages=[UserMessage("执行")],
                capabilities=resolution,
                max_turns=2,
            )
        ]

    events = asyncio.run(collect())
    result = next(event.result for event in events if event.type == "tool_execution_end")
    assert result.content == "valu"
    assert result.details["outputLimit"] == 4


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


def test_capability_registry_denies_approval_policy_and_unknown_enabled_tools():
    resolution = CapabilityRegistry([ReadTool(), DeniedTool()]).resolve(
        role_id="role-1",
        session_key="role:role-1",
        run_id="run-1",
        enabled_tools={"memory.read", "memory.denied", "memory.missing"},
    )

    assert [tool.name for tool in resolution.snapshot.tools] == ["memory.read"]
    assert resolution.snapshot.policy_decisions["memory.denied"] == "denied:approval"
    assert resolution.snapshot.policy_decisions["memory.missing"] == "denied:unknown"
    assert resolution.denial_reason("memory.denied") == "tool approval policy denies this run"
    assert resolution.denial_reason("memory.missing") == "tool is not registered"


def test_capability_registry_requires_explicit_activation_for_deferred_tools():
    registry = CapabilityRegistry([DeferredTool()])

    deferred = registry.resolve(
        role_id="role-1",
        session_key="role:role-1",
        run_id="run-deferred",
        enabled_tools={"memory.deferred"},
    )
    assert deferred.snapshot.tools == ()
    assert deferred.snapshot.policy_decisions["memory.deferred"] == "denied:deferred"
    assert deferred.denial_reason("memory.deferred") == "tool is deferred and not activated"

    activated = registry.resolve(
        role_id="role-1",
        session_key="role:role-1",
        run_id="run-activated",
        enabled_tools={"memory.deferred"},
        activated_tools={"memory.deferred"},
    )
    assert [definition.name for definition in activated.snapshot.tools] == ["memory.deferred"]


def test_capability_registry_requires_approval_for_prompted_tools():
    registry = CapabilityRegistry([ApprovalTool()])

    pending = registry.resolve(
        role_id="role-1",
        session_key="role:role-1",
        run_id="run-pending-approval",
        enabled_tools={"memory.write-approval"},
    )
    assert pending.snapshot.tools == ()
    assert pending.snapshot.policy_decisions["memory.write-approval"] == "denied:approval_required"
    assert pending.denial_reason("memory.write-approval") == "tool approval required: writes"

    approved = registry.resolve(
        role_id="role-1",
        session_key="role:role-1",
        run_id="run-approved",
        enabled_tools={"memory.write-approval"},
        approved_tools={"memory.write-approval"},
    )
    assert [definition.name for definition in approved.snapshot.tools] == ["memory.write-approval"]


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
    assert result.details["tool"] == "memory.write"
    assert result.details["reason"] == "tool is disabled for this run"
    assert result.details["status"] == "denied"
    assert result.details["truncated"] is False


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
