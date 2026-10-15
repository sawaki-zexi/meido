from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from backend.app.agent_runtime import CancellationToken, ToolContext
from backend.app.channels import ChannelAdapter, ChannelHost, DeliveryResult, DeliveryStatus, OutboundMessage, TargetKind, target_from_chat
from backend.app.channels.models import ChannelCapabilities
from backend.app.channels.push import ChannelPushService, MessagePushTool
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore


class Credentials:
    def get(self, _key):
        return ""

    def set(self, _key, _value):
        pass


class Adapter(ChannelAdapter):
    channel = "qq"
    capabilities = ChannelCapabilities(private=True, targets=frozenset({TargetKind.PRIVATE}))

    def __init__(self, account_id, target, *, failure=None):
        super().__init__()
        self.account_id, self.target, self.failure = account_id, target, failure
        self.sent = []

    def parse_event(self, _payload):
        return None

    async def list_targets(self):
        return [self.target]

    async def send(self, message: OutboundMessage):
        self.sent.append(message)
        if self.failure:
            raise self.failure
        return DeliveryResult(DeliveryStatus.SENT, message.target, "receipt-1")


def setup_push(tmp_path, *, failure=None):
    sessions = SessionStore(tmp_path / "sessions.db")
    store = MultichannelStore(tmp_path / "sessions.db", sessions, Credentials())
    sessions.open_role_session("role-1")
    account = store.register_channel_account("role-1", "qq", account_id="qq-1", enabled=True)
    target = target_from_chat(account_id=account.account_id, chat_id="owner-chat", kind=TargetKind.PRIVATE, display_name="主人")
    store.remember_targets("role-1", [target])
    code = store.create_pairing_code("role-1", "qq")
    assert store.consume_pairing_code(code, role_id="role-1", channel="qq", scope="platform", platform_user_id="owner-id", account_id=account.account_id, chat_id=target.chat_id)
    adapter = Adapter(account.account_id, target, failure=failure)
    host = ChannelHost(store)
    host.register(adapter)
    return store, adapter, ChannelPushService(store, host), target


def context():
    return ToolContext("role-1", "role:role-1", "run-1", CancellationToken(), "call-1")


def test_push_only_reaches_paired_owner_and_records_call_source(tmp_path):
    store, adapter, service, target = setup_push(tmp_path)
    tool = MessagePushTool(service, is_owner=True)
    denied = asyncio.run(tool.execute({"channel": "qq", "chat_id": "stranger", "message": "不能发"}, context()))
    sent = asyncio.run(tool.execute({"channel": "qq", "chat_id": target.id, "message": "提醒"}, context()))
    assert denied.is_error and not sent.is_error
    assert len(adapter.sent) == 1
    delivery = store.list_deliveries("role-1")[0]
    assert delivery["sourceSessionKey"] == "role:role-1"
    assert delivery["sourceRunId"] == "run-1"
    assert delivery["sourceToolCallId"] == "call-1"
    assert delivery["sourceKind"] == "message_push"
    assert delivery["accountGeneration"]
    thread = next(thread for thread in store.list_threads("role-1") if thread.channel == "qq")
    assert store.sessions.list_messages(thread.session_key)[0].content == "提醒"


def test_external_push_cannot_contact_other_channel_or_desktop(tmp_path):
    store, adapter, service, _ = setup_push(tmp_path)
    visitor = store.ensure_channel_thread("role-1", "qq", "qq-1", "visitor", "private")
    tool = MessagePushTool(service, is_owner=False, source_thread_id=visitor.thread_id, source_account_id="qq-1")
    assert tool.definition.input_schema["properties"]["channel"]["enum"] == ["qq"]
    assert tool.definition.input_schema["properties"]["chat_id"]["enum"] == ["owner", "user"]
    for channel, chat_id in (("desktop", "owner"), ("qq", "owner-chat"), ("telegram", "owner")):
        result = asyncio.run(tool.execute({"channel": channel, "chat_id": chat_id, "message": "no"}, context()))
        assert result.is_error
    result = asyncio.run(tool.execute({"channel": "qq", "chat_id": "owner", "message": "帮我联系主人"}, context()))
    assert not result.is_error
    assert len(adapter.sent) == 1


def test_reminders_require_explicit_paired_target_and_never_repeat_uncertain(tmp_path):
    store, adapter, service, target = setup_push(tmp_path, failure=TimeoutError("timeout"))
    assert asyncio.run(service.deliver_reminder("role-1", "todo-1", "提醒")) == []
    with pytest.raises(ValueError):
        service.configure("role-1", "qq-1", "guessed-id")
    service.configure("role-1", "qq-1", target.id)
    result = asyncio.run(service.deliver_reminder("role-1", "todo-1", "提醒"))
    assert result[0]["status"] == "uncertain"
    restarted = ChannelPushService(store, service.host)
    assert asyncio.run(restarted.deliver_reminder("role-1", "todo-1", "提醒")) == []
    assert len(adapter.sent) == 1
    assert store.list_deliveries("role-1")[0]["status"] == "uncertain"


def test_unbinding_owner_invalidates_configured_reminder_destination(tmp_path):
    store, adapter, service, target = setup_push(tmp_path)
    service.configure("role-1", "qq-1", target.id)
    store.unbind_identity(store.list_identities()[0]["identityId"])
    assert service.list_targets("role-1") == []
    assert asyncio.run(service.deliver_reminder("role-1", "todo-2", "不能发")) == []
    assert not adapter.sent
    code = store.create_pairing_code("role-1", "qq")
    assert store.consume_pairing_code(code, role_id="role-1", channel="qq", scope="platform", platform_user_id="owner-id", account_id="qq-1", chat_id=target.chat_id)
    assert service.list_targets("role-1")[0]["enabled"] is False
    assert asyncio.run(service.deliver_reminder("role-1", "todo-3", "未重新勾选")) == []
    assert not adapter.sent


def test_push_target_api_rejects_foreign_account_and_supports_opt_in(tmp_path, monkeypatch):
    from backend.app import main
    from backend.app.models import RoleInput, RoleProfile
    from backend.app.role_store import RoleStore

    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    store, _, service, target = setup_push(tmp_path)
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "_require_channel_role", lambda _role_id: None)
    monkeypatch.setattr(main, "multichannel_store", store)
    monkeypatch.setattr(main, "channel_host", service.host)
    monkeypatch.setattr(main, "_push_service_cache", service)
    client = TestClient(main.app)
    foreign = client.put(f"/api/roles/{role.id}/channels/accounts/qq-1/push-target", json={"targetId": target.id})
    assert foreign.status_code == 404
    path = "/api/roles/role-1/channels/accounts/qq-1/push-target"
    assert client.put(path, json={"targetId": "stranger"}).status_code == 422
    assert client.put(path, json={"targetId": target.id}).json()["targets"][0]["enabled"]
    assert not client.put(path, json={"targetId": None}).json()["targets"][0]["enabled"]
