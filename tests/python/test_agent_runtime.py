import asyncio
import sqlite3
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime, timezone

from backend.app.agent_runtime import (
    AssistantDoneEvent,
    AssistantMessage,
    CancellationToken,
    MessageEndEvent,
    RuntimeManager,
    TextDeltaEvent,
    ToolCall,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    ToolResultMessage,
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    UserMessage,
    run_agent_loop,
)
from backend.app.agent_runtime.provider import ProviderErrorEvent, ProviderStartEvent, ToolCallEndEvent
from backend.app.agent_runtime.tools import AgentToolResult
from backend.app.models import AgentRun
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app import main
from backend.app.model_config import ModelConfigurationStore
from backend.app.models import RoleInput, RoleProfile
from backend.app.role_store import RoleStore
from fastapi.testclient import TestClient
import pytest


class FakeProvider:
    def __init__(self, turns: Sequence[Sequence[object]]) -> None:
        self.turns = list(turns)
        self.calls: list[tuple[object, ...]] = []

    def stream_response(self, *, model: str, system: str, messages: Sequence[object], tools, signal, session_id=None) -> AsyncIterator[object]:
        del model, system, tools, signal, session_id
        self.calls.append(tuple(messages))
        events = self.turns.pop(0)

        async def stream() -> AsyncIterator[object]:
            for event in events:
                yield event

        return stream()


class ReadTool:
    definition = ToolDefinition(
        "read",
        "Read a value",
        {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    )

    def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        assert context.role_id == "role-1"
        if on_update:
            on_update(AgentToolResult("working"))
        return AgentToolResult(f"value:{arguments['key']}")


def test_tool_schema_and_output_limits_are_enforced_by_runtime():
    class BoundedTool:
        definition = ToolDefinition(
            "bounded",
            "Bounded tool",
            {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["safe"]},
                    "count": {"type": "integer", "minimum": 1, "maximum": 2},
                },
                "required": ["mode", "count"],
                "additionalProperties": False,
            },
            output_limit=5,
        )

        def execute(self, arguments, context, on_update=None):
            del arguments, context
            if on_update:
                on_update(AgentToolResult("update-too-long"))
            return AgentToolResult("result-too-long")

    call = ToolCall("bounded-call", "bounded", {"mode": "safe", "count": 2})
    provider = FakeProvider([
        [ToolCallEndEvent(call), AssistantDoneEvent(AssistantMessage(tool_calls=(call,), stop_reason="tool_use"))],
        [TextDeltaEvent("完成"), AssistantDoneEvent(AssistantMessage("完成"))],
    ])
    events = asyncio.run(_collect(run_agent_loop(
        provider=provider,
        model="fake",
        system="",
        messages=[UserMessage("执行")],
        tools=[BoundedTool()],
    )))
    update = next(event.result for event in events if isinstance(event, ToolExecutionUpdateEvent))
    result = next(event.result for event in events if isinstance(event, ToolExecutionEndEvent))
    assert update.content == "updat"
    assert update.details == {"truncated": True, "outputLimit": 5}
    assert result.content == "resul"
    assert result.details == {"truncated": True, "outputLimit": 5}

    registry = ToolRegistry([BoundedTool()])
    with pytest.raises(ValueError, match="unsupported value"):
        registry.validate_arguments("bounded", {"mode": "unsafe", "count": 1})
    with pytest.raises(ValueError, match="below the minimum"):
        registry.validate_arguments("bounded", {"mode": "safe", "count": 0})
    with pytest.raises(ValueError, match="exceeds the maximum"):
        registry.validate_arguments("bounded", {"mode": "safe", "count": 3})


def test_tool_registry_rejects_duplicates_and_validates_nested_schema():
    class NestedTool:
        definition = ToolDefinition(
            "nested",
            "Nested tool",
            {
                "type": "object",
                "properties": {
                    "options": {
                        "type": "object",
                        "required": ["kind"],
                        "properties": {"kind": {"type": "string", "enum": ["read"]}},
                        "additionalProperties": False,
                    },
                    "items": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 1},
                        "maxItems": 2,
                    },
                },
                "required": ["options", "items"],
                "additionalProperties": False,
            },
        )

        def execute(self, arguments, context, on_update=None):
            del arguments, context, on_update
            return AgentToolResult("ok")

    registry = ToolRegistry([NestedTool()])
    with pytest.raises(ValueError, match="duplicate tool"):
        ToolRegistry([NestedTool(), NestedTool()])
    with pytest.raises(ValueError, match="missing required arguments at options"):
        registry.validate_arguments("nested", {"options": {}, "items": [1]})
    with pytest.raises(ValueError, match="unsupported value"):
        registry.validate_arguments("nested", {"options": {"kind": "write"}, "items": [1]})
    with pytest.raises(ValueError, match="exceeds the maximum"):
        registry.validate_arguments("nested", {"options": {"kind": "read"}, "items": [1, 2, 3]})
    with pytest.raises(ValueError, match="below the minimum"):
        registry.validate_arguments("nested", {"options": {"kind": "read"}, "items": [0]})


