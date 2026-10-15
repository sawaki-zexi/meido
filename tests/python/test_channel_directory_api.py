"""Channel directory queries reach the next provider through real host/storage."""
import json
import asyncio
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.agent_runtime.runtime import RuntimeManager
from backend.app.channels import ChannelHost
from backend.app.channels.models import TargetKind, target_from_chat
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.channels.directory import DirectoryQuery
from backend.app.channels.directory import DirectoryError
from backend.app.memory_config import MemorySettingsStore
from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from backend.app.model_config import ModelConfigurationStore
from backend.app.multichannel import MultichannelStore
from backend.app.multichannel import TelegramBotApi
from backend.app.channels.telegram import TelegramAccountAdapter
from backend.app.agent_runtime import CancellationToken, ToolContext
from backend.app.models import AgentRun
from backend.app.role_store import RoleStore
from backend.app.session_manager import SessionManager
from backend.app.session_store import SessionStore


class Credentials:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        self.values[key] = value


@pytest.fixture
def directory_api(tmp_path, monkeypatch, local_platform_only):
    roles = RoleStore(tmp_path / "roles")
    sessions = SessionStore(tmp_path / "sessions.db")
    channels = MultichannelStore(tmp_path / "sessions.db", sessions, Credentials())
    settings = MemorySettingsStore(tmp_path / "memory.json")
    settings.update(enabled=False)
    host = ChannelHost(channels)
    for name, value in {
        "store": roles, "session_store": sessions, "session_manager": SessionManager(roles, sessions),
        "multichannel_store": channels, "channel_host": host,
        "model_configuration_store": ModelConfigurationStore(tmp_path / "models.json"),
        "memory_settings_store": settings, "runtime_manager": RuntimeManager(sessions),
        "role_locks": {}, "channel_adapters": {}, "channel_transports": {},
        "telegram_services": {}, "telegram_tasks": {}, "channel_tasks": {},
    }.items():
        monkeypatch.setattr(main, name, value)
    client = TestClient(main.app)
    role = client.post("/api/roles", json={"name": "目录", "profile": {"profile": "设定"}}).json()["role"]
    account = channels.register_channel_account(role["id"], "qq", account_id="directory-qq", enabled=True, allow_group=True)
    yield client, role, account, host, channels, sessions
    client.close()


@pytest.mark.parametrize("kind", ["known", "friends"])
def test_owner_reads_current_connection_and_pages_directory_in_next_provider(directory_api, monkeypatch, kind):
    client, role, account, host, channels, sessions = directory_api
    calls = []

    async def rpc(action, params):
        calls.append(action)
        assert kind == "friends" and action == "get_friend_list"
        return {"items": [{"user_id": i, "nickname": f"Friend {i}"} for i in range(1, 4)]}

    adapter = NapCatAdapter(NapCatConfig(account.account_id), transport=rpc)
    host.register(adapter)
    channels.remember_targets(role["id"], [target_from_chat(account_id=account.account_id, chat_id=str(i),
        kind=TargetKind.PRIVATE, display_name=f"Friend {i}") for i in range(1, 4)])

    class Model:
        count = 0
        seen = []

        async def stream_structured_messages(self, messages, configuration, tools):
            self.count += 1
            if self.count == 1:
                yield ModelToolCall("accounts", "account_list", {})
            else:
                content = next(item["content"] for item in reversed(messages) if item["role"] == "tool")
                data = json.loads(content)
                if self.count == 2:
                    assert data[0]["online"] is None and data[0]["connection"] == "unknown"
                    assert data[0]["supportedKinds"] == ["known", "friends", "groups", "members"]
                    yield ModelToolCall("first", "account_targets", {"account_id": account.account_id, "limit": 2,
                        **({"kind": kind} if kind != "known" else {})})
                else:
                    self.seen.extend(item["chatId"] for item in data["targets"])
                    assert data["coverage"] == ("known" if kind == "known" else "platform") and data["complete"] is True
                    if data["nextCursor"]:
                        yield ModelToolCall("next", "account_targets", {"account_id": account.account_id,
                            "limit": 2, "cursor": data["nextCursor"], "kind": kind})
                    else:
                        assert set(self.seen) == {"1", "2", "3"} and len(self.seen) == 3
                        yield ModelTextDelta("已读完观察目录")

    model = Model()
    monkeypatch.setattr(main, "model_adapter", model)
    response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "查看账号及已交互对象"})
    assert "已读完观察目录" in response.text and model.count == 4
    assert calls == ([] if kind == "known" else ["get_friend_list"])
    results = [item for item in sessions.list_messages(f"role:{role['id']}") if item.messageType == "tool_result"]
    assert len(results) == 3 and all(not item.isError for item in results)


