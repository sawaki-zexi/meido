import asyncio
import json

import pytest

from backend.app.agent_runtime import CancellationToken, ToolContext
from backend.app.agent_runtime.message_history_tools import build_message_history_tools
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore


class TestCredentials:
    __test__ = False

    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        self.values[key] = value


def setup_history(tmp_path, *, thread=None, owner=True):
    sessions = SessionStore(tmp_path / "sessions.db")
    channels = MultichannelStore(tmp_path / "sessions.db", sessions, TestCredentials())
    sessions.open_role_session("r1")
    tools = {tool.definition.name: tool for tool in build_message_history_tools(
        sessions, channels, role_id="r1", source_thread_id=thread, is_owner=owner,
    )}
    context = ToolContext("r1", f"thread:{thread}" if thread else "role:r1", "run-1", CancellationToken())
    return sessions, channels, tools, context


def execute(tool, arguments, context):
    return json.loads(asyncio.run(tool.execute(arguments, context)).content)


def test_search_then_fetch_returns_original_text_and_adjacent_messages(tmp_path):
    sessions, _, tools, context = setup_history(tmp_path)
    before = sessions.append_message("role:r1", "user", "明天做什么？")
    match = sessions.append_message("role:r1", "assistant", "约定：周三 15:30 出发\n带伞", metadata={"secret": "internal"})
    after = sessions.append_message("role:r1", "user", "好的")

    search = execute(tools["search_messages"], {"query": "15:30"}, context)
    assert search["count"] == search["matched_count"] == 1
    assert search["messages"][0]["source_ref"] == match.id
    assert search["messages"][0]["preview"] == match.content
    fetched = execute(tools["fetch_messages"], {"source_ref": match.id, "context": 1}, context)
    assert fetched["matched_count"] == 1
    assert [message["id"] for message in fetched["messages"]] == [before.id, match.id, after.id]
    assert fetched["messages"][1] == {
        "id": match.id, "source_ref": match.id, "session_key": "role:r1", "seq": 2,
        "role": "assistant", "content": match.content, "timestamp": match.createdAt.isoformat(),
        "in_source_ref": True,
    }


def test_desktop_reads_only_currently_bound_owner_private_history(tmp_path):
    sessions, channels, tools, context = setup_history(tmp_path)
    account = channels.register_channel_account("r1", "qq", account_id="qq-one")
    owner = channels.ensure_channel_thread("r1", "qq", account.account_id, "owner", "private")
    visitor = channels.ensure_channel_thread("r1", "qq", account.account_id, "visitor", "private")
    group = channels.ensure_channel_thread("r1", "qq", account.account_id, "group", "group")
    channels.append_event(owner, direction="inbound", content="证据 配对之前", external_message_id="old")
    code = channels.create_pairing_code("r1", "qq")
    assert channels.consume_pairing_code(code, role_id="r1", channel="qq", scope="platform",
                                         platform_user_id="owner", account_id=account.account_id, chat_id="owner")
    _, allowed = channels.append_event(owner, direction="inbound", content="证据 主人私聊", external_message_id="allowed")
    channels.append_event(visitor, direction="inbound", content="证据 访客秘密", external_message_id="visitor")
    channels.append_event(group, direction="inbound", content="证据 群秘密", external_message_id="group")
    sessions.append_message("role:r2", "user", "证据 其他角色")
    sessions.append_message(owner.session_key, "assistant", "证据 尚未投递", metadata={"deliveryStatus": "accepted"})
    sessions.append_message(owner.session_key, "assistant", "证据 投递失败", status="failed")

    result = execute(tools["search_messages"], {"query": "证据"}, context)
    assert [message["id"] for message in result["messages"]] == [allowed.id]
    assert result["messages"][0]["seq"] == allowed.sequence
    channels.unbind_identity(str(channels.list_identities()[0]["identityId"]))
    with pytest.raises(PermissionError):
        execute(tools["fetch_messages"], {"source_ref": allowed.id}, context)
    fresh = build_message_history_tools(sessions, channels, role_id="r1")[1]
    assert execute(fresh, {"source_ref": allowed.id}, context)["count"] == 0