def test_agent_loop_runs_tool_then_final_provider_turn():
    call = ToolCall("call-1", "read", {"key": "name"})
    provider = FakeProvider([
        [ProviderStartEvent(), ToolCallEndEvent(call), AssistantDoneEvent(AssistantMessage(tool_calls=(call,), stop_reason="tool_use"))],
        [ProviderStartEvent(), TextDeltaEvent("完成"), AssistantDoneEvent(AssistantMessage("完成"))],
    ])
    messages = [UserMessage("读取名字")]

    async def collect():
        return [event async for event in run_agent_loop(
            provider=provider,
            model="fake",
            system="",
            messages=messages,
            tools=[ReadTool()],
            role_id="role-1",
            session_key="role:role-1",
            run_id="run-1",
        )]

    events = asyncio.run(collect())
    assert messages[-2] == ToolResultMessage("call-1", "read", "value:name")
    assert provider.calls[1][-1] == messages[-2]
    assert any(isinstance(event, ToolExecutionStartEvent) for event in events)
    assert any(isinstance(event, ToolExecutionEndEvent) for event in events)
    assert any(isinstance(event, MessageEndEvent) and isinstance(event.message, AssistantMessage) and event.message.content == "完成" for event in events)
    runtime_events = [event for event in events if event.run_id == "run-1"]
    assert [event.sequence for event in runtime_events] == list(range(1, len(runtime_events) + 1))


def test_agent_loop_executes_multiple_tool_calls_in_order():
    first = ToolCall("call-name", "read", {"key": "name"})
    second = ToolCall("call-city", "read", {"key": "city"})
    provider = FakeProvider([
        [ToolCallEndEvent(first), ToolCallEndEvent(second), AssistantDoneEvent(AssistantMessage(tool_calls=(first, second), stop_reason="tool_use"))],
        [TextDeltaEvent("完成"), AssistantDoneEvent(AssistantMessage("完成"))],
    ])

    events = asyncio.run(_collect(run_agent_loop(
        provider=provider,
        model="fake",
        system="",
        messages=[UserMessage("读取资料")],
        tools=[ReadTool()],
        role_id="role-1",
    )))

    results = [event for event in events if isinstance(event, ToolExecutionEndEvent)]
    assert [event.tool_call_id for event in results] == ["call-name", "call-city"]
    assert [message.content for message in provider.calls[1][-2:]] == ["value:name", "value:city"]


def test_agent_loop_unknown_tool_returns_error_result_and_continues():
    call = ToolCall("unknown-call", "missing", {})
    provider = FakeProvider([
        [ToolCallEndEvent(call), AssistantDoneEvent(AssistantMessage(tool_calls=(call,), stop_reason="tool_use"))],
        [TextDeltaEvent("已说明"), AssistantDoneEvent(AssistantMessage("已说明"))],
    ])

    events = asyncio.run(_collect(run_agent_loop(
        provider=provider,
        model="fake",
        system="",
        messages=[UserMessage("运行未知工具")],
    )))

    result = next(event.result for event in events if isinstance(event, ToolExecutionEndEvent))
    assert result.is_error is True
    assert result.content == "unknown tool: missing"
    assert events[-1].type == "agent_end" and events[-1].reason == "completed"