def test_friends_query_uses_one_platform_snapshot_and_never_enumerates_groups(directory_api):
    _, role, account, host, channels, _ = directory_api
    calls = []

    async def rpc(action, params):
        calls.append((action, params))
        assert action == "get_friend_list"
        return {"items": [{"user_id": i, "nickname": f"Name{i}"} for i in range(1, 4)]}

    adapter = NapCatAdapter(NapCatConfig(account.account_id), transport=rpc)
    host.register(adapter)

    async def scenario():
        first = await host.query_directory(role["id"], account.account_id, DirectoryQuery("friends", limit=2), scope="owner")
        second = await host.query_directory(role["id"], account.account_id,
            DirectoryQuery("friends", limit=2, cursor=first["nextCursor"]), scope="owner")
        assert [item["chatId"] for item in first["targets"] + second["targets"]] == ["1", "2", "3"]
        assert second["nextCursor"] is None and second["coverage"] == "platform"
        assert calls == [("get_friend_list", {})]
        assert len(channels.list_targets(role["id"], account.account_id)) == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("kind,action,payload,expected", [
    ("groups", "get_group_list", [{"group_id": 100, "group_name": "Known group"}], "100"),
    ("members", "get_group_member_list", [{"user_id": 22, "card": "Member"}], "22"),
])
def test_qq_directory_fetches_only_requested_kind_and_scopes_members(directory_api, kind, action, payload, expected):
    _, role, account, host, channels, _ = directory_api
    group = target_from_chat(account_id=account.account_id, chat_id="100", kind=TargetKind.GROUP)
    channels.remember_targets(role["id"], [group])
    calls = []

    async def rpc(operation, params):
        calls.append((operation, params))
        assert operation == action
        return {"items": payload}

    host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=rpc))
    query = DirectoryQuery(kind, group_id="100" if kind == "members" else "")
    result = asyncio.run(host.query_directory(role["id"], account.account_id, query, scope="owner"))
    assert [item["chatId"] for item in result["targets"]] == [expected]
    assert len(calls) == 1
    if kind == "members":
        assert result["targets"][0]["groupId"] == "100"
        assert calls[0][1] == {"group_id": "100", "no_cache": True}


@pytest.mark.parametrize("withdraw", ["disable", "replace", "source", "config"])
def test_platform_response_is_not_persisted_after_authority_changes(directory_api, withdraw):
    _, role, account, host, channels, _ = directory_api

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        allowed = True

        async def rpc(action, params):
            entered.set()
            await release.wait()
            return {"items": [{"user_id": 77, "nickname": "Stale"}]}

        host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=rpc))
        pending = asyncio.create_task(host.query_directory(role["id"], account.account_id, DirectoryQuery("friends"),
            scope="owner", authorized=lambda: allowed))
        await asyncio.wait_for(entered.wait(), 2)
        if withdraw == "disable":
            channels.set_account_state(account.account_id, enabled=False)
        elif withdraw == "replace":
            host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=rpc))
        elif withdraw == "config":
            channels.set_channel_config(account.account_id, {"endpoint": "ws://changed.invalid"})
        else:
            allowed = False
        release.set()
        with pytest.raises(PermissionError):
            await pending
        assert channels.list_targets(role["id"], account.account_id) == []

    asyncio.run(scenario())


