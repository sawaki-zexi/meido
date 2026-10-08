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
    PluginDescriptor,
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
from backend.app.agent_runtime.skills import SkillDescriptor


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
        declared_tools=("plugin.echo", "plugin.nested"),
        declared_skills=("plugin-study",),
        lifecycle_contributions=(
            "demo.uppercase",
            "demo.deny",
            "demo.observe",
            "demo.mutating-observer",
            "demo.nested-passthrough",
            "demo.slow",
            "demo.cancelling",
        ),
    )


def test_plugin_manifest_rejects_unknown_capability_and_duplicate_ids():
    with pytest.raises(ValueError, match="unknown capability"):
        PluginManifest("demo", "1.0.0", ("network.open",))
    with pytest.raises(ValueError, match="unsupported plugin trust level"):
        PluginManifest("demo", "1.0.0", trust_level="untrusted")
    with pytest.raises(ValueError, match="unsupported plugin source"):
        PluginManifest("demo", "1.0.0", source="forged")
    with pytest.raises(ValueError, match="exceeds source trust"):
        PluginManifest("demo", "1.0.0", source="external", trust_level="builtin")

    registry = PluginRegistry()
    registry.register(_manifest(), lambda context: PluginContribution())
    with pytest.raises(ValueError, match="duplicate plugin"):
        registry.register(_manifest(), lambda context: PluginContribution())

    with pytest.raises(ValueError, match="hook timeout must be positive"):
        HookDefinition("bad-timeout", "transform", timeout_seconds=float("nan"))
    with pytest.raises(ValueError, match="hook timeout must be positive"):
        HookDefinition("bad-bool-timeout", "transform", timeout_seconds=True)
    with pytest.raises(ValueError, match="hook timeout must be positive"):
        HookDefinition("bad-huge-timeout", "transform", timeout_seconds=10**1000)


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
    assert unavailable.diagnostics[0]["error"] == "host service unavailable"
    assert unavailable.diagnostics[0]["errorType"] == "HostServiceUnavailable"
    assert called == []


def test_plugin_context_exposes_a_readonly_copy_of_host_services():
    services = {"memory.read": object()}
    contexts = []

    def factory(context):
        contexts.append(context)
        return PluginContribution()

    result = asyncio.run(PluginRegistry([(_manifest(), factory)]).load(host_services=services))

    assert len(result.loaded) == 1
    original_service = contexts[0].host_services["memory.read"]
    services["memory.read"] = object()
    assert contexts[0].host_services["memory.read"] is original_service
    with pytest.raises(TypeError):
        contexts[0].host_services["memory.write"] = object()
    asyncio.run(result.close())


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


def test_untrusted_plugins_require_explicit_activation():
    registry = PluginRegistry([
        (
            PluginManifest("external-demo", "1.0.0", source="external", trust_level="external"),
            lambda context: PluginContribution(),
        ),
    ])

    implicit = asyncio.run(registry.load())
    assert implicit.loaded == ()
    assert implicit.diagnostics == ({
        "pluginId": "external-demo",
        "status": "denied",
        "error": "plugin requires explicit activation for its trust level",
    },)

    explicit = asyncio.run(registry.load(enabled_ids={"external-demo"}))
    assert [item.descriptor.manifest.plugin_id for item in explicit.loaded] == ["external-demo"]
    asyncio.run(explicit.close())


