import asyncio
from datetime import datetime, timezone

import pytest

from backend.app import main
from backend.app.channels import ChannelHost
from backend.app.channels import ChannelAdapterError
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore
from backend.app.agent_runtime import ActiveRunError, RuntimeManager
from backend.app.models import AgentRun
from channels.test_host import FakeAdapter, make_target

pytest_plugins = ["test_channel_api"]


@pytest.fixture(autouse=True)
def isolated_channel_settings_runtime(monkeypatch):
    monkeypatch.setattr(main, "_managed_qq_pending", {})
    monkeypatch.setattr(main, "channel_platform_locks", {})


class RecordingNapCatTransport:
    instances = []

    def __init__(self, endpoint, **_kwargs):
        self.endpoint = endpoint
        self.connected = False
        self.instances.append(self)

    async def start(self, _handler):
        self.connected = True

    async def stop(self):
        self.connected = False

    async def wait_connected(self):
        pass

    async def __call__(self, action, _params):
        if action == "get_login_info":
            return {"user_id": self.endpoint, "nickname": "QQ"}
        if action == "get_status":
            return {"online": True, "good": True}
        return {"data": []}


class FakeManagedNapCat:
    """Process/login boundary: no QQ installation or network in API tests."""
    def __init__(self):
        self.owner = {}
        self.online = True
        self.stopped = []

    def begin(self, role_id):
        ref = "a" * 32
        self.owner[ref] = role_id
        return ref

    def check(self, ref, role_id):
        if ref not in self.owner:
            raise KeyError(ref)
        if self.owner[ref] != role_id:
            raise PermissionError("other role")

    async def connect(self, ref, *, role_id, expected_uin=""):
        self.check(ref, role_id)

    async def status(self, ref, *, role_id):
        self.check(ref, role_id)
        return {"ref": ref, "available": True, "preparation": {"stage": "ready", "percent": 100, "version": "test"},
                "login": {"phase": "online" if self.online else "login_required", "uin": "10001" if self.online else "", "nickname": "QQ", "qrcode": ""}}

    async def refresh_qrcode(self, ref, *, role_id):
        return await self.status(ref, role_id=role_id)

    def endpoint(self, ref, *, role_id):
        self.check(ref, role_id)
        return "ws://127.0.0.1:6709", "private-managed-token"

    def remember_identity(self, ref, *, role_id, uin):
        self.check(ref, role_id)
        if uin != "10001":
            raise ValueError("wrong identity")

    async def stop(self, ref, *, role_id):
        self.check(ref, role_id)
        self.stopped.append(ref)

    async def delete(self, ref, *, role_id):
        await self.stop(ref, role_id=role_id)
        del self.owner[ref]


class ManagedNapCatTransport(RecordingNapCatTransport):
    async def __call__(self, action, params):
        if action == "get_login_info":
            return {"user_id": "10001", "nickname": "QQ"}
        return await super().__call__(action, params)


def test_platform_switch_persists_without_changing_account_intent(channel_api, monkeypatch):
    role, channels, credentials, client = channel_api
    account = channels.register_channel_account(role.id, "qq", account_id="qq-settings", enabled=True)

    response = client.post("/api/channels/settings", json={"channel": "qq", "enabled": False})

    assert response.status_code == 200
    setting = next(item for item in response.json()["channels"] if item["channel"] == "qq")
    assert setting["enabled"] is False
    public_account = client.get(f"/api/roles/{role.id}/channels/accounts").json()["accounts"][0]
    assert public_account["enabled"] is True
    assert public_account["platformEnabled"] is False
    assert public_account["effectiveEnabled"] is False

    restarted = MultichannelStore(channels.database_path, SessionStore(channels.database_path), credentials)
    monkeypatch.setattr(main, "multichannel_store", restarted)
    monkeypatch.setattr(main, "channel_host", ChannelHost(restarted))
    restored = client.get("/api/channels/settings")
    assert restored.status_code == 200
    assert next(item for item in restored.json()["channels"] if item["channel"] == "qq")["enabled"] is False
    assert restarted.get_account(account.account_id).enabled