def test_role_removed_during_public_tool_call_cannot_import_platform_directory(directory_api, monkeypatch):
    client, role, account, host, channels, _ = directory_api

    async def rpc(action, params):
        main.store.delete(role["id"])
        return {"items": [{"user_id": 77, "nickname": "Stale private directory"}]}

    host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=rpc))

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            if not any(item["role"] == "tool" for item in messages):
                yield ModelToolCall("friends", "account_targets", {"account_id": account.account_id, "kind": "friends"})
            else:
                yield ModelTextDelta("Stopped")

    monkeypatch.setattr(main, "model_adapter", Model())
    client.post(f"/api/roles/{role['id']}/messages", json={"content": "查询好友"})
    assert channels.list_targets(role["id"], account.account_id) == []


def test_telegram_member_uses_real_bot_api_and_does_not_create_send_destination(directory_api, monkeypatch):
    client, role, _, host, channels, sessions = directory_api
    account = channels.save_account(role["id"], account_id="directory-telegram", token="fixture-token", enabled=True, allow_group=True)
    group = target_from_chat(account_id=account.account_id, chat_id="-100", kind=TargetKind.GROUP)
    channels.remember_targets(role["id"], [group])
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            requests.append((parsed.path.rsplit("/", 1)[-1], parse_qs(parsed.query)))
            body = json.dumps({"ok": True, "result": {"status": "member", "user": {
                "id": 22, "first_name": "Alice", "last_name": "Member", "username": "alice"}}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        api = TelegramBotApi(f"http://127.0.0.1:{server.server_port}")
        host.register(TelegramAccountAdapter(account.account_id, lambda: "fixture-token", api))

        class Model:
            count = 0

            async def stream_structured_messages(self, messages, configuration, tools):
                self.count += 1
                if self.count == 1:
                    yield ModelToolCall("accounts", "account_list", {})
                else:
                    result = json.loads(next(item["content"] for item in reversed(messages) if item["role"] == "tool"))
                    if self.count == 2:
                        observed = next(item for item in result if item["accountId"] == account.account_id)
                        assert observed["online"] is None and observed["supportedKinds"] == ["known", "member"]
                        yield ModelToolCall("member", "account_targets", {"account_id": account.account_id,
                            "kind": "member", "group_id": "-100", "member_id": "22"})
                    else:
                        member = result["targets"][0]
                        assert member["memberId"] == "22" and member["groupId"] == "-100" and member["canSend"] is False
                        assert member["displayName"] == "Alice Member" and member["status"] == "member"
                        yield ModelTextDelta("已读取真实成员资料")

        model = Model()
        monkeypatch.setattr(main, "model_adapter", model)
        response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "查询群内成员"})
        assert "已读取真实成员资料" in response.text and model.count == 3
        assert len([item for item in sessions.list_messages(f"role:{role['id']}") if item.messageType == "tool_result"]) == 2
        assert requests == [("getChatMember", {"chat_id": ["-100"], "user_id": ["22"]})]
        assert [item.id for item in channels.list_targets(role["id"], account.account_id)] == [group.id]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_web_and_model_share_transport_evidence_not_handler_or_task_presence(directory_api, monkeypatch):
    client, role, account, host, _, _ = directory_api
    from backend.app.channels.transports import NapCatWebSocketTransport
    transport = NapCatWebSocketTransport("ws://127.0.0.1:1")
    transport.connected = True
    adapter = NapCatAdapter(NapCatConfig(account.account_id), transport=transport)
    host.register(adapter)
    monkeypatch.setattr(main, "channel_transports", {account.account_id: transport})
    snapshot = host.account_list(role["id"])[0]
    assert snapshot["connection"] == "connected" and snapshot["online"] is None
    assert main._channel_account_payload(account)["connectionStatus"] == "connected"


