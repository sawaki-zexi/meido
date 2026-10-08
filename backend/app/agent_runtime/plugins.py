from __future__ import annotations

import asyncio
from copy import deepcopy
import inspect
import math
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Awaitable, Callable, Iterable, Mapping, Protocol, cast

from .skills import SkillDescriptor
from .tools import AgentTool, ToolContext


_PLUGIN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_TRUST_LEVELS = frozenset({"builtin", "project", "installed", "external"})
_IMPLICIT_ACTIVATION_TRUST_LEVELS = frozenset({"installed", "external"})
_TRUST_RANK = {"external": 0, "installed": 1, "project": 2, "builtin": 3}
_SOURCE_TRUST = {"builtin": "builtin", "project": "project", "installed": "installed", "external": "external"}
_DEFAULT_RUNTIME_API = "meido.agent_runtime.v1"
KNOWN_CAPABILITIES = frozenset({
    "runtime.hook",
    "runtime.tool",
    "memory.read",
    "memory.write",
})
HookMode = str
Cleanup = Callable[[], object | Awaitable[object]]


class PluginError(RuntimeError):
    """Base error for plugin loading and host service access."""


class CapabilityNotGranted(PluginError):
    """The plugin requested a capability that was not granted."""


class HostServiceUnavailable(PluginError):
    """A granted host capability has no service in this host instance."""


class PluginSetupError(PluginError):
    """A plugin failed while building its contributions."""


@dataclass(frozen=True, slots=True)
class PluginManifest:
    plugin_id: str
    version: str
    requested_capabilities: tuple[str, ...] = ()
    source: str = "builtin"
    trust_level: str = "builtin"
    manifest_hash: str | None = None
    runtime_api: str = _DEFAULT_RUNTIME_API
    config_schema: Mapping[str, object] | None = None
    config_defaults: Mapping[str, object] | None = None
    declared_tools: tuple[str, ...] = ()
    declared_skills: tuple[str, ...] = ()
    lifecycle_contributions: tuple[str, ...] = ()
    resource_dir: str | None = None
    generation: str | None = None

    def __post_init__(self) -> None:
        if not _PLUGIN_ID.fullmatch(self.plugin_id):
            raise ValueError("plugin_id must contain only lowercase letters, digits, '.', '_' or '-'")
        if not self.version.strip():
            raise ValueError("plugin version is required")
        if self.trust_level not in _TRUST_LEVELS:
            raise ValueError(f"unsupported plugin trust level: {self.trust_level}")
        source_trust = _SOURCE_TRUST.get(self.source)
        if source_trust is None:
            raise ValueError(f"unsupported plugin source: {self.source}")
        if _TRUST_RANK[self.trust_level] > _TRUST_RANK[source_trust]:
            raise ValueError("plugin trust level exceeds source trust")
        if not self.runtime_api.strip():
            raise ValueError("plugin runtime_api is required")
        for field_name, values in (
            ("declared_tools", self.declared_tools),
            ("declared_skills", self.declared_skills),
            ("lifecycle_contributions", self.lifecycle_contributions),
        ):
            if len(set(values)) != len(values) or any(not item.strip() for item in values):
                raise ValueError(f"plugin {field_name} must contain unique non-empty IDs")
        if self.resource_dir is not None and not self.resource_dir.strip():
            raise ValueError("plugin resource_dir must not be empty")
        if self.config_schema is not None and not isinstance(self.config_schema, dict):
            raise ValueError("plugin config_schema must be an object")
        if self.config_defaults is not None and not isinstance(self.config_defaults, dict):
            raise ValueError("plugin config_defaults must be an object")
        if self.config_schema is not None:
            object.__setattr__(self, "config_schema", cast(Mapping[str, object], _freeze_config(self.config_schema)))
        if self.config_defaults is not None:
            object.__setattr__(self, "config_defaults", cast(Mapping[str, object], _freeze_config(self.config_defaults)))
        if len(set(self.requested_capabilities)) != len(self.requested_capabilities):
            raise ValueError("duplicate requested capability")
        unknown = set(self.requested_capabilities) - KNOWN_CAPABILITIES
        if unknown:
            raise ValueError(f"unknown capability: {', '.join(sorted(unknown))}")

    def to_dict(self, *, include_config: bool = True) -> dict[str, object]:
        return {
            "id": self.plugin_id,
            "version": self.version,
            "source": self.source,
            "trustLevel": self.trust_level,
            "manifestHash": self.manifest_hash,
            "requestedCapabilities": list(self.requested_capabilities),
            "runtimeApi": self.runtime_api,
            "configSchema": _thaw_config(self.config_schema) if include_config else None,
            "configDefaults": _thaw_config(self.config_defaults) if include_config else None,
            "declaredTools": list(self.declared_tools),
            "declaredSkills": list(self.declared_skills),
            "lifecycleContributions": list(self.lifecycle_contributions),
            "resourceDir": self.resource_dir,
            "generation": self.generation or self.manifest_hash or f"{self.plugin_id}@{self.version}",
        }