def test_group_and_visitor_tools_stay_in_host_bound_thread(tmp_path):
    sessions, channels, _, _ = setup_history(tmp_path)
    account = channels.register_channel_account("r1", "qq", account_id="qq-one")
    group = channels.ensure_channel_thread("r1", "qq", account.account_id, "g1", "group")
    second = channels.ensure_channel_thread("r1", "qq", account.account_id, "g2", "group")
    visitor = channels.ensure_channel_thread("r1", "qq", account.account_id, "guest", "private")
    secret = sessions.append_message("role:r1", "user", "秘密 主人")
    sessions.append_message(second.session_key, "user", "秘密 另一群")
    for thread, owner in ((group, True), (visitor, False)):
        own = sessions.append_message(thread.session_key, "user", "秘密 当前会话")
        tool = build_message_history_tools(sessions, channels, role_id="r1", source_thread_id=thread.thread_id, is_owner=owner)
        context = ToolContext("r1", thread.session_key, "run", CancellationToken())
        assert [message["id"] for message in execute(tool[0], {"query": "秘密"}, context)["messages"]] == [own.id]
        assert execute(tool[0], {"query": "秘密", "session_key": "role:r1"}, context)["count"] == 0
        assert execute(tool[1], {"source_ref": secret.id, "context": 10}, context)["count"] == 0
        with pytest.raises(PermissionError):
            execute(tool[0], {"query": "秘密"}, ToolContext("r2", thread.session_key, "run", CancellationToken()))
        with pytest.raises(PermissionError):
            execute(tool[0], {"query": "秘密"}, ToolContext("r1", second.session_key, "run", CancellationToken()))


def test_evidence_ids_and_json_source_refs_resolve_with_same_transcript_neighbors(tmp_path):
    sessions, channels, tools, context = setup_history(tmp_path)
    before = sessions.append_message("role:r1", "user", "第一句")
    account = channels.register_channel_account("r1", "qq", account_id="qq-one")
    owner = channels.ensure_channel_thread("r1", "qq", account.account_id, "owner", "private")
    code = channels.create_pairing_code("r1", "qq")
    channels.consume_pairing_code(code, role_id="r1", channel="qq", scope="platform",
                                  platform_user_id="owner", account_id=account.account_id, chat_id="owner")
    other = sessions.append_message(owner.session_key, "user", "夹在中间的私聊")
    match = sessions.append_message("role:r1", "assistant", "精确原文\n空格  不能丢")
    after = sessions.append_message("role:r1", "user", "最后一句")
    result = execute(tools["fetch_messages"], {"evidence": [{"kind": "message", "refs": [json.dumps([match.id]) + "#1-2"], "resolver": "session"}], "ids": [match.id], "context": 1}, context)
    assert result["count"] == 3 and result["matched_count"] == 1
    assert [message["id"] for message in result["messages"]] == [before.id, match.id, after.id]
    assert other.id not in str(result)


def test_search_pagination_role_filter_and_bounded_preview(tmp_path):
    sessions, _, tools, context = setup_history(tmp_path)
    first = sessions.append_message("role:r1", "user", "needle first")
    second = sessions.append_message("role:r1", "user", "\n".join(["needle second"] + ["行"] * 60))
    sessions.append_message("role:r1", "assistant", "needle answer")
    page = execute(tools["search_messages"], {"query": "NEEDLE", "role": "user", "limit": 1}, context)
    assert [message["id"] for message in page["messages"]] == [second.id]
    assert page["matched_count"] == 2 and page["has_more"] and page["next_offset"] == 1
    last = execute(tools["search_messages"], {"query": "needle", "role": "user", "limit": 1, "offset": page["next_offset"]}, context)
    assert last["messages"][0]["id"] == first.id
    assert page["messages"][0]["preview_line_count"] == 50
    assert page["messages"][0]["total_line_count"] == 61
    assert page["messages"][0]["truncated"] is True
    assert last["has_more"] is False and last["next_offset"] is None
    assert execute(tools["search_messages"], {"query": "   "}, context)["count"] == 0


def test_only_completed_conversation_text_is_evidence_and_identity_fields_are_rejected(tmp_path):
    sessions, _, tools, context = setup_history(tmp_path)
    sessions.append_message("role:r1", "assistant", "hidden failed", status="failed")
    sessions.append_message("role:r1", "assistant", "hidden thinking", status="streaming")
    sessions.append_message("role:r1", "assistant", "hidden call", message_type="tool_call")
    sessions.append_message("role:r1", "tool", "hidden result", message_type="tool_result")
    sessions.append_proactive_message_once("role:r1", "hidden reminder", event_id="proactive-one")
    assert execute(tools["search_messages"], {"query": "hidden"}, context)["count"] == 0
    assert execute(tools["fetch_messages"], {}, context) == {"count": 0, "matched_count": 0, "messages": []}
    for field in ("role_id", "thread_id", "turn_session_key"):
        with pytest.raises(ValueError, match="unknown arguments"):
            execute(tools["search_messages"], {"query": "hidden", field: "r2"}, context)
    with pytest.raises(ValueError):
        execute(tools["search_messages"], {"query": "hidden", "limit": True}, context)
    with pytest.raises(ValueError):
        execute(tools["fetch_messages"], {"context": 11}, context)
    context.signal.cancel()
    with pytest.raises(RuntimeError, match="cancelled"):
        execute(tools["search_messages"], {"query": "hidden"}, context)


