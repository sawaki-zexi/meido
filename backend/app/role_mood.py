"""Bounded local mood presentation; never grants media-send or desktop-pet authority."""
import json

from .agent_runtime.tools import AgentToolResult, ToolDefinition, ToolRegistry


class RoleMoodTool:
    def __init__(self, store, role, *, authorized):
        self.store, self.role_id, self.authorized = store, role.id, authorized
        self.bindings = dict(role.moodIllustrationBindings)
        self.bound_run = None
        self.definition = ToolDefinition('set_role_mood',
            '回复前，根据当前对话选择匹配的已配置角色心情，切换本地会话中的心情插画。心情未改变时可不调用。只改变呈现，不发送图片、不控制桌宠。',
            {'type': 'object', 'properties': {'mood': {'type': 'string', 'enum': list(self.bindings)}},
             'required': ['mood'], 'additionalProperties': False}, risk='mutating',
            search_hint='角色 心情 表情 插画 mood expression', output_limit=1024)

    def bind_run(self, run_id, session_key):
        if not run_id or session_key != f'role:{self.role_id}' or not self.bindings:
            raise PermissionError('角色心情只允许当前桌面主人回合')
        binding = (run_id, session_key)
        if self.bound_run is not None and self.bound_run != binding:
            raise PermissionError('角色心情工具不可重新绑定其他回合')
        self.bound_run = binding

    async def execute(self, arguments, context, on_update=None):
        try:
            ToolRegistry.validate_definition(self.definition, arguments)
            def allowed():
                current = self.store.get(self.role_id)
                return bool(not context.signal.cancelled and context.role_id == self.role_id
                    and self.bound_run == (context.run_id, context.session_key)
                    and current and current.moodIllustrationBindings == self.bindings
                    and self.authorized(context))
            role = self.store.set_current_mood(self.role_id, arguments['mood'], authorized=allowed)
            result = {'mood': role.currentMood, 'illustrationId': role.moodIllustrationBindings[role.currentMood]}
            return AgentToolResult(json.dumps(result, ensure_ascii=False), details=result)
        except (PermissionError, ValueError, KeyError, OSError, RuntimeError) as error:
            return AgentToolResult(str(error)[:256], is_error=True)
