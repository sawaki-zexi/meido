from __future__ import annotations

import json
from dataclasses import asdict

from ..agent_runtime.tools import AgentToolResult, ToolDefinition, ToolRegistry
from .application import _owned_call


SCHEDULE_NAMES = frozenset({'schedule', 'list_schedules', 'cancel_schedule'})
SOFT_TOOLS = frozenset({'read_file', 'list_dir', 'web_search', 'web_fetch', 'write_file', 'edit_file', 'shell'})


class ScheduleTool:
    definition = ToolDefinition(name='schedule', description='Create a role-owned time task. instant sends fixed text; soft runs an independent bounded task at its time. after uses the actual host message reception time. every accepts duration or cron; explicit cron is also supported. Only use on the owner request.',
        input_schema={'type': 'object', 'properties': {
            'tier': {'type': 'string', 'enum': ['instant', 'soft']},
            'trigger': {'type': 'string', 'enum': ['at', 'after', 'every', 'cron']},
            'when': {'type': 'string', 'minLength': 1, 'maxLength': 200},
            'timezone': {'type': 'string', 'maxLength': 100},
            'message': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
            'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 20000},
            'name': {'type': 'string', 'maxLength': 100},
            'count': {'type': 'integer', 'minimum': 1, 'maximum': 1000},
            'channel': {'type': 'string', 'maxLength': 50},
            'chat_id': {'type': 'string', 'maxLength': 200}},
            'required': ['tier', 'trigger', 'when'], 'additionalProperties': False}, risk='mutating', source='role', search_hint='cron timer 延时执行')

    def __init__(self, application, sessions, *, origin_resolver, authorized):
        self.application, self.sessions = application, sessions
        self.origin_resolver, self.authorized = origin_resolver, authorized
        self._bound = None

    def bind(self, capabilities, role, configuration):
        self._bound = (capabilities, role, configuration)

    def _freeze(self):
        if self._bound is None:
            raise PermissionError('frozen execution capabilities are unavailable')
        capabilities, role, configuration = self._bound
        current = capabilities.current()
        frozen = {'role': role.model_dump(mode='json', exclude={'modelConfig'}),
            'configuration': configuration.model_dump(mode='json', exclude={'apiKey'}) if configuration else None,
            'tools': [asdict(definition) for tool in current.tools
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
                received_source=received_source, frozen=self._freeze() if arguments['tier'] == 'soft' else {},
                admitted=lambda: not context.signal.is_cancelled() and self.authorized(context))
            return AgentToolResult(json.dumps(result, ensure_ascii=False), result)
        except (ValueError, KeyError, PermissionError) as error:
            return AgentToolResult(str(error), {'status': 'rejected', 'error': str(error)}, True)


class ScheduleManageTool:
    def __init__(self, name, application, *, origin_resolver, authorized):
        self.application, self.origin_resolver, self.authorized = application, origin_resolver, authorized
        self.definition = ToolDefinition(name=name, description='List time tasks in this exact role and original source.' if name == 'list_schedules' else 'Cancel owned time tasks by unique id prefix (at least 8 characters) or exact name; repeated cancellation is safe.',
            input_schema={'type': 'object', 'properties': {} if name == 'list_schedules' else {
                'id': {'type': 'string', 'minLength': 8, 'maxLength': 32}, 'name': {'type': 'string', 'minLength': 1, 'maxLength': 100}},
                'additionalProperties': False}, source='role', risk='read_only' if name == 'list_schedules' else 'mutating', search_hint='提醒列表 已有计划' if name == 'list_schedules' else '删除提醒 取消任务')

    async def execute(self, arguments, context, on_update=None):
        del on_update
        try:
            ToolRegistry.validate_definition(self.definition, arguments)
            if not self.authorized(context):
                raise PermissionError('schedule management requires the actual owner source')
            origin = self.origin_resolver(context)
            result = {'schedules': self.application().list(origin)} if self.definition.name == 'list_schedules' else await _owned_call(self.application().cancel,
                origin, identifier=str(arguments.get('id', '')), name=str(arguments.get('name', '')))
            return AgentToolResult(json.dumps(result, ensure_ascii=False), result)
        except (ValueError, KeyError, PermissionError) as error:
            return AgentToolResult(str(error), {'status': 'rejected', 'error': str(error)}, True)
