from __future__ import annotations

import json

from backend.app.channels.group_tools import MultichannelGroupRepository
from backend.app.memory_maintenance import MemoryMaintenance
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore


class Credentials:
    def get(self, key):
        return ""

    def set(self, key, value):
        pass


def setup(tmp_path):
    sessions = SessionStore(tmp_path / "sessions.db")
    store = MultichannelStore(tmp_path / "sessions.db", sessions, Credentials())
    account = store.register_channel_account("r1", "qq", account_id="qq-first")
    owner = store.ensure_channel_thread("r1", "qq", account.account_id, "owner", "private")
    visitor = store.ensure_channel_thread("r1", "qq", account.account_id, "visitor", "private")
    group = store.ensure_channel_thread("r1", "qq", account.account_id, "group", "group")
    store.append_event(owner, direction="inbound", content="配对前信息", external_message_id="prepair")
    code = store.create_pairing_code("r1", "qq")
    assert store.consume_pairing_code(code, role_id="r1", channel="qq", scope="platform", platform_user_id="owner", account_id=account.account_id, chat_id="owner")
    session = sessions.open_role_session("r1")
    store.register_desktop_thread("r1", session.sessionKey, session.createdAt.isoformat(), session.updatedAt.isoformat())
    sessions.append_message(session.sessionKey, "user", "桌面消息")
    sessions.append_message(session.sessionKey, "assistant", "桌面回复")
    store.append_event(owner, direction="inbound", content="主人私聊", external_message_id="owner")
    store.append_event(owner, direction="outbound", content="主人私聊回复", external_message_id="owner-reply", role="assistant", metadata={"deliveryStatus": "sent"})
    store.append_event(visitor, direction="inbound", content="访客秘密", external_message_id="visitor")
    store.append_event(group, direction="inbound", content="群聊秘密", external_message_id="group")
    maintenance = MemoryMaintenance(tmp_path / "roles", sessions, messages_provider=lambda role, _session: store.owner_context_messages(role))
    return store, sessions, maintenance, owner, visitor, group


def test_owner_markdown_includes_desktop_and_authorised_private_history(tmp_path):
    store, sessions, maintenance, owner, _, _ = setup(tmp_path)
    maintenance.maintain("r1", "role:r1")
    recent = (tmp_path / "roles/r1/memory/RECENT_CONTEXT.md").read_text(encoding="utf-8")
    assert "桌面消息" in recent and "主人私聊" in recent
    assert all(text not in recent for text in ("配对前信息", "访客秘密", "群聊秘密"))
    projected = store.owner_context_messages("r1")
    assert [message.sequence for message in projected] == sorted(message.sequence for message in projected)
    assert sessions.list_messages(owner.session_key)[-1].sequence == 3
    assert projected[-1].metadata["sourceSessionKey"] == owner.session_key


def test_unbinding_refreshes_recent_owner_context_and_new_desktop_messages_keep_their_order(tmp_path):
    store, sessions, maintenance, _, _, _ = setup(tmp_path)
    maintenance.maintain("r1", "role:r1")
    store.unbind_identity(str(store.list_identities()[0]["identityId"]))
    maintenance.maintain("r1", "role:r1")
    recent_path = tmp_path / "roles/r1/memory/RECENT_CONTEXT.md"
    assert "主人私聊" not in recent_path.read_text(encoding="utf-8")
    sessions.append_message("role:r1", "user", "解绑后新的桌面消息")
    sessions.append_message("role:r1", "assistant", "新的桌面回复")
    maintenance.maintain("r1", "role:r1")
    assert "解绑后新的桌面消息" in recent_path.read_text(encoding="utf-8")


def test_legacy_desktop_cursor_migrates_without_reconsolidating_old_messages(tmp_path):
    store, sessions, maintenance, _, _, _ = setup(tmp_path)
    memory_dir = tmp_path / "roles/r1/memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / ".maintenance.json").write_text(json.dumps({"lastSequence": 1}), encoding="utf-8")
    maintenance.maintain("r1", "role:r1")
    cursor = json.loads((memory_dir / ".maintenance.json").read_text(encoding="utf-8"))
    original = sessions.list_messages("role:r1")[0]
    mapped = next(message for message in store.owner_context_messages("r1") if message.id == original.id)
    assert cursor["lastSequence"] == mapped.sequence
    assert cursor["sequenceSource"] == "global-rowid"


