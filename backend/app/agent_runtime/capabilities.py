from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
import asyncio
from collections.abc import Mapping as MappingABC
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, cast, TYPE_CHECKING
from collections.abc import Awaitable, Callable

from .plugins import HookContext, PluginDescriptor, PluginResolution, PluginRegistry, run_hook
from .skills import SkillDescriptor, SkillRegistry, SkillResolution
from .tools import AgentTool, AgentToolResult, ToolContext, ToolDefinition, ToolRegistry

if TYPE_CHECKING:
    from .discovery import DiscoveryState, ServiceGrant


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """Serializable description of the capabilities used by one run."""

    snapshot_id: str
    run_id: str
    role_id: str
    session_key: str
    tools: tuple[ToolDefinition, ...]
    policy_decisions: MappingProxyType
    skills: tuple[SkillDescriptor, ...] = ()
    prompt_sections: tuple[str, ...] = ()
    skill_diagnostics: tuple[MappingProxyType, ...] = ()
    plugins: tuple[PluginDescriptor, ...] = ()
    plugin_diagnostics: tuple[MappingProxyType, ...] = ()
    generation: str = ""
    candidates: tuple[ToolDefinition, ...] = ()
    visibility_version: int = 0
    parent_snapshot_id: str | None = None
    candidate_catalog_id: str = ""
    service_grants: tuple[ServiceGrant, ...] = ()

    def provider_schemas(self) -> list[dict[str, object]]:
        return [definition.as_provider_schema() for definition in self.tools]

    def to_dict(self, *, include_prompt_sections: bool = True) -> dict[str, object]:
        snapshot: dict[str, object] = {
            "snapshotId": self.snapshot_id,
            "runId": self.run_id,
            "roleId": self.role_id,
            "sessionKey": self.session_key,
            "tools": [_definition_payload(definition) for definition in self.tools],
            "policyDecisions": dict(self.policy_decisions),
            "skills": [
                skill.to_dict()
                for skill in self.skills
            ],
            "skillDiagnostics": [_thaw_json(item) for item in self.skill_diagnostics],
            "plugins": [plugin.to_dict() for plugin in self.plugins],
            "pluginDiagnostics": [dict(item) for item in self.plugin_diagnostics],
            "generation": self.generation,
        }
        if include_prompt_sections:
            snapshot["promptSections"] = list(self.prompt_sections)
        if self.candidates:
            snapshot["candidates"] = [_definition_payload(item) for item in self.candidates]
            snapshot["visibilityVersion"] = self.visibility_version
            snapshot["parentSnapshotId"] = self.parent_snapshot_id
            snapshot["candidateCatalogId"] = self.candidate_catalog_id
            snapshot["serviceGrants"] = [item.to_dict() for item in self.service_grants]
        return snapshot