def test_telegram_poll_status_requires_success_and_is_cleared_on_stop(directory_api):
    _, role, _, _, channels, _ = directory_api
    from backend.app.multichannel import TelegramChannelService
    account = channels.save_account(role["id"], token="fixture", enabled=True)

    async def scenario():
        stop = asyncio.Event()
        entered, release = asyncio.Event(), asyncio.Event()

        class Transport:
            async def get_me(self, token):
                return {"id": 1}

            async def get_updates(self, token, *, offset=None):
                entered.set()
                await release.wait()
                stop.set()
                return []

        service = TelegramChannelService(channels, account.account_id, Transport())
        running = asyncio.create_task(service.run_polling(stop))
        await entered.wait()
        assert service.connection_snapshot()["online"] is None
        release.set()
        await running
        assert service.connection_snapshot()["online"] is False
        assert service.connection_snapshot()["reason"] == "polling_stopped"

    asyncio.run(scenario())


def test_inbound_qq_observation_does_not_enumerate_platform_directories(directory_api):
    _, role, account, host, channels, _ = directory_api
    calls = []

    async def rpc(action, params):
        calls.append(action)
        return {"items": []}

    host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=rpc))
    asyncio.run(host.accept_event(account.account_id, {"post_type": "message", "message_type": "group", "self_id": 99,
        "group_id": 100, "message_id": 1, "sender": {"user_id": 22, "nickname": "Observed"},
        "message": [{"type": "text", "data": {"text": "hello"}}]}))
    assert calls == []
    assert any(target.chat_id == "100" for target in channels.list_targets(role["id"], account.account_id))


def test_inbound_observation_does_not_bulk_import_unrelated_cached_members(directory_api):
    _, role, account, host, channels, _ = directory_api
    adapter = NapCatAdapter(NapCatConfig(account.account_id))
    adapter.remember_target(target_from_chat(account_id=account.account_id, chat_id="unrelated-member",
        kind=TargetKind.GROUP_MEMBER, group_id="unrelated-group"))
    host.register(adapter)
    asyncio.run(host.accept_event(account.account_id, {"post_type": "message", "message_type": "group", "self_id": 99,
        "group_id": 100, "message_id": 1, "sender": {"user_id": 22, "nickname": "Observed"},
        "message": [{"type": "text", "data": {"text": "hello"}}]}))
    assert {target.chat_id for target in channels.list_targets(role["id"], account.account_id)} == {"100", "22"}


def test_network_guard_blocks_default_constructor_fallback_before_connect(directory_api, local_platform_only):
    _, _, account, _, channels, _ = directory_api
    from backend.app.multichannel import TelegramChannelService
    service = TelegramChannelService(channels, account.account_id, object())
    assert isinstance(service.transport, TelegramBotApi)
    try:
        asyncio.run(service.transport.get_updates("synthetic-fixture-credential", timeout=0))
    except AssertionError:
        pass
    assert len(local_platform_only) == 1
    local_platform_only.clear()  # This test intentionally probes the guard itself.


def test_network_guard_covers_literal_ip_and_background_async_connect(local_platform_only):
    import socket
    with socket.socket() as connection:
        with pytest.raises(AssertionError):
            connection.connect(("203.0.113.1", 443))

    async def scenario():
        pending = asyncio.create_task(asyncio.open_connection("203.0.113.1", 443))
        with pytest.raises(AssertionError):
            await pending

    asyncio.run(scenario())
    assert len(local_platform_only) == 2
    local_platform_only.clear()


