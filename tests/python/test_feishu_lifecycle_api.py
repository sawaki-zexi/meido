"""Public account lifecycle retains and retires real Feishu-owned runners.

Only the private long-connection factory is controlled here.  The API,
ChannelHost, adapter, transport and connection thread remain production code.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import threading
from types import SimpleNamespace

import httpx
import pytest


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


class PendingConnection:
    """A cooperative, in-flight handshake on the runner's actual thread."""

    def __init__(self, on_event, *, resist_cancel=False, block_disconnect=False):
        self.on_event = on_event
        self.entered = threading.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()
        self.disconnected = threading.Event()
        self.disconnect_entered = threading.Event()
        self.release_disconnect = threading.Event()
        if not block_disconnect:
            self.release_disconnect.set()
        self.thread = None
        self.connected = False
        self.resist_cancel = resist_cancel

    async def connect(self):
        self.thread = threading.current_thread()
        self.entered.set()
        while not self.release.is_set():
            try:
                await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                self.cancelled.set()
                if not self.resist_cancel:
                    raise
        self.connected = True

    def is_connected(self):
        return self.connected

    async def keepalive(self):
        await asyncio.Future()

    async def disconnect(self):
        self.disconnect_entered.set()
        while not self.release_disconnect.is_set():
            await asyncio.sleep(0.005)
        self.connected = False
        self.disconnected.set()


async def wait_until(predicate, *, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.fixture
def feishu_api(tmp_path, monkeypatch, local_platform_only):
    # Set every application data root before importing main or constructing
    # transports.  The network fixture remains installed through our teardown.
    for key, child in (
        ("MEIDO_DATA_DIR", "data"),
        ("MEIDO_ROLES_DIR", "roles"),
        ("MEIDO_OWNER_KNOWLEDGE_DATA_DIR", "owner-knowledge"),
    ):
        monkeypatch.setenv(key, str((tmp_path / child).resolve()))
    main = importlib.import_module("backend.app.main")
    from backend.app.channels import ChannelHost
    from backend.app.channels.transports import FeishuWebhookTransport
    from backend.app.models import RoleInput, RoleProfile
    from backend.app.multichannel import MultichannelStore
    from backend.app.role_store import RoleStore
    from backend.app.session_store import SessionStore

    roles = RoleStore(tmp_path / "roles")
    sessions = SessionStore(tmp_path / "sessions.db")
    credentials = MemoryCredentials()
    channels = MultichannelStore(tmp_path / "sessions.db", sessions, credentials)
    host = ChannelHost(channels)
    for name, value in {
        "store": roles, "session_store": sessions,
        "multichannel_store": channels, "channel_host": host,
        "channel_adapters": {}, "channel_transports": {},
        "telegram_services": {}, "telegram_tasks": {}, "telegram_stops": {},
        "channel_tasks": {}, "channel_event_locks": {},
        "channel_lifecycle_locks": {}, "channel_platform_locks": {},
        "_channel_roles_deleting": set(),
    }.items():
        monkeypatch.setattr(main, name, value)
    # Avatar fetches are independent of handshake/ownership and must never
    # select a production platform endpoint in this lifecycle fixture.
    monkeypatch.setattr(main, "_refresh_channel_avatars", lambda *_args, **_kwargs: None)
    closed_services = []

    async def close_maintenance():
        closed_services.append("maintenance")

    async def close_managed_qq():
        closed_services.append("managed-qq")

    async def start_maintenance():
        pass

    async def cleanup_deleted_roles(_roles):
        pass

    # Those unrelated services are not started by an ASGITransport request.
    # Keep the actual registered channel-shutdown callback under test without
    # closing singleton services belonging to other API fixtures.
    monkeypatch.setattr(main, "channel_memory_maintenance", SimpleNamespace(start=start_maintenance, close=close_maintenance))
    monkeypatch.setattr(main, "managed_napcat", SimpleNamespace(cleanup_deleted_roles=cleanup_deleted_roles, close=close_managed_qq))
    connections, transports = [], []
    faults = {"start_after_runner": False, "stop": False,
              "resist_cancel": False, "block_disconnect": False}

    def connection_factory(on_event):
        connection = PendingConnection(on_event, resist_cancel=faults["resist_cancel"],
                                       block_disconnect=faults["block_disconnect"])
        connections.append(connection)
        return connection

    class ControlledTransport(FeishuWebhookTransport):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, long_connection_factory=connection_factory)
            self._fixture_start_failed = False
            transports.append(self)

        async def start(self, handler):
            await super().start(handler)
            if faults["start_after_runner"] is True or faults["start_after_runner"] == self.app_id:
                await wait_until(lambda: bool(connections) and connections[-1].entered.is_set())
                self._fixture_start_failed = True
                raise RuntimeError("controlled failure after runner ownership")

        async def stop(self):
            if (faults["stop"] is True or faults["stop"] == self.app_id
                    or faults["stop"] == "failed_start" and self._fixture_start_failed):
                # Explicit ownership failure seam: the real runner is alive;
                # its stop implementation is tested separately and on all
                # ordinary lifecycle paths below.
                raise RuntimeError("controlled cleanup refusal")
            await super().stop()

    monkeypatch.setattr(main, "FeishuWebhookTransport", ControlledTransport)
    role = roles.create(RoleInput(name="飞书生命周期", profile=RoleProfile(profile="本地测试")))
    other_role = roles.create(RoleInput(name="隔离角色", profile=RoleProfile(profile="本地测试")))
    env = SimpleNamespace(main=main, roles=roles, role=role, other_role=other_role,
                          channels=channels, sessions=sessions, credentials=credentials,
                          host=host, connections=connections, transports=transports,
                          faults=faults, closed_services=closed_services)
    yield env

    async def cleanup():
        # Even a failed old-version RED releases its controlled connect and
        # joins the exact thread; never leave daemon work outside the guard.
        faults.update(start_after_runner=False, stop=False)
        for connection in connections:
            connection.release.set()
            connection.release_disconnect.set()
        # Cleanup failures must not prevent joining the original fixture-owned
        # threads (including a deliberately broken-version RED).
        cleanup_results = await asyncio.gather(*(transport.stop() for transport in transports), return_exceptions=True)
        env.cleanup_errors = [type(item).__name__ for item in cleanup_results if isinstance(item, BaseException)]
        for connection in connections:
            if connection.thread is not None:
                await asyncio.to_thread(connection.thread.join, 3)
                assert not connection.thread.is_alive()
        assert all(connection.disconnected.is_set() for connection in connections)
        for transport in transports:
            await transport._http.aclose()
        for account in channels.list_role_accounts_for_all_roles():
            await host.intake.close(account.account_id)
            await channels.avatars.stop_account(account.account_id)

    asyncio.run(cleanup())


