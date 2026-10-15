import asyncio
from datetime import datetime, timezone

import pytest
import httpx

from backend.app import main
from backend.app.channels import ChannelHost
from backend.app.channels import ChannelAdapterError
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore
from backend.app.agent_runtime import ActiveRunError, RuntimeManager
from backend.app.models import AgentRun
from channels.test_host import FakeAdapter, make_target
from test_channel_api import channel_api as channel_api


@pytest.fixture(autouse=True)
def isolated_channel_settings_runtime(monkeypatch):
    monkeypatch.setattr(main, "_managed_qq_pending", {})
    monkeypatch.setattr(main, "channel_platform_locks", {})


class RecordingNapCatTransport:
    instances = []

    def __init__(self, endpoint, **_kwargs):
        self.endpoint = endpoint
        self.connected = False
        self.online = None
        self.observed_at = None
        self.instances.append(self)

    async def start(self, _handler):
        self.connected = True
        self.observed_at = datetime.now(timezone.utc).isoformat()

    async def stop(self):
        self.connected = False
        self.online = None

    def connection_snapshot(self):
        return {"connection": "connected" if self.connected else "disconnected",
                "online": self.online, "observedAt": self.observed_at, "reason": "fixture_transport_observation"}

    async def wait_connected(self):
        pass

    async def __call__(self, action, _params):
        if action == "get_login_info":
            return {"user_id": self.endpoint, "nickname": "QQ"}
        if action == "get_status":
            self.online = True
            self.observed_at = datetime.now(timezone.utc).isoformat()
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


class RotatingManagedNapCat(FakeManagedNapCat):
    def __init__(self):
        super().__init__()
        self.tokens = {}
        self.generation = 0
        self.process_failed = False

    def endpoint(self, ref, *, role_id):
        self.check(ref, role_id)
        if ref not in self.tokens:
            self.generation += 1
            self.tokens[ref] = f"private-managed-token-{self.generation}"
        return "ws://127.0.0.1:6709", self.tokens[ref]

    async def connect(self, ref, *, role_id, expected_uin=""):
        self.endpoint(ref, role_id=role_id)
        self.process_failed = False

    async def status(self, ref, *, role_id):
        payload = await super().status(ref, role_id=role_id)
        if self.process_failed:
            payload["login"] = {"phase": "error", "qrcode": "", "error": "QQ process exited"}
        return payload

    async def stop(self, ref, *, role_id):
        await super().stop(ref, role_id=role_id)
        self.tokens.pop(ref, None)

    def process_exited(self, ref):
        # A real managed process forgets its IPC credentials in _run's finally.
        self.tokens.pop(ref, None)
        self.process_failed = True


def test_managed_qq_process_exit_is_reported_and_user_can_retry(channel_api, monkeypatch):
    role, channels, _, _ = channel_api
    manager = RotatingManagedNapCat()
    monkeypatch.setattr(main, "managed_napcat", manager)
    monkeypatch.setattr(main, "NapCatWebSocketTransport", ManagedNapCatTransport)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            prefix = f"/api/roles/{role.id}/channels/qq/managed"
            ref = (await client.post(f"{prefix}/begin")).json()["ref"]
            committed = await client.post(f"{prefix}/{ref}/commit", json={})
            assert committed.status_code == 200
            account_id = committed.json()["account"]["accountId"]
            accounts_url = f"/api/roles/{role.id}/channels/accounts"
            # A healthy online monitor must not mask a connected account.
            assert (await client.get(accounts_url)).json()["accounts"][0]["connectionStatus"] == "connected"
            events = channels.subscribe(role.id)
            async def account_changed():
                async for event in events:
                    if event["type"] == "account":
                        return event
            changed = asyncio.create_task(account_changed())
            await asyncio.sleep(0)
            manager.process_exited(ref)
            try:
                for _ in range(100):
                    account = (await client.get(accounts_url)).json()["accounts"][0]
                    if account["connectionStatus"] == "error":
                        break
                    await asyncio.sleep(.01)
                assert account["connectionStatus"] == "error"
                assert account["lastError"]
                assert (await asyncio.wait_for(changed, 1))["type"] == "account"
                retried = await client.post(f"{accounts_url}/{account_id}/state", json={"enabled": True})
                assert retried.status_code == 200
                assert retried.json()["account"]["connectionStatus"] == "connected"
            finally:
                changed.cancel()
                await asyncio.gather(changed, return_exceptions=True)
                await events.aclose()
                await client.post(f"{accounts_url}/{account_id}/state", json={"enabled": False})

    asyncio.run(scenario())


