from __future__ import annotations

import sqlite3

import pytest

from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore


class Credentials:
    def get(self, _key):
        return ""

    def set(self, _key, _value):
        pass


def setup_store(tmp_path):
    sessions = SessionStore(tmp_path / "sessions.db")
    store = MultichannelStore(tmp_path / "sessions.db", sessions, Credentials())
    store.register_channel_account("role", "qq", account_id="qq", enabled=True)
    return store, sessions


def test_identical_numeric_private_and_group_ids_have_separate_history_and_dedupe(tmp_path):
    store, sessions = setup_store(tmp_path)
    private = store.ensure_channel_thread("role", "qq", "qq", "123", "private")
    group = store.ensure_channel_thread("role", "qq", "qq", "123", "group")
    assert private.thread_id != group.thread_id and private.session_key != group.session_key
    assert store.append_event(private, direction="inbound", content="私聊秘密", external_message_id="1")[0]
    assert store.append_event(group, direction="inbound", content="群内容", external_message_id="1")[0]
    private_context = sessions.runtime_messages(private.session_key)
    group_context = sessions.runtime_messages(group.session_key)
    assert len(private_context) == 1 and private_context[0].content.endswith("私聊秘密")
    assert len(group_context) == 1 and group_context[0].content.endswith("群内容")
    assert "群内容" not in private_context[0].content and "私聊秘密" not in group_context[0].content
    assert store.get_thread_by_chat("role", "qq", "123", chat_type="private").thread_id == private.thread_id
    assert store.get_thread_by_chat("role", "qq", "123", chat_type="group").thread_id == group.thread_id
    with pytest.raises(ValueError, match="chat_type"):
        store.get_thread_by_chat("role", "qq", "123")


def test_owner_authorization_and_listening_clear_never_match_numeric_group_alias(tmp_path):
    store, sessions = setup_store(tmp_path)
    private = store.ensure_channel_thread("role", "qq", "qq", "123", "private")
    group = store.ensure_channel_thread("role", "qq", "qq", "123", "group")
    code = store.create_pairing_code("role", "qq")
    assert store.consume_pairing_code(code, role_id="role", channel="qq", scope="platform", platform_user_id="123", account_id="qq", chat_id="123")
    store.append_event(private, direction="inbound", content="主人秘密", external_message_id="private-1")
    store.append_event(group, direction="inbound", content="群原文不可共享", external_message_id="group-1")
    assert [item[2] for item in store.owner_context("role", exclude_thread_id="none")] == ["主人秘密"]
    assert [item.content for item in store.owner_context_messages("role")] == ["主人秘密"]
    store.set_group_listening("role", "qq", "123", True)
    store.listening.hear(group, "旁听", "listening-1", {})
    assert store.clear_listening_messages("role", "qq", "123") == 1
    assert sessions.list_messages(private.session_key)[0].content == "主人秘密"


def test_only_confirmed_external_assistant_messages_enter_owner_shared_context(tmp_path):
    store, _ = setup_store(tmp_path)
    private = store.ensure_channel_thread("role", "qq", "qq", "123", "private")
    code = store.create_pairing_code("role", "qq")
    assert store.consume_pairing_code(code, role_id="role", channel="qq", scope="platform", platform_user_id="123", account_id="qq", chat_id="123")
    for index, status in enumerate((None, "pending", "uncertain", "failed", "sent")):
        store.append_event(private, direction="outbound", content=f"reply-{status}", external_message_id=str(index), role="assistant", metadata={"deliveryStatus": status})
    assert [item[2] for item in store.owner_context("role", exclude_thread_id="none")] == ["reply-sent"]
    assert [item.content for item in store.owner_context_messages("role")] == ["reply-sent"]


def test_legacy_unique_constraint_migrates_without_rewriting_existing_history(tmp_path):
    store, sessions = setup_store(tmp_path)
    original = store.ensure_channel_thread("role", "qq", "qq", "123", "private")
    _, message = store.append_event(original, direction="inbound", content="既有私聊", external_message_id="legacy")
    with sqlite3.connect(store.database_path) as db:
        db.execute("""CREATE TABLE channel_threads_legacy (
            thread_id TEXT PRIMARY KEY,session_key TEXT NOT NULL UNIQUE,role_id TEXT NOT NULL,
            channel TEXT NOT NULL,account_id TEXT NOT NULL,chat_id TEXT NOT NULL,chat_type TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',last_activity TEXT NOT NULL,metadata_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(role_id,channel,account_id,chat_id)
        )""")
        db.execute("INSERT INTO channel_threads_legacy SELECT * FROM channel_threads")
        db.execute("DROP TABLE channel_threads")
        db.execute("ALTER TABLE channel_threads_legacy RENAME TO channel_threads")
    restarted = MultichannelStore(store.database_path, sessions, Credentials())
    restored = restarted.ensure_channel_thread("role", "qq", "qq", "123", "private")
    group = restarted.ensure_channel_thread("role", "qq", "qq", "123", "group")
    assert restored.thread_id == original.thread_id and restored.session_key == original.session_key
    retained = sessions.list_messages(restored.session_key)
    assert retained[0].id == message.id and retained[0].sequence == message.sequence and retained[0].content == "既有私聊"
    assert group.thread_id != restored.thread_id
    twice = MultichannelStore(store.database_path, sessions, Credentials())
    assert twice.get_thread_by_chat("role", "qq", "123", chat_type="private").thread_id == original.thread_id