async def enable_account(client, env, *, role=None, account_id="feishu-owned"):
    role = role or env.role
    previous_connections = len(env.connections)
    response = await client.put(f"/api/roles/{role.id}/channels/feishu", json={
        "accountId": account_id, "appId": f"app-{account_id}",
        "appSecret": f"synthetic-secret-{account_id}",
        "verificationToken": "synthetic-verify", "enabled": True,
    })
    assert response.status_code == 200, response.text
    await wait_until(lambda: len(env.connections) > previous_connections
                     and env.connections[-1].entered.is_set())
    return response.json()["account"]


def test_public_disable_cancels_inflight_handshake_before_reporting_success(feishu_api):
    env = feishu_api

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.main.app),
                                     base_url="http://localhost") as client:
            account = await enable_account(client, env)
            connection = env.connections[0]
            runner = env.main.channel_transports[account["accountId"]]._long_connection
            response = await client.post(
                f"/api/roles/{env.role.id}/channels/accounts/{account['accountId']}/state",
                json={"enabled": False},
            )
            assert response.status_code == 200, response.text
            assert not runner.alive, "successful disable still owns a live connection thread"
            assert connection.cancelled.is_set()
            assert connection.disconnected.is_set()
            assert response.json()["account"]["enabled"] is False
            assert not env.host.accepts_events(account["accountId"])

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["switch", "delete", "shutdown"])
def test_public_lifecycle_retires_old_handshake_and_preserves_scoped_history(feishu_api, operation):
    env = feishu_api

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.main.app),
                                     base_url="http://localhost") as client:
            account = await enable_account(client, env)
            account_id = account["accountId"]
            connection = env.connections[0]
            runner = env.transports[0]._long_connection
            await enable_account(client, env, role=env.other_role, account_id="feishu-other")
            other_runner = env.transports[1]._long_connection
            thread = env.channels.ensure_channel_thread(env.role.id, "feishu", account_id, "chat-history", "private")
            created, message = env.channels.append_event(
                thread, direction="inbound", content="original private history",
                external_message_id="history-message",
            )
            assert created
            if operation == "switch":
                response = await client.put(f"/api/roles/{env.role.id}/channels/feishu", json={
                    "accountId": account_id, "appId": "app-feishu-owned",
                    "endpoint": "https://localhost/new/open-apis",
                    "appSecret": "synthetic-replacement", "enabled": True,
                })
                assert response.status_code == 200, response.text
                await wait_until(lambda: len(env.connections) == 3 and env.connections[-1].entered.is_set())
                replacement = env.main.channel_transports[account_id]
                assert replacement is not env.transports[0]
                assert replacement._long_connection.alive
                assert env.channels.channel_config(account_id)["endpoint"] == "https://localhost/new/open-apis"
            elif operation == "delete":
                response = await client.delete(f"/api/roles/{env.role.id}/channels/accounts/{account_id}")
                assert response.status_code == 204, response.text
                assert env.channels.get_account(account_id) is None
                assert account_id not in env.main.channel_transports
            else:
                assert env.main._close_channel_workers in env.main.app.router.on_shutdown
                await env.main._close_channel_workers()
                assert not other_runner.alive
                assert env.closed_services == ["maintenance", "managed-qq"]
            assert not runner.alive
            assert connection.cancelled.is_set() and connection.disconnected.is_set()
            if operation != "shutdown":
                assert other_runner.alive
            history = await client.get(f"/api/roles/{env.role.id}/channels/threads/{thread.thread_id}/messages")
            assert history.status_code == 200, history.text
            assert [item["id"] for item in history.json()["messages"]] == [message.id]
            assert history.json()["messages"][0]["content"] == "original private history"
            denied = await client.get(f"/api/roles/{env.other_role.id}/channels/threads/{thread.thread_id}/messages")
            assert denied.status_code == 404
            visible = await client.get(f"/api/roles/{env.other_role.id}/channels/accounts")
            assert visible.status_code == 200
            assert [item["accountId"] for item in visible.json()["accounts"]] == ["feishu-other"]

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["disable", "delete"])
def test_public_cleanup_failure_is_retryable_without_dropping_owned_transport(feishu_api, operation):
    env = feishu_api

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.main.app),
                                     base_url="http://localhost") as client:
            account = await enable_account(client, env)
            account_id = account["accountId"]
            transport = env.transports[0]
            runner = transport._long_connection
            env.faults["stop"] = True
            endpoint = f"/api/roles/{env.role.id}/channels/accounts/{account_id}"

            async def stop_request():
                if operation == "delete":
                    return await client.delete(endpoint)
                return await client.post(f"{endpoint}/state", json={"enabled": False})

            failed = await stop_request()
            assert failed.status_code == 503, failed.text
            assert env.channels.get_account(account_id).enabled is False
            assert env.main.channel_transports[account_id] is transport and runner.alive
            assert not env.host.accepts_events(account_id)
            assert "synthetic-secret" not in failed.text
            env.faults["stop"] = False
            retried = await stop_request()
            assert retried.status_code == (204 if operation == "delete" else 200), retried.text
            assert not runner.alive
            if operation == "delete":
                assert env.channels.get_account(account_id) is None
            else:
                assert env.channels.get_account(account_id).enabled is False

    asyncio.run(scenario())


