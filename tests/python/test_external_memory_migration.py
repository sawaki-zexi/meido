import sqlite3
from datetime import datetime, timezone

import pytest

from backend.app import main
from backend.app.channels.group_tools import MultichannelGroupRepository
from backend.app.multichannel import MultichannelStore
from test_channel_api import channel_api as channel_api
from test_external_memory_api import memory_url


def test_migration_cannot_infer_old_application_from_current_account(channel_api, monkeypatch):
    role, channels, _, client = channel_api
    channels.register_channel_account(role.id, "feishu", account_id="app")
    old = channels.ensure_channel_thread(role.id, "feishu", "app", "old", "group")
    channels.append_event(old, direction="inbound", content="old", external_message_id="old", metadata={"senderId": "22", "senderName": "旧昵称"})
    old_time = datetime.now(timezone.utc).isoformat()
    channels.set_platform_identity("app", "verified-app")
    # Updating the thread snapshot must not rewrite its older message proof.
    channels.ensure_channel_thread(role.id, "feishu", "app", "old", "group")
    new = channels.ensure_channel_thread(role.id, "feishu", "app", "new", "group")
    channels.append_event(new, direction="inbound", content="new", external_message_id="new", metadata={"senderId": "22", "senderName": "新昵称"})
    MultichannelGroupRepository(channels)
    with channels._connect() as db:
        db.execute("DROP TABLE channel_external_migrations")
        for thread, profile, stamp in ((old, "旧应用的事实", old_time), (new, "可证明应用的事实", datetime.now(timezone.utc).isoformat())):
            db.execute("INSERT INTO channel_member_profiles VALUES(?,?,?,?,?,?,?)", (role.id, thread.thread_id, "22", "不可采信的模型名", profile, "", stamp))
    restarted = MultichannelStore(channels.database_path, channels.sessions, channels.credentials)
    monkeypatch.setattr(main, "multichannel_store", restarted)
    old_member = client.get(memory_url(role.id, old.thread_id)).json()["members"][0]
    new_member = client.get(memory_url(role.id, new.thread_id)).json()["members"][0]
    assert old_member["scope"] == "legacy:" + old.thread_id
    assert new_member["scope"] == "application:verified-app"
    assert old_member["key"] != new_member["key"]
    assert old_member["profile"] == "旧应用的事实" and new_member["profile"] == "可证明应用的事实"
    assert "旧应用的事实" not in MultichannelGroupRepository(restarted).prompt_context(role.id, new.thread_id, "22")


def test_migration_failure_rolls_back_and_retries_from_verified_backup(channel_api, monkeypatch):
    role, channels, _, client = channel_api
    channels.register_channel_account(role.id, "qq", account_id="qq")
    thread = channels.ensure_channel_thread(role.id, "qq", "qq", "100", "group")
    MultichannelGroupRepository(channels)
    with channels._connect() as db:
        db.execute("DROP TABLE channel_external_migrations")
        db.execute("INSERT INTO channel_member_profiles VALUES(?,?,?,?,?,?,?)",
            (role.id, thread.thread_id, "22", "", "旧资料", "", datetime.now(timezone.utc).isoformat()))
        db.execute("CREATE TRIGGER fail_memory_migration BEFORE UPDATE ON channel_external_members BEGIN SELECT RAISE(ABORT,'controlled write failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        MultichannelStore(channels.database_path, channels.sessions, channels.credentials)
    assert client.get(memory_url(role.id, thread.thread_id)).json()["members"] == []
    with channels._connect() as db:
        assert db.execute("SELECT profile FROM channel_member_profiles").fetchone()[0] == "旧资料"
        db.execute("DROP TRIGGER fail_memory_migration")
    restarted = MultichannelStore(channels.database_path, channels.sessions, channels.credentials)
    monkeypatch.setattr(main, "multichannel_store", restarted)
    members = client.get(memory_url(role.id, thread.thread_id)).json()["members"]
    assert len(members) == 1 and members[0]["profile"] == "旧资料"


def test_migration_uses_actual_observation_time_across_offsets(channel_api, monkeypatch):
    role, channels, _, client = channel_api
    channels.register_channel_account(role.id, "feishu", account_id="app")
    thread = channels.ensure_channel_thread(role.id, "feishu", "app", "group", "group")
    first = channels.append_event(thread, direction="inbound", content="早先应用的观察", external_message_id="first",
        metadata={"senderId": "22", "senderName": "早先昵称"})[1]
    channels.set_platform_identity("app", "second-app")
    thread = channels.ensure_channel_thread(role.id, "feishu", "app", "group", "group")
    later = channels.append_event(thread, direction="inbound", content="档案生成后的新应用", external_message_id="later",
        metadata={"senderId": "22", "senderName": "后来昵称"})[1]
    MultichannelGroupRepository(channels)
    with channels._connect() as db:
        db.execute("DROP TABLE channel_external_migrations")
        for table in ("channel_external_member_sources", "channel_external_members", "channel_external_revisions"):
            db.execute(f"DELETE FROM {table}")
        db.execute("UPDATE messages SET created_at=? WHERE message_id=?", ("2026-10-25T09:00:00+08:00", first.id))
        db.execute("UPDATE messages SET created_at=? WHERE message_id=?", ("2026-10-24T22:00:00-05:00", later.id))
        db.execute("INSERT INTO channel_member_profiles VALUES(?,?,?,?,?,?,?)",
            (role.id, thread.thread_id, "22", "", "早先应用的档案", "", "2026-10-25T02:00:00Z"))
    restarted = MultichannelStore(channels.database_path, channels.sessions, channels.credentials)
    monkeypatch.setattr(main, "multichannel_store", restarted)
    member = client.get(memory_url(role.id, thread.thread_id)).json()["members"][0]
    assert member["scope"] == "legacy:" + thread.thread_id
    assert member["nicknames"] == ["早先昵称"]
    assert member["sources"][0]["lastSeen"] == "2026-10-25T01:00:00+00:00"
