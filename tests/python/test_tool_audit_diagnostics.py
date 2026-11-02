"""Offline coverage for the runtime's persisted tool failure diagnostics."""
import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import httpx
import pytest

from backend.app.agent_runtime import runtime
from backend.app.agent_runtime import (
    AgentToolResult, AssistantDoneEvent, AssistantMessage, MessageEndEvent,
    RuntimeManager, ToolCall, ToolDefinition, ToolExecutionEndEvent,
)
from backend.app.models import AgentRun
from backend.app.network_tools import NetworkToolsConfig, WebSearchTool
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


PRIVATE = "SYNTHETIC_PRIVATE_QUERY_CREDENTIAL_OR_BODY"


@pytest.fixture(params=["settled_callback", "end_event"])
def audit_path(request, monkeypatch):
    if request.param == "end_event":
        original_loop = runtime.run_agent_loop

        def event_only_loop(**kwargs):
            # Compatibility with loops that publish the receipt through events.
            kwargs["on_tool_result"] = None
            return original_loop(**kwargs)

        monkeypatch.setattr(runtime, "run_agent_loop", event_only_loop)
    return request.param


def run_tool(tmp_path, tool, arguments=None):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / "sessions.db")
    session = sessions.open_role_session("fixture-role")
    run = AgentRun(runId="fixture-run", roleId="fixture-role", sessionKey=session.sessionKey,
                   status="created", startedAt=datetime.now(timezone.utc))
    call = ToolCall("fixture-call", tool.definition.name, arguments or {})

    class Provider:
        requests = 0

        async def stream_response(self, **kwargs):
            self.requests += 1
            if self.requests == 1:
                yield AssistantDoneEvent(AssistantMessage(tool_calls=(call,), stop_reason="tool_use"))
            else:
                # Persistence must be complete before the next model turn.
                assert any(row.messageType == "tool_result" for row in sessions.list_messages(session.sessionKey))
                with closing(sqlite3.connect(sessions.database_path)) as db:
                    assert db.execute("SELECT ended_at FROM tool_audits").fetchone()[0]
                yield AssistantDoneEvent(AssistantMessage("fixture final answer"))

    provider = Provider()

    def persist(event: MessageEndEvent):
        return sessions.append_agent_message(session.sessionKey, event.message, run_id=run.runId)

    async def exercise():
        return [event async for event in RuntimeManager(sessions).run(
            run, provider=provider, model="fixture", system="", messages=[],
            tools=[tool], on_message_end=persist, max_turns=3)]

    events = asyncio.run(exercise())
    with closing(sqlite3.connect(sessions.database_path)) as db:
        rows = db.execute("SELECT result_category, error_type, error_message FROM tool_audits").fetchall()
        assert len(rows) == 1
        audit = rows[0]
        assert PRIVATE not in str(db.execute("SELECT argument_summary_json FROM tool_audits").fetchone())
    messages = [row for row in sessions.list_messages(session.sessionKey) if row.messageType == "tool_result"]
    assert len(messages) == 1
    ends = [event for event in events if isinstance(event, ToolExecutionEndEvent)]
    assert len(ends) == 1
    return audit, messages[0], ends[0], sessions.get_run(run.runId), provider.requests