@dataclass(frozen=True, slots=True)
class CapabilityResolution:
    """A frozen snapshot plus the process-local tools that implement it."""

    snapshot: CapabilitySnapshot
    tools: tuple[AgentTool, ...]
    denied_tools: MappingProxyType
    plugin_resolution: PluginResolution | None = None
    _hook_diagnostics: list[MappingProxyType] = field(default_factory=list, repr=False, compare=False)
    _tools_by_name: MappingProxyType = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
        compare=False,
    )
    _definitions_by_name: MappingProxyType = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
        compare=False,
    )
    _discovery: DiscoveryState | None = field(default=None, repr=False, compare=False)
    _authorize_tool: Callable[[ToolDefinition, ToolContext], bool | str] | None = field(default=None, repr=False, compare=False)
    _authorize_call: Callable[[ToolDefinition, ToolContext, Mapping[str, object]], bool | str] | None = field(default=None, repr=False, compare=False)
    _candidate_tools: MappingProxyType = field(default_factory=lambda: MappingProxyType({}), repr=False, compare=False)
    _resource_close: Callable[[], Awaitable[None]] | None = field(default=None, repr=False, compare=False)
    _executors_by_name: MappingProxyType = field(default_factory=lambda: MappingProxyType({}), repr=False, compare=False)
    _definition_fingerprints: MappingProxyType = field(default_factory=lambda: MappingProxyType({}), repr=False, compare=False)
    source_scope: str = ""
    restoration_diagnostics: tuple[dict[str, str], ...] = ()

    def selection_cache(self) -> dict[str, object]:
        """A restoration hint, not authority; no candidate catalog or schemas."""
        return {"format": "tool-selection-v1", "roleId": self.snapshot.role_id,
                "sessionKey": self.snapshot.session_key, "sourceScope": self.source_scope,
                "selected": [{"name": item.name, "source": item.source,
                    "fingerprint": definition_fingerprint(item)}
                    for item in self.snapshot.tools if item.exposure == "deferred"]}

    def call_record(self, name: str) -> dict[str, object]:
        """Used declaration and minimal author evidence, never unused candidates."""
        definition = self.candidate_definition(name)
        return {"format": "tool-call-v1", "snapshotId": self.snapshot.snapshot_id,
                "runId": self.snapshot.run_id, "roleId": self.snapshot.role_id,
                "sessionKey": self.snapshot.session_key, "sourceScope": self.source_scope,
                "visibilityVersion": self.snapshot.visibility_version,
                "tools": [_definition_payload(definition)] if definition is not None else [],
                "active": self.definition(name) is not None,
                "definitionFingerprint": definition_fingerprint(definition) if definition is not None else None,
                "skills": [{"id": item.skill_id, "source": item.source, "contentHash": item.content_hash}
                    for item in self.snapshot.skills if item.skill_id == "create-drift-skill"]}

    def selected_subset(self, names: frozenset[str] | set[str]) -> CapabilityResolution:
        """A delegation holds only its selected bindings, not the parent's catalog."""
        definitions = tuple(item for item in self.snapshot.tools if item.name in names)
        selected = {item.name for item in definitions}
        return replace(self, snapshot=replace(self.snapshot, tools=definitions, candidates=(),
            skills=(), prompt_sections=(), plugins=(), service_grants=(),
            policy_decisions=MappingProxyType({name: "allowed" for name in selected})),
            tools=tuple(self._tools_by_name[name] for name in selected), plugin_resolution=None,
            _discovery=None, _resource_close=None, denied_tools=MappingProxyType({}),
            _tools_by_name=MappingProxyType({name: self._tools_by_name[name] for name in selected}),
            _definitions_by_name=MappingProxyType({name: self._definitions_by_name[name] for name in selected}),
            _candidate_tools=MappingProxyType({}), restoration_diagnostics=(),
            _executors_by_name=MappingProxyType({name: self._executors_by_name[name] for name in selected}),
            _definition_fingerprints=MappingProxyType({name: value for name, value in self._definition_fingerprints.items() if name in selected}))

    def run_metadata(self) -> dict[str, object]:
        """Compact run diagnostics; actual declarations live on their call rows."""
        return {"format": "tool-run-v1", "snapshotId": self.snapshot.snapshot_id,
                "runId": self.snapshot.run_id, "roleId": self.snapshot.role_id,
                "sessionKey": self.snapshot.session_key, "sourceScope": self.source_scope,
                "skills": [item.to_dict() for item in self.snapshot.skills],
                "skillDiagnostics": [_thaw_json(item) for item in self.snapshot.skill_diagnostics],
                "plugins": [item.to_dict() for item in self.snapshot.plugins],
                "pluginDiagnostics": [dict(item) for item in self.snapshot.plugin_diagnostics],
                "restorationDiagnostics": list(self.restoration_diagnostics)}

    def stage_service_tools(self, context: ToolContext, *, grant_id: str, parent_snapshot_id: str,
                            tools: tuple[AgentTool, ...], activate: bool = False, rebind: bool = False) -> tuple[str, ...]:
        if self._discovery is None:
            raise PermissionError("service revisions require a frozen discovery directory")
        return self._discovery.stage_service_tools(context, grant_id=grant_id, parent_snapshot_id=parent_snapshot_id, tools=tools, activate=activate, rebind=rebind)

    def stage_service_removal(self, context: ToolContext, *, grant_id: str, parent_snapshot_id: str) -> None:
        if self._discovery is None:
            raise PermissionError("service revisions require a frozen discovery directory")
        self._discovery.stage_service_removal(context, grant_id=grant_id, parent_snapshot_id=parent_snapshot_id)

    def authorization_denial(self, definition: ToolDefinition | None, context: ToolContext,
                             arguments: Mapping[str, object] | None = None) -> str | None:
        if definition is None:
            return "tool has no frozen definition"
        if (context.role_id, context.session_key, context.run_id) != (self.snapshot.role_id, self.snapshot.session_key, self.snapshot.run_id):
            return "tool context does not match the frozen run scope"
        original = self._candidate_tools.get(definition.name) or self._tools_by_name.get(definition.name)
        fingerprint = self._definition_fingerprints.get(definition.name)
        if original is not None and fingerprint is not None and definition_fingerprint(original.definition) != fingerprint:
            return "stale_definition: the request's tool implementation declaration changed"
        if self._authorize_tool is not None:
            decision = self._authorize_tool(definition, context)
            if decision is not True:
                return str(decision) if isinstance(decision, str) else "tool permission was revoked"
        if arguments is not None and self._authorize_call is not None:
            decision = self._authorize_call(definition, context, arguments)
            if decision is not True:
                return str(decision) if isinstance(decision, str) else "tool call permission was revoked"
        return None

    def current(self) -> CapabilityResolution:
        return self._discovery.current if self._discovery is not None else self

    def pending_revision(self, call_id: str) -> CapabilityResolution | None:
        return self._discovery.propose(call_id) if self._discovery is not None else None

    def discard_pending(self, call_id: str | None = None) -> None:
        if self._discovery is not None:
            self._discovery.discard_pending(call_id)

    def publish_revision(self, revision: CapabilityResolution) -> None:
        if self._discovery is None:
            raise ValueError("tool discovery is not enabled")
        self._discovery.publish(revision)

    def provider_schemas(self) -> list[dict[str, object]]:
        if self._definitions_by_name:
            return [definition.as_provider_schema() for definition in self._definitions_by_name.values()]
        return self.snapshot.provider_schemas()

    def get(self, name: str) -> AgentTool | None:
        return self._tools_by_name.get(name)

    def tools_for_binding(self) -> tuple[AgentTool, ...]:
        """Host initialization of admitted candidates, including deferred tools.

        This does not publish schemas or permit execution; callers must still
        use the request's selected definition and captured executor at dispatch.
        """
        return tuple(self._candidate_tools.values()) or self.tools

    def executor(self, name: str):
        """The callable captured with this declaration, not a latest-name lookup."""
        return self._executors_by_name[name]

    def definition(self, name: str) -> ToolDefinition | None:
        """Return the immutable-at-resolution policy used by this run."""

        definition = self._definitions_by_name.get(name)
        return deepcopy(definition) if definition is not None else None

    def candidate_definition(self, name: str) -> ToolDefinition | None:
        """Inspect the frozen declaration for audit, without granting execution."""
        definition = next((item for item in self.snapshot.candidates if item.name == name), None)
        return replace(definition, input_schema=cast(dict[str, object], _thaw_json(definition.input_schema))) if definition is not None else self.definition(name)

    def denial_reason(self, name: str) -> str | None:
        reason = self.denied_tools.get(name)
        return str(reason) if reason is not None else None

    def prompt_context(self) -> str:
        return "\n\n".join(self.snapshot.prompt_sections)

    def validate_arguments(self, name: str, arguments: dict[str, object]) -> None:
        definition = self.definition(name)
        if definition is None:
            raise ValueError(f"unknown tool: {name}")
        ToolRegistry.validate_definition(definition, arguments)

    @property
    def hooks(self):
        return self.plugin_resolution.hooks if self.plugin_resolution is not None else ()

    @property
    def hook_diagnostics(self) -> tuple[MappingProxyType, ...]:
        """Return non-blocking Hook diagnostics collected during this run."""

        return tuple(self._hook_diagnostics)

    async def prepare_tool_call(
        self,
        name: str,
        arguments: dict[str, object],
        context: ToolContext,
    ) -> tuple[dict[str, object], AgentToolResult | None]:
        current = dict(arguments)
        for hook in self.hooks:
            if hook.definition.tool_names and name not in hook.definition.tool_names:
                continue
            try:
                context.signal.raise_if_cancelled()
                # Hooks may only return transformed arguments explicitly; do not
                # let an in-place mutation bypass the hook mode contract.
                hook_arguments = _readonly_arguments(deepcopy(current))
                pending = run_hook(hook, HookContext(name, hook_arguments, context))
                outcome = (
                    await asyncio.wait_for(pending, timeout=hook.definition.timeout_seconds)
                    if hook.definition.timeout_seconds is not None
                    else await pending
                )
                context.signal.raise_if_cancelled()
            except Exception as error:
                status = (
                    "timed_out"
                    if isinstance(error, asyncio.TimeoutError)
                    else "cancelled"
                    if isinstance(error, RuntimeError) and str(error) == "agent run cancelled"
                    else "failed"
                )
                failure = AgentToolResult(
                    "tool hook failed",
                    details={
                        "hook": hook.definition.hook_id,
                        "status": status,
                        "errorType": type(error).__name__,
                        "message": (
                            "tool hook timed out"
                            if status == "timed_out"
                            else "tool hook cancelled"
                            if status == "cancelled"
                            else "tool hook failed"
                        ),
                    },
                    is_error=True,
                )
                if status == "cancelled":
                    return current, failure
                if hook.definition.mode == "observe":
                    details = failure.details if isinstance(failure.details, dict) else {}
                    self._hook_diagnostics.append(MappingProxyType(dict(details)))
                    continue
                return current, failure
            if not outcome.allowed:
                return current, AgentToolResult(
                    "tool call denied by hook",
                    details={
                        "hook": hook.definition.hook_id,
                        "status": "denied",
                        "reason": outcome.reason or "denied",
                    },
                    is_error=True,
                )
            if outcome.arguments is not None:
                current = _mutable_arguments(outcome.arguments)
        return current, None

    async def close(self) -> None:
        self.discard_pending()
        try:
            if self.plugin_resolution is not None:
                await self.plugin_resolution.close()
        finally:
            if self._resource_close is not None:
                await self._resource_close()