def test_agent_loop_tool_exception_returns_error_result_and_continues():
    call = ToolCall("error-call", "explode", {})

    class ExplodingTool:
        definition = ToolDefinition("explode", "Raises an execution error", {"type": "object"})

        def execute(self, arguments, context, on_update=None):
            del arguments, context, on_update
            raise ValueError("tool failed")

    provider = FakeProvider([
        [ToolCallEndEvent(call), AssistantDoneEvent(AssistantMessage(tool_calls=(call,), stop_reason="tool_use"))],
        [TextDeltaEvent("已恢复"), AssistantDoneEvent(AssistantMessage("已恢复"))],
    ])

    events = asyncio.run(_collect(run_agent_loop(
        provider=provider,
        model="fake",
        system="",
        messages=[UserMessage("运行失败工具")],
        tools=[ExplodingTool()],
    )))

    result = next(event.result for event in events if isinstance(event, ToolExecutionEndEvent))
    assert result.is_error is True
    assert result.content == "tool failed"
    assert events[-1].type == "agent_end" and events[-1].reason == "completed"


def test_agent_loop_max_turns_stops_after_limit():
    call = ToolCall("call-1", "missing", {})
    provider = FakeProvider([
        [ToolCallEndEvent(call), AssistantDoneEvent(AssistantMessage(tool_calls=(call,), stop_reason="tool_use"))],
    ])

    events = asyncio.run(_collect(run_agent_loop(
        provider=provider,
        model="fake",
        system="",
        messages=[UserMessage("有限轮次")],
        max_turns=1,
    )))

    assert len(provider.calls) == 1
    assert events[-1].type == "agent_end" and events[-1].reason == "max_turns"


def test_agent_loop_provider_error_finishes_failed_run():
    provider = FakeProvider([[ProviderErrorEvent("upstream rejected request")]])

    events = asyncio.run(_collect(run_agent_loop(
        provider=provider,
        model="fake",
        system="",
        messages=[UserMessage("发生错误")],
    )))

    assert events[-1].type == "agent_end"
    assert events[-1].reason == "failed"
    assert events[-1].error == "upstream rejected request"


def test_agent_loop_cancellation_emits_aborted_message():
    token = CancellationToken()
    token.cancel()
    provider = FakeProvider([[ProviderStartEvent(), TextDeltaEvent("部分")]])

    events = asyncio.run(_collect(run_agent_loop(provider=provider, model="fake", system="", messages=[UserMessage("继续")], signal=token)))
    assert events[-1].type == "agent_end"
    assert events[-1].messages[-1].stop_reason == "aborted"
    assert events[-1].reason == "cancelled"


def test_agent_loop_returns_structured_error_for_invalid_tool_arguments():
    call = ToolCall("call-1", "read", {})
    provider = FakeProvider([
        [ToolCallEndEvent(call), AssistantDoneEvent(AssistantMessage(tool_calls=(call,), stop_reason="tool_use"))],
        [TextDeltaEvent("无法读取"), AssistantDoneEvent(AssistantMessage("无法读取"))],
    ])

    async def collect():
        return [event async for event in run_agent_loop(
            provider=provider,
            model="fake",
            system="",
            messages=[UserMessage("读取名字")],
            tools=[ReadTool()],
            role_id="role-1",
            run_id="run-invalid",
        )]

    events = asyncio.run(collect())
    tool_result = next(event for event in events if isinstance(event, ToolExecutionEndEvent))
    assert tool_result.result.is_error is True
    assert "required" in tool_result.result.content


def test_agent_loop_tool_timeout_becomes_error_result_and_can_continue():
    call = ToolCall("slow-call", "slow", {})

    class SlowTool:
        definition = ToolDefinition("slow", "Slow read-only operation", {"type": "object"})

        async def execute(self, arguments, context, on_update=None):
            del arguments, context, on_update
            await asyncio.sleep(0.05)
            return AgentToolResult("too late")

    provider = FakeProvider([
        [ToolCallEndEvent(call), AssistantDoneEvent(AssistantMessage(tool_calls=(call,), stop_reason="tool_use"))],
        [TextDeltaEvent("继续完成"), AssistantDoneEvent(AssistantMessage("继续完成"))],
    ])

    events = asyncio.run(_collect(run_agent_loop(
        provider=provider,
        model="fake",
        system="",
        messages=[UserMessage("执行慢工具")],
        tools=[SlowTool()],
        tool_timeout=0.001,
    )))

    tool_result = next(event for event in events if isinstance(event, ToolExecutionEndEvent))
    assert tool_result.result.is_error is True
    assert tool_result.result.content == "tool timed out"
    assert events[-1].type == "agent_end" and events[-1].reason == "completed"
    assert provider.calls[1][-1] == ToolResultMessage("slow-call", "slow", "tool timed out", is_error=True)


