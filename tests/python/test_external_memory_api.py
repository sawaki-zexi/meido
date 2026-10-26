"""Owner-facing external memory through HTTP; offline data remains editable."""
import asyncio
import sqlite3
from pathlib import Path
import pytest

from backend.app import main
from backend.app.channels.consolidation import ChannelMemoryMaintenance
from backend.app.channels.group_tools import MultichannelGroupRepository
from backend.app.multichannel import MultichannelStore
from test_channel_api import channel_api as channel_api


def memory_url(role_id, thread_id):
    return f"/api/roles/{role_id}/channels/threads/{thread_id}/external-memory"


def test_notes_are_independent_versioned_and_persist_while_account_is_offline(channel_api, monkeypatch):
    role, channels, _, client = channel_api
    channels.register_channel_account(role.id, "qq", account_id="offline", enabled=False, allow_group=True)
    group = channels.ensure_channel_thread(role.id, "qq", "offline", "100", "group")
    visitor = channels.ensure_channel_thread(role.id, "qq", "offline", "22", "private")
    url = memory_url(role.id, group.thread_id)
    first = client.get(url)
    assert first.status_code == 200
    assert first.json()["note"] == "" and first.json()["revision"] == 0
    saved = client.put(url + "/note", json={"note": "每周讨论读书", "expectedRevision": 0})
    assert saved.status_code == 200 and saved.json()["note"] == "每周讨论读书"
    revision = saved.json()["revision"]
    assert revision > 0
    assert client.put(url + "/note", json={"note": "过期页面", "expectedRevision": 0}).status_code == 409
    private_url = memory_url(role.id, visitor.thread_id)
    assert client.get(private_url).json()["note"] == ""
    assert client.put(private_url + "/note", json={"note": "陌生人私聊笔记", "expectedRevision": 0}).status_code == 200
    restarted = MultichannelStore(channels.database_path, channels.sessions, channels.credentials)
    monkeypatch.setattr(main, "multichannel_store", restarted)
    assert client.get(url).json()["note"] == "每周讨论读书"
    assert client.put(url + "/note", json={"note": "", "expectedRevision": revision}).status_code == 200
    assert client.get(url).json()["note"] == ""
    assert client.get(private_url).json()["note"] == "陌生人私聊笔记"


def test_old_consolidation_cannot_overwrite_clear_and_restore_or_advance_cursor(channel_api):
    role, channels, _, client = channel_api
    channels.register_channel_account(role.id, "qq", account_id="qq", enabled=True, allow_group=True)
    thread = channels.ensure_channel_thread(role.id, "qq", "qq", "100", "group")
    channels.set_group_listening(role.id, "qq", "100", True)
    for index in range(50):
        channels.listening.hear(thread, "群友谈论读书", str(index), {"senderId": "22"})
    url = memory_url(role.id, thread.thread_id)
    first = client.put(url + "/note", json={"note": "原笔记", "expectedRevision": 0}).json()
    calls = []
    async def summarize(_thread, messages, context, profiles):
        calls.append([message["id"] for message in messages])
        if len(calls) == 1:
            cleared = client.put(url + "/note", json={"note": "", "expectedRevision": first["revision"]}).json()
            restored = client.put(url + "/note", json={"note": "原笔记", "expectedRevision": cleared["revision"]})
            assert restored.status_code == 200
            return {"groupNote": "过期结果", "members": []}
        assert context["groupNote"] == "原笔记"
        return {"groupNote": "重新整理的笔记", "members": []}
    groups = MultichannelGroupRepository(channels)
    worker = ChannelMemoryMaintenance(lambda: channels, lambda: groups, summarize, lambda *a: None)
    assert not asyncio.run(worker.consolidate(thread.thread_id, listening=True))
    assert client.get(url).json()["note"] == "原笔记"
    assert asyncio.run(worker.consolidate(thread.thread_id, listening=True))
    assert calls[0] == calls[1]
    assert client.get(url).json()["note"] == "重新整理的笔记"


