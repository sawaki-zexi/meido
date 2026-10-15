from __future__ import annotations

import logging
import re
import asyncio
import base64
from dataclasses import replace
from urllib.parse import unquote
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .base import ChannelAdapter, ChannelAdapterError, SendTransport, UnsupportedTargetError, delivery_status_for_error, is_blocked_identity
from .models import (
    Attachment,
    ChannelCapabilities,
    ChatType,
    DeliveryResult,
    DeliveryStatus,
    DownloadedMedia,
    InboundEvent,
    OutboundMessage,
    Target,
    TargetKind,
    target_from_chat,
)

logger = logging.getLogger(__name__)
FORWARD_MAX_CHARS = 300
FORWARD_MAX_LINES = 10
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


@dataclass(slots=True)
class NapCatConfig:
    account_id: str
    endpoint: str = "ws://127.0.0.1:6700"
    access_token_key: str | None = None
    private_enabled: bool = True
    group_enabled: bool = True
    group_listening: bool = False
    blocked_sender_ids: set[str] = field(default_factory=set)


class NapCatAdapter(ChannelAdapter):
    channel = "qq"
    capabilities = ChannelCapabilities(
        private=True,
        group=True,
        group_listening=True,
        media=True,
        replies=True,
        streaming=False,
        targets=frozenset({TargetKind.PRIVATE, TargetKind.GROUP, TargetKind.GROUP_MEMBER}),
    )

    def __init__(self, config: NapCatConfig, *, transport: SendTransport | None = None) -> None:
        super().__init__()
        self.config = config
        self.account_id = config.account_id
        self._transport = transport
        self._targets: dict[str, Target] = {}
        self._group_ids: set[str] = set()
        self._bot_message_ids: set[str] = set()
        self._self_id = ""
        self._self_name = ""

    @staticmethod
    def _segments(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, str):
            # OneBot may deliver a CQ-code string instead of an array of
            # message segments.  Parse the structural @/reply markers so
            # trigger detection does not depend on display text.
            segments: list[dict[str, Any]] = []
            cursor = 0
            for match in re.finditer(r"\[CQ:([a-z_]+)((?:,[^\]]*)?)\]", payload, re.IGNORECASE):
                if match.start() > cursor:
                    segments.append({"type": "text", "data": {"text": payload[cursor:match.start()]}})
                values: dict[str, str] = {}
                for item in match.group(2).lstrip(",").split(","):
                    if "=" in item:
                        key, value = item.split("=", 1)
                        values[key.strip()] = unquote(value.strip())
                kind = match.group(1).lower()
                segments.append({"type": kind, "data": values})
                cursor = match.end()
            if cursor < len(payload):
                segments.append({"type": "text", "data": {"text": payload[cursor:]}})
            return segments or [{"type": "text", "data": {"text": payload}}]
        return []

    def parse_event(self, payload: Mapping[str, Any]) -> InboundEvent | None:
        if payload.get("post_type") != "message":
            return None
        message_type = payload.get("message_type")
        if message_type not in {"private", "group"}:
            return None
        chat_type = ChatType.PRIVATE if message_type == "private" else ChatType.GROUP
        if chat_type is ChatType.PRIVATE and not self.config.private_enabled:
            return None
        if chat_type is ChatType.GROUP and not self.config.group_enabled:
            return None
        sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
        sender_id = str(sender.get("user_id", payload.get("user_id", "")))
        sender_name = str(sender.get("nickname", sender.get("card", sender.get("name", ""))))
        aliases = (sender_name, sender.get("nickname"), sender.get("card"), sender.get("name"))
        if not sender_id or is_blocked_identity(self.config.blocked_sender_ids, sender_ids=(sender_id,), aliases=aliases):
            return None
        chat_id = str(payload.get("group_id" if chat_type is ChatType.GROUP else "user_id", ""))
        message_id = str(payload.get("message_id", ""))
        if not chat_id or not message_id:
            return None
        segments = self._segments(payload.get("message"))
        text_parts: list[str] = []
        mentions: list[str] = []
        attachments: list[Attachment] = []
        for segment in segments:
            kind = str(segment.get("type", ""))
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            if kind == "text":
                text_parts.append(str(data.get("text", "")))
            elif kind == "at":
                qq = str(data.get("qq", ""))
                if qq:
                    mentions.append(qq)
            elif kind in {"image", "file", "video", "record"}:
                attachments.append(Attachment(kind=kind, url=str(data.get("url")) if data.get("url") else None, file_name=str(data.get("file")) if data.get("file") else None))
        reply = next((segment for segment in segments if segment.get("type") == "reply"), None)
        reply_data = reply.get("data") if isinstance(reply, dict) and isinstance(reply.get("data"), dict) else {}
        # NapCat emits a reply segment and @ segment.  A group message is
        # eligible for a model turn only when one of those addresses the bot.
        # ``account_id`` is an internal opaque id in Meido; only the platform
        # self_id is authoritative for @bot matching.  Keep account_id as a
        # compatibility fallback for fixtures that do not include self_id.
        self_id = str(payload.get("self_id", ""))
        mentioned = bool((self_id and self_id in mentions) or (not self_id and str(self.account_id) in mentions))
        reply_to_sender_id = str(reply_data.get("sender_id", reply_data.get("user_id", "")))
        # Some OneBot implementations omit sender_id on a reply segment.  A
        # reply id still addresses the bot when the platform provides the
        # explicit ``reply_to`` marker or a bot self_id in the segment.
        reply_to_self = reply_data.get("is_bot") is True or reply_data.get("sender_is_self") is True
        replied = bool(reply_data.get("id")) and (
            reply_to_sender_id in {value for value in (self_id, str(self.account_id)) if value}
            or bool(reply_to_self)
            or str(reply_data.get("user_id", "")) == self_id
            or str(reply_data.get("id", "")) in self._bot_message_ids
        )
        triggered = chat_type is ChatType.PRIVATE or mentioned or replied
        event = InboundEvent(
            channel=self.channel,
            account_id=self.account_id,
            external_event_id=f"{self.account_id}:{message_id}",
            external_message_id=message_id,
            chat_id=chat_id,
            chat_type=chat_type,
            sender_id=sender_id,
            sender_name=sender_name,
            content="".join(text_parts).strip(),
            group_name=str(payload.get("group_name", "")) or None,
            mentioned=mentioned,
            mentioned_ids=tuple(mentions),
            reply_to_message_id=str(reply_data.get("id")) if reply_data.get("id") else None,
            reply_to_sender_id=reply_to_sender_id or None,
            attachments=tuple(attachments),
            triggered=triggered,
            metadata={"self_id": str(payload.get("self_id", "")), "raw_message": payload.get("raw_message", "")},
        )
        kind = TargetKind.GROUP if chat_type is ChatType.GROUP else TargetKind.PRIVATE
        target = target_from_chat(account_id=self.account_id, chat_id=event.chat_id, kind=kind, display_name=event.group_name or event.sender_name)
        self._targets[target.id] = target
        if chat_type is ChatType.GROUP:
            self._group_ids.add(event.chat_id)
            member = target_from_chat(
                account_id=self.account_id,
                chat_id=sender_id,
                kind=TargetKind.GROUP_MEMBER,
                display_name=event.sender_name,
                group_id=event.chat_id,
                name_observed_at=datetime.now(timezone.utc).isoformat(),
            )
            self._targets[member.id] = member
        return event

    async def accept_event(self, payload: Mapping[str, Any]) -> InboundEvent | None:
        """Resolve reply targets whose sender is omitted from the CQ segment.

        Message IDs in ``_bot_message_ids`` cover the common same-process
        case.  Looking up an unknown group reply through OneBot keeps trigger
        detection correct after a restart and avoids treating replies to
        ordinary members as a role invocation.
        """

        event = await super().accept_event(payload)
        if (
            event is None
            or event.chat_type is not ChatType.GROUP
            or event.triggered
            or not event.reply_to_message_id
            or self._transport is None
        ):
            return event
        try:
            response = await self._transport("get_msg", {"message_id": event.reply_to_message_id})
        except Exception:
            return event
        quoted = response.get("data") if isinstance(response.get("data"), Mapping) else response
        if isinstance(response.get("message"), Mapping):
            quoted = response["message"]
        if isinstance(quoted, Mapping):
            if quoted.get("group_id") is not None and str(quoted["group_id"]) != event.chat_id:
                return event
            sender = quoted.get("sender") if isinstance(quoted.get("sender"), Mapping) else {}
            sender_id = str(sender.get("user_id") or quoted.get("user_id") or "")
            self_id = str(event.metadata.get("self_id", ""))
            if sender_id and self_id and sender_id == self_id:
                return replace(event, triggered=True, reply_to_sender_id=sender_id)
        return event

    async def enrich_reply(self, event: InboundEvent) -> InboundEvent:
        """Resolve a quote only after the host admitted a triggering message."""
        if not event.reply_to_message_id or not event.triggered or self._transport is None:
            return event
        try:
            response = await self._transport("get_msg", {"message_id": event.reply_to_message_id})
        except Exception:
            return event
        quoted = response.get("data") if isinstance(response.get("data"), Mapping) else response
        if not isinstance(quoted, Mapping):
            return event
        quoted_group = str(quoted.get("group_id") or "")
        if quoted_group and (event.chat_type is not ChatType.GROUP or quoted_group != event.chat_id):
            return event
        sender = quoted.get("sender") if isinstance(quoted.get("sender"), Mapping) else {}
        attachments, text = [], []
        for segment in self._segments(quoted.get("message")):
            data = segment.get("data") if isinstance(segment.get("data"), Mapping) else {}
            if segment.get("type") == "text":
                text.append(str(data.get("text") or ""))
            elif segment.get("type") in {"image", "file"} and data.get("url"):
                attachments.append(Attachment(str(segment["type"]), str(data["url"]), str(data.get("file") or "") or None))
        return replace(event, metadata={
            **event.metadata,
            "replyToContent": "".join(text),
            "replyToSenderName": str(sender.get("card") or sender.get("nickname") or ""),
            "quotedAttachments": tuple(attachments),
        }, reply_to_sender_id=str(sender.get("user_id") or quoted.get("user_id") or event.reply_to_sender_id or "") or None)

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        self._validate_target(message.target)
        if self._transport is None:
            raise ChannelAdapterError("NapCat transport is not configured")
        visible_sends = 0
        try:
            # Resolve every image before the first request. NapCat may run in
            # another host/container and cannot read Meido's local paths.
            images = [await self._image_segment(item) for item in message.attachments]
            operation = "send_group_msg" if message.target.kind is TargetKind.GROUP else "send_private_msg"
            params: dict[str, Any] = {}
            if not message.target.chat_id.isdigit() or int(message.target.chat_id) < 1:
                raise ChannelAdapterError("NapCat 目标必须使用有效 QQ 数字标识")
            if message.target.kind is TargetKind.GROUP:
                params["group_id"] = int(message.target.chat_id)
            else:
                params["user_id"] = int(message.target.chat_id)
                if message.target.kind is TargetKind.GROUP_MEMBER:
                    group_id = str(message.target.metadata.get("group_id") or "")
                    if not group_id.isdigit():
                        raise ChannelAdapterError("NapCat 群临时私聊缺少有效群号")
                    params["group_id"] = int(group_id)
            mentions = []
            if message.target.kind is TargetKind.GROUP:
                if message.reply_to_message_id:
                    mentions.append({"type": "reply", "data": {"id": message.reply_to_message_id}})
                for mention_id in message.metadata.get("mentionIds", ()) if isinstance(message.metadata.get("mentionIds"), (list, tuple)) else ():
                    if str(mention_id):
                        mentions.append({"type": "at", "data": {"qq": str(mention_id)}})
            if len(message.content) > FORWARD_MAX_CHARS or len(message.content.splitlines()) > FORWARD_MAX_LINES:
                identity = await self._transport("get_login_info", {})
                self._self_id = str(identity.get("user_id") or "")
                self._self_name = str(identity.get("nickname") or self._self_id)
                if not self._self_id.isdigit():
                    raise ChannelAdapterError("NapCat 合并转发缺少登录账号身份")
                paragraphs = _PARAGRAPH_BREAK.split(message.content.strip())
                nodes = [{"type": "node", "data": {
                    "user_id": int(self._self_id), "nickname": self._self_name,
                    "content": [{"type": "text", "data": {"text": text}}] + (images if index == len(paragraphs) - 1 else []),
                }} for index, text in enumerate(paragraphs)]
                if mentions:
                    await self._send_receipt(operation, {**params, "message": mentions})
                    visible_sends += 1
                forward_operation = "send_group_forward_msg" if message.target.kind is TargetKind.GROUP else "send_private_forward_msg"
                result, message_id = await self._send_receipt(forward_operation, {**params, "messages": nodes})
            else:
                segments = [*mentions, *([{"type": "text", "data": {"text": message.content}}] if message.content else []), *images]
                body = segments if message.target.kind is TargetKind.GROUP or images else message.content
                result, message_id = await self._send_receipt(operation, {**params, "message": body})
            return DeliveryResult(DeliveryStatus.SENT, message.target, message_id, raw=dict(result))
        except Exception as error:
            status = DeliveryStatus.UNCERTAIN if visible_sends else delivery_status_for_error(error)
            return DeliveryResult(status, message.target, error=type(error).__name__)

    async def _send_receipt(self, operation: str, params: Mapping[str, Any]) -> tuple[Mapping[str, Any], str]:
        from .base import PartialDeliveryError

        assert self._transport is not None
        result = await self._transport(operation, params)
        message_id = str(result.get("message_id") or "").strip() if isinstance(result, Mapping) else ""
        if not re.fullmatch(r"-?[1-9][0-9]*", message_id):
            raise PartialDeliveryError("NapCat 平台已接受请求但未返回有效消息回执")
        self._bot_message_ids.add(message_id)
        return result, message_id

    async def _image_segment(self, attachment: Attachment) -> dict[str, Any]:
        if attachment.kind != "image" or not attachment.url:
            raise ChannelAdapterError("NapCat 主动附件仅支持图片")
        path = Path(attachment.url)
        if not path.is_absolute() or not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
            raise ChannelAdapterError("NapCat 图片附件必须是已授权的本地图片，且不超过 10 MB")
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError as error:
            raise ChannelAdapterError("NapCat 图片附件无法读取") from error
        if not data or len(data) > 10 * 1024 * 1024:
            raise ChannelAdapterError("NapCat 图片附件为空或超过大小限制")
        if not (data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM")) or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")):
            raise ChannelAdapterError("NapCat 图片只支持 PNG、JPEG、GIF、WebP 和 BMP")
        return {"type": "image", "data": {"file": "base64://" + base64.b64encode(data).decode("ascii")}}

    async def list_targets(self) -> list[Target]:
        if self._transport is not None:
            try:
                friends_payload = await self._transport("get_friend_list", {})
                friends = friends_payload.get("items", friends_payload.get("data", []))
                if isinstance(friends, list):
                    for item in friends:
                        if isinstance(item, dict) and item.get("user_id"):
                            friend = target_from_chat(account_id=self.account_id, chat_id=str(item["user_id"]), kind=TargetKind.PRIVATE, display_name=str(item.get("remark") or item.get("nickname") or item["user_id"]))
                            self._targets[friend.id] = friend
            except Exception:
                pass
            try:
                groups_payload = await self._transport("get_group_list", {})
                groups = groups_payload.get("items", groups_payload.get("data", []))
                if isinstance(groups, list):
                    self._group_ids.update(str(item.get("group_id")) for item in groups if isinstance(item, dict) and item.get("group_id"))
                    for item in groups:
                        if not isinstance(item, dict) or not item.get("group_id"):
                            continue
                        target = target_from_chat(account_id=self.account_id, chat_id=str(item["group_id"]), kind=TargetKind.GROUP, display_name=str(item.get("group_name", "")))
                        self._targets[target.id] = target
            except Exception:
                pass
            for group_id in sorted(self._group_ids)[:100]:
                try:
                    members_payload = await self._transport("get_group_member_list", {"group_id": group_id, "no_cache": True})
                except Exception:
                    continue
                members = members_payload.get("items", members_payload.get("data", []))
                if not isinstance(members, list):
                    continue
                for item in members:
                    if not isinstance(item, dict) or not item.get("user_id"):
                        continue
                    member = target_from_chat(
                        account_id=self.account_id,
                        chat_id=str(item["user_id"]),
                        kind=TargetKind.GROUP_MEMBER,
                        display_name=str(item.get("card") or item.get("nickname", "")),
                        group_id=group_id,
                        name_observed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    self._targets[member.id] = member
        return list(self._targets.values())

    def _validate_target(self, target: Target) -> None:
        if target.account_id != self.account_id or target.id not in {item.id for item in self._targets.values()}:
            raise UnsupportedTargetError("目标不在 NapCat 账号可达目录中")
        if target.kind not in self.capabilities.targets:
            raise UnsupportedTargetError("NapCat 不支持该目标类型")

    async def download_media(self, event: InboundEvent, *, max_bytes: int = 10 * 1024 * 1024) -> tuple[DownloadedMedia, ...]:
        from .media import download_qq_media

        downloaded: list[DownloadedMedia] = []
        for attachment in event.attachments[:4]:
            if attachment.kind not in {"image", "file"} or not attachment.url:
                continue
            try:
                data, mime_type = await download_qq_media(attachment.url, max_bytes=max_bytes)
            except Exception as error:
                logger.warning("NapCat 附件下载失败 account=%s kind=%s error=%s", self.account_id, attachment.kind, type(error).__name__)
                continue
            downloaded.append(DownloadedMedia(attachment.kind, data, attachment.file_name, mime_type))
        return tuple(downloaded)
