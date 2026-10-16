"""An explicitly enabled role writes only its own desktop conversation."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

from .agent_runtime.tools import AgentToolResult, ToolContext, ToolDefinition, ToolRegistry
from .chat_attachments import public_attachment, resolve_attachment
from .session_store import SessionStore


class MessagePushTool:
    definition = ToolDefinition('message_push', '向当前角色自己的桌面对话推送文本、图片或文件；不发送到外部渠道。媒体使用已归属此角色的附件ID。', {
        'type': 'object', 'properties': {
            'channel': {'type': 'string', 'enum': ['desktop']},
            'chat_id': {'type': 'string', 'enum': ['owner', 'user']},
            'message': {'type': 'string', 'maxLength': 4000},
            'image': {'type': 'string', 'pattern': '^[0-9a-f]{32}$'},
            'file': {'type': 'string', 'pattern': '^[0-9a-f]{32}$'},
            'attachments': {'type': 'array', 'maxItems': 4, 'items': {'type': 'string', 'pattern': '^[0-9a-f]{32}$'}},
        }, 'required': ['channel', 'chat_id'], 'additionalProperties': False,
    }, risk='mutating', external_allowed=False)

    def __init__(self, sessions: SessionStore, workspace: Callable[[], Path], *, role_id: str, authorized: Callable[[ToolContext], bool]):
        # Construction and directory preview never open a database or channel.
        self.sessions, self.workspace, self.role_id, self.authorized = sessions, workspace, role_id, authorized

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        try:
            ToolRegistry.validate_definition(self.definition, arguments)
            if not self._allowed(context):
                raise PermissionError('desktop push unavailable')
            message = str(arguments.get('message', ''))
            ids = list(cast(list[str], arguments.get('attachments', [])))
            ids.extend(cast(str, arguments[key]) for key in ('image', 'file') if key in arguments)
            ids = list(dict.fromkeys(ids))
            if (not message.strip() and not ids) or len(ids) > 4:
                raise ValueError('provide text or at most four role attachments')
            attachments = self._attachments(ids, arguments)
            pending = asyncio.create_task(asyncio.to_thread(self.sessions.append_desktop_push_once,
                self.role_id, context.run_id, str(context.tool_call_id), message,
                attachments=attachments, authorized=lambda: self._attachments(ids, arguments) == attachments
                and self._allowed(context)))
            try:
                saved = await asyncio.shield(pending)
            except asyncio.CancelledError:
                context.signal.cancel()
                while not pending.done():
                    try:
                        await asyncio.shield(pending)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                # Consume an authorization rejection after releasing the write
                # transaction, then preserve the caller's cancellation.
                if not pending.cancelled():
                    pending.exception()
                raise asyncio.CancelledError
            receipt = {'status': 'sent', 'messageId': saved.id}
            return AgentToolResult(json.dumps(receipt, ensure_ascii=False), receipt)
        except (KeyError, ValueError, OSError, PermissionError):
            if arguments.get('channel') != 'desktop':
                return AgentToolResult('message_push 只投递当前角色桌面对话；对外消息请使用 account_send。', {'status': 'denied'}, is_error=True)
            return AgentToolResult('桌面消息未写入：权限或附件不可用，参数不符合要求。', {'status': 'denied'}, is_error=True)

    def _attachments(self, ids: list[str], arguments: Mapping[str, object]) -> list[dict[str, object]]:
        attachments = []
        workspace = self.workspace()
        directory = workspace / 'attachments'
        if ids and (directory.is_symlink() or directory.is_junction()):
            raise ValueError('role attachment directory was redirected')
        for identifier in ids:
            item = resolve_attachment(workspace, identifier)
            if item is None or item.get('id') != identifier or not Path(str(item['path'])).is_file():
                raise ValueError('role attachment unavailable')
            if item.get('kind') not in {'image', 'file'} or any(
                    item.get('kind') != kind for kind in ('image', 'file') if arguments.get(kind) == identifier):
                raise ValueError('attachment kind does not match')
            attachments.append(public_attachment(item))
        return attachments

    def _allowed(self, context: ToolContext) -> bool:
        return bool(context.role_id == self.role_id and context.run_id and context.tool_call_id
                    and not context.signal.is_cancelled() and self.authorized(context)
                    and not context.signal.is_cancelled())
