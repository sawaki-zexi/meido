"""Read-only capability inspection using the runtime's policy evaluator."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .capabilities import CapabilityRegistry
from .plugins import PluginManifest
from .tools import ToolDefinition


@dataclass(frozen=True)
class RuntimeCapabilityPolicy:
    enabled_tools: frozenset[str]
    enabled_plugins: frozenset[str]
    granted_capabilities: frozenset[str]
    provided_capabilities: frozenset[str]


@dataclass
class _DeclaredTool:
    definition: ToolDefinition

    def execute(self, *args, **kwargs):
        raise RuntimeError("catalog declarations cannot execute tools")


def tool_catalog(
    *, role_id: str, session_key: str, definitions: Sequence[ToolDefinition],
    policy: RuntimeCapabilityPolicy, manifests: Sequence[PluginManifest],
    dependencies: Mapping[str, dict[str, str]], reasons: Mapping[str, str],
) -> dict[str, object]:
    plugins: list[dict[str, object]] = []
    plugin_denials: dict[str, str] = {}
    plugin_by_id = {manifest.plugin_id: manifest for manifest in manifests}
    for manifest in manifests:
        enabled = manifest.plugin_id in policy.enabled_plugins
        missing = set(manifest.requested_capabilities) - policy.granted_capabilities
        unavailable = set(manifest.requested_capabilities) - policy.provided_capabilities
        reason = (
            "插件未启用" if not enabled else
            "插件缺少授权：" + ", ".join(sorted(missing)) if missing else
            "宿主服务未提供：" + ", ".join(sorted(unavailable)) if unavailable else ""
        )
        if not enabled or missing:
            plugin_denials[manifest.plugin_id] = reason
        plugins.append({
            "id": manifest.plugin_id, "version": manifest.version, "source": manifest.source,
            "enabled": enabled, "authorized": enabled and not missing,
            "requestedCapabilities": list(manifest.requested_capabilities),
            "reason": reason or "仅检查声明；未执行插件初始化",
        })
    enabled_tools = set(policy.enabled_tools)
    for definition in definitions:
        plugin_id = definition.source.removeprefix("plugin:") if definition.source.startswith("plugin:") else ""
        if plugin_id and (plugin_id in plugin_denials or plugin_id not in plugin_by_id):
            enabled_tools.discard(definition.name)
    registry = CapabilityRegistry([_DeclaredTool(item) for item in definitions])
    resolution = registry.resolve(
        role_id=role_id, session_key=session_key, run_id="catalog-preview", enabled_tools=enabled_tools,
        allowed_risks={"read_only", "mutating", "external"},
    )
    admission = registry.resolve(role_id=role_id, session_key=session_key, run_id="catalog-admission", enabled_tools=enabled_tools,
                                 activated_tools={item.name for item in definitions}, allowed_risks={"read_only", "mutating", "external"})
    tools: list[dict[str, object]] = []
    known = {item.name for item in definitions}
    for definition in definitions:
        plugin_id = definition.source.removeprefix("plugin:") if definition.source.startswith("plugin:") else ""
        tool_reason = reasons.get(definition.name) or plugin_denials.get(plugin_id) or resolution.denial_reason(definition.name)
        tools.append({
            "name": definition.name, "description": definition.description,
            "source": definition.source, "version": definition.version, "risk": definition.risk,
            "exposure": definition.exposure, "registered": True,
            "enabled": definition.name in policy.enabled_tools,
            "authorized": admission.get(definition.name) is not None,
            "modelVisible": resolution.get(definition.name) is not None,
            "dependency": dependencies.get(definition.name, {"status": "unchecked", "reason": "未执行工具；依赖将在调用时验证"}),
            "reason": tool_reason or "本次配置允许在桌面对话中提供此工具",
        })
    for manifest in manifests:
        for name in manifest.declared_tools:
            if name in known:
                continue
            known.add(name)
            enabled = name in policy.enabled_tools
            denial = plugin_denials.get(manifest.plugin_id)
            tools.append({
                "name": name, "description": "插件声明的工具；具体接口由运行时加载后确认",
                "source": f"plugin:{manifest.plugin_id}", "version": manifest.version,
                "risk": None, "exposure": None, "registered": True, "enabled": enabled,
                "authorized": enabled and not denial, "modelVisible": None if enabled and not denial else False,
                "dependency": {"status": "unchecked", "reason": "未执行插件初始化；实际接口和依赖待运行确认"},
                "reason": denial or ("接口和模型可见性待运行确认" if enabled else "工具未启用"),
            })
    for name in sorted(policy.enabled_tools - known):
        tools.append({
            "name": name, "description": "配置引用了未注册工具", "source": "unknown", "version": None,
            "risk": None, "exposure": None, "registered": False, "enabled": True,
            "authorized": False, "modelVisible": False, "reason": "工具未注册",
            "dependency": {"status": "unavailable", "reason": "当前宿主没有此工具"},
        })
    return {"roleId": role_id, "sessionKey": session_key, "preview": True, "tools": tools, "plugins": plugins}
