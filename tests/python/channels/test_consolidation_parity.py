import asyncio
from datetime import datetime, timedelta

import pytest

from backend.app.channels.consolidation import ChannelMemoryMaintenance
from backend.app.channels.group_tools import MultichannelGroupRepository
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore


def _maintenance(tmp_path, summarize, owner_statements=None):
    sessions = SessionStore(tmp_path / "sessions.db")
    store = MultichannelStore(tmp_path / "sessions.db", sessions)
    store.register_channel_account("role", "qq", account_id="qq", enabled=True, allow_group=True)
    thread = store.ensure_channel_thread("role", "qq", "qq", "group", "group")
    store.set_group_listening("role", "qq", "group", True)
    groups = MultichannelGroupRepository(store)
    owner_turns = []
    worker = ChannelMemoryMaintenance(lambda: store, lambda: groups, summarize, lambda *args: owner_turns.append(args), owner_statements=owner_statements)
    return store, thread, groups, worker, owner_turns


def test_listening_consolidation_keeps_owner_and_external_layers_separate(tmp_path):
    observed = []
    owners = []

    async def summarize(thread, messages, context, profiles):
        observed.extend(message["content"] for message in messages)
        return {"groupNote": "群笔记", "summary": "群最近动态", "members": [{"senderId": "visitor", "name": "访客", "profile": "访客档案", "shorthand": "速记"}, {"senderId": "unknown", "profile": "不能写"}]}

    async def owner_statements(thread, messages):
        owners.extend(message["content"] for message in messages)

    store, thread, groups, worker, owner_turns = _maintenance(tmp_path, summarize, owner_statements)
    yesterday = datetime.now().astimezone() - timedelta(days=1)
    store.listening.hear(thread, "主人喜欢红茶", "owner", {"senderId": "owner", "senderIsOwner": True}, timestamp=yesterday)
    store.listening.hear(thread, "访客喜欢绿茶", "visitor", {"senderId": "visitor", "senderIsOwner": False}, timestamp=yesterday)
    assert asyncio.run(worker.consolidate(thread.thread_id, listening=True))
    assert observed == ["主人喜欢红茶", "访客喜欢绿茶"]
    assert owners == ["主人喜欢红茶"]
    assert owner_turns == []
    assert groups.read_context("role", thread.thread_id)["summary"] == "群最近动态"
    assert worker.profile("role", thread.thread_id, "visitor")["profile"] == "访客档案"
    assert worker.profile("role", thread.thread_id, "unknown")["profile"] == ""
    with pytest.raises(KeyError):
        worker.profile("other-role", thread.thread_id, "visitor")
    assert not asyncio.run(worker.consolidate(thread.thread_id, listening=True))
    assert store.sessions.list_messages(thread.session_key) == []


def test_listening_consolidation_retries_stale_or_failed_models_without_advancing_cursor(tmp_path):
    async def summarize(thread, messages, context, profiles):
        groups.update_context("role", thread.thread_id, group_note="主人刚修改的笔记")
        return {"groupNote": "过期结果", "members": []}

    store, thread, groups, worker, _ = _maintenance(tmp_path, summarize)
    for i in range(50):
        store.listening.hear(thread, "heard", str(i), {"senderId": "visitor"})
    assert not asyncio.run(worker.consolidate(thread.thread_id, listening=True))
    assert groups.read_context("role", thread.thread_id)["groupNote"] == "主人刚修改的笔记"

    async def failed(*_):
        raise TimeoutError

    worker.summarize = failed
    with pytest.raises(TimeoutError):
        asyncio.run(worker.consolidate(thread.thread_id, listening=True))
    with store._connect() as db:
        assert db.execute("SELECT sequence FROM channel_listening_cursors WHERE thread_id=?", (thread.thread_id,)).fetchone() is None


def test_visitor_turn_never_enters_owner_memory_and_deleted_role_is_quiesced(tmp_path):
    async def summarize(*_):
        return {"members": []}

    store, thread, _, worker, owner_turns = _maintenance(tmp_path, summarize)
    _, user = store.append_event(thread, direction="inbound", content="hello", external_message_id="hello")
    _, assistant = store.append_event(thread, direction="outbound", content="hi", external_message_id="hi", role="assistant")

    async def scenario():
        await worker.commit_turn(thread, False, user, assistant)
        await asyncio.sleep(0)
        assert owner_turns == []
        await worker.begin_role_deletion("role")
        worker.request(thread.thread_id)
        assert not worker._tasks
        await worker.close()

    asyncio.run(scenario())
