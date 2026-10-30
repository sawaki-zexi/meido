"""Tool loading facts from the same raw transcript used for model context."""
from collections.abc import Sequence
import json

from .types import AgentMessage, AssistantMessage, ToolResultMessage


def window_tool_names(messages: Sequence[AgentMessage]) -> tuple[str, ...]:
    names: dict[str, None] = {}
    for message in messages:
        if isinstance(message, AssistantMessage):
            for call in message.tool_calls:
                names.setdefault(call.name, None)
        elif isinstance(message, ToolResultMessage):
            names.setdefault(message.tool_name, None)
            if message.is_error:
                continue
            for name in message.unlocked_tools:
                names.setdefault(name, None)
            if message.tool_name != "tool_search":
                continue
            details = message.details
            # Original search receipts already stored unlocked names in details.
            # Older transcripts may contain only the original JSON response.
            if not isinstance(details, dict) and message.tool_name == "tool_search":
                try:
                    details = json.loads(message.content)
                except (ValueError, TypeError):
                    details = None
            unlocked = details.get("unlocked", ()) if isinstance(details, dict) else ()
            if isinstance(unlocked, (list, tuple)):
                for name in unlocked:
                    if isinstance(name, str) and name:
                        names.setdefault(name, None)
    return tuple(names)
