"""Member lookup uses real host observations and durable role/thread data."""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.tools import ToolContext
from backend.app.channels.group_tools import LookupGroupMemberTool, MultichannelGroupRepository
from backend.app.channels.consolidation import ChannelMemoryMaintenance
from backend.app.channels.host import ChannelHost
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.channels.models import TargetKind, target_from_chat
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore
from backend.app.models import AgentRun
from backend.app.channels.directory import DirectoryQuery
from channels.test_host import MemoryCredentials


def test_real_sender_historical_names_survive_restart(tmp_path):
    database = tmp_path / "sessions.db"
    sessions = SessionStore(database)
    channels = MultichannelStore(database, sessions)
    channels.register_channel_account("r", "qq", account_id="a", enabled=True, allow_group=True)

    async def transport(operation, params):
        assert operation in {"get_friend_list", "get_group_list", "get_group_member_list"}
        return {"data": []}

    adapter = NapCatAdapter(NapCatConfig("a"), transport=transport)
    host = ChannelHost(channels)
    host.register(adapter)

    async def observe():
        for number, nickname in enumerate(("Alice", "艾丽丝")):
            event = await host.accept_event("a", {
                "post_type": "message", "message_type": "group", "self_id": 99,
                "group_id": 100, "message_id": number + 1,
                "sender": {"user_id": 22, "nickname": nickname},
                "message": [{"type": "at", "data": {"qq": "99"}}, {"type": "text", "data": {"text": "你好"}}],
            })
            assert event is not None

    asyncio.run(observe())
    # New service instances read the saved names, without a live adapter.
    restarted = MultichannelStore(database, SessionStore(database))
    tool = LookupGroupMemberTool(MultichannelGroupRepository(restarted), role_id="r")
    context = ToolContext("r", "role:r", "run", CancellationToken())
    for name in ("alice", "艾丽丝"):
        result = asyncio.run(tool.execute({"name": name}, context))
        assert not result.is_error, result.content
        data = json.loads(result.content)
        assert data["total"] == 1
        assert data["candidates"] == [{"channel": "qq", "member_id": "22", "name": "艾丽丝", "nicknames": ["艾丽丝", "Alice"]}]


def test_name_candidates_are_bounded_with_accurate_total_and_channel_identity(tmp_path):
    database = tmp_path / "sessions.db"
    channels = MultichannelStore(database, SessionStore(database))
    for channel, account in (("qq", "a"), ("feishu", "b")):
        channels.register_channel_account("r", channel, account_id=account)
        channels.remember_targets("r", [target_from_chat(
            account_id=account, chat_id=str(member), kind=TargetKind.GROUP_MEMBER,
            display_name=f"Alice {member}", group_id="group",
        ) for member in range(13 if channel == "qq" else 1)])
    tool = LookupGroupMemberTool(MultichannelGroupRepository(channels), role_id="r")
    context = ToolContext("r", "role:r", "run", CancellationToken())
    qq = json.loads(asyncio.run(tool.execute({"name": "ALICE", "member_channel": "qq"}, context)).content)
    assert qq["total"] == 13 and len(qq["candidates"]) == 10
    assert all(item["channel"] == "qq" for item in qq["candidates"])
    all_channels = json.loads(asyncio.run(tool.execute({"name": "Alice 0"}, context)).content)
    assert {(item["channel"], item["member_id"]) for item in all_channels["candidates"]} == {("qq", "0"), ("feishu", "0")}
    empty = json.loads(asyncio.run(tool.execute({"name": "unknown"}, context)).content)
    assert empty == {"total": 0, "candidates": []}


