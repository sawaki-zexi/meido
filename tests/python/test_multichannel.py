from __future__ import annotations

import asyncio
import json

import pytest

from backend.app.multichannel import MultichannelStore, TelegramChannelService
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


class FakeTelegram:
    def __init__(self):
        self.sent = []

    async def get_me(self, token):
        assert token == "bot-secret"
        return {"id": 42, "username": "meido_bot"}

    async def send_message(self, token, chat_id, text, *, reply_to=None):
        self.sent.append((token, chat_id, text, reply_to))
        return {"message_id": 9001}


def make_store(tmp_path):
    credentials = MemoryCredentials()
    sessions = SessionStore(tmp_path / "sessions.db")
    store = MultichannelStore(tmp_path / "sessions.db", sessions, credentials)
    return store, sessions, credentials


def test_channel_account_keeps_token_out_of_database_and_deletes_it(tmp_path):
    store, _, credentials = make_store(tmp_path)
    account = store.save_account("role-1", token="bot-secret", enabled=True)

    assert store.token(account.account_id) == "bot-secret"
    assert account.public_dict()["tokenConfigured"] is True
    assert "bot-secret" not in json.dumps(account.public_dict())
    assert credentials.values

    store.delete_account(account.account_id)
    assert store.token(account.account_id) == ""
    assert store.get_account(account.account_id) is None


def test_channel_history_keeps_account_snapshot_after_delete_and_recreate(tmp_path):
    store, sessions, _ = make_store(tmp_path)
    account = store.register_channel_account(
        "role-1", "qq", account_id="old-qq", platform_id="123", display_name="旧账号",
    )
    thread = store.ensure_channel_thread("role-1", "qq", account.account_id, "group-1", "group")
    _, message = store.append_event(
        thread, direction="inbound", content="旧账号收到的消息", external_message_id="m-1",
        metadata={"viaAccount": {"displayName": "伪造来源"}},
    )
    assert message.metadata["viaAccount"]["displayName"] == "旧账号"
    assert message.metadata["viaAccount"]["platformAccountId"] == "123"
    assert thread.public_dict()["viaAccount"]["accountId"] == "old-qq"

    store.delete_account(account.account_id)
    store.register_channel_account("role-1", "qq", account_id="new-qq", display_name="新账号")
    historical = store.get_thread("role-1", thread.thread_id)
    assert historical.public_dict()["accountRemoved"] is True
    assert historical.public_dict()["viaAccount"]["displayName"] == "旧账号"
    saved = sessions.list_messages(thread.session_key)[0]
    assert saved.metadata["viaAccount"] == message.metadata["viaAccount"]
    assert store.list_messages("role-1", thread.thread_id)[1][0].content == "旧账号收到的消息"


def test_thread_creation_rejects_account_owned_by_other_role_or_platform(tmp_path):
    store, _, _ = make_store(tmp_path)
    account = store.register_channel_account("role-1", "qq", account_id="qq-1")
    with pytest.raises(KeyError):
        store.ensure_channel_thread("role-2", "qq", account.account_id, "group-1", "group")
    with pytest.raises(KeyError):
        store.ensure_channel_thread("role-1", "feishu", account.account_id, "group-1", "group")


def test_pairing_is_single_use_and_owner_identity_is_account_scoped(tmp_path):
    store, _, _ = make_store(tmp_path)
    account = store.save_account("role-1", token="bot-secret", enabled=True)
    code = store.create_pairing_code("role-1", ttl_seconds=600)

    assert store.consume_pairing_code(code, role_id="role-1", channel="telegram", scope="platform", platform_user_id="user-7", account_id=account.account_id, chat_id="user-7")
    assert not store.consume_pairing_code(code, role_id="role-1", channel="telegram", scope="platform", platform_user_id="user-8", account_id=account.account_id, chat_id="user-8")
    assert store.identity_is_owner(role_id="role-1", channel="telegram", scope="platform", platform_user_id="user-7", account_id=account.account_id)
    assert not store.identity_is_owner(role_id="role-2", channel="telegram", scope="platform", platform_user_id="user-7", account_id=account.account_id)


