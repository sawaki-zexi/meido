from __future__ import annotations

import asyncio
import json

from ..agent_runtime.tools import AgentToolResult, ToolDefinition, ToolRegistry


CODING_TOOL_NAMES = frozenset({'herdr_start_task', 'herdr_list_tasks', 'herdr_get_task', 'herdr_continue_task', 'herdr_cancel_task'})


async def _owned_thread(function, *arguments, **kwargs):
    pending = asyncio.create_task(asyncio.to_thread(function, *arguments, **kwargs))
    interrupted = False
    while True:
        try:
            result = await asyncio.shield(pending)
            break
        except asyncio.CancelledError:
            interrupted = True
            if pending.done():
                result = pending.result()
                break
    if interrupted:
        raise asyncio.CancelledError
    return result


class CodingTool:
    def __init__(self, name, application, *, role_id, origin_resolver, authorized, access):
        self.application, self.role_id = application, role_id
        self.origin_resolver, self.authorized = origin_resolver, authorized
        self.access = access
        task_id = {'type': 'string', 'minLength': 1, 'maxLength': 100}
        requirement = {'type': 'string', 'minLength': 1, 'maxLength': 20000}
        if name == 'herdr_start_task':
            properties = {'repository': {'type': 'string', 'minLength': 1, 'maxLength': 2000},
                          'requirement': requirement, 'agent': {'type': 'string', 'enum': ['codex', 'claude']}}
            required = ['repository', 'requirement']
            description = 'Start an owned Herdr coding task in an explicitly granted trusted repository. Returns stable task/attempt IDs; the background attempt owns its server, worktree and 600-second budget. Local edit and fixed validation only; publishing requires separate owner approval.'
        elif name == 'herdr_list_tasks':
            properties, required = {}, []
            description = 'List Herdr coding tasks belonging to this exact role and original source. No other role, sender or rebound account inherits them.'
        elif name == 'herdr_continue_task':
            properties, required = {'task_id': task_id, 'requirement': requirement}, ['task_id', 'requirement']
            description = 'Continue the same owned Herdr task in its exact verified native session and worktree. Creates one new bounded attempt; missing session or incomplete cleanup blocks continuation.'
        else:
            properties, required = {'task_id': task_id}, ['task_id']
            description = 'Inspect bounded current Herdr task, native/cleanup/validation and delivery facts.' if name == 'herdr_get_task' else 'Cancel the current owned Herdr attempt and confirm local resource cleanup. Cancellation does not roll back file edits, model consumption or uncertain remote effects.'
        self.definition = ToolDefinition(name=name, description=description,
            input_schema={'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False},
            source='builtin:coding', exposure='deferred', risk='external' if name in {'herdr_start_task', 'herdr_continue_task', 'herdr_cancel_task'} else 'read_only',
            external_allowed=False, timeout_seconds=30, output_limit=12000, search_hint='写代码 开发 修bug coding agent codex claude herdr PR' if name == 'herdr_start_task' else 'Herdr 任务 状态 进度 继续 取消 coding agent')

    async def execute(self, arguments, context, on_update=None):
        del on_update
        try:
            ToolRegistry.validate_definition(self.definition, arguments)
            if context.role_id != self.role_id or not self.access.get('enabled') or context.signal.is_cancelled() or not self.authorized(context):
                raise PermissionError('coding requires the actual authorized owner source')
            origin = self.origin_resolver(context)
            if origin.role_id != self.role_id or origin.session_key != context.session_key:
                raise PermissionError('coding origin differs from its host context')
            application = self.application()
            if self.access.get('generation') is not None and application.generation(self.role_id) != self.access['generation']:
                raise PermissionError('coding authority was revoked after this run began')
            name = self.definition.name
            if name == 'herdr_start_task':
                result = await _owned_thread(application.start_task, origin, repository=arguments['repository'],
                    requirement=arguments['requirement'], agent=arguments.get('agent'),
                    admitted=lambda: not context.signal.is_cancelled() and self.authorized(context), frozen_access=self.access)
            elif name == 'herdr_continue_task':
                result = await _owned_thread(application.continue_task, arguments['task_id'], origin,
                    requirement=arguments['requirement'], admitted=lambda: not context.signal.is_cancelled() and self.authorized(context), frozen_access=self.access)
            elif name == 'herdr_list_tasks':
                records = await _owned_thread(application.list_tasks, origin)
                result = {'tasks': [{**item, 'summary': item.get('summary', '')[:200]} for item in records]}
            elif name == 'herdr_get_task':
                result = await _owned_thread(application.inspect, arguments['task_id'], origin)
            else:
                result = await _owned_thread(application.cancel_task, arguments['task_id'], origin)
            return AgentToolResult(json.dumps(result, ensure_ascii=False), result)
        except (ValueError, KeyError, PermissionError, OSError) as error:
            return AgentToolResult('编程任务操作被拒绝：' + type(error).__name__,
                                   {'status': 'rejected', 'error': type(error).__name__}, True)


def build_coding_tools(application, *, role_id, origin_resolver, authorized, access):
    return tuple(CodingTool(name, application, role_id=role_id, origin_resolver=origin_resolver, authorized=authorized, access=access)
                 for name in sorted(CODING_TOOL_NAMES))
