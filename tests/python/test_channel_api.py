from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.channels import ChannelHost
from backend.app.channels.credentials import credential_key
from backend.app.models import RoleInput, RoleProfile
from backend.app.multichannel import MultichannelStore
from backend.app.role_store import RoleStore
from backend.app.session_store import SessionStore


class MemoryCredentials:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str:
        return self.values.get(key, "")

    def set(self, key: str, value: str) -> None:
        if value:
            self.values[key] = value
        else:
            self.values.pop(key, None)


@pytest.fixture
def channel_api(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="测试角色", profile=RoleProfile(profile="核心设定")))
    sessions = SessionStore(tmp_path / "sessions.db")
    credentials = MemoryCredentials()
    channels = MultichannelStore(tmp_path / "sessions.db", sessions, credentials)
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "multichannel_store", channels)
    monkeypatch.setattr(main, "channel_host", ChannelHost(channels))
    monkeypatch.setattr(main, "channel_adapters", {})
    monkeypatch.setattr(main, "channel_transports", {})
    monkeypatch.setattr(main, "telegram_services", {})
    monkeypatch.setattr(main, "telegram_tasks", {})
    monkeypatch.setattr(main, "telegram_stops", {})
    monkeypatch.setattr(main, "channel_tasks", {})
    return role, channels, credentials, TestClient(main.app)


@pytest.mark.parametrize("account_state", ["absent", "disabled", "unconfigured"])
def test_desktop_capabilities_hide_channel_tools_without_usable_account(channel_api, account_state):
    role, channels, _, _ = channel_api
    if account_state != "absent":
        channels.save_account(
            role.id, account_id="telegram-unavailable", enabled=account_state != "disabled",
        )

    capabilities = asyncio.run(main._runtime_capabilities(
        role, session_key=f"role:{role.id}", run_id="desktop-no-channel",
    ))
    names = {definition.name for definition in capabilities.snapshot.tools}

    assert names.isdisjoint({"account_list", "account_targets", "account_send"})
    asyncio.run(capabilities.close())


def test_desktop_capabilities_expose_channel_tools_with_usable_account(channel_api):
    role, channels, _, _ = channel_api
    channels.register_channel_account(role.id, "qq", account_id="qq-enabled", enabled=True)

    capabilities = asyncio.run(main._runtime_capabilities(
        role, session_key=f"role:{role.id}", run_id="desktop-channel",
    ))
    names = {definition.name for definition in capabilities.snapshot.tools}

    assert {"account_list", "account_targets", "account_send"} <= names
    asyncio.run(capabilities.close())


def test_channel_thread_capabilities_keep_channel_tools(channel_api):
    role, _, _, _ = channel_api
    capabilities = asyncio.run(main._runtime_capabilities(
        role, session_key="thread:telegram:source:chat", run_id="channel-thread", external_only=True,
    ))

    assert capabilities.get("account_send") is not None
    asyncio.run(capabilities.close())


def test_connection_test_does_not_register_or_leave_a_disabled_napcat_running(channel_api, monkeypatch):
    role, channels, _, client = channel_api
    account = channels.register_channel_account(role.id, "qq", account_id="qq-test", enabled=False)
    channels.set_channel_config(account.account_id, {"endpoint": "ws://napcat.test:6700"})
    candidates: list[Any] = []

    class CandidateTransport:
        def __init__(self, *_: object, **__: object) -> None:
            self.connected = False
            self.stopped = False
            candidates.append(self)

        async def start(self, handler) -> None:
            self.connected = True

        async def stop(self) -> None:
            self.connected = False
            self.stopped = True

    monkeypatch.setattr(main, "NapCatWebSocketTransport", CandidateTransport)
    response = client.post(f"/api/roles/{role.id}/channels/qq/{account.account_id}/test")

    assert response.status_code == 200
    assert response.json()["account"]["enabled"] is False
    assert candidates[0].stopped
    assert not main.channel_adapters
    assert not main.channel_transports
    with pytest.raises(KeyError):
        main.channel_host.adapter(account.account_id)