def feishu_payload(identifier):
    return {
        "header": {"event_id": identifier, "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "controlled-sender"}, "sender_type": "user"},
            "message": {"message_id": identifier, "chat_id": "controlled-chat", "chat_type": "p2p",
                        "message_type": "text", "content": json.dumps({"text": identifier})},
        },
    }


def test_public_disable_drains_admitted_handler_and_rejects_later_events(feishu_api, monkeypatch):
    env = feishu_api

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        handled, rejected = [], []

        async def handle(event):
            handled.append(event.external_message_id)
            entered.set()
            await release.wait()

        async def reject(_adapter, payload):
            # Retry notices are external sends; observe rejected intake here
            # instead of sending anything to a platform.
            rejected.append(payload["header"]["event_id"])

        monkeypatch.setattr(env.main, "_handle_channel_event", handle)
        monkeypatch.setattr(env.main, "_reject_channel_payload", reject)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.main.app),
                                     base_url="http://localhost") as client:
            account = await enable_account(client, env)
            account_id = account["accountId"]
            connection = env.connections[0]
            runner = env.transports[0]._long_connection
            runner._loop.call_soon_threadsafe(connection.on_event, feishu_payload("admitted"))
            await asyncio.wait_for(entered.wait(), 2)
            pending = asyncio.create_task(client.post(
                f"/api/roles/{env.role.id}/channels/accounts/{account_id}/state",
                json={"enabled": False},
            ))
            try:
                await wait_until(lambda: not env.host.accepts_events(account_id))
                assert not pending.done()
                assert not connection.cancelled.is_set(), "transport stopped before admitted handler drained"
                runner._loop.call_soon_threadsafe(connection.on_event, feishu_payload("late"))
                await wait_until(lambda: bool(env.host.intake._buffers.get(account_id)) or bool(rejected))
                # The unrelated HTTP route remains responsive while drain
                # awaits an already admitted handler.
                other = await asyncio.wait_for(client.get(f"/api/roles/{env.other_role.id}/channels/accounts"), 1)
                assert other.status_code == 200
            finally:
                release.set()
                response = await asyncio.wait_for(pending, 3)
            assert response.status_code == 200, response.text
            assert not runner.alive
            assert handled == ["admitted"] and rejected == ["late"]
            assert not env.host._inflight_events.get(account_id)

    asyncio.run(scenario())


