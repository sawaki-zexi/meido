"""A group can stay silent while the account and other chats remain usable."""
import asyncio

import pytest

from backend.app import main
from backend.app.channels import DeliveryStatus, TargetKind, target_from_chat
from backend.app.channels.base import UnsupportedTargetError
from backend.app.channels.models import Attachment
from backend.app.channels import media
from backend.app.model_adapter import ModelTextDelta
from test_meme_turn import image_bytes, meme_turn, payload


@pytest.mark.parametrize("quoted", [False, True], ids=["mention", "quote"])
@pytest.mark.parametrize("listening", [False, True])
def test_disabled_group_does_not_generate_or_reply_even_when_addressed(meme_turn, monkeypatch, quoted, listening):
    s = meme_turn
    s.raw = image_bytes("PNG")
    s.channels.set_group_replies(s.role.id, s.adapter.account_id, "100", False)
    if listening:
        s.channels.set_group_listening(s.role.id, s.adapter.account_id, "100", True)
    calls = []

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            calls.append(messages)
            yield ModelTextDelta("不应发送")

    monkeypatch.setattr(main, "model_adapter", Model())

    async def scenario():
        try:
            await main._dispatch_channel_payload(s.adapter.account_id,
                payload("disabled", image=False, quoted=quoted), source_adapter=s.adapter)
            assert calls == []
            assert not any(operation.startswith("send_") for operation, _ in s.operations)
            thread = s.channels.get_thread_by_chat(s.role.id, s.adapter.account_id, "100", chat_type="group")
            if listening:
                assert len(s.channels.listening.recent(s.role.id, thread.thread_id)) == 1
            assert thread is None or not s.sessions.list_messages(thread.session_key)
        finally:
            if main._proactive is not None:
                await main._proactive.close()

    asyncio.run(asyncio.wait_for(scenario(), 10))


@pytest.mark.parametrize("actor", ["owner", "reply"])
def test_disabled_group_rejects_tool_and_control_reply_delivery(meme_turn, actor):
    s = meme_turn
    thread = s.channels.ensure_channel_thread(s.role.id, "qq", s.adapter.account_id, "100", "group")
    target = target_from_chat(account_id=s.adapter.account_id, chat_id="100", kind=TargetKind.GROUP)
    s.channels.remember_targets(s.role.id, [target])
    s.channels.set_group_replies(s.role.id, s.adapter.account_id, "100", False)

    async def scenario():
        with pytest.raises(UnsupportedTargetError):
            await main.channel_host.account_send(s.role.id, s.adapter.account_id, target.id,
                "不得绕过本群开关", actor=actor, source_thread_id=thread.thread_id)
        event = s.adapter.parse_event(payload("retry", image=False))
        result = await main.channel_host.send_retry_notice(s.adapter, event)
        assert result.status is DeliveryStatus.FAILED
        assert s.operations == []
    asyncio.run(scenario())


def test_group_disabled_while_preparing_media_cannot_dispatch_after_reenable(meme_turn, monkeypatch):
    s = meme_turn
    target = target_from_chat(account_id=s.adapter.account_id, chat_id="100", kind=TargetKind.GROUP)
    s.channels.remember_targets(s.role.id, [target])

    async def prepare(_item):
        s.channels.set_group_replies(s.role.id, s.adapter.account_id, "100", False)
        s.channels.set_group_replies(s.role.id, s.adapter.account_id, "100", True)
        return {"type": "image", "data": {"file": "fixture"}}

    monkeypatch.setattr(s.adapter, "_image_segment", prepare)

    async def scenario():
        result = await main.channel_host.account_send(s.role.id, s.adapter.account_id, target.id,
            "旧图片", actor="owner", attachments=(Attachment("image", "fixture"),))
        assert result.status is DeliveryStatus.FAILED
        assert s.operations == []
    asyncio.run(scenario())


