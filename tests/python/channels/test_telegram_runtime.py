from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from backend.app.channels.models import Attachment, DeliveryStatus, OutboundMessage, TargetKind, target_from_chat
from backend.app.channels.host import ChannelHost
from backend.app.channels.telegram import TelegramAccountAdapter
from backend.app.multichannel import MultichannelStore, TelegramBotApi, TelegramChannelService
from backend.app.session_store import SessionStore


class Credentials:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        self.values[key] = value


class TelegramTransport:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def get_me(self, token):
        return {"id": 42, "username": "meido_bot"}

    async def send_message(self, token, chat_id, text, *, reply_to=None):
        self.sent.append((chat_id, text, reply_to))
        return {"message_id": 700 + len(self.sent)}

    async def edit_message_text(self, token, chat_id, message_id, text):
        self.edited.append((chat_id, message_id, text))
        return {"message_id": message_id}


def make_store(tmp_path):
    sessions = SessionStore(tmp_path / "sessions.db")
    store = MultichannelStore(tmp_path / "sessions.db", sessions, Credentials())
    account = store.save_account("role-1", token="secret", enabled=True)
    return store, sessions, account


def update(content, message_id=1, *, reply=None):
    message = {"message_id": message_id, "text": content, "chat": {"id": 100, "type": "private"}, "from": {"id": 101, "first_name": "Alice"}}
    if reply is not None:
        message["reply_to_message"] = reply
    return {"update_id": message_id, "message": message}


def test_polling_dispatch_callback_drains_all_admitted_turns_before_exit(tmp_path):
    store, _, account = make_store(tmp_path)
    async def scenario():
        release = asyncio.Event()
        admitted = asyncio.Event()
        stop = asyncio.Event()
        received = []
        finished = []
        async def on_update(account_id, payload):
            received.append(payload["update_id"])
            if len(received) == 2:
                admitted.set()
            await release.wait()
            finished.append(payload["update_id"])
        class PollingTransport(TelegramTransport):
            calls = 0
            async def get_updates(self, token, *, offset=None):
                self.calls += 1
                if self.calls == 1:
                    return [update("first", 1), update("second", 2)]
                await admitted.wait()
                stop.set()
                return []
        service = TelegramChannelService(store, account.account_id, PollingTransport(), on_update=on_update)
        polling = asyncio.create_task(service.run_polling(stop))
        await asyncio.wait_for(admitted.wait(), 1)
        await asyncio.sleep(0)
        assert not polling.done()
        assert finished == []
        release.set()
        await asyncio.wait_for(polling, 1)
        assert finished == [1, 2]
    asyncio.run(scenario())


def test_private_stream_updates_and_finalizes_same_platform_message():
    async def scenario():
        transport = TelegramTransport()
        adapter = TelegramAccountAdapter("account", lambda: "secret", transport)
        target = target_from_chat(account_id="account", chat_id="100", kind=TargetKind.PRIVATE)
        adapter.remember_target(target)
        session = await adapter.stream_start(target, reply_to_message_id="55")
        await adapter.stream_update(session, "hello")
        await session.task
        result = await adapter.stream_finalize(session, "hello world")

        assert transport.sent == [("100", "…", "55")]
        assert transport.edited == [("100", "701", "hello"), ("100", "701", "hello world")]
        assert result.status is DeliveryStatus.SENT
        assert result.platform_message_id == "701"

    asyncio.run(scenario())