def test_id_lookup_uses_shared_profile_and_ignores_model_names(tmp_path):
    database = tmp_path / "sessions.db"
    channels = MultichannelStore(database, SessionStore(database))
    channels.register_channel_account("r", "qq", account_id="a", enabled=True, allow_group=True)
    groups = MultichannelGroupRepository(channels)

    async def summarize(thread, messages, context, profiles):
        assert messages and all(item["metadata"]["senderId"] == "22" for item in messages)
        if thread.chat_id == "200":
            assert profiles["22"]["profile"] == "喜欢猫"
        return {"members": [{"senderId": "22", "name": "ModelInventedName",
                             "profile": "喜欢猫" if thread.chat_id == "100" else "喜欢猫，也养狗",
                             "shorthand": f"本群速记 {thread.chat_id}"}]}

    worker = ChannelMemoryMaintenance(lambda: channels, lambda: groups, summarize, lambda *args: None)
    threads = []
    for chat, nickname in (("100", "Alice"), ("200", "艾丽丝")):
        thread = channels.ensure_channel_thread("r", "qq", "a", chat, "group")
        threads.append(thread)
        for number in range(18):
            channels.append_event(thread, direction="inbound", content="RAW_CHAT_MUST_NOT_LEAK",
                external_message_id=f"{chat}-{number}", metadata={"senderId": "22", "senderName": nickname})
        assert asyncio.run(worker.consolidate(thread.thread_id, listening=False))
    tool = LookupGroupMemberTool(groups, role_id="r")
    context = ToolContext("r", "role:r", "run", CancellationToken())
    result = asyncio.run(tool.execute({"member_id": "22", "member_channel": "qq"}, context))
    assert not result.is_error, result.content
    data = json.loads(result.content)
    assert data["member_id"] == "22" and data["channel"] == "qq"
    assert data["name"] == "艾丽丝" and data["nicknames"] == ["艾丽丝", "Alice"]
    assert {(item["thread_id"], item["chat_id"], item["profile"], item["shorthand"]) for item in data["sources"]} == {
        (threads[0].thread_id, "100", "喜欢猫，也养狗", "本群速记 200"),
        (threads[1].thread_id, "200", "喜欢猫，也养狗", "本群速记 200"),
    }
    assert "RAW_CHAT_MUST_NOT_LEAK" not in result.content
    fake = json.loads(asyncio.run(tool.execute({"name": "ModelInventedName"}, context)).content)
    assert fake == {"total": 0, "candidates": []}


def _channel_context(channels, *, owner=False, chat_type="group"):
    if owner:
        code = channels.create_pairing_code("r", "qq")
        assert channels.consume_pairing_code(code, role_id="r", channel="qq", scope="platform",
            platform_user_id="sender", account_id="a", chat_id="100")
    thread = channels.ensure_channel_thread("r", "qq", "a", "100", chat_type)
    _, message = channels.append_event(thread, direction="inbound", content="查成员",
        external_message_id="query", metadata={"senderId": "sender", "senderName": "访客", "senderIsOwner": owner})
    run = AgentRun(runId="member-run", roleId="r", sessionKey=thread.session_key,
        status="created", startedAt=datetime.now(timezone.utc))
    channels.sessions.create_run_with_existing_user_message(run, message.id)
    return thread, ToolContext("r", thread.session_key, run.runId, CancellationToken())


@pytest.mark.parametrize("owner,chat_type,expected", [(False, "group", 1), (True, "group", 1), (False, "private", 1), (True, "private", 2)])
def test_member_lookup_scopes_real_sources_and_rechecks_owner_revocation(tmp_path, owner, chat_type, expected):
    database = tmp_path / "sessions.db"
    channels = MultichannelStore(database, SessionStore(database))
    channels.register_channel_account("r", "qq", account_id="a", enabled=True, allow_group=True)
    thread, context = _channel_context(channels, owner=owner, chat_type=chat_type)
    for chat, kind in (("100", chat_type), ("200", "group")):
        target = channels.ensure_channel_thread("r", "qq", "a", chat, kind)
        channels.append_event(target, direction="inbound", content="PRIVATE_RAW",
            external_message_id=f"name-{chat}", metadata={"senderId": f"member-{chat}", "senderName": "Alice"})
    tool = LookupGroupMemberTool(MultichannelGroupRepository(channels), role_id="r",
        source_thread_id=thread.thread_id, is_owner=owner)
    result = asyncio.run(tool.execute({"name": "Alice"}, context))
    assert not result.is_error, result.content
    assert json.loads(result.content)["total"] == expected
    assert "PRIVATE_RAW" not in result.content
    if expected == 1:
        other = asyncio.run(tool.execute({"member_id": "member-200", "member_channel": "qq"}, context))
        assert json.loads(other.content)["found"] is False
    if owner:
        channels.unbind_identity(channels.list_identities()[0]["identityId"])
        assert asyncio.run(tool.execute({"name": "Alice"}, context)).is_error


