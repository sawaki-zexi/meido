import json
from pathlib import Path

from ..agent_runtime.tools import ToolDefinition, ToolRegistry


_SPECS = json.loads(Path(__file__).with_name("tools.json").read_text(encoding="utf-8"))
COMPUTER_USE_TOOL_NAMES = frozenset("computer_" + spec["name"] for spec in _SPECS)


class ComputerTool:
    def __init__(self, host, spec, role_id=""):
        self.host, self.spec, self.role_id = host, spec, role_id
        self.generation = host.generation(role_id) if role_id else None
        self.definition = ToolDefinition("computer_" + spec["name"], "[Computer Use] " + spec["description"], spec["inputSchema"],
            risk="external", source="plugin:computer_use", version="0.28.2-meido.1", timeout_seconds=35, output_limit=65536, search_hint='电脑 桌面 窗口 控件 截图 点击 输入 快捷键 computer')

    async def execute(self, arguments, context, on_update=None):
        from .host import _error
        if self.role_id and context.role_id != self.role_id:
            return _error("permission_denied")
        ToolRegistry.validate_definition(self.definition, arguments)
        text = arguments.get("text", "")
        if (len(text) > 4000 or len(text.encode("utf-8")) > 16384
                or len(text) * arguments.get("delay_ms", 0) > 20000
                or len(arguments.get("query", "")) > 256):
            return _error("input_budget_exceeded")
        if len(arguments.get("keys", [])) > 5 or len(arguments.get("key", "")) > 32:
            return _error("input_budget_exceeded")
        return await self.host.call(self.spec["name"], dict(arguments), context, self.generation)


def load_computer_tools(host, role_id=""):
    return tuple(ComputerTool(host, spec, role_id) for spec in _SPECS)


def register_computer_use_plugin(registry):
    from ..agent_runtime.plugins import PluginManifest, PluginContribution
    manifest = PluginManifest(plugin_id="computer_use", version="0.28.2-meido.1", requested_capabilities=("desktop.control",),
        declared_tools=tuple(sorted(COMPUTER_USE_TOOL_NAMES)))
    def factory(context):
        host = context.require("desktop.control")
        context.add_cleanup(lambda: host.close_run(context.role_id, context.run_id))
        return PluginContribution()
    registry.register(manifest, factory)
