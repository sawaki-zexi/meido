"""Platform-neutral channel host and account target operations.

Adapters only know how to parse/send a platform event.  ``ChannelHost`` owns
role/account boundaries, the durable target directory, and delivery status so
models cannot send to an arbitrary platform identifier.
"""
from __future__ import annotations

import asyncio
import contextvars
import uuid
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from .base import ChannelAdapter, ChannelAdapterError, UnsupportedTargetError, is_delivery_uncertain
from .models import Attachment, ChatType, DeliveryResult, DeliveryStatus, InboundEvent, OutboundMessage, Target, TargetKind, target_from_chat
from .intake import ChannelIntake


CHANNEL_INTAKE_RETRY_NOTICE = "渠道配置正在切换，这条消息尚未处理，请稍后重新发送。"

if TYPE_CHECKING:
    from ..multichannel import MultichannelStore


_active_event_accounts: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "channel_active_event_accounts", default=frozenset()
)


def target_payload(target: Target) -> dict[str, object]:
    """Return the public, opaque target representation used by account_targets."""

    return {
        "id": target.id,
        "kind": target.kind.value,
        "displayName": target.display_name,
        "accountId": target.account_id,
        "chatId": target.chat_id,
        "capabilities": sorted(item.value for item in target.capabilities),
        **({"groupId": str(target.metadata["group_id"])} if target.metadata.get("group_id") else {}),
        **({"topicIds": list(target.metadata["topics"])} if isinstance(target.metadata.get("topics"), (list, tuple)) else {}),
        **({"memberIds": list(target.metadata["member_ids"])} if isinstance(target.metadata.get("member_ids"), (list, tuple)) else {}),
    }


