from __future__ import annotations

import asyncio

import pytest

from backend.app.channels import ChannelHost, OutboundMessage, TargetKind, TelegramAccountAdapter, UnsupportedTargetError, target_from_chat
from backend.app.channels.host import target_payload
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore


class Credentials:
    def get(self, _key):
        return ""

    def set(self, _key, _value):
        pass


class Transport:
    def __init__(self):
        self.sent = []

    async def send_message(self, _token, chat_id, content, **options):
        self.sent.append((chat_id, content, options))
        return {"message_id": 1}


def setup_group(tmp_path):
    store = MultichannelStore(tmp_path / "sessions.db", SessionStore(tmp_path / "sessions.db"), Credentials())
    account = store.save_account("role-1", account_id="tg-1", enabled=True, allow_group=True)
    target = target_from_chat(
        account_id=account.account_id, chat_id="-1001", kind=TargetKind.GROUP,
        display_name="群", topics=[10, 20], member_ids=["123"], message_thread_id=20,
    )
    store.remember_targets("role-1", [target])
    transport = Transport()
    adapter = TelegramAccountAdapter("tg-1", lambda: "token", transport)
    adapter.remember_target(target)
    host = ChannelHost(store)
    host.register(adapter)
    return store, host, transport, target


def test_directory_exposes_observed_topics_and_members(tmp_path):
    _, _, _, target = setup_group(tmp_path)
    payload = target_payload(target)
    assert payload["topicIds"] == [10, 20]
    assert payload["memberIds"] == ["123"]
    assert payload["chatId"] == "-1001"


def test_send_preserves_explicit_topic_and_native_mention(tmp_path):
    store, host, transport, target = setup_group(tmp_path)
    result = asyncio.run(host.account_send(
        "role-1", "tg-1", target.id, "<正文>", actor="owner",
        message_thread_id=10, mention_ids=("123",),
    ))
    assert result.status == "sent"
    chat_id, content, options = transport.sent[0]
    assert chat_id == "-1001"
    assert '<a href="tg://user?id=123">@123</a>' in content
    assert "&lt;正文&gt;" in content
    assert options["message_thread_id"] == 10
    assert options["parse_mode"] == "HTML"
    thread = next(thread for thread in store.list_threads("role-1") if thread.channel == "telegram")
    assert store.sessions.list_messages(thread.session_key)[0].metadata["messageThreadId"] == 10


def test_send_rejects_unknown_topic_and_member_before_platform_call(tmp_path):
    _, host, transport, target = setup_group(tmp_path)
    for options in ({"message_thread_id": 99}, {"mention_ids": ("999",)}):
        with pytest.raises(UnsupportedTargetError):
            asyncio.run(host.account_send("role-1", "tg-1", target.id, "正文", actor="owner", **options))
    assert not transport.sent


def test_unspecified_topic_does_not_guess_last_active_topic(tmp_path):
    _, host, transport, target = setup_group(tmp_path)
    asyncio.run(host.account_send("role-1", "tg-1", target.id, "全群", actor="owner"))
    assert "message_thread_id" not in transport.sent[0][2]


def test_adapter_prevalidates_every_attachment_before_sending(tmp_path):
    from backend.app.channels.models import Attachment

    _, host, transport, target = setup_group(tmp_path)
    media_attempts = []

    async def send_photo(_token, _chat_id, _data, _name, **_options):
        media_attempts.append(_name)
        return {"message_id": 1}

    transport.send_photo = send_photo
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    adapter = host.adapter("tg-1")
    result = asyncio.run(adapter.send(OutboundMessage(target, "图片", attachments=(
        Attachment("image", str(image)), Attachment("file", str(tmp_path / "missing")),
    ))))
    assert result.status == "failed"
    assert not media_attempts
    assert not transport.sent
