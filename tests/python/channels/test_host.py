from __future__ import annotations

import asyncio

import pytest

from backend.app.channels import (
    ChannelAdapter,
    ChannelAdapterError,
    ChannelHost,
    ChatType,
    DeliveryResult,
    DeliveryStatus,
    InboundEvent,
    OutboundMessage,
    Target,
    TargetKind,
    UnsupportedTargetError,
)
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore


class MemoryCredentials:
    def get(self, _key: str) -> str:
        return ""

    def set(self, _key: str, _value: str) -> None:
        return None


class FakeAdapter(ChannelAdapter):
    channel = "qq"

    def __init__(self, account_id: str, target: Target, *, failure: BaseException | None = None) -> None:
        super().__init__()
        self.account_id = account_id
        self._targets = {target.chat_id: target}
        self.failure = failure
        self.sent: list[OutboundMessage] = []

    @property
    def capabilities(self):
        from backend.app.channels.models import ChannelCapabilities

        return ChannelCapabilities(private=True, group=True, targets=frozenset({TargetKind.PRIVATE, TargetKind.GROUP}))

    def parse_event(self, _payload):
        return None

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        if self.failure is not None:
            raise self.failure
        self.sent.append(message)
        return DeliveryResult(DeliveryStatus.SENT, message.target, "platform-message-1")

    async def list_targets(self) -> list[Target]:
        return list(self._targets.values())


def make_store(tmp_path):
    sessions = SessionStore(tmp_path / "sessions.db")
    store = MultichannelStore(tmp_path / "sessions.db", sessions, MemoryCredentials())
    return store


def make_target(account_id: str, chat_id: str = "chat-1") -> Target:
    from backend.app.channels.models import target_from_chat

    return target_from_chat(account_id=account_id, chat_id=chat_id, kind=TargetKind.PRIVATE, display_name="Alice")


def test_group_member_private_send_never_enters_origin_group_history(tmp_path):
    from backend.app.channels.models import target_from_chat
    store = make_store(tmp_path)
    account = store.register_channel_account("role", "qq", account_id="qq", enabled=True, allow_group=True)
    group = store.ensure_channel_thread("role", "qq", account.account_id, "group", "group")
    member = target_from_chat(account_id=account.account_id, chat_id="member", kind=TargetKind.GROUP_MEMBER, group_id="group", display_name="Alice")
    adapter = FakeAdapter(account.account_id, member)
    host = ChannelHost(store)
    host.register(adapter)
    asyncio.run(host.account_targets("role", account.account_id))
    result = asyncio.run(host.account_send("role", account.account_id, member.id, "private content", actor="owner"))
    assert result.status is DeliveryStatus.SENT
    assert store.sessions.list_messages(group.session_key) == []
    private = store.get_thread_by_chat("role", account.account_id, "member")
    assert private.chat_type == "private"
    assert [message.content for message in store.sessions.list_messages(private.session_key)] == ["private content"]
    assert private.metadata["originGroupId"] == "group"


def test_uncertain_outgoing_media_is_visible_but_not_replayed_as_delivered_transcript(tmp_path):
    from backend.app.chat_attachments import save_attachment
    store = make_store(tmp_path)
    account = store.register_channel_account("role", "qq", account_id="qq", enabled=True)
    target = make_target(account.account_id)
    attachment = save_attachment(tmp_path / "role", "note.txt", "text/plain", b"note")
    adapter = FakeAdapter(account.account_id, target, failure=TimeoutError())
    host = ChannelHost(store, attachment_resolver=lambda role_id, attachment_id: attachment if role_id == "role" and attachment_id == attachment["id"] else None)
    host.register(adapter)
    asyncio.run(host.account_targets("role", account.account_id))
    result = asyncio.run(host.account_send("role", account.account_id, target.id, "private media", actor="owner", attachment_ids=(attachment["id"],)))
    thread = store.get_thread_by_chat("role", account.account_id, target.chat_id)
    message = store.sessions.list_messages(thread.session_key)[0]
    assert result.status is DeliveryStatus.UNCERTAIN
    assert message.status == "failed"
    assert message.metadata["deliveryStatus"] == "uncertain"
    assert message.metadata["attachments"][0]["id"] == attachment["id"]
    assert store.sessions.runtime_messages(thread.session_key) == []


