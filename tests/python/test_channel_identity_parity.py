from __future__ import annotations

import pytest

from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore


class MemoryCredentials:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        if value:
            self.values[key] = value
        else:
            self.values.pop(key, None)


def make_store(tmp_path):
    sessions = SessionStore(tmp_path / "sessions.db")
    return MultichannelStore(tmp_path / "sessions.db", sessions, MemoryCredentials()), sessions


def pair(store, account, user="owner", scope="platform", chat="owner"):
    code = store.create_pairing_code(account.role_id, account.channel)
    assert store.consume_pairing_code(
        code, role_id=account.role_id, channel=account.channel, scope=scope,
        platform_user_id=user, account_id=account.account_id, chat_id=chat,
    )


def test_platform_owner_identity_applies_to_other_role_accounts_without_sharing_history(tmp_path):
    store, _ = make_store(tmp_path)
    first = store.register_channel_account("r1", "qq", account_id="qq-first", platform_id="101")
    second = store.register_channel_account("r2", "qq", account_id="qq-second", platform_id="102")
    pair(store, first)
    assert store.identity_is_owner(role_id="r2", channel="qq", scope="platform", platform_user_id="owner", account_id=second.account_id)
    assert not store.identity_is_owner(role_id="r1", channel="qq", scope="platform", platform_user_id="owner", account_id=second.account_id)
    thread = store.ensure_channel_thread("r1", "qq", first.account_id, "owner", "private")
    store.append_event(thread, direction="inbound", content="只属于 r1 的历史", external_message_id="old")
    store.remember_owner_chat(role_id="r2", channel="qq", scope="platform", platform_user_id="owner", account_id=second.account_id, chat_id="owner")
    assert store.owner_context("r2", exclude_thread_id="other") == []


def test_account_owner_identity_does_not_apply_to_another_application(tmp_path):
    store, _ = make_store(tmp_path)
    first = store.register_channel_account("r1", "feishu", account_id="feishu-first", platform_id="app-1")
    second = store.register_channel_account("r2", "feishu", account_id="feishu-second", platform_id="app-2")
    pair(store, first, scope="account")
    assert store.identity_is_owner(role_id="r1", channel="feishu", scope="account", platform_user_id="owner", account_id=first.account_id)
    assert not store.identity_is_owner(role_id="r2", channel="feishu", scope="account", platform_user_id="owner", account_id=second.account_id)


def test_unbinding_revokes_owner_targets_and_context_for_every_platform_account(tmp_path):
    from backend.app.channels.models import TargetKind, target_from_chat

    store, _ = make_store(tmp_path)
    accounts = [store.register_channel_account(role, "qq", account_id=f"qq-{role}") for role in ("r1", "r2")]
    pair(store, accounts[0])
    for account in accounts:
        store.remember_owner_chat(role_id=account.role_id, channel="qq", scope="platform", platform_user_id="owner", account_id=account.account_id, chat_id="owner")
        store.remember_targets(account.role_id, [target_from_chat(account_id=account.account_id, chat_id="owner", kind=TargetKind.PRIVATE)])
        assert store.owner_target(account.role_id, account.account_id) is not None
    identities = store.list_identities()
    assert len(identities) == 1
    assert identities[0]["accountId"] is None
    store.unbind_identity(str(identities[0]["identityId"]))
    assert store.list_identities() == []
    for account in accounts:
        assert not store.identity_is_owner(role_id=account.role_id, channel="qq", scope="platform", platform_user_id="owner", account_id=account.account_id)
        assert store.owner_target(account.role_id, account.account_id) is None
        assert store.owner_chat_context_since(role_id=account.role_id, channel="qq", account_id=account.account_id, chat_id="owner") is None
    with pytest.raises(KeyError):
        store.unbind_identity(str(identities[0]["identityId"]))


def test_deleting_original_role_keeps_global_platform_identity(tmp_path):
    store, _ = make_store(tmp_path)
    first = store.register_channel_account("r1", "qq", account_id="qq-first")
    second = store.register_channel_account("r2", "qq", account_id="qq-second")
    pair(store, first)
    store.delete_role("r1")
    assert store.identity_is_owner(role_id="r2", channel="qq", scope="platform", platform_user_id="owner", account_id=second.account_id)


def test_same_platform_account_cannot_be_registered_to_another_role(tmp_path):
    store, _ = make_store(tmp_path)
    store.register_channel_account("r1", "qqbot", account_id="qqbot-first", platform_id="app-1")
    with pytest.raises(ValueError, match="平台账号"):
        store.register_channel_account("r2", "qqbot", account_id="qqbot-second", platform_id="app-1")
    assert store.get_role_account("r2", "qqbot") is None


def test_telegram_resolved_identity_is_unique_and_cannot_replace_an_existing_bot(tmp_path):
    store, _ = make_store(tmp_path)
    first = store.save_account("r1", token="token-first")
    second = store.save_account("r2", token="token-second")
    store.set_platform_identity(first.account_id, "bot-1", display_name="Bot One")
    with pytest.raises(ValueError, match="平台账号"):
        store.set_platform_identity(second.account_id, "bot-1")
    with pytest.raises(ValueError, match="删除"):
        store.set_platform_identity(first.account_id, "bot-2")
    assert store.get_account(first.account_id).platform_id == "bot-1"