def test_external_prompt_includes_only_profiles_observed_in_current_thread(tmp_path):
    from datetime import datetime, timezone

    store, _, _, _, _, first = setup(tmp_path)
    second = store.ensure_channel_thread("r1", "qq", "qq-first", "other-group", "group")
    repo = MultichannelGroupRepository(store)
    repo.update_context("r1", first.thread_id, group_note="当前群笔记", summary="当前群摘要")
    for thread, sender in ((first, "same-user"), (second, "other-user")):
        store.append_event(thread, direction="inbound", content="实际观察", external_message_id=sender,
            metadata={"senderId": sender, "senderName": "相同昵称"})
    # Simulate old data, then exercise the real migration and prompt boundary.
    with store._connect() as db:
        db.execute("DROP TABLE channel_external_migrations")
        for thread, sender, profile in ((first, "same-user", "当前群成员资料"), (second, "other-user", "其他群机密档案")):
            db.execute("INSERT INTO channel_member_profiles VALUES(?,?,?,?,?,?,?)", ("r1", thread.thread_id, sender, "昵称", profile, "短资料", datetime.now(timezone.utc).isoformat()))
    store = MultichannelStore(store.database_path, store.sessions, store.credentials)
    repo = MultichannelGroupRepository(store)
    prompt = repo.prompt_context("r1", first.thread_id, "same-user")
    assert "当前群笔记" in prompt and "当前群成员资料" in prompt
    assert "其他群机密档案" not in prompt
    assert "其他群机密档案" not in repo.prompt_context("r1", first.thread_id, "other-user")
    assert repo.prompt_context("r2", first.thread_id, "same-user") == ""


def test_interleaved_owner_transcripts_preserve_each_completed_turn(tmp_path):
    store, sessions, maintenance, owner, _, _ = setup(tmp_path)
    desktop_user = sessions.append_message("role:r1", "user", "同时到达的桌面问题")
    mobile_user = sessions.append_message(owner.session_key, "user", "同时到达的手机问题")
    sessions.append_message("role:r1", "assistant", "桌面回复")
    sessions.append_message(owner.session_key, "assistant", "手机回复", metadata={"deliveryStatus": "sent"})
    committed = maintenance._messages("r1", "role:r1")
    assert {desktop_user.id, mobile_user.id}.issubset({message.id for message in committed})


def test_owner_context_read_refreshes_revoked_private_raw_history_without_a_model(tmp_path, monkeypatch):
    from backend.app import main

    store, sessions, maintenance, _, _, _ = setup(tmp_path)
    maintenance.maintain("r1", "role:r1")
    monkeypatch.setattr(main, "multichannel_store", store)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "memory_maintenance", maintenance)
    monkeypatch.setattr(main, "roles_root", tmp_path / "roles")
    assert "主人私聊" in main._role_memory_context("r1")
    store.unbind_identity(str(store.list_identities()[0]["identityId"]))
    context = main._role_memory_context("r1")
    assert "主人私聊" not in context
    assert "桌面消息" in context


def test_pairing_does_not_promote_a_visitor_turn_completed_after_authorisation(tmp_path):
    store, sessions, _, owner, visitor, _ = setup(tmp_path)
    delayed_user = sessions.append_message(owner.session_key, "user", "授权前仍在执行的问题", run_id="delayed-visitor")
    delayed_reply = sessions.append_message(owner.session_key, "assistant", "授权后才完成的访客回答", run_id="delayed-visitor", metadata={"deliveryStatus": "sent"})
    allowed_user = sessions.append_message(owner.session_key, "user", "授权后的新问题", run_id="new-owner")
    allowed_reply = sessions.append_message(owner.session_key, "assistant", "授权后的新回答", run_id="new-owner", metadata={"deliveryStatus": "sent"})
    proactive = sessions.append_message(owner.session_key, "assistant", "授权后的主动提醒", metadata={"deliveryStatus": "sent"})
    orphan = sessions.append_message(owner.session_key, "assistant", "无法证明来源授权的回答", run_id="unknown-source", metadata={"deliveryStatus": "sent"})
    # A source in another transcript cannot authorise this conversation's run.
    sessions.append_message(visitor.session_key, "user", "其他会话的同名回合", run_id="unknown-source")
    with store._connect() as db:
        db.execute("UPDATE channel_owner_chats SET context_since=? WHERE chat_id=?", ("2026-10-15T01:00:00+00:00", "owner"))
        db.execute("UPDATE messages SET created_at=? WHERE message_id=?", ("2026-10-15T00:59:59+00:00", delayed_user.id))
        for message in (delayed_reply, allowed_user, allowed_reply, proactive, orphan):
            db.execute("UPDATE messages SET created_at=? WHERE message_id=?", ("2026-10-15T01:00:01+00:00", message.id))

    forbidden = {delayed_user.content, delayed_reply.content, orphan.content}
    expected = {allowed_user.content, allowed_reply.content, proactive.content}
    recent = {content for _, _, content, _ in store.owner_context("r1", exclude_thread_id="")}
    memory = {message.content for message in store.owner_context_messages("r1")}
    assert not forbidden.intersection(recent)
    assert not forbidden.intersection(memory)
    assert expected.issubset(recent)
    assert expected.issubset(memory)


