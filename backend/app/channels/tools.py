from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace

from .host import ChannelHost
from .models import DeliveryStatus
from ..agent_runtime.tools import AgentToolResult, ToolContext, ToolDefinition


class ChannelAccountListTool:
    definition = ToolDefinition(
        "account_list",
        "查看当前角色已配置的外部聊天账号及能力。",
        {"type": "object", "properties": {}, "additionalProperties": False},
        external_allowed=False,
    )

    def __init__(self, host: ChannelHost) -> None:
        self.host = host

    def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del arguments, on_update
        return AgentToolResult(json.dumps(self.host.account_list(context.role_id), ensure_ascii=False))


class ChannelTargetsTool:
    definition = ToolDefinition(
        "account_targets",
        "列出指定渠道账号实际可达的联系人、群或群成员；只能向返回的目标 ID 投递。",
        {
            "type": "object",
            "properties": {"account_id": {"type": "string", "minLength": 1, "maxLength": 160}},
            "required": ["account_id"],
            "additionalProperties": False,
        },
        external_allowed=False,
    )

    def __init__(self, host: ChannelHost) -> None:
        self.host = host

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        try:
            targets = await self.host.account_targets(context.role_id, str(arguments["account_id"]))
        except KeyError:
            return AgentToolResult("该渠道账号不属于当前角色。", is_error=True)
        return AgentToolResult(json.dumps(targets, ensure_ascii=False))


class ChannelSendTool:
    definition = ToolDefinition(
        "account_send",
        "向 account_targets 返回的可达目标发送文本和角色附件。主人回合使用目录 target_id；外部回合只能将 target_id 设为 owner，以联系已配对主人。",
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
    ) -> None:
        self.host = host
        self.is_owner = is_owner
        self.source_thread_id = source_thread_id
        self.source_account_id = source_account_id
        if not is_owner:
            schema = deepcopy(type(self).definition.input_schema)
            schema["properties"].pop("account_id", None)
            schema["properties"].pop("mention_ids", None)
            schema["properties"].pop("message_thread_id", None)
            schema["properties"]["target_id"] = {"type": "string", "enum": ["owner"]}
            self.definition = replace(type(self).definition, input_schema=schema)

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        content_value = arguments.get("content", "")
        attachment_values = arguments.get("attachments", [])
        mention_values = arguments.get("mention_ids", [])
        if not isinstance(mention_values, list) or any(not isinstance(item, str) for item in mention_values):
            return AgentToolResult("群成员提及参数无效。", is_error=True)
        if not isinstance(content_value, str) or not isinstance(attachment_values, list) or any(not isinstance(item, str) for item in attachment_values):
            return AgentToolResult("渠道消息参数无效。", is_error=True)
        if not content_value.strip() and not attachment_values:
            return AgentToolResult("渠道消息需要文本或附件。", is_error=True)
        try:
            account_id = str(arguments.get("account_id", "")) if self.is_owner else str(self.source_account_id or "")
            target_id = str(arguments["target_id"])
            if not self.is_owner and target_id != "owner":
                return AgentToolResult("外部会话只能向已配对主人发送消息。", is_error=True)
            if self.is_owner and not account_id:
                return AgentToolResult("主人回合必须指定渠道账号。", is_error=True)
            result = await self.host.account_send(
                context.role_id,
                account_id,
                target_id,
                content_value,
                attachment_ids=tuple(attachment_values),
                mention_ids=tuple(mention_values),
                message_thread_id=arguments.get("message_thread_id"),
                actor="owner" if self.is_owner else "external",
                source_thread_id=self.source_thread_id,
                source_session_key=context.session_key,
                source_run_id=context.run_id,
                source_tool_call_id=context.tool_call_id,
                source_kind="account_send",
            )
        except (KeyError, ValueError, RuntimeError) as error:
            return AgentToolResult(f"渠道消息未发送：{type(error).__name__}", is_error=True)
        return AgentToolResult(
            json.dumps({
                "status": result.status.value,
                "targetId": result.target.id,
                "platformMessageId": result.platform_message_id,
                "attemptId": result.attempt_id,
                "error": result.error,
            }, ensure_ascii=False),
            details={"status": result.status.value, "delivery": result.status is DeliveryStatus.SENT, "attemptId": result.attempt_id},
            is_error=result.status is not DeliveryStatus.SENT,
        )