def test_same_qq_member_has_one_editable_profile_across_observed_groups(channel_api):
    role, channels, _, client = channel_api
    channels.register_channel_account(role.id, "qq", account_id="qq", enabled=True, allow_group=True, platform_id="99")
    first = channels.ensure_channel_thread(role.id, "qq", "qq", "100", "group")
    second = channels.ensure_channel_thread(role.id, "qq", "qq", "200", "group")
    for thread in (first, second):
        channels.set_group_listening(role.id, "qq", thread.chat_id, True)
        for index in range(50):
            channels.listening.hear(thread, "喜欢红茶", str(index), {"senderId": "22", "senderName": "阿宁" if thread == first else "新昵称"})
    groups = MultichannelGroupRepository(channels)
    async def summarize(thread, messages, context, profiles):
        if thread.thread_id == second.thread_id:
            assert profiles["22"]["profile"] == "喜欢红茶"
        return {"members": [{"senderId": "22", "profile": "喜欢红茶", "shorthand": "茶友"}]}
    worker = ChannelMemoryMaintenance(lambda: channels, lambda: groups, summarize, lambda *a: None)
    assert asyncio.run(worker.consolidate(first.thread_id, listening=True))
    assert asyncio.run(worker.consolidate(second.thread_id, listening=True))
    first_url, second_url = memory_url(role.id, first.thread_id), memory_url(role.id, second.thread_id)
    one = client.get(first_url).json()["members"][0]
    two = client.get(second_url).json()["members"][0]
    assert one["key"] == two["key"]
    member_url = first_url + "/members/" + one["key"]
    detail = client.get(member_url).json()
    assert detail["nicknames"] == ["阿宁", "新昵称"]
    assert {source["threadId"] for source in detail["sources"]} == {first.thread_id, second.thread_id}
    saved = client.put(member_url, json={"profile": "爱红茶，也爱读书", "shorthand": "茶友书友", "expectedRevision": detail["revision"]})
    assert saved.status_code == 200
    assert client.get(second_url + "/members/" + one["key"]).json()["profile"] == "爱红茶，也爱读书"
    assert "爱红茶，也爱读书" in groups.prompt_context(role.id, second.thread_id, "22")


def test_deleting_a_profile_invalidates_old_consolidation_but_fresh_learning_can_rebuild(channel_api):
    role, channels, _, client = channel_api
    channels.register_channel_account(role.id, "qq", account_id="qq", enabled=True, allow_group=True)
    thread = channels.ensure_channel_thread(role.id, "qq", "qq", "100", "group")
    channels.set_group_listening(role.id, "qq", "100", True)
    for index in range(100):
        channels.listening.hear(thread, "群友喜欢读书", str(index), {"senderId": "22", "senderName": "阿宁"})
    url = memory_url(role.id, thread.thread_id)
    calls = []
    async def summarize(_thread, messages, context, profiles):
        calls.append([message["id"] for message in messages])
        if len(calls) == 2:
            member = client.get(url).json()["members"][0]
            deleted = client.delete(url + "/members/" + member["key"], params={"expectedRevision": member["revision"]})
            assert deleted.status_code == 204
        if len(calls) == 3:
            assert profiles["22"]["profile"] == ""
        return {"members": [{"senderId": "22", "profile": "读书爱好者", "shorthand": "书友"}]}
    groups = MultichannelGroupRepository(channels)
    worker = ChannelMemoryMaintenance(lambda: channels, lambda: groups, summarize, lambda *a: None)
    assert asyncio.run(worker.consolidate(thread.thread_id, listening=True))
    assert not asyncio.run(worker.consolidate(thread.thread_id, listening=True))
    assert client.get(url).json()["members"] == []
    assert asyncio.run(worker.consolidate(thread.thread_id, listening=True))
    assert calls[1] == calls[2]
    assert client.get(url).json()["members"][0]["profile"] == "读书爱好者"


