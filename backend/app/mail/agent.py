from __future__ import annotations

import json
from collections.abc import Mapping

from ..agent_runtime.tools import AgentToolResult, ToolContext, ToolDefinition
from .service import MailService


class MailManageTool:
    definition = ToolDefinition(
        name="mail.manage",
        description=(
            "Manage the owner's QQ mail service only when the owner clearly requests an email action. "
            "You may check status, sync, enable or pause processing, list or read synchronized mail, "
            "list QQ drafts, inspect recent sent-message samples, process pending mail or a thread, "
            "retry work, create a draft, or create a new draft version. Never send, delete, archive, "
            "or move mail. Draft creation writes only to QQ Drafts; the owner reviews and sends there. "
            "Use an exact mailId or threadId from the list."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "sync", "enable", "pause", "list", "read", "drafts", "sent_samples", "process_pending", "process_thread", "process", "retry", "regenerate", "draft"]},
                "mailId": {"type": "string", "maxLength": 100},
                "threadId": {"type": "string", "maxLength": 200},
                "body": {"type": "string", "maxLength": 12000},
                "requestId": {"type": "string", "maxLength": 100},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        risk="external",
        source="builtin:mail",
        exposure="model_only",
        approval="prompt",
        timeout_seconds=120,
        output_limit=6000,
    )

    def __init__(self, service: MailService) -> None:
        self.service = service

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        action = str(arguments.get("action", ""))
        mail_id = str(arguments.get("mailId", ""))
        thread_id = str(arguments.get("threadId", ""))
        body = str(arguments.get("body", ""))
        request_id = str(arguments.get("requestId") or context.tool_call_id or "")
        try:
            result = await self.service.role_action(action, mail_id=mail_id, query=body, request_id=request_id, thread_id=thread_id, role_id=context.role_id, run_id=context.run_id)
            return AgentToolResult(json.dumps(result, ensure_ascii=False), details={"action": action, "mailId": mail_id or None, "status": "completed"})
        except Exception as error:
            self.service.store.audit(action or "unknown", context.role_id, context.run_id, mail_id or None, "failed", str(error))
            return AgentToolResult(str(error), details={"action": action, "mailId": mail_id or None}, is_error=True)
