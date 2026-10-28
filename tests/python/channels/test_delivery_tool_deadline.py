import asyncio
import time

from backend.app.agent_runtime.loop import _execute_tool
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.tools import AgentToolResult, ToolContext, ToolDefinition, ToolRegistry
from backend.app.agent_runtime.types import ToolCall


def test_tool_receives_original_earliest_deadline_before_it_can_dispatch():
    observed = []
    class Tool:
        definition = ToolDefinition('deadline_probe', 'record deadline', {'type': 'object'}, timeout_seconds=20)
        async def execute(self, arguments, context, on_update=None):
            observed.append(context.deadline)
            return AgentToolResult('done')
    async def scenario():
        signal = CancellationToken()
        original = time.monotonic() + 10
        registry = ToolRegistry([Tool()])
        context = ToolContext('r', 'role:r', 'run', signal, deadline=original)
        await _execute_tool(ToolCall('call', 'deadline_probe', {}), registry, context, signal,
                            timeout=30, on_progress=lambda result: None)
        assert len(observed) == 1 and observed[0] <= original
        assert original - observed[0] < .1
    asyncio.run(scenario())
