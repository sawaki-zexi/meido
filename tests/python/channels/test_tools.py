from __future__ import annotations

import asyncio
import json

from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.tools import ToolContext
from backend.app.channels.models import DeliveryResult, DeliveryStatus, Target, TargetKind
from backend.app.channels.tools import ChannelSendTool


class _Host:
    def __init__(self) -> None:
        self.call: dict[str, object] | None = None

    async def account_send(self, role_id, account_id, target_id, content, **kwargs):
        self.call = {
            "role_id": role_id,
            "account_id": account_id,
            "target_id": target_id,
            "content": content,
            **kwargs,
        }
        target = Target(target_id, TargetKind.PRIVATE, "Owner", account_id, "owner-chat")
        return DeliveryResult(DeliveryStatus.SENT, target, "message-1", attempt_id="attempt-1")


def test_account_send_passes_attachment_ids_to_role_scoped_host() -> None:
    host = _Host()
    tool = ChannelSendTool(host, is_owner=True, source_thread_id="thread-1")
    context = ToolContext("role-1", "thread-1", "run-1", CancellationToken())
    attachment_id = "a" * 32

    result = asyncio.run(tool.execute({
        "account_id": "qq-1",
        "target_id": "target:v1:opaque-owner-target",
        "content": "看一下这个文件",
        "attachments": [attachment_id],
    }, context))

    authorize = host.call.pop('authorize_source')
    assert authorize()
    context.signal.cancel()
    assert not authorize()
    assert host.call.pop('delivery_deadline') is None
    assert host.call == {
        "role_id": "role-1",
        "account_id": "qq-1",
        "target_id": "target:v1:opaque-owner-target",
        "content": "看一下这个文件",
        "attachment_ids": (attachment_id,),
        "mention_ids": (),
        "message_thread_id": None,
        "actor": "owner",
        "source_thread_id": "thread-1",
        "source_session_key": "thread-1",
        "source_run_id": "run-1",
        "source_tool_call_id": None,
        "source_kind": "account_send",
    }
    assert json.loads(result.content)["status"] == "sent"
    assert ChannelSendTool.definition.input_schema["properties"]["attachments"]["items"]["pattern"] == "^[0-9a-f]{32}$"


def test_account_send_rejects_empty_payload_before_transport() -> None:
    host = _Host()
    tool = ChannelSendTool(host, is_owner=True, source_thread_id="thread-1")
    context = ToolContext("role-1", "thread-1", "run-1", CancellationToken())

    result = asyncio.run(tool.execute({"account_id": "qq-1", "target_id": "target"}, context))

    assert result.is_error
    assert host.call is None
