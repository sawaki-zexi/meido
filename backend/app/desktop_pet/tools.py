import json

from ..agent_runtime.tools import AgentToolResult, ToolDefinition, ToolRegistry


def register_desktop_pet_plugin(registry):
    from ..agent_runtime.plugins import PluginManifest, PluginContribution
    manifest = PluginManifest(plugin_id='desktop_pet', version='1.0.0',
        requested_capabilities=('desktop.pet',), declared_tools=('pet_action',))
    def factory(context):
        context.require('desktop.pet')
        # Native windows belong to the application, never to a model run.
        return PluginContribution()
    registry.register(manifest, factory)


class PetActionTool:
    def __init__(self, application, role_id, *, authorized):
        self.application, self.role_id, self.authorized = application, role_id, authorized
        role, package, _ = application.store.binding(role_id)
        self.generation = role['generation']
        actions = sorted(package['actions']) if package else []
        self.definition = ToolDefinition('pet_action', '移动当前角色桌宠或播放动作包声明的动作。每回合最多一次，间隔至少3秒。',
            {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['move', 'play']},
                'target': {'type': 'string', 'enum': ['top_left', 'top_right', 'center', 'bottom_left', 'bottom_right']},
                'animation': {'type': 'string', 'enum': ['', 'idle', 'run']},
                'name': {'type': 'string', 'enum': actions}}, 'required': ['action'], 'additionalProperties': False},
            risk='mutating', source='plugin:desktop_pet', timeout_seconds=20, output_limit=4096,
            search_hint='桌宠 动作 移动 动画 pet move play')

    async def execute(self, arguments, context, on_update=None):
        ToolRegistry.validate_definition(self.definition, arguments)
        try:
            result = await self.application.action(self.role_id, self.generation, dict(arguments), context,
                authorized=self.authorized)
            return AgentToolResult(json.dumps(result, ensure_ascii=False), details=result,
                is_error=result.get('type') in {'unknown', 'error'})
        except (ValueError, OSError, RuntimeError) as error:
            return AgentToolResult(str(error)[:256], is_error=True)