@pytest.mark.parametrize("arguments", [{}, {"name": ""}, {"name": "Alice", "member_id": "1"}, {"member_id": "1"}, {"name": []}, {"name": "Alice", "role_id": "foreign"}])
def test_member_lookup_rejects_ambiguous_or_forged_arguments(tmp_path, arguments):
    database = tmp_path / "sessions.db"
    channels = MultichannelStore(database, SessionStore(database))
    tool = LookupGroupMemberTool(MultichannelGroupRepository(channels), role_id="r")
    result = asyncio.run(tool.execute(arguments, ToolContext("r", "role:r", "run", CancellationToken())))
    assert result.is_error


def test_old_database_backfills_only_host_observed_names_and_role_delete_cleans_them(tmp_path):
    class Credentials:
        def get(self, key):
            return ""

        def set(self, key, value):
            pass

    database = tmp_path / "sessions.db"
    channels = MultichannelStore(database, SessionStore(database), Credentials())
    MultichannelGroupRepository(channels)
    channels.register_channel_account("r", "qq", account_id="a", enabled=True, allow_group=True)
    thread = channels.ensure_channel_thread("r", "qq", "a", "100", "group")
    channels.remember_targets("r", [target_from_chat(account_id="a", chat_id="22", kind=TargetKind.GROUP_MEMBER,
        display_name="Alice", group_id="100")])
    channels.append_event(thread, direction="inbound", content="RAW_NOT_A_NICKNAME",
        external_message_id="old", metadata={"senderId": "22", "senderName": "艾丽丝"})
    with channels._connect() as db:
        db.execute("DROP TABLE channel_member_names")
        db.execute("DROP TABLE IF EXISTS channel_member_name_schema")
        db.execute("INSERT INTO channel_member_profiles VALUES(?,?,?,?,?,?,?)",
            ("r", thread.thread_id, "22", "ModelInventedName", "已保存资料", "简称", "old"))
    restarted = MultichannelStore(database, SessionStore(database), Credentials())
    repository = MultichannelGroupRepository(restarted)
    found = repository.find_members("r", name="alice")
    assert len(found) == 1 and found[0]["nicknames"] == ["艾丽丝", "Alice"]
    assert repository.find_members("r", name="ModelInventedName") == []
    assert repository.find_members("r", name="RAW_NOT_A_NICKNAME") == []
    restarted.delete_role("r")
    assert repository.find_members("r", name="") == []


@pytest.mark.parametrize("change", ["platform", "block", "scope", "rebind", "recreate"])
def test_lookup_never_reuses_revoked_source_authority(tmp_path, change):
    class Credentials:
        def get(self, key):
            return ""

        def set(self, key, value):
            pass

    database = tmp_path / "sessions.db"
    channels = MultichannelStore(database, SessionStore(database), Credentials())
    channels.register_channel_account("r", "qq", account_id="a", enabled=True, allow_group=True)
    thread, context = _channel_context(channels, owner=True, chat_type="private")
    tool = LookupGroupMemberTool(MultichannelGroupRepository(channels), role_id="r",
        source_thread_id=thread.thread_id, is_owner=True)
    assert not asyncio.run(tool.execute({"name": "访客"}, context)).is_error
    if change == "platform":
        channels.set_channel_enabled("qq", False)
    elif change == "rebind":
        channels.unbind_identity(channels.list_identities()[0]["identityId"])
        code = channels.create_pairing_code("r", "qq")
        assert channels.consume_pairing_code(code, role_id="r", channel="qq", scope="platform",
            platform_user_id="sender", account_id="a", chat_id="100")
    elif change == "recreate":
        channels.delete_account("a")
        channels.register_channel_account("r", "qq", account_id="a", enabled=True)
    else:
        channels.register_channel_account("r", "qq", account_id="a", enabled=True,
            **({"blocked_senders": ["sender"]} if change == "block" else {"allow_private": False}))
    result = asyncio.run(tool.execute({"name": "访客"}, context))
    assert result.is_error and "nicknames" not in result.content


def test_guest_owner_claim_and_cross_role_context_cannot_read_member_names(tmp_path):
    database = tmp_path / "sessions.db"
    channels = MultichannelStore(database, SessionStore(database))
    channels.register_channel_account("r", "qq", account_id="a", enabled=True, allow_group=True)
    thread, context = _channel_context(channels, owner=False)
    claimed_owner = LookupGroupMemberTool(MultichannelGroupRepository(channels), role_id="r",
        source_thread_id=thread.thread_id, is_owner=True)
    assert asyncio.run(claimed_owner.execute({"name": "访客"}, context)).is_error
    desktop = LookupGroupMemberTool(MultichannelGroupRepository(channels), role_id="r")
    assert asyncio.run(desktop.execute({"name": "访客"}, ToolContext("foreign", "role:r", "run", CancellationToken()))).is_error


