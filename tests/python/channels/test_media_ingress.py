from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.app import main
from backend.app.channels.base import ChannelAdapterError
from backend.app.channels.models import Attachment, ChatType, DownloadedMedia, InboundEvent
from backend.app.channels.qqbot import QQBotAdapter, QQBotConfig
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore


class MemoryCredentials:
    def get(self, _key: str) -> str:
        return ""

    def set(self, _key: str, _value: str) -> None:
        return None


def _event(*, chat_type: ChatType, triggered: bool) -> InboundEvent:
    return InboundEvent(
        channel="qq",
        account_id="qq-1",
        external_event_id="event-1",
        external_message_id="message-1",
        chat_id="group-1" if chat_type is ChatType.GROUP else "user-1",
        chat_type=chat_type,
        sender_id="user-1",
        attachments=(Attachment("image", url="https://img.qpic.cn/signed?token=secret", file_name="photo.png"),),
        triggered=triggered,
        metadata={"raw_message": "[CQ:image,url=https://img.qpic.cn/signed?token=secret]"},
    )


def test_channel_media_is_persisted_for_triggered_messages_but_not_group_listening(tmp_path: Path, monkeypatch) -> None:
    class Workspace:
        def workspace_path(self, role_id: str) -> Path:
            return tmp_path / role_id

    class Adapter:
        calls = 0

        async def download_media(self, _event):
            self.calls += 1
            return (DownloadedMedia("image", b"\x89PNG\r\n\x1a\nimage", "photo.png", "image/png"),)

    adapter = Adapter()
    monkeypatch.setattr(main, "store", Workspace())
    monkeypatch.setattr(main, "channel_adapters", {"qq-1": adapter})

    listening = asyncio.run(main._persist_channel_attachments(_event(chat_type=ChatType.GROUP, triggered=False), "role-1"))
    assert listening == []
    assert adapter.calls == 0

    saved = asyncio.run(main._persist_channel_attachments(_event(chat_type=ChatType.GROUP, triggered=True), "role-1"))
    assert adapter.calls == 1
    assert len(saved) == 1
    assert saved[0]["kind"] == "image"
    assert saved[0]["mediaType"] == "image/png"
    assert Path(str(saved[0]["path"])).is_file()
    assert "token=secret" not in str(saved[0])


def test_live_channel_message_event_redacts_local_attachment_details(tmp_path: Path) -> None:
    sessions = SessionStore(tmp_path / "sessions.db")
    store = MultichannelStore(tmp_path / "sessions.db", sessions, MemoryCredentials())
    store.register_channel_account("role-1", "qq", account_id="qq-1", enabled=True)
    thread = store.ensure_channel_thread("role-1", "qq", "qq-1", "user-1", "private")
    attachment = main.save_attachment(tmp_path / "role-1", "notes.txt", "text/plain", b"private text")
    queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    store._subscribers["role-1"] = {queue}

    accepted, _ = store.append_event(
        thread,
        direction="inbound",
        content="[收到附件]",
        external_message_id="message-1",
        metadata={"attachments": [attachment]},
    )

    payload = queue.get_nowait()
    public_attachment = payload["message"]["metadata"]["attachments"][0]
    assert accepted
    assert public_attachment["id"] == attachment["id"]
    assert "path" not in public_attachment
    assert "text" not in public_attachment


def test_qqbot_inbound_media_uses_bounded_qq_host_downloads(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []

    async def fake_download(url: str, *, max_bytes: int):
        calls.append((url, max_bytes))
        return b"image", "image/png"

    monkeypatch.setattr("backend.app.channels.media.download_qq_media", fake_download)
    adapter = QQBotAdapter(QQBotConfig(account_id="qqbot-1", app_id="app", max_attachment_bytes=32))
    event = InboundEvent(
        channel="qqbot",
        account_id="qqbot-1",
        external_event_id="event-2",
        external_message_id="message-2",
        chat_id="openid-1",
        chat_type=ChatType.PRIVATE,
        sender_id="openid-1",
        attachments=(Attachment("image", url="https://multimedia.nt.qq.com.cn/file?id=1"),),
    )

    downloaded = asyncio.run(adapter.download_media(event))

    assert calls == [("https://multimedia.nt.qq.com.cn/file?id=1", 32)]
    assert downloaded == (DownloadedMedia("image", b"image", None, "image/png"),)


def test_qq_media_downloader_accepts_official_qq_com_cn_hosts_and_rejects_private_urls(monkeypatch) -> None:
    class Response:
        headers = {"content-length": "5", "content-type": "image/png"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield b"image"

    class Client:
        def __init__(self, *, timeout, follow_redirects):
            assert timeout == 20
            assert follow_redirects is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, method, url):
            assert method == "GET"
            assert url.startswith("https://multimedia.nt.qq.com.cn/")
            return Response()

    monkeypatch.setattr("backend.app.channels.media.httpx.AsyncClient", Client)

    from backend.app.channels.media import download_qq_media

    data, mime_type = asyncio.run(download_qq_media("https://multimedia.nt.qq.com.cn/image?id=1", max_bytes=8))
    assert data == b"image"
    assert mime_type == "image/png"
    with pytest.raises(ChannelAdapterError, match="可信 QQ 媒体域名"):
        asyncio.run(download_qq_media("https://127.0.0.1/private"))