def test_plugin_can_contribute_declarative_skill():
    skill = SkillDescriptor(
        skill_id="plugin-study",
        version="1.0.0",
        description="Plugin study guidance",
        tools=("plugin.echo",),
        activation="explicit",
        source="builtin",
        path="skills/plugin-study/SKILL.md",
        content_hash="plugin-hash",
        body="先调用 echo，再总结结果。",
        trust_level="builtin",
    )
    registry = PluginRegistry([
        (
            _manifest(),
            lambda context: PluginContribution(tools=(EchoTool(),), skills=(skill,)),
        ),
    ])

    capabilities = asyncio.run(
        CapabilityRegistry(plugins=registry).resolve_async(
            role_id="role-1",
            session_key="role:role-1",
            run_id="run-plugin-skill",
            enabled_plugin_ids={"demo"},
            enabled_tools={"plugin.echo"},
            explicit_skill_ids={"plugin-study"},
        )
    )
    assert [item.skill_id for item in capabilities.snapshot.skills] == ["plugin-study"]
    assert capabilities.prompt_context() == "先调用 echo，再总结结果。"
    assert capabilities.snapshot.skills[0].tools == ("plugin.echo",)
    assert len(capabilities.snapshot.generation) == 24
    asyncio.run(capabilities.close())


def test_plugin_manifest_persists_runtime_config_and_generation_metadata():
    manifest = PluginManifest(
        "configured",
        "2.0.0",
        runtime_api="meido.agent_runtime.v2",
        config_schema={"type": "object"},
        config_defaults={"enabled": True},
        declared_tools=("configured.read",),
        declared_skills=("configured.help",),
        lifecycle_contributions=("configured.audit",),
        resource_dir="resources",
        manifest_hash="manifest-hash",
    )

    serialized = manifest.to_dict()
    assert serialized["runtimeApi"] == "meido.agent_runtime.v2"
    assert serialized["configDefaults"] == {"enabled": True}
    assert serialized["declaredTools"] == ["configured.read"]
    assert serialized["declaredSkills"] == ["configured.help"]
    assert serialized["lifecycleContributions"] == ["configured.audit"]
    assert serialized["resourceDir"] == "resources"
    assert serialized["generation"] == "manifest-hash"
    descriptor = PluginDescriptor(manifest, status="loaded").to_dict()
    assert descriptor["configSchema"] is None
    assert descriptor["configDefaults"] is None
    assert "enabled" not in str(descriptor)


def test_plugin_manifest_config_is_frozen_and_detached_from_serialized_copy():
    source_schema = {"properties": {"query": {"type": "string"}}}
    source_defaults = {"nested": {"values": ["one"]}}
    manifest = PluginManifest(
        "configured",
        "1.0.0",
        config_schema=source_schema,
        config_defaults=source_defaults,
    )

    source_schema["properties"]["query"]["type"] = "integer"
    source_defaults["nested"]["values"].append("two")

    assert manifest.config_schema["properties"]["query"]["type"] == "string"
    assert manifest.config_defaults["nested"]["values"] == ("one",)
    with pytest.raises(TypeError):
        manifest.config_schema["properties"]["query"]["type"] = "integer"
    with pytest.raises(AttributeError):
        manifest.config_defaults["nested"]["values"].append("three")

    serialized = manifest.to_dict()
    serialized["configDefaults"]["nested"]["values"].append("three")
    assert manifest.config_defaults["nested"]["values"] == ("one",)


def test_plugin_cannot_escalate_contributed_skill_trust():
    skill = SkillDescriptor(
        skill_id="untrusted-plugin-skill",
        version="1.0.0",
        description="Plugin skill",
        tools=(),
        activation="explicit",
        source="builtin",
        path="skills/untrusted/SKILL.md",
        content_hash="hash",
        body="规则",
        trust_level="builtin",
    )
    registry = PluginRegistry([
        (
            PluginManifest(
                "low-trust-demo",
                "1.0.0",
                source="builtin",
                trust_level="external",
                declared_skills=("untrusted-plugin-skill",),
            ),
            lambda context: PluginContribution(skills=(skill,)),
        ),
    ])

    result = asyncio.run(registry.load(enabled_ids={"low-trust-demo"}))

    assert result.loaded == ()
    assert result.diagnostics == ({
        "pluginId": "low-trust-demo",
        "status": "failed",
        "error": "plugin skill trust level exceeds plugin trust: untrusted-plugin-skill",
    },)