def test_repairing_keeps_the_first_private_history_boundary(tmp_path):
    store, _ = make_store(tmp_path)
    account = store.register_channel_account("r1", "qq", account_id="qq-first")
    pair(store, account)
    since = store.owner_chat_context_since(role_id="r1", channel="qq", account_id=account.account_id, chat_id="owner")
    identity_id = store.list_identities()[0]["identityId"]
    pair(store, account)
    assert store.owner_chat_context_since(role_id="r1", channel="qq", account_id=account.account_id, chat_id="owner") == since
    assert store.list_identities()[0]["identityId"] == identity_id


def test_identity_list_and_unbind_api_revokes_a_global_binding(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.app import main

    store, _ = make_store(tmp_path)
    account = store.register_channel_account("r1", "qq", account_id="qq-first")
    pair(store, account)
    monkeypatch.setattr(main, "multichannel_store", store)
    client = TestClient(main.app)
    listed = client.get("/api/channels/identities")
    assert listed.status_code == 200
    identity = listed.json()["identities"][0]
    assert identity["scope"] == "platform"
    assert identity["platformUserId"] == "owner"
    assert client.delete(f"/api/channels/identities/{identity['identityId']}").status_code == 204
    assert client.get("/api/channels/identities").json() == {"identities": []}
    assert client.delete(f"/api/channels/identities/{identity['identityId']}").status_code == 404


def test_legacy_platform_bindings_migrate_to_one_global_identity(tmp_path):
    store, sessions = make_store(tmp_path)
    first = store.register_channel_account("r1", "qq", account_id="qq-first")
    second = store.register_channel_account("r2", "qq", account_id="qq-second")
    with store._connect() as db:
        db.execute("DROP INDEX channel_identity_global")
        for account, time in ((first, "2026-10-10T00:00:00+00:00"), (second, "2026-10-11T00:00:00+00:00")):
            db.execute("INSERT INTO channel_identities(identity_id,role_id,channel,scope,platform_user_id,account_id,owner,paired_at,context_since,target_chat_id,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (f"old-{account.role_id}", account.role_id, "qq", "platform", "owner", account.account_id, 1, time, time, "owner", time))
    migrated = MultichannelStore(tmp_path / "sessions.db", sessions, MemoryCredentials())
    assert len(migrated.list_identities()) == 1
    assert migrated.list_identities()[0]["identityId"] == "old-r1"
    assert migrated.identity_is_owner(role_id="r2", channel="qq", scope="platform", platform_user_id="owner", account_id=second.account_id)


def test_restart_does_not_register_a_channel_session_as_desktop_history(tmp_path):
    store, sessions = make_store(tmp_path)
    account = store.register_channel_account("r1", "qq", account_id="qq-first")
    thread = store.ensure_channel_thread("r1", "qq", account.account_id, "visitor", "private")
    store.append_event(thread, direction="inbound", content="访客私有消息", external_message_id="private")
    restarted = MultichannelStore(tmp_path / "sessions.db", sessions, MemoryCredentials())
    assert restarted.owner_context("r1", exclude_thread_id="owner-thread") == []


def test_pending_pairing_code_is_global_replaced_by_new_code_and_not_persisted(tmp_path):
    store, sessions = make_store(tmp_path)
    receiver = store.register_channel_account("r2", "feishu", account_id="feishu-second")
    old = store.create_pairing_code("r1", "telegram")
    current = store.create_pairing_code("r1", "qq")
    arguments = dict(role_id="r2", channel="feishu", scope="account", platform_user_id="owner", account_id=receiver.account_id, chat_id="owner")
    assert not store.consume_pairing_code(old, **arguments)
    assert store.consume_pairing_code(current, **arguments)
    assert not store.consume_pairing_code(current, **arguments)
    assert store.identity_is_owner(role_id="r2", channel="feishu", scope="account", platform_user_id="owner", account_id=receiver.account_id)
    pending = store.create_pairing_code("r1", "telegram")
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM channel_pairing_codes").fetchone()[0] == 0
    restarted = MultichannelStore(tmp_path / "sessions.db", sessions, MemoryCredentials())
    assert not restarted.consume_pairing_code(pending, **arguments)


def test_invalid_receiving_account_does_not_consume_a_global_pairing_code(tmp_path):
    store, _ = make_store(tmp_path)
    account = store.register_channel_account("r1", "qq", account_id="qq-first")
    code = store.create_pairing_code("r2", "feishu")
    assert not store.consume_pairing_code(code, role_id="r2", channel="qq", scope="platform", platform_user_id="owner", account_id=account.account_id, chat_id="owner")
    assert store.consume_pairing_code(code, role_id="r1", channel="qq", scope="platform", platform_user_id="owner", account_id=account.account_id, chat_id="owner")
