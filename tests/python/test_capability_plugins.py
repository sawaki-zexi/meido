import asyncio
from collections.abc import Mapping

import pytest

from backend.app.agent_runtime import (
    AgentToolResult,
    AssistantDoneEvent,
    AssistantMessage,
    CapabilityRegistry,
    CancellationToken,
    HookDefinition,
    HookOutcome,
    PluginContribution,
    PluginManifest,
    PluginRegistry,
    ToolCall,
    ToolCallEndEvent,
    ToolContext,
    ToolDefinition,
    UserMessage,
    run_agent_loop,
)
from backend.app.agent_runtime.plugins import HookContext


class EchoTool:
    definition = ToolDefinition(
        name="plugin.echo",
        description="Echo a message",
        input_schema={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
        source="plugin:demo",
    )

    def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del context, on_update
        return AgentToolResult(str(arguments["message"]))


class UppercaseHook:
    definition = HookDefinition(
        hook_id="demo.uppercase",
        mode="transform",
        tool_names=("plugin.echo",),
    )

    def before_tool(self, context: HookContext) -> HookOutcome:
        return HookOutcome(arguments={"message": str(context.arguments["message"]).upper()})


class DenyHook:
    definition = HookDefinition(
        hook_id="demo.deny",
        mode="deny",
        tool_names=("plugin.echo",),
    )

    def before_tool(self, context: HookContext) -> HookOutcome:
        del context
        return HookOutcome.deny("demo policy")


class ObserveHook:
    definition = HookDefinition(
        hook_id="demo.observe",
        mode="observe",
        tool_names=("plugin.echo",),
    )

    def __init__(self):
        self.seen = []

    def before_tool(self, context: HookContext) -> HookOutcome:
        self.seen.append(dict(context.arguments))
        return HookOutcome.deny("ignored")


class SlowHook:
    definition = HookDefinition(
        hook_id="demo.slow",
        mode="transform",
        tool_names=("plugin.echo",),
        timeout_seconds=0.001,
    )

    async def before_tool(self, context: HookContext) -> HookOutcome:
        del context
        await asyncio.sleep(0.01)
        return HookOutcome()


def _manifest(*capabilities: str) -> PluginManifest:
    return PluginManifest(
        plugin_id="demo",
        version="1.0.0",
        requested_capabilities=capabilities,
        source="builtin",
    )


def test_plugin_manifest_rejects_unknown_capability_and_duplicate_ids():
    with pytest.raises(ValueError, match="unknown capability"):
        PluginManifest("demo", "1.0.0", ("network.open",))

    registry = PluginRegistry()
    registry.register(_manifest(), lambda context: PluginContribution())
    with pytest.raises(ValueError, match="duplicate plugin"):
        registry.register(_manifest(), lambda context: PluginContribution())

    with pytest.raises(ValueError, match="hook timeout must be positive"):
        HookDefinition("bad-timeout", "transform", timeout_seconds=float("nan"))
    with pytest.raises(ValueError, match="hook timeout must be positive"):
        HookDefinition("bad-bool-timeout", "transform", timeout_seconds=True)


def test_plugin_grant_and_host_service_diagnostics_are_distinct():
    called = []

    def factory(context):
        called.append(context.require("memory.read"))
        return PluginContribution()

    registry = PluginRegistry([(_manifest("memory.read"), factory)])
    denied = asyncio.run(registry.load(granted_capabilities=set()))
    assert denied.loaded == ()
    assert denied.diagnostics[0]["error"] == "capability not granted: memory.read"
    assert called == []

    unavailable = asyncio.run(registry.load(granted_capabilities={"memory.read"}))
    assert unavailable.loaded == ()
    assert "host service unavailable: memory.read" in unavailable.diagnostics[0]["error"]
    assert called == []


def test_unknown_capability_grant_is_diagnosed_without_aborting_load():
    result = asyncio.run(PluginRegistry().load(granted_capabilities={"unknown.capability"}))

    assert result.loaded == ()
    assert result.diagnostics == ({
        "status": "denied",
        "error": "unknown capability grant: unknown.capability",
    },)


def test_plugin_registry_reports_unknown_enabled_plugin():
    registry = PluginRegistry()

    result = asyncio.run(registry.load(enabled_ids={"missing"}))

    assert result.loaded == ()
    assert result.diagnostics == ({
        "pluginId": "missing",
        "status": "unknown",
        "error": "plugin is not registered",
    },)


def test_plugin_registry_rejects_duplicate_hook_ids_across_plugins():
    contribution = PluginContribution(hooks=(ObserveHook(),))
    registry = PluginRegistry([
        (PluginManifest("demo-a", "1.0.0"), lambda context: contribution),
        (PluginManifest("demo-b", "1.0.0"), lambda context: contribution),
    ])

    result = asyncio.run(registry.load(enabled_ids={"demo-a", "demo-b"}))

    assert [plugin.descriptor.manifest.plugin_id for plugin in result.loaded] == ["demo-a"]
    assert result.diagnostics == ({
        "pluginId": "demo-b",
        "status": "failed",
        "error": "duplicate plugin hook: demo.observe",
    },)


def test_plugin_setup_rollback_and_cleanup_are_idempotent():
    cleanup = []

    def factory(context):
        context.add_cleanup(lambda: cleanup.append("rolled-back"))
        raise RuntimeError("setup failed")

    registry = PluginRegistry([(_manifest(), factory)])
    result = asyncio.run(registry.load())
    assert result.loaded == ()
    assert result.diagnostics[0]["status"] == "failed"
    assert cleanup == ["rolled-back"]

    async def good_factory(context):
        context.add_cleanup(lambda: cleanup.append("closed"))
        return PluginContribution(tools=(EchoTool(),))

    good = PluginRegistry([(_manifest(), good_factory)])
    resolution = asyncio.run(good.load())
    capabilities = CapabilityRegistry(plugins=good).resolve(
        role_id="role-1",
        session_key="role:role-1",
        run_id="run-1",
        plugin_resolution=resolution,
    )
    assert capabilities.snapshot.to_dict()["plugins"][0]["status"] == "loaded"
    asyncio.run(capabilities.close())
    asyncio.run(capabilities.close())
    assert cleanup == ["rolled-back", "closed"]
    reloaded = asyncio.run(resolution.reload(good))
    asyncio.run(reloaded.close())
    assert cleanup == ["rolled-back", "closed", "closed"]


def test_plugin_resources_are_closed_when_capability_resolution_fails():
    cleanup = []

    class DuplicateTool:
        definition = EchoTool.definition

        def execute(self, arguments, context, on_update=None):
            del arguments, context, on_update
            return AgentToolResult("unreachable")

    def factory(context):
        context.add_cleanup(lambda: cleanup.append("closed"))
        return PluginContribution(tools=(DuplicateTool(),))

    registry = PluginRegistry([(_manifest(), factory)])
    capabilities = CapabilityRegistry([EchoTool()], plugins=registry)

    with pytest.raises(ValueError, match="duplicate tool"):
        asyncio.run(capabilities.resolve_async(
            role_id="role-1",
            session_key="role:role-1",
            run_id="run-duplicate",
            enabled_plugin_ids={"demo"},
            enabled_tools={"plugin.echo"},
        ))

    assert cleanup == ["closed"]


def test_plugin_transform_hook_changes_tool_arguments_before_execution():
    def factory(context):
        del context
        return PluginContribution(tools=(EchoTool(),), hooks=(UppercaseHook(),))

    registry = PluginRegistry([(_manifest(), factory)])
    capabilities = asyncio.run(
        CapabilityRegistry(plugins=registry).resolve_async(
            role_id="role-1",
            session_key="role:role-1",
            run_id="run-1",
            enabled_tools={"plugin.echo"},
        )
    )

    class Provider:
        def __init__(self):
            self.turn = 0

        def stream_response(self, *, model, system, messages, tools, signal, session_id=None):
            del model, system, messages, tools, signal, session_id
            self.turn += 1
            call = ToolCall("call-1", "plugin.echo", {"message": "hello"})

            async def stream():
                if self.turn == 1:
                    yield ToolCallEndEvent(call)
                    yield AssistantDoneEvent(AssistantMessage(tool_calls=(call,), stop_reason="tool_use"))
                else:
                    yield AssistantDoneEvent(AssistantMessage(content="done"))

            return stream()

    async def collect():
        return [
            event
            async for event in run_agent_loop(
                provider=Provider(),
                model="fake",
                system="",
                messages=[UserMessage("echo")],
                capabilities=capabilities,
                max_turns=2,
            )
        ]

    events = asyncio.run(collect())
    result = next(event.result for event in events if event.type == "tool_execution_end")
    assert result.content == "HELLO"
    asyncio.run(capabilities.close())


def test_plugin_deny_hook_returns_structured_tool_error():
    def factory(context):
        del context
        return PluginContribution(tools=(EchoTool(),), hooks=(DenyHook(),))

    capabilities = asyncio.run(
        CapabilityRegistry(plugins=PluginRegistry([(_manifest(), factory)])).resolve_async(
            role_id="role-1",
            session_key="role:role-1",
            run_id="run-1",
            enabled_tools={"plugin.echo"},
        )
    )
    arguments, error = asyncio.run(
        capabilities.prepare_tool_call(
            "plugin.echo",
            {"message": "hello"},
            ToolContext("role-1", "role:role-1", "run-1", signal=CancellationToken()),
        )
    )
    assert arguments == {"message": "hello"}
    assert error is not None
    assert error.is_error is True
    assert error.details == {"hook": "demo.deny", "status": "denied", "reason": "demo policy"}
    asyncio.run(capabilities.close())


def test_plugin_observe_hook_cannot_change_or_deny_tool_call():
    observe = ObserveHook()

    def factory(context):
        del context
        return PluginContribution(tools=(EchoTool(),), hooks=(observe,))

    capabilities = asyncio.run(
        CapabilityRegistry(plugins=PluginRegistry([(_manifest(), factory)])).resolve_async(
            role_id="role-1",
            session_key="role:role-1",
            run_id="run-1",
            enabled_tools={"plugin.echo"},
        )
    )
    arguments, error = asyncio.run(
        capabilities.prepare_tool_call(
            "plugin.echo",
            {"message": "hello"},
            ToolContext("role-1", "role:role-1", "run-1", signal=CancellationToken()),
        )
    )
    assert arguments == {"message": "hello"}
    assert error is None
    assert observe.seen == [{"message": "hello"}]
    asyncio.run(capabilities.close())


def test_plugin_hook_cannot_mutate_arguments_in_place():
    class MutatingHook:
        definition = HookDefinition(
            hook_id="demo.mutating-observer",
            mode="observe",
            tool_names=("plugin.echo",),
        )

        def before_tool(self, context: HookContext) -> HookOutcome:
            context.arguments["metadata"]["flag"] = True  # type: ignore[index]
            return HookOutcome()

    def factory(context):
        del context
        return PluginContribution(tools=(EchoTool(),), hooks=(MutatingHook(),))

    capabilities = asyncio.run(
        CapabilityRegistry(plugins=PluginRegistry([(_manifest(), factory)])).resolve_async(
            role_id="role-1",
            session_key="role:role-1",
            run_id="run-1",
            enabled_tools={"plugin.echo"},
        )
    )
    arguments, error = asyncio.run(
        capabilities.prepare_tool_call(
            "plugin.echo",
            {"message": "hello", "metadata": {"flag": False}},
            ToolContext("role-1", "role:role-1", "run-1", signal=CancellationToken()),
        )
    )
    assert arguments == {"message": "hello", "metadata": {"flag": False}}
    assert error is not None
    assert error.is_error is True
    assert error.details["hook"] == "demo.mutating-observer"
    assert error.details["status"] == "failed"
    asyncio.run(capabilities.close())


def test_transform_hook_can_return_nested_readonly_arguments():
    class NestedTool:
        definition = ToolDefinition(
            name="plugin.nested",
            description="Accept nested arguments",
            input_schema={
                "type": "object",
                "properties": {
                    "metadata": {
                        "type": "object",
                        "properties": {"flag": {"type": "boolean"}},
                        "required": ["flag"],
                        "additionalProperties": False,
                    },
                    "labels": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["metadata", "labels"],
                "additionalProperties": False,
            },
        )

        def execute(self, arguments, context, on_update=None):
            del arguments, context, on_update
            return AgentToolResult("ok")

    class PassthroughHook:
        definition = HookDefinition(
            hook_id="demo.nested-passthrough",
            mode="transform",
            tool_names=("plugin.nested",),
        )

        def before_tool(self, context: HookContext) -> HookOutcome:
            return HookOutcome(arguments=dict(context.arguments))

    def factory(context):
        del context
        return PluginContribution(tools=(NestedTool(),), hooks=(PassthroughHook(),))

    capabilities = asyncio.run(
        CapabilityRegistry(plugins=PluginRegistry([(_manifest(), factory)])).resolve_async(
            role_id="role-1",
            session_key="role:role-1",
            run_id="run-1",
            enabled_tools={"plugin.nested"},
        )
    )
    arguments, error = asyncio.run(
        capabilities.prepare_tool_call(
            "plugin.nested",
            {"metadata": {"flag": True}, "labels": ["one", "two"]},
            ToolContext("role-1", "role:role-1", "run-1", signal=CancellationToken()),
        )
    )
    assert error is None
    assert arguments == {"metadata": {"flag": True}, "labels": ["one", "two"]}
    capabilities.validate_arguments("plugin.nested", arguments)
    asyncio.run(capabilities.close())


def test_plugin_hook_timeout_returns_structured_error():
    def factory(context):
        del context
        return PluginContribution(tools=(EchoTool(),), hooks=(SlowHook(),))

    capabilities = asyncio.run(
        CapabilityRegistry(plugins=PluginRegistry([(_manifest(), factory)])).resolve_async(
            role_id="role-1",
            session_key="role:role-1",
            run_id="run-1",
            enabled_tools={"plugin.echo"},
        )
    )
    _, error = asyncio.run(
        capabilities.prepare_tool_call(
            "plugin.echo",
            {"message": "hello"},
            ToolContext("role-1", "role:role-1", "run-1", signal=CancellationToken()),
        )
    )
    assert error is not None
    assert error.content == "tool hook failed"
    assert error.details == {
        "hook": "demo.slow",
        "status": "timed_out",
        "errorType": "TimeoutError",
        "message": "tool hook timed out",
    }
    asyncio.run(capabilities.close())


def test_plugin_hook_stops_when_run_is_cancelled_while_awaiting():
    class CancellingHook:
        definition = HookDefinition(
            hook_id="demo.cancelling",
            mode="transform",
            tool_names=("plugin.echo",),
        )

        async def before_tool(self, context: HookContext) -> HookOutcome:
            context.tool_context.signal.cancel()
            await asyncio.sleep(0)
            return HookOutcome()

    def factory(context):
        del context
        return PluginContribution(tools=(EchoTool(),), hooks=(CancellingHook(),))

    capabilities = asyncio.run(
        CapabilityRegistry(plugins=PluginRegistry([(_manifest(), factory)])).resolve_async(
            role_id="role-1",
            session_key="role:role-1",
            run_id="run-1",
            enabled_tools={"plugin.echo"},
        )
    )
    _, error = asyncio.run(
        capabilities.prepare_tool_call(
            "plugin.echo",
            {"message": "hello"},
            ToolContext("role-1", "role:role-1", "run-1", signal=CancellationToken()),
        )
    )
    assert error is not None
    assert error.is_error is True
    assert error.details["status"] == "cancelled"
    assert error.details["message"] == "tool hook cancelled"
    asyncio.run(capabilities.close())


def test_plugin_activation_and_tool_allowlist_are_independent():
    def factory(context):
        del context
        return PluginContribution(tools=(EchoTool(),))

    registry = PluginRegistry([(_manifest(), factory)])

    disabled = asyncio.run(
        CapabilityRegistry(plugins=registry).resolve_async(
            role_id="role-1",
            session_key="role:role-1",
            run_id="run-disabled",
            enabled_plugin_ids=set(),
            enabled_tools={"plugin.echo"},
        )
    )
    assert disabled.snapshot.plugins == ()
    assert disabled.snapshot.tools == ()
    asyncio.run(disabled.close())

    plugin_enabled = asyncio.run(
        CapabilityRegistry(plugins=registry).resolve_async(
            role_id="role-1",
            session_key="role:role-1",
            run_id="run-plugin-only",
            enabled_plugin_ids={"demo"},
            enabled_tools=set(),
        )
    )
    assert plugin_enabled.snapshot.plugins[0].status == "loaded"
    assert plugin_enabled.snapshot.tools == ()
    assert plugin_enabled.snapshot.policy_decisions["plugin.echo"] == "denied:disabled"
    asyncio.run(plugin_enabled.close())

    fully_enabled = asyncio.run(
        CapabilityRegistry(plugins=registry).resolve_async(
            role_id="role-1",
            session_key="role:role-1",
            run_id="run-enabled",
            enabled_plugin_ids={"demo"},
            enabled_tools={"plugin.echo"},
        )
    )
    assert [definition.name for definition in fully_enabled.snapshot.tools] == ["plugin.echo"]
    assert fully_enabled.snapshot.policy_decisions["plugin.echo"] == "allowed"
    asyncio.run(fully_enabled.close())