def test_account_targets_are_role_scoped_and_survive_host_restart(tmp_path):
    store = make_store(tmp_path)
    account = store.register_channel_account("role-a", "qq", account_id="qq-a", enabled=True)
    target = make_target(account.account_id)
    adapter = FakeAdapter(account.account_id, target)
    host = ChannelHost(store)
    host.register(adapter)

    first = asyncio.run(host.account_targets("role-a", account.account_id))
    restarted = MultichannelStore(tmp_path / "sessions.db", SessionStore(tmp_path / "sessions.db"), MemoryCredentials())

    assert first[0]["id"] == target.id
    assert first[0]["displayName"] == "Alice"
    assert restarted.list_targets("role-a", account.account_id)[0].id == target.id
    with pytest.raises(KeyError):
        asyncio.run(host.account_targets("role-b", account.account_id))


def test_host_rejects_targets_and_events_from_another_account(tmp_path):
    store = make_store(tmp_path)
    account = store.register_channel_account("role-a", "qq", account_id="qq-a", enabled=True)
    foreign_target = make_target("qq-b")
    adapter = FakeAdapter(account.account_id, foreign_target)
    host = ChannelHost(store)
    host.register(adapter)

    with pytest.raises(ChannelAdapterError, match="其他账号"):
        asyncio.run(host.account_targets("role-a", account.account_id))
    assert store.list_targets("role-a", account.account_id) == []

    class ForeignEventAdapter(FakeAdapter):
        def parse_event(self, _payload):
            return InboundEvent(
                channel="telegram",
                account_id="qq-b",
                external_event_id="foreign-event",
                external_message_id="foreign-message",
                chat_id="chat-1",
                chat_type=ChatType.PRIVATE,
                sender_id="sender",
            )

    event_host = ChannelHost(store)
    event_host.register(ForeignEventAdapter(account.account_id, make_target(account.account_id)))
    with pytest.raises(ChannelAdapterError, match="其他账号"):
        asyncio.run(event_host.accept_event(account.account_id, {}))


def test_account_send_rejects_unobserved_targets_and_cross_chat_external_send(tmp_path):
    store = make_store(tmp_path)
    account = store.register_channel_account("role-a", "qq", account_id="qq-a", enabled=True)
    target = make_target(account.account_id)
    second_target = make_target(account.account_id, "chat-2")
    adapter = FakeAdapter(account.account_id, target)
    adapter._targets[second_target.chat_id] = second_target
    host = ChannelHost(store)
    host.register(adapter)
    asyncio.run(host.account_targets("role-a", account.account_id))
    source_thread = store.ensure_channel_thread("role-a", "qq", account.account_id, target.chat_id, "private")

    with pytest.raises(UnsupportedTargetError):
        asyncio.run(host.account_send("role-a", account.account_id, "qq-a:private:arbitrary", "hello", actor="owner"))
    with pytest.raises(UnsupportedTargetError):
        asyncio.run(host.account_send("role-a", account.account_id, second_target.id, "hello", actor="external", source_thread_id=source_thread.thread_id))
    assert not adapter.sent
    assert store.list_deliveries("role-a") == []