def test_telegram_anonymous_sender_chat_cannot_inherit_compatibility_user_owner_identity(tmp_path):
    store, sessions, account = make_store(tmp_path)
    account = store.save_account("role-1", account_id=account.account_id, enabled=True, allow_group=True)
    transport = TelegramTransport()
    calls = []
    service = TelegramChannelService(store, account.account_id, transport, on_message=lambda *args: calls.append(args))
    asyncio.run(service.test_account(account.account_id))
    code = store.create_pairing_code("role-1")
    assert store.consume_pairing_code(code, role_id="role-1", channel="telegram", scope="platform", platform_user_id="101", account_id=account.account_id, chat_id="100")
    payload = update("@meido_bot hello")
    payload["message"].update({"chat": {"id": -100, "type": "supergroup", "title": "Current Group"}, "sender_chat": {"id": -200, "title": "Anonymous Channel"}, "entities": [{"type": "mention", "offset": 0, "length": 10}]})
    assert asyncio.run(service.handle_update(account.account_id, payload))
    assert calls[0][3] is False
    assert calls[0][2]["senderId"] == "chat:-200"
    assert calls[0][2]["senderName"] == "Anonymous Channel"
    assert sessions.list_messages(calls[0][0].session_key)[0].metadata["senderIsOwner"] is False


def test_telegram_group_preserves_personal_sender_name_and_owner_source_without_rewriting_raw_history(tmp_path):
    store, sessions, account = make_store(tmp_path)
    account = store.save_account("role-1", account_id=account.account_id, enabled=True, allow_group=True)
    transport = TelegramTransport()
    service = TelegramChannelService(store, account.account_id, transport)
    asyncio.run(service.test_account(account.account_id))
    code = store.create_pairing_code("role-1")
    assert store.consume_pairing_code(code, role_id="role-1", channel="telegram", scope="platform", platform_user_id="101", account_id=account.account_id, chat_id="100")
    payload = update("@meido_bot hello")
    payload["message"]["chat"] = {"id": -100, "type": "supergroup", "title": "Current Group"}
    payload["message"]["from"]["last_name"] = "Smith"
    payload["message"]["entities"] = [{"type": "mention", "offset": 0, "length": 10}]
    assert asyncio.run(service.handle_update(account.account_id, payload))
    thread = store.get_thread_by_chat("role-1", account.account_id, "-100")
    message = sessions.list_messages(thread.session_key)[0]
    assert message.content == "@meido_bot hello"
    assert message.metadata["senderName"] == "Alice Smith"
    assert message.metadata["senderIsOwner"] is True
    projected = sessions.runtime_messages(thread.session_key)[0].content
    assert "主人 Alice Smith" in projected and "群名：Current Group" in projected


def test_private_stream_preserves_long_final_content_in_follow_up_chunks():
    async def scenario():
        transport = TelegramTransport()
        adapter = TelegramAccountAdapter("account", lambda: "secret", transport)
        target = target_from_chat(account_id="account", chat_id="100", kind=TargetKind.PRIVATE)
        adapter.remember_target(target)
        session = await adapter.stream_start(target)
        text = "a" * 5000
        result = await adapter.stream_finalize(session, text)

        assert transport.edited[0][2] + transport.sent[1][1] == text
        assert result.platform_message_id == "701"

    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["/chatid", "/myid", "/stop"])
def test_telegram_control_commands_never_enter_history_or_model(tmp_path, command):
    store, sessions, account = make_store(tmp_path)
    transport = TelegramTransport()
    calls = []
    service = TelegramChannelService(store, account.account_id, transport, on_message=lambda *args: calls.append(args))

    assert asyncio.run(service.handle_update(account.account_id, update(command)))
    assert not calls
    assert not sessions.list_messages(store.list_threads("role-1")[0].session_key)
    assert transport.sent[0][2] == "1"


@pytest.mark.parametrize("format_code", [lambda code: code.lower(), lambda code: f"/PAIR {code}"])
def test_telegram_accepts_bare_pairing_code_and_uppercase_command(tmp_path, format_code):
    store, _, account = make_store(tmp_path)
    transport = TelegramTransport()
    calls = []
    service = TelegramChannelService(store, account.account_id, transport, on_message=lambda *args: calls.append(args))
    code = store.create_pairing_code("role-1")

    assert asyncio.run(service.handle_update(account.account_id, update(format_code(code))))
    assert store.identity_is_owner(role_id="role-1", channel="telegram", scope="platform", platform_user_id="101", account_id=account.account_id)
    assert not calls
    assert transport.sent[0][1] == "配对成功。"


