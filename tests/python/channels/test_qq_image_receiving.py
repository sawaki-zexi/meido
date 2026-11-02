"""Image receipt outcomes through QQ admission, Runtime and the public API.

Only remote QQ/media and model I/O are doubles; all data is synthetic.
"""
import asyncio
import json

import httpx
import pytest

from backend.app import main
from backend.app.channels import media
from backend.app.channels.models import DownloadedMedia
from backend.app.chat_attachments import save_attachment
from backend.app.model_adapter import ModelTextDelta
from test_meme_turn import image_bytes, meme_turn, payload


QUOTE_STATUS = "本次未取得新的引用图片内容"
SECRET_URL = "https://qq.test/image?token=SYNTHETIC_SIGNED_SECRET"
SECRET_NAME = "C:\\private\\SYNTHETIC_IMAGE_PATH.png"
SECRET_ERROR = "SYNTHETIC_EXCEPTION_SECRET"


def images(value):
    if isinstance(value, list):
        return sum(images(item) for item in value)
    if isinstance(value, dict):
        return int(value.get("type") == "image_url") + sum(images(item) for item in value.values())
    return 0


def event_for(identifier, sender, *, private=False, quoted=False, text="请查看图片"):
    event = payload(identifier, sender=sender, image=False, quoted=quoted)
    event["message"] = [part for part in event["message"] if part["type"] != "text"]
    if text:
        event["message"].append({"type": "text", "data": {"text": text}})
    if private:
        event.update(message_type="private", user_id=int(sender))
        event.pop("group_id")
    return event


def add_image(event, url=SECRET_URL):
    event["message"].append({"type": "image", "data": {"file": SECRET_NAME, "url": url}})


def controlled_model(monkeypatch, requests):
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            yield ModelTextDelta("已确认本次接收状态")
    monkeypatch.setattr(main, "model_adapter", Model())


async def dispatch_and_read(state, event):
    try:
        await main._dispatch_channel_payload(state.adapter.account_id, event, source_adapter=state.adapter)
        private = event["message_type"] == "private"
        thread = state.channels.get_thread_by_chat(state.role.id, state.adapter.account_id,
            str(event["user_id"] if private else event["group_id"]), chat_type="private" if private else "group")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            response = await client.get(f"/api/roles/{state.role.id}/channels/threads/{thread.thread_id}/messages")
        assert response.status_code == 200, response.text
        result = response.json()
        return result, next(item for item in result["messages"]
            if item["metadata"].get("platformMessageId") == event["message_id"])
    finally:
        if main._proactive is not None:
            await main._proactive.close()