def test_queued_owner_turns_keep_each_run_and_original_global_cursor(tmp_path):
    store, sessions, maintenance, owner, _, _ = setup(tmp_path)
    first_user = sessions.append_message(owner.session_key, "user", "排队前的第一个问题", run_id="queued-first")
    second_user = sessions.append_message(owner.session_key, "user", "提前保存的第二个问题", run_id="queued-second")
    first_reply = sessions.append_message(owner.session_key, "assistant", "第一个问题的回答", run_id="queued-first", metadata={"deliveryStatus": "sent"})
    second_reply = sessions.append_message(owner.session_key, "assistant", "第二个问题的回答", run_id="queued-second", metadata={"deliveryStatus": "sent"})
    expected_ids = [message.id for message in (first_user, second_user, first_reply, second_reply)]
    projection = store.owner_context_messages("r1")
    source_sequences = {message.id: message.sequence for message in projection}

    committed = maintenance._messages("r1", "role:r1")
    queued = [message for message in committed if message.id in expected_ids]
    assert [message.id for message in queued] == expected_ids
    assert [message.sequence for message in queued] == [source_sequences[message_id] for message_id in expected_ids]
    maintenance.maintain("r1", "role:r1")
    recent = (tmp_path / "roles/r1/memory/RECENT_CONTEXT.md").read_text(encoding="utf-8")
    assert all(message.content in recent for message in (first_user, second_user, first_reply, second_reply))


def test_completed_memory_turns_do_not_pair_across_sessions_or_run_ids(tmp_path):
    _, sessions, maintenance, owner, visitor, _ = setup(tmp_path)
    source = sessions.append_message(owner.session_key, "user", "等待自己回合的回答", run_id="same-id")
    other_session = sessions.append_message(visitor.session_key, "assistant", "另一个会话同名回合的回答", run_id="same-id")
    other_run = sessions.append_message(owner.session_key, "assistant", "同会话其他回合的回答", run_id="different-id")
    legacy_user = sessions.append_message(owner.session_key, "user", "旧格式问题")
    legacy_reply = sessions.append_message(owner.session_key, "assistant", "旧格式回答")

    committed = maintenance._committed_messages([source, other_session, other_run, legacy_user, legacy_reply])
    assert {message.id for message in committed} == {legacy_user.id, legacy_reply.id}


def test_external_profiles_prioritize_trigger_mentions_and_quote_over_busy_group(tmp_path):
    store, _, _, _, _, thread = setup(tmp_path)
    groups = MultichannelGroupRepository(store)
    senders = ["z-quoted", "z-mentioned", *(f"a-{index:02}" for index in range(12)), "z-trigger"]
    for sender in senders:
        _, message = store.append_event(thread, direction="inbound", content="观察到的群友",
            external_message_id=sender, metadata={"senderId": sender, "senderName": sender})
        with store._connect() as db:
            store.external_memory.members.write_in(db, thread, message.metadata["externalMemberKey"],
                profile=f"完整档案-{sender}", shorthand=f"速记-{sender}")
    prompt = groups.prompt_context("r1", thread.thread_id, "z-trigger",
        trigger_metadata={"mentionedIds": ["z-mentioned", "not-observed", "owner"], "replyToSenderId": "z-quoted", "replyContextVerified": True})
    assert all(f"完整档案-{sender}" in prompt for sender in ("z-trigger", "z-mentioned", "z-quoted"))
    assert "完整档案-a-11" not in prompt and "速记-a-11" in prompt
    assert prompt.index("速记-a-11") < prompt.index("速记-a-10")
    assert "not-observed" not in prompt and '"senderId": "owner"' not in prompt
    unverified = groups.prompt_context("r1", thread.thread_id, "z-trigger",
        trigger_metadata={"replyToSenderId": "z-quoted", "replyContextVerified": False})
    assert "完整档案-z-quoted" not in unverified