def test_failed_connection_test_cleans_candidate_and_preserves_live_adapter(channel_api, monkeypatch):
    role, channels, credentials, client = channel_api
    account = channels.register_channel_account(role.id, "qqbot", account_id="qqbot-test", enabled=True)
    channels.set_channel_config(account.account_id, {"appId": "app-1"})
    credentials.set(credential_key("qqbot", account.account_id, "client_secret"), "secret")
    live_adapter, live_transport = object(), object()
    main.channel_adapters[account.account_id] = live_adapter
    main.channel_transports[account.account_id] = live_transport
    candidates: list[Any] = []

    class CandidateTransport:
        def __init__(self, *_: object, **__: object) -> None:
            self.stopped = False
            candidates.append(self)

        async def _gateway_url(self) -> str:
            raise RuntimeError("gateway rejected credentials")

        async def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr(main, "QQBotGatewayTransport", CandidateTransport)
    response = client.post(f"/api/roles/{role.id}/channels/qqbot/{account.account_id}/test")

    assert response.status_code == 502
    assert candidates[0].stopped
    assert main.channel_adapters[account.account_id] is live_adapter
    assert main.channel_transports[account.account_id] is live_transport
    assert channels.get_account(account.account_id).enabled


def test_platform_configuration_validates_before_writing_new_credentials(channel_api):
    role, channels, credentials, client = channel_api
    response = client.put(
        f"/api/roles/{role.id}/channels/qqbot",
        json={"clientSecret": "must-not-leak", "appId": ""},
    )

    assert response.status_code == 422
    assert not credentials.values
    assert channels.get_role_account(role.id, "qqbot") is None


def test_platform_configuration_failure_restores_existing_account(channel_api, monkeypatch):
    role, channels, credentials, client = channel_api
    account = channels.register_channel_account(role.id, "qqbot", account_id="qqbot-rollback", enabled=True)
    channels.set_channel_config(account.account_id, {"appId": "old-app", "endpoint": "https://old.example"})
    credentials.set(credential_key("qqbot", account.account_id, "client_secret"), "old-secret")

    async def fail_start(_account_id: str) -> None:
        raise RuntimeError("new configuration rejected")

    monkeypatch.setattr(main, "_start_channel_account", fail_start)
    response = client.put(
        f"/api/roles/{role.id}/channels/qqbot",
        json={
            "accountId": account.account_id,
            "appId": "new-app",
            "endpoint": "https://new.example",
            "clientSecret": "new-secret",
            "enabled": True,
        },
    )

    assert response.status_code == 502
    restored = channels.get_account(account.account_id)
    assert restored is not None and restored.enabled
    assert channels.channel_config(account.account_id) == {"appId": "old-app", "endpoint": "https://old.example"}
    assert credentials.get(credential_key("qqbot", account.account_id, "client_secret")) == "old-secret"


def test_telegram_test_endpoint_uses_telegram_service(channel_api, monkeypatch):
    role, channels, _, client = channel_api
    account = channels.save_account(role.id, account_id="telegram-test", token="secret")
    tested: list[str] = []

    class TelegramService:
        async def test_account(self, account_id: str):
            tested.append(account_id)
            return {"id": 42, "username": "test_bot"}

    monkeypatch.setattr(main, "_telegram_service", lambda account_id: TelegramService())
    response = client.post(f"/api/roles/{role.id}/channels/telegram/{account.account_id}/test")

    assert response.status_code == 200
    assert response.json()["bot"]["id"] == 42
    assert tested == [account.account_id]