def export_ui(tmp_path, identifier, result, current):
    # Real public projection retained for optional offline browser verification.
    (tmp_path / f"ui-{identifier}.json").write_text(json.dumps({
        "id": identifier, "thread": result["thread"], "messages": result["messages"],
        "currentMessageId": current["id"], "forbiddenMarkers": [SECRET_URL, SECRET_NAME, SECRET_ERROR],
    }, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize("sender,private", [("22", False), ("11", False), ("11", True)],
    ids=["group-visitor", "group-owner", "private-owner"])
@pytest.mark.parametrize("failure,reason", [
    ("missing-url", "没有可用下载地址"), ("download", "图片下载失败或地址已失效"),
    ("too-large", "图片超过接收大小限制"), ("untrusted", "图片下载地址不可信"),
    ("timeout", "图片接收超时"), ("invalid-save", "图片存储空间不足或保存失败"),
    ("quota", "图片存储空间不足或保存失败"),
])
def test_image_failure_reaches_model_and_public_history(meme_turn, monkeypatch, tmp_path,
        sender, private, failure, reason):
    state, requests, downloads = meme_turn, [], []
    original = state.adapter._transport
    async def transport(operation, params):
        if operation == "send_private_msg":
            return {"message_id": "9001"}
        return await original(operation, params)
    async def download(url, *, max_bytes):
        downloads.append(url)
        if failure == "too-large":
            raise media.MediaTooLargeError(SECRET_ERROR + SECRET_URL)
        if failure == "untrusted":
            raise media.UntrustedMediaUrlError(SECRET_ERROR + SECRET_URL)
        if failure == "timeout":
            raise TimeoutError(SECRET_ERROR + SECRET_URL)
        if failure == "invalid-save":
            return b"not an image", "image/png"
        if failure == "quota":
            return image_bytes("PNG"), "image/png"
        raise OSError(SECRET_ERROR + SECRET_URL)
    monkeypatch.setattr(state.adapter, "_transport", transport)
    monkeypatch.setattr(media, "download_qq_media", download)
    if failure == "quota":
        monkeypatch.setattr(main, "_MAX_CHANNEL_ATTACHMENT_STORAGE_BYTES", 0)
    controlled_model(monkeypatch, requests)
    event = event_for("failed-current", sender, private=private)
    add_image(event, None if failure == "missing-url" else SECRET_URL)
    result, current = asyncio.run(asyncio.wait_for(dispatch_and_read(state, event), 10))
    assert len(requests) == 1
    assert reason in str(requests[0]) and "未读取图片内容" in str(requests[0])
    assert reason in current["content"] and "请查看图片" in current["content"]
    assert current["metadata"]["attachmentFailures"] == [{"kind": "image", "name": "图片", "reason": reason}]
    assert not current["metadata"]["attachments"] and images(requests[0]) == 0
    for marker in (SECRET_NAME, SECRET_URL, SECRET_ERROR):
        assert marker not in json.dumps(result, ensure_ascii=False) + str(requests)
    assert bool(downloads) is (failure != "missing-url")
    if sender == "22" and failure == "download":
        export_ui(tmp_path, "ordinary_failure", result, current)


@pytest.mark.parametrize("mode", ["pure", "mixed", "normal", "limit"])
def test_image_outcomes_preserve_success_and_bound_excess_failures(meme_turn, monkeypatch, tmp_path, mode):
    state, requests, downloads = meme_turn, [], []
    async def download(url, *, max_bytes):
        downloads.append(url)
        if url == SECRET_URL:
            raise OSError(SECRET_ERROR)
        return image_bytes("PNG"), "image/png"
    monkeypatch.setattr(media, "download_qq_media", download)
    controlled_model(monkeypatch, requests)
    event = event_for("mixed-current", "22", text="" if mode == "pure" else "请查看图片")
    add_image(event, SECRET_URL if mode in {"pure", "mixed"} else "https://qq.test/image")
    if mode == "mixed":
        add_image(event, "https://qq.test/image")
    if mode == "limit":
        for _ in range(40):
            add_image(event, "https://qq.test/image")
    result, current = asyncio.run(asyncio.wait_for(dispatch_and_read(state, event), 10))
    expected_images = {"pure": 0, "mixed": 1, "normal": 1, "limit": 4}[mode]
    assert len(requests) == 1 and images(requests[0]) == expected_images
    assert len(current["metadata"]["attachments"]) == expected_images
    failures = current["metadata"]["attachmentFailures"]
    assert len(failures) == (0 if mode == "normal" else 1)
    if mode != "normal":
        assert "未读取图片内容" in current["content"] and "未读取图片内容" in str(requests[0])
    if mode == "limit":
        assert len(downloads) == 4 and "每条消息最多接收 4 个附件" in current["content"]
        assert len(current["content"]) < 150
    if mode == "mixed":
        export_ui(tmp_path, "mixed", result, current)


@pytest.mark.parametrize("sender", ["11", "22"], ids=["owner", "visitor"])
@pytest.mark.parametrize("missing_url", [False, True])
def test_group_quote_failure_has_one_current_receipt_note(meme_turn, monkeypatch, tmp_path, sender, missing_url):
    state, requests = meme_turn, []
    original = state.adapter._transport
    async def transport(operation, params):
        if operation == "get_msg":
            return {"data": {"group_id": 100, "sender": {"user_id": 99}, "message": [
                {"type": "image", "data": {"file": SECRET_NAME, "url": None if missing_url else SECRET_URL}},
                {"type": "image", "data": {"file": SECRET_NAME, "url": SECRET_URL}},
            ]}}
        return await original(operation, params)
    async def download(*args, **kwargs):
        raise OSError(SECRET_ERROR)
    monkeypatch.setattr(state.adapter, "_transport", transport)
    monkeypatch.setattr(media, "download_qq_media", download)
    controlled_model(monkeypatch, requests)
    event = event_for("failed-quote", sender, quoted=True)
    result, current = asyncio.run(asyncio.wait_for(dispatch_and_read(state, event), 10))
    assert len(requests) == 1 and images(requests[0]) == 0
    assert current["content"] == "请查看图片\n[" + QUOTE_STATUS + "]"
    assert str(requests[0]).count(QUOTE_STATUS) == 1
    assert len(current["metadata"]["quotedAttachmentFailures"]) == 2
    for marker in (SECRET_NAME, SECRET_URL, SECRET_ERROR):
        assert marker not in json.dumps(result, ensure_ascii=False) + str(requests)
    if sender == "22" and not missing_url:
        export_ui(tmp_path, "quoted_failure", result, current)


@pytest.mark.parametrize("known", [False, True], ids=["invisible-original", "authorized-original"])
def test_private_owner_quote_uses_authorized_history_and_current_receipt_only(meme_turn, monkeypatch, known):
    state, requests = meme_turn, []
    # This fixture already has prior history, so the bot identity is known before intake freezes it.
    state.channels.set_platform_identity(state.adapter.account_id, "99")
    thread = state.channels.ensure_channel_thread(state.role.id, "qq", state.adapter.account_id, "11", "private")
    if known:
        historical = save_attachment(state.roles.workspace_path(state.role.id), "historical.png", "image/png", image_bytes("PNG"))
        state.sessions.append_message(thread.session_key, "user", "AUTHORIZED_ORIGINAL", metadata={
            "platformMessageId": "quoted-message", "senderIsOwner": True, "attachments": [historical],
        })
    async def transport(operation, params):
        if operation == "get_msg":
            return {"data": {"message_type": "private", "user_id": 11,
                "sender": {"user_id": 11, "nickname": "FETCHED_NAME_SECRET"}, "message": [
                    {"type": "text", "data": {"text": "FETCHED_QUOTE_SECRET"}},
                    {"type": "image", "data": {"file": SECRET_NAME, "url": SECRET_URL}},
                ]}}
        if operation == "send_private_msg":
            return {"message_id": "9002"}
        return {"data": []}
    async def download(*args, **kwargs):
        raise OSError(SECRET_ERROR)
    monkeypatch.setattr(state.adapter, "_transport", transport)
    monkeypatch.setattr(media, "download_qq_media", download)
    controlled_model(monkeypatch, requests)
    event = event_for("private-quote", "11", private=True, quoted=True)
    _, current = asyncio.run(asyncio.wait_for(dispatch_and_read(state, event), 10))
    assert len(requests) == 1
    user = next(item for item in reversed(requests[0]) if item["role"] == "user")
    assert str(user).count(QUOTE_STATUS) == 1
    assert current["content"] == "请查看图片\n[" + QUOTE_STATUS + "]"
    assert current["metadata"]["quotedAttachmentFailures"] == [{
        "kind": "image", "name": "图片", "reason": "图片下载失败或地址已失效",
    }]
    for marker in ("FETCHED_QUOTE_SECRET", "FETCHED_NAME_SECRET", SECRET_NAME, SECRET_URL, SECRET_ERROR):
        assert marker not in str(requests)
    if known:
        assert "AUTHORIZED_ORIGINAL" in str(user) and images(user) == 1
    else:
        assert "引用原文不在当前授权历史中或已缺失" in str(user) and images(user) == 0


@pytest.mark.parametrize("data,reason", [
    (None, "图片数据无效"), (b"x" * (10 * 1024 * 1024 + 1), "图片超过接收大小限制"),
], ids=["invalid-type", "oversize"])
def test_host_image_validation_failure_is_not_silently_dropped(meme_turn, monkeypatch, data, reason):
    state, requests = meme_turn, []
    async def download(*args, **kwargs):
        return data, "image/png"
    monkeypatch.setattr(media, "download_qq_media", download)
    controlled_model(monkeypatch, requests)
    event = event_for("invalid-download", "22")
    add_image(event)
    result, current = asyncio.run(asyncio.wait_for(dispatch_and_read(state, event), 10))
    assert reason in current["content"] and reason in str(requests[0])
    assert images(requests[0]) == 0 and not current["metadata"]["attachments"]
    assert SECRET_NAME not in json.dumps(result)


def test_host_sanitizes_uncontrolled_image_error_from_adapter(meme_turn, monkeypatch):
    state, requests = meme_turn, []
    async def download(*args, **kwargs):
        return (DownloadedMedia("image", b"", SECRET_NAME, error=SECRET_ERROR + SECRET_URL),)
    monkeypatch.setattr(state.adapter, "download_media", download)
    controlled_model(monkeypatch, requests)
    event = event_for("adapter-error", "22")
    add_image(event)
    result, current = asyncio.run(asyncio.wait_for(dispatch_and_read(state, event), 10))
    assert current["metadata"]["attachmentFailures"] == [{
        "kind": "image", "name": "图片", "reason": "图片下载失败或地址已失效",
    }]
    for marker in (SECRET_NAME, SECRET_URL, SECRET_ERROR):
        assert marker not in json.dumps(result, ensure_ascii=False) + str(requests)


@pytest.mark.parametrize("change", ["account", "group", "pairing"])
def test_revocation_during_failed_image_receipt_prevents_model(meme_turn, monkeypatch, change):
    state, requests = meme_turn, []
    async def download(*args, **kwargs):
        if change == "account":
            state.channels.set_account_state(state.adapter.account_id, enabled=False)
        elif change == "group":
            state.channels.set_group_replies(state.role.id, state.adapter.account_id, "100", False)
        else:
            state.channels.unbind_identity(state.channels.list_identities()[0]["identityId"])
        raise OSError(SECRET_ERROR)
    monkeypatch.setattr(media, "download_qq_media", download)
    controlled_model(monkeypatch, requests)
    event = event_for("revoked-image", "11")
    add_image(event)
    _, current = asyncio.run(asyncio.wait_for(dispatch_and_read(state, event), 10))
    assert not requests and current["metadata"]["channelTurnState"] == "invalidated"
    assert not current["metadata"]["attachments"] and not current["metadata"]["attachmentFailures"]
    assert not list(state.roles.workspace_path(state.role.id).glob("attachments/*.png"))


def test_untriggered_image_does_not_download_or_run_model(meme_turn, monkeypatch):
    state, requests = meme_turn, []
    state.channels.set_group_listening(state.role.id, state.adapter.account_id, "100", True)
    async def unexpected(*args, **kwargs):
        pytest.fail("ordinary listening must not fetch images or quotes")
    monkeypatch.setattr(media, "download_qq_media", unexpected)
    monkeypatch.setattr(state.adapter, "_transport", unexpected)
    controlled_model(monkeypatch, requests)
    event = payload("listened-image", sender="22", triggered=False)
    asyncio.run(asyncio.wait_for(main._dispatch_channel_payload(state.adapter.account_id, event,
        source_adapter=state.adapter), 10))
    assert not requests
    thread = state.channels.get_thread_by_chat(state.role.id, state.adapter.account_id, "100", chat_type="group")
    assert len(state.channels.listening.recent(state.role.id, thread.thread_id)) == 1


@pytest.mark.parametrize("one_failed", [False, True], ids=["normal-quote", "mixed-quote"])
def test_current_and_quote_batches_preserve_successful_images(meme_turn, monkeypatch, one_failed):
    state, requests, downloads = meme_turn, [], []
    original = state.adapter._transport
    async def transport(operation, params):
        if operation == "get_msg":
            return {"data": {"group_id": 100, "sender": {"user_id": 99}, "message": [
                {"type": "image", "data": {"file": "quoted.png", "url":
                    SECRET_URL if one_failed and index == 3 else "https://qq.test/image"}}
                for index in range(4)
            ]}}
        return await original(operation, params)
    async def download(url, *, max_bytes):
        downloads.append(url)
        if url == SECRET_URL:
            raise OSError(SECRET_ERROR)
        return image_bytes("PNG"), "image/png"
    monkeypatch.setattr(state.adapter, "_transport", transport)
    monkeypatch.setattr(media, "download_qq_media", download)
    controlled_model(monkeypatch, requests)
    event = event_for("two-batches", "22", quoted=True)
    for _ in range(4):
        add_image(event, "https://qq.test/image")
    _, current = asyncio.run(asyncio.wait_for(dispatch_and_read(state, event), 10))
    assert len(downloads) == 8 and len(current["metadata"]["attachments"]) == 4
    assert len(current["metadata"]["quotedAttachments"]) == 4 - int(one_failed)
    assert images(requests[0]) == 8 - int(one_failed)
    assert (QUOTE_STATUS in current["content"]) is one_failed