def test_polling_receives_stop_while_reply_runs_and_drains_accepted_work(tmp_path):
    store, _, account = make_store(tmp_path)

    async def scenario():
        started, release, finished, stop = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()

        class PollingTransport(TelegramTransport):
            async def get_updates(self, token, *, offset=None):
                if offset is None:
                    return [update("hello")]
                await started.wait()
                return [update("/stop", 2)]

        async def handle_message(*args):
            started.set()
            await release.wait()
            await asyncio.sleep(0)
            finished.set()

        async def handle_command(thread, command, sender):
            assert command == "stop" and sender == "101"
            release.set()
            stop.set()
            return "stopped"

        service = TelegramChannelService(store, account.account_id, PollingTransport(), on_message=handle_message, on_command=handle_command)
        await asyncio.wait_for(service.run_polling(stop), timeout=2)
        assert finished.is_set()
        assert not service._message_tasks

    asyncio.run(scenario())


def test_telegram_reply_metadata_carries_quoted_body_and_sender(tmp_path):
    store, _, account = make_store(tmp_path)
    calls = []
    service = TelegramChannelService(store, account.account_id, TelegramTransport(), on_message=lambda *args: calls.append(args))
    reply = {"message_id": 7, "text": "quoted", "from": {"id": 99, "first_name": "Bob"}}

    assert asyncio.run(service.handle_update(account.account_id, update("hello", reply=reply)))
    metadata = calls[0][2]
    assert metadata["platformMessageId"] == "1"
    assert metadata["replyTo"] == "7"
    assert metadata["replyToContent"] == "quoted"
    assert metadata["replyToSenderName"] == "Bob"


def test_paused_telegram_intake_replies_retry_notice_without_history(tmp_path):
    from backend.app.channels.host import CHANNEL_INTAKE_RETRY_NOTICE

    store, sessions, account = make_store(tmp_path)
    calls = []
    transport = TelegramTransport()
    service = TelegramChannelService(
        store, account.account_id, transport, on_message=lambda *args: calls.append(args),
        on_admit=lambda account_id: False,
    )

    assert not asyncio.run(service.handle_update(account.account_id, update("hello")))
    assert not calls
    assert transport.sent == [("100", CHANNEL_INTAKE_RETRY_NOTICE, "1")]
    thread = store.get_thread_by_chat("role-1", account.account_id, "100")
    assert not sessions.list_messages(thread.session_key)


def test_telegram_observed_group_directory_accumulates_topics_and_members(tmp_path):
    store, _, account = make_store(tmp_path)
    store.save_account("role-1", account_id=account.account_id, enabled=True, allow_group=True)
    store.set_platform_identity(account.account_id, "42", display_name="meido_bot")
    calls = []
    service = TelegramChannelService(store, account.account_id, TelegramTransport(), on_message=lambda *args: calls.append(args))

    for message_id, topic_id, sender_id in [(1, 12, 101), (2, 13, 102)]:
        payload = update("@meido_bot hello", message_id)
        payload["message"]["chat"]["type"] = "supergroup"
        payload["message"]["message_thread_id"] = topic_id
        payload["message"]["from"]["id"] = sender_id
        payload["message"]["entities"] = [{"type": "mention", "offset": 0, "length": 10}]
        assert asyncio.run(service.handle_update(account.account_id, payload))

    target = store.list_targets("role-1", account.account_id)[0]
    assert target.metadata["topics"] == [12, 13]
    assert target.metadata["member_ids"] == ["101", "102"]
    assert calls[-1][2]["messageThreadId"] == 13
    assert calls[-1][0].chat_type == "group"


