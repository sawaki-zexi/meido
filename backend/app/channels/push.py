"""Owner-bound pushes and opt-in reminder destinations.

The destination remains a host-owned pairing fact.  Neither a model nor a
reminder can turn a raw platform ID into authorization to contact a stranger.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .host import ChannelHost
from .models import DeliveryStatus, Target
from ..agent_runtime.tools import AgentToolResult, ToolContext, ToolDefinition

if TYPE_CHECKING:
    from ..multichannel import MultichannelStore


class ChannelPushService:
    def __init__(self, store: "MultichannelStore", host: ChannelHost) -> None:
        self.store, self.host = store, host
        with store._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS channel_push_targets (
                    role_id TEXT NOT NULL, account_id TEXT NOT NULL,
                    target_id TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(role_id,account_id)
                );
                CREATE TABLE IF NOT EXISTS channel_push_events (
                    role_id TEXT NOT NULL, event_id TEXT NOT NULL,
                    account_id TEXT NOT NULL, target_id TEXT NOT NULL,
                    status TEXT NOT NULL, attempt_id TEXT, error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(role_id,event_id,account_id)
                );
            """)
            db.execute("UPDATE channel_push_events SET status='uncertain',error='process_interrupted' WHERE status='pending'")

    def list_targets(self, role_id: str) -> list[dict[str, object]]:
        with self.store._connect() as db:
            selected = {str(row[0]): str(row[1]) for row in db.execute(
                "SELECT account_id,target_id FROM channel_push_targets WHERE role_id=?", (role_id,),
            )}
        result: list[dict[str, object]] = []
        for account in self.store.list_role_accounts(role_id):
            targets = self.store.owner_targets(role_id, account.account_id)
            for target in targets:
                result.append({
                    "accountId": account.account_id, "channel": account.channel,
                    "accountName": account.display_name, "targetId": target.id,
                    "chatId": target.chat_id,
                    "displayName": target.display_name or target.chat_id,
                    "enabled": selected.get(account.account_id) == target.id,
                    "available": self.store.account_effectively_enabled(account),
                })
        return result

    def configure(self, role_id: str, account_id: str, target_id: str | None) -> None:
        account = self.store.get_account(account_id)
        if account is None or account.role_id != role_id:
            raise KeyError(account_id)
        if target_id is not None and not any(
            target.id == target_id for target in self.store.owner_targets(role_id, account_id)
        ):
            raise ValueError("主动提醒只能绑定该账号已配对的主人私聊")
        with self.store._connect() as db:
            if target_id is None:
                db.execute("DELETE FROM channel_push_targets WHERE role_id=? AND account_id=?", (role_id, account_id))
            else:
                db.execute(
                    "INSERT INTO channel_push_targets VALUES(?,?,?,?) ON CONFLICT(role_id,account_id) DO UPDATE SET target_id=excluded.target_id,updated_at=excluded.updated_at",
                    (role_id, account_id, target_id, datetime.now(timezone.utc).isoformat()),
                )

    def clear_account(self, account_id: str) -> None:
        with self.store._connect() as db:
            db.execute("DELETE FROM channel_push_targets WHERE account_id=?", (account_id,))
            db.execute("DELETE FROM channel_push_events WHERE account_id=?", (account_id,))

    def clear_role(self, role_id: str) -> None:
        with self.store._connect() as db:
            db.execute("DELETE FROM channel_push_targets WHERE role_id=?", (role_id,))
            db.execute("DELETE FROM channel_push_events WHERE role_id=?", (role_id,))

    def owner_destination(self, role_id: str, channel: str, requested: str) -> tuple[str, Target]:
        account = self.store.get_role_account(role_id, channel)
        if account is None:
            raise ValueError("角色未配置此渠道账号")
        targets = self.store.owner_targets(role_id, account.account_id)
        target = next((item for item in targets if requested in {item.id, item.chat_id}), None)
        if requested in {"owner", "user"}:
            with self.store._connect() as db:
                row = db.execute("SELECT target_id FROM channel_push_targets WHERE role_id=? AND account_id=?", (role_id, account.account_id)).fetchone()
            target = next((item for item in targets if row and item.id == str(row[0])), None)
            if target is None and len(targets) == 1:
                target = targets[0]
        if target is None:
            raise ValueError("目标必须是当前角色渠道中已配对的主人私聊；多个主人目标时请指定目录 ID")
        return account.account_id, target

    async def deliver_reminder(self, role_id: str, event_id: str, content: str) -> list[dict[str, object]]:
        """Deliver once to explicitly opted-in targets; uncertain sends never retry."""
        outcomes: list[dict[str, object]] = []
        for destination in self.list_targets(role_id):
            if not destination["enabled"]:
                continue
            account_id, target_id = str(destination["accountId"]), str(destination["targetId"])
            now = datetime.now(timezone.utc).isoformat()
            with self.store._connect() as db:
                claimed = db.execute(
                    "INSERT OR IGNORE INTO channel_push_events(role_id,event_id,account_id,target_id,status,updated_at) VALUES(?,?,?,?,'pending',?)",
                    (role_id, event_id, account_id, target_id, now),
                ).rowcount
            if not claimed:
                continue
            attempt_id, error, status = None, None, "failed"
            try:
                # Recheck the pairing at execution, after the event was claimed.
                if not any(item.id == target_id for item in self.store.owner_targets(role_id, account_id)):
                    raise ValueError("主人投递目标已解绑")
                receipt = await self.host.account_send(
                    role_id, account_id, target_id, content, actor="push",
                    source_session_key=f"role:{role_id}", source_kind=f"todo_reminder:{event_id}",
                )
                attempt_id, error, status = receipt.attempt_id, receipt.error, receipt.status.value
            except Exception as failure:
                error = type(failure).__name__
            with self.store._connect() as db:
                db.execute(
                    "UPDATE channel_push_events SET status=?,attempt_id=?,error=?,updated_at=? WHERE role_id=? AND event_id=? AND account_id=?",
                    (status, attempt_id, error, datetime.now(timezone.utc).isoformat(), role_id, event_id, account_id),
                )
            outcomes.append({"accountId": account_id, "status": status, "attemptId": attempt_id, "error": error})
        return outcomes


