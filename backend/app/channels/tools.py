from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import cast
from pathlib import Path
from fastapi import HTTPException
import httpx
from copy import deepcopy
from dataclasses import replace

from .host import ChannelHost
from .models import DeliveryStatus
from .directory import DirectoryError, DirectoryQuery, supported_kinds
from .base import ChannelAdapterError
from ..chat_attachments import prepare_output_attachment
from ..agent_runtime.tools import AgentToolResult, ToolContext, ToolDefinition, ToolRegistry


class ChannelDirectoryAccess:
    """Bind directory observations to the real caller across platform waits."""
    def __init__(self, *, authorized: Callable[[ToolContext], bool] | None = None,
                 source_scope: Callable[[ToolContext], str] | None = None):
        self.authorized, self.source_scope = authorized, source_scope

    def allowed(self, context: ToolContext) -> bool:
        return (not context.signal.is_cancelled() and (self.authorized is None or self.authorized(context))
                and not context.signal.is_cancelled())

    def scope(self, context: ToolContext) -> str:
        if not self.allowed(context):
            raise PermissionError("directory_source_revoked")
        return context.session_key + ":" + (self.source_scope(context) if self.source_scope else "")


class ChannelAccountListTool:
    definition = ToolDefinition(
        "account_list",
        "查看当前角色已配置的外部聊天账号及能力。",
        {"type": "object", "properties": {}, "additionalProperties": False},
        external_allowed=True,
    )

    def __init__(self, host: ChannelHost, *, access: ChannelDirectoryAccess | None = None) -> None:
        self.host = host
        self.access = access or ChannelDirectoryAccess()

    def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del arguments, on_update
        if not self.access.allowed(context):
            return AgentToolResult("directory_source_revoked", is_error=True)
        return AgentToolResult(json.dumps(self.host.account_list(context.role_id), ensure_ascii=False))


class ChannelTargetsTool:
    definition = ToolDefinition(
        "account_targets",
        "按种类分页查询账号目录；省略kind取本地known观察缓存。member仅群资料，不授予发送目标；只有可发送的id可用于account_send。",
        {
            "type": "object",
            "properties": {"account_id": {"type": "string", "minLength": 1, "maxLength": 160},
                "kind": {"type": "string", "enum": ["known", "friends", "groups", "members", "member"]},
                "group_id": {"type": "string", "minLength": 1, "maxLength": 160},
                "member_id": {"type": "string", "minLength": 1, "maxLength": 160},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "cursor": {"type": "string", "maxLength": 512}},
            "required": ["account_id"],
            "additionalProperties": False,
        },
        external_allowed=True,
    )

    def __init__(self, host: ChannelHost, *, access: ChannelDirectoryAccess | None = None) -> None:
        self.host = host
        self.access = access or ChannelDirectoryAccess()

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        try:
            ToolRegistry.validate_definition(self.definition, arguments)
            query = DirectoryQuery(kind=cast(str, arguments.get("kind", "known")),
                group_id=cast(str, arguments.get("group_id", "")), member_id=cast(str, arguments.get("member_id", "")),
                limit=cast(int, arguments.get("limit", 25)), cursor=cast(str, arguments.get("cursor", "")))
            targets = await self.host.query_directory(context.role_id, str(arguments["account_id"]), query,
                scope=self.access.scope(context), authorized=lambda: self.access.allowed(context))
        except DirectoryError as error:
            account = self.host.store.get_account(str(arguments["account_id"]))
            return AgentToolResult(json.dumps({"error": error.code, "complete": False,
                "supportedKinds": supported_kinds(account.channel) if account and account.role_id == context.role_id else []}), is_error=True)
        except KeyError:
            return AgentToolResult('{"error":"account_not_found","complete":false}', is_error=True)
        except PermissionError:
            return AgentToolResult('{"error":"permission_revoked","complete":false}', is_error=True)
        except TimeoutError:
            return AgentToolResult('{"error":"timeout","complete":false}', is_error=True)
        except (ChannelAdapterError, httpx.HTTPError, OSError):
            return AgentToolResult('{"error":"platform_error","complete":false}', is_error=True)
        except ValueError:
            return AgentToolResult('{"error":"invalid_query_or_response","complete":false}', is_error=True)
        return AgentToolResult(json.dumps(targets, ensure_ascii=False))


