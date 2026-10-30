from __future__ import annotations

import json

from ..agent_runtime.tools import AgentToolResult, ToolDefinition, ToolRegistry
from .application import _owned_call


SCHEDULE_NAMES = frozenset({'schedule', 'list_schedules', 'cancel_schedule'})
SOFT_TOOLS = frozenset({'read_file', 'list_dir', 'web_search', 'web_fetch', 'write_file', 'edit_file', 'shell'})


class ScheduleTool:
    definition = ToolDefinition(name='schedule', description='按主人明确请求创建当前角色的定时任务或提醒。at指定绝对时间，after相对延迟（宿主按当前消息实际接收时间计算），every循环间隔或cron表达式，cron为显式cron扩展。instant到时推送固定文本；soft到时运行独立任务生成实时内容。创建成功不等于已经执行，不修改主人待办。',
        input_schema={'type': 'object', 'properties': {
            'tier': {'type': 'string', 'enum': ['instant', 'soft'], 'description': 'instant固定消息；soft到时运行AI任务'},
            'trigger': {'type': 'string', 'enum': ['at', 'after', 'every', 'cron'], 'description': '绝对时间、相对延迟、循环或显式cron'},
            'when': {'type': 'string', 'minLength': 1, 'maxLength': 200, 'description': 'at如14:30或2026-11-01T09:00；after如30s/5m/2h；every如1h或0 9 * * *；cron用5字段表达式'},
            'timezone': {'type': 'string', 'maxLength': 100, 'description': 'IANA时区，例如Asia/Shanghai；省略用Asia/Shanghai'},
            'message': {'type': 'string', 'minLength': 1, 'maxLength': 4000, 'description': 'instant必填，到时发送的固定文本'},
            'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 20000, 'description': 'soft必填，到时执行的完整任务及所需上下文'},
            'name': {'type': 'string', 'maxLength': 100, 'description': '计划名称，便于查询或按精确名称取消'},
            'count': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'description': '循环计划执行次数上限，可选'},
            'channel': {'type': 'string', 'maxLength': 50, 'description': '目标渠道原名；省略用当前来源，desktop为当前角色桌面对话。只能用宿主已知且允许的渠道'},
            'chat_id': {'type': 'string', 'maxLength': 200, 'description': '宿主目录中的明确目标或owner；省略沿当前来源。桌面只接受owner/user，不猜其他ID；来源权限仍由宿主核验'}},
            'required': ['tier', 'trigger', 'when'], 'additionalProperties': False}, risk='mutating', source='role', search_hint='cron timer 延时执行')

    def __init__(self, application, sessions, *, origin_resolver, authorized):
        self.application, self.sessions = application, sessions
        self.origin_resolver, self.authorized = origin_resolver, authorized
        self._bound = None

    def bind(self, capabilities, role, configuration):
        self._bound = (capabilities, role, configuration)

    def _freeze(self, context):
        if self._bound is None:
            raise PermissionError('task execution context is unavailable')
        capabilities, role, configuration = self._bound
        current = capabilities.current()
        if ((context.role_id, context.session_key, context.run_id) !=
                (current.snapshot.role_id, current.snapshot.session_key, current.snapshot.run_id)):
            raise PermissionError('schedule request binding is stale')
        frozen = {'role': role.model_dump(mode='json', exclude={'modelConfig'}),
            'configuration': configuration.model_dump(mode='json', exclude={'apiKey'}) if configuration else None,
            'toolNames': [tool.definition.name for tool in current.tools
                      if tool.definition.name in SOFT_TOOLS
                      and (definition := current.definition(tool.definition.name)) is not None
                      and definition.source in {'builtin', 'role'}]}
        if len(json.dumps(frozen)) > 200000:
            raise ValueError('schedule frozen tool budget exceeded')
        return frozen

    async def execute(self, arguments, context, on_update=None):
        del on_update
        try:
            ToolRegistry.validate_definition(self.definition, arguments)
            if context.signal.is_cancelled() or not self.authorized(context):
                raise PermissionError('schedule requires the actual owner source')
            origin = self.origin_resolver(context)
            user = self.sessions.user_message_for_run(context.run_id)
            if user is None or user.sessionKey != origin.session_key:
                raise PermissionError('actual inbound message is unavailable')
            received = user.metadata.get('hostReceivedAt')
            received_source = 'hostReceivedAt' if type(received) in {int, float} else 'createdAt_fallback'
            received = float(received) if received_source == 'hostReceivedAt' else user.createdAt.timestamp()
            result = self.application().create(origin, dict(arguments), received_at=received,
                received_source=received_source, frozen=self._freeze(context) if arguments['tier'] == 'soft' else {},
                admitted=lambda: not context.signal.is_cancelled() and self.authorized(context))
            return AgentToolResult(json.dumps(result, ensure_ascii=False), result)
        except (ValueError, KeyError, PermissionError) as error:
            return AgentToolResult(str(error), {'status': 'rejected', 'error': str(error)}, True)


class ScheduleManageTool:
    def __init__(self, name, application, *, origin_resolver, authorized):
        self.application, self.origin_resolver, self.authorized = application, origin_resolver, authorized
        self.definition = ToolDefinition(name=name, description='查看当前角色已有的定时提醒和计划，包括本角色其他会话的计划；只读，不代表任务已执行或消息已送达。' if name == 'list_schedules' else '按真实计划ID唯一前缀（至少8字符）或精确名称取消本来源有权取消的定时任务；先查询，不猜ID，重取消保留真实状态。',
            input_schema={'type': 'object', 'properties': {} if name == 'list_schedules' else {
                'id': {'type': 'string', 'minLength': 8, 'maxLength': 32, 'description': 'list_schedules返回的ID或唯一前缀；与name二选一'}, 'name': {'type': 'string', 'minLength': 1, 'maxLength': 100, 'description': '精确计划名称；与id二选一'}},
                'additionalProperties': False}, source='role', risk='read_only' if name == 'list_schedules' else 'mutating',
            external_allowed=name == 'list_schedules', search_hint='提醒列表 已有计划' if name == 'list_schedules' else '删除提醒 取消任务')

    async def execute(self, arguments, context, on_update=None):
        del on_update
        try:
            ToolRegistry.validate_definition(self.definition, arguments)
            if not self.authorized(context):
                raise PermissionError('schedule management source is unavailable or unauthorized')
            if self.definition.name == 'list_schedules':
                result = {'schedules': self.application().list_role(context.role_id)}
            else:
                origin = self.origin_resolver(context)
                result = await _owned_call(self.application().cancel,
                    origin, identifier=str(arguments.get('id', '')), name=str(arguments.get('name', '')))
            return AgentToolResult(json.dumps(result, ensure_ascii=False), result)
        except (ValueError, KeyError, PermissionError) as error:
            return AgentToolResult(str(error), {'status': 'rejected', 'error': str(error)}, True)
