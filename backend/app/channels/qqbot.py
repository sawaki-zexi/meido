from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .base import ChannelAdapter, ChannelAdapterError, PartialDeliveryError, SendTransport, UnsupportedTargetError, delivery_status_for_error, is_blocked_identity
from .models import Attachment, ChannelCapabilities, ChatType, DeliveryResult, DeliveryStatus, DownloadedMedia, InboundEvent, OutboundMessage, Target, TargetKind, target_from_chat

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class QQBotConfig:
    account_id: str
    app_id: str
    endpoint: str = "https://api.sgroup.qq.com"
    token_key: str | None = None
    private_enabled: bool = True
    blocked_sender_ids: set[str] = field(default_factory=set)
    max_attachment_bytes: int = 10 * 1024 * 1024


@dataclass(slots=True)
class QQBotStreamSession:
    """State for one official QQBot C2C replacement stream."""

    target: Target
    msg_id: str
    msg_seq: int
    stream_msg_id: str | None = None
    index: int = 0
    completed: bool = False
    error: Exception | None = None


class QQBotAdapter(ChannelAdapter):
    channel = "qqbot"
    capabilities = ChannelCapabilities(private=True, group=False, media=True, replies=True, streaming=True, targets=frozenset({TargetKind.PRIVATE}))

    def __init__(self, config: QQBotConfig, *, transport: SendTransport | None = None) -> None:
        super().__init__()
        self.config = config
        self.account_id = config.account_id
        self._transport = transport
        self._targets: dict[str, Target] = {}

    def gateway_identify(self, *, access_token: str) -> dict[str, Any]:
        """Return the non-secret Gateway identify payload for the host loop."""

        if not self.config.app_id:
            raise ChannelAdapterError("QQBot app_id 未配置")
        if not access_token:
            raise ChannelAdapterError("QQBot access token 未配置")
        return {"op": 2, "d": {"token": f"QQBot {access_token}", "intents": 1 << 25, "shard": [0, 1]}}

    def parse_event(self, payload: Mapping[str, Any]) -> InboundEvent | None:
        if not self.config.private_enabled:
            return None
        if payload.get("t") not in {"C2C_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"}:
            return None
        data = payload.get("d") if isinstance(payload.get("d"), dict) else payload
        if str(data.get("message_type", "c2c")).lower() not in {"c2c", "private"}:
            return None
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        sender_id = str(author.get("user_openid", author.get("id", "")))
        message_id = str(data.get("id", data.get("message_id", "")))
        sender_name = str(author.get("username", author.get("nick", author.get("nickname", ""))))
        aliases = (sender_name, author.get("username"), author.get("nick"), author.get("nickname"), author.get("display_name"))
        if not sender_id or not message_id or is_blocked_identity(self.config.blocked_sender_ids, sender_ids=(sender_id,), aliases=aliases):
            return None
        content = str(data.get("content", "")).strip()
        attachments = tuple(Attachment(kind="image", url=str(item.get("url"))) for item in data.get("attachments", []) if isinstance(item, dict) and item.get("url"))
        event = InboundEvent(
            channel=self.channel,
            account_id=self.account_id,
            external_event_id=f"{self.account_id}:{message_id}",
            external_message_id=message_id,
            chat_id=sender_id,
            chat_type=ChatType.PRIVATE,
            sender_id=sender_id,
            sender_name=sender_name,
            content=content,
            attachments=attachments,
            reply_to_message_id=str(data.get("msg_id")) if data.get("msg_id") else None,
            metadata={"app_id": self.config.app_id, "event_type": payload.get("t")},
        )
        self._targets[event.chat_id] = target_from_chat(
            account_id=self.account_id,
            chat_id=event.chat_id,
            kind=TargetKind.PRIVATE,
            display_name=event.sender_name,
            external_message_id=message_id,
            event_id=message_id,
        )
        return event

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        self._validate_target(message.target)
        if self._transport is None:
            raise ChannelAdapterError("QQBot transport is not configured")
        visible_sends = 0
        first_message_id: str | None = None
        last_result: Mapping[str, Any] = {}
        try:
            images = [await self._read_image(attachment) for attachment in message.attachments]
            if message.content or not images:
                params: dict[str, Any] = {"openid": message.target.chat_id, "content": message.content}
                if message.reply_to_message_id:
                    params["msg_id"] = message.reply_to_message_id
                last_result = await self._transport("send_c2c", params)
                visible_sends += 1
                first_message_id = _response_message_id(last_result)
                if first_message_id is None:
                    raise PartialDeliveryError("QQBot 文本发送没有返回真实消息回执")
            for data in images:
                upload = await self._transport("upload_c2c_file", {
                    "openid": message.target.chat_id,
                    "body": {
                        "file_type": 1,
                        "file_data": base64.b64encode(data).decode("ascii"),
                        "srv_send_msg": False,
                    },
                })
                file_info = str(upload.get("file_info", "")).strip()
                if not file_info:
                    raise ChannelAdapterError("QQBot 图片上传响应缺少 file_info")
                last_result = await self._transport("send_c2c_image", {
                    "openid": message.target.chat_id,
                    "file_info": file_info,
                    "msg_seq": _next_msg_seq(),
                })
                visible_sends += 1
                image_message_id = _response_message_id(last_result)
                if image_message_id is None:
                    raise PartialDeliveryError("QQBot 图片发送没有返回真实消息回执", platform_message_id=first_message_id)
                first_message_id = first_message_id or image_message_id
            return DeliveryResult(
                DeliveryStatus.SENT, message.target, first_message_id, raw=dict(last_result),
            )
        except Exception as error:
            status = DeliveryStatus.UNCERTAIN if visible_sends else delivery_status_for_error(error)
            return DeliveryResult(status, message.target, first_message_id, error=type(error).__name__, raw=dict(last_result))

    async def _read_image(self, attachment: Attachment) -> bytes:
        if attachment.kind.lower() != "image" or not attachment.url:
            raise ChannelAdapterError("官方 QQBot 出站仅支持本地图片附件")
        if attachment.url.startswith(("http://", "https://")):
            raise ChannelAdapterError("QQBot 图片必须来自已授权的本地附件")
        path = Path(attachment.url)
        data = attachment.frozen_data
        if not path.is_absolute() or (data is None and not path.is_file()):
            raise ChannelAdapterError("QQBot 图片必须来自已解析的本地附件")
        limit = max(1, self.config.max_attachment_bytes)
        if data is None:
            try:
                if path.stat().st_size <= 0 or path.stat().st_size > limit:
                    raise ChannelAdapterError("QQBot 图片为空或超过大小限制")
                data = await asyncio.to_thread(path.read_bytes)
            except OSError as error:
                raise ChannelAdapterError("QQBot 图片附件无法读取") from error
        if not isinstance(data, bytes) or not data or len(data) > limit:
            raise ChannelAdapterError("QQBot 图片超过大小限制")
        mime_type = _image_mime_from_signature(data)
        if mime_type is None:
            raise ChannelAdapterError("QQBot 图片仅支持 PNG、JPEG、WebP 或 GIF")
        declared = (attachment.mime_type or "").lower().split(";", 1)[0].strip()
        if declared and declared not in {mime_type, "application/octet-stream"}:
            raise ChannelAdapterError("QQBot 图片内容与附件类型不匹配")
        return data

    async def stream_start(
        self,
        target: Target,
        *,
        title: str = "",
        reply_to_message_id: str | None = None,
    ) -> QQBotStreamSession:
        """Prepare a C2C stream tied to the inbound message being answered.

        QQBot's stream endpoint edits a message associated with an inbound
        ``msg_id``; it cannot be opened as an arbitrary proactive message.
        The target directory therefore carries the last observed message ID.
        """

        self._validate_target(target)
        msg_id = str(
            reply_to_message_id
            or target.metadata.get("external_message_id")
            or target.metadata.get("msg_id")
            or ""
        )
        if not msg_id:
            raise ChannelAdapterError("QQBot 流式回复缺少关联的入站消息 ID")
        return QQBotStreamSession(target, msg_id, _next_msg_seq())

    async def stream_update(self, session: QQBotStreamSession, content: str) -> None:
        if session.completed:
            raise ChannelAdapterError("QQBot 流式会话已经结束")
        if session.error is not None:
            return
        try:
            result = await self._send_stream_update(session, content, terminal=False)
            if not _stream_message_id(result) and not session.stream_msg_id:
                raise PartialDeliveryError("QQBot 流式响应缺少消息 ID")
        except Exception as error:
            session.error = error

    async def stream_finalize(self, session: QQBotStreamSession, content: str) -> DeliveryResult:
        if session.completed:
            return DeliveryResult(DeliveryStatus.SENT, session.target, session.stream_msg_id)
        if session.error is not None and not _definitely_rejected(session.error):
            return DeliveryResult(
                delivery_status_for_error(session.error), session.target, session.stream_msg_id,
                error=type(session.error).__name__,
            )
        try:
            result = await self._send_stream_update(session, content, terminal=True)
            message_id = _stream_message_id(result) or session.stream_msg_id
            if not message_id:
                raise PartialDeliveryError("QQBot 流式响应缺少消息 ID")
            return DeliveryResult(DeliveryStatus.SENT, session.target, message_id, raw=dict(result))
        except Exception as error:
            if not _definitely_rejected(error):
                return DeliveryResult(
                    delivery_status_for_error(error), session.target, session.stream_msg_id,
                    error=type(error).__name__,
                )
            if session.stream_msg_id:
                try:
                    assert self._transport is not None
                    await self._transport("delete_c2c", {
                        "openid": session.target.chat_id,
                        "message_id": session.stream_msg_id,
                    })
                except Exception as cleanup_error:
                    return DeliveryResult(
                        delivery_status_for_error(cleanup_error), session.target, session.stream_msg_id,
                        error=type(cleanup_error).__name__,
                    )
            fallback = await self.send(OutboundMessage(session.target, content))
            session.completed = fallback.status is DeliveryStatus.SENT
            return fallback

    async def _send_stream_update(
        self, session: QQBotStreamSession, content: str, *, terminal: bool
    ) -> Mapping[str, Any]:
        if self._transport is None:
            raise ChannelAdapterError("QQBot transport is not configured")
        body: dict[str, Any] = {
            "input_mode": "replace",
            "input_state": 10 if terminal else 1,
            "content_type": "markdown",
            "content_raw": content,
            "event_id": session.msg_id,
            "msg_id": session.msg_id,
            "msg_seq": session.msg_seq,
            "index": session.index,
        }
        if session.stream_msg_id:
            body["stream_msg_id"] = session.stream_msg_id
        result = await self._transport("stream_c2c", {"openid": session.target.chat_id, "body": body})
        stream_id = _stream_message_id(result)
        if stream_id:
            session.stream_msg_id = stream_id
        session.index += 1
        if terminal:
            session.completed = True
        return result

    async def list_targets(self) -> list[Target]:
        return list(self._targets.values())

    def _validate_target(self, target: Target) -> None:
        if target.account_id != self.account_id or target.id not in {item.id for item in self._targets.values()}:
            raise UnsupportedTargetError("目标不在 QQBot C2C 可达目录中")
        if target.kind is not TargetKind.PRIVATE:
            raise UnsupportedTargetError("官方 QQBot 首期仅支持 C2C 私聊")

    async def download_media(self, event: InboundEvent, *, max_bytes: int | None = None) -> tuple[DownloadedMedia, ...]:
        from .media import download_qq_media

        limit = self.config.max_attachment_bytes if max_bytes is None else max(1, max_bytes)
        downloaded: list[DownloadedMedia] = []
        for attachment in event.attachments[:4]:
            if attachment.kind != "image" or not attachment.url:
                continue
            try:
                data, mime_type = await download_qq_media(attachment.url, max_bytes=limit)
            except Exception as error:
                logger.warning("QQBot 附件下载失败 account=%s kind=%s error=%s", self.account_id, attachment.kind, type(error).__name__)
                continue
            downloaded.append(DownloadedMedia("image", data, attachment.file_name, mime_type))
        return tuple(downloaded)


def _next_msg_seq() -> int:
    return int(time.time() * 1000) % 65536


def _response_message_id(result: Mapping[str, Any]) -> str | None:
    value = result.get("id", result.get("message_id"))
    return str(value).strip() if value else None


def _image_mime_from_signature(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None


def _stream_message_id(result: Mapping[str, Any]) -> str | None:
    value = result.get("id", result.get("message_id"))
    if not value and isinstance(result.get("data"), Mapping):
        value = result["data"].get("id", result["data"].get("message_id"))
    return str(value).strip() if value else None


def _http_status(error: BaseException) -> int | None:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _definitely_rejected(error: BaseException) -> bool:
    status = _http_status(error)
    return status is not None and 400 <= status < 500 and status != 408