def test_failed_public_enable_keeps_the_live_transport_when_cleanup_fails(feishu_api):
    env = feishu_api
    from backend.app.channels.credentials import credential_key

    account = env.channels.register_channel_account(
        env.role.id, "feishu", account_id="feishu-enable-failure", enabled=False,
    )
    env.channels.set_channel_config(account.account_id, {"appId": "controlled-app"})
    env.credentials.set(credential_key("feishu", account.account_id, "app_secret"), "synthetic-only")
    env.faults.update(start_after_runner=True, stop=True)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.main.app),
                                     base_url="http://localhost") as client:
            response = await client.post(
                f"/api/roles/{env.role.id}/channels/accounts/{account.account_id}/state",
                json={"enabled": True},
            )
            assert response.status_code in {502, 503}, response.text
            transport = env.transports[0]
            assert env.connections[0].thread.is_alive()
            assert env.main.channel_transports.get(account.account_id) is transport
            assert env.main.channel_adapters[account.account_id] is env.host.adapter(account.account_id)
            assert env.host.adapter(account.account_id)._transport is transport
            assert env.channels.get_account(account.account_id).enabled is False
            assert not env.host.accepts_events(account.account_id)
            assert "synthetic-only" not in response.text
            # A second enable may retry cleanup, but must never replace the
            # still-owned runner merely because startup returned an error.
            retry = await client.post(
                f"/api/roles/{env.role.id}/channels/accounts/{account.account_id}/state",
                json={"enabled": True},
            )
            assert retry.status_code in {502, 503}, retry.text
            assert env.transports == [transport]
            assert len(env.connections) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("existing,retry", [(False, "delete"), (True, "enable"), (True, "save")])
