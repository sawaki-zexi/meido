"""Events emitted by the committed conversation boundary."""

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

