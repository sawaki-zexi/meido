from __future__ import annotations

import asyncio
import json
import base64
import binascii
import logging
import mimetypes
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from .base import ChannelAdapter, ChannelAdapterError, PartialDeliveryError, SendTransport, UnsupportedTargetError, delivery_status_for_error, is_blocked_identity
from .models import Attachment, ChannelCapabilities, ChatType, DeliveryResult, DeliveryStatus, InboundEvent, OutboundMessage, Target, TargetKind, target_from_chat
from .formatting import markdown_chunks, markdown_renderings, feishu_card
from .delivery import DeliveryAttempt, OutboundScheduler, StreamDelivery

logger = logging.getLogger(__name__)
_LIVE_CARD_ELEMENT_ID = "reply_md"
_LIVE_CARD_UPDATE_INTERVAL = 0.5


@dataclass(slots=True)
class FeishuConfig:
    account_id: str
    app_id: str
    endpoint: str = "https://open.feishu.cn/open-apis"
    app_secret_key: str | None = None
    verification_token_key: str | None = None
    private_enabled: bool = True
    blocked_sender_ids: set[str] = field(default_factory=set)
    max_attachment_bytes: int = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FeishuMedia:
    """A bounded Feishu resource downloaded for the current turn.

    The adapter deliberately does not write files.  The host may persist the
    bytes through its attachment store and pass the resulting local reference
    to the model/runtime.
    """

    kind: str
    data: bytes
    file_name: str | None = None
    mime_type: str | None = None
    file_key: str | None = None


@dataclass(slots=True)
class FeishuStreamSession:
    """Opaque CardKit stream handle returned by :meth:`stream_start`."""

    card_id: str
    target: Target
    message_id: str | None = None
    sequence: int = 0
    shown: str = ""
    pending: str = ""
    last_update_at: float = 0.0
    failed: bool = False
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    fallback: bool = False
    reply_to_message_id: str | None = None
    delivery: StreamDelivery | None = None