def test_disabled_platform_rejects_events_and_sends_but_preserves_history(channel_api):
    role, channels, _, client = channel_api
    account = channels.register_channel_account(role.id, "qq", account_id="qq-gate", enabled=True)
    target = make_target(account.account_id)
    adapter = FakeAdapter(account.account_id, target)
    host = main.channel_host
    host.register(adapter)
    asyncio.run(host.account_targets(role.id, account.account_id))
    asyncio.run(host.account_send(role.id, account.account_id, target.id, "before pause", actor="owner"))
    thread = channels.get_thread_by_chat(role.id, account.account_id, target.chat_id)

    assert client.post("/api/channels/settings", json={"channel": "qq", "enabled": False}).status_code == 200

    host = ChannelHost(channels)
    host.register(adapter)
    assert not host.accepts_events(account.account_id)
    with pytest.raises(ChannelAdapterError):
        asyncio.run(host.account_send(role.id, account.account_id, target.id, "after pause", actor="owner"))
    assert [message.content for message in channels.sessions.list_messages(thread.session_key)] == ["before pause"]
    assert client.get(f"/api/roles/{role.id}/channels/threads").json()["threads"]


def test_global_pairing_code_can_be_issued_without_an_account_and_replaces_previous(channel_api):
    role, channels, _, client = channel_api
    first = client.post("/api/channels/pairing")
    assert first.status_code == 200
    second = client.post("/api/channels/pairing")
    assert second.status_code == 200
    payload = second.json()
    assert payload["expiresInSeconds"] == 600
    expires = datetime.fromisoformat(payload["expiresAt"])
    assert 590 < (expires - datetime.now(timezone.utc)).total_seconds() <= 600
    account = channels.register_channel_account(role.id, "feishu", account_id="feishu-pair", enabled=True)
    pairing = dict(role_id=role.id, channel="feishu", scope="account", platform_user_id="owner", account_id=account.account_id, chat_id="owner-chat")
    assert not channels.consume_pairing_code(first.json()["code"], **pairing)
    assert channels.consume_pairing_code(payload["code"], **pairing)
    assert not channels.consume_pairing_code(payload["code"], **pairing)
    assert client.get("/api/channels/settings").json()["identities"][0]["platformUserId"] == "owner"


def test_platform_enable_preserves_disabled_accounts_and_restart_respects_gate(channel_api, monkeypatch):
    role, channels, _, client = channel_api
    monkeypatch.setattr(main, "NapCatWebSocketTransport", RecordingNapCatTransport)
    RecordingNapCatTransport.instances = []
    account = channels.register_channel_account(role.id, "qq", account_id="qq-active", enabled=True)
    channels.set_channel_config(account.account_id, {"endpoint": "ws://active.test"})
    inactive = channels.register_channel_account("another-role", "qq", account_id="qq-paused", enabled=False)

    assert client.post("/api/channels/settings", json={"channel": "qq", "enabled": False}).status_code == 200
    # Start the application while the platform is off: no connection is opened.
    asyncio.run(main._start_channel_workers())
    assert not RecordingNapCatTransport.instances
    assert client.post("/api/channels/settings", json={"channel": "qq", "enabled": True}).status_code == 200
    assert len(RecordingNapCatTransport.instances) == 1
    assert RecordingNapCatTransport.instances[0].connected
    assert not channels.get_account(inactive.account_id).enabled
    assert client.post("/api/channels/settings", json={"channel": "qq", "enabled": False}).status_code == 200
    assert not RecordingNapCatTransport.instances[0].connected
    assert channels.get_account(account.account_id).enabled


def test_queued_channel_turn_rechecks_switch_after_waiting_for_role(channel_api):
    role, channels, _, _ = channel_api
    account = channels.register_channel_account(role.id, "qq", account_id="qq-queued", enabled=True)
    thread = channels.ensure_channel_thread(role.id, "qq", account.account_id, "visitor", "private")
    _, message = channels.append_event(thread, direction="inbound", content="queued", external_message_id="queued")
    manager = RuntimeManager(channels.sessions)
    run = AgentRun(runId="queued-run", roleId=role.id, sessionKey=thread.session_key, status="created", startedAt=datetime.now(timezone.utc))

    async def scenario():
        lock = manager.lock_for(role.id)
        await lock.acquire()
        waiting = asyncio.create_task(manager.reserve_existing_user_message(
            run, message.id, can_start=lambda: channels.account_effectively_enabled(channels.get_account(account.account_id)),
        ))
        await asyncio.sleep(0)
        channels.set_channel_enabled("qq", False)
        lock.release()
        with pytest.raises(ActiveRunError):
            await waiting
        assert channels.sessions.get_run(run.runId) is None
        assert not lock.locked()

    asyncio.run(scenario())


