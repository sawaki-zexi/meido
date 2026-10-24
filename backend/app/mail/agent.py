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
            "You may check status, view inbox statistics, sync, enable or pause processing, list or search synchronized mail, "
            "list QQ drafts, inspect recent sent-message samples, process pending mail or a thread, "
            "inspect thread follow-up status, explicitly create a reviewable follow-up draft, retry work, resume processing, create a draft, or create a new draft version. When asked to modify a draft, "
            "create a new version and preserve the existing one. Use read for persisted questions and sources. "
            "Use answer with the exact questionId and the owner's explicit answer to submit a job, and job to query it. "
            "Never send, delete, archive, "
            "or move mail. Draft creation writes only to QQ Drafts; the owner reviews and sends there. "
            "Use an exact mailId or threadId from the list."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "stats", "sync", "enable", "pause", "resume", "list", "threads", "read", "drafts", "sent_samples", "followup", "followup_draft", "process_pending", "process_thread", "process", "retry", "regenerate", "draft", "answer", "job"]},
                "mailId": {"type": "string", "maxLength": 100},
                "draftId": {"type": "string", "maxLength": 100},
                "threadId": {"type": "string", "maxLength": 200},
                "query": {"type": "string", "maxLength": 200},
                "body": {"type": "string", "maxLength": 12000},
                "requestId": {"type": "string", "maxLength": 100},
                "questionId": {"type": "string", "maxLength": 100},
                "jobId": {"type": "string", "maxLength": 100},
                "confirmUncertainNewVersion": {"type": "boolean"},
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
        draft_id = str(arguments.get("draftId", ""))
        thread_id = str(arguments.get("threadId", ""))
        query = str(arguments.get("query", ""))
        body = str(arguments.get("body", ""))
        request_id = str(arguments.get("requestId") or context.tool_call_id or "")
        try:
            if not self.service.preferences()["roleAccess"]:
                raise PermissionError("主人尚未允许角色管理邮件，或许可已撤销")
            result = await self.service.role_action(action, mail_id=mail_id, draft_id=draft_id, query=query if action in {"list", "stats", "threads"} else body, request_id=request_id, thread_id=thread_id, role_id=context.role_id, run_id=context.run_id,
                question_id=str(arguments.get("questionId") or ""), job_id=str(arguments.get("jobId") or ""),
                confirm_uncertain_new_version=arguments.get("confirmUncertainNewVersion") is True)
            return AgentToolResult(json.dumps(result, ensure_ascii=False), details={"action": action, "mailId": mail_id or None, "status": "completed"})
        except Exception as error:
            self.service.store.audit(action or "unknown", context.role_id, context.run_id, mail_id or None, "failed", str(error))
            return AgentToolResult(str(error), details={"action": action, "mailId": mail_id or None}, is_error=True)