def test_paired_private_rechecks_binding_without_upgrading_visitor_run(tmp_path):
    sessions, channels, _, _ = setup_history(tmp_path)
    account = channels.register_channel_account("r1", "qq", account_id="qq-one")
    thread = channels.ensure_channel_thread("r1", "qq", account.account_id, "owner", "private")
    old = sessions.append_message(thread.session_key, "user", "证据 配对之前")
    desktop = sessions.append_message("role:r1", "user", "证据 桌面主人")
    code = channels.create_pairing_code("r1", "qq")
    channels.consume_pairing_code(code, role_id="r1", channel="qq", scope="platform",
                                  platform_user_id="owner", account_id=account.account_id, chat_id="owner")
    allowed = sessions.append_message(thread.session_key, "user", "证据 配对之后")
    context = ToolContext("r1", thread.session_key, "run", CancellationToken())
    owner_tool = build_message_history_tools(sessions, channels, role_id="r1", source_thread_id=thread.thread_id)[0]
    guest_tool = build_message_history_tools(sessions, channels, role_id="r1", source_thread_id=thread.thread_id, is_owner=False)[0]
    assert {message["id"] for message in execute(owner_tool, {"query": "证据"}, context)["messages"]} == {desktop.id, allowed.id}
    assert {message["id"] for message in execute(guest_tool, {"query": "证据"}, context)["messages"]} == {old.id, allowed.id}
    channels.unbind_identity(str(channels.list_identities()[0]["identityId"]))
    with pytest.raises(PermissionError):
        execute(owner_tool, {"query": "证据"}, context)
    channels.delete_account(account.account_id)
    with pytest.raises(PermissionError):
        execute(owner_tool, {"query": "证据"}, context)


def test_multiple_search_terms_match_any_and_rank_by_coverage_then_recentness(tmp_path):
    sessions, _, tools, context = setup_history(tmp_path)
    both = sessions.append_message("role:r1", "user", "executable config")
    single = sessions.append_message("role:r1", "user", "executable location")
    recent = sessions.append_message("role:r1", "user", "config latest")
    result = execute(tools["search_messages"], {"query": "executable config"}, context)
    assert [message["id"] for message in result["messages"]] == [both.id, recent.id, single.id]


def test_large_history_search_keeps_event_loop_responsive(tmp_path):
    sessions, _, tools, context = setup_history(tmp_path)
    for _ in range(10):
        sessions.append_message("role:r1", "user", "a" * 1_000_000)

    async def run():
        loop = asyncio.get_running_loop()
        gaps = []
        finished = False

        async def heartbeat():
            previous = loop.time()
            while not finished:
                await asyncio.sleep(.01)
                now = loop.time()
                gaps.append(now - previous)
                previous = now

        pulse = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        try:
            result = await tools["search_messages"].execute({"query": " ".join(f"q{i:03}" for i in range(100))}, context)
            assert json.loads(result.content)["count"] == 0
        finally:
            finished = True
            await pulse
        assert max(gaps) < .25, "history search prevented cancellation and HTTP requests from running"

    asyncio.run(run())


def test_history_deadline_and_running_cancel_leave_parent_scope_usable(tmp_path):
    sessions, _, tools, context = setup_history(tmp_path)
    for _ in range(10):
        sessions.append_message("role:r1", "user", "a" * 1_000_000)

    async def run():
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(tools["search_messages"].execute({"query": "not-present"}, context), timeout=.001)
        assert loop.time() - started < .25
        context.signal.raise_if_cancelled()
        result = await tools["fetch_messages"].execute({}, context)
        assert json.loads(result.content)["count"] == 0

        async def cancel_soon():
            await asyncio.sleep(.001)
            context.signal.cancel()

        cancellation = asyncio.create_task(cancel_soon())
        with pytest.raises(RuntimeError, match="cancelled"):
            await tools["search_messages"].execute({"query": "not-present"}, context)
        await cancellation

    asyncio.run(run())