class MessagePushTool:
    definition = ToolDefinition(
        "message_push", "向当前角色已配对的主人主动发送消息、图片或文件。channel 使用渠道原名；chat_id 使用 owner/user 或主人私聊目录 ID。不能向未配对目标发送。",
        {"type": "object", "properties": {
            "channel": {"type": "string", "enum": ["desktop", "telegram", "qq", "qqbot", "feishu"]},
            "chat_id": {"type": "string", "minLength": 1, "maxLength": 512},
            "message": {"type": "string", "maxLength": 4000},
            "image": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            "file": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            "attachments": {"type": "array", "maxItems": 4, "items": {"type": "string", "pattern": "^[0-9a-f]{32}$"}},
        }, "required": ["channel", "chat_id"], "additionalProperties": False},
        risk="external", external_allowed=True,
    )

    def __init__(self, service: ChannelPushService, *, is_owner: bool, source_thread_id: str | None = None, source_account_id: str | None = None) -> None:
        self.service, self.is_owner = service, is_owner
        self.source_thread_id, self.source_account_id = source_thread_id, source_account_id
        if not is_owner:
            schema = deepcopy(type(self).definition.input_schema)
            account = service.store.get_account(str(source_account_id or ""))
            schema["properties"]["channel"] = {"type": "string", "enum": [account.channel] if account else ["telegram", "qq", "qqbot", "feishu"]}
            schema["properties"]["chat_id"] = {"type": "string", "enum": ["owner", "user"]}
            self.definition = replace(type(self).definition, input_schema=schema)

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        channel, requested = str(arguments.get("channel", "")), str(arguments.get("chat_id", ""))
        message = arguments.get("message", "")
        attachments = list(arguments.get("attachments", [])) if isinstance(arguments.get("attachments", []), list) else []
        attachments.extend(str(arguments[name]) for name in ("image", "file") if arguments.get(name))
        attachments = list(dict.fromkeys(attachments))
        if not isinstance(message, str) or (not message.strip() and not attachments):
            return AgentToolResult("message、image 或 file 至少提供一个。", is_error=True)
        try:
            if channel == "desktop":
                if not self.is_owner or requested not in {"owner", "user"} or attachments:
                    raise ValueError("桌面主动消息只允许主人回合发送文本到 owner/user")
                item = self.service.store.sessions.append_proactive_message_once(
                    f"role:{context.role_id}", message,
                    event_id=f"message_push:{context.run_id}:{context.tool_call_id}",
                    metadata={"source": "message_push", "sourceRunId": context.run_id},
                )
                self.service.store.publish(context.role_id, {
                    "type": "message", "roleId": context.role_id,
                    "threadId": f"desktop:{context.role_id}", "messageId": item.id,
                })
                return AgentToolResult(json.dumps({"status": "sent", "messageId": item.id}, ensure_ascii=False))
            account_id, target = self.service.owner_destination(context.role_id, channel, requested)
            if not self.is_owner and (requested not in {"owner", "user"} or account_id != self.source_account_id):
                raise ValueError("外部会话只能通过当前账号联系已配对主人")
            receipt = await self.service.host.account_send(
                context.role_id, account_id, target.id if self.is_owner else "owner", message,
                actor="push" if self.is_owner else "external", attachment_ids=tuple(attachments),
                source_thread_id=self.source_thread_id, source_session_key=context.session_key,
                source_run_id=context.run_id, source_tool_call_id=context.tool_call_id,
                source_kind="message_push",
            )
        except (KeyError, ValueError, RuntimeError) as failure:
            return AgentToolResult(f"主动消息未发送：{failure}", is_error=True)
        return AgentToolResult(json.dumps({
            "status": receipt.status.value, "attemptId": receipt.attempt_id,
            "platformMessageId": receipt.platform_message_id, "error": receipt.error,
        }, ensure_ascii=False), is_error=receipt.status is not DeliveryStatus.SENT)
