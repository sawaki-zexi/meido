import asyncio
import json
import pytest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from backend.app.agent_runtime import CancellationToken, ToolContext, MemoryRecallTool, RoleScopedMemoryReadPort
from backend.app.agent_runtime.memory_write_tools import MemorizeTool, ForgetMemoryTool, RoleMemoryAccess
from backend.app.memory_engine import DefaultMemoryEngine, MemoryMutation, MemoryQuery, MemoryScope
from backend.app.memory_service import MemoryService
from backend.app.memory_store import MemoryStore
from backend.app.models import AgentRun, MemorySourceRef
from backend.app.session_store import SessionStore
from backend.app.multichannel import MultichannelStore
from backend.app.storage import initialize_databases


def setup_memory(tmp_path):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / "sessions.db")
    sessions.open_role_session("role-a")
    _, user, _ = sessions.create_run_with_messages(AgentRun(runId="run-a", roleId="role-a", sessionKey="role:role-a",
        status="running", startedAt=datetime.now(timezone.utc)), "请记住我喜欢海边")
    engine = DefaultMemoryEngine(MemoryService(MemoryStore(tmp_path / "memory.db")), role_exists=lambda value: value in {"role-a", "role-b"})
    access = RoleMemoryAccess(engine, sessions, "role-a")
    context = ToolContext("role-a", "role:role-a", "run-a", CancellationToken())
    return engine, access, context, sessions, user


async def wait_for_embedding(event):
    deadline = asyncio.get_running_loop().time() + 3
    while not event.is_set():
        assert asyncio.get_running_loop().time() < deadline, "embedding was never reached"
        await asyncio.sleep(.001)


def test_role_memorizes_with_actual_user_message_evidence_and_domain(tmp_path):
    engine, access, context, sessions, user = setup_memory(tmp_path)

    async def run():
        written = await MemorizeTool(access).execute({"summary": "主人喜欢海边", "memory_kind": "preference", "memory_domain": "relationship"}, context)
        recalled = await engine.query(MemoryQuery("海边", scope=MemoryScope("role-a", "role:role-a")))
        return written, recalled

    written, recalled = asyncio.run(run())
    assert not written.is_error
    assert written.details["item_id"] == recalled.records[0].id
    assert recalled.records[0].domain == "relationship"
    assert recalled.records[0].evidence[0].refs == [user.id]
    assert recalled.records[0].evidence[0].source_ref.split("#")[0] == sessions.user_message_for_run("run-a").id


def test_role_recall_filters_real_memory_kind_domain_and_time_with_compatible_evidence(tmp_path):
    engine, access, context, _, user = setup_memory(tmp_path)

    async def run():
        await MemorizeTool(access).execute({"summary": "主人今天去海边", "memory_kind": "event", "memory_domain": "shared"}, context)
        await MemorizeTool(access).execute({"summary": "主人喜欢海边", "memory_kind": "preference", "memory_domain": "relationship"}, context)
        return await MemoryRecallTool(access).execute({"query": "海边", "intent": "timeline", "memory_kind": "event",
            "memory_domain": "shared", "time_filter": "recent_7d", "limit": 30}, context)

    result = asyncio.run(run())
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["count"] == 1
    assert payload["records"] == payload["items"]
    assert payload["items"][0]["summary"] == "主人今天去海边"
    assert payload["items"][0]["source_ref"].split("#")[0] == user.id
    assert payload["items"][0]["evidence"][0]["refs"] == [user.id]
    assert payload["cited_item_ids"] == [payload["items"][0]["id"]]


def test_role_forgets_only_owned_ids_and_repeat_is_reported_as_unchanged(tmp_path):
    engine, access, context, _, _ = setup_memory(tmp_path)

    async def run():
        written = await MemorizeTool(access).execute({"summary": "主人喜欢海边"}, context)
        item_id = written.details["item_id"]
        forgotten = await ForgetMemoryTool(access).execute({"ids": [item_id, item_id, "unknown"]}, context)
        repeated = await ForgetMemoryTool(access).execute({"ids": [item_id]}, context)
        recalled = await MemoryRecallTool(access).execute({"query": "海边"}, context)
        return item_id, forgotten, repeated, recalled

    item_id, forgotten, repeated, recalled = asyncio.run(run())
    assert forgotten.details["superseded_ids"] == [item_id]
    assert forgotten.details["missing_ids"] == ["unknown"]
    assert repeated.details["count"] == 0
    assert repeated.details["already_forgotten_ids"] == [item_id]
    assert json.loads(recalled.content)["count"] == 0


