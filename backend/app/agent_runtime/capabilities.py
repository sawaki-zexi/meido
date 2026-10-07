from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import asyncio
from collections.abc import Mapping as MappingABC
from types import MappingProxyType
from typing import Mapping

from .plugins import HookContext, PluginDescriptor, PluginResolution, PluginRegistry, run_hook
from .skills import SkillDescriptor, SkillRegistry, SkillResolution
from .tools import AgentTool, AgentToolResult, ToolContext, ToolDefinition, ToolRegistry


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

    def provider_schemas(self) -> list[dict[str, object]]:
        return [definition.as_provider_schema() for definition in self.tools]

    def to_dict(self, *, include_prompt_sections: bool = True) -> dict[str, object]:
        snapshot: dict[str, object] = {
            "snapshotId": self.snapshot_id,
            "runId": self.run_id,
            "roleId": self.role_id,
            "sessionKey": self.session_key,
            "tools": [
                {
                    "name": definition.name,
                    "version": definition.version,
                    "source": definition.source,
                    "risk": definition.risk,
                    "exposure": definition.exposure,
                    "approval": definition.approval,
                    "timeoutSeconds": definition.timeout_seconds,
                    "outputLimit": definition.output_limit,
                    "inputSchema": deepcopy(definition.input_schema),
                    "description": definition.description,
                }
                for definition in self.tools
            ],
            "policyDecisions": dict(self.policy_decisions),
            "skills": [
                skill.to_dict()
                for skill in self.skills
            ],
            "skillDiagnostics": [dict(item) for item in self.skill_diagnostics],
            "plugins": [plugin.to_dict() for plugin in self.plugins],
            "pluginDiagnostics": [dict(item) for item in self.plugin_diagnostics],
        }
        if include_prompt_sections:
            snapshot["promptSections"] = list(self.prompt_sections)
        return snapshot


@dataclass(frozen=True, slots=True)
class CapabilityResolution:
    """A frozen snapshot plus the process-local tools that implement it."""

    snapshot: CapabilitySnapshot
    tools: tuple[AgentTool, ...]
    denied_tools: MappingProxyType
    plugin_resolution: PluginResolution | None = None

    def provider_schemas(self) -> list[dict[str, object]]:
        return self.snapshot.provider_schemas()

    def get(self, name: str) -> AgentTool | None:
        return next((tool for tool in self.tools if tool.definition.name == name), None)

    def denial_reason(self, name: str) -> str | None:
        reason = self.denied_tools.get(name)
        return str(reason) if reason is not None else None

    def prompt_context(self) -> str:
        return "\n\n".join(self.snapshot.prompt_sections)

    def validate_arguments(self, name: str, arguments: dict[str, object]) -> None:
        ToolRegistry(self.tools).validate_arguments(name, arguments)

    @property
    def hooks(self):
        return self.plugin_resolution.hooks if self.plugin_resolution is not None else ()

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
                return current, AgentToolResult(
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
        if self.plugin_resolution is not None:
            await self.plugin_resolution.close()


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
        allowed_risks: set[str] | frozenset[str] | None = None,
        prompt_text: str = "",
        explicit_skill_ids: set[str] | frozenset[str] = frozenset(),
        plugin_resolution: PluginResolution | None = None,
    ) -> CapabilityResolution:
        registered_tools = list(self._tools.values())
        if plugin_resolution is not None:
            registered_tools.extend(plugin_resolution.tools)
        names: set[str] = set()
        for tool in registered_tools:
            if tool.definition.name in names:
                raise ValueError(f"duplicate tool: {tool.definition.name}")
            names.add(tool.definition.name)
        selected: list[AgentTool] = []
        decisions: dict[str, str] = {}
        denied: dict[str, str] = {}
        for tool in registered_tools:
            definition = tool.definition
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
            if definition.exposure == "hidden":
                denied[definition.name] = "tool is hidden"
                decisions[definition.name] = "denied:hidden"
                continue
            selected.append(tool)
            decisions[definition.name] = "allowed"

        if enabled_tools is not None:
            unknown_tools = set(enabled_tools) - names
            for name in sorted(unknown_tools):
                denied[name] = "tool is not registered"
                decisions[name] = "denied:unknown"

        snapshot_definitions = tuple(
            ToolDefinition(
                name=tool.definition.name,
                description=tool.definition.description,
                input_schema=deepcopy(tool.definition.input_schema),
                risk=tool.definition.risk,
                source=tool.definition.source,
                version=tool.definition.version,
                exposure=tool.definition.exposure,
                approval=tool.definition.approval,
                timeout_seconds=tool.definition.timeout_seconds,
                output_limit=tool.definition.output_limit,
            )
            for tool in selected
        )
        skill_resolution: SkillResolution = self.skills.resolve(
            prompt=prompt_text,
            explicit_ids=explicit_skill_ids,
            available_tools={tool.definition.name for tool in registered_tools},
            enabled_tools=set(tool.definition.name for tool in selected),
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
        )
        return CapabilityResolution(
            snapshot=snapshot,
            tools=tuple(selected),
            denied_tools=MappingProxyType(dict(denied)),
            plugin_resolution=plugin_resolution,
        )

    async def resolve_async(
        self,
        *,
        role_id: str,
        session_key: str,
        run_id: str,
        enabled_tools: set[str] | frozenset[str] | None = None,
        allowed_risks: set[str] | frozenset[str] | None = None,
        prompt_text: str = "",
        explicit_skill_ids: set[str] | frozenset[str] = frozenset(),
        enabled_plugin_ids: set[str] | frozenset[str] | None = None,
        granted_capabilities: set[str] | frozenset[str] = frozenset(),
        host_services: Mapping[str, object] = MappingProxyType({}),
        plugin_dir: str = "",
    ) -> CapabilityResolution:
        plugin_resolution = await self.plugins.load(
            enabled_ids=enabled_plugin_ids,
            granted_capabilities=granted_capabilities,
            host_services=host_services,
            plugin_dir=plugin_dir,
        )
        try:
            return self.resolve(
                role_id=role_id,
                session_key=session_key,
                run_id=run_id,
                enabled_tools=enabled_tools,
                allowed_risks=allowed_risks,
                prompt_text=prompt_text,
                explicit_skill_ids=explicit_skill_ids,
                plugin_resolution=plugin_resolution,
            )
        except BaseException:
            try:
                await plugin_resolution.close()
            except Exception:
                pass
            raise