def test_agent_loop_provider_timeout_finishes_failed_run():
    class BlockingProvider:
        def stream_response(self, *, model, system, messages, tools, signal, session_id=None):
            del model, system, messages, tools, signal, session_id

            async def stream():
                await asyncio.Event().wait()
                yield TextDeltaEvent("never")

            return stream()

    events = asyncio.run(_collect(run_agent_loop(
        provider=BlockingProvider(),
        model="fake",
        system="",
        messages=[UserMessage("等待")],
        provider_timeout=0.001,
    )))

    assert events[-1].type == "agent_end"
    assert events[-1].reason == "failed"
    assert events[-1].error == "provider timed out"


def test_agent_loop_provider_end_without_assistant_done_fails_run():
    provider = FakeProvider([[TextDeltaEvent("部分回复")]])

    events = asyncio.run(_collect(run_agent_loop(
        provider=provider,
        model="fake",
        system="",
        messages=[UserMessage("继续")],
    )))

    assert events[-1].type == "agent_end"
    assert events[-1].reason == "failed"
    assert events[-1].error == "provider ended without assistant_done"


async def _collect(source):
    return [event async for event in source]


def test_session_store_persists_runtime_messages_and_runs(tmp_path):
    initialize_databases(tmp_path / "data")
    store = SessionStore(tmp_path / "data" / "sessions.db")
    session = store.open_role_session("role-1")
    run = store.create_run(AgentRun(runId="run-1", roleId="role-1", sessionKey=session.sessionKey, status="running", modelSnapshot={"model": "fake"}, startedAt=datetime.now(timezone.utc)))
    store.append_message(session.sessionKey, "assistant", "", "completed", message_type="tool_call", tool_call_id="call-1", tool_name="read", tool_arguments={"key": "name"}, run_id=run.runId)
    store.append_message(session.sessionKey, "tool", "value:name", "completed", message_type="tool_result", tool_call_id="call-1", tool_name="read", tool_result={"value": "name"}, run_id=run.runId)

    messages = store.list_messages(session.sessionKey)
    assert messages[0].messageType == "tool_call"
    assert messages[0].toolArguments == {"key": "name"}
    assert messages[1].messageType == "tool_result"
    assert messages[1].toolResult == {"value": "name"}
    completed = store.update_run(run.runId, status="completed", turn_count=2, ended_at=datetime.now(timezone.utc))
    assert completed.status == "completed"
    assert completed.turnCount == 2


def test_session_store_rebuilds_structured_runtime_transcript(tmp_path):
    initialize_databases(tmp_path / "data")
    store = SessionStore(tmp_path / "data" / "sessions.db")
    session = store.open_role_session("role-1")
    call = ToolCall("call-1", "read", {"key": "name"})
    store.append_agent_message(session.sessionKey, UserMessage("读取名字"), run_id="run-1")
    store.append_agent_message(session.sessionKey, AssistantMessage(tool_calls=(call,), stop_reason="tool_use"), run_id="run-1")
    store.append_agent_message(session.sessionKey, ToolResultMessage("call-1", "read", "value:name"), run_id="run-1")

    transcript = store.runtime_messages(session.sessionKey)
    assert transcript == [
        UserMessage("读取名字"),
        AssistantMessage(tool_calls=(call,), stop_reason="tool_use"),
        ToolResultMessage("call-1", "read", "value:name"),
    ]


def test_session_store_excludes_failed_runtime_tool_messages_from_context(tmp_path):
    initialize_databases(tmp_path / "data")
    store = SessionStore(tmp_path / "data" / "sessions.db")
    session = store.open_role_session("role-1")
    call = ToolCall("failed-call", "read", {"key": "name"})
    store.append_agent_message(session.sessionKey, UserMessage("失败的读取"), run_id="run-failed")
    store.append_agent_message(
        session.sessionKey,
        AssistantMessage(tool_calls=(call,), stop_reason="tool_use"),
        run_id="run-failed",
        status="failed",
    )
    store.append_agent_message(
        session.sessionKey,
        ToolResultMessage(call.id, call.name, "不应重放"),
        run_id="run-failed",
        status="failed",
    )

    assert store.runtime_messages(session.sessionKey) == [UserMessage("失败的读取")]