@pytest.mark.parametrize("tool_type,arguments", [
    (MemorizeTool, {"summary": "test", "role_id": "role-b"}),
    (MemorizeTool, {"summary": "test", "current_user_source_ref": "forged"}),
    (MemorizeTool, {"summary": "test", "metadata": {"messageIds": ["forged"]}}),
    (ForgetMemoryTool, {"ids": [], "session_key": "role:role-b"}),
    (MemoryRecallTool, {"query": "test", "role_id": "role-b"}),
])
def test_memory_tools_reject_model_identity_and_source_overrides(tmp_path, tool_type, arguments):
    _, access, context, _, _ = setup_memory(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(tool_type(access).execute(arguments, context))


@pytest.mark.parametrize("tool_type,arguments", [(MemorizeTool, {"summary": "test"}), (ForgetMemoryTool, {"ids": []}), (MemoryRecallTool, {"query": "test"})])
@pytest.mark.parametrize("denial", ["disabled", "foreign_role", "group", "visitor", "cancelled"])
def test_memory_tools_honor_live_permission_role_origin_and_cancel(tmp_path, tool_type, arguments, denial):
    _, access, context, _, _ = setup_memory(tmp_path)
    if denial == "disabled":
        access.enabled = lambda: False
    elif denial == "foreign_role":
        context = ToolContext("role-b", "role:role-b", "run-a", CancellationToken())
    elif denial in {"group", "visitor"}:
        context = ToolContext("role-a", f"thread:{denial}", "run-a", CancellationToken())
    else:
        context.signal.cancel()
    with pytest.raises((PermissionError, RuntimeError)):
        asyncio.run(tool_type(access).execute(arguments, context))


@pytest.mark.parametrize("value", ["bad", "2026-02-30", "2026-12-30~2026-01-01", "recent_5d", "2026-1-2"])
def test_recall_reports_invalid_time_filters_without_claiming_matches(tmp_path, value):
    _, access, context, _, _ = setup_memory(tmp_path)
    result = asyncio.run(MemoryRecallTool(access).execute({"query": "test", "time_filter": value}, context))
    assert result.is_error
    assert json.loads(result.content)["error"] == "invalid_time_filter"


def test_forget_many_unknown_ids_returns_bounded_valid_json(tmp_path):
    _, access, context, _, _ = setup_memory(tmp_path)
    tool = ForgetMemoryTool(access)
    ids = [str(index) + "x" * 190 for index in range(200)]
    result = asyncio.run(tool.execute({"ids": ids}, context))
    assert result.is_error
    assert len(result.content) <= tool.definition.output_limit
    payload = json.loads(result.content)
    assert payload["missing_count"] == 200
    assert payload["truncated"]


def test_same_user_message_can_teach_multiple_memories_and_procedure_steps_idempotently(tmp_path):
    engine, access, context, _, _ = setup_memory(tmp_path)

    async def run():
        first = await MemorizeTool(access).execute({"summary": "主人喜欢海边", "memory_kind": "preference"}, context)
        second = await MemorizeTool(access).execute({"summary": "主人喜欢森林", "memory_kind": "preference"}, context)
        repeat = await MemorizeTool(access).execute({"summary": "主人喜欢海边", "memory_kind": "preference"}, context)
        await MemorizeTool(access).execute({"summary": "阅读资料流程", "memory_kind": "procedure", "tool_requirement": "web_fetch", "steps": ["查看来源", "总结证据"]}, context)
        procedure = await engine.query(MemoryQuery("阅读资料", intent="procedure", effect="read_only", scope=access.scope))
        recalled = await MemoryRecallTool(access).execute({"query": "阅读资料", "intent": "procedure"}, context)
        return first, second, repeat, procedure, json.loads(recalled.content)

    first, second, repeat, procedure, recalled = asyncio.run(run())
    assert first.details["item_id"] != second.details["item_id"]
    assert first.details["item_id"] == repeat.details["item_id"]
    assert procedure.records[0].extra["steps"] == ["查看来源", "总结证据"]
    assert procedure.records[0].extra["tool_requirement"] == "web_fetch"
    assert recalled["items"][0]["steps"] == ["查看来源", "总结证据"]
    assert recalled["items"][0]["tool_requirement"] == "web_fetch"


def test_legacy_role_read_port_rejects_channel_origin_without_explicit_owner_adapter(tmp_path):
    engine, _, _, _, _ = setup_memory(tmp_path)
    tool = MemoryRecallTool(RoleScopedMemoryReadPort(engine, "role-a"))
    with pytest.raises(PermissionError):
        asyncio.run(tool.execute({"query": "private"}, ToolContext("role-a", "thread:group", "run-a", CancellationToken())))


def test_unknown_run_and_foreign_memory_ids_cannot_write_or_forget_current_role(tmp_path):
    engine, access, context, _, _ = setup_memory(tmp_path)
    with pytest.raises(PermissionError):
        asyncio.run(MemorizeTool(access).execute({"summary": "untrusted"}, ToolContext("role-a", "role:role-a", "unknown", CancellationToken())))

    async def run():
        foreign = await engine.mutate(MemoryMutation(kind="remember", scope=MemoryScope("role-b", "role:role-b"), summary="其他角色秘密"))
        forgotten = await ForgetMemoryTool(access).execute({"ids": [foreign.item_id]}, context)
        retained = await engine.query(MemoryQuery("秘密", effect="read_only", scope=MemoryScope("role-b", "role:role-b")))
        return foreign, forgotten, retained

    foreign, forgotten, retained = asyncio.run(run())
    assert forgotten.details["missing_ids"] == [foreign.item_id]
    assert [item.id for item in retained.records] == [foreign.item_id]


def test_engine_rejects_host_source_from_foreign_role_scope(tmp_path):
    engine, access, _, _, _ = setup_memory(tmp_path)
    with pytest.raises(PermissionError):
        asyncio.run(engine.mutate(MemoryMutation(kind="remember", scope=access.scope, summary="invalid",
            source=MemorySourceRef(kind="message", sessionKey="role:role-b", messageIds=["foreign"], stableSourceKey="foreign"))))


def test_binding_external_session_without_owner_authorizer_does_not_grant_private_memory(tmp_path):
    engine, _, _, sessions, _ = setup_memory(tmp_path)
    access = RoleMemoryAccess(engine, sessions, "role-a", session_key="thread:group")
    with pytest.raises(PermissionError):
        asyncio.run(MemoryRecallTool(access).execute({"query": "private"}, ToolContext("role-a", "thread:group", "run-a", CancellationToken())))


def test_paired_owner_private_message_is_valid_source_and_unbinding_revokes_memory_access(tmp_path):
    engine, _, _, sessions, _ = setup_memory(tmp_path)
    sessions.update_run("run-a", status="completed")

    class Credentials:
        def get(self, key):
            return ""

        def set(self, key, value):
            pass

    channels = MultichannelStore(sessions.database_path, sessions, Credentials())
    account = channels.register_channel_account("role-a", "qq", account_id="qq-owner")
    thread = channels.ensure_channel_thread("role-a", "qq", account.account_id, "owner", "private")
    code = channels.create_pairing_code("role-a", "qq")
    assert channels.consume_pairing_code(code, role_id="role-a", channel="qq", scope="platform",
        platform_user_id="owner", account_id=account.account_id, chat_id="owner")
    _, message = channels.append_event(thread, direction="inbound", content="请保存我的海边偏好", external_message_id="inbound-1")
    sessions.create_run_with_existing_user_message(AgentRun(runId="channel-run", roleId="role-a", sessionKey=thread.session_key,
        status="running", startedAt=datetime.now(timezone.utc)), message.id)
    context = ToolContext("role-a", thread.session_key, "channel-run", CancellationToken())
    access = RoleMemoryAccess(engine, sessions, "role-a", session_key=thread.session_key,
        authorize=lambda ctx: any(item.id == sessions.user_message_for_run(ctx.run_id).id for item in channels.owner_context_messages("role-a")))

    async def run():
        written = await MemorizeTool(access).execute({"summary": "主人喜欢海边"}, context)
        recalled = await MemoryRecallTool(access).execute({"query": "海边"}, context)
        return written, json.loads(recalled.content)

    written, recalled = asyncio.run(run())
    assert not written.is_error
    assert recalled["items"][0]["evidence"][0]["refs"] == [message.id]
    channels.unbind_identity(channels.list_identities()[0]["identityId"])
    with pytest.raises(PermissionError):
        asyncio.run(MemoryRecallTool(access).execute({"query": "海边"}, context))


def test_all_shiori_time_presets_and_inclusive_dates_select_real_event_times(tmp_path):
    engine, access, context, _, _ = setup_memory(tmp_path)
    tool = MemoryRecallTool(access, now=lambda: datetime(2026, 10, 15, 15, tzinfo=ZoneInfo("Asia/Shanghai")))

    async def run():
        await engine.mutate(MemoryMutation(kind="remember", scope=access.scope, summary="今日海边", memory_kind="event", happened_at="2026-10-15T12:00:00+08:00"))
        await engine.mutate(MemoryMutation(kind="remember", scope=access.scope, summary="昨日海边", memory_kind="event", happened_at="2026-10-14T12:00:00+08:00"))
        actual = {}
        for value in ["today", "yesterday", "recent_3d", "recent_7d", "recent_30d", "2026-10-14", "2026-10-14~2026-10-15", "2026-10-16"]:
            result = await tool.execute({"query": "海边", "intent": "timeline", "time_filter": value}, context)
            actual[value] = [item["summary"] for item in json.loads(result.content)["items"]]
        return actual

    actual = asyncio.run(run())
    assert actual == {"today": ["今日海边"], "yesterday": ["昨日海边"], "recent_3d": ["今日海边", "昨日海边"],
        "recent_7d": ["今日海边", "昨日海边"], "recent_30d": ["今日海边", "昨日海边"], "2026-10-14": ["昨日海边"],
        "2026-10-14~2026-10-15": ["今日海边", "昨日海边"], "2026-10-16": []}


@pytest.mark.parametrize("interrupt", ["deadline", "cancel"])
def test_running_embedding_write_keeps_loop_responsive_and_rolls_back_after_worker_finishes(tmp_path, interrupt):
    import time
    import threading

    engine, access, context, _, _ = setup_memory(tmp_path)
    entered = threading.Event()
    finished = threading.Event()

    class Embeddings:
        def embed(self, role_id, text):
            entered.set()
            time.sleep(.3)
            finished.set()
            return [1.0, 0.0]

    engine.service.embedding_provider = Embeddings()

    async def run():
        ticks = []
        started = asyncio.get_running_loop().time()

        async def heartbeat():
            await asyncio.sleep(.01)
            ticks.append(asyncio.get_running_loop().time() - started)

        timer = asyncio.create_task(heartbeat())
        task = asyncio.create_task(MemorizeTool(access).execute({"summary": "不要迟到写入"}, context))
        await wait_for_embedding(entered)
        if interrupt == "deadline":
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(task, .05)
        else:
            await asyncio.sleep(.05)
            context.signal.cancel()
            with pytest.raises(RuntimeError, match="cancelled"):
                await task
        await timer
        assert entered.is_set() and finished.is_set()
        assert ticks[0] < .15
        assert engine.list_active_items("role-a") == []
        await asyncio.sleep(.35)
        assert engine.list_active_items("role-a") == []

    asyncio.run(run())


def test_cancelled_mutation_holds_write_lock_until_worker_and_rollback_are_finished(tmp_path):
    import time
    import threading

    engine, access, context, _, _ = setup_memory(tmp_path)
    entered = threading.Event()

    class Embeddings:
        def embed(self, role_id, text):
            if text == "超时写入":
                entered.set()
                time.sleep(.3)
            return [1.0, 0.0]

    engine.service.embedding_provider = Embeddings()

    async def run():
        async def first():
            operation = asyncio.create_task(MemorizeTool(access).execute({"summary": "超时写入"}, context))
            await wait_for_embedding(entered)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(operation, .05)

        interrupted = asyncio.create_task(first())
        await wait_for_embedding(entered)
        await asyncio.sleep(.1)
        second = asyncio.create_task(MemorizeTool(access).execute({"summary": "后续合法写入"}, context))
        await asyncio.sleep(.02)
        assert not interrupted.done() and not second.done()
        await interrupted
        result = await second
        assert not result.is_error
        await asyncio.sleep(.35)
        assert [item.summary for item in engine.list_active_items("role-a")] == ["后续合法写入"]

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["query", "backfill"])
def test_recall_embedding_is_cancellable_without_blocking_loop_or_late_backfill(tmp_path, phase):
    import time
    import threading

    engine, access, context, _, _ = setup_memory(tmp_path)
    saved = asyncio.run(MemorizeTool(access).execute({"summary": "海边事实"}, context))
    entered = threading.Event()

    class Embeddings:
        def embed(self, role_id, text):
            if phase == "query" or text == "海边事实":
                entered.set()
                time.sleep(.3)
            return [1.0, 0.0]

    engine.service.embedding_provider = Embeddings()

    async def run():
        started = asyncio.get_running_loop().time()
        ticks = []

        async def heartbeat():
            await asyncio.sleep(.01)
            ticks.append(asyncio.get_running_loop().time() - started)

        timer = asyncio.create_task(heartbeat())
        operation = asyncio.create_task(MemoryRecallTool(access).execute({"query": "海边"}, context))
        await wait_for_embedding(entered)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(operation, .05)
        await timer
        assert ticks[0] < .15
        await asyncio.sleep(.4)
        item = engine.get_item_for_admin("role-a", saved.details["item_id"], include_embedding=True)
        assert item["status"] == "active" and item["embedding"] is None

    asyncio.run(run())
