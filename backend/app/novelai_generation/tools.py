from __future__ import annotations

import json
import sqlite3

from ..agent_runtime.tools import AgentToolResult, ToolDefinition
from ..agent_runtime.provider import CancellationToken
from ..agent_runtime.tools import ToolContext
from .context import capture_tool_context
from .parameters import GenerationRequest

_SCHEMA = GenerationRequest.model_json_schema()
_SCHEMA.pop('title', None)


class GenerateImageTool:
    definition = ToolDefinition(name='generate_image',
        description='Generate one approved NovelAI image from English tags, or an image attachment from this actual owner input. Uses finite paid-attempt allowance; unknown results are never retried automatically. Foreground only until background delivery is enabled.',
        input_schema=_SCHEMA, risk='external', source='builtin:novelai', exposure='direct',
        external_allowed=False, timeout_seconds=330, output_limit=4000)

    def __init__(self, application, role_id, *, is_owner, source_thread_id=None, source_account_id=None, frozen_role=None):
        self.application, self.role_id, self.is_owner = application, role_id, is_owner
        self.source_thread_id, self.source_account_id = source_thread_id, source_account_id
        self.service_revision = application.settings.public()['revision']
        role = frozen_role or application.roles.get(role_id)
        self.role_revision = application._role_revision(role) if role is not None else ''
        self.bound_run = None
        self.bound_source = None

    def bind_run(self, run_id, session_key):
        source = capture_tool_context(self.application.sessions, self.application.channels,
            ToolContext(self.role_id, session_key, run_id, CancellationToken(), 'host-freeze'),
            role_id=self.role_id, is_owner=self.is_owner, source_thread_id=self.source_thread_id,
            source_account_id=self.source_account_id)
        self.bound_run, self.bound_source = (run_id, session_key), source

    async def execute(self, arguments, context, on_update=None):
        del on_update
        try:
            if self.bound_run is not None and self.bound_run != (context.run_id, context.session_key):
                raise PermissionError('generation tool is bound to another host run')
            source = capture_tool_context(self.application.sessions, self.application.channels, context,
                role_id=self.role_id, is_owner=self.is_owner, source_thread_id=self.source_thread_id,
                source_account_id=self.source_account_id)
            if self.bound_source is not None and (source.channel_revision != self.bound_source.channel_revision
                    or source.channel_source != self.bound_source.channel_source):
                raise PermissionError('frozen generation source changed')
            result = await self.application.submit(source, dict(arguments), signal=context.signal,
                expected_revision=self.service_revision, expected_role_revision=self.role_revision)
            details = {key: result[key] for key in ('generationId', 'attemptId', 'status', 'attachments', 'taskId', 'errorCode', 'consumption')}
            details['parameters'] = {key: result['parameters'][key] for key in ('model', 'sampler', 'seed', 'width', 'height', 'steps')}
            return AgentToolResult(json.dumps(details, ensure_ascii=False), details=details,
                is_error=result['status'] != 'succeeded' or bool(result['errorCode']))
        except (ValueError, PermissionError, KeyError, OSError, RuntimeError, sqlite3.Error):
            return AgentToolResult('生图请求未完成；检查当前来源、配置、有限许可、参数与归属历史。已出网的请求可能计费，不会自动重试。',
                details={'status': 'rejected', 'errorCode': 'generation_not_available'}, is_error=True)