@pytest.mark.parametrize("chat_type", ["group", "private"])
def test_notes_use_actual_conversation_but_member_facts_exclude_owner(channel_api, chat_type):
    role, channels, _, client = channel_api
    channels.register_channel_account(role.id, "qq", account_id="qq", enabled=True, allow_group=True)
    thread = channels.ensure_channel_thread(role.id, "qq", "qq", "100" if chat_type == "group" else "22", chat_type)
    channels.append_event(thread, direction="inbound", content="主人提议读书" if chat_type == "group" else "群友喜欢读书",
        external_message_id="u1", metadata={"senderId": "11" if chat_type == "group" else "22", "senderIsOwner": chat_type == "group"})
    channels.append_event(thread, direction="outbound", content="角色已确认回复", external_message_id="a1", role="assistant", metadata={"deliveryStatus": "sent"})
    channels.append_event(thread, direction="outbound", content="未确认送达不能当作已说", external_message_id="a2", role="assistant", metadata={"deliveryStatus": "uncertain"})
    for index in range(17):
        channels.append_event(thread, direction="inbound", content="群友参与", external_message_id=f"u{index+2}", metadata={"senderId": "22"})
    observed = []
    async def summarize(_thread, messages, context, profiles):
        observed.extend(message["content"] for message in messages)
        assert "11" not in profiles
        return {"groupNote": "读书会笔记", "summary": "大家讨论了读书", "members": [
            {"senderId": "22", "profile": "爱读书"}, {"senderId": "11", "profile": "不应提取主人档案"}]}
    groups = MultichannelGroupRepository(channels)
    worker = ChannelMemoryMaintenance(lambda: channels, lambda: groups, summarize, lambda *a: None)
    assert asyncio.run(worker.consolidate(thread.thread_id, listening=False))
    assert "角色已确认回复" in observed
    assert "未确认送达不能当作已说" not in observed
    if chat_type == "group":
        assert "主人提议读书" in observed
    result = client.get(memory_url(role.id, thread.thread_id)).json()
    assert result["note"] == "读书会笔记" and result["activity"]["text"] == "大家讨论了读书"
    assert [member["senderId"] for member in result["members"]] == ["22"]


