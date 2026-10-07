import asyncio
import json

import pytest

from backend.app.agent_runtime import CancellationToken, MemoryRecallTool, RoleScopedMemoryReadPort, ToolContext
from backend.app.memory_engine import EvidenceRef, MemoryQuery, MemoryQueryResult, MemoryRecord, MemoryScope
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


class FakeMemoryReadPort:
    def __init__(self) -> None:
        self.requests: list[MemoryQuery] = []

    async def query(self, request: MemoryQuery) -> MemoryQueryResult:
        self.requests.append(request)
        return MemoryQueryResult(
            text_block="- [fact] 喜欢海边",
            records=[MemoryRecord(
                id="memory-1",
                kind="fact",
                summary="喜欢海边",
                score=0.8,
                engine_kind="fake",
                evidence=[EvidenceRef(kind="message", refs=["message-1"], resolver="session")],
                domain="shared",
            )],
        )


def _context(role_id: str = "role-a", session_key: str | None = None) -> ToolContext:
    return ToolContext(role_id, session_key or f"role:{role_id}", "run-1", CancellationToken())


def test_memory_recall_tool_uses_injected_scope_and_read_only_effect():
    engine = FakeMemoryReadPort()
    tool = MemoryRecallTool(RoleScopedMemoryReadPort(engine, "role-a"))

    result = asyncio.run(tool.execute(
        {"query": "我喜欢什么", "intent": "interest", "limit": 2},
        _context(),
    ))

    assert result.is_error is False
    payload = json.loads(result.content)
    assert payload["records"][0]["summary"] == "喜欢海边"
    assert payload["records"][0]["evidence"] == [{"kind": "message", "refs": ["message-1"], "resolver": "session"}]
    assert result.details == {"status": "succeeded", "count": 1, "intent": "interest"}
    assert len(engine.requests) == 1
    request = engine.requests[0]
    assert request.effect == "read_only"
    assert request.scope == MemoryScope("role-a", "role:role-a")


def test_memory_recall_tool_rejects_cross_role_context():
    engine = FakeMemoryReadPort()
    tool = MemoryRecallTool(RoleScopedMemoryReadPort(engine, "role-a"))

    with pytest.raises(PermissionError, match="scope"):
        asyncio.run(tool.execute({"query": "越权"}, _context("role-b")))
    assert engine.requests == []


def test_memory_recall_tool_rejects_invalid_arguments_before_engine_call():
    engine = FakeMemoryReadPort()
    tool = MemoryRecallTool(RoleScopedMemoryReadPort(engine, "role-a"))

    with pytest.raises(ValueError, match="too long"):
        asyncio.run(tool.execute({"query": "x" * 501}, _context()))
    with pytest.raises(ValueError, match="between 1 and 8"):
        asyncio.run(tool.execute({"query": "事实", "limit": 9}, _context()))
    with pytest.raises(ValueError, match="unsupported"):
        asyncio.run(tool.execute({"query": "事实", "intent": "timeline"}, _context()))
    assert engine.requests == []


def test_memory_recall_tool_honors_cancelled_context_before_engine_call():
    engine = FakeMemoryReadPort()
    token = CancellationToken()
    token.cancel()
    tool = MemoryRecallTool(RoleScopedMemoryReadPort(engine, "role-a"))

    with pytest.raises(RuntimeError, match="agent run cancelled"):
        asyncio.run(tool.execute(
            {"query": "事实"},
            ToolContext("role-a", "role:role-a", "run-1", token),
        ))

    assert engine.requests == []


def test_role_scoped_port_rejects_non_read_only_or_foreign_requests():
    engine = FakeMemoryReadPort()
    port = RoleScopedMemoryReadPort(engine, "role-a")

    with pytest.raises(PermissionError, match="read-only"):
        asyncio.run(port.query(MemoryQuery(
            text="事实", effect="stateful", scope=MemoryScope("role-a", "role:role-a")
        )))
    with pytest.raises(PermissionError, match="scope"):
        asyncio.run(port.query(MemoryQuery(
            text="事实", effect="read_only", scope=MemoryScope("role-b", "role:role-b")
        )))


def test_memory_recall_output_limit_preserves_valid_json():
    class LargeMemoryReadPort(FakeMemoryReadPort):
        async def query(self, request: MemoryQuery) -> MemoryQueryResult:
            del request
            return MemoryQueryResult(text_block="\n" * 20000, records=[])

    tool = MemoryRecallTool(RoleScopedMemoryReadPort(LargeMemoryReadPort(), "role-a"))
    result = asyncio.run(tool.execute({"query": "事实"}, _context()))

    assert len(result.content) <= tool.definition.output_limit
    payload = json.loads(result.content)
    assert payload["truncated"] is True


def test_open_tool_audit_is_recovered_as_failed_after_restart(tmp_path):
    initialize_databases(tmp_path / "data")
    first = SessionStore(tmp_path / "data" / "sessions.db")
    first.start_tool_audit(
        run_id="run-restarted",
        role_id="role-a",
        tool_name="recall_memory",
        call_id="call-1",
        argument_summary={"fields": {"query": {"type": "string", "length": 2}}},
        snapshot_id="cap-run-restarted",
        tool_source="builtin:memory",
        tool_version="1.0.0",
        policy_decision="allowed",
    )

    reopened = SessionStore(tmp_path / "data" / "sessions.db")
    audit = reopened.list_tool_audits("run-restarted")[0]

    assert audit["endedAt"] is not None
    assert audit["resultCategory"] == "failed"
    assert audit["errorType"] == "RuntimeError"
    assert audit["errorMessage"] == "服务重启时工具运行未完成"
    assert audit["snapshotId"] == "cap-run-restarted"
    assert audit["toolSource"] == "builtin:memory"
    assert audit["toolVersion"] == "1.0.0"
    assert audit["policyDecision"] == "allowed"


def test_finish_open_tool_audits_closes_only_unfinished_rows(tmp_path):
    initialize_databases(tmp_path / "data")
    store = SessionStore(tmp_path / "data" / "sessions.db")
    for call_id in ("open", "done"):
        store.start_tool_audit(
            run_id="run-close",
            role_id="role-a",
            tool_name="recall_memory",
            call_id=call_id,
            argument_summary={},
        )
    store.finish_tool_audit(
        run_id="run-close",
        call_id="done",
        result_category="succeeded",
    )

    store.finish_open_tool_audits(
        "run-close",
        result_category="cancelled",
        error_type="CancelledError",
        error_message="客户端断开",
    )
    audits = {item["callId"]: item for item in store.list_tool_audits("run-close")}

    assert audits["open"]["resultCategory"] == "cancelled"
    assert audits["open"]["errorType"] == "CancelledError"
    assert audits["done"]["resultCategory"] == "succeeded"