@pytest.mark.parametrize("change", ["source", "query", "config_restore", "platform_restore", "adapter", "recreated"])
def test_cursor_is_bound_to_source_query_and_nonreviving_account_generation(directory_api, change):
    _, role, account, host, channels, _ = directory_api

    async def rpc(action, params):
        return {"items": [{"user_id": i, "nickname": str(i)} for i in range(1, 4)]}

    adapter = NapCatAdapter(NapCatConfig(account.account_id), transport=rpc)
    host.register(adapter)

    async def scenario():
        first = await host.query_directory(role["id"], account.account_id, DirectoryQuery("friends", limit=2), scope="original")
        scope, kind = "original", "friends"
        if change == "source":
            scope = "another_sender_or_pairing"
        elif change == "query":
            kind = "groups"
        elif change == "config_restore":
            channels.set_channel_config(account.account_id, {"endpoint": "ws://changed.invalid"})
            channels.set_channel_config(account.account_id, {})
        elif change == "platform_restore":
            channels.set_channel_enabled("qq", False)
            channels.set_channel_enabled("qq", True)
        elif change == "adapter":
            host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=rpc))
        else:
            channels.delete_account(account.account_id)
            channels.register_channel_account(role["id"], "qq", account_id=account.account_id, enabled=True)
        with pytest.raises((DirectoryError, PermissionError)):
            await host.query_directory(role["id"], account.account_id,
                DirectoryQuery(kind, limit=2, cursor=first["nextCursor"]), scope=scope)

    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [
    {"items": [{"user_id": i} for i in range(1, 5002)]},
    {"items": [{"user_id": 1, "nickname": "界" * 400000}]},
])
def test_directory_over_budget_never_persists_or_claims_complete(directory_api, payload):
    _, role, account, host, channels, _ = directory_api

    async def rpc(action, params):
        return payload

    host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=rpc))
    with pytest.raises(DirectoryError, match="directory_too_large"):
        asyncio.run(host.query_directory(role["id"], account.account_id, DirectoryQuery("friends"), scope="owner"))
    assert channels.list_targets(role["id"], account.account_id) == []


def test_snapshot_capacity_rejects_before_another_platform_request(directory_api):
    _, role, account, host, _, _ = directory_api
    calls = []

    async def rpc(action, params):
        calls.append(action)
        return {"items": [{"user_id": i} for i in range(1, 4)]}

    host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=rpc))

    async def scenario():
        for _ in range(2):
            await host.query_directory(role["id"], account.account_id, DirectoryQuery("friends", limit=1), scope="owner")
        with pytest.raises(DirectoryError, match="directory_busy"):
            await host.query_directory(role["id"], account.account_id, DirectoryQuery("friends", limit=1), scope="owner")
        assert calls == ["get_friend_list", "get_friend_list"]

    asyncio.run(scenario())


def test_cursor_expires_without_refetch_and_known_snapshot_survives_new_observations(directory_api, monkeypatch):
    _, role, account, host, channels, _ = directory_api
    from backend.app.channels import directory
    monkeypatch.setattr(directory, "SNAPSHOT_TTL", .05)
    channels.remember_targets(role["id"], [target_from_chat(account_id=account.account_id, chat_id=str(i),
        kind=TargetKind.PRIVATE) for i in range(3)])

    async def scenario():
        first = await host.query_directory(role["id"], account.account_id, DirectoryQuery(limit=2), scope="owner")
        channels.remember_targets(role["id"], [target_from_chat(account_id=account.account_id, chat_id="new", kind=TargetKind.PRIVATE)])
        second = await host.query_directory(role["id"], account.account_id,
            DirectoryQuery(limit=2, cursor=first["nextCursor"]), scope="owner")
        assert len(first["targets"] + second["targets"]) == 3 and second["nextCursor"] is None
        assert "new" not in {item["chatId"] for item in first["targets"] + second["targets"]}
        await asyncio.sleep(.06)
        with pytest.raises(DirectoryError, match="invalid_or_expired_cursor"):
            await host.query_directory(role["id"], account.account_id,
                DirectoryQuery(limit=2, cursor=first["nextCursor"]), scope="owner")

    asyncio.run(scenario())