@pytest.mark.parametrize("blocking_group", [100, 200])
def test_group_toggle_does_not_revive_input_waiting_behind_another_download(meme_turn, monkeypatch, blocking_group):
    s = meme_turn
    downloading, release, received = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls, sends = [], []

    async def download(_url, *, max_bytes):
        downloading.set()
        await release.wait()
        return image_bytes("PNG"), "image/png"

    async def transport(operation, params):
        assert operation == "send_group_msg"
        sends.append(params)
        return {"message_id": 1000 + len(sends)}

    accept = main.channel_host.accept_event
    async def observe(account_id, data):
        event = await accept(account_id, data)
        if event is not None and event.external_message_id == "waiting":
            received.set()
        return event

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            calls.append(messages)
            yield ModelTextDelta("回复")

    monkeypatch.setattr(media, "download_qq_media", download)
    monkeypatch.setattr(s.adapter, "_transport", transport)
    monkeypatch.setattr(main.channel_host, "accept_event", observe)
    monkeypatch.setattr(main, "model_adapter", Model())

    async def scenario():
        original = payload("downloading")
        original["group_id"] = blocking_group
        first = asyncio.create_task(main._dispatch_channel_payload(s.adapter.account_id, original, source_adapter=s.adapter))
        waiting = None
        try:
            await asyncio.wait_for(downloading.wait(), 3)
            waiting = asyncio.create_task(main._dispatch_channel_payload(s.adapter.account_id,
                payload("waiting", image=False), source_adapter=s.adapter))
            await asyncio.wait_for(received.wait(), 3)
            s.channels.set_group_replies(s.role.id, s.adapter.account_id, "100", False)
            s.channels.set_group_replies(s.role.id, s.adapter.account_id, "100", True)
            release.set()
            await asyncio.gather(first, waiting)
            assert [item["group_id"] for item in sends] == ([] if blocking_group == 100 else [200])
            assert len(calls) == (0 if blocking_group == 100 else 1)
        finally:
            release.set()
            await asyncio.gather(first, *([waiting] if waiting else []), return_exceptions=True)
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(asyncio.wait_for(scenario(), 10))


@pytest.mark.parametrize("reenable", [False, True])
def test_disabling_during_generation_prevents_late_reply_even_after_reenable(meme_turn, monkeypatch, reenable):
    s = meme_turn
    started, release = asyncio.Event(), asyncio.Event()

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            started.set()
            await release.wait()
            yield ModelTextDelta("关闭前开始生成的旧回复")

    monkeypatch.setattr(main, "model_adapter", Model())

    async def scenario():
        pending = asyncio.create_task(main._dispatch_channel_payload(s.adapter.account_id,
            payload("running", image=False), source_adapter=s.adapter))
        try:
            await asyncio.wait_for(started.wait(), 5)
            s.channels.set_group_replies(s.role.id, s.adapter.account_id, "100", False)
            if reenable:
                s.channels.set_group_replies(s.role.id, s.adapter.account_id, "100", True)
            release.set()
            await pending
            assert not any(operation.startswith("send_") for operation, _ in s.operations)
        finally:
            release.set()
            await pending
            if main._proactive is not None:
                await main._proactive.close()

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_group_toggle_during_quote_resolution_cannot_admit_the_old_message(meme_turn, monkeypatch):
    s = meme_turn
    resolving, release = asyncio.Event(), asyncio.Event()
    calls, sends = [], []

    async def transport(operation, params):
        if operation == "get_msg":
            resolving.set()
            await release.wait()
            return {"data": {"group_id": 100, "sender": {"user_id": 99}, "message": []}}
        sends.append((operation, params))
        return {"message_id": 1001}

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            calls.append(messages)
            yield ModelTextDelta("不得回复旧引用")

    monkeypatch.setattr(s.adapter, "_transport", transport)
    monkeypatch.setattr(main, "model_adapter", Model())

    async def scenario():
        pending = asyncio.create_task(main._dispatch_channel_payload(s.adapter.account_id,
            payload("resolving", image=False, quoted=True), source_adapter=s.adapter))
        try:
            await asyncio.wait_for(resolving.wait(), 3)
            s.channels.set_group_replies(s.role.id, s.adapter.account_id, "100", False)
            s.channels.set_group_replies(s.role.id, s.adapter.account_id, "100", True)
            release.set()
            await pending
            assert calls == [] and sends == []
        finally:
            release.set()
            await pending
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(asyncio.wait_for(scenario(), 10))