def _freeze_config(value: object) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("plugin configuration keys must be strings")
        return MappingProxyType({key: _freeze_config(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_config(item) for item in value)
    return value


def _thaw_config(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_config(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_config(item) for item in value]
    return deepcopy(value)


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True, slots=True)
class HookDefinition:
    hook_id: str
    mode: HookMode
    tool_names: tuple[str, ...] = ()
    priority: int = 0
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.hook_id.strip():
            raise ValueError("hook_id is required")
        if self.mode not in {"observe", "transform", "deny"}:
            raise ValueError(f"unsupported hook mode: {self.mode}")
        if self.timeout_seconds is not None:
            if (
                isinstance(self.timeout_seconds, bool)
                or not isinstance(self.timeout_seconds, (int, float))
                or not _is_finite_number(self.timeout_seconds)
                or self.timeout_seconds <= 0
            ):
                raise ValueError("hook timeout must be positive and finite")


@dataclass(frozen=True, slots=True)
class HookOutcome:
    arguments: dict[str, object] | None = None
    allowed: bool = True
    reason: str | None = None

    @classmethod
    def deny(cls, reason: str) -> HookOutcome:
        return cls(allowed=False, reason=reason)


@dataclass(frozen=True, slots=True)
class HookContext:
    tool_name: str
    arguments: Mapping[str, object]
    tool_context: ToolContext


class AgentToolHook(Protocol):
    definition: HookDefinition

    def before_tool(
        self,
        context: HookContext,
    ) -> HookOutcome | Awaitable[HookOutcome] | None: ...


@dataclass(frozen=True, slots=True)
class PluginContribution:
    tools: tuple[AgentTool, ...] = ()
    hooks: tuple[AgentToolHook, ...] = ()
    skills: tuple[SkillDescriptor, ...] = ()


@dataclass(slots=True)
class PluginContext:
    plugin_id: str
    plugin_dir: str
    grants: frozenset[str]
    host_services: Mapping[str, object]
    _cleanups: list[Cleanup] = field(default_factory=list)
    _closed: bool = False

    def require(self, capability: str) -> object:
        if capability not in self.grants:
            raise CapabilityNotGranted(f"capability not granted: {capability}")
        try:
            return self.host_services[capability]
        except KeyError as error:
            raise HostServiceUnavailable(f"host service unavailable: {capability}") from error

    def add_cleanup(self, cleanup: Cleanup) -> None:
        if self._closed:
            raise RuntimeError("plugin context is closed")
        self._cleanups.append(cleanup)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[str] = []
        cancellation: asyncio.CancelledError | None = None
        while self._cleanups:
            cleanup = self._cleanups.pop()
            try:
                result = cleanup()
                if inspect.isawaitable(result):
                    cleanup_task = asyncio.ensure_future(result)
                    while True:
                        try:
                            await asyncio.shield(cleanup_task)
                            break
                        except asyncio.CancelledError as error:
                            if cancellation is None:
                                cancellation = error
                            if cleanup_task.done():
                                break
                        except Exception as error:  # cleanup must not prevent remaining effects
                            errors.append(str(error))
                            break
                    if cleanup_task.cancelled() and cancellation is None:
                        cancellation = asyncio.CancelledError()
            except asyncio.CancelledError as error:
                if cancellation is None:
                    cancellation = error
            except Exception as error:  # cleanup must not prevent remaining effects
                errors.append(str(error))
        if cancellation is not None:
            raise cancellation
        if errors:
            raise PluginError("plugin cleanup failed: " + "; ".join(errors))


async def _close_plugin_contexts(
    contexts: Iterable[PluginContext],
    *,
    suppress_errors: bool = False,
) -> None:
    errors: list[str] = []
    cancellation: asyncio.CancelledError | None = None
    for context in contexts:
        try:
            await context.close()
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
        except PluginError as error:
            errors.append(str(error))
    if suppress_errors:
        return
    if cancellation is not None:
        raise cancellation
    if errors:
        raise PluginError("; ".join(errors))


PluginFactory = Callable[[PluginContext], PluginContribution | Awaitable[PluginContribution]]


@dataclass(frozen=True, slots=True)
class PluginDescriptor:
    manifest: PluginManifest
    status: str
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        # Config schemas/defaults may contain secret-bearing defaults. They
        # remain available to the in-process loader but never enter a run
        # snapshot or audit record.
        result = self.manifest.to_dict(include_config=False)
        result.update({"status": self.status, "error": self.error})
        return result


@dataclass(slots=True)
class LoadedPlugin:
    descriptor: PluginDescriptor
    context: PluginContext
    contribution: PluginContribution


@dataclass(slots=True)
class PluginResolution:
    loaded: tuple[LoadedPlugin, ...]
    diagnostics: tuple[MappingProxyType, ...]

    @property
    def tools(self) -> tuple[AgentTool, ...]:
        return tuple(tool for plugin in self.loaded for tool in plugin.contribution.tools)

    @property
    def hooks(self) -> tuple[AgentToolHook, ...]:
        hooks = [hook for plugin in self.loaded for hook in plugin.contribution.hooks]
        return tuple(sorted(hooks, key=lambda item: (item.definition.priority, item.definition.hook_id)))

    @property
    def skills(self) -> tuple[SkillDescriptor, ...]:
        return tuple(skill for plugin in self.loaded for skill in plugin.contribution.skills)

    @property
    def descriptors(self) -> tuple[PluginDescriptor, ...]:
        return tuple(plugin.descriptor for plugin in self.loaded)

    async def close(self) -> None:
        await _close_plugin_contexts(plugin.context for plugin in reversed(self.loaded))

    async def reload(
        self,
        registry: PluginRegistry,
        *,
        enabled_ids: set[str] | frozenset[str] | None = None,
        granted_capabilities: set[str] | frozenset[str] = frozenset(),
        host_services: Mapping[str, object] = MappingProxyType({}),
        plugin_dir: str = "",
    ) -> PluginResolution:
        await self.close()
        return await registry.load(
            enabled_ids=enabled_ids,
            granted_capabilities=granted_capabilities,
            host_services=host_services,
            plugin_dir=plugin_dir,
        )


@dataclass(frozen=True, slots=True)
class _Registration:
    manifest: PluginManifest
    factory: PluginFactory


class PluginRegistry:
    """Static, in-process plugin registry with explicit host capability grants."""

    def __init__(self, registrations: Iterable[tuple[PluginManifest, PluginFactory]] = ()) -> None:
        self._registrations: dict[str, _Registration] = {}
        for manifest, factory in registrations:
            self.register(manifest, factory)

    def register(self, manifest: PluginManifest, factory: PluginFactory) -> None:
        if manifest.plugin_id in self._registrations:
            raise ValueError(f"duplicate plugin: {manifest.plugin_id}")
        self._registrations[manifest.plugin_id] = _Registration(manifest, factory)

    async def load(
        self,
        *,
        enabled_ids: set[str] | frozenset[str] | None = None,
        granted_capabilities: set[str] | frozenset[str] = frozenset(),
        host_services: Mapping[str, object] = MappingProxyType({}),
        plugin_dir: str = "",
    ) -> PluginResolution:
        unknown_grants = set(granted_capabilities) - KNOWN_CAPABILITIES
        loaded: list[LoadedPlugin] = []
        diagnostics: list[MappingProxyType] = []
        if unknown_grants:
            diagnostics.append(MappingProxyType({
                "status": "denied",
                "error": f"unknown capability grant: {', '.join(sorted(unknown_grants))}",
            }))
        hook_ids: set[str] = set()
        if enabled_ids is not None:
            unknown_plugins = set(enabled_ids) - set(self._registrations)
            diagnostics.extend(
                MappingProxyType({
                    "pluginId": plugin_id,
                    "status": "unknown",
                    "error": "plugin is not registered",
                })
                for plugin_id in sorted(unknown_plugins)
            )
        for registration in self._registrations.values():
            manifest = registration.manifest
            if enabled_ids is not None and manifest.plugin_id not in enabled_ids:
                continue
            if (
                enabled_ids is None
                and manifest.trust_level in _IMPLICIT_ACTIVATION_TRUST_LEVELS
            ):
                diagnostics.append(MappingProxyType({
                    "pluginId": manifest.plugin_id,
                    "status": "denied",
                    "error": "plugin requires explicit activation for its trust level",
                }))
                continue
            missing = set(manifest.requested_capabilities) - set(granted_capabilities)
            if missing:
                diagnostics.append(MappingProxyType({
                    "pluginId": manifest.plugin_id,
                    "status": "denied",
                    "error": f"capability not granted: {', '.join(sorted(missing))}",
                }))
                continue
            context = PluginContext(
                plugin_id=manifest.plugin_id,
                plugin_dir=plugin_dir,
                grants=frozenset(manifest.requested_capabilities),
                host_services=MappingProxyType(dict(host_services)),
            )
            try:
                contribution = registration.factory(context)
                if inspect.isawaitable(contribution):
                    contribution = await contribution
                if not isinstance(contribution, PluginContribution):
                    raise PluginSetupError("plugin factory returned invalid contribution")
                _validate_contribution(contribution, manifest=manifest)
                duplicate_hooks = {
                    hook.definition.hook_id
                    for hook in contribution.hooks
                } & hook_ids
                if duplicate_hooks:
                    raise PluginSetupError(
                        f"duplicate plugin hook: {', '.join(sorted(duplicate_hooks))}"
                    )
                hook_ids.update(hook.definition.hook_id for hook in contribution.hooks)
            except asyncio.CancelledError:
                await _close_plugin_contexts(
                    [context, *(plugin.context for plugin in reversed(loaded))],
                    suppress_errors=True,
                )
                raise
            except Exception as error:
                try:
                    await context.close()
                except asyncio.CancelledError:
                    await _close_plugin_contexts(
                        (plugin.context for plugin in reversed(loaded)),
                        suppress_errors=True,
                    )
                    raise
                except Exception as cleanup_error:
                    error = PluginSetupError(f"{error}; rollback cleanup failed: {cleanup_error}")
                diagnostics.append(MappingProxyType({
                    "pluginId": manifest.plugin_id,
                    "status": "failed",
                    "error": str(error),
                }))
                continue
            except BaseException:
                await _close_plugin_contexts(
                    [context, *(plugin.context for plugin in reversed(loaded))],
                    suppress_errors=True,
                )
                raise
            descriptor = PluginDescriptor(manifest, status="loaded")
            loaded.append(LoadedPlugin(descriptor, context, contribution))
        return PluginResolution(tuple(loaded), tuple(diagnostics))


def _validate_contribution(contribution: PluginContribution, *, manifest: PluginManifest) -> None:
    undeclared_tools = {
        tool.definition.name for tool in contribution.tools
    } - set(manifest.declared_tools)
    if undeclared_tools:
        raise PluginSetupError(
            f"plugin contributed undeclared tool: {', '.join(sorted(undeclared_tools))}"
        )
    names: set[str] = set()
    expected_tool_source = f"plugin:{manifest.plugin_id}"
    for tool in contribution.tools:
        name = tool.definition.name
        if name in names:
            raise PluginSetupError(f"duplicate plugin tool: {name}")
        if tool.definition.source != expected_tool_source:
            raise PluginSetupError(
                f"plugin tool source must be {expected_tool_source}: {name}"
            )
        names.add(name)
    skill_ids: set[str] = set()
    for skill in contribution.skills:
        if skill.skill_id in skill_ids:
            raise PluginSetupError(f"duplicate plugin skill: {skill.skill_id}")
        if skill.skill_id not in manifest.declared_skills:
            raise PluginSetupError(f"plugin contributed undeclared skill: {skill.skill_id}")
        if skill.source != manifest.source:
            raise PluginSetupError(
                f"plugin skill source does not match manifest source: {skill.skill_id}"
            )
        if _TRUST_RANK[skill.trust_level] > _TRUST_RANK[manifest.trust_level]:
            raise PluginSetupError(
                f"plugin skill trust level exceeds plugin trust: {skill.skill_id}"
            )
        skill_ids.add(skill.skill_id)
    hook_ids: set[str] = set()
    for hook in contribution.hooks:
        hook_id = hook.definition.hook_id
        if hook_id in hook_ids:
            raise PluginSetupError(f"duplicate plugin hook: {hook_id}")
        if hook_id not in manifest.lifecycle_contributions:
            raise PluginSetupError(f"plugin contributed undeclared hook: {hook_id}")
        hook_ids.add(hook_id)


async def run_hook(hook: AgentToolHook, context: HookContext) -> HookOutcome:
    result = hook.before_tool(context)
    if inspect.isawaitable(result):
        result = await result
    if result is None:
        return HookOutcome()
    if not isinstance(result, HookOutcome):
        raise PluginSetupError(f"hook returned invalid outcome: {hook.definition.hook_id}")
    if hook.definition.mode == "observe":
        return HookOutcome()
    if hook.definition.mode == "deny":
        return HookOutcome.deny(result.reason or "tool call denied by hook") if not result.allowed else HookOutcome()
    return result
