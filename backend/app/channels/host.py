"""Platform-neutral channel host and account target operations.

Adapters only know how to parse/send a platform event.  ``ChannelHost`` owns
role/account boundaries, the durable target directory, and delivery status so
models cannot send to an arbitrary platform identifier.
"""
from __future__ import annotations

import asyncio
import contextvars
import uuid
import hashlib
import time
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import replace
from functools import wraps
from typing import TYPE_CHECKING, Any, Awaitable, Concatenate, Coroutine, ParamSpec, TypeVar

from .base import ChannelAdapter, ChannelAdapterError, UnsupportedTargetError, is_delivery_uncertain, is_blocked_identity
from .models import Attachment, ChatType, DeliveryResult, DeliveryStatus, InboundEvent, OutboundMessage, Target, TargetKind, target_from_chat
from .intake import ChannelIntake
from . import directory
from .directory import DirectoryError, DirectoryPages, DirectoryQuery, MAX_RECORDS, bound_records, observed_now, supported_kinds
from .runtime_owner import ChannelRuntimeOwner


CHANNEL_INTAKE_RETRY_NOTICE = "渠道配置正在切换，这条消息尚未处理，请稍后重新发送。"

if TYPE_CHECKING:
    from ..multichannel import MultichannelStore


_active_event_accounts: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "channel_active_event_accounts", default=frozenset()
)

_SendArgs = ParamSpec('_SendArgs')
_SendResult = TypeVar('_SendResult')


