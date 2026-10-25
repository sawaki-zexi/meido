from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ChatType(StrEnum):
    PRIVATE = "private"
    GROUP = "group"


class TargetKind(StrEnum):
    PRIVATE = "private"
    GROUP = "group"
    GROUP_MEMBER = "group_member"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class ChannelCapabilities:
    private: bool = True
    group: bool = False
    group_listening: bool = False
    media: bool = False
    replies: bool = False
    streaming: bool = False
    targets: frozenset[TargetKind] = frozenset({TargetKind.PRIVATE})


@dataclass(frozen=True, slots=True)
class Attachment:
    kind: str
    url: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    size: int | None = None
    # Owned outgoing bytes stay in memory; message metadata retains the ID.
    frozen_data: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    kind: str
    data: bytes
    file_name: str | None = None
    mime_type: str | None = None


@dataclass(frozen=True, slots=True)
class InboundEvent:
    """Platform-neutral event handed to the host.

    ``triggered`` is intentionally a parsed fact, not an authorisation flag:
    the host still decides whether a role responds and which tools are shown.
    """

    channel: str
    account_id: str
    external_event_id: str
    external_message_id: str
    chat_id: str
    chat_type: ChatType
    sender_id: str
    sender_name: str = ""
    content: str = ""
    group_name: str | None = None
    mentioned: bool = False
    mentioned_ids: tuple[str, ...] = ()
    reply_to_message_id: str | None = None
    reply_to_sender_id: str | None = None
    attachments: tuple[Attachment, ...] = ()
    triggered: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    received_at: float | None = None

    @property
    def thread_id(self) -> str:
        return f"thread:{self.channel}:{self.account_id}:{self.chat_type.value}:{self.chat_id}"

    @property
    def is_group(self) -> bool:
        return self.chat_type is ChatType.GROUP


@dataclass(frozen=True, slots=True)
class Target:
    id: str
    kind: TargetKind
    display_name: str
    account_id: str
    chat_id: str
    capabilities: frozenset[TargetKind] = frozenset()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    target: Target
    content: str
    reply_to_message_id: str | None = None
    attachments: tuple[Attachment, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    # Host-only last-safe-point check; never derived from platform/model input.
    authorize_dispatch: Callable[[], bool] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    status: DeliveryStatus
    target: Target
    platform_message_id: str | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    attempt_id: str | None = None


def make_target_id(account_id: str, chat_id: str, kind: TargetKind, *, scope: str = "") -> str:
    """Return a stable opaque directory key without exposing platform IDs."""

    value = "\0".join((account_id, kind.value, scope, chat_id))
    return f"target:v1:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def target_from_chat(*, account_id: str, chat_id: str, kind: TargetKind, display_name: str = "", **metadata: Any) -> Target:
    scope = str(metadata.get("group_id") or metadata.get("groupId") or "") if kind is TargetKind.GROUP_MEMBER else ""
    return Target(
        id=make_target_id(account_id, chat_id, kind, scope=scope),
        kind=kind,
        display_name=display_name,
        account_id=account_id,
        chat_id=chat_id,
        capabilities=frozenset({kind}),
        metadata=metadata,
    )