def test_concurrent_queries_and_cancellation_do_not_leave_an_unbounded_rpc(directory_api):
    _, role, account, host, channels, _ = directory_api

    async def scenario():
        entered, release, joined = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = 0

        async def rpc(action, params):
            nonlocal calls
            calls += 1
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finally:
                joined.set()
            return {"items": [{"user_id": 77}]}

        host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=rpc))
        first = asyncio.create_task(host.query_directory(role["id"], account.account_id, DirectoryQuery("friends"), scope="owner"))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(host.query_directory(role["id"], account.account_id, DirectoryQuery("friends"), scope="owner"))
        try:
            await asyncio.sleep(.01)
            assert second.done(), "a concurrent query must be rejected before starting another RPC"
            with pytest.raises(DirectoryError, match="directory_busy"):
                await second
            first.cancel()
            await asyncio.sleep(0)
            assert not first.done()
        finally:
            first.cancel()
            release.set()
            await asyncio.gather(first, second, return_exceptions=True)
        assert joined.is_set() and calls == 1
        assert channels.list_targets(role["id"], account.account_id) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["next_run", "sender", "rebind"])
def test_channel_cursor_follows_real_owner_binding_across_runs(directory_api, change):
    _, role, account, host, channels, sessions = directory_api
    rid = role["id"]
    thread = channels.ensure_channel_thread(rid, "qq", account.account_id, "owner-group", "group")
    calls = []

    def pair():
        code = channels.create_pairing_code(rid, "qq")
        assert channels.consume_pairing_code(code, role_id=rid, channel="qq", scope="platform",
            platform_user_id="owner", account_id=account.account_id, chat_id="owner-private")

    pair()

    async def rpc(action, params):
        calls.append(action)
        return {"items": [{"user_id": i} for i in range(1, 4)]}

    host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=rpc))

    def turn(identifier, sender="owner"):
        previous = sessions.active_run(rid)
        if previous:
            sessions.update_run(previous.runId, status="completed", ended_at=datetime.now(timezone.utc))
        _, message = channels.append_event(thread, direction="inbound", content="directory request",
            external_message_id=identifier, metadata={"senderId": sender, "senderIsOwner": True})
        sessions.create_run_with_existing_user_message(AgentRun(runId=identifier, roleId=rid,
            sessionKey=thread.session_key, status="running", startedAt=datetime.now(timezone.utc)), message.id)
        tools = {tool.definition.name: tool for tool in main._runtime_tool_candidates(main.store.get(rid),
            is_channel_owner=True, source_thread_id=thread.thread_id, source_account_id=account.account_id, include_channel=True)}
        return tools["account_targets"], ToolContext(rid, thread.session_key, identifier, CancellationToken(), "directory-call")

    async def scenario():
        tool, context = turn("first-owner-run")
        first = await tool.execute({"account_id": account.account_id, "kind": "friends", "limit": 2}, context)
        assert not first.is_error, first.content
        cursor = json.loads(first.content)["nextCursor"]
        if change == "rebind":
            channels.unbind_identity(channels.list_identities()[0]["identityId"])
            pair()
        tool, context = turn("next-owner-run", "visitor" if change == "sender" else "owner")
        second = await tool.execute({"account_id": account.account_id, "kind": "friends", "limit": 2, "cursor": cursor}, context)
        assert second.is_error is (change != "next_run"), second.content
        if change == "next_run":
            assert len(json.loads(second.content)["targets"]) == 1
        assert calls == ["get_friend_list"]

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["empty", "rejected", "timeout", "unsupported"])
def test_public_provider_distinguishes_empty_from_directory_failures(directory_api, monkeypatch, outcome):
    client, role, account, host, channels, sessions = directory_api
    from backend.app.channels.base import PlatformRejectedError
    from backend.app.channels import directory
    canceled = []
    calls = []

    async def rpc(action, params):
        calls.append(action)
        if outcome == "rejected":
            raise PlatformRejectedError("NEVER_ECHO_REMOTE_DETAIL")
        if outcome == "timeout":
            try:
                await asyncio.sleep(10)
            finally:
                canceled.append(True)
        return {"items": []}

    host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=rpc))
    if outcome == "unsupported":
        account = channels.register_channel_account(role["id"], "qqbot", account_id="known-only", enabled=True)
    if outcome == "timeout":
        monkeypatch.setattr(directory, "QUERY_TIMEOUT", .01)

    class Model:
        count = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.count += 1
            if self.count == 1:
                yield ModelToolCall("query", "account_targets", {"account_id": account.account_id, "kind": "friends"})
            else:
                raw = next(item["content"] for item in reversed(messages) if item["role"] == "tool")
                assert "NEVER_ECHO_REMOTE_DETAIL" not in raw
                result = json.loads(raw)
                if outcome == "empty":
                    assert result["complete"] is True and result["targets"] == []
                else:
                    assert result["complete"] is False
                    assert result["error"] == {"rejected": "platform_rejected", "timeout": "timeout", "unsupported": "unsupported"}[outcome]
                yield ModelTextDelta("已准确解释目录结果")

    model = Model()
    monkeypatch.setattr(main, "model_adapter", model)
    response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "查询目录"})
    assert "已准确解释目录结果" in response.text and model.count == 2
    assert len(calls) == (0 if outcome == "unsupported" else 1)
    assert bool(canceled) is (outcome == "timeout")
    assert len([item for item in sessions.list_messages(f"role:{role['id']}") if item.messageType == "tool_result"]) == 1


