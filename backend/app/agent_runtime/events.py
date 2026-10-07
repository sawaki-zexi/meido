from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Literal, TypeVar

from .tools import AgentToolResult
from .types import AgentMessage, ToolResultMessage


@dataclass(frozen=True, slots=True)
class EventMetadata:
    """Identity shared by every event emitted during one runtime run."""

    run_id: str = ""
    sequence: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class AgentStartEvent:
    type: Literal["agent_start"] = "agent_start"
    run_id: str = ""
    sequence: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class AgentEndEvent:
    messages: tuple[AgentMessage, ...] = ()
    type: Literal["agent_end"] = "agent_end"
    reason: Literal["completed", "failed", "cancelled", "max_turns"] = "completed"
    error: str | None = None
    run_id: str = ""
    sequence: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class TurnStartEvent:
    turn: int
    type: Literal["turn_start"] = "turn_start"
    run_id: str = ""
    sequence: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class TurnEndEvent:
    message: AgentMessage
    tool_results: tuple[ToolResultMessage, ...] = ()
    type: Literal["turn_end"] = "turn_end"
    run_id: str = ""
    sequence: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class MessageStartEvent:
    message: AgentMessage
    type: Literal["message_start"] = "message_start"
    run_id: str = ""
    sequence: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class MessageUpdateEvent:
    message: AgentMessage
    delta: str
    type: Literal["message_update"] = "message_update"
    run_id: str = ""
    sequence: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class MessageEndEvent:
    message: AgentMessage
    type: Literal["message_end"] = "message_end"
    run_id: str = ""
    sequence: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class ToolExecutionStartEvent:
    tool_call_id: str
    tool_name: str
    arguments: dict[str, object] = field(default_factory=dict)
    type: Literal["tool_execution_start"] = "tool_execution_start"
    run_id: str = ""
    sequence: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class ToolExecutionUpdateEvent:
    tool_call_id: str
    tool_name: str
    result: AgentToolResult
    type: Literal["tool_execution_update"] = "tool_execution_update"
    run_id: str = ""
    sequence: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class ToolExecutionEndEvent:
    tool_call_id: str
    tool_name: str
    result: AgentToolResult
    type: Literal["tool_execution_end"] = "tool_execution_end"
    run_id: str = ""
    sequence: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


AgentEvent = (
    AgentStartEvent
    | AgentEndEvent
    | TurnStartEvent
    | TurnEndEvent
    | MessageStartEvent
    | MessageUpdateEvent
    | MessageEndEvent
    | ToolExecutionStartEvent
    | ToolExecutionUpdateEvent
    | ToolExecutionEndEvent
)


EventT = TypeVar("EventT", bound=AgentEvent)


def stamp_event(event: EventT, *, run_id: str, sequence: int) -> EventT:
    """Attach a run identity without changing event payload semantics."""

    return replace(
        event,
        run_id=run_id,
        sequence=sequence,
        timestamp=datetime.now(timezone.utc),
    )
