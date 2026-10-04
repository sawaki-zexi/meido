"""Events emitted by committed conversation and Markdown maintenance boundaries."""

from __future__ import annotations

from dataclasses import dataclass

from .models import Message


@dataclass(frozen=True)
class TurnCommitted:
    """A completed user/assistant turn ready for independent consumers."""

    role_id: str
    session_key: str
    user_message: Message
    assistant_message: Message

    @property
    def stable_source_key(self) -> str:
        return f"turn:{self.session_key}:{self.user_message.id}:{self.assistant_message.id}"


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

