from __future__ import annotations

import inspect
import math
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Awaitable, Callable, Iterable, Mapping, Protocol

from .tools import AgentTool, ToolContext


_PLUGIN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
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

    def __post_init__(self) -> None:
        if not _PLUGIN_ID.fullmatch(self.plugin_id):
            raise ValueError("plugin_id must contain only lowercase letters, digits, '.', '_' or '-'")
        if not self.version.strip():
            raise ValueError("plugin version is required")
        if len(set(self.requested_capabilities)) != len(self.requested_capabilities):
            raise ValueError("duplicate requested capability")
        unknown = set(self.requested_capabilities) - KNOWN_CAPABILITIES
        if unknown:
            raise ValueError(f"unknown capability: {', '.join(sorted(unknown))}")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.plugin_id,
            "version": self.version,
            "source": self.source,
            "trustLevel": self.trust_level,
            "manifestHash": self.manifest_hash,
            "requestedCapabilities": list(self.requested_capabilities),
        }


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
                or not math.isfinite(self.timeout_seconds)
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
        while self._cleanups:
            cleanup = self._cleanups.pop()
            try:
                result = cleanup()
                if inspect.isawaitable(result):
                    await result
            except Exception as error:  # cleanup must not prevent remaining effects
                errors.append(str(error))
        if errors:
            raise PluginError("plugin cleanup failed: " + "; ".join(errors))


PluginFactory = Callable[[PluginContext], PluginContribution | Awaitable[PluginContribution]]


@dataclass(frozen=True, slots=True)
class PluginDescriptor:
    manifest: PluginManifest
    status: str
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        result = self.manifest.to_dict()
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
    def descriptors(self) -> tuple[PluginDescriptor, ...]:
        return tuple(plugin.descriptor for plugin in self.loaded)

    async def close(self) -> None:
        errors: list[str] = []
        for plugin in reversed(self.loaded):
            try:
                await plugin.context.close()
            except PluginError as error:
                errors.append(str(error))
        if errors:
            raise PluginError("; ".join(errors))

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
                host_services=host_services,
            )
            try:
                contribution = registration.factory(context)
                if inspect.isawaitable(contribution):
                    contribution = await contribution
                if not isinstance(contribution, PluginContribution):
                    raise PluginSetupError("plugin factory returned invalid contribution")
                _validate_contribution(contribution)
                duplicate_hooks = {
                    hook.definition.hook_id
                    for hook in contribution.hooks
                } & hook_ids
                if duplicate_hooks:
                    raise PluginSetupError(
                        f"duplicate plugin hook: {', '.join(sorted(duplicate_hooks))}"
                    )
                hook_ids.update(hook.definition.hook_id for hook in contribution.hooks)
            except Exception as error:
                try:
                    await context.close()
                except Exception as cleanup_error:
                    error = PluginSetupError(f"{error}; rollback cleanup failed: {cleanup_error}")
                diagnostics.append(MappingProxyType({
                    "pluginId": manifest.plugin_id,
                    "status": "failed",
                    "error": str(error),
                }))
                continue
            descriptor = PluginDescriptor(manifest, status="loaded")
            loaded.append(LoadedPlugin(descriptor, context, contribution))
        return PluginResolution(tuple(loaded), tuple(diagnostics))


def _validate_contribution(contribution: PluginContribution) -> None:
    names: set[str] = set()
    for tool in contribution.tools:
        name = tool.definition.name
        if name in names:
            raise PluginSetupError(f"duplicate plugin tool: {name}")
        names.add(name)
    hook_ids: set[str] = set()
    for hook in contribution.hooks:
        hook_id = hook.definition.hook_id
        if hook_id in hook_ids:
            raise PluginSetupError(f"duplicate plugin hook: {hook_id}")
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