def _readonly_arguments(arguments: dict[str, object]) -> Mapping[str, object]:
    return MappingProxyType({
        key: _readonly_value(value)
        for key, value in arguments.items()
    })


def _readonly_value(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({
            key: _readonly_value(item)
            for key, item in value.items()
        })
    if isinstance(value, list):
        return tuple(_readonly_value(item) for item in value)
    return value


def _mutable_arguments(arguments: Mapping[str, object]) -> dict[str, object]:
    return {
        key: _mutable_value(value)
        for key, value in arguments.items()
    }


def _mutable_value(value: object) -> object:
    if isinstance(value, MappingABC):
        return {
            key: _mutable_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_mutable_value(item) for item in value]
    return value


def _freeze_json(value: object) -> object:
    if isinstance(value, MappingABC):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return deepcopy(value)


def _thaw_json(value: object) -> object:
    if isinstance(value, MappingABC):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return deepcopy(value)


def _definition_payload(definition: ToolDefinition) -> dict[str, object]:
    return {
        "name": definition.name, "description": definition.description,
        "inputSchema": _thaw_json(definition.input_schema), "source": definition.source,
        "version": definition.version, "risk": definition.risk, "exposure": definition.exposure,
        "approval": definition.approval, "timeoutSeconds": definition.timeout_seconds,
        "outputLimit": definition.output_limit, "externalAllowed": definition.external_allowed,
        **({"searchHint": definition.search_hint} if definition.search_hint else {}),
        **({"namespaceSummary": definition.namespace_summary} if getattr(definition, "namespace_summary", "") else {}),
    }


class CapabilityRegistry:
    """Resolve registered tools into a stable, role-scoped run snapshot."""

    def __init__(
        self,
        tools: tuple[AgentTool, ...] | list[AgentTool] = (),
        *,
        skills: SkillRegistry | None = None,
        plugins: PluginRegistry | None = None,
    ) -> None:
        self._tools: dict[str, AgentTool] = {}
        self.skills = skills or SkillRegistry()
        self.plugins = plugins or PluginRegistry()
        for tool in tools:
            self.register(tool)

    def register(self, tool: AgentTool) -> None:
        name = tool.definition.name
        if name in self._tools:
            raise ValueError(f"duplicate tool: {name}")
        self._tools[name] = tool

    def resolve(
        self,
        *,
        role_id: str,
        session_key: str,
        run_id: str,
        enabled_tools: set[str] | frozenset[str] | None = None,
        activated_tools: set[str] | frozenset[str] = frozenset(),
        approved_tools: set[str] | frozenset[str] = frozenset(),
        allowed_risks: set[str] | frozenset[str] | None = None,
        external_only: bool = False,
        prompt_text: str = "",
        explicit_skill_ids: set[str] | frozenset[str] = frozenset(),
        plugin_resolution: PluginResolution | None = None,
        discovery_enabled: bool = False,
        direct_exposure: bool = False,
        authorize_tool: Callable[[ToolDefinition, ToolContext], bool | str] | None = None,
        authorize_call: Callable[[ToolDefinition, ToolContext, Mapping[str, object]], bool | str] | None = None,
        deferred_tool_names: frozenset[str] = frozenset(),
        restored_activation: Mapping[str, object] | None = None,
        source_scope: str = "",
        service_grants: tuple[ServiceGrant, ...] = (),
        resource_close: Callable[[], Awaitable[None]] | None = None,
        skill_dependency_check=None,
    ) -> CapabilityResolution:
        registered_tools = list(self._tools.values())
        if plugin_resolution is not None:
            registered_tools.extend(plugin_resolution.tools)
        discovery = None
        if discovery_enabled:
            from .discovery import DiscoveryState, ToolSearchTool
            discovery = DiscoveryState(service_grants=service_grants, external_only=external_only)
            # Shiori's search switch controls schema loading, not permission to
            # use the core discovery tool. Direct mode can report already_loaded.
            registered_tools.append(ToolSearchTool(discovery))
        names: set[str] = set()
        for tool in registered_tools:
            if tool.definition.name in names:
                raise ValueError(f"duplicate tool: {tool.definition.name}")
            names.add(tool.definition.name)
        selected: list[AgentTool] = []
        candidates: list[AgentTool] = []
        admitted_definitions: dict[str, ToolDefinition] = {}
        decisions: dict[str, str] = {}
        denied: dict[str, str] = {}
        restored_definitions = {}
        restored_order: list[str] = []
        diagnostics: list[dict[str, str]] = []
        if (discovery_enabled and restored_activation is not None and restored_activation.get("roleId") == role_id
                and restored_activation.get("sessionKey") == session_key
                and restored_activation.get("sourceScope", "") == source_scope):
            if restored_activation.get("format") == "tool-selection-v1":
                restored_tools = restored_activation.get("selected", [])
            elif not restored_activation.get("format"):
                restored_tools = restored_activation.get("tools", [])
            else:
                restored_tools = []
            if isinstance(restored_tools, list):
                restored_definitions = {item["name"]: item for item in restored_tools
                    if isinstance(item, dict) and isinstance(item.get("name"), str)}
                restored_order = list(restored_definitions)
        for tool in registered_tools:
            definition = tool.definition
            if direct_exposure and definition.exposure == 'deferred':
                definition = replace(definition, exposure='direct')
            elif discovery_enabled and not direct_exposure and definition.name in deferred_tool_names:
                definition = replace(definition, exposure='deferred')
            if external_only and not definition.external_allowed:
                denied[definition.name] = "tool is not allowed for external context"
                decisions[definition.name] = "denied:external_context"
                continue
            if enabled_tools is not None and definition.name not in enabled_tools:
                denied[definition.name] = "tool is disabled for this run"
                decisions[definition.name] = "denied:disabled"
                continue
            if allowed_risks is not None and definition.risk not in allowed_risks:
                denied[definition.name] = "tool risk is not allowed for this run"
                decisions[definition.name] = "denied:risk"
                continue
            if definition.approval == "deny":
                denied[definition.name] = "tool approval policy denies this run"
                decisions[definition.name] = "denied:approval"
                continue
            if definition.approval in {"prompt", "writes"} and definition.name not in approved_tools:
                denied[definition.name] = f"tool approval required: {definition.approval}"
                decisions[definition.name] = "denied:approval_required"
                continue
            if definition.exposure == "hidden":
                denied[definition.name] = "tool is hidden"
                decisions[definition.name] = "denied:hidden"
                continue
            candidates.append(tool)
            admitted_definitions[definition.name] = definition
            saved = restored_definitions.get(definition.name)
            restored = saved == _definition_payload(definition) or (
                saved is not None and saved == {"name": definition.name, "source": definition.source,
                                               "fingerprint": definition_fingerprint(definition)})
            if saved is not None and not restored:
                diagnostics.append({"name": definition.name, "reason": "definition_changed"})
            if restored and authorize_tool is not None:
                from .provider import CancellationToken
                if authorize_tool(definition, ToolContext(role_id, session_key, run_id, CancellationToken())) is not True:
                    restored = False
                    diagnostics.append({"name": definition.name, "reason": "revoked"})
            if definition.exposure == "deferred" and definition.name not in activated_tools and not restored:
                denied[definition.name] = "tool is deferred and not activated" + (
                    f"; use tool_search(query='select:{definition.name}') and call it in the next provider request" if discovery_enabled else "")
                decisions[definition.name] = "denied:deferred"
                continue
            selected.append(tool)
            decisions[definition.name] = "allowed"

        if enabled_tools is not None:
            unknown_tools = set(enabled_tools) - names
            for name in sorted(unknown_tools):
                denied[name] = "tool is not registered"
                decisions[name] = "denied:unknown"

        for name in restored_order:
            if name not in admitted_definitions:
                diagnostics.append({"name": name, "reason": "absent" if name not in names else "revoked"})
        # Baseline first, then previous load order, then explicitly activated tools.
        if restored_order:
            order = {name: index for index, name in enumerate(restored_order)}
            selected.sort(key=lambda tool: (admitted_definitions[tool.definition.name].exposure == "deferred",
                order.get(tool.definition.name, len(order))))

        if discovery is not None and restored_order:
            from .search_budget import select_schema_budget
            baseline = [admitted_definitions[tool.definition.name] for tool in selected
                        if admitted_definitions[tool.definition.name].exposure != "deferred"]
            deferred = [admitted_definitions[tool.definition.name] for tool in selected
                        if admitted_definitions[tool.definition.name].exposure == "deferred"]
            budget = select_schema_budget(deferred, baseline=baseline, new_limit=None)
            skipped = {item.name for item in budget.skipped}
            selected = [tool for tool in selected if tool.definition.name not in skipped]
            for name in skipped:
                denied[name] = "tool restoration exceeded the current declaration budget"
                decisions[name] = "denied:budget"
                diagnostics.append({"name": name, "reason": "budget"})

        execution_definitions = tuple(replace(admitted_definitions[tool.definition.name], input_schema=deepcopy(admitted_definitions[tool.definition.name].input_schema)) for tool in selected)
        snapshot_definitions = tuple(
            replace(
                definition,
                input_schema=cast(dict[str, object], _freeze_json(definition.input_schema)),
            )
            for definition in execution_definitions
        )
        skill_registry = self.skills
        if plugin_resolution is not None and plugin_resolution.skills:
            skill_registry = SkillRegistry(
                (*self.skills.skills, *plugin_resolution.skills),
                diagnostics=self.skills.diagnostics,
            )
        skill_resolution: SkillResolution = skill_registry.resolve(
            prompt=prompt_text,
            explicit_ids=explicit_skill_ids,
            available_tools={tool.definition.name for tool in registered_tools},
            enabled_tools=set(tool.definition.name for tool in selected),
            dependency_check=skill_dependency_check,
        )
        snapshot = CapabilitySnapshot(
            snapshot_id=f"cap-{run_id}",
            run_id=run_id,
            role_id=role_id,
            session_key=session_key,
            tools=snapshot_definitions,
            policy_decisions=MappingProxyType(dict(decisions)),
            skills=skill_resolution.skills,
            prompt_sections=skill_resolution.prompt_sections,
            skill_diagnostics=skill_resolution.diagnostics,
            plugins=plugin_resolution.descriptors if plugin_resolution is not None else (),
            plugin_diagnostics=plugin_resolution.diagnostics if plugin_resolution is not None else (),
            generation=_snapshot_generation(
                tools=snapshot_definitions,
                skills=skill_resolution.skills,
                plugins=plugin_resolution.descriptors if plugin_resolution is not None else (),
                policy_decisions=decisions,
            ),
            candidates=tuple(replace(definition, input_schema=cast(dict[str, object], _freeze_json(definition.input_schema))) for definition in admitted_definitions.values()) if discovery is not None else (),
            service_grants=tuple(service_grants) if discovery is not None else (),
        )
        resolution = CapabilityResolution(
            snapshot=snapshot,
            tools=tuple(selected),
            denied_tools=MappingProxyType(dict(denied)),
            plugin_resolution=plugin_resolution,
            _tools_by_name=MappingProxyType({tool.definition.name: tool for tool in selected}),
            _definitions_by_name=MappingProxyType({
                definition.name: definition
                for definition in execution_definitions
            }),
            _discovery=discovery,
            _authorize_tool=authorize_tool,
            _authorize_call=authorize_call,
            _candidate_tools=MappingProxyType({tool.definition.name: tool for tool in candidates}),
            _executors_by_name=MappingProxyType({tool.definition.name: tool.execute for tool in candidates}),
            _definition_fingerprints=MappingProxyType({tool.definition.name: definition_fingerprint(tool.definition) for tool in candidates}),
            _resource_close=resource_close,
            source_scope=source_scope,
            restoration_diagnostics=tuple(diagnostics[:128]),
        )
        if discovery is not None:
            snapshot = replace(snapshot, candidate_catalog_id=_candidate_catalog_id(snapshot))
            resolution = replace(resolution, snapshot=snapshot)
            discovery.bind(resolution, names)
        return resolution

    async def resolve_async(
        self,
        *,
        role_id: str,
        session_key: str,
        run_id: str,
        enabled_tools: set[str] | frozenset[str] | None = None,
        activated_tools: set[str] | frozenset[str] = frozenset(),
        approved_tools: set[str] | frozenset[str] = frozenset(),
        allowed_risks: set[str] | frozenset[str] | None = None,
        external_only: bool = False,
        prompt_text: str = "",
        explicit_skill_ids: set[str] | frozenset[str] = frozenset(),
        enabled_plugin_ids: set[str] | frozenset[str] | None = None,
        granted_capabilities: set[str] | frozenset[str] = frozenset(),
        host_services: Mapping[str, object] = MappingProxyType({}),
        plugin_dir: str = "",
        discovery_enabled: bool = False,
        direct_exposure: bool = False,
        authorize_tool: Callable[[ToolDefinition, ToolContext], bool | str] | None = None,
        authorize_call: Callable[[ToolDefinition, ToolContext, Mapping[str, object]], bool | str] | None = None,
        deferred_tool_names: frozenset[str] = frozenset(),
        restored_activation: Mapping[str, object] | None = None,
        source_scope: str = "",
        service_grants: tuple[ServiceGrant, ...] = (),
        resource_close: Callable[[], Awaitable[None]] | None = None,
        skill_dependency_check=None,
    ) -> CapabilityResolution:
        plugin_resolution = await self.plugins.load(
            enabled_ids=enabled_plugin_ids,
            granted_capabilities=granted_capabilities,
            host_services=host_services,
            plugin_dir=plugin_dir,
            role_id=role_id,
            run_id=run_id,
        )
        try:
            return self.resolve(
                role_id=role_id,
                session_key=session_key,
                run_id=run_id,
                enabled_tools=enabled_tools,
                activated_tools=activated_tools,
                approved_tools=approved_tools,
                allowed_risks=allowed_risks,
                external_only=external_only,
                prompt_text=prompt_text,
                explicit_skill_ids=explicit_skill_ids,
                plugin_resolution=plugin_resolution,
                discovery_enabled=discovery_enabled,
                direct_exposure=direct_exposure,
                authorize_tool=authorize_tool,
                authorize_call=authorize_call,
                deferred_tool_names=deferred_tool_names,
                restored_activation=restored_activation,
                source_scope=source_scope,
                service_grants=service_grants,
                resource_close=resource_close,
                skill_dependency_check=skill_dependency_check,
            )
        except BaseException:
            try:
                await plugin_resolution.close()
            except Exception:
                pass
            raise


def definition_fingerprint(definition: ToolDefinition) -> str:
    """Full safety declaration, not merely the provider-visible schema."""
    return hashlib.sha256(json.dumps(_definition_payload(definition), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _snapshot_generation(
    *,
    tools: tuple[ToolDefinition, ...],
    skills: tuple[SkillDescriptor, ...],
    plugins: tuple[PluginDescriptor, ...],
    policy_decisions: Mapping[str, str],
) -> str:
    payload = {
        "tools": [
            _definition_payload(item)
            for item in sorted(tools, key=lambda item: (item.name, item.version, item.source))
        ],
        "skills": [
            {
                "id": item.skill_id,
                "version": item.version,
                "contentHash": item.content_hash,
                "supportingFiles": [resource.to_dict() for resource in item.supporting_files],
                "trustLevel": item.trust_level,
            }
            for item in sorted(skills, key=lambda item: (item.skill_id, item.version, item.source, item.path))
        ],
        "plugins": [
            item.to_dict()
            for item in sorted(plugins, key=lambda item: (item.manifest.plugin_id, item.manifest.version))
        ],
        "policyDecisions": dict(sorted(policy_decisions.items())),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _candidate_catalog_id(snapshot: CapabilitySnapshot) -> str:
    return "catalog-" + _snapshot_generation(tools=snapshot.candidates, skills=(), plugins=snapshot.plugins,
        policy_decisions={"scope": json.dumps([snapshot.role_id, snapshot.session_key], ensure_ascii=False),
                          "serviceGrants": json.dumps([item.to_dict() for item in snapshot.service_grants], sort_keys=True)})