def test_managed_qq_login_can_be_committed_without_exposing_transport_secrets(channel_api, monkeypatch):
    role, channels, _, client = channel_api
    manager = FakeManagedNapCat()
    monkeypatch.setattr(main, "managed_napcat", manager, raising=False)
    monkeypatch.setattr(main, "NapCatWebSocketTransport", ManagedNapCatTransport)
    prefix = f"/api/roles/{role.id}/channels/qq/managed"
    begun = client.post(f"{prefix}/begin")
    assert begun.status_code == 200
    ref = begun.json()["ref"]
    assert client.get(f"{prefix}/{ref}").json()["login"]["phase"] == "online"
    committed = client.post(f"{prefix}/{ref}/commit", json={"displayName": "Phone QQ"})
    assert committed.status_code == 200
    account = committed.json()["account"]
    assert account["mode"] == "managed"
    assert account["managedRef"] == ref
    assert account["platformId"] == "10001"
    assert account["enabled"] is True
    assert "private-managed-token" not in committed.text
    assert "ws://127.0.0.1" not in committed.text
    saved = client.put(f"/api/roles/{role.id}/channels/qq", json={"displayName": "Updated QQ", "allowGroup": False})
    assert saved.status_code == 200
    assert saved.json()["account"]["mode"] == "managed"
    assert saved.json()["account"]["platformId"] == "10001"
    assert saved.json()["account"]["allowGroup"] is False
    assert client.delete(f"{prefix}/{ref}").status_code == 409
    assert client.delete(f"/api/roles/{role.id}/channels/accounts/{account['accountId']}").status_code == 204
    assert ref not in manager.owner


def test_managed_qq_cancel_is_role_scoped_and_cannot_commit_before_login(channel_api, monkeypatch):
    role, channels, _, client = channel_api
    manager = FakeManagedNapCat()
    manager.online = False
    monkeypatch.setattr(main, "managed_napcat", manager, raising=False)
    prefix = f"/api/roles/{role.id}/channels/qq/managed"
    ref = client.post(f"{prefix}/begin").json()["ref"]
    other = main.store.create(main.RoleInput(name="other", profile=role.profile))
    assert client.get(f"/api/roles/{other.id}/channels/qq/managed/{ref}").status_code == 404
    assert client.post(f"{prefix}/{ref}/commit", json={}).status_code == 409
    assert channels.get_role_account(role.id, "qq") is None
    assert client.post(f"{prefix}/{ref}/refresh").status_code == 200
    assert client.delete(f"{prefix}/{ref}").status_code == 204
    assert client.get(f"{prefix}/{ref}").status_code == 404


def test_managed_qq_refuses_onebot_identity_different_from_scanned_account(channel_api, monkeypatch):
    role, channels, _, client = channel_api
    manager = FakeManagedNapCat()
    monkeypatch.setattr(main, "managed_napcat", manager, raising=False)

    class WrongIdentity(ManagedNapCatTransport):
        async def __call__(self, action, params):
            if action == "get_login_info":
                return {"user_id": "20002", "nickname": "Other QQ"}
            return await super().__call__(action, params)

    monkeypatch.setattr(main, "NapCatWebSocketTransport", WrongIdentity)
    prefix = f"/api/roles/{role.id}/channels/qq/managed"
    ref = client.post(f"{prefix}/begin").json()["ref"]
    response = client.post(f"{prefix}/{ref}/commit", json={})
    assert response.status_code == 422
    assert channels.get_role_account(role.id, "qq") is None
    assert client.delete(f"{prefix}/{ref}").status_code == 204


def test_stop_failure_keeps_platform_disabled_and_account_preference(channel_api, monkeypatch):
    role, channels, _, client = channel_api

    class StopFailure(RecordingNapCatTransport):
        async def stop(self):
            raise RuntimeError("socket refused to close")

    monkeypatch.setattr(main, "NapCatWebSocketTransport", StopFailure)
    account = channels.register_channel_account(role.id, "qq", account_id="qq-stop-failure", enabled=True)
    assert client.post("/api/channels/settings", json={"channel": "qq", "enabled": True}).status_code == 200
    assert client.post("/api/channels/settings", json={"channel": "qq", "enabled": False}).status_code == 503
    settings = client.get("/api/channels/settings").json()
    assert next(item for item in settings["channels"] if item["channel"] == "qq")["enabled"] is False
    assert channels.get_account(account.account_id).enabled
    assert not main.channel_host.accepts_events(account.account_id)


@pytest.mark.parametrize("payload", [{"channel": {}}, {"channel": "qq", "enabled": "false"}, {"channel": "desktop", "enabled": False}])
def test_invalid_platform_settings_are_rejected(channel_api, payload):
    _, _, _, client = channel_api
    assert client.post("/api/channels/settings", json=payload).status_code == 422
