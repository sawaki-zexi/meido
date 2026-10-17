"""A finalized proactive reply has one canonical desktop receipt and message."""
import asyncio
import json

import pytest

from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
from backend.app.mcp_runtime import McpHost
from backend.app.proactive_runtime import ProactiveApplication
from backend.app.proactive_runtime.config import AutonomyConfig
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskRuntime
from test_proactive_mcp import source
from test_proactive_snapshot import grant_for


pytestmark = pytest.mark.usefixtures("local_platform_only")


class ReplyModel:
    def __init__(self):
        self.requests = []
        self.identifier = None

    async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
        self.requests.append((messages, tools, max_tokens))
        step = len(self.requests)
        if step == 1:
            name, arguments = "get_content_events", {}
        elif step == 2:
            self.identifier = json.loads(messages[-1].content[0].text)[0]["id"]
            name, arguments = "mark_interesting", {"item_ids": [self.identifier]}
        elif step == 3:
            name, arguments = "message_push", {"message": "本地来源的主动消息", "mood": "neutral",
                "thought": "我正在验证本地协议。", "evidence": [self.identifier]}
        else:
            name, arguments = "finish_turn", {"decision": "reply"}
        yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f"reply-{step}", name, arguments),)))


def test_reply_receipt_recovery_and_next_tick_never_duplicate(source, tmp_path):
    root, store, *_ = source
    data = tmp_path / "delivery"
    initialize_databases(data)
    sessions, tasks = SessionStore(data / "sessions.db"), TaskRuntime(data / "tasks")
    now, host, adapter = [1780000000.0], McpHost(store), ReplyModel()
    app = ProactiveApplication(sessions, tasks, clock=lambda: now[0], role_getter=lambda key: {"id": key},
        model_getter=lambda key: {"id": key}, mcp_host=host, adapter_factory=lambda config: adapter)
    app.configure("role", AutonomyConfig(activeDecisionEnabled=True,
        decisionModelConfigurationId="explicit-model", consentRevision=1,
        sourceGrants=[grant_for(source)]), expected_revision=0)

    async def wait_for_terminal():
        async with asyncio.timeout(10):
            while True:
                await app.recover()
                run = app.query("role")["runs"][0]
                if run["runtimeAcknowledged"]:
                    return run
                await asyncio.sleep(.01)

    async def exercise():
        try:
            now[0] += 1800
            await app.on_clock()
            first = await wait_for_terminal()
            assert first["status"] == "sent"
            messages = sessions.list_messages("role:role")
            assert len(messages) == 1 and messages[0].role == "assistant"
            assert messages[0].messageType == "internal_completion"
            assert messages[0].metadata["internalEvent"] == "active_decision"
            assert messages[0].metadata["deliveryStatus"] == "sent"
            assert first["delivery"]["messageId"] == messages[0].id
            assert len(adapter.requests) == 4
            for _ in range(3):
                await app.recover()
            now[0] += 1800
            await app.on_clock()
            second = await wait_for_terminal()
            assert second["status"] == "no_events"
            assert len(adapter.requests) == 4
            assert len(sessions.list_messages("role:role")) == 1
            assert len((root / "calls.jsonl").read_text().splitlines()) == 2
        finally:
            await app.close()
            await host.close()
    try:
        asyncio.run(exercise())
    finally:
        tasks.close()
