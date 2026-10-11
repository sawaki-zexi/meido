from ..agent_runtime.plugins import PluginContext, PluginContribution, PluginManifest
from .tool import BROWSER_USE_TOOL_NAMES, BrowserSessions, load_tools

BROWSER_USE_MANIFEST = PluginManifest(
    plugin_id="browser_use",
    version="0.38.2",
    requested_capabilities=("browser.session",),
    source="builtin",
    trust_level="builtin",
    declared_tools=BROWSER_USE_TOOL_NAMES,
)


def browser_use_plugin_factory(context: PluginContext) -> PluginContribution:
    sessions = context.require("browser.session")
    if not isinstance(sessions, BrowserSessions):
        raise TypeError("browser.session host service has an invalid type")
    if context.role_id and context.run_id:
        context.add_cleanup(lambda: sessions.close_run(context.role_id, context.run_id))
    return PluginContribution(tools=load_tools(sessions))


def register_browser_use_plugin(registry) -> None:
    registry.register(BROWSER_USE_MANIFEST, browser_use_plugin_factory)