def test_session_store_rebuilds_multi_call_turn_and_repairs_results(tmp_path):
    initialize_databases(tmp_path / "data")
    store = SessionStore(tmp_path / "data" / "sessions.db")
    session = store.open_role_session("role-1")
    first = ToolCall("call-1", "read", {"key": "name"})
    second = ToolCall("call-2", "read", {"key": "city"})
    store.append_agent_message(session.sessionKey, UserMessage("读取资料"), run_id="run-1")
    store.append_agent_message(
        session.sessionKey,
        AssistantMessage(tool_calls=(first, second), stop_reason="tool_use"),
        run_id="run-1",
    )
    store.append_agent_message(session.sessionKey, ToolResultMessage("call-2", "read", "value:city"), run_id="run-1")
    store.append_message(
        session.sessionKey,
        "tool",
        "duplicate",
        message_type="tool_result",
        tool_call_id="call-2",
        tool_name="read",
        run_id="run-1",
    )
    store.append_message(
        session.sessionKey,
        "tool",
        "orphan",
        message_type="tool_result",
        tool_call_id="orphan",
        tool_name="read",
        run_id="run-1",
    )
    store.append_agent_message(session.sessionKey, AssistantMessage("最终回答"), run_id="run-1")

    transcript = store.runtime_messages(session.sessionKey)
    assert transcript[0] == UserMessage("读取资料")
    assert transcript[1] == AssistantMessage(tool_calls=(first, second), stop_reason="tool_use")
    assert transcript[2] == ToolResultMessage(
        "call-1",
        "read",
        "Tool call interrupted before a result was recorded",
        is_error=True,
        details={"repaired": True},
    )
    assert transcript[3] == ToolResultMessage("call-2", "read", "value:city")
    assert transcript[4] == AssistantMessage("最终回答")
    assert store.runtime_messages(session.sessionKey, max_messages=2) == transcript


def test_session_store_persists_assistant_tool_calls_atomically(tmp_path):
    initialize_databases(tmp_path / "data")
    store = SessionStore(tmp_path / "data" / "sessions.db")
    session = store.open_role_session("role-1")
    good = ToolCall("call-good", "read", {"key": "name"})
    unserializable = ToolCall("call-bad", "read", {"key": object()})

    try:
        store.append_agent_message(
            session.sessionKey,
            AssistantMessage(tool_calls=(good, unserializable), stop_reason="tool_use"),
            run_id="run-atomic",
        )
    except TypeError:
        pass
    else:
        raise AssertionError("unserializable tool arguments should fail")

    assert store.list_messages(session.sessionKey) == []


def test_create_run_with_messages_rolls_back_duplicate_run_without_partial_messages(tmp_path):
    initialize_databases(tmp_path / "data")
    store = SessionStore(tmp_path / "data" / "sessions.db")
    session = store.open_role_session("role-1")
    run = AgentRun(
        runId="run-1",
        roleId="role-1",
        sessionKey=session.sessionKey,
        status="running",
        startedAt=datetime.now(timezone.utc),
    )
    store.create_run_with_messages(run, "第一次")
    before = store.list_messages(session.sessionKey)
    try:
        store.create_run_with_messages(run, "重复运行")
    except Exception as error:
        assert "UNIQUE" in str(error).upper()
    else:
        raise AssertionError("duplicate run should fail")
    after = store.list_messages(session.sessionKey)
    assert [(message.role, message.content) for message in after] == [(message.role, message.content) for message in before]


def test_finish_run_commits_assistant_state_and_is_idempotent(tmp_path):
    initialize_databases(tmp_path / "data")
    store = SessionStore(tmp_path / "data" / "sessions.db")
    session = store.open_role_session("role-1")
    run = AgentRun(
        runId="run-terminal",
        roleId="role-1",
        sessionKey=session.sessionKey,
        status="running",
        startedAt=datetime.now(timezone.utc),
    )
    _, _, assistant = store.create_run_with_messages(run, "问题")

    first_run, first_message = store.finish_run(
        run.runId,
        status="failed",
        assistant_message_id=assistant.id,
        assistant_content="部分回答",
        turn_count=1,
        error="模型连接中断",
    )
    repeated_run, repeated_message = store.finish_run(
        run.runId,
        status="completed",
        assistant_message_id=assistant.id,
        assistant_content="不应覆盖",
        turn_count=2,
    )

    assert first_run.status == repeated_run.status == "failed"
    assert first_message.status == repeated_message.status == "failed"
    assert repeated_message.content == "部分回答"
    assert repeated_message.metadata["runtimeReason"] == "failed"