@pytest.mark.parametrize("status,encoding,error", [(200, "gzip", "unsupported_response_encoding"),
    (403, "", "permission_denied"), (401, "", "permission_denied"), (500, "", "platform_rejected")])
def test_real_telegram_protocol_refuses_compression_and_classifies_rejections(directory_api, status, encoding, error):
    import gzip
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.headers.get("Accept-Encoding"))
            raw = b"x" * (2 * 1024 * 1024)
            body = gzip.compress(raw) if encoding else b'{"description":"NEVER_ECHO_REMOTE_DETAIL"}'
            self.send_response(status)
            if encoding:
                self.send_header("Content-Encoding", encoding)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        api = TelegramBotApi(f"http://127.0.0.1:{server.server_port}")
        with pytest.raises(DirectoryError, match=error):
            asyncio.run(api.get_chat_member("synthetic-fixture-credential", "-100", "22"))
        assert requests == ["identity"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_known_directory_rejects_oversized_stored_observation_without_claiming_complete(directory_api):
    _, role, account, host, channels, _ = directory_api
    channels.remember_targets(role["id"], [target_from_chat(account_id=account.account_id, chat_id="1",
        kind=TargetKind.PRIVATE, platform_detail="界" * 400000)])
    with pytest.raises(DirectoryError, match="directory_too_large"):
        asyncio.run(host.query_directory(role["id"], account.account_id, DirectoryQuery(), scope="owner"))


def test_real_sqlite_writer_wait_rechecks_cancel_before_directory_commit(directory_api):
    import sqlite3
    import time
    _, role, account, host, channels, _ = directory_api
    held, rpc_ready = threading.Event(), threading.Event()
    token = CancellationToken()

    def hold_writer():
        with sqlite3.connect(channels.database_path, timeout=2) as db:
            db.execute("BEGIN IMMEDIATE")
            held.set()
            if rpc_ready.wait(2):
                time.sleep(.05)
                token.cancel()
            db.commit()

    writer = threading.Thread(target=hold_writer, daemon=True)
    writer.start()
    assert held.wait(2)

    async def rpc(action, params):
        rpc_ready.set()
        return {"items": [{"user_id": 88, "nickname": "Must roll back"}]}

    host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=rpc))
    try:
        with pytest.raises(PermissionError):
            asyncio.run(host.query_directory(role["id"], account.account_id, DirectoryQuery("friends"),
                scope="owner", authorized=lambda: not token.is_cancelled()))
    finally:
        rpc_ready.set()
        writer.join(3)
    assert not writer.is_alive() and token.is_cancelled()
    assert channels.list_targets(role["id"], account.account_id) == []
