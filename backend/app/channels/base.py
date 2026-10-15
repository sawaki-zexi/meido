from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from .models import ChannelCapabilities, DeliveryResult, DeliveryStatus, InboundEvent, OutboundMessage, Target


class ChannelAdapterError(RuntimeError):
    """An adapter rejected or could not process a platform operation."""


class UnsupportedTargetError(ChannelAdapterError):
    """The platform does not support the requested target type."""


class DuplicateEventError(ChannelAdapterError):
    """An event was already accepted by this adapter."""


class PartialDeliveryError(ChannelAdapterError):
    """A message was accepted at least once before a later operation failed."""

    def __init__(self, message: str, *, platform_message_id: str | None = None) -> None:
        super().__init__(message)
        self.platform_message_id = platform_message_id


class PlatformRejectedError(ChannelAdapterError):
    """The platform returned an explicit rejection for an operation."""


SendTransport = Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]
EventHandler = Callable[[Mapping[str, Any]], Awaitable[None]]


def is_delivery_timeout(error: BaseException) -> bool:
    """Classify timeout types from HTTP/WebSocket SDKs without importing them."""

    return isinstance(error, (TimeoutError, asyncio.TimeoutError)) or "timeout" in type(error).__name__.lower()


def is_delivery_uncertain(error: BaseException) -> bool:
    """Detect transport failures where the remote side may have accepted data."""

    name = type(error).__name__.lower()
    if is_delivery_timeout(error) or isinstance(error, (ConnectionError, OSError, PartialDeliveryError)):
        return True
    if "transport" in name and not _is_local_transport_error(error):
        return True
    message = str(error).casefold()
    if any(marker in message for marker in ("connection", "network", "transport", "protocol", "socket", "broken pipe")):
        return True
    return any(
        marker in name for marker in (
            "connection", "connect", "network", "transport", "protocol", "socket",
            "readerror", "writeerror", "remote", "brokenpipe",
        )
    )


def delivery_status_for_error(error: BaseException) -> DeliveryStatus:
    """Map an outbound exception to the public delivery contract.

    HTTP 4xx responses (apart from 408) are explicit platform rejections. A
    timeout, 5xx, disconnect, protocol failure, or otherwise unknown
    transport error leaves acceptance ambiguous and must not be retried as a
    definite failure.
    """

    if isinstance(error, PartialDeliveryError):
        return DeliveryStatus.UNCERTAIN
    if isinstance(error, PlatformRejectedError):
        return DeliveryStatus.FAILED
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        if status == 408 or status >= 500:
            return DeliveryStatus.UNCERTAIN
        if 400 <= status < 500:
            return DeliveryStatus.FAILED
    return DeliveryStatus.UNCERTAIN if is_delivery_uncertain(error) else DeliveryStatus.FAILED


def is_blocked_identity(
    blocked: Iterable[str],
    *,
    sender_ids: Iterable[Any] = (),
    aliases: Iterable[Any] = (),
) -> bool:
    """Match exact platform IDs and non-empty display aliases safely."""

    configured = {str(value).strip() for value in blocked if str(value).strip()}
    if not configured:
        return False
    ids = {str(value).strip() for value in sender_ids if str(value).strip()}
    if ids.intersection(configured):
        return True
    normalized = {value.casefold() for value in configured}
    return any(str(value).strip().casefold() in normalized for value in aliases if str(value).strip())


def _is_local_transport_error(error: BaseException) -> bool:
    message = str(error).casefold()
    return any(marker in message for marker in (
        "invalid", "unsupported", "missing", "缺少", "无效", "不支持", "未配置", "超过", "必须",
    ))


class EventDedupe:
    """Small in-process replay window; the host must persist message IDs too."""

    def __init__(self, *, max_entries: int = 4096, ttl_seconds: float = 3600) -> None:
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._items: OrderedDict[str, float] = OrderedDict()
        self._lock = asyncio.Lock()

    async def seen_or_add(self, event_id: str) -> bool:
        now = time.monotonic()
        async with self._lock:
            while self._items and now - next(iter(self._items.values())) > self.ttl_seconds:
                self._items.popitem(last=False)
            if event_id in self._items:
                self._items.move_to_end(event_id)
                return True
            self._items[event_id] = now
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)
            return False


class ChannelAdapter(ABC):
    channel: str
    account_id: str
    capabilities: ChannelCapabilities

    @abstractmethod
    def parse_event(self, payload: Mapping[str, Any]) -> InboundEvent | None:
        """Parse one platform payload; return None for unsupported events."""

    @abstractmethod
    async def send(self, message: OutboundMessage) -> DeliveryResult:
        """Send through the currently active account connection."""

    @abstractmethod
    async def list_targets(self) -> list[Target]:
        """Return only targets observed or discoverable through this account."""

    def remember_target(self, target: Target) -> None:
        """Hydrate a target restored from the host's durable directory.

        Adapters with an in-memory target cache inherit this implementation;
        custom adapters may override it when their transport needs additional
        target metadata before sending.
        """

        if target.account_id != self.account_id:
            raise ValueError("目标账号与适配器不匹配")
        known = getattr(self, "_targets", None)
        if isinstance(known, dict):
            known[target.chat_id] = target

    async def accept_event(self, payload: Mapping[str, Any]) -> InboundEvent | None:
        event = self.parse_event(payload)
        if event is None:
            return None
        if await self._dedupe.seen_or_add(event.external_event_id):
            return None
        return event

    async def start(self) -> None:
        """Start an injected lifecycle-aware transport when present."""

        transport = getattr(self, "_transport", None)
        starter = getattr(transport, "start", None)
        if callable(starter):
            await starter(self._emit_payload)

    async def stop(self) -> None:
        """Stop an injected lifecycle-aware transport when present."""

        transport = getattr(self, "_transport", None)
        stopper = getattr(transport, "stop", None)
        if callable(stopper):
            await stopper()

    def configuration_key(self) -> str:
        """Stable account configuration identity used during handover."""

        return f"{self.channel}:{self.account_id}"

    def __init__(self) -> None:
        self._dedupe = EventDedupe()
        self._event_handler: EventHandler | None = None

    def set_event_handler(self, handler: EventHandler | None) -> None:
        """Set the host callback for raw platform events.

        The callback deliberately receives the original platform payload. The
        host remains responsible for normalization, authorization and durable
        deduplication.
        """

        self._event_handler = handler

    async def _emit_payload(self, payload: Mapping[str, Any]) -> None:
        if self._event_handler is not None:
            await self._event_handler(payload)