def test_startup_fails_reserved_and_running_runs_with_streaming_assistants(tmp_path):
    initialize_databases(tmp_path / "data")
    database = tmp_path / "data" / "sessions.db"
    store = SessionStore(database)
    created_session = store.open_role_session("role-created")
    created_run = AgentRun(
        runId="run-created",
        roleId="role-created",
        sessionKey=created_session.sessionKey,
        status="created",
        startedAt=datetime.now(timezone.utc),
    )
    store.create_run_with_messages(created_run, "未开始")
    running_session = store.open_role_session("role-running")
    running_run = AgentRun(
        runId="run-running",
        roleId="role-running",
        sessionKey=running_session.sessionKey,
        status="running",
        startedAt=datetime.now(timezone.utc),
    )
    store.create_run_with_messages(running_run, "运行中")

    recovered = SessionStore(database)

    for run_id, role_id in (("run-created", "role-created"), ("run-running", "role-running")):
        run = recovered.get_run(run_id)
        assert run is not None and run.status == "failed"
        assert run.endedAt is not None
        assert run.error == "服务重启时运行未完成"
        assistant = recovered.list_messages(f"role:{role_id}")[-1]
        assert assistant.role == "assistant" and assistant.status == "failed"


def test_sqlite_allows_only_one_created_or_running_run_per_role(tmp_path):
    initialize_databases(tmp_path / "data")
    store = SessionStore(tmp_path / "data" / "sessions.db")
    session = store.open_role_session("role-1")
    first = AgentRun(
        runId="run-active-1",
        roleId="role-1",
        sessionKey=session.sessionKey,
        status="created",
        startedAt=datetime.now(timezone.utc),
    )
    second = AgentRun(
        runId="run-active-2",
        roleId="role-1",
        sessionKey=session.sessionKey,
        status="created",
        startedAt=datetime.now(timezone.utc),
    )
    store.create_run_with_messages(first, "首个请求")
    active = store.active_run("role-1")
    assert active is not None and active.runId == first.runId and active.status == "created"

    try:
        store.create_run_with_messages(second, "并发请求")
    except Exception as error:
        assert "UNIQUE" in str(error).upper()
    else:
        raise AssertionError("SQLite must reject a second active run for the same role")

    assert [message.content for message in store.list_messages(session.sessionKey)] == ["首个请求", ""]


def test_initialize_databases_upgrades_and_repairs_active_run_index(tmp_path):
    data_dir = tmp_path / "data"
    initialize_databases(data_dir)
    database = data_dir / "sessions.db"
    started = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX agent_runs_one_active_per_role")
        connection.execute("CREATE UNIQUE INDEX agent_runs_one_active_per_role ON agent_runs(role_id) WHERE status = 'running'")
        for run_id in ("run-old-1", "run-old-2"):
            connection.execute(
                "INSERT INTO agent_runs (run_id, role_id, session_key, status, model_snapshot_json, started_at) VALUES (?, 'role-1', 'role:role-1', 'created', '{}', ?)",
                (run_id, started),
            )

    initialize_databases(data_dir)

    with sqlite3.connect(database) as connection:
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='agent_runs_one_active_per_role'"
        ).fetchone()[0]
        assert "created" in index_sql and "running" in index_sql
        active = connection.execute(
            "SELECT run_id, status FROM agent_runs WHERE role_id='role-1' AND status IN ('created', 'running')"
        ).fetchall()
        assert len(active) == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO agent_runs (run_id, role_id, session_key, status, model_snapshot_json, started_at) VALUES ('run-old-3', 'role-1', 'role:role-1', 'created', '{}', ?)",
                (started,),
            )


def test_runtime_manager_cancels_provider_blocked_inside_await(tmp_path):
    initialize_databases(tmp_path / "data")
    store = SessionStore(tmp_path / "data" / "sessions.db")
    session = store.open_role_session("role-1")
    manager = RuntimeManager(store)
    started = asyncio.Event()

    class BlockingProvider:
        def stream_response(self, *, model, system, messages, tools, signal, session_id=None):
            del model, system, messages, tools, signal, session_id

            async def stream():
                started.set()
                await asyncio.Event().wait()
                yield TextDeltaEvent("never")

            return stream()

    run = AgentRun(
        runId="run-blocked",
        roleId="role-1",
        sessionKey=session.sessionKey,
        status="created",
        startedAt=datetime.now(timezone.utc),
    )

    async def exercise():
        task = asyncio.create_task(_collect(manager.run(
            run,
            provider=BlockingProvider(),
            model="fake",
            system="",
            messages=[UserMessage("等待")],
        )))
        await started.wait()
        assert manager.cancel_run(run.runId) is True
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(exercise())
    assert store.get_run(run.runId).status == "cancelled"


