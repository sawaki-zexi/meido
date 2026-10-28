"""Provider-neutral model core.

The package owns transcript types, message conversion, protocol adapters and
the adapter registry. Application code should depend on this public seam
instead of importing provider implementations directly.
"""

from .message_transform import transform_messages
from .model_adapter import (
    ModelAdapter,
    ModelResponseDone,
    ModelStreamError,
    ModelTextDelta,
    ModelToolCall,
    OpenAICompatibleAdapter,
)
from .model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
from .model_types import (
    ModelAssistantMessage,
    ModelCapabilities,
    ModelContentBlock,
    ModelCost,
    ModelImageBlock,
    ModelMessage,
    ModelResponseMetadata,
    ModelStreamDone,
    ModelStreamError as RichModelStreamError,
    ModelStreamEvent,
    ModelSystemMessage,
    ModelTextBlock,
    ModelThinkingBlock,
    ModelToolCallBlock,
    ModelToolResultMessage,
    ModelUsage,
    ModelUserMessage,
)

__all__ = [
    "ModelAdapter",
    "ModelResponseDone",
    "ModelAdapterRegistry",
    "ModelAssistantMessage",
    "ModelCapabilities",
    "ModelContentBlock",
    "ModelCost",
    "ModelImageBlock",
    "ModelMessage",
    "ModelResponseMetadata",
    "ModelStreamDone",
    "ModelStreamError",
    "ModelSystemMessage",
    "ModelTextBlock",
    "ModelTextDelta",
    "ModelThinkingBlock",
    "ModelToolCall",
    "ModelToolCallBlock",
    "ModelToolResultMessage",
    "ModelUsage",
    "ModelUserMessage",
    "OpenAICompatibleAdapter",
    "RichModelStreamError",
    "RoutedModelAdapter",
    "ModelStreamEvent",
    "transform_messages",
]
