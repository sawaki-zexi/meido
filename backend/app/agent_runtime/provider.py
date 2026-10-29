from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from .types import AssistantMessage, ToolCall


class CancellationToken:
    """Cooperative cancellation; an optional parent's state flows to children."""

    def __init__(self, *, parent: CancellationToken | None = None) -> None:
        self._cancelled = False
        self._parent = parent

    @property
    def cancelled(self) -> bool:
        return self._cancelled or self._parent is not None and self._parent.cancelled

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self.cancelled

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise RuntimeError("agent run cancelled")


@dataclass(frozen=True, slots=True)
class ProviderStartEvent:
    type: str = "provider_start"


@dataclass(frozen=True, slots=True)
class TextDeltaEvent:
    delta: str
    type: str = "text_delta"


@dataclass(frozen=True, slots=True)
class ThinkingDeltaEvent:
    delta: str
    type: str = "thinking_delta"


@dataclass(frozen=True, slots=True)
class ToolCallEndEvent:
    tool_call: ToolCall
    type: str = "tool_call_end"


@dataclass(frozen=True, slots=True)
class AssistantDoneEvent:
    message: AssistantMessage
    type: str = "assistant_done"


@dataclass(frozen=True, slots=True)
class ProviderErrorEvent:
    error: str
    type: str = "provider_error"


ProviderEvent = ProviderStartEvent | TextDeltaEvent | ThinkingDeltaEvent | ToolCallEndEvent | AssistantDoneEvent | ProviderErrorEvent


class ModelProvider(Protocol):
    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[object],
        tools: Sequence[Mapping[str, object]],
        signal: CancellationToken,
        session_id: str | None = None,
    ) -> AsyncIterator[ProviderEvent]: ...