class FeishuAdapter(ChannelAdapter):
    channel = "feishu"
    capabilities = ChannelCapabilities(private=True, group=False, media=True, replies=True, streaming=True, targets=frozenset({TargetKind.PRIVATE}))

    def __init__(self, config: FeishuConfig, *, transport: SendTransport | None = None, scheduler=None) -> None:
        super().__init__()
        self.config = config
        self.account_id = config.account_id
        self._transport = transport
        self.scheduler = scheduler or OutboundScheduler(edit_interval=.5)
        self._targets: dict[str, Target] = {}

    def webhook_challenge(self, payload: Mapping[str, Any], *, expected_token: str | None = None) -> dict[str, str] | None:
        """Handle Feishu URL verification without creating a chat event."""

        if payload.get("type") != "url_verification":
            return None
        token = payload.get("token")
        if expected_token and token != expected_token:
            raise ChannelAdapterError("飞书 webhook token 校验失败")
        challenge = payload.get("challenge")
        if not isinstance(challenge, str) or not challenge:
            raise ChannelAdapterError("飞书 webhook challenge 缺失")
        return {"challenge": challenge}

    @staticmethod
    def _json_content(value: Any) -> tuple[str, tuple[Attachment, ...]]:
        if not isinstance(value, str):
            return str(value or ""), ()
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return value, ()
        if not isinstance(parsed, dict):
            return value, ()
        text = str(parsed.get("text", parsed.get("content", "")))
        attachments: list[Attachment] = []
        for key in ("image_key", "file_key", "file_token", "file_key_token"):
            if parsed.get(key):
                attachments.append(Attachment(
                    kind="image" if key == "image_key" else "file",
                    url=str(parsed[key]),
                    file_name=str(parsed.get("file_name") or "") or None,
                ))
        return text, tuple(attachments)

    @staticmethod
    def _post_content(value: Any) -> tuple[str, tuple[Attachment, ...]]:
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
        except (TypeError, ValueError):
            return "[富文本]", ()
        root = parsed.get("zh_cn", parsed.get("en_us", parsed)) if isinstance(parsed, dict) else {}
        if not isinstance(root, dict):
            return "[富文本]", ()
        lines: list[str] = [str(root.get("title", ""))] if root.get("title") else []
        images: list[Attachment] = []
        for row in root.get("content", []) if isinstance(root.get("content"), list) else []:
            if not isinstance(row, list):
                continue
            parts: list[str] = []
            for element in row:
                if not isinstance(element, dict):
                    continue
                tag = str(element.get("tag", ""))
                if tag == "text":
                    parts.append(str(element.get("text", "")))
                elif tag == "a":
                    parts.append(str(element.get("text", element.get("href", ""))))
                elif tag == "img" and element.get("image_key"):
                    images.append(Attachment(kind="image", url=str(element["image_key"])))
            if parts:
                lines.append("".join(parts))
        return "\n".join(line for line in lines if line) or "[富文本]", tuple(images)

    def parse_event(self, payload: Mapping[str, Any]) -> InboundEvent | None:
        if not self.config.private_enabled:
            return None
        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        if header.get("event_type") not in {"im.message.receive_v1", "message.receive_v1"}:
            return None
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        message = event.get("message") if isinstance(event.get("message"), dict) else event
        if str(message.get("chat_type", "p2p")).lower() not in {"p2p", "private"}:
            return None
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        sender_id_obj = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else sender
        sender_id = str(sender_id_obj.get("open_id", sender_id_obj.get("user_id", "")))
        message_id = str(message.get("message_id", message.get("message_id", header.get("event_id", ""))))
        chat_id = str(message.get("chat_id", sender_id))
        sender_name = str(sender.get("name", sender.get("sender_name", sender.get("nickname", ""))))
        aliases = [sender_name, sender.get("sender_name"), sender.get("nickname"), sender.get("card"), sender_id_obj.get("user_id"), sender_id_obj.get("open_id")]
        if not sender_id or not message_id or not chat_id or is_blocked_identity(self.config.blocked_sender_ids, sender_ids=(sender_id,), aliases=aliases):
            return None
        msg_type = str(message.get("message_type", message.get("msg_type", "text"))).lower()
        content, attachments = self._json_content(message.get("content", ""))
        if msg_type == "post":
            content, attachments = self._post_content(message.get("content", ""))
        # Feishu's post payload stores media in nested ``content`` arrays.  A
        # compact representation is retained in Attachment.metadata by the
        # host; the opaque key is later resolved with ``download_media``.
        if msg_type == "image" and attachments:
            content = content or "[图片]"
        elif msg_type == "file" and attachments:
            file_name = ""
            try:
                raw_content = json.loads(str(message.get("content", "")))
                file_name = str(raw_content.get("file_name", "")) if isinstance(raw_content, dict) else ""
            except (TypeError, ValueError):
                pass
            if file_name and attachments:
                item = attachments[0]
                attachments = (Attachment(item.kind, item.url, file_name, item.mime_type, item.size), *attachments[1:])
            content = f"[文件: {file_name or 'file'}]"
        if msg_type in {"image", "file", "audio", "media", "sticker"} and not attachments:
            media_key = message.get("image_key") or message.get("file_key") or message.get("file_token")
            if media_key:
                attachments = (Attachment(kind="image" if msg_type == "image" else msg_type, url=str(media_key)),)
        parsed_event = InboundEvent(
            channel=self.channel,
            account_id=self.account_id,
            external_event_id=f"{self.account_id}:{header.get('event_id', message_id)}",
            external_message_id=message_id,
            chat_id=chat_id,
            chat_type=ChatType.PRIVATE,
            sender_id=sender_id,
            sender_name=sender_name,
            content=content,
            attachments=attachments,
            reply_to_message_id=str(message.get("parent_id")) if message.get("parent_id") else None,
            metadata={"open_id": sender_id, "event_type": header.get("event_type"), "messageType": msg_type},
        )
        self._targets[chat_id] = target_from_chat(account_id=self.account_id, chat_id=chat_id, kind=TargetKind.PRIVATE, display_name=parsed_event.sender_name)
        return parsed_event

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        self._validate_target(message.target)
        if self._transport is None:
            raise ChannelAdapterError("Feishu transport is not configured")
        try:
            result = await self._send_content(message)
            return result
        except Exception as error:
            status = delivery_status_for_error(error)
            return DeliveryResult(status, message.target, getattr(error, "platform_message_id", None), error=type(error).__name__)

    async def _send_content(self, message: OutboundMessage) -> DeliveryResult:
        """Send text and supported attachments through abstract Feishu APIs."""

        assert self._transport is not None
        params: dict[str, Any] = {
            "receive_id_type": "chat_id",
            "receive_id": message.target.chat_id,
            "msg_type": "interactive",
        }
        if message.reply_to_message_id:
            params["reply_message_id"] = message.reply_to_message_id
        prepared = []
        for attachment in message.attachments:
            if not attachment.url or attachment.url.startswith(("http://", "https://")) or attachment.kind not in {"image", "file"}:
                raise ChannelAdapterError("飞书出站附件必须是受支持的本地图片或文件")
            path = Path(attachment.url)
            data = attachment.frozen_data
            if data is None:
                try:
                    if path.stat().st_size > self.config.max_attachment_bytes:
                        raise ChannelAdapterError("飞书出站附件超过大小限制")
                    data = await asyncio.to_thread(path.read_bytes)
                except OSError as error:
                    raise ChannelAdapterError("飞书出站附件无法读取") from error
            elif not path.is_absolute():
                raise ChannelAdapterError("飞书冻结附件必须保留原本地附件路径")
            if not isinstance(data, bytes) or len(data) > self.config.max_attachment_bytes:
                raise ChannelAdapterError("飞书出站附件超过大小限制")
            prepared.append((attachment, path, data))
        if not message.content.strip() and not prepared:
            raise ChannelAdapterError("飞书出站消息不能为空")
        chunks = markdown_chunks(message.content)
        rendered = markdown_renderings(chunks)
        parts = [{'kind': 'text', 'content': chunk} for chunk in chunks]
        parts += [{'kind': 'media', 'content': '', 'attachmentIndex': index,
                   'fileName': attachment.file_name or path.name}
                  for index, (attachment, path, data) in enumerate(prepared)]
        uploaded = {}
        async def send_part(part):
            body = dict(params)
            if part['withoutReply']:
                body.pop('reply_message_id', None)
            if part['kind'] == 'text':
                body.update(msg_type='text' if part['plainText'] else 'interactive',
                    content=json.dumps({'text': part['content']}, ensure_ascii=False) if part['plainText'] else feishu_card(rendered[part['index']]))
            else:
                index = part['attachmentIndex']
                attachment, path, data = prepared[index]
                kind = 'image' if attachment.kind == 'image' else 'file'
                if index not in uploaded:
                    upload = await self._transport('im.v1.media.create', {
                        'kind': kind, 'data': data, 'file_name': part['fileName'], 'mime_type': attachment.mime_type})
                    key = upload.get('image_key' if kind == 'image' else 'file_key')
                    if not key:
                        raise ChannelAdapterError('feishu_upload_missing_key')
                    uploaded[index] = str(key)
                # Upload is not a visible send. Recheck after it as well.
                attempt.require_current()
                body.update(msg_type=kind, content=json.dumps({kind + '_key': uploaded[index]}))
            return await self._transport('im.v1.message.create', body)
        attempt = DeliveryAttempt(message, scheduler=self.scheduler, max_attempts=4)
        return await attempt.run(parts, send_part, receipt_id=lambda response:
            response.get('message_id') or response.get('data', {}).get('message_id'))

    async def download_media(self, event: InboundEvent, *, max_bytes: int | None = None) -> tuple[FeishuMedia, ...]:
        """Download message resources with a hard size bound.

        ``FeishuWebhookTransport`` supplies the actual authenticated HTTP
        operation.  Returning bytes lets the host apply its own safe filename
        and persistence policy.
        """

        if self._transport is None:
            raise ChannelAdapterError("Feishu transport is not configured")
        limit = self.config.max_attachment_bytes if max_bytes is None else max(1, max_bytes)
        media: list[FeishuMedia] = []
        for attachment in event.attachments[:4]:
            key = str(attachment.url or "")
            if not key:
                continue
            result = await self._transport("im.v1.message.resource.get", {
                "message_id": event.external_message_id,
                "file_key": key,
                "type": "image" if attachment.kind == "image" else "file",
                "max_bytes": limit,
            })
            blob = _bounded_bytes(result, limit)
            if blob is None:
                raise ChannelAdapterError("飞书附件超过大小限制或响应无效")
            mime = attachment.mime_type or mimetypes.guess_type(attachment.file_name or "")[0]
            media.append(FeishuMedia(attachment.kind, blob, attachment.file_name, mime, key))
        return tuple(media)

    async def stream_start(self, target: Target, *, title: str = '', reply_to_message_id: str | None = None,
                           outbound: OutboundMessage | None = None) -> FeishuStreamSession:
        self._validate_target(target)
        if self._transport is None:
            raise ChannelAdapterError('Feishu transport is not configured')
        message = outbound or OutboundMessage(target, '', reply_to_message_id=reply_to_message_id)
        delivery = StreamDelivery(message, self.scheduler, max_attempts=4)
        session = FeishuStreamSession('', target, reply_to_message_id=reply_to_message_id, delivery=delivery)
        card_json = json.dumps({'schema': '2.0', 'config': {'update_multi': True, 'streaming_mode': True,
            'summary': {'content': ''}}, 'body': {'elements': [{'tag': 'markdown', 'content': title or '…',
            'element_id': _LIVE_CARD_ELEMENT_ID}]}}, ensure_ascii=False)
        async def create(part):
            if not session.card_id:
                result = await self._transport('cardkit.create', {'card_json': card_json})
                session.card_id = str(result.get('card_id') or result.get('data', {}).get('card_id') or '')
                if not session.card_id:
                    raise ChannelAdapterError('feishu_cardkit_missing_card_id')
            delivery.require_current()
            params = {'receive_id_type': 'chat_id', 'receive_id': target.chat_id, 'msg_type': 'interactive',
                'content': json.dumps({'type': 'card', 'data': {'card_id': session.card_id}})}
            if reply_to_message_id and not part['withoutReply']:
                params['reply_message_id'] = reply_to_message_id
            return await self._transport('im.v1.message.create', params)
        result = await delivery.update('', create, kind='send', preview=True, receipt_id=_message_id)
        session.message_id = result.platform_message_id
        return session

    async def stream_update(self, session: FeishuStreamSession, content: str) -> None:
        session.delivery.require_current()
        session.pending = content
        if session.delivery.error is None and (session.task is None or session.task.done()):
            session.task = asyncio.create_task(self._flush_stream_updates(session))

    async def _flush_stream_updates(self, session: FeishuStreamSession) -> None:
        while session.pending != session.shown and session.delivery.error is None:
            pending = session.pending
            chunk = next(iter(markdown_chunks(pending)), '')
            if not chunk:
                return
            sequence = session.sequence + 1
            operation_id = uuid.uuid4().hex
            async def update(part):
                await self._transport('cardkit.update', {'card_id': session.card_id,
                    'element_id': _LIVE_CARD_ELEMENT_ID, 'content': chunk, 'sequence': sequence, 'uuid': operation_id})
                return {'message_id': session.message_id}
            result = await session.delivery.update(chunk, update, receipt_id=_message_id)
            if result.status is DeliveryStatus.SENT:
                session.sequence = sequence
                session.shown = pending

    async def stream_finalize(self, session: FeishuStreamSession, content: str) -> DeliveryResult:
        if session.task is not None:
            await asyncio.gather(session.task, return_exceptions=True)
        chunks = markdown_chunks(content or '…')
        rendered = markdown_renderings(chunks)
        parts = [{'kind': 'text', 'content': chunk} for chunk in chunks]
        sequence, operation_id, settings_id = session.sequence + 1, uuid.uuid4().hex, uuid.uuid4().hex
        async def send(part):
            if part['index'] == 0:
                await self._transport('cardkit.finalize', {'card_id': session.card_id, 'element_id': _LIVE_CARD_ELEMENT_ID,
                    'content': rendered[part['index']], 'sequence': sequence, 'settings_sequence': sequence + 1,
                    'settings': json.dumps({'config': {'streaming_mode': False, 'summary': {'content': content[:200]}}}),
                    'uuid': operation_id, 'settings_uuid': settings_id})
                return {'message_id': session.message_id}
            params = {'receive_id_type': 'chat_id', 'receive_id': session.target.chat_id,
                'msg_type': 'text' if part['plainText'] else 'interactive',
                'content': json.dumps({'text': part['content']}, ensure_ascii=False) if part['plainText'] else feishu_card(rendered[part['index']])}
            if session.reply_to_message_id and not part['withoutReply']:
                params['reply_message_id'] = session.reply_to_message_id
            return await self._transport('im.v1.message.create', params)
        result = await session.delivery.finish(parts, send, receipt_id=_message_id)
        if result.status is DeliveryStatus.SENT:
            # Final text is already durable before this optional UI setting.
            async def close(part):
                await self._transport('cardkit.close', {'card_id': session.card_id, 'sequence': sequence + 1,
                    'uuid': settings_id, 'settings': json.dumps({'config': {'streaming_mode': False}})})
                return {'message_id': session.message_id}
            control = DeliveryAttempt(replace(session.delivery.message, record_delivery=None), scheduler=self.scheduler)
            await control.run([{'kind': 'preview', 'content': '', 'operation': 'edit'}], close, receipt_id=_message_id)
        return result

    async def list_targets(self) -> list[Target]:
        return list(self._targets.values())

    def _validate_target(self, target: Target) -> None:
        if target.account_id != self.account_id or target.id not in {item.id for item in self._targets.values()}:
            raise UnsupportedTargetError("目标不在飞书账号可达目录中")
        if target.kind is not TargetKind.PRIVATE:
            raise UnsupportedTargetError("飞书首期仅支持私聊")


def _bounded_bytes(value: Any, limit: int) -> bytes | None:
    """Normalize transport bytes/base64 responses without allocating unbounded data."""

    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        return data if len(data) <= limit else None
    if isinstance(value, Mapping):
        declared = value.get("size")
        if isinstance(declared, int) and (declared < 0 or declared > limit):
            return None
        raw = value.get("content", value.get("data", value.get("bytes")))
        if isinstance(raw, (bytes, bytearray, memoryview)):
            data = bytes(raw)
            return data if len(data) <= limit else None
        if isinstance(raw, str):
            if len(raw) > ((limit + 2) // 3) * 4:
                return None
            try:
                data = base64.b64decode(raw, validate=True)
            except (ValueError, binascii.Error):
                return None
            return data if len(data) <= limit else None
    return None


def _message_id(response):
    return response.get('message_id') or response.get('data', {}).get('message_id')