def test_member_profile_pages_thread_sources_without_losing_or_merging_them(tmp_path):
    database = tmp_path / "sessions.db"
    channels = MultichannelStore(database, SessionStore(database))
    channels.register_channel_account("r", "qq", account_id="a", enabled=True, allow_group=True)
    for group in range(7):
        thread = channels.ensure_channel_thread("r", "qq", "a", str(group), "group")
        channels.append_event(thread, direction="inbound", content="RAW",
            external_message_id=str(group), metadata={"senderId": "22", "senderName": "Alice"})
    tool = LookupGroupMemberTool(MultichannelGroupRepository(channels), role_id="r")
    context = ToolContext("r", "role:r", "run", CancellationToken())
    first = json.loads(asyncio.run(tool.execute({"member_id": "22", "member_channel": "qq"}, context)).content)
    assert first["sources_total"] == 7 and len(first["sources"]) == 5 and first["next_source_page"] == 2
    second = json.loads(asyncio.run(tool.execute({"member_id": "22", "member_channel": "qq", "source_page": 2}, context)).content)
    assert second["sources_total"] == 7 and len(second["sources"]) == 2 and second["next_source_page"] is None
    assert {item["chat_id"] for item in (*first["sources"], *second["sources"])} == {str(group) for group in range(7)}


def test_cached_directory_refresh_cannot_replace_the_last_observed_member_name(tmp_path):
    database = tmp_path / "sessions.db"
    channels = MultichannelStore(database, SessionStore(database), MemoryCredentials())
    channels.register_channel_account("r", "qq", account_id="a", enabled=True, allow_group=True)

    directory_members = {}

    async def transport(operation, params):
        if operation == "get_group_member_list":
            return {"data": directory_members.get(str(params["group_id"]), [])}
        return {"data": []}

    host = ChannelHost(channels)
    host.register(NapCatAdapter(NapCatConfig("a"), transport=transport))
    tool = LookupGroupMemberTool(MultichannelGroupRepository(channels), role_id="r")
    context = ToolContext("r", "role:r", "run", CancellationToken())

    async def observe():
        for number, (group, sender, name) in enumerate(((100, 22, "Bob"), (200, 22, "Alice"),
                                                     (100, 22, "Bob"), (300, 33, "Charlie"))):
            event = await host.accept_event("a", {
                "post_type": "message", "message_type": "group", "self_id": 99,
                "group_id": group, "message_id": number + 1,
                "sender": {"user_id": sender, "nickname": name},
                "message": [{"type": "text", "data": {"text": "ordinary message"}}],
            })
            assert event is not None
            thread = channels.ensure_channel_thread("r", "qq", "a", event.chat_id, "group")
            channels.append_event(thread, direction="inbound", content=event.content,
                external_message_id=event.external_message_id,
                metadata={"senderId": event.sender_id, "senderName": event.sender_name})
        result = await tool.execute({"name": "Alice"}, context)
        assert json.loads(result.content)["candidates"] == [
            {"channel": "qq", "member_id": "22", "name": "Bob", "nicknames": ["Bob", "Alice"]}]
        # A successful fresh directory response is a real observation even
        # when the name is unchanged within that group's cached target.
        directory_members["200"] = [{"user_id": 22, "nickname": "Alice"}]
        await host.query_directory("r", "a", DirectoryQuery("members", group_id="200"), scope="role:r")
        current = json.loads((await tool.execute({"name": "Bob"}, context)).content)
        assert current["candidates"] == [
            {"channel": "qq", "member_id": "22", "name": "Alice", "nicknames": ["Alice", "Bob"]}]

    asyncio.run(observe())