def test_external_account_send_resolves_only_the_paired_owner_chat(tmp_path):
    store = make_store(tmp_path)
    account = store.register_channel_account("role-a", "qq", account_id="qq-a", enabled=True)
    guest_target = make_target(account.account_id, "guest-chat")
    owner_target = make_target(account.account_id, "owner-chat")
    adapter = FakeAdapter(account.account_id, guest_target)
    adapter._targets[owner_target.chat_id] = owner_target
    host = ChannelHost(store)
    host.register(adapter)
    asyncio.run(host.account_targets("role-a", account.account_id))
    source_thread = store.ensure_channel_thread("role-a", "qq", account.account_id, guest_target.chat_id, "private")
    pairing_code = store.create_pairing_code("role-a", "qq")
    assert store.consume_pairing_code(
        pairing_code,
        role_id="role-a",
        channel="qq",
        scope="platform",
        platform_user_id="owner-user",
        account_id=account.account_id,
        chat_id=owner_target.chat_id,
    )

    with pytest.raises(UnsupportedTargetError):
        asyncio.run(host.account_send("role-a", account.account_id, guest_target.id, "not owner", actor="external", source_thread_id=source_thread.thread_id))
    result = asyncio.run(host.account_send("role-a", account.account_id, "owner", "owner only", actor="external", source_thread_id=source_thread.thread_id))

    assert result.status is DeliveryStatus.SENT
    assert adapter.sent[-1].target.chat_id == owner_target.chat_id


def test_account_send_records_platform_result_and_uncertain_timeout(tmp_path):
    store = make_store(tmp_path)
    account = store.register_channel_account("role-a", "qq", account_id="qq-a", enabled=True)
    target = make_target(account.account_id)
    adapter = FakeAdapter(account.account_id, target)
    host = ChannelHost(store)
    host.register(adapter)
    asyncio.run(host.account_targets("role-a", account.account_id))

    result = asyncio.run(host.account_send("role-a", account.account_id, target.id, "sent", actor="owner"))
    assert result.status is DeliveryStatus.SENT
    assert result.platform_message_id == "platform-message-1"
    delivery = store.list_deliveries("role-a")[0]
    assert delivery["status"] == "sent"
    assert delivery["platformMessageId"] == "platform-message-1"
    thread = store.get_thread("role-a", str(delivery["threadId"]))
    assert thread is not None
    sent_message = store.sessions.list_messages(thread.session_key)[0]
    assert sent_message.metadata["deliveryStatus"] == "sent"

    class RequestTimeout(Exception):
        pass

    timeout_adapter = FakeAdapter(account.account_id, target, failure=RequestTimeout("secret request details"))
    timeout_host = ChannelHost(store)
    timeout_host.register(timeout_adapter)
    result = asyncio.run(timeout_host.account_send("role-a", account.account_id, target.id, "uncertain", actor="owner"))
    assert result.status is DeliveryStatus.UNCERTAIN
    assert result.error == "RequestTimeout"
    assert store.list_deliveries("role-a")[0]["status"] == "uncertain"
    assert "secret request details" not in str(store.list_deliveries("role-a"))


def test_pause_drains_admitted_turn_but_disabled_account_revokes_unsent_reply(tmp_path):
    async def scenario() -> None:
        store = make_store(tmp_path)
        account = store.register_channel_account("role-a", "qq", account_id="qq-a", enabled=True)
        target = make_target(account.account_id)
        adapter = FakeAdapter(account.account_id, target)
        host = ChannelHost(store)
        host.register(adapter)
        await host.account_targets("role-a", account.account_id)
        entered = asyncio.Event()
        finish_turn = asyncio.Event()

        async def admitted_turn() -> None:
            async with host.event_scope(account.account_id) as admitted:
                assert admitted
                entered.set()
                await finish_turn.wait()
                with pytest.raises(ChannelAdapterError, match='授权已失效'):
                    await host.account_send("role-a", account.account_id, target.id, "final reply", actor="owner")

        turn = asyncio.create_task(admitted_turn())
        await entered.wait()
        store.set_account_state(account.account_id, enabled=False)
        drain = asyncio.create_task(host.pause_and_drain(account.account_id))
        for _ in range(20):
            if account.account_id in host._paused_accounts:
                break
            await asyncio.sleep(0)

        assert not drain.done()
        async with host.event_scope(account.account_id) as admitted:
            assert not admitted
        with pytest.raises(ChannelAdapterError, match="未启用|切换"):
            await host.account_send("role-a", account.account_id, target.id, "new send", actor="owner")

        finish_turn.set()
        await turn
        await asyncio.wait_for(drain, timeout=1)
        assert adapter.sent == []

    asyncio.run(scenario())