def test_runtime_manager_cancels_reserved_run_during_preparation(tmp_path):
    initialize_databases(tmp_path / "data")
    store = SessionStore(tmp_path / "data" / "sessions.db")
    session = store.open_role_session("role-1")
    manager = RuntimeManager(store)
    run = AgentRun(
        runId="run-preparation-cancel",
        roleId="role-1",
        sessionKey=session.sessionKey,
        status="created",
        startedAt=datetime.now(timezone.utc),
    )

    async def exercise():
        await manager.reserve_run(run, "预备请求")
        started = asyncio.Event()

        async def prepare():
            assert manager.attach_current_task(run.runId)
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                manager.cancel_unstarted_run(run.runId)
                raise

        task = asyncio.create_task(prepare())
        await started.wait()
        assert manager.cancel_run(run.runId)
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    completed = store.get_run(run.runId)
    assert completed is not None and completed.status == "cancelled"
    assert not manager.lock_for(run.roleId).locked()


def test_runtime_persists_tool_result_before_requesting_next_provider_turn(tmp_path):
    initialize_databases(tmp_path / "data")
    store = SessionStore(tmp_path / "data" / "sessions.db")
    session = store.open_role_session("role-1")
    run = AgentRun(
        runId="run-persist-order",
        roleId="role-1",
        sessionKey=session.sessionKey,
        status="running",
        startedAt=datetime.now(timezone.utc),
    )
    store.create_run_with_messages(run, "读取名字")
    call = ToolCall("call-1", "read", {"key": "name"})

    class VerifyingProvider(FakeProvider):
        def stream_response(self, *, model, system, messages, tools, signal, session_id=None):
            if self.calls:
                assert any(
                    message.messageType == "tool_result" and message.toolCallId == call.id
                    for message in store.list_messages(session.sessionKey)
                )
            return super().stream_response(
                model=model,
                system=system,
                messages=messages,
                tools=tools,
                signal=signal,
                session_id=session_id,
            )

    provider = VerifyingProvider([
        [ToolCallEndEvent(call), AssistantDoneEvent(AssistantMessage(tool_calls=(call,), stop_reason="tool_use"))],
        [TextDeltaEvent("完成"), AssistantDoneEvent(AssistantMessage("完成"))],
    ])

    def persist(event):
        if isinstance(event.message, AssistantMessage):
            store.append_agent_message(session.sessionKey, event.message, run_id=run.runId)
        elif isinstance(event.message, ToolResultMessage):
            store.append_agent_message(session.sessionKey, event.message, run_id=run.runId)

    async def exercise():
        manager = RuntimeManager(store)
        await _collect(manager.run(
            run,
            provider=provider,
            model="fake",
            system="",
            messages=[UserMessage("读取名字")],
            tools=[ReadTool()],
            on_message_end=persist,
        ))

    asyncio.run(exercise())
    assert store.get_run(run.runId).status == "completed"


def test_http_text_run_persists_agent_run_and_keeps_sse_compatibility(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="运行角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")

    class Adapter:
        async def stream_reply(self, role, history, configuration=None, memory_context=""):
            del role, history, configuration, memory_context
            yield "完成"

    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", main.SessionManager(roles, sessions))
    monkeypatch.setattr(main, "model_adapter", Adapter())
    monkeypatch.setattr(main, "model_configuration_store", ModelConfigurationStore(tmp_path / "data" / "model-config.json"))
    monkeypatch.setattr(main, "role_locks", {})

    response = TestClient(main.app).post(f"/api/roles/{role.id}/messages", json={"content": "你好"})
    assert response.status_code == 200
    assert "event: assistant_completed" in response.text
    assert '"runId": "run-' in response.text
    run = sessions.active_run(role.id)
    assert run is None
    stored = sessions.list_messages(f"role:{role.id}")
    assert stored[-1].content == "完成"
