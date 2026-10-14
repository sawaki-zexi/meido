from __future__ import annotations

import asyncio
import html
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import ChannelAdapter, ChannelAdapterError, PartialDeliveryError, UnsupportedTargetError, is_delivery_uncertain
from .models import ChannelCapabilities, DeliveryResult, DeliveryStatus, InboundEvent, OutboundMessage, Target, TargetKind


@dataclass(slots=True)
class TelegramStreamSession:
    target: Target
    message_id: str
    shown: str = "…"
    pending: str = ""
    last_update_at: float = 0.0
    task: asyncio.Task[None] | None = None
    error: BaseException | None = None


class TelegramAccountAdapter(ChannelAdapter):
    """Account-scoped Telegram sender; intake is owned by long polling service."""

    channel = "telegram"
    capabilities = ChannelCapabilities(
        private=True,
        group=True,
        group_listening=False,
        media=True,
        replies=True,
        streaming=True,
        targets=frozenset({TargetKind.PRIVATE, TargetKind.GROUP}),
    )

    def __init__(self, account_id: str, token_getter, transport) -> None:
        super().__init__()
        self.account_id = account_id
        self._token_getter = token_getter
        self._transport = transport
        self._targets: dict[str, Any] = {}

    def parse_event(self, payload: Mapping[str, Any]) -> InboundEvent | None:
        del payload
        return None

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        self._validate_target(message.target)
        options: dict[str, Any] = {"reply_to": message.reply_to_message_id}
        topic = message.target.metadata.get("message_thread_id")
        if isinstance(topic, int) and not isinstance(topic, bool) and topic > 0:
            options["message_thread_id"] = topic
        mentions = message.metadata.get("mentionIds", ())
        mentions = tuple(str(item) for item in mentions) if isinstance(mentions, (list, tuple)) else ()
        if mentions and any(not item.isdigit() for item in mentions):
            raise UnsupportedTargetError("Telegram 提及成员必须使用数字平台 ID")
        mention_text = " ".join(f"@{item}" for item in mentions)
        mention_html = " ".join(f'<a href="tg://user?id={item}">@{item}</a>' for item in mentions)
        if len(mention_text) >= 4096:
            raise UnsupportedTargetError("Telegram 提及文本超过消息长度限制")

        def render(content: str, *, include_mentions: bool) -> tuple[str, dict[str, Any]]:
            chunk_options = dict(options)
            if include_mentions and mentions:
                chunk_options["parse_mode"] = "HTML"
                return mention_html + (" " + html.escape(content) if content else ""), chunk_options
            return content, chunk_options

        visible_sends = 0
        try:
            token = self._token_getter()
            media = []
            for attachment in message.attachments:
                path = Path(attachment.url or "")
                if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
                    raise ChannelAdapterError("Telegram 出站附件不存在或超过大小限制")
                data = path.read_bytes()
                if len(data) > 10 * 1024 * 1024:
                    raise ChannelAdapterError("Telegram 出站附件超过大小限制")
                sender = getattr(self._transport, "send_photo", None) if attachment.kind == "image" else None
                if not callable(sender):
                    sender = getattr(self._transport, "send_document", None)
                if not callable(sender):
                    raise ChannelAdapterError("Telegram 传输不支持媒体发送")
                media.append((sender, data, attachment.file_name or path.name or "attachment"))

            caption_fits = bool(media) and len(message.content) + len(mention_text) + bool(mentions) <= 1024
            needs_text = not caption_fits and bool(message.content or mentions)
            text_sender = getattr(self._transport, "send_message", None)
            if needs_text and not callable(text_sender):
                raise ChannelAdapterError("Telegram 传输不支持文本发送")
            if not media and not needs_text:
                raise ChannelAdapterError("Telegram 出站消息不能为空")

            result = None
            if needs_text:
                remaining = message.content
                first = True
                while remaining or (first and mentions):
                    limit = 4096 - (len(mention_text) + bool(mentions) if first else 0)
                    chunk, remaining = remaining[:limit], remaining[limit:]
                    content, chunk_options = render(chunk, include_mentions=first)
                    result = await text_sender(token, message.target.chat_id, content, **chunk_options)
                    visible_sends += 1
                    if not isinstance(result, Mapping) or not result.get("message_id"):
                        raise PartialDeliveryError("Telegram 已接受文本消息，但未返回消息 ID")
                    first = False
            for index, (sender, data, file_name) in enumerate(media):
                caption, chunk_options = render(
                    message.content if caption_fits and index == 0 else "",
                    include_mentions=caption_fits and index == 0,
                )
                result = await sender(token, message.target.chat_id, data, file_name, caption=caption, **chunk_options)
                visible_sends += 1
                if not isinstance(result, Mapping) or not result.get("message_id"):
                    raise PartialDeliveryError("Telegram 已接受附件消息，但未返回消息 ID")
            assert result is not None
            message_id = result.get("message_id")
            return DeliveryResult(
                DeliveryStatus.SENT,
                message.target,
                str(message_id) if message_id is not None else None,
                raw={"message_id": message_id} if message_id is not None else {},
            )
        except Exception as error:
            return DeliveryResult(
                DeliveryStatus.UNCERTAIN if visible_sends or is_delivery_uncertain(error) else DeliveryStatus.FAILED,
                message.target,
                error=type(error).__name__,
            )

    async def list_targets(self):
        return list(self._targets.values())

    async def stream_start(self, target: Target, *, reply_to_message_id: str | None = None) -> TelegramStreamSession:
        self._validate_target(target)
        if target.kind is not TargetKind.PRIVATE:
            raise UnsupportedTargetError("Telegram 群聊只投递完整回复")
        result = await self._transport.send_message(
            self._token_getter(), target.chat_id, "…", reply_to=reply_to_message_id,
        )
        message_id = str(result.get("message_id") or "")
        if not message_id:
            raise PartialDeliveryError("Telegram 已接受预览消息，但未返回消息 ID")
        return TelegramStreamSession(target, message_id)

    async def stream_update(self, session: TelegramStreamSession, content: str) -> None:
        session.pending = content[:4096]
        if session.error is None and (session.task is None or session.task.done()):
            session.task = asyncio.create_task(self._flush_stream_updates(session))

    async def _flush_stream_updates(self, session: TelegramStreamSession) -> None:
        try:
            while session.pending != session.shown:
                delay = session.last_update_at + 0.8 - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                content = session.pending
                if not content or content == session.shown:
                    return
                await self._transport.edit_message_text(
                    self._token_getter(), session.target.chat_id, session.message_id, content,
                )
                session.shown = content
                session.last_update_at = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            session.error = error

    async def stream_finalize(self, session: TelegramStreamSession, content: str) -> DeliveryResult:
        if session.task is not None:
            await asyncio.gather(session.task, return_exceptions=True)
        try:
            first_chunk = content[:4096] or "…"
            if first_chunk != session.shown:
                await self._transport.edit_message_text(
                    self._token_getter(), session.target.chat_id, session.message_id, first_chunk,
                )
                session.shown = first_chunk
            for offset in range(4096, len(content), 4096):
                await self._transport.send_message(
                    self._token_getter(), session.target.chat_id, content[offset:offset + 4096],
                    reply_to=session.message_id,
                )
        except Exception as error:
            return DeliveryResult(
                DeliveryStatus.UNCERTAIN,
                session.target, session.message_id, error=type(error).__name__,
            )
        return DeliveryResult(DeliveryStatus.SENT, session.target, session.message_id)

    def _validate_target(self, target) -> None:
        if target.account_id != self.account_id or target.id not in {item.id for item in self._targets.values()}:
            raise UnsupportedTargetError("目标不在 Telegram 账号可达目录中")
        if target.kind not in self.capabilities.targets:
            raise UnsupportedTargetError("Telegram 不支持该目标类型")