def test_telegram_private_messages_are_deduplicated_and_isolated_by_chat(tmp_path):
    store, sessions, _ = make_store(tmp_path)
    account = store.save_account("role-1", token="bot-secret", enabled=True)
    received = []
    service = TelegramChannelService(store, FakeTelegram(), lambda thread, text, metadata, owner: received.append((thread, text, owner)))
    first = {"update_id": 11, "message": {"message_id": 21, "text": "hello", "chat": {"id": 100, "type": "private", "first_name": "Alice"}, "from": {"id": 100, "first_name": "Alice"}}}
    other = {"update_id": 12, "message": {"message_id": 22, "text": "other", "chat": {"id": 200, "type": "private", "first_name": "Bob"}, "from": {"id": 200, "first_name": "Bob"}}}

    assert asyncio.run(service.handle_update(account.account_id, first))
    assert not asyncio.run(service.handle_update(account.account_id, first))
    assert asyncio.run(service.handle_update(account.account_id, other))
    threads = store.list_threads("role-1")
    assert len(threads) == 2
    assert threads[0].session_key != threads[1].session_key
    contents_by_chat = {
        thread.chat_id: [item.content for item in sessions.list_messages(thread.session_key)]
        for thread in threads
    }
    assert contents_by_chat == {"100": ["hello"], "200": ["other"]}
    assert [item[1] for item in received] == ["hello", "other"]
    assert [item[2] for item in received] == [False, False]
    assert {target.chat_id for target in store.list_targets("role-1", account.account_id)} == {"100", "200"}


def test_role_deletion_clears_credentials_for_all_channel_types(tmp_path):
    from backend.app.channels.credentials import credential_key

    store, _, credentials = make_store(tmp_path)
    telegram = store.save_account("role-1", token="telegram-secret")
    qq = store.register_channel_account("role-1", "qq", account_id="qq-1")
    qqbot = store.register_channel_account("role-1", "qqbot", account_id="qqbot-1")
    feishu = store.register_channel_account("role-1", "feishu", account_id="feishu-1")
    keys = {
        credential_key("telegram", telegram.account_id, "bot_token"): "telegram-secret",
        credential_key("qq", qq.account_id, "access_token"): "qq-secret",
        credential_key("qqbot", qqbot.account_id, "client_secret"): "qqbot-secret",
        credential_key("feishu", feishu.account_id, "app_secret"): "feishu-secret",
        credential_key("feishu", feishu.account_id, "verification_token"): "verify-secret",
        credential_key("feishu", feishu.account_id, "encrypt_key"): "encrypt-secret",
    }
    for key, value in keys.items():
        credentials.set(key, value)

    store.delete_role("role-1")

    assert all(credentials.get(key) == "" for key in keys)


def test_role_deletion_keeps_account_rows_when_credential_cleanup_fails(tmp_path):
    store, _, credentials = make_store(tmp_path)
    account = store.register_channel_account("role-1", "qqbot", account_id="qqbot-1")
    from backend.app.channels.credentials import credential_key

    key = credential_key("qqbot", account.account_id, "client_secret")
    credentials.set(key, "secret")
    original_set = credentials.set

    def fail_clear(name, value):
        if name == key and not value:
            raise OSError("vault unavailable")
        original_set(name, value)

    credentials.set = fail_clear
    try:
        try:
            store.delete_role("role-1")
        except RuntimeError:
            pass
        else:
            raise AssertionError("credential cleanup failure must be retryable")
        assert store.get_account(account.account_id) is not None
        assert credentials.get(key) == "secret"
    finally:
        credentials.set = original_set


def test_pair_command_does_not_enter_history_and_reply_uses_source_thread(tmp_path):
    store, sessions, _ = make_store(tmp_path)
    account = store.save_account("role-1", token="bot-secret", enabled=True)
    telegram = FakeTelegram()
    code = store.create_pairing_code("role-1")
    service = TelegramChannelService(store, telegram)
    update = {"update_id": 1, "message": {"message_id": 10, "text": f"/pair {code}", "chat": {"id": 100, "type": "private", "first_name": "Alice"}, "from": {"id": 100, "first_name": "Alice"}}}

    assert asyncio.run(service.handle_update(account.account_id, update))
    thread = store.list_threads("role-1")[0]
    assert sessions.list_messages(thread.session_key) == []
    assert telegram.sent == [("bot-secret", "100", "配对成功。", None)]


def test_thread_pagination_rejects_other_role_and_uses_stable_sequence(tmp_path):
    store, _, _ = make_store(tmp_path)
    account = store.save_account("role-1", token="bot-secret", enabled=True)
    thread = store.ensure_thread("role-1", account.account_id, "100", "private")
    for index in range(5):
        store.append_event(thread, direction="inbound", content=f"m{index}", external_message_id=str(index))

    _, page, has_more, cursor = store.list_messages("role-1", thread.thread_id, limit=2)
    assert [message.content for message in page] == ["m3", "m4"]
    assert has_more is True
    _, older, has_more, cursor = store.list_messages("role-1", thread.thread_id, before=cursor, limit=2)
    assert [message.content for message in older] == ["m1", "m2"]
    assert has_more is True
    try:
        store.list_messages("role-2", thread.thread_id)
    except KeyError:
        pass
    else:
        raise AssertionError("cross-role thread lookup must fail")
