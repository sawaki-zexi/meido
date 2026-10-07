"""Provider-neutral Agent Runtime primitives used by Meido."""

from .events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from .capabilities import CapabilityRegistry, CapabilityResolution, CapabilitySnapshot
from .loop import run_agent_loop
from .memory_tool import MemoryReadPort, MemoryRecallTool, RoleScopedMemoryReadPort
from .meido_provider import MeidoProvider
from .runtime import ActiveRunError, RuntimeManager
from .shell_tool import ShellTool
from .provider import (
    AssistantDoneEvent,
    CancellationToken,
    ProviderErrorEvent,
    ProviderEvent,
    ProviderStartEvent,
    TextDeltaEvent,
    ToolCallEndEvent,
)
from .tools import AgentTool, AgentToolResult, ToolContext, ToolDefinition, ToolRegistry, summarize_tool_arguments
from .types import AgentMessage, AssistantMessage, ToolCall, ToolResultMessage, UserMessage

__all__ = [
    "AgentEndEvent",
    "AgentEvent",
    "AgentMessage",
    "AgentStartEvent",
    "AgentTool",
    "AgentToolResult",
    "ActiveRunError",
    "CapabilityRegistry",
    "CapabilityResolution",
    "CapabilitySnapshot",
    "AssistantDoneEvent",
    "AssistantMessage",
    "CancellationToken",
    "MessageEndEvent",
    "MessageStartEvent",
    "MessageUpdateEvent",
    "MeidoProvider",
    "MemoryReadPort",
    "MemoryRecallTool",
    "RoleScopedMemoryReadPort",
    "RuntimeManager",
    "ShellTool",
    "ProviderErrorEvent",
    "ProviderEvent",
    "ProviderStartEvent",
    "TextDeltaEvent",
    "ToolCall",
    "ToolCallEndEvent",
    "ToolContext",
    "ToolDefinition",
    "ToolExecutionEndEvent",
    "ToolExecutionStartEvent",
    "ToolExecutionUpdateEvent",
    "ToolRegistry",
    "summarize_tool_arguments",
    "ToolResultMessage",
    "TurnEndEvent",
    "TurnStartEvent",
    "UserMessage",
    "run_agent_loop",
]