def test_plugin_contribution_source_must_match_manifest():
    class SpoofedSourceTool:
        definition = ToolDefinition(
            name="plugin.spoofed",
            description="Tool with forged provenance",
            source="builtin",
        )

        def execute(self, arguments, context, on_update=None):
            del arguments, context, on_update
            return AgentToolResult("unreachable")

    cleanup = []

    def contribute_spoofed_tool(context):
        context.add_cleanup(lambda: cleanup.append("closed"))
        return PluginContribution(tools=(SpoofedSourceTool(),))

    tool_result = asyncio.run(PluginRegistry([
        (
            PluginManifest(
                "demo",
                "1.0.0",
                declared_tools=("plugin.spoofed",),
            ),
            contribute_spoofed_tool,
        ),
    ]).load(enabled_ids={"demo"}))
    assert tool_result.loaded == ()
    assert cleanup == ["closed"]
    assert tool_result.diagnostics[0]["error"] == (
        "plugin tool source must be plugin:demo: plugin.spoofed"
    )

    skill = SkillDescriptor(
        skill_id="external-misattributed",
        version="1.0.0",
        description="External guidance",
        tools=(),
        activation="explicit",
        source="builtin",
        path="skills/external-misattributed/SKILL.md",
        content_hash="hash",
        body="规则",
        trust_level="external",
    )
    skill_result = asyncio.run(PluginRegistry([
        (
            PluginManifest(
                "external-demo",
                "1.0.0",
                source="external",
                trust_level="external",
                declared_skills=("external-misattributed",),
            ),
            lambda context: PluginContribution(skills=(skill,)),
        ),
    ]).load(enabled_ids={"external-demo"}))
    assert skill_result.loaded == ()
    assert skill_result.diagnostics[0]["error"] == (
        "plugin skill source does not match manifest source: external-misattributed"
    )


def test_plugin_contribution_keeps_tools_hooks_positional_contract():
    contribution = PluginContribution((EchoTool(),), (UppercaseHook(),))

    assert contribution.tools[0].definition.name == "plugin.echo"
    assert contribution.hooks[0].definition.hook_id == "demo.uppercase"
    assert contribution.skills == ()


def test_plugin_registry_rejects_contributions_outside_manifest_declarations():
    registry = PluginRegistry([
        (
            PluginManifest("demo", "1.0.0", declared_tools=("plugin.allowed",)),
            lambda context: PluginContribution(tools=(EchoTool(),)),
        ),
    ])

    result = asyncio.run(registry.load(enabled_ids={"demo"}))

    assert result.loaded == ()
    assert result.diagnostics[0]["status"] == "failed"
    assert result.diagnostics[0]["error"] == "plugin contributed undeclared tool: plugin.echo"


@pytest.mark.parametrize("contribution_kind", ["skill", "hook"])
def test_plugin_registry_rejects_undeclared_skill_and_hook(contribution_kind):
    if contribution_kind == "skill":
        skill = SkillDescriptor(
            skill_id="plugin-study",
            version="1.0.0",
            description="Study guidance",
            tools=(),
            activation="explicit",
            source="builtin",
            path="skills/plugin-study/SKILL.md",
            content_hash="hash",
            body="study",
            trust_level="builtin",
        )
        contribution = PluginContribution(skills=(skill,))
        expected = "plugin contributed undeclared skill: plugin-study"
    else:
        contribution = PluginContribution(hooks=(ObserveHook(),))
        expected = "plugin contributed undeclared hook: demo.observe"

    result = asyncio.run(PluginRegistry([
        (PluginManifest("demo", "1.0.0"), lambda context: contribution),
    ]).load(enabled_ids={"demo"}))

    assert result.loaded == ()
    assert result.diagnostics[0]["error"] == expected


