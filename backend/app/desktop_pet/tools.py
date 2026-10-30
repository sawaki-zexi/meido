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
        self.definition = ToolDefinition('pet_action', '操控当前绑定桌宠：移动到受限语义位置或播放动作包声明的动作。仅桌面主人会话可用。每回合最多一次，间隔至少3秒；成功以真实执行回执为准。',
            {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['move', 'play'], 'description': 'move移动位置；play播放当前包动作'},
                'target': {'type': 'string', 'enum': ['top_left', 'top_right', 'center', 'bottom_left', 'bottom_right'], 'description': 'move的语义位置目标'},
                'animation': {'type': 'string', 'enum': ['', 'idle', 'run'], 'description': 'move的移动表现，可省略'},
                'name': {'type': 'string', 'enum': actions, 'description': 'play的动作名，只能使用当前包枚举值'}}, 'required': ['action'], 'additionalProperties': False},
            risk='external', source='plugin:desktop_pet', version='1.0.0.' + (self.generation or 'off'), timeout_seconds=20, output_limit=4096,
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