class ChannelHost:
    """Coordinate adapters registered for role-owned channel accounts."""

    def __init__(self, store: "MultichannelStore", *, attachment_resolver=None) -> None:
        self.store = store
        self.attachment_resolver = attachment_resolver
        self._adapters: dict[str, ChannelAdapter] = {}
        self._adapter_generations: dict[int, str] = {}
        self._send_state_lock = asyncio.Lock()
        self._paused_accounts: set[str] = set()
        self._inflight_sends: dict[str, int] = {}
        self._inflight_events: dict[str, int] = {}
        self._account_idle: dict[str, asyncio.Event] = {}
        self.intake = ChannelIntake()

    def register(self, adapter: ChannelAdapter) -> None:
        account = self.store.get_account(adapter.account_id)
        if account is None:
            raise KeyError(adapter.account_id)
        if account.channel != adapter.channel:
            raise ValueError("适配器平台与账号配置不匹配")
        self._adapters[adapter.account_id] = adapter
        self._adapter_generations.setdefault(id(adapter), uuid.uuid4().hex)

    def unregister(self, account_id: str) -> None:
        adapter = self._adapters.pop(account_id, None)
        if adapter is not None:
            self._adapter_generations.pop(id(adapter), None)
        if not self._inflight_sends.get(account_id) and not self._inflight_events.get(account_id):
            self._paused_accounts.discard(account_id)
            self._account_idle.pop(account_id, None)

    async def pause_and_drain(self, account_id: str) -> None:
        """Reject new events and sends, then wait for admitted work to finish."""

        async with self._send_state_lock:
            self._paused_accounts.add(account_id)
            self.intake.pause(account_id)
            idle = self._account_idle.setdefault(account_id, asyncio.Event())
            if self._activity_count(account_id) == 0:
                idle.set()
        await idle.wait()

    async def resume(self, account_id: str) -> None:
        async with self._send_state_lock:
            self._paused_accounts.discard(account_id)
        self.intake.resume(account_id)

    @asynccontextmanager
    async def event_scope(self, account_id: str) -> AsyncIterator[bool]:
        """Admit one inbound event and keep its whole turn alive through drain.

        Sends made by this event remain authorized after admission is paused,
        allowing an already-running model turn to deliver its final response.
        """

        active = _active_event_accounts.get()
        if account_id in active:
            yield True
            return
        async with self._send_state_lock:
            account = self.store.get_account(account_id)
            admitted = (
                account is not None
                and self.store.account_effectively_enabled(account)
                and account_id not in self._paused_accounts
                and account_id in self._adapters
            )
            if admitted:
                self._inflight_events[account_id] = self._inflight_events.get(account_id, 0) + 1
                self._account_idle.setdefault(account_id, asyncio.Event()).clear()
        if not admitted:
            yield False
            return
        token = _active_event_accounts.set(active | {account_id})
        try:
            yield True
        finally:
            _active_event_accounts.reset(token)
            await self._end_activity(account_id, event=True)

    def adapter(self, account_id: str) -> ChannelAdapter:
        adapter = self._adapters.get(account_id)
        if adapter is None:
            raise KeyError(account_id)
        return adapter

    def accepts_events(self, account_id: str) -> bool:
        account = self.store.get_account(account_id)
        return bool(account and self.store.account_effectively_enabled(account) and account_id in self._adapters and account_id not in self._paused_accounts)

    async def send_retry_notice(self, adapter: ChannelAdapter, event: InboundEvent) -> DeliveryResult:
        """Answer rejected intake through the original connection, without history."""
        if adapter.account_id != event.account_id or adapter.channel != event.channel:
            raise ValueError("拒收提示只能回复原始来源账号")
        target = target_from_chat(
            account_id=event.account_id, chat_id=event.chat_id,
            kind=TargetKind.GROUP if event.chat_type is ChatType.GROUP else TargetKind.PRIVATE,
            display_name=event.group_name or event.sender_name,
        )
        async with self._send_state_lock:
            if id(adapter) not in self._adapter_generations:
                raise ChannelAdapterError("拒收提示的来源连接尚未注册")
            self._inflight_sends[event.account_id] = self._inflight_sends.get(event.account_id, 0) + 1
            self._account_idle.setdefault(event.account_id, asyncio.Event()).clear()
        try:
            adapter.remember_target(target)
            return await adapter.send(OutboundMessage(
                target, CHANNEL_INTAKE_RETRY_NOTICE, reply_to_message_id=event.external_message_id,
            ))
        finally:
            await self._end_activity(event.account_id, event=False)

    def account_list(self, role_id: str) -> list[dict[str, object]]:
        accounts: list[dict[str, object]] = []
        for account in self.store.list_role_accounts(role_id):
            adapter = self._adapters.get(account.account_id)
            capabilities = adapter.capabilities if adapter is not None else None
            accounts.append({
                "accountId": account.account_id,
                "channel": account.channel,
                "displayName": account.display_name,
                "enabled": account.enabled,
                "platformEnabled": self.store.channel_enabled(account.channel),
                "effectiveEnabled": self.store.account_effectively_enabled(account),
                "capabilities": ({
                    "private": capabilities.private,
                    "group": capabilities.group,
                    "groupListening": capabilities.group_listening,
                    "media": capabilities.media,
                    "replies": capabilities.replies,
                    "streaming": capabilities.streaming,
                    "targets": sorted(item.value for item in capabilities.targets),
                } if capabilities is not None else {}),
            })
        return accounts

    async def start(self, account_id: str) -> None:
        account = self.store.get_account(account_id)
        if account is None or not self.store.account_effectively_enabled(account):
            return
        await self.adapter(account_id).start()

    async def stop(self, account_id: str) -> None:
        adapter = self._adapters.get(account_id)
        if adapter is not None:
            await adapter.stop()

    async def accept_event(self, account_id: str, payload: Mapping[str, Any]):
        """Normalize, deduplicate, and directory-index one platform event."""

        account = self.store.get_account(account_id)
        if account is None:
            raise KeyError(account_id)
        if (not self.store.account_effectively_enabled(account) and not self._has_active_event(account_id)) or (
            account_id in self._paused_accounts and not self._has_active_event(account_id)
        ):
            return None
        adapter = self.adapter(account_id)
        event = await adapter.accept_event(payload)
        if event is not None:
            current = self.store.get_account(account_id)
            if current is None or (
                (not self.store.account_effectively_enabled(current) or account_id in self._paused_accounts)
                and not self._has_active_event(account_id)
            ) or self._adapters.get(account_id) is not adapter:
                return None
            if event.account_id != account_id or event.channel != account.channel:
                raise ChannelAdapterError("适配器返回了其他账号或平台的事件")
            await self.refresh_targets(account_id)
        return event

    async def refresh_targets(self, account_id: str) -> list[Target]:
        adapter = self.adapter(account_id)
        account = self.store.get_account(account_id)
        if account is None:
            raise KeyError(account_id)
        if not self.store.account_effectively_enabled(account) and not self._has_active_event(account_id):
            raise ChannelAdapterError("渠道账号或平台未启用")
        targets = await adapter.list_targets()
        if self._adapters.get(account_id) is not adapter or (
            account_id in self._paused_accounts and not self._has_active_event(account_id)
        ):
            raise ChannelAdapterError("渠道账号连接正在切换")
        if any(target.account_id != account_id for target in targets):
            raise ChannelAdapterError("适配器返回了其他账号的可达目标")
        self.store.remember_targets(account.role_id, targets)
        return targets

    async def account_targets(self, role_id: str, account_id: str) -> list[dict[str, object]]:
        account = self.store.get_account(account_id)
        if account is None or account.role_id != role_id:
            raise KeyError(account_id)
        if account_id in self._adapters and self.store.account_effectively_enabled(account):
            await self.refresh_targets(account_id)
        targets = self.store.list_targets(role_id, account_id)
        adapter = self._adapters.get(account_id)
        if adapter is not None:
            for target in targets:
                adapter.remember_target(target)
        return [target_payload(target) for target in targets]

    async def account_send(
        self,
        role_id: str,
        account_id: str,
        target_id: str,
        content: str,
        *,
        reply_to_message_id: str | None = None,
        attachments: tuple[Attachment, ...] = (),
        attachment_ids: tuple[str, ...] = (),
        stream_updates: AsyncIterator[str] | None = None,
        mention_ids: tuple[str, ...] = (),
        message_thread_id: int | None = None,
        actor: str,
        source_thread_id: str | None = None,
        source_session_key: str | None = None,
        source_run_id: str | None = None,
        source_tool_call_id: str | None = None,
        source_kind: str | None = None,
        record_message: bool = True,
    ) -> DeliveryResult:
        """Send to an observed target and persist the resulting delivery state.

        ``actor='external'`` can only contact the paired owner through the
        originating account. Guest turns cannot choose third-party targets.
        """

        account = self.store.get_account(account_id)
        if account is None or account.role_id != role_id:
            raise KeyError(account_id)
        if not self.store.account_effectively_enabled(account) and not self._has_active_event(account_id):
            raise ChannelAdapterError("渠道账号未启用")
        if actor == "owner":
            target = self.store.get_target(role_id, account_id, target_id)
            if target is None:
                # A caller may have parsed an event immediately before this call;
                # refresh once before rejecting an unknown opaque ID.
                await self.refresh_targets(account_id)
                target = self.store.get_target(role_id, account_id, target_id)
        elif actor == "push":
            target = next((item for item in self.store.owner_targets(role_id, account_id) if item.id == target_id), None)
        elif actor == "external" and target_id == "owner":
            source = self.store.get_thread(role_id, source_thread_id or "")
            if source is None or source.account_id != account_id or source.channel != account.channel:
                raise UnsupportedTargetError("外部会话只能使用当前账号联系已配对主人")
            target = self.store.owner_target(role_id, account_id)
            if target is None:
                raise UnsupportedTargetError("当前渠道账号没有已配对的主人私聊")
        elif actor == "reply":
            source = self.store.get_thread(role_id, source_thread_id or "")
            if source is None or source.account_id != account_id or source.channel != account.channel:
                raise UnsupportedTargetError("回复只能发送到当前渠道会话")
            expected_kind = "group" if source.chat_type == "group" else "private"
            target = self.store.get_target(role_id, account_id, target_id)
            if target is None or target.chat_id != source.chat_id or target.kind.value != expected_kind:
                raise UnsupportedTargetError("回复目标必须是当前渠道会话")
        else:
            raise UnsupportedTargetError("外部会话只能向已配对主人投递")
        if target is None:
            raise UnsupportedTargetError("目标不在渠道账号可达目录中")

        if mention_ids:
            if target.kind is not TargetKind.GROUP or account.channel not in {"qq", "telegram"}:
                raise UnsupportedTargetError("该平台或目标不支持群成员提及")
            if len(mention_ids) > 20 or any(not isinstance(item, str) or not item for item in mention_ids):
                raise UnsupportedTargetError("群成员提及参数无效")
            members = {str(item) for item in target.metadata.get("member_ids", [])}
            members.update(
                item.chat_id for item in self.store.list_targets(role_id, account_id)
                if item.kind is TargetKind.GROUP_MEMBER and str(item.metadata.get("group_id") or "") == target.chat_id
            )
            if any(item not in members for item in mention_ids):
                raise UnsupportedTargetError("只能提及该群目录已观察或发现的成员")
            mention_ids = tuple(dict.fromkeys(mention_ids))
        if message_thread_id is not None:
            if isinstance(message_thread_id, bool) or not isinstance(message_thread_id, int) or message_thread_id <= 0:
                raise UnsupportedTargetError("话题 ID 必须是正整数")
            if account.channel != "telegram" or target.kind is not TargetKind.GROUP:
                raise UnsupportedTargetError("该平台或目标不支持 Telegram 群话题")
            if message_thread_id not in target.metadata.get("topics", []):
                raise UnsupportedTargetError("只能发送到该群已观察到的话题")
        if account.channel == "telegram":
            # The last inbound topic is not an implicit destination. A caller
            # must select a known topic, and replies carry their own snapshot.
            target = replace(target, metadata={
                **{key: value for key, value in target.metadata.items() if key != "message_thread_id"},
                **({"message_thread_id": message_thread_id} if message_thread_id is not None else {}),
            })

        attachment_metadata: list[dict[str, object]] = []
        if attachment_ids:
            if len(attachment_ids) > 4 or self.attachment_resolver is None:
                raise UnsupportedTargetError("附件数量超出限制或当前渠道没有附件存储")
            if actor == "external":
                source = self.store.get_thread(role_id, source_thread_id or "")
                if source is None:
                    raise UnsupportedTargetError("无法验证外部会话附件来源")
                visible_ids = {
                    str(item.get("id"))
                    for message in self.store.sessions.list_messages(source.session_key)
                    for item in (message.metadata.get("attachments", []) if isinstance(message.metadata, dict) else [])
                    if isinstance(item, dict) and item.get("id")
                }
                if any(item not in visible_ids for item in attachment_ids):
                    raise UnsupportedTargetError("外部会话只能发送当前会话收到的附件")
            resolved: list[Attachment] = []
            for attachment_id in attachment_ids:
                item = self.attachment_resolver(role_id, attachment_id)
                if item is None or not isinstance(item.get("path"), str):
                    raise UnsupportedTargetError("附件不存在或不属于当前角色")
                attachment_metadata.append(item)
                resolved.append(Attachment(
                    kind=str(item.get("kind") or "file"),
                    url=str(item["path"]),
                    file_name=str(item.get("name") or "attachment"),
                    mime_type=str(item.get("mediaType") or "application/octet-stream"),
                    size=int(item["size"]) if isinstance(item.get("size"), int) else None,
                ))
            attachments = (*attachments, *resolved)

        adapter = await self._begin_send(account_id)
        try:
            if actor in {"external", "push"} and not any(
                item.id == target.id for item in self.store.owner_targets(role_id, account_id)
            ):
                raise UnsupportedTargetError("主人投递目标已解绑")
            adapter.remember_target(target)
            group_id = str(target.metadata.get("group_id") or "") if target.kind.value == "group_member" else ""
            thread_chat_id = target.chat_id
            thread_chat_type = "group" if target.kind.value == "group" else "private"
            thread = self.store.ensure_channel_thread(
                role_id,
                account.channel,
                account_id,
                thread_chat_id,
                thread_chat_type,
                title=target.display_name,
                metadata={"deliveryTargetId": target.id, "deliveryTargetKind": target.kind.value, **({"originGroupId": group_id} if group_id else {})},
            )
            attempt_id = self.store.begin_delivery(
                thread, content,
                source_session_key=source_session_key,
                source_run_id=source_run_id,
                source_tool_call_id=source_tool_call_id,
                source_kind=source_kind or actor,
                account_generation=self._adapter_generations.get(id(adapter)),
            )
            try:
                outbound = OutboundMessage(
                    target=target,
                    content=content,
                    reply_to_message_id=reply_to_message_id,
                    attachments=attachments,
                    metadata={"deliveryAttemptId": attempt_id, "mentionIds": list(mention_ids)},
                )
                stream_start = getattr(adapter, "stream_start", None)
                stream_update = getattr(adapter, "stream_update", None)
                stream_finalize = getattr(adapter, "stream_finalize", None)
                if stream_updates is not None and callable(stream_start) and callable(stream_update) and callable(stream_finalize) and not attachments:
                    try:
                        session = await stream_start(target, reply_to_message_id=reply_to_message_id)
                        stream_content = content
                        async for update in stream_updates:
                            stream_content = update
                            await stream_update(session, update)
                        content = stream_content
                        result = await stream_finalize(session, stream_content)
                    except Exception:
                        # A stream adapter must report uncertain outcomes itself;
                        # do not issue a duplicate plain message after a partial send.
                        raise
                else:
                    if stream_updates is not None:
                        async for _ in stream_updates:
                            pass
                    result = await adapter.send(outbound)
            except Exception as error:
                status = DeliveryStatus.UNCERTAIN if is_delivery_uncertain(error) else DeliveryStatus.FAILED
                result = DeliveryResult(status, target, error=type(error).__name__)
            self.store.finish_delivery(
                attempt_id,
                status=result.status.value,
                platform_message_id=result.platform_message_id,
                error=result.error,
                content=content,
            )
            result = replace(result, attempt_id=attempt_id)
            metadata = {
                "targetId": target.id,
                "targetKind": target.kind.value,
                "deliveryAttemptId": attempt_id,
                "deliveryStatus": result.status.value,
                "platformMessageId": result.platform_message_id,
                "mentionIds": list(mention_ids),
                "messageThreadId": message_thread_id,
                "error": result.error,
                "attachments": attachment_metadata,
                "platformMessageIds": result.raw.get("platform_message_ids", []),
            }
            if record_message:
                self.store.append_event(
                    thread,
                    direction="outbound",
                    content=content,
                    external_message_id=result.platform_message_id,
                    role="assistant",
                    metadata=metadata,
                    status="completed" if result.status is DeliveryStatus.SENT else "failed",
                )
            self.store.publish(thread.role_id, {"type": "delivery", "roleId": thread.role_id, "attemptId": attempt_id})
            return result
        finally:
            await self._end_send(account_id)

    async def _begin_send(self, account_id: str) -> ChannelAdapter:
        async with self._send_state_lock:
            account = self.store.get_account(account_id)
            has_event_lease = self._has_active_event(account_id)
            if account is None or (not self.store.account_effectively_enabled(account) and not has_event_lease):
                raise ChannelAdapterError("渠道账号未启用")
            if account_id in self._paused_accounts and not has_event_lease:
                raise ChannelAdapterError("渠道账号连接正在切换")
            adapter = self._adapters.get(account_id)
            if adapter is None:
                raise KeyError(account_id)
            self._account_idle.setdefault(account_id, asyncio.Event()).clear()
            self._inflight_sends[account_id] = self._inflight_sends.get(account_id, 0) + 1
            return adapter

    async def _end_send(self, account_id: str) -> None:
        await self._end_activity(account_id, event=False)

    def _has_active_event(self, account_id: str) -> bool:
        return account_id in _active_event_accounts.get()

    def _activity_count(self, account_id: str) -> int:
        return self._inflight_sends.get(account_id, 0) + self._inflight_events.get(account_id, 0)

    async def _end_activity(self, account_id: str, *, event: bool) -> None:
        async with self._send_state_lock:
            counts = self._inflight_events if event else self._inflight_sends
            remaining = counts.get(account_id, 1) - 1
            if remaining <= 0:
                counts.pop(account_id, None)
            else:
                counts[account_id] = remaining
            if self._activity_count(account_id) == 0:
                self._account_idle.setdefault(account_id, asyncio.Event()).set()