def _runtime_send(method: Callable[Concatenate[ChannelHost, _SendArgs], Awaitable[_SendResult]]) -> Callable[Concatenate[ChannelHost, _SendArgs], Coroutine[Any, Any, _SendResult]]:
    @wraps(method)
    async def owned(host: ChannelHost, *args: _SendArgs.args, **kwargs: _SendArgs.kwargs) -> _SendResult:
        kwargs['delivery_deadline'] = min(time.monotonic() + 120,
            kwargs.get('delivery_deadline') if kwargs.get('delivery_deadline') is not None else float('inf'))
        # Freeze the only mutable byte mapping before crossing an await/loop.
        # The original body still validates its actor, IDs, bytes and metadata
        # on the actual channel loop, including authorization after its lease.
        frozen = kwargs.get('frozen_attachment_bytes')
        if frozen is not None:
            if not isinstance(frozen, Mapping) or not 1 <= len(frozen) <= 4:
                raise UnsupportedTargetError('冻结附件必须引用原始附件 ID')
            kwargs['frozen_attachment_bytes'] = dict(frozen)
        account_id = args[1] if len(args) > 1 else kwargs.get('account_id')
        account = host.store.get_account(account_id)
        kwargs['_delivery_generation'] = (host.connection_generation(account_id),
            host.store.directory_revision(account_id, account.channel)) if account is not None else None
        return await host._runtime_owner.run(lambda: method(host, *args, **kwargs))
    return owned


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
        self._runtime_owner = ChannelRuntimeOwner()
        self.intake = ChannelIntake()
        self._directory_pages = DirectoryPages()
        self._directory_requests: set[tuple[str, str]] = set()

    def bind_runtime_loop(self) -> None:
        """Bind before background admission; never transfer live resources."""
        previous, current = self._runtime_owner.loop, asyncio.get_running_loop()
        if previous is not current and (self._inflight_sends or self._inflight_events
                or previous is not None and self._adapters):
            raise RuntimeError('the original channel host still owns resources')
        changed = self._runtime_owner.bind()
        if changed and previous is not None:
            self._send_state_lock = asyncio.Lock()
            self._account_idle.clear()

    def register(self, adapter: ChannelAdapter) -> None:
        account = self.store.get_account(adapter.account_id)
        if account is None:
            raise KeyError(adapter.account_id)
        if account.channel != adapter.channel:
            raise ValueError("适配器平台与账号配置不匹配")
        self._adapters[adapter.account_id] = adapter
        self._adapter_generations.setdefault(id(adapter), uuid.uuid4().hex)

    def unregister(self, account_id: str, *, keep_paused: bool = False) -> None:
        adapter = self._adapters.pop(account_id, None)
        if adapter is not None:
            self._adapter_generations.pop(id(adapter), None)
        if not self._inflight_sends.get(account_id) and not self._inflight_events.get(account_id):
            if not keep_paused:
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
        resume = getattr(self._adapters.get(account_id), 'resume_intake', None)
        if callable(resume):
            resume()

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

    def connection_generation(self, account_id: str, *, transport_generation: str | None = None) -> str | None:
        """Opaque connection identity for an accepted, non-replayable turn."""
        adapter = self._adapters.get(account_id)
        generation = self._adapter_generations.get(id(adapter)) if adapter is not None else None
        epoch_reader = getattr(adapter, "connection_generation", None)
        epoch = transport_generation if transport_generation is not None else (epoch_reader() if callable(epoch_reader) else None)
        return f"{generation}:{epoch}" if generation is not None and epoch is not None else generation

    def accepts_events(self, account_id: str) -> bool:
        account = self.store.get_account(account_id)
        return bool(account and self.store.account_effectively_enabled(account) and account_id in self._adapters and account_id not in self._paused_accounts)

    def start_private_progress(self, thread, *, run_id: str, authorize_source: Callable[[], bool]):
        """Bind temporary feedback to this source and connection; never record a reply."""
        if thread.channel != 'telegram' or thread.chat_type != 'private':
            return None
        adapter = self._adapters.get(thread.account_id)
        start = getattr(adapter, 'start_progress', None)
        if not callable(start) or not self.accepts_events(thread.account_id):
            return None
        target_id = target_from_chat(account_id=thread.account_id, chat_id=thread.chat_id, kind=TargetKind.PRIVATE).id
        target = self.store.get_target(thread.role_id, thread.account_id, target_id)
        if target is None:
            return None
        generation = self.connection_generation(thread.account_id)
        revision = self.store.directory_revision(thread.account_id, thread.channel)
        def authorized():
            account = self.store.get_account(thread.account_id)
            return bool(account and account.role_id == thread.role_id and account.allow_private
                and self.accepts_events(thread.account_id) and self._adapters.get(thread.account_id) is adapter
                and self.connection_generation(thread.account_id) == generation
                and self.store.directory_revision(thread.account_id, thread.channel) == revision
                and authorize_source())
        if not authorized():
            return None
        adapter.remember_target(target)
        return start(OutboundMessage(target, '', authorize_dispatch=authorized), run_id=run_id)

    def _group_reply_guard(self, account_id: str, target: Target) -> Callable[[], bool] | None:
        account = self.store.get_account(account_id)
        if account is None or account.channel != "qq" or target.kind is not TargetKind.GROUP:
            return None
        policy = self.store.group_reply_policy(account.role_id, account_id, target.chat_id)
        return lambda: policy[0] and self.store.group_reply_policy(account.role_id, account_id, target.chat_id) == policy

    async def send_retry_notice(self, adapter: ChannelAdapter, event: InboundEvent) -> DeliveryResult:
        """Answer rejected intake through the original connection, without history."""
        if adapter.account_id != event.account_id or adapter.channel != event.channel:
            raise ValueError("拒收提示只能回复原始来源账号")
        target = target_from_chat(
            account_id=event.account_id, chat_id=event.chat_id,
            kind=TargetKind.GROUP if event.chat_type is ChatType.GROUP else TargetKind.PRIVATE,
            display_name=event.group_name or event.sender_name,
        )
        group_guard = self._group_reply_guard(event.account_id, target)
        if group_guard is not None and not group_guard():
            return DeliveryResult(DeliveryStatus.FAILED, target, error="本群回复已关闭")
        async with self._send_state_lock:
            if id(adapter) not in self._adapter_generations:
                raise ChannelAdapterError("拒收提示的来源连接尚未注册")
            self._inflight_sends[event.account_id] = self._inflight_sends.get(event.account_id, 0) + 1
            self._account_idle.setdefault(event.account_id, asyncio.Event()).clear()
        try:
            adapter.remember_target(target)
            return await adapter.send(OutboundMessage(
                target, CHANNEL_INTAKE_RETRY_NOTICE, reply_to_message_id=event.external_message_id,
                authorize_dispatch=group_guard,
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
                **self.connection_snapshot(account.account_id),
                "supportedKinds": supported_kinds(account.channel),
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

    def connection_snapshot(self, account_id: str) -> dict[str, object]:
        account = self.store.get_account(account_id)
        adapter = self._adapters.get(account_id)
        if account is None or not self.store.account_effectively_enabled(account):
            return {"connection": "disconnected", "online": False, "observedAt": observed_now(), "reason": "disabled"}
        if adapter is None and account.last_error:
            return {"connection": "error", "online": None, "observedAt": account.updated_at, "reason": "connection_unavailable"}
        if account_id in self._paused_accounts:
            return {"connection": "connecting", "online": None, "observedAt": observed_now(), "reason": "configuration_switch"}
        if adapter is not None:
            return adapter.connection_snapshot()
        return {"connection": "unknown", "online": None, "observedAt": observed_now(), "reason": "no_connection_evidence"}

    def freeze_owner_target(self, role_id: str, account_id: str, target_id: str) -> dict[str, Any]:
        """Capture an observed owner destination without connecting or reading secrets.

        Adapter generations stay private to this host. Callers persist this opaque
        generation and ask this interface again; they never inspect its registry.
        """
        account = self.store.get_account(account_id)
        adapter = self._adapters.get(account_id)
        if (account is None or account.role_id != role_id or not self.store.account_effectively_enabled(account)
                or not account.allow_private or account_id in self._paused_accounts or adapter is None
                or not adapter.capabilities.private or TargetKind.PRIVATE not in adapter.capabilities.targets):
            raise PermissionError('owner target account is unavailable')
        target = next((item for item in self.store.owner_targets(role_id, account_id) if item.id == target_id), None)
        if (target is None or target.kind is not TargetKind.PRIVATE
                or is_blocked_identity(account.blocked_senders, sender_ids=(target.chat_id,), aliases=(target.display_name,))):
            raise PermissionError('target is not a currently paired owner private chat')
        binding = self.store.owner_target_binding(role_id, account_id, target.chat_id)
        if binding is None:
            raise PermissionError('owner target binding is unavailable')
        revision = self.store.directory_revision(account_id, account.channel)
        configuration = repr((self.store.channel_config(account_id), adapter.configuration_key()))
        return {'roleId': role_id, 'accountId': account_id, 'accountCreatedAt': account.created_at,
            'channel': account.channel, 'targetId': target.id, 'chatId': target.chat_id, 'kind': 'private',
            'directoryRevision': {'account': revision[0], 'platform': revision[1]}, 'ownerBinding': binding,
            'configurationHash': hashlib.sha256(configuration.encode()).hexdigest(),
            'adapterGeneration': self._adapter_generations[id(adapter)],
            'media': adapter.capabilities.media, 'displayName': target.display_name,
            'accountDisplayName': account.display_name, 'viaAccount': account.source_snapshot()}

    async def query_directory(self, role_id: str, account_id: str, query: DirectoryQuery, *,
                              scope: str, authorized: Callable[[], bool] | None = None) -> dict:
        key = (role_id, account_id)
        if not query.cursor:
            if (key in self._directory_requests or len(self._directory_requests) >= 4
                    or sum(item[0] == role_id for item in self._directory_requests) >= 2):
                raise DirectoryError("directory_busy")
            self._directory_requests.add(key)
        try:
            return await self._query_directory(role_id, account_id, query, scope=scope, authorized=authorized)
        finally:
            if not query.cursor:
                self._directory_requests.discard(key)

    async def _query_directory(self, role_id: str, account_id: str, query: DirectoryQuery, *,
                               scope: str, authorized: Callable[[], bool] | None) -> dict:
        account = self.store.get_account(account_id)
        if account is None or account.role_id != role_id:
            raise KeyError(account_id)
        if query.kind not in supported_kinds(account.channel):
            raise DirectoryError("unsupported")
        adapter = self._adapters.get(account_id)
        frozen = (account, self.store.channel_config(account_id), self._adapter_generations.get(id(adapter)),
                  self.store.directory_revision(account_id, account.channel), adapter.configuration_key() if adapter else None)

        def check():
            current = self.store.get_account(account_id)
            if (current is None or current != account or self.store.channel_config(account_id) != frozen[1]
                    or self.store.directory_revision(account_id, account.channel) != frozen[3]
                    or adapter is not None and adapter.configuration_key() != frozen[4]
                    or not self.store.account_effectively_enabled(current) or account_id in self._paused_accounts
                    or self._adapters.get(account_id) is not adapter
                    or authorized is not None and not authorized()):
                raise PermissionError("directory_permission_revoked")

        check()
        owner = (role_id, account_id, scope, repr(frozen))
        if query.cursor:
            result = self._directory_pages.read(owner, query)
            check()
            return result
        self._directory_pages.ensure_capacity(owner)
        if query.kind == "known":
            targets = self.store.list_targets(role_id, account_id, limit=MAX_RECORDS + 1, stable=True,
                max_bytes=directory.MAX_RESPONSE_BYTES)
            records = [target_payload(target) for target in targets]
        else:
            if adapter is None:
                raise DirectoryError("disconnected")
            if query.kind in {"members", "member"}:
                if not account.allow_group or not self.store.known_group(role_id, account_id, query.group_id):
                    raise DirectoryError("group_not_authorized")
            pending = asyncio.create_task(adapter.query_directory(query))
            try:
                observation = await asyncio.wait_for(asyncio.shield(pending), timeout=directory.QUERY_TIMEOUT)
            except (asyncio.CancelledError, TimeoutError):
                pending.cancel()
                while not pending.done():
                    try:
                        await asyncio.shield(pending)
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        break
                if not pending.cancelled():
                    pending.exception()
                raise
            targets = observation.targets
            records = [target_payload(target) for target in targets] + observation.members
            for record in records:
                record.setdefault("canSend", True)
            bound_records(records)
            check()
            def still_allowed():
                check()
                return True

            self.store.remember_targets(role_id, targets, authorized=still_allowed)
            for target in targets:
                adapter.remember_target(target)
        check()
        result = self._directory_pages.create(owner, query, records, coverage="known" if query.kind == "known" else "platform")
        check()
        return result

    async def start(self, account_id: str) -> None:
        account = self.store.get_account(account_id)
        if account is None or not self.store.account_effectively_enabled(account):
            return
        await self.adapter(account_id).start()

    async def stop(self, account_id: str) -> None:
        adapter = self._adapters.get(account_id)
        if adapter is not None:
            await adapter.stop()

    def capture_media_source(self, account_id: str) -> dict:
        """Capture before ingress can wait; a later check may only revoke it."""
        account = self.store.get_account(account_id)
        if account is None:
            return {}
        bindings = sorted((str(item["identityId"]), str(item["pairedAt"]))
            for item in self.store.list_identities() if item["channel"] == account.channel
            and (item["scope"] == "platform" or item["accountId"] == account_id))
        return {"roleId": account.role_id, "channel": account.channel, "createdAt": account.created_at,
            "revision": self.store.directory_revision(account_id, account.channel),
            "generation": self.connection_generation(account_id), "bindings": bindings}

    def media_authorization(self, role_id: str, event) -> Callable[[], bool]:
        """Freeze an admitted media source; checks can revoke, never renew it."""
        account = self.store.get_account(event.account_id)
        adapter = self._adapters.get(event.account_id)
        source = event.metadata.get("hostMediaSource", self.capture_media_source(event.account_id))
        generation = (event.metadata.get("hostConnectionGeneration", source.get("generation"))
            if event.channel == "qq" else source.get("generation"))
        group_revision = (self.store.group_reply_policy(role_id, event.account_id, event.chat_id)
            if event.channel == "qq" and event.is_group and event.triggered else None)

        def authorized() -> bool:
            current = self.store.get_account(event.account_id)
            if (account is None or current is None or adapter is None
                    or (current.role_id, current.channel, current.created_at) != (role_id, event.channel, account.created_at)
                    or not self.store.account_effectively_enabled(current)
                    or self._adapters.get(event.account_id) is not adapter
                    or self.connection_generation(event.account_id) != generation
                    or self.capture_media_source(event.account_id) != source
                    or event.sender_id in current.blocked_senders
                    or not (current.allow_group if event.is_group else current.allow_private)):
                return False
            if group_revision is not None and (not group_revision[0]
                    or self.store.group_reply_policy(role_id, event.account_id, event.chat_id) != group_revision):
                return False
            return True

        return authorized

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
            self.store.remember_inbound_member(current.role_id, event)
            await self.refresh_targets(account_id, event=event)
        return event

    async def refresh_targets(self, account_id: str, *, event: InboundEvent | None = None) -> list[Target]:
        adapter = self.adapter(account_id)
        account = self.store.get_account(account_id)
        if account is None:
            raise KeyError(account_id)
        if not self.store.account_effectively_enabled(account) and not self._has_active_event(account_id):
            raise ChannelAdapterError("渠道账号或平台未启用")
        targets = adapter.inbound_targets(event) if event is not None else await adapter.list_targets()
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

    @_runtime_send
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
        frozen_attachment_bytes: Mapping[str, bytes] | None = None,
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
        authorize_source: Callable[[], bool] | None = None,
        message_metadata: Mapping[str, object] | None = None,
        delivery_deadline: float | None = None,
        _delivery_generation=None,
    ) -> DeliveryResult:
        """Send to an observed target and persist the resulting delivery state.

        ``actor='external'`` can only contact the paired owner through the
        originating account. Guest turns cannot choose third-party targets.
        Frozen bytes require the original role-owned IDs and a continuing push
        or current-source reply authorization; they never replace attachment ownership or metadata.
        """

        delivery_deadline = min(time.monotonic() + 120, delivery_deadline or float('inf'))
        frozen_payloads: dict[str, bytes] | None = None
        if frozen_attachment_bytes is not None:
            if not isinstance(frozen_attachment_bytes, Mapping) or not 1 <= len(frozen_attachment_bytes) <= 4:
                raise UnsupportedTargetError("冻结附件必须引用原始附件 ID")
            frozen_payloads = dict(frozen_attachment_bytes)
            if (not (actor == "push" or actor == "reply" and source_kind in {"chat_meme", "natural", "role_reaction"})
                    or not callable(authorize_source) or attachments
                    or not 1 <= len(attachment_ids) <= 4
                    or len(set(attachment_ids)) != len(attachment_ids)
                    or set(frozen_payloads) != set(attachment_ids)
                    or any(not isinstance(data, bytes) or not data or len(data) > 10 * 1024 * 1024
                           for data in frozen_payloads.values())
                    or sum(len(data) for data in frozen_payloads.values()) > 40 * 1024 * 1024):
                raise UnsupportedTargetError("冻结附件必须匹配原始许可，且每份不超过 10 MB")

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

        group_guard = self._group_reply_guard(account_id, target)
        if group_guard is not None and not group_guard():
            raise UnsupportedTargetError("本群回复已关闭")

        def dispatch_authorized():
            current = self.store.get_account(account_id)
            return bool(current and current.role_id == role_id and self.store.account_effectively_enabled(current)
                and _delivery_generation == (self.connection_generation(account_id),
                    self.store.directory_revision(account_id, current.channel))
                and (group_guard is None or group_guard())
                and (authorize_source is None or authorize_source())
                and (actor not in {'external', 'push'} or owner_binding is not None
                    and self.store.owner_target_binding(role_id, account_id, target.chat_id) == owner_binding))

        owner_binding = self.store.owner_target_binding(role_id, account_id, target.chat_id) if actor in {'external', 'push'} else None

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
                frozen_data = frozen_payloads[attachment_id] if frozen_payloads is not None else None
                if frozen_data is not None and (type(item.get("size")) is not int or item["size"] != len(frozen_data)):
                    raise UnsupportedTargetError("冻结附件与原始附件元数据不匹配")
                attachment_metadata.append(item)
                resolved.append(Attachment(
                    kind=str(item.get("kind") or "file"),
                    url=str(item["path"]),
                    file_name=str(item.get("name") or "attachment"),
                    mime_type=str(item.get("mediaType") or "application/octet-stream"),
                    size=int(item["size"]) if isinstance(item.get("size"), int) else None,
                    frozen_data=frozen_data,
                ))
            attachments = (*attachments, *resolved)

        adapter = await self._begin_send(account_id)
        try:
            if not dispatch_authorized():
                raise UnsupportedTargetError("原始任务来源授权已失效")
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
            pending_message = None
            if record_message:
                _, pending_message = self.store.append_event(thread, direction="outbound", content=content,
                    external_message_id=None, role="assistant", status="pending",
                    metadata={**dict(message_metadata or {}), "naturalReply": source_kind == 'natural',
                        "deliveryAttemptId": attempt_id, "deliveryStatus": "pending", "attachments": attachment_metadata})
            dispatch_cancelled = False
            latest_details = {}
            def record_parts(details):
                if details.get('receiptKind') == 'transport_acceptance' and account.channel != 'grok_bot':
                    raise ChannelAdapterError('transport_acceptance_requires_grok')
                details['channel'] = account.channel
                latest_details.clear()
                latest_details.update(details)
                self.store.record_delivery_parts(attempt_id, details)
                self.store.publish(role_id, {'type': 'delivery', 'roleId': role_id, 'attemptId': attempt_id})
            try:
                outbound = OutboundMessage(
                    target=target,
                    content=content,
                    reply_to_message_id=reply_to_message_id,
                    attachments=attachments,
                    metadata={"deliveryAttemptId": attempt_id, "mentionIds": list(mention_ids)},
                    # Ordinary replies and current-source stickers share the
                    # last-safe-point guard; proactive frozen leases are unchanged.
                    authorize_dispatch=dispatch_authorized,
                    record_delivery=record_parts,
                    delivery_deadline=delivery_deadline,
                    reserve_reply=lambda message_id, count: self.store.reserve_passive_reply(attempt_id, message_id, count),
                )
                stream_start = getattr(adapter, "stream_start", None)
                stream_update = getattr(adapter, "stream_update", None)
                stream_finalize = getattr(adapter, "stream_finalize", None)
                if stream_updates is not None and callable(stream_start) and callable(stream_update) and callable(stream_finalize) and not attachments:
                    async with asyncio.timeout(max(0, delivery_deadline - time.monotonic())):
                        if not dispatch_authorized():
                            raise UnsupportedTargetError('original_stream_source_revoked')
                        session = await stream_start(target, reply_to_message_id=reply_to_message_id, outbound=outbound)
                        stream_content = content
                        try:
                            async for update in stream_updates:
                                content = stream_content = update
                                if not dispatch_authorized():
                                    raise UnsupportedTargetError('original_stream_source_revoked')
                                await stream_update(session, update)
                            content = stream_content
                            result = await stream_finalize(session, stream_content)
                        finally:
                            task = getattr(session, 'task', None)
                            if task is not None and not task.done():
                                task.cancel()
                                await asyncio.gather(task, return_exceptions=True)
                else:
                    if stream_updates is not None:
                        async for _ in stream_updates:
                            pass
                    result = await adapter.send(outbound)
            except asyncio.CancelledError:
                dispatch_cancelled = True
                result = DeliveryResult(DeliveryStatus.UNCERTAIN, target, error="dispatch_cancelled")
            except Exception as error:
                status = DeliveryStatus.UNCERTAIN if is_delivery_uncertain(error) else DeliveryStatus.FAILED
                result = DeliveryResult(status, target, error=type(error).__name__)
            if result.raw.get('receiptKind') == 'transport_acceptance' and account.channel != 'grok_bot':
                result = DeliveryResult(DeliveryStatus.UNCERTAIN, target, error='invalid_transport_receipt')
            result.raw['channel'] = account.channel
            if latest_details and not result.raw.get('parts'):
                for part in latest_details.get('parts', []):
                    if part['status'] == 'sending':
                        part.update(status='uncertain', error=result.error or 'dispatch_interrupted')
                result = replace(result, raw=latest_details,
                    platform_message_id=next(iter(latest_details.get('platform_message_ids', [])), result.platform_message_id))
                parts = latest_details.get('parts', [])
                if parts and not any(part.get('status') == 'uncertain' for part in parts):
                    complete = not latest_details.get('streaming') and all(
                        part.get('status') == 'sent' and part.get('kind') != 'preview' for part in parts)
                    result = replace(result, status=DeliveryStatus.SENT if complete else DeliveryStatus.FAILED,
                        error=None if complete else result.error)
                from .delivery_projection import confirmed_parts
                result.raw['partialDelivery'] = bool(confirmed_parts(result.raw)) and result.status is not DeliveryStatus.SENT
            if result.raw.get('parts'):
                self.store.record_delivery_parts(attempt_id, result.raw)
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
                **{key: result.raw[key] for key in ('parts', 'partialDelivery', 'deliveredContent', 'receiptKind', 'deliveryDeadlineAt') if key in result.raw},
            }
            if pending_message is not None:
                updated = self.store.sessions.update_message(pending_message.id, content,
                    "completed" if result.status is DeliveryStatus.SENT or result.raw.get('partialDelivery') else "failed",
                    metadata={**pending_message.metadata, **metadata})
                self.store.publish(thread.role_id, {"type": "message", "roleId": thread.role_id,
                    "thread": self.store.public_thread_payload(thread),
                    "message": self.store.public_message_payload(thread, updated)})
            elif record_message:
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
            if dispatch_cancelled:
                raise asyncio.CancelledError
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
