"""Claude-specific error-image projection, without changing stored messages."""

from .model_protocol import safe_tool_id
from .model_types import (
    ModelContentBlock, ModelImageBlock, ModelTextBlock, ModelThinkingBlock,
    ModelToolResultMessage,
)


def split_error_tool_images(
    message: ModelToolResultMessage,
) -> tuple[tuple[ModelContentBlock, ...], tuple[ModelContentBlock, ...]]:
    """Keep errors text-only and return images for the end of the result batch.

    Claude endpoints have rejected images nested in error results even though
    the public schema permits both fields. Successful results are unchanged.
    """
    images = tuple(block for block in message.content if isinstance(block, ModelImageBlock))
    if not message.is_error or not images:
        return message.content, ()

    content: list[ModelContentBlock] = []
    for block in message.content:
        if isinstance(block, ModelImageBlock):
            content.append(ModelTextBlock("\n[Image from this failed tool is attached after the tool results.]\n"))
        elif isinstance(block, ModelTextBlock):
            content.append(block)
        elif isinstance(block, ModelThinkingBlock) and block.text and not block.redacted:
            content.append(ModelTextBlock(block.text))
    caption = ModelTextBlock(
        f"Images from failed tool {message.tool_name} "
        f"(tool call {safe_tool_id(message.tool_call_id)}):"
    )
    return tuple(content), (caption, *images)
