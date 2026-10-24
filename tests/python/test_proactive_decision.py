"""Real source and Runtime feed a bounded model/tool decision, not a user run."""
import asyncio
import json

import pytest

from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock, ModelTextBlock
from backend.app.mcp_runtime import McpHost
from backend.app.proactive_runtime import ProactiveApplication
from backend.app.proactive_runtime.config import AutonomyConfig
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskRuntime
from test_proactive_mcp import source
from test_proactive_snapshot import grant_for


pytestmark = pytest.mark.usefixtures("local_platform_only")


class ScriptedDecision:
    def __init__(self, *, never_finish=False):
        self.requests = []
        self.item_id = None
        self.never_finish = never_finish

    async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
        self.requests.append((messages, tools, max_tokens))
        if self.never_finish:
            message = ModelAssistantMessage(content=(ModelTextBlock("Still considering."),))
        else:
            step = len(self.requests)
            if step == 1:
                name, arguments = "get_content_events", {}
            elif step == 2:
                self.item_id = json.loads(messages[-1].content[0].text)[0]["id"]
                name, arguments = "get_content", {"item_ids": [self.item_id]}
            elif step == 3:
                body = json.loads(messages[-1].content[0].text)[self.item_id]
                assert body["body"] == "" and body["error"] == "body is unavailable"
                name, arguments = "mark_not_interesting", {"item_ids": [self.item_id], "reason": "Explicit fixture decision"}
            else:
                name, arguments = "finish_turn", {"decision": "skip", "reason": "other", "note": "Fixture skip"}
            message = ModelAssistantMessage(content=(ModelToolCallBlock(f"call-{step}", name, arguments),))
        yield ModelStreamDone(message)


@pytest.mark.parametrize("never_finish", [False, True])
@pytest.mark.parametrize('max_steps', [35, 3])
def test_real_tick_model_tool_loop_requires_explicit_finish(source, tmp_path, never_finish, max_steps):
    _, store, *_ = source
    data = tmp_path / "decision"
    initialize_databases(data)
    sessions, tasks = SessionStore(data / "sessions.db"), TaskRuntime(data / "tasks")
    now, host, adapter = [1780000000.0], McpHost(store), ScriptedDecision(never_finish=never_finish)
    app = ProactiveApplication(sessions, tasks, clock=lambda: now[0], role_getter=lambda key: {"id": key},
        model_getter=lambda key: {"id": key}, mcp_host=host, adapter_factory=lambda config: adapter)
    app.configure("role", AutonomyConfig(activeDecisionEnabled=True, agentMaxSteps=max_steps,
        decisionModelConfigurationId="explicit-model", consentRevision=1,
        sourceGrants=[grant_for(source)]), expected_revision=0)

    async def exercise():
        try:
            now[0] += 1800
            await app.on_clock()
            async with asyncio.timeout(10):
                while True:
                    await app.recover()
                    run = app.query("role")["runs"][0]
                    if run["runtimeAcknowledged"]:
                        break
                    await asyncio.sleep(.01)
            assert run["mode"] == "active_decision"
            assert run["status"] == ("aborted" if never_finish or max_steps < 4 else "skipped")
            assert len(adapter.requests) == min(max_steps, 12 if never_finish else 4)
            assert all(request[2] == 1024 for request in adapter.requests)
            names = {tool["function"]["name"] for tool in adapter.requests[0][1]}
            assert len(names) == 12 and {"message_push", "finish_turn", "recall_memory"} <= names
            assert sessions.list_messages("role:role") == []
        finally:
            await app.close()
            await host.close()
    try:
        asyncio.run(exercise())
    finally:
        tasks.close()