def test_failed_save_restores_saved_settings_and_keeps_candidate_owned_until_cleanup(feishu_api, existing, retry):
    env = feishu_api
    from backend.app.channels.credentials import credential_key

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.main.app),
                                     base_url="http://localhost") as client:
            account_id = "feishu-owned"
            old_runner = None
            saved_config, saved_secret = None, None
            if existing:
                await enable_account(client, env, account_id=account_id)
                old_runner = env.transports[0]._long_connection
                env.connections[0].release.set()
                await wait_until(lambda: env.connections[0].connected)
                saved_config = env.channels.channel_config(account_id)
                saved_secret = env.credentials.get(credential_key("feishu", account_id, "app_secret"))
            app_id = "app-feishu-owned" if existing else "replacement-app"
            env.faults.update(start_after_runner=app_id, stop="failed_start")
            response = await client.put(f"/api/roles/{env.role.id}/channels/feishu", json={
                "accountId": account_id, "appId": app_id,
                "endpoint": "https://localhost/replacement/open-apis",
                "appSecret": "synthetic-candidate-secret", "enabled": True,
            })
            assert response.status_code in {502, 503}, response.text
            candidate = env.transports[-1]
            candidate_runner = candidate._long_connection
            assert env.connections[-1].thread.is_alive()
            if old_runner is not None:
                assert not old_runner.alive
            current = env.channels.get_account(account_id)
            assert current is not None, "failed cleanup deleted the live candidate's account"
            assert current.enabled is False
            if existing:
                # Persisted settings retain their rollback contract even
                # while the failed candidate still owns its frozen inputs.
                assert env.channels.channel_config(account_id) == saved_config
                assert env.credentials.get(credential_key("feishu", account_id, "app_secret")) == saved_secret
            else:
                # No old settings exist: retain a disabled account so its
                # still-live candidate remains explicitly removable.
                assert env.channels.channel_config(account_id)["endpoint"] == "https://localhost/replacement/open-apis"
                assert env.credentials.get(credential_key("feishu", account_id, "app_secret")) == "synthetic-candidate-secret"
            assert candidate.endpoint == "https://localhost/replacement/open-apis"
            assert candidate.app_secret == "synthetic-candidate-secret"
            assert env.main.channel_transports.get(account_id) is candidate
            assert env.host.adapter(account_id)._transport is candidate
            assert not env.host.accepts_events(account_id)
            assert "synthetic-candidate-secret" not in response.text

            env.faults.update(start_after_runner=False, stop=False)
            if retry == "delete":
                removed = await client.delete(f"/api/roles/{env.role.id}/channels/accounts/{account_id}")
                assert removed.status_code == 204, removed.text
                assert not candidate_runner.alive
                assert env.channels.get_account(account_id) is None
                assert account_id not in env.main.channel_transports
                assert not env.credentials.values
            else:
                # Neither operation supplies replacement settings.  The
                # owner's retry must use the restored saved configuration,
                # retiring the candidate before starting another connection.
                if retry == "enable":
                    restored = await client.post(
                        f"/api/roles/{env.role.id}/channels/accounts/{account_id}/state",
                        json={"enabled": True},
                    )
                else:
                    restored = await client.put(f"/api/roles/{env.role.id}/channels/feishu", json={
                        "accountId": account_id, "enabled": True,
                    })
                assert restored.status_code == 200, restored.text
                assert not candidate_runner.alive
                replacement = env.main.channel_transports[account_id]
                assert replacement is not candidate
                assert replacement.endpoint == saved_config["endpoint"]
                assert replacement.app_secret == saved_secret
                assert env.channels.channel_config(account_id) == saved_config
                assert env.credentials.get(credential_key("feishu", account_id, "app_secret")) == saved_secret
                assert env.channels.get_account(account_id).enabled is True
                assert env.host.adapter(account_id)._transport is replacement
                assert len(env.transports) == 3

    asyncio.run(scenario())