def test_plugin_registry_rejects_duplicate_hook_ids_across_plugins():
    contribution = PluginContribution(hooks=(ObserveHook(),))
    registry = PluginRegistry([
        (
            PluginManifest("demo-a", "1.0.0", lifecycle_contributions=("demo.observe",)),
            lambda context: contribution,
        ),
        (
            PluginManifest("demo-b", "1.0.0", lifecycle_contributions=("demo.observe",)),
            lambda context: contribution,
        ),
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


def test_plugin_factory_error_diagnostic_does_not_persist_exception_secrets():
    cleanup = []
    secret = "plugin-api-key-do-not-persist"

    def fail_cleanup():
        cleanup.append("closed")
        raise RuntimeError(f"cleanup failed with {secret}")

    def factory(context):
        context.add_cleanup(fail_cleanup)
        raise RuntimeError(f"failed to initialize with {secret}")

    registry = PluginRegistry([(_manifest(), factory)])
    capabilities = asyncio.run(CapabilityRegistry(plugins=registry).resolve_async(
        role_id="role-1",
        session_key="role:role-1",
        run_id="run-secret-plugin-error",
        enabled_plugin_ids={"demo"},
    ))

    assert cleanup == ["closed"]
    diagnostic = dict(capabilities.snapshot.plugin_diagnostics[0])
    assert diagnostic["status"] == "failed"
    assert diagnostic["error"] == "plugin setup and rollback cleanup failed"
    assert diagnostic["errorType"] == "RuntimeError"
    assert diagnostic["cleanupErrorType"] == "PluginError"
    assert secret not in str(capabilities.snapshot.to_dict())
    asyncio.run(capabilities.close())


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


def test_plugin_load_cancellation_rolls_back_loaded_and_pending_plugins():
    cleanup = []
    setup_started = asyncio.Event()
    first = PluginManifest("first", "1.0.0")
    second = PluginManifest("second", "1.0.0")

    def first_factory(context):
        context.add_cleanup(lambda: cleanup.append("first"))
        return PluginContribution()

    async def second_factory(context):
        def failing_cleanup():
            raise RuntimeError("cleanup failed")

        context.add_cleanup(lambda: cleanup.append("second"))
        context.add_cleanup(failing_cleanup)
        setup_started.set()
        await asyncio.Event().wait()
        return PluginContribution()

    registry = PluginRegistry([
        (first, first_factory),
        (second, second_factory),
    ])

    async def cancel_load() -> None:
        task = asyncio.create_task(registry.load(enabled_ids={"first", "second"}))
        await setup_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_load())
    assert cleanup == ["second", "first"]


def test_plugin_resolution_close_finishes_all_cleanup_after_cancellation():
    cleanup = []
    cleanup_started = asyncio.Event()
    allow_cleanup_to_finish = asyncio.Event()
    first = PluginManifest("first", "1.0.0")
    second = PluginManifest("second", "1.0.0")

    def first_factory(context):
        context.add_cleanup(lambda: cleanup.append("first"))
        return PluginContribution()

    async def second_factory(context):
        async def blocking_cleanup():
            cleanup_started.set()
            await allow_cleanup_to_finish.wait()
            cleanup.append("second-async")

        context.add_cleanup(blocking_cleanup)
        context.add_cleanup(lambda: cleanup.append("second-prior-cleanup"))
        return PluginContribution()

    registry = PluginRegistry([
        (first, first_factory),
        (second, second_factory),
    ])

    async def cancel_close() -> None:
        resolution = await registry.load(enabled_ids={"first", "second"})
        task = asyncio.create_task(resolution.close())
        await cleanup_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        allow_cleanup_to_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_close())
    assert cleanup == ["second-prior-cleanup", "second-async", "first"]


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
    assert error is None
    assert len(capabilities.hook_diagnostics) == 1
    diagnostic = capabilities.hook_diagnostics[0]
    assert diagnostic["hook"] == "demo.mutating-observer"
    assert diagnostic["status"] == "failed"
    assert diagnostic["message"] == "tool hook failed"
    asyncio.run(capabilities.close())


def test_transform_hook_can_return_nested_readonly_arguments():
    class NestedTool:
        definition = ToolDefinition(
            name="plugin.nested",
            description="Accept nested arguments",
            source="plugin:demo",
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