class ChannelSendTool:
    definition = ToolDefinition(
        "account_send",
        "向 account_targets 返回的可达目标发送文本和角色附件。主人回合使用目录 target_id；外部回合只能将 target_id 设为 owner，可选择角色任一渠道账号联系该渠道已配对主人。",
        {
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "minLength": 1, "maxLength": 160},
                "target_id": {"type": "string", "minLength": 1, "maxLength": 512},
                "content": {"type": "string", "maxLength": 4000},
                "attachments": {
                    "type": "array",
                    "maxItems": 4,
                    "items": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                },
                "media": {"type": "array", "maxItems": 4,
                    "items": {"type": "string", "minLength": 1, "maxLength": 8192},
                    "description": "图片工作区路径、已有附件ID或公共HTTP(S) URL；仅外部回合时不能读取角色本地文件。"},
                "mention_ids": {"type": "array", "maxItems": 20, "items": {"type": "string", "minLength": 1}},
                "message_thread_id": {"type": "integer", "minimum": 1},
            },
            "required": ["target_id"],
            "additionalProperties": False,
        },
        risk="external",
        external_allowed=True,
    )

    def __init__(
        self,
        host: ChannelHost,
        *,
        is_owner: bool,
        source_thread_id: str | None,
        source_account_id: str | None = None,
        workspace: Callable[[], Path] | None = None,
        access: ChannelDirectoryAccess | None = None,
    ) -> None:
        self.host = host
        self.workspace = workspace
        self.is_owner = is_owner
        self.source_thread_id = source_thread_id
        self.source_account_id = source_account_id
        self.access = access or ChannelDirectoryAccess()
        if not is_owner:
            schema = deepcopy(type(self).definition.input_schema)
            schema["properties"].pop("mention_ids", None)
            schema["properties"].pop("message_thread_id", None)
            schema["properties"]["target_id"] = {"type": "string", "enum": ["owner"]}
            self.definition = replace(type(self).definition, input_schema=schema)

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        if not self.access.allowed(context):
            return AgentToolResult("channel_source_revoked", is_error=True)
        try:
            ToolRegistry.validate_definition(self.definition, arguments)
        except ValueError:
            return AgentToolResult("渠道消息参数无效。", is_error=True)
        content_value = arguments.get("content", "")
        attachment_values = arguments.get("attachments", [])
        media_values = arguments.get("media", [])
        mention_values = arguments.get("mention_ids", [])
        if not isinstance(mention_values, list) or any(not isinstance(item, str) for item in mention_values):
            return AgentToolResult("群成员提及参数无效。", is_error=True)
        if not isinstance(content_value, str) or not isinstance(attachment_values, list) or any(not isinstance(item, str) for item in attachment_values):
            return AgentToolResult("渠道消息参数无效。", is_error=True)
        if not isinstance(media_values, list) or any(not isinstance(item, str) for item in media_values):
            return AgentToolResult("图片参数无效。", is_error=True)
        if not content_value.strip() and not attachment_values and not media_values:
            return AgentToolResult("渠道消息需要文本或附件。", is_error=True)
        try:
            account_id = str(arguments.get("account_id") or (self.source_account_id if not self.is_owner else "") or "")
            target_id = str(arguments["target_id"])
            if not self.is_owner and target_id != "owner":
                return AgentToolResult("外部会话只能向已配对主人发送消息。", is_error=True)
            if self.is_owner and not account_id:
                return AgentToolResult("主人回合必须指定渠道账号。", is_error=True)
            if media_values and self.workspace is None:
                raise ValueError('media storage unavailable')
            prepared = [await prepare_output_attachment(self.workspace(), reference, kind='image',
                allow_local=self.is_owner, authorized=lambda: self.access.allowed(context))
                for reference in media_values]
            attachment_values = list(dict.fromkeys([*attachment_values, *(str(item['id']) for item in prepared)]))
            if len(attachment_values) > 4:
                raise ValueError('at most four attachments')
            result = await self.host.account_send(
                context.role_id,
                account_id,
                target_id,
                content_value,
                attachment_ids=tuple(attachment_values),
                **({'_prepared_attachment_ids': frozenset(str(item['id'])
                    for reference, item in zip(media_values, prepared) if reference.startswith(('http://', 'https://')))}
                    if not self.is_owner and prepared else {}),
                mention_ids=tuple(mention_values),
                message_thread_id=arguments.get("message_thread_id"),
                actor="owner" if self.is_owner else "external",
                source_thread_id=self.source_thread_id,
                source_session_key=context.session_key,
                source_run_id=context.run_id,
                source_tool_call_id=context.tool_call_id,
                source_kind="account_send",
                authorize_source=lambda: self.access.allowed(context),
                delivery_deadline=context.deadline,
            )
        except (KeyError, ValueError, RuntimeError, PermissionError, OSError, TimeoutError, HTTPException, httpx.HTTPError) as error:
            return AgentToolResult(f"渠道消息未发送：{type(error).__name__}", is_error=True)
        return AgentToolResult(
            json.dumps({
                "status": result.status.value,
                "targetId": result.target.id,
                "platformMessageId": result.platform_message_id,
                "attemptId": result.attempt_id,
                "error": result.error,
                **{key: result.raw[key] for key in ('parts', 'partialDelivery', 'deliveredContent', 'receiptKind') if key in result.raw},
            }, ensure_ascii=False),
            details={"status": result.status.value, "delivery": result.status is DeliveryStatus.SENT, "attemptId": result.attempt_id},
            is_error=result.status is not DeliveryStatus.SENT,
        )
