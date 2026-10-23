from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias


@dataclass(frozen=True, slots=True)
class ImageContent:
    """Ephemeral image content passed to the next provider request."""

    media_type: str
    data_base64: str
    detail: Literal["low", "high", "auto"] = "low"


@dataclass(frozen=True, slots=True)
class AttachmentContent:
    """A user attachment that is available to the provider for this turn."""

    name: str
    media_type: str
    text: str = ""
    image: ImageContent | None = None


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UserMessage:
    content: str
    attachments: tuple[AttachmentContent, ...] = ()
    role: Literal["user"] = "user"
    timestamp: int | None = None
    source_message_ids: tuple[str, ...] = field(default=(), compare=False)


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: Literal["stop", "tool_use", "error", "aborted"] = "stop"
    role: Literal["assistant"] = "assistant"
    metadata: dict[str, object] = field(default_factory=dict)
    timestamp: int | None = None
    source_message_ids: tuple[str, ...] = field(default=(), compare=False)


@dataclass(frozen=True, slots=True)
class ToolResultMessage:
    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False
    details: object | None = None
    images: tuple[ImageContent, ...] = ()
    role: Literal["tool"] = "tool"
    usage: object | None = None
    nested_calls: tuple[ToolCall, ...] = ()
    timestamp: int | None = None
    source_message_ids: tuple[str, ...] = field(default=(), compare=False)


AgentMessage: TypeAlias = UserMessage | AssistantMessage | ToolResultMessage