def test_managed_qq_retry_waits_for_fault_cleanup_and_keeps_new_connection(channel_api, monkeypatch):
    role, channels, _, _ = channel_api
    manager = RotatingManagedNapCat()
    monkeypatch.setattr(main, "managed_napcat", manager)

    async def scenario():
        cleanup_started = asyncio.Event()
        finish_cleanup = asyncio.Event()
        event_started = asyncio.Event()
        finish_event = asyncio.Event()
        delayed_stop = {"active": False, "held": False}

        class PausedCleanupTransport(ManagedNapCatTransport):
            async def stop(self):
                if delayed_stop["active"] and not delayed_stop["held"]:
                    delayed_stop["held"] = True
                    cleanup_started.set()
                    await finish_cleanup.wait()
                await super().stop()

        monkeypatch.setattr(main, "NapCatWebSocketTransport", PausedCleanupTransport)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            prefix = f"/api/roles/{role.id}/channels/qq/managed"
            ref = (await client.post(f"{prefix}/begin")).json()["ref"]
            committed = await client.post(f"{prefix}/{ref}/commit", json={})
            assert committed.status_code == 200
            account_id = committed.json()["account"]["accountId"]
            accounts_url = f"/api/roles/{role.id}/channels/accounts"
            state_url = f"{accounts_url}/{account_id}/state"

            async def admitted_event():
                async with main.channel_host.event_scope(account_id) as admitted:
                    assert admitted
                    event_started.set()
                    await finish_event.wait()

            active_event = asyncio.create_task(admitted_event())
            await event_started.wait()
            events = channels.subscribe(role.id)

            async def fault_published():
                async for event in events:
                    if event["type"] == "account":
                        return event

            published = asyncio.create_task(fault_published())
            await asyncio.sleep(0)
            delayed_stop["active"] = True
            manager.process_exited(ref)
            retry = None
            try:
                await asyncio.wait_for(published, 2)
                retry = asyncio.create_task(client.post(state_url, json={"enabled": True}))
                finish_event.set()
                await asyncio.wait_for(cleanup_started.wait(), 2)
                completed, _ = await asyncio.wait({retry}, timeout=.05)
                assert not completed, "retry completed before the predecessor's cleanup released its lifecycle lock"
                finish_cleanup.set()
                assert (await asyncio.wait_for(retry, 2)).status_code == 200
                await asyncio.sleep(0)
                account = (await client.get(accounts_url)).json()["accounts"][0]
                assert account["connectionStatus"] == "connected"
                assert main.channel_host.accepts_events(account_id)
            finally:
                finish_event.set()
                finish_cleanup.set()
                delayed_stop["active"] = False
                await active_event
                if retry is not None:
                    await asyncio.gather(retry, return_exceptions=True)
                published.cancel()
                await asyncio.gather(published, return_exceptions=True)
                await events.aclose()
                await client.post(state_url, json={"enabled": False})

    asyncio.run(scenario())


def test_managed_qq_requires_scan_again_without_claiming_connected(channel_api, monkeypatch):
    role, channels, _, _ = channel_api
    manager = RotatingManagedNapCat()
    monkeypatch.setattr(main, "managed_napcat", manager)
    monkeypatch.setattr(main, "NapCatWebSocketTransport", ManagedNapCatTransport)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            prefix = f"/api/roles/{role.id}/channels/qq/managed"
            ref = (await client.post(f"{prefix}/begin")).json()["ref"]
            committed = await client.post(f"{prefix}/{ref}/commit", json={})
            assert committed.status_code == 200
            account_id = committed.json()["account"]["accountId"]
            accounts_url = f"/api/roles/{role.id}/channels/accounts"
            events = channels.subscribe(role.id)

            async def account_changed():
                async for event in events:
                    if event["type"] == "account":
                        return event

            changed = asyncio.create_task(account_changed())
            await asyncio.sleep(0)
            manager.online = False
            try:
                await asyncio.wait_for(changed, 2)
                account = (await client.get(accounts_url)).json()["accounts"][0]
                assert account["connectionStatus"] == "connecting"
                assert (await client.get(f"{prefix}/{ref}")).json()["login"]["phase"] == "login_required"
                changed = asyncio.create_task(account_changed())
                await asyncio.sleep(0)
                manager.online = True
                await asyncio.wait_for(changed, 2)
                assert (await client.get(accounts_url)).json()["accounts"][0]["connectionStatus"] == "connected"
            finally:
                changed.cancel()
                await asyncio.gather(changed, return_exceptions=True)
                await events.aclose()
                await client.post(f"{accounts_url}/{account_id}/state", json={"enabled": False})

    asyncio.run(scenario())