def test_legacy_conflicting_profiles_require_confirmation_and_keep_verified_backup(channel_api, monkeypatch):
    role, channels, _, client = channel_api
    channels.register_channel_account(role.id, "qq", account_id="qq", enabled=True, allow_group=True)
    threads = [channels.ensure_channel_thread(role.id, "qq", "qq", chat, "group", title=chat) for chat in ("100", "200")]
    groups = MultichannelGroupRepository(channels)
    with channels._connect() as db:
        db.execute("DROP TABLE IF EXISTS channel_external_migrations")
        for thread, profile in zip(threads, ("喜欢红茶", "喜欢绿茶")):
            db.execute("INSERT INTO channel_member_profiles VALUES(?,?,?,?,?,?,?)",
                (role.id, thread.thread_id, "22", "模型伪造昵称", profile, "茶友", "2026-10-25T12:00:00+00:00"))
    for thread in threads:
        channels.append_event(thread, direction="inbound", content="真实原文", external_message_id="old",
            metadata={"senderId": "22", "senderName": "阿宁"})
    restarted = MultichannelStore(channels.database_path, channels.sessions, channels.credentials)
    monkeypatch.setattr(main, "multichannel_store", restarted)
    url = memory_url(role.id, threads[0].thread_id)
    members = client.get(url).json()["members"]
    assert len(members) == 1 and members[0]["state"] == "conflict"
    member_url = url + "/members/" + members[0]["key"]
    detail = client.get(member_url).json()
    assert detail["nicknames"] == ["阿宁"]
    assert {item["profile"] for item in detail["candidates"]} == {"喜欢红茶", "喜欢绿茶"}
    assert "喜欢红茶" not in MultichannelGroupRepository(restarted).prompt_context(role.id, threads[1].thread_id, "22")
    # Restarting cannot multiply candidates or publish their unconfirmed facts.
    restarted = MultichannelStore(channels.database_path, channels.sessions, channels.credentials)
    monkeypatch.setattr(main, "multichannel_store", restarted)
    assert len(client.get(member_url).json()["candidates"]) == 2
    # Automatic learning can add source evidence, but cannot resolve a migration conflict.
    restarted.set_group_listening(role.id, "qq", "100", True)
    for index in range(50):
        restarted.listening.hear(threads[0], "新的茶偏好", f"new-{index}", {"senderId": "22"})
    async def summarize(*args):
        return {"members": [{"senderId": "22", "profile": "模型擅自统一", "shorthand": "新线索"}]}
    worker = ChannelMemoryMaintenance(lambda: restarted, lambda: groups, summarize, lambda *a: None)
    assert asyncio.run(worker.consolidate(threads[0].thread_id, listening=True))
    detail = client.get(member_url).json()
    assert detail["state"] == "conflict" and len(detail["candidates"]) == 3
    assert "模型擅自统一" not in MultichannelGroupRepository(restarted).prompt_context(role.id, threads[1].thread_id, "22")
    saved = client.put(member_url, json={"profile": "喝红茶和绿茶", "shorthand": "茶友", "expectedRevision": detail["revision"]})
    assert saved.status_code == 200 and saved.json()["state"] == "published"
    assert "喝红茶和绿茶" in MultichannelGroupRepository(restarted).prompt_context(role.id, threads[1].thread_id, "22")
    assert len(restarted.sessions.list_messages(threads[0].session_key)) == 1
    backup = Path(channels.database_path).with_suffix(".before-external-memory-v1.sqlite3")
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*) FROM channel_member_profiles").fetchone()[0] == 2


def test_pairing_revokes_open_member_pages_and_inflight_learning(channel_api):
    role, channels, _, client = channel_api
    channels.register_channel_account(role.id, "qq", account_id="qq", enabled=True, allow_group=True, platform_id="99")
    thread = channels.ensure_channel_thread(role.id, "qq", "qq", "100", "group")
    private = channels.ensure_channel_thread(role.id, "qq", "qq", "22", "private")
    channels.set_group_listening(role.id, "qq", "100", True)
    for index in range(100):
        channels.listening.hear(thread, "访客谈论读书", str(index), {"senderId": "22"})
    calls = []
    async def summarize(*args):
        calls.append(True)
        if len(calls) == 2:
            code = channels.create_pairing_code()
            assert channels.consume_pairing_code(code, role_id=role.id, channel="qq", scope="platform",
                platform_user_id="22", account_id="qq", chat_id="22")
        return {"members": [{"senderId": "22", "profile": "爱读书"}]}
    groups = MultichannelGroupRepository(channels)
    worker = ChannelMemoryMaintenance(lambda: channels, lambda: groups, summarize, lambda *a: None)
    assert asyncio.run(worker.consolidate(thread.thread_id, listening=True))
    url = memory_url(role.id, thread.thread_id)
    detail = client.get(url).json()["members"][0]
    assert not asyncio.run(worker.consolidate(thread.thread_id, listening=True))
    member_url = url + "/members/" + detail["key"]
    assert client.get(member_url).status_code == 403
    assert client.put(member_url, json={"profile": "旧页面", "shorthand": "", "expectedRevision": detail["revision"]}).status_code == 403
    assert client.delete(member_url, params={"expectedRevision": detail["revision"]}).status_code == 403
    assert client.get(memory_url(role.id, private.thread_id)).status_code == 403
    assert client.get(url).json()["members"] == []
    assert "爱读书" not in groups.prompt_context(role.id, thread.thread_id, "22")
