"""Events emitted by committed conversation and memory pipeline boundaries.

The observer bus is deliberately best effort: telemetry consumers must never
change the outcome of a conversation or a memory write.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from .models import Message


@dataclass(frozen=True)
class TurnCommitted:
    """A completed user/assistant turn ready for independent consumers."""

    role_id: str
    session_key: str
    user_message: Message
    assistant_message: Message
    explicit_memory_ids: tuple[str, ...] = ()
    tool_metadata: dict[str, object] | None = None

    @property
    def stable_source_key(self) -> str:
        return f"turn:{self.session_key}:{self.user_message.id}:{self.assistant_message.id}"


@dataclass(frozen=True)
class TurnIngested:
    role_id: str
    session_key: str
    source_key: str
    memory_ids: tuple[str, ...] = ()
    implicit: bool = True
    error: str | None = None


@dataclass(frozen=True)
class RetrievalCompleted:
    role_id: str
    session_key: str
    query: str
    intent: str
    result_count: int
    injected_count: int = 0
    trace: dict[str, object] | None = None
    error: str | None = None


@dataclass(frozen=True)
class MemoryWritten:
    role_id: str
    session_key: str
    source_key: str
    memory_ids: tuple[str, ...] = ()
    operation: str = "upsert"
    error: str | None = None


class MemoryEventBus:
    """Small in-process observer port matching Shiori's event boundary."""

    def __init__(self) -> None:
        self._observers: list[Callable[[Any], object]] = []
        self.errors: list[str] = []
        self.events: list[Any] = []

    def subscribe(self, observer: Callable[[Any], object]) -> None:
        self._observers.append(observer)

    def publish(self, event: Any) -> None:
        self.events.append(event)
        if len(self.events) > 2000:
            del self.events[:-2000]
        for observer in tuple(self._observers):
            try:
                observer(event)
            except Exception as error:  # observers are never on the critical path
                self.errors.append(f"{type(event).__name__}: {error}")


@dataclass(frozen=True)
class ConsolidationCandidate:
    """A candidate extracted from one committed Markdown window."""

    memory_type: str
    summary: str
    source_key: str


@dataclass(frozen=True)
class ConsolidationCommitted:
    """A successfully committed Markdown consolidation window."""

    role_id: str
    session_key: str
    source_key: str
    message_ids: tuple[str, ...]
    message_range: tuple[int, int] | None
    candidates: tuple[ConsolidationCandidate, ...]
    error: str | None = None