@pytest.mark.parametrize("restart", ["account", "platform", "process_exit"])
def test_managed_qq_reconnect_uses_current_process_credentials(channel_api, monkeypatch, restart):
    role, channels, _, client = channel_api
    manager = RotatingManagedNapCat()
    monkeypatch.setattr(main, "managed_napcat", manager)

    class AuthenticatedTransport(ManagedNapCatTransport):
        def __init__(self, endpoint, *, access_token):
            super().__init__(endpoint)
            self.access_token = access_token

        async def start(self, handler):
            if self.access_token != manager.endpoint("a" * 32, role_id=role.id)[1]:
                raise main.ChannelTransportError("OneBot authentication rejected")
            await super().start(handler)

    monkeypatch.setattr(main, "NapCatWebSocketTransport", AuthenticatedTransport)
    prefix = f"/api/roles/{role.id}/channels/qq/managed"
    ref = client.post(f"{prefix}/begin").json()["ref"]
    committed = client.post(f"{prefix}/{ref}/commit", json={})
    assert committed.status_code == 200
    account_id = committed.json()["account"]["accountId"]
    state_url = f"/api/roles/{role.id}/channels/accounts/{account_id}/state"
    thread = channels.ensure_channel_thread(role.id, "qq", account_id, "owner-chat", "private")
    channels.append_event(thread, direction="inbound", content="keep history", external_message_id="keep")

    if restart == "platform":
        assert client.post("/api/channels/settings", json={"channel": "qq", "enabled": False}).status_code == 200
        response = client.post("/api/channels/settings", json={"channel": "qq", "enabled": True})
    else:
        if restart == "account":
            assert client.post(state_url, json={"enabled": False}).status_code == 200
        else:
            manager.process_exited(ref)
        response = client.post(state_url, json={"enabled": True})

    assert response.status_code == 200
    account = client.get(f"/api/roles/{role.id}/channels/accounts").json()["accounts"][0]
    assert account["connectionStatus"] == "connected"
    assert account["enabled"] is True
    assert [message.content for message in channels.sessions.list_messages(thread.session_key)] == ["keep history"]


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


def test_legacy_desktop_thread_and_messages_remain_readable_without_channel_avatar_ids(channel_api):
    role, channels, _, client = channel_api
    session_key = f"role:{role.id}"
    timestamp = datetime.now(timezone.utc).isoformat()
    channels.register_desktop_thread(role.id, session_key, timestamp, timestamp)
    channels.sessions.append_message(
        session_key, "user", "desktop history", "completed",
        metadata={"senderId": "desktop-owner", "viaAccount": {"platformAccountId": "desktop"}},
    )

    threads = client.get(f"/api/roles/{role.id}/channels/threads")
    assert threads.status_code == 200
    desktop = threads.json()["threads"][0]
    assert desktop["channel"] == "desktop"
    assert desktop["avatarUrl"] is None
    messages = client.get(f"/api/roles/{role.id}/channels/threads/{desktop['threadId']}/messages")
    assert messages.status_code == 200
    message = messages.json()["messages"][0]
    assert message["content"] == "desktop history"
    assert message["metadata"]["senderAvatarUrl"] is None
    assert message["metadata"]["viaAccount"]["avatarUrl"] is None


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


def test_managed_qq_delete_can_retry_after_credential_vault_recovers(channel_api, monkeypatch, tmp_path):
    from backend.app.channels.napcat_managed import ManagedNapCat

    role, channels, credentials, client = channel_api
    manager = ManagedNapCat(tmp_path)
    monkeypatch.setattr(main, "managed_napcat", manager)
    ref = manager.begin(role.id)
    account = channels.register_channel_account(role.id, "qq", account_id="managed-delete-retry", enabled=False)
    channels.set_channel_config(account.account_id, {"managedRef": ref})
    original_set = credentials.set

    def unavailable_vault(_key, _value):
        raise RuntimeError("vault unavailable")

    monkeypatch.setattr(credentials, "set", unavailable_vault)
    endpoint = f"/api/roles/{role.id}/channels/accounts/{account.account_id}"
    assert client.delete(endpoint).status_code == 503
    assert client.get(f"/api/roles/{role.id}/channels/accounts").json()["accounts"][0]["enabled"] is False
    monkeypatch.setattr(credentials, "set", original_set)
    assert client.delete(endpoint).status_code == 204
    assert client.get(f"/api/roles/{role.id}/channels/accounts").json()["accounts"] == []


def test_managed_qq_explains_unsupported_platform_and_manual_alternative(channel_api, monkeypatch):
    class UnsupportedPlatform(FakeManagedNapCat):
        async def connect(self, ref, *, role_id, expected_uin=""):
            raise RuntimeError("托管 NapCat 仅支持 Windows x64")

    role, _, _, client = channel_api
    monkeypatch.setattr(main, "managed_napcat", UnsupportedPlatform())
    response = client.post(f"/api/roles/{role.id}/channels/qq/managed/begin")
    assert response.status_code == 422
    assert "Windows x64" in response.json()["detail"]
    assert "手动" in response.json()["detail"]


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