def test_startup_recovery_keeps_failed_cleanup_owned_for_later_shutdown(feishu_api):
    env = feishu_api
    from backend.app.channels.credentials import credential_key

    account = env.channels.register_channel_account(
        env.role.id, "feishu", account_id="feishu-startup", enabled=True,
    )
    env.channels.set_channel_config(account.account_id, {"appId": "startup-app"})
    env.credentials.set(credential_key("feishu", account.account_id, "app_secret"), "synthetic-only")
    env.faults.update(start_after_runner=True, stop=True)

    async def scenario():
        assert env.main._start_channel_workers in env.main.app.router.on_startup
        await env.main._start_channel_workers()
        assert env.channels.get_account(account.account_id).enabled is False
        transport = env.transports[0]
        runner = transport._long_connection
        assert runner.alive
        assert env.main.channel_transports.get(account.account_id) is transport
        assert env.host.adapter(account.account_id)._transport is transport
        assert not env.host.accepts_events(account.account_id)
        env.faults.update(start_after_runner=False, stop=False)
        await env.main._close_channel_workers()
        assert not runner.alive

    asyncio.run(scenario())


def test_failed_switch_blocks_webhook_and_retry_retires_same_owned_runner(feishu_api, monkeypatch):
    env = feishu_api
    from backend.app.channels import feishu_connection

    monkeypatch.setattr(feishu_connection, "STOP_TIMEOUT", 0.05)
    env.faults["resist_cancel"] = True

    async def scenario():
        received = []

        async def handle(event):
            received.append(event.external_message_id)

        monkeypatch.setattr(env.main, "_handle_channel_event", handle)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.main.app),
                                     base_url="http://localhost") as client:
            account = await enable_account(client, env)
            account_id = account["accountId"]
            transport = env.transports[0]
            runner = transport._long_connection
            old_config = env.channels.channel_config(account_id)
            payload = {"accountId": account_id, "appId": "app-feishu-owned",
                       "endpoint": "https://localhost/new/open-apis", "enabled": True}
            failed = await client.put(f"/api/roles/{env.role.id}/channels/feishu", json=payload)
            assert failed.status_code in {502, 503}, failed.text
            assert runner.alive and env.connections[0].cancelled.is_set()
            assert env.main.channel_transports[account_id] is transport
            assert env.channels.channel_config(account_id) == old_config
            current = await client.get(f"/api/roles/{env.role.id}/channels/accounts")
            observed = current.json()["accounts"][0]
            assert observed["online"] is None and observed["reason"] == "stopping"
            event = feishu_payload("after-failed-stop")
            event["header"]["token"] = "synthetic-verify"
            rejected = await client.post(f"/api/channels/feishu/{account_id}/webhook", json=event)
            assert rejected.status_code in {400, 409, 503}, rejected.text
            assert received == []
            assert len(env.transports) == 1 and len(env.connections) == 1
            # The same runner can genuinely finish after its owned resource
            # becomes cooperative.  A past timeout is not a permanent latch
            # preventing the owner's explicit cleanup retry.
            env.connections[0].release.set()
            await wait_until(lambda: not runner.alive)
            env.faults["resist_cancel"] = False
            retried = await client.put(f"/api/roles/{env.role.id}/channels/feishu", json=payload)
            assert retried.status_code == 200, retried.text
            assert len(env.transports) == 2
            assert env.main.channel_transports[account_id] is not transport

    asyncio.run(scenario())


def test_repeated_shutdown_cancellation_still_retires_both_accounts(feishu_api):
    env = feishu_api
    env.faults["block_disconnect"] = True

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.main.app),
                                     base_url="http://localhost") as client:
            await enable_account(client, env)
            await enable_account(client, env, role=env.other_role, account_id="feishu-other")
            runners = [transport._long_connection for transport in env.transports]
            shutdown = asyncio.create_task(env.main._close_channel_workers())
            try:
                await wait_until(lambda: any(item.disconnect_entered.is_set() for item in env.connections))
                for _ in range(3):
                    shutdown.cancel()
                    await asyncio.sleep(0)
                assert not shutdown.done(), "cancel abandoned owned disconnect before it completed"
                status = await asyncio.wait_for(client.get(f"/api/roles/{env.other_role.id}/channels/accounts"), 1)
                assert status.status_code == 200
            finally:
                for connection in env.connections:
                    connection.release_disconnect.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(shutdown, 3)
            assert all(not runner.alive for runner in runners), "shutdown cancellation abandoned another account"
            assert all(item.cancelled.is_set() and item.disconnected.is_set() for item in env.connections)
            assert env.closed_services == ["maintenance", "managed-qq"]

    asyncio.run(scenario())