def test_old_listening_nicknames_remain_queryable_after_upgrade(tmp_path):
    database = tmp_path / "sessions.db"
    channels = MultichannelStore(database, SessionStore(database))
    channels.register_channel_account("r", "qq", account_id="a", enabled=True, allow_group=True)
    thread = channels.ensure_channel_thread("r", "qq", "a", "100", "group")
    channels.set_group_listening("r", "a", "100", True)
    for number, name in enumerate(("Alice", "Beth")):
        assert channels.listening.hear(thread, "LISTENING_RAW_MUST_NOT_LEAK", str(number),
            {"senderId": "22", "senderName": name}) is not None
    channels.remember_targets("r", [target_from_chat(account_id="a", chat_id="22",
        kind=TargetKind.GROUP_MEMBER, display_name="Beth", group_id="100")])
    with channels._connect() as db:
        db.execute("DROP TABLE channel_member_names")
        db.execute("DROP TABLE channel_member_name_schema")
    restarted = MultichannelStore(database, SessionStore(database))
    tool = LookupGroupMemberTool(MultichannelGroupRepository(restarted), role_id="r")
    result = asyncio.run(tool.execute({"name": "Alice"}, ToolContext("r", "role:r", "run", CancellationToken())))
    assert not result.is_error
    assert json.loads(result.content) == {"total": 1, "candidates": [
        {"channel": "qq", "member_id": "22", "name": "Beth", "nicknames": ["Beth", "Alice"]}]}
    assert "LISTENING_RAW_MUST_NOT_LEAK" not in result.content


def test_reused_account_id_keeps_channels_and_profile_sources_separate(tmp_path):
    class Credentials:
        def get(self, key):
            return ""

        def set(self, key, value):
            pass

    database = tmp_path / "sessions.db"
    channels = MultichannelStore(database, SessionStore(database), Credentials())
    repository = MultichannelGroupRepository(channels)
    with channels._connect() as db:
        db.execute("DROP TABLE channel_external_migrations")
    for channel in ("qq", "feishu"):
        channels.register_channel_account("r", channel, account_id="reused", enabled=True, allow_group=True)
        # Archived threads can retain a removed account ID across platforms.
        # Seed that valid persisted shape before querying the public tool.
        with channels._connect() as db:
            db.execute("INSERT INTO channel_threads VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"archive-{channel}", f"thread:archive-{channel}", "r", channel, "reused", "100", "group", "",
                 datetime.now(timezone.utc).isoformat(), "{}"))
        thread = channels.get_thread("r", f"archive-{channel}")
        assert thread is not None
        channels.append_event(thread, direction="inbound", content="RAW",
            external_message_id=channel, metadata={"senderId": "22", "senderName": "Alice"})
        with channels._connect() as db:
            db.execute("INSERT INTO channel_member_profiles VALUES(?,?,?,?,?,?,?)",
                ("r", thread.thread_id, "22", "Alice", f"{channel} profile", "", datetime.now(timezone.utc).isoformat()))
        channels.delete_account("reused")
    channels = MultichannelStore(database, SessionStore(database), Credentials())
    repository = MultichannelGroupRepository(channels)
    tool = LookupGroupMemberTool(repository, role_id="r")
    context = ToolContext("r", "role:r", "run", CancellationToken())
    found = json.loads(asyncio.run(tool.execute({"name": "Alice"}, context)).content)
    assert found["total"] == 2
    assert {(item["channel"], item["member_id"]) for item in found["candidates"]} == {("qq", "22"), ("feishu", "22")}
    for channel in ("qq", "feishu"):
        profile = json.loads(asyncio.run(tool.execute({"member_id": "22", "member_channel": channel}, context)).content)
        assert profile["sources_total"] == 1
        assert [item["profile"] for item in profile["sources"]] == [f"{channel} profile"]


def test_role_deletion_rollback_restores_historical_member_names(tmp_path):
    class Credentials:
        def get(self, key):
            return ""

        def set(self, key, value):
            pass

    database = tmp_path / "sessions.db"
    channels = MultichannelStore(database, SessionStore(database), Credentials())
    channels.register_channel_account("r", "qq", account_id="a", enabled=True, allow_group=True)
    thread = channels.ensure_channel_thread("r", "qq", "a", "100", "group")
    for number, name in enumerate(("Alice", "Beth")):
        channels.append_event(thread, direction="inbound", content="RAW",
            external_message_id=str(number), metadata={"senderId": "22", "senderName": name})
    tool = LookupGroupMemberTool(MultichannelGroupRepository(channels), role_id="r")
    context = ToolContext("r", "role:r", "run", CancellationToken())
    snapshot = channels.snapshot_role("r")
    channels.delete_role("r")
    assert json.loads(asyncio.run(tool.execute({"name": "Alice"}, context)).content)["total"] == 0
    channels.restore_role("r", snapshot)
    restored = json.loads(asyncio.run(tool.execute({"name": "Alice"}, context)).content)
    assert restored == {"total": 1, "candidates": [
        {"channel": "qq", "member_id": "22", "name": "Beth", "nicknames": ["Beth", "Alice"]}]}