def test_feishu_webhook_rejects_unknown_and_disabled_accounts(channel_api):
    role, channels, _, client = channel_api
    account = channels.register_channel_account(role.id, "feishu", account_id="feishu-test", enabled=False)

    assert client.post("/api/channels/feishu/missing/webhook", json={}).status_code == 404
    assert client.post(f"/api/channels/feishu/{account.account_id}/webhook", json={}).status_code == 409
    assert not main.channel_adapters


def test_feishu_webhook_checks_raw_body_signature_before_dispatch(channel_api, monkeypatch):
    role, channels, credentials, client = channel_api
    account = channels.register_channel_account(role.id, "feishu", account_id="feishu-test", enabled=True)
    channels.set_channel_config(account.account_id, {"appId": "app-1"})
    for name, value in (("app_secret", "secret"), ("verification_token", "verify"), ("encrypt_key", "encrypt")):
        credentials.set(credential_key("feishu", account.account_id, name), value)
    received: list[Any] = []

    async def handle(event) -> None:
        received.append(event)

    monkeypatch.setattr(main, "_handle_channel_event", handle)
    payload = {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1", "event_id": "event-1", "token": "verify"},
        "event": {
            "sender": {"sender_id": {"open_id": "user-1"}},
            "message": {"message_id": "message-1", "chat_id": "chat-1", "chat_type": "p2p", "content": '{"text":"你好"}'},
        },
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    signature = hashlib.sha256(b"123nonceencrypt" + raw).hexdigest()
    headers = {"X-Lark-Request-Timestamp": "123", "X-Lark-Request-Nonce": "nonce", "X-Lark-Signature": signature}
    endpoint = f"/api/channels/feishu/{account.account_id}/webhook"
    try:
        invalid = client.post(endpoint, content=raw, headers={**headers, "X-Lark-Signature": "bad"})
        assert invalid.status_code == 400
        assert not received
        valid = client.post(endpoint, content=raw, headers=headers)
        assert valid.status_code == 200
        assert valid.json() == {"code": 0}
        assert received[0].content == "你好"
        assert received[0].sender_id == "user-1"
        assert channels.list_targets(role.id, account.account_id)[0].chat_id == "chat-1"
    finally:
        transport = main.channel_transports.get(account.account_id)
        if transport is not None:
            asyncio.run(transport.stop())


def test_feishu_webhook_challenge_and_invalid_json_do_not_create_threads(channel_api):
    role, channels, credentials, client = channel_api
    account = channels.register_channel_account(role.id, "feishu", account_id="feishu-test", enabled=True)
    channels.set_channel_config(account.account_id, {"appId": "app-1"})
    credentials.set(credential_key("feishu", account.account_id, "app_secret"), "secret")
    credentials.set(credential_key("feishu", account.account_id, "verification_token"), "verify")
    endpoint = f"/api/channels/feishu/{account.account_id}/webhook"
    try:
        malformed = client.post(endpoint, content=b"{")
        assert malformed.status_code == 400
        bad_token = client.post(endpoint, json={"type": "url_verification", "token": "bad", "challenge": "challenge"})
        assert bad_token.status_code == 400
        challenge = client.post(endpoint, json={"type": "url_verification", "token": "verify", "challenge": "challenge"})
        assert challenge.status_code == 200
        assert challenge.json() == {"challenge": "challenge"}
        assert not channels.list_threads(role.id)
    finally:
        transport = main.channel_transports.get(account.account_id)
        if transport is not None:
            asyncio.run(transport.stop())


def test_feishu_long_connection_only_requires_app_credentials(channel_api):
    role, channels, credentials, _ = channel_api
    account = channels.register_channel_account(role.id, "feishu", account_id="feishu-long")
    channels.set_channel_config(account.account_id, {"appId": "app-1"})
    credentials.set(credential_key("feishu", account.account_id, "app_secret"), "secret")

    assert main._channel_credentials_configured(account)


def test_delete_channel_account_stops_transport_and_clears_all_secrets(channel_api, monkeypatch):
    role, channels, credentials, client = channel_api
    account = channels.register_channel_account(role.id, "feishu", account_id="feishu-test", enabled=True)
    channels.set_channel_config(account.account_id, {"appId": "app-1"})
    for name in ("app_secret", "verification_token", "encrypt_key"):
        credentials.set(credential_key("feishu", account.account_id, name), name)
    stopped: list[str] = []

    class Adapter:
        account_id = account.account_id
        channel = "feishu"

        async def stop(self):
            stopped.append(self.account_id)

    adapter = Adapter()
    main.channel_host.register(adapter)
    main.channel_adapters[account.account_id] = adapter
    main.channel_transports[account.account_id] = object()
    response = client.delete(f"/api/roles/{role.id}/channels/accounts/{account.account_id}")

    assert response.status_code == 204
    assert stopped == [account.account_id]
    assert channels.get_account(account.account_id) is None
    assert channels.channel_config(account.account_id) == {}
    assert not credentials.values
    assert not main.channel_adapters
    assert not main.channel_transports
    with pytest.raises(KeyError):
        main.channel_host.adapter(account.account_id)


def test_delete_keeps_disabled_account_when_any_credential_cleanup_fails(channel_api):
    role, channels, credentials, client = channel_api
    account = channels.register_channel_account(role.id, "feishu", account_id="feishu-retry", enabled=True)
    channels.set_channel_config(account.account_id, {"appId": "app-1"})
    names = ("app_secret", "verification_token", "encrypt_key")
    for name in names:
        credentials.set(credential_key("feishu", account.account_id, name), name)

    original_set = credentials.set
    attempted: list[str] = []

    def flaky_set(key: str, value: str) -> None:
        attempted.append(key)
        if key == credential_key("feishu", account.account_id, "verification_token") and not value:
            raise OSError("credential vault unavailable")
        original_set(key, value)

    credentials.set = flaky_set
    endpoint = f"/api/roles/{role.id}/channels/accounts/{account.account_id}"
    failed = client.delete(endpoint)

    assert failed.status_code == 503
    assert set(attempted) == {credential_key("feishu", account.account_id, name) for name in names}
    remaining = channels.get_account(account.account_id)
    assert remaining is not None and not remaining.enabled
    assert credentials.get(credential_key("feishu", account.account_id, "verification_token")) == "verification_token"
    assert credentials.get(credential_key("feishu", account.account_id, "app_secret")) == ""
    assert credentials.get(credential_key("feishu", account.account_id, "encrypt_key")) == ""

    credentials.set = original_set
    retried = client.delete(endpoint)
    assert retried.status_code == 204
    assert channels.get_account(account.account_id) is None
    assert not credentials.values


def test_disabling_account_closes_admission_and_waits_for_active_event(channel_api):
    role, channels, _, _ = channel_api
    account = channels.register_channel_account(role.id, "qq", account_id="qq-drain", enabled=True)
    stopped: list[str] = []

    class Adapter:
        account_id = account.account_id
        channel = "qq"

        async def stop(self):
            stopped.append(self.account_id)

    main.channel_host.register(Adapter())

    async def scenario() -> None:
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def active_event() -> None:
            async with main.channel_host.event_scope(account.account_id) as admitted:
                assert admitted
                entered.set()
                await finish.wait()

        event_task = asyncio.create_task(active_event())
        await entered.wait()
        disable_task = asyncio.create_task(main.set_channel_account_state(role.id, account.account_id, {"enabled": False}))
        for _ in range(20):
            if account.account_id in main.channel_host._paused_accounts:
                break
            await asyncio.sleep(0)

        assert not disable_task.done()
        async with main.channel_host.event_scope(account.account_id) as admitted:
            assert not admitted
        finish.set()
        await event_task
        result = await asyncio.wait_for(disable_task, timeout=1)
        assert result["account"]["enabled"] is False

    asyncio.run(scenario())
    assert stopped == [account.account_id]
