from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UserMessage:
    content: str
    role: Literal["user"] = "user"


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: Literal["stop", "tool_use", "error", "aborted"] = "stop"
    role: Literal["assistant"] = "assistant"


@dataclass(frozen=True, slots=True)
class ToolResultMessage:
    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False
    details: object | None = None
    role: Literal["tool"] = "tool"


AgentMessage: TypeAlias = UserMessage | AssistantMessage | ToolResultMessage
