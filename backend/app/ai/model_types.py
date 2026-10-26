"""Provider-neutral model messages and streaming events.

The runtime deliberately keeps its public message model small.  These types
belong to the model boundary and preserve the structure that providers need
when a response is replayed (thinking, images, tool calls and usage).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias


@dataclass(frozen=True, slots=True)
class ModelTextBlock:
    text: str
    type: Literal["text"] = "text"
    text_signature: str | None = None


@dataclass(frozen=True, slots=True)
class ModelThinkingBlock:
    text: str = ""
    signature: str | None = None
    type: Literal["thinking"] = "thinking"
    redacted: bool = False


@dataclass(frozen=True, slots=True)
class ModelImageBlock:
    media_type: str
    data_base64: str
    detail: Literal["low", "high", "auto"] = "auto"
    type: Literal["image"] = "image"


@dataclass(frozen=True, slots=True)
class ModelToolCallBlock:
    id: str
    name: str
    arguments: dict[str, object] = field(default_factory=dict)
    type: Literal["tool_call"] = "tool_call"
    thought_signature: str | None = None
    namespace: str | None = None
    arguments_complete: bool = True


ModelContentBlock: TypeAlias = (
    ModelTextBlock
    | ModelThinkingBlock
    | ModelImageBlock
    | ModelToolCallBlock
)


@dataclass(frozen=True, slots=True)
class ModelCost:
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    total: float = 0.0


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost: ModelCost = field(default_factory=ModelCost)


@dataclass(frozen=True, slots=True)
class ModelResponseMetadata:
    provider: str | None = None
    api: str | None = None
    model: str | None = None
    response_id: str | None = None
    usage: ModelUsage = field(default_factory=ModelUsage)
    stop_reason: str | None = None
    error_message: str | None = None
    timestamp: int | None = None


@dataclass(frozen=True, slots=True)
class ModelSystemMessage:
    content: tuple[ModelTextBlock, ...] = ()
    tools_added: tuple[dict[str, object], ...] = ()
    tools_removed: tuple[str, ...] = ()
    role: Literal["system"] = "system"
    timestamp: int | None = None


@dataclass(frozen=True, slots=True)
class ModelUserMessage:
    content: tuple[ModelContentBlock, ...] = ()
    role: Literal["user"] = "user"
    timestamp: int | None = None


@dataclass(frozen=True, slots=True)
class ModelAssistantMessage:
    content: tuple[ModelContentBlock, ...] = ()
    metadata: ModelResponseMetadata = field(default_factory=ModelResponseMetadata)
    role: Literal["assistant"] = "assistant"
    timestamp: int | None = None


@dataclass(frozen=True, slots=True)
class ModelToolResultMessage:
    content: tuple[ModelContentBlock, ...] = ()
    tool_call_id: str = ""
    tool_name: str = ""
    is_error: bool = False
    role: Literal["tool_result"] = "tool_result"
    details: object | None = None
    usage: ModelUsage = field(default_factory=ModelUsage)
    nested_calls: tuple[ModelToolCallBlock, ...] = ()
    timestamp: int | None = None


ModelMessage: TypeAlias = (
    ModelSystemMessage
    | ModelUserMessage
    | ModelAssistantMessage
    | ModelToolResultMessage
)


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    supports_images: bool = True
    supports_reasoning: bool = True
    preserve_thinking_signatures: bool = True
    tool_call_id_pattern: str | None = None
    max_tool_call_id_length: int | None = None
    preserve_tool_result_images: bool = False


@dataclass(frozen=True, slots=True)
class ModelStreamStart:
    type: Literal["start"] = "start"


@dataclass(frozen=True, slots=True)
class ModelTextStart:
    type: Literal["text_start"] = "text_start"


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    text: str
    type: Literal["text_delta"] = "text_delta"


@dataclass(frozen=True, slots=True)
class ModelTextEnd:
    type: Literal["text_end"] = "text_end"


@dataclass(frozen=True, slots=True)
class ModelThinkingStart:
    type: Literal["thinking_start"] = "thinking_start"


@dataclass(frozen=True, slots=True)
class ModelThinkingDelta:
    text: str
    type: Literal["thinking_delta"] = "thinking_delta"


@dataclass(frozen=True, slots=True)
class ModelThinkingEnd:
    type: Literal["thinking_end"] = "thinking_end"


@dataclass(frozen=True, slots=True)
class ModelToolCallStart:
    id: str
    name: str
    type: Literal["tool_call_start"] = "tool_call_start"


@dataclass(frozen=True, slots=True)
class ModelToolCallDelta:
    id: str
    arguments: str
    type: Literal["tool_call_delta"] = "tool_call_delta"


@dataclass(frozen=True, slots=True)
class ModelToolCallEnd:
    call: ModelToolCallBlock
    type: Literal["tool_call_end"] = "tool_call_end"


@dataclass(frozen=True, slots=True)
class ModelStreamDone:
    message: ModelAssistantMessage
    type: Literal["done"] = "done"


@dataclass(frozen=True, slots=True)
class ModelStreamError:
    error: str
    message: ModelAssistantMessage | None = None
    type: Literal["error"] = "error"


ModelStreamEvent: TypeAlias = (
    ModelStreamStart
    | ModelTextStart
    | ModelTextDelta
    | ModelTextEnd
    | ModelThinkingStart
    | ModelThinkingDelta
    | ModelThinkingEnd
    | ModelToolCallStart
    | ModelToolCallDelta
    | ModelToolCallEnd
    | ModelStreamDone
    | ModelStreamError
)
