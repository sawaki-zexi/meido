from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import ChannelAdapter, ChannelAdapterError, PartialDeliveryError, UnsupportedTargetError, is_delivery_uncertain
from .models import ChannelCapabilities, DeliveryResult, DeliveryStatus, InboundEvent, OutboundMessage, Target, TargetKind
from .directory import DirectoryError, DirectoryObservation, DirectoryQuery
from .formatting import telegram_text, telegram_chunks, utf16_length
from .delivery import DeliveryAttempt, OutboundScheduler, StreamDelivery


@dataclass(slots=True)
class TelegramStreamSession:
    target: Target
    message_id: str
    shown: str = "…"
    pending: str = ""
    last_update_at: float = 0.0
    task: asyncio.Task[None] | None = None
    error: BaseException | None = None
    delivery: StreamDelivery | None = None


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

    def __init__(self, account_id: str, token_getter, transport, *, connection_snapshot=None, scheduler=None) -> None:
        super().__init__()
        self.account_id = account_id
        self._token_getter = token_getter
        self._transport = transport
        self._targets: dict[str, Any] = {}
        self._connection_snapshot = connection_snapshot
        self.scheduler = scheduler or OutboundScheduler(send_interval=2, edit_interval=5, typing_interval=8, account_interval=.25)

    def connection_snapshot(self) -> dict[str, object]:
        return self._connection_snapshot() if self._connection_snapshot is not None else super().connection_snapshot()

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
        if len(mention_text) >= 4096:
            raise UnsupportedTargetError("Telegram 提及文本超过消息长度限制")

        try:
            token = self._token_getter()
            media = []
            for attachment in message.attachments:
                path = Path(attachment.url or "")
                data = attachment.frozen_data
                if data is None:
                    if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
                        raise ChannelAdapterError("Telegram 出站附件不存在或超过大小限制")
                    data = path.read_bytes()
                elif not path.is_absolute():
                    raise ChannelAdapterError("Telegram 冻结附件必须保留原本地附件路径")
                if not isinstance(data, bytes) or len(data) > 10 * 1024 * 1024:
                    raise ChannelAdapterError("Telegram 出站附件超过大小限制")
                sender = getattr(self._transport, "send_photo", None) if attachment.kind == "image" else None
                if not callable(sender):
                    sender = getattr(self._transport, "send_document", None)
                if not callable(sender):
                    raise ChannelAdapterError("Telegram 传输不支持媒体发送")
                media.append((sender, data, attachment.file_name or path.name or "attachment"))

            rendered, entities = telegram_text(message.content, mentions=mentions)
            caption_fits = bool(media) and utf16_length(rendered) <= 1024
            needs_text = not caption_fits and bool(message.content or mentions)
            text_sender = getattr(self._transport, "send_message", None)
            if needs_text and not callable(text_sender):
                raise ChannelAdapterError("Telegram 传输不支持文本发送")
            if not media and not needs_text:
                raise ChannelAdapterError("Telegram 出站消息不能为空")

            operations = []
            if needs_text:
                for content, chunk_entities in telegram_chunks(rendered, entities):
                    chunk_options = {**options, **({'entities': chunk_entities} if chunk_entities else {})}
                    operations.append((text_sender, (token, message.target.chat_id, content), chunk_options,
                        {'kind': 'text', 'content': content}))
            for index, (sender, data, file_name) in enumerate(media):
                caption = rendered if caption_fits and index == 0 else ''
                chunk_options = {**options, **({'caption_entities': [entity.to_dict() for entity in entities]}
                    if caption and entities else {})}
                operations.append((sender, (token, message.target.chat_id, data, file_name),
                    {**chunk_options, 'caption': caption}, {'kind': 'media', 'content': caption, 'fileName': file_name, 'attachmentIndex': index}))
        except Exception as error:
            return DeliveryResult(DeliveryStatus.FAILED, message.target, error=type(error).__name__)
        async def send_part(part):
            method, args, kwargs, _ = operations[part['index']]
            kwargs = dict(kwargs)
            if part['withoutReply']:
                kwargs['reply_to'] = None
            if part['plainText']:
                kwargs.pop('entities', None)
                kwargs.pop('caption_entities', None)
            return await method(*args, **kwargs)
        attempt = DeliveryAttempt(message, scheduler=self.scheduler, max_attempts=5)
        return await attempt.run([item[3] for item in operations], send_part,
            receipt_id=lambda response: response.get('message_id') if isinstance(response, Mapping) else None)

    async def list_targets(self):
        return list(self._targets.values())

    async def query_directory(self, query: DirectoryQuery) -> DirectoryObservation:
        if query.kind != "member":
            raise DirectoryError("unsupported")
        request = getattr(self._transport, "get_chat_member", None)
        if not callable(request):
            raise DirectoryError("unsupported")
        result = await request(self._token_getter(), query.group_id, query.member_id)
        user = result.get("user")
        if not isinstance(user, Mapping) or str(user.get("id")) != query.member_id:
            raise DirectoryError("invalid_platform_response")
        status = result.get("status")
        if status not in {"creator", "administrator", "member", "restricted", "left", "kicked"}:
            raise DirectoryError("invalid_platform_response")
        name = " ".join(str(user.get(key) or "").strip() for key in ("first_name", "last_name")).strip()
        return DirectoryObservation(members=[{"kind": "member", "memberId": query.member_id,
            "groupId": query.group_id, "accountId": self.account_id, "displayName": name,
            "username": str(user.get("username") or ""), "status": status,
            "isMember": status not in {"left", "kicked"} and (status != "restricted" or result.get("is_member") is True),
            "canSend": False, "sendTargetId": None}])

    async def stream_start(self, target: Target, *, reply_to_message_id: str | None = None,
                           outbound: OutboundMessage | None = None) -> TelegramStreamSession:
        self._validate_target(target)
        if target.kind is not TargetKind.PRIVATE:
            raise UnsupportedTargetError('Telegram 群聊只投递完整回复')
        message = outbound or OutboundMessage(target, '', reply_to_message_id=reply_to_message_id)
        delivery = StreamDelivery(message, self.scheduler, max_attempts=5)
        async def create(part):
            return await self._transport.send_message(self._token_getter(), target.chat_id, '…',
                reply_to=None if part['withoutReply'] else reply_to_message_id)
        result = await delivery.update('', create, receipt_id=lambda response: response.get('message_id'), kind='send', preview=True)
        session = TelegramStreamSession(target, result.platform_message_id or '', delivery=delivery)
        return session

    async def stream_update(self, session: TelegramStreamSession, content: str) -> None:
        session.delivery.require_current()
        session.pending = content
        if session.delivery.error is None and (session.task is None or session.task.done()):
            session.task = asyncio.create_task(self._flush_stream_updates(session))

    async def _edit_stream(self, session, text, entities, part):
        options = {'entities': [] if part['plainText'] else entities} if entities else {}
        return await self._transport.edit_message_text(self._token_getter(), session.target.chat_id,
            session.message_id, text, **options)

    async def _flush_stream_updates(self, session: TelegramStreamSession) -> None:
        try:
            while session.pending != session.shown and session.delivery.error is None:
                pending = session.pending
                text, entities = telegram_text(pending)
                chunks = telegram_chunks(text, entities)
                if not chunks:
                    return
                first, chunk_entities = chunks[0]
                await session.delivery.update(first,
                    lambda part: self._edit_stream(session, first, chunk_entities, part),
                    receipt_id=lambda response: response.get('message_id'))
                session.shown = pending
        except asyncio.CancelledError:
            raise
        except Exception as error:
            session.error = error

    async def stream_finalize(self, session: TelegramStreamSession, content: str) -> DeliveryResult:
        if session.task is not None:
            await asyncio.gather(session.task, return_exceptions=True)
        text, entities = telegram_text(content or '…')
        chunks = telegram_chunks(text, entities)
        parts = [{'kind': 'text', 'content': chunk} for chunk, _ in chunks]
        async def send(part):
            chunk, chunk_entities = chunks[part['index']]
            if part['index'] == 0:
                return await self._edit_stream(session, chunk, chunk_entities, part)
            options = {'entities': chunk_entities} if chunk_entities and not part['plainText'] else {}
            return await self._transport.send_message(self._token_getter(), session.target.chat_id, chunk,
                reply_to=None if part['withoutReply'] else session.delivery.message.reply_to_message_id, **options)
        return await session.delivery.finish(parts, send, receipt_id=lambda response: response.get('message_id'))

    def _validate_target(self, target) -> None:
        if target.account_id != self.account_id or target.id not in {item.id for item in self._targets.values()}:
            raise UnsupportedTargetError("目标不在 Telegram 账号可达目录中")
        if target.kind not in self.capabilities.targets:
            raise UnsupportedTargetError("Telegram 不支持该目标类型")
