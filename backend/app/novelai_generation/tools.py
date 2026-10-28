from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

from ..agent_runtime.tools import AgentToolResult, ToolDefinition
from ..agent_runtime.provider import CancellationToken
from ..agent_runtime.tools import ToolContext
from .context import capture_tool_context
from .parameters import GenerationRequest
from .availability import GenerationUnavailable
from .failures import GenerationBudgetExceeded

_SCHEMA = GenerationRequest.model_json_schema()
_SCHEMA.pop('title', None)
# Per-image preference overrides are explicit local-owner actions, not model grants.
for _field in ('qualityToggle', 'ucPreset'):
    _SCHEMA['properties'].pop(_field, None)
# Manual/API requests keep ``mode`` strict, but ordinary chat expression
# calls intentionally omit it so the host can select the safe txt2img path.
# Keep the complete property set for backwards-compatible owner-directed
# calls while making only the expression tags mandatory to the model.
_SCHEMA['required'] = ['prompt']
# The runtime applies the owner-controlled service default only to explicit
# UI/API requests. Chat expression calls omit this field and are fixed to one
# image below, so a static Pydantic default would be misleading to a model.
_SCHEMA['properties']['samples'].pop('default', None)
_SCHEMA['properties']['samples']['description'] = '生成张数（1–4）；普通聊天省略时由宿主固定为一张。'
_SCHEMA['properties']['prompt']['description'] = '逗号分隔的完整英文 NovelAI tags：主体外貌（发色、发型、眼睛、服饰等已知特征）以及此刻的表情、动作、场景与构图。宿主不会自动补入角色外观。'


class GenerateImageTool:
    definition = ToolDefinition(name='generate_image',
        description=('Generate one approved NovelAI expression image during the current conversation when an image adds value to the reply. '
            'Decide yourself whether to call it; do not call it every turn and do not ask the owner to fill image parameters. '
            'Provide complete comma-separated English NovelAI tags, including the subject\'s known appearance, clothing, emotion, pose, scene and framing. '
            'For self-portraits, preserve your appearance from the role profile/SELF and inspect_role_image references; never invent it from an image filename. '
            'The host supplies model, sampler, size, seed, limits, delivery and a one-image default, but does not add appearance tags. '
            'Explicit advanced fields remain accepted for owner-directed parameter reuse within the same allowlist. Uses one finite paid attempt; unknown results are never retried.'),
        input_schema=_SCHEMA, risk='external', source='builtin:novelai', exposure='direct',
        external_allowed=True, timeout_seconds=330, output_limit=4000, search_hint='生图 生成图片 NovelAI 立绘 场景图')

    def __init__(self, application, role_id, *, is_owner, source_thread_id=None, source_account_id=None, frozen_role=None):
        self.application, self.role_id, self.is_owner = application, role_id, is_owner
        self.source_thread_id, self.source_account_id = source_thread_id, source_account_id
        self.service_revision = application.settings.public()['revision']
        role = frozen_role or application.roles.get(role_id)
        availability = application.availability(role)
        self.definition = replace(self.definition, description=(self.definition.description
            + '\n当前 NovelAI 状态：' + availability['message']
            + ' 已接入不等于当前可以生成；不可用时说明具体原因，不要声称没有生图工具。'))
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
            self.application.scenes.note_manual(source)
            # The normal chat path supplies only prompt tags. Keep the
            # owner-directed advanced contract for compatibility, while
            # applying safe host defaults when the model omits those fields.
            request = dict(arguments)
            request.setdefault('mode', 'txt2img')
            request.setdefault('samples', 1)
            request.setdefault('background', False)
            # This tool is the chat-expression capability.  Do not let a
            # model relabel it as a manual or scene request through an
            # optional compatibility field.
            request['intent'] = 'chat_expression'
            result = await self.application.submit(source, request, signal=context.signal,
                expected_revision=self.service_revision, expected_role_revision=self.role_revision)
            details = {key: result[key] for key in ('generationId', 'attemptId', 'status', 'attachments', 'taskId', 'errorCode', 'errorMessage', 'consumption')}
            details['parameters'] = {key: result['parameters'][key] for key in ('model', 'sampler', 'seed', 'samples', 'width', 'height', 'steps')}
            accepted = result['backgroundAccepted']
            if accepted:
                details['status'] = 'accepted'
            return AgentToolResult(json.dumps(details, ensure_ascii=False), details=details,
                is_error=not accepted and (result['status'] != 'succeeded' or bool(result['errorCode'])))
        except (GenerationUnavailable, GenerationBudgetExceeded) as error:
            return AgentToolResult(str(error),
                details={'status': 'rejected', 'errorCode': error.code, 'errorMessage': str(error)}, is_error=True)
        except (ValueError, PermissionError, KeyError, OSError, RuntimeError, sqlite3.Error):
            return AgentToolResult('生图请求未完成；请检查当前来源、配置、参数与归属历史。设置改变后需刷新工具，已出网的请求可能计费，不会自动重试。',
                details={'status': 'rejected', 'errorCode': 'generation_not_available'}, is_error=True)