@pytest.mark.parametrize("details,expected", [
    pytest.param({"errorType": "LegacyError", "message": "safe legacy failure"},
                 ("LegacyError", "safe legacy failure"), id="legacy"),
    pytest.param({"error_code": "provider_error", "error": "safe provider failure", "hint": "try later", "retryable": True},
                 ("provider_error", "safe provider failure"), id="structured"),
    pytest.param({"errorType": "LegacyError", "message": "legacy", "error_code": "other", "error": "other"},
                 ("LegacyError", "legacy"), id="legacy-precedence"),
    pytest.param({"errorType": {"raw": PRIVATE}, "message": [PRIVATE], "error_code": "safe_code", "error": "safe message"},
                 ("safe_code", "safe message"), id="nonstring-legacy-fallback"),
    pytest.param({"errorType": "", "message": "", "error_code": "safe_code", "error": "safe message"},
                 ("safe_code", "safe message"), id="empty-legacy-fallback"),
    pytest.param({"error_code": "code_only"}, ("code_only", None), id="missing-message"),
    pytest.param({"error": "message only"}, (None, "message only"), id="missing-code"),
    pytest.param({"errorType": {"raw": PRIVATE}, "message": [PRIVATE], "error_code": 401, "error": {"body": PRIVATE}},
                 (None, None), id="nonstring-diagnostics"),
    pytest.param({"raw_response": PRIVATE, "query": PRIVATE, "credentials": PRIVATE},
                 (None, None), id="unrecognized-fields"),
    pytest.param(None, (None, None), id="missing-details"),
    pytest.param([PRIVATE], (None, None), id="nonmapping-details"),
])
def test_failed_tool_audits_use_only_named_string_diagnostics(tmp_path, audit_path, details, expected):
    class Tool:
        definition = ToolDefinition("fixture", "Fixture", {"type": "object"})
        calls = 0

        def execute(self, arguments, context, on_update=None):
            self.calls += 1
            return AgentToolResult(PRIVATE, details=details, is_error=True)

    tool = Tool()
    audit, saved, ended, run, requests = run_tool(tmp_path, tool, {"query": PRIVATE})
    assert audit == ("failed", *expected)
    assert PRIVATE not in str(audit)
    assert tool.calls == 1 and requests == 2
    assert run.status == "completed"
    assert saved.isError and ended.result.is_error
    assert saved.toolResult == ended.result.details
    assert saved.content == ended.result.content == PRIVATE
    if isinstance(details, dict):
        assert all(saved.toolResult[key] == value for key, value in details.items())


@pytest.mark.parametrize("outcome,expected_audit,run_status,requests", [
    ("success", ("succeeded", None, None), "completed", 2),
    ("exception", ("failed", "ValueError", "tool execution failed"), "completed", 2),
    ("cancelled", ("cancelled", "RuntimeError", "tool cancelled"), "cancelled", 1),
])
def test_tool_audits_preserve_success_exception_and_cancellation(tmp_path, audit_path, outcome, expected_audit, run_status, requests):
    class Tool:
        definition = ToolDefinition("fixture", "Fixture", {"type": "object"})
        calls = 0

        def execute(self, arguments, context, on_update=None):
            self.calls += 1
            if outcome == "exception":
                raise ValueError(PRIVATE)
            if outcome == "cancelled":
                raise RuntimeError("agent run cancelled")
            return AgentToolResult("safe success", details={"error_code": PRIVATE, "error": PRIVATE})

    tool = Tool()
    audit, saved, ended, run, provider_requests = run_tool(tmp_path, tool)
    assert audit == expected_audit
    assert PRIVATE not in str(audit)
    assert run.status == run_status
    assert tool.calls == 1 and provider_requests == requests
    assert saved.isError == ended.result.is_error == (outcome != "success")


def test_web_search_safe_failure_reaches_audit_without_raw_upstream_body(tmp_path, audit_path, local_platform_only):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": 1,
            "result": {"isError": True, "content": [{"type": "text", "text": PRIVATE}]},
        })

    tool = WebSearchTool(NetworkToolsConfig(search_endpoint="https://example.test/mcp",
                        search_headers={"Authorization": f"Bearer {PRIVATE}"}),
                        transport=httpx.MockTransport(respond))
    audit, saved, ended, run, requests = run_tool(tmp_path, tool, {"query": PRIVATE})
    assert audit == ("failed", saved.toolResult["error_code"], saved.toolResult["error"])
    assert audit[1] and audit[2]
    assert PRIVATE not in str(audit) + saved.content + str(saved.toolResult)
    assert saved.toolResult == ended.result.details
    assert "hint" in saved.toolResult and "retryable" in saved.toolResult
    assert len(calls) == 1 and requests == 2
    assert run.status == "completed"