def test_telegram_api_forwards_topics_and_html_to_text_and_media(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: async_client(transport=httpx.MockTransport(handle), **kwargs))

    async def scenario():
        api = TelegramBotApi()
        await api.send_message("secret", "100", "hello", message_thread_id=12, parse_mode="HTML")
        await api.send_photo("secret", "100", b"image", "image.png", reply_to="55", message_thread_id=12, parse_mode="HTML")
        await api.send_document("secret", "100", b"document", "doc.txt", reply_to="55", message_thread_id=12, parse_mode="HTML")

    asyncio.run(scenario())
    text = json.loads(requests[0].content)
    assert text["message_thread_id"] == 12 and text["parse_mode"] == "HTML"
    for request in requests[1:]:
        assert b'name="message_thread_id"\r\n\r\n12' in request.content
        assert b'name="parse_mode"\r\n\r\nHTML' in request.content
        assert b'name="reply_parameters"\r\n\r\n{"message_id": 55}' in request.content


def test_telegram_long_text_and_media_caption_preserve_full_content(tmp_path):
    async def scenario():
        class MediaTransport(TelegramTransport):
            def __init__(self):
                super().__init__()
                self.photos = []

            async def send_photo(self, token, chat_id, data, file_name, *, caption="", **kwargs):
                self.photos.append(caption)
                return {"message_id": 99}

        transport = MediaTransport()
        adapter = TelegramAccountAdapter("account", lambda: "secret", transport)
        target = target_from_chat(account_id="account", chat_id="100", kind=TargetKind.GROUP)
        adapter.remember_target(target)
        text = "a" * 9000
        result = await adapter.send(OutboundMessage(target, text))
        assert result.status is DeliveryStatus.SENT
        assert "".join(item[1] for item in transport.sent) == text
        assert all(len(item[1]) <= 4096 for item in transport.sent)

        transport.sent.clear()
        image = tmp_path / "image.png"
        image.write_bytes(b"image")
        result = await adapter.send(OutboundMessage(target, text, attachments=(Attachment("image", str(image)),)))
        assert result.status is DeliveryStatus.SENT
        assert "".join(item[1] for item in transport.sent) == text
        assert transport.photos == [""]

    asyncio.run(scenario())


def test_telegram_partially_sent_long_text_and_failed_stream_are_uncertain():
    async def scenario():
        class FailingTransport(TelegramTransport):
            async def send_message(self, token, chat_id, text, *, reply_to=None):
                if self.sent:
                    raise RuntimeError("platform rejected remaining text")
                return await super().send_message(token, chat_id, text, reply_to=reply_to)

            async def edit_message_text(self, *args):
                raise RuntimeError("platform rejected edit")

        target = target_from_chat(account_id="account", chat_id="100", kind=TargetKind.PRIVATE)
        transport = FailingTransport()
        adapter = TelegramAccountAdapter("account", lambda: "secret", transport)
        adapter.remember_target(target)
        result = await adapter.send(OutboundMessage(target, "a" * 5000))
        assert result.status is DeliveryStatus.UNCERTAIN

        transport.sent.clear()
        session = await adapter.stream_start(target)
        result = await adapter.stream_finalize(session, "final")
        assert result.status is DeliveryStatus.UNCERTAIN
        assert result.platform_message_id == "701"

    asyncio.run(scenario())


def test_stream_preview_without_receipt_keeps_delivery_uncertain(tmp_path):
    store, _, account = make_store(tmp_path)

    async def scenario():
        class MissingReceiptTransport(TelegramTransport):
            async def send_message(self, *args, **kwargs):
                await super().send_message(*args, **kwargs)
                return {}

        host = ChannelHost(store)
        adapter = TelegramAccountAdapter(account.account_id, lambda: "secret", MissingReceiptTransport())
        host.register(adapter)
        target = target_from_chat(account_id=account.account_id, chat_id="100", kind=TargetKind.PRIVATE)
        store.remember_targets("role-1", [target])

        async def updates():
            yield "final"

        result = await host.account_send("role-1", account.account_id, target.id, "", actor="owner", stream_updates=updates())
        assert result.status is DeliveryStatus.UNCERTAIN
        assert store.list_deliveries("role-1")[0]["status"] == "uncertain"

    asyncio.run(scenario())
