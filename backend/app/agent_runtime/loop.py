from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import nullcontext
from typing import Literal

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
    stamp_event,
)
from .provider import (
    AssistantDoneEvent,
    CancellationToken,
    ModelProvider,
    ProviderErrorEvent,
    TextDeltaEvent,
    ToolCallEndEvent,
)
from .tools import AgentTool, AgentToolResult, ToolContext, ToolRegistry, resolve_tool_result
from .types import AgentMessage, AssistantMessage, ToolCall, ToolResultMessage


async def run_agent_loop(
    *,
    provider: ModelProvider,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: Sequence[AgentTool] = (),
    max_turns: int | None = None,
    signal: CancellationToken | None = None,
    session_id: str | None = None,
    role_id: str = "",
    session_key: str = "",
    run_id: str = "",
    provider_timeout: float | None = None,
    tool_timeout: float | None = None,
) -> AsyncIterator[AgentEvent]:
    """Run the provider/tool loop while keeping the transcript mutable.

    The loop deliberately knows nothing about FastAPI, SQLite, roles or memory.
    Consumers persist messages at ``message_end`` boundaries and may translate
    the events to a transport-specific stream.
    """

    token = signal or CancellationToken()
    registry = ToolRegistry(tools)
    sequence = 0

    async def emit(event: AgentEvent) -> AsyncIterator[AgentEvent]:
        nonlocal sequence
        sequence += 1
        yield stamp_event(event, run_id=run_id, sequence=sequence)

    async for event in emit(AgentStartEvent()):
        yield event
    turn = 0

    while True:
        if token.is_cancelled():
            aborted = AssistantMessage(stop_reason="aborted")
            messages.append(aborted)
            async for event in emit(MessageStartEvent(aborted)):
                yield event
            async for event in emit(MessageEndEvent(aborted)):
                yield event
            async for event in emit(AgentEndEvent(tuple(messages), reason="cancelled")):
                yield event
            return
        turn += 1
        if max_turns is not None and turn > max_turns:
            exhausted = AssistantMessage(content=f"Agent stopped after max_turns={max_turns}", stop_reason="error")
            messages.append(exhausted)
            async for event in emit(MessageStartEvent(exhausted)):
                yield event
            async for event in emit(MessageEndEvent(exhausted)):
                yield event
            async for event in emit(AgentEndEvent(tuple(messages), reason="max_turns", error=f"Agent stopped after max_turns={max_turns}")):
                yield event
            return

        async for event in emit(TurnStartEvent(turn)):
            yield event
        content = ""
        tool_calls: list[ToolCall] = []
        assistant: AssistantMessage | None = None
        terminal_error: str | None = None
        started = False

        try:
            source = provider.stream_response(
                model=model,
                system=system,
                messages=tuple(messages),
                tools=tuple(registry.provider_schemas()),
                signal=token,
                session_id=session_id,
            )
            timeout_context = asyncio.timeout(provider_timeout) if provider_timeout is not None else nullcontext()
            async with timeout_context:
                async for provider_event in source:
                    if token.is_cancelled():
                        break
                    if isinstance(provider_event, TextDeltaEvent):
                        if not started:
                            started = True
                            async for event in emit(MessageStartEvent(AssistantMessage())):
                                yield event
                        content += provider_event.delta
                        current = AssistantMessage(content=content, tool_calls=tuple(tool_calls))
                        async for event in emit(MessageUpdateEvent(current, provider_event.delta)):
                            yield event
                    elif isinstance(provider_event, ToolCallEndEvent):
                        if not started:
                            started = True
                            async for event in emit(MessageStartEvent(AssistantMessage())):
                                yield event
                        tool_calls.append(provider_event.tool_call)
                    elif isinstance(provider_event, AssistantDoneEvent):
                        assistant = provider_event.message
                    elif isinstance(provider_event, ProviderErrorEvent):
                        terminal_error = provider_event.error
                        assistant = AssistantMessage(content=content, stop_reason="error")
                        break
        except TimeoutError:
            terminal_error = "provider timed out"
            assistant = AssistantMessage(content=content, stop_reason="error")
        except asyncio.CancelledError:
            token.cancel()
            raise
        except Exception as error:
            terminal_error = str(error)
            assistant = AssistantMessage(content=content, stop_reason="error")

        if token.is_cancelled():
            assistant = AssistantMessage(content=content, tool_calls=tuple(tool_calls), stop_reason="aborted")
        elif assistant is None:
            terminal_error = terminal_error or "provider ended without assistant_done"
            assistant = AssistantMessage(content=content, tool_calls=tuple(tool_calls), stop_reason="error")
        elif not assistant.content and content:
            assistant = AssistantMessage(
                content=content,
                tool_calls=assistant.tool_calls or tuple(tool_calls),
                stop_reason=assistant.stop_reason,
            )
        elif tool_calls and not assistant.tool_calls:
            assistant = AssistantMessage(
                content=assistant.content,
                tool_calls=tuple(tool_calls),
                stop_reason=assistant.stop_reason,
            )
        if assistant.tool_calls and assistant.stop_reason == "stop":
            assistant = AssistantMessage(
                content=assistant.content,
                tool_calls=assistant.tool_calls,
                stop_reason="tool_use",
            )

        messages.append(assistant)
        if not started:
            async for event in emit(MessageStartEvent(assistant)):
                yield event
        async for event in emit(MessageEndEvent(assistant)):
            yield event
        if assistant.stop_reason in {"error", "aborted"}:
            reason: Literal["failed", "cancelled"] = "cancelled" if assistant.stop_reason == "aborted" else "failed"
            async for event in emit(TurnEndEvent(assistant)):
                yield event
            async for event in emit(AgentEndEvent(tuple(messages), reason=reason, error=terminal_error)):
                yield event
            return

        results: list[ToolResultMessage] = []
        for call in assistant.tool_calls:
            async for event in emit(ToolExecutionStartEvent(call.id, call.name, dict(call.arguments))):
                yield event
            result = await _execute_tool(
                call,
                registry,
                ToolContext(role_id=role_id, session_key=session_key, run_id=run_id, signal=token),
                token,
                tool_timeout,
            )
            for update in result[1]:
                async for event in emit(ToolExecutionUpdateEvent(call.id, call.name, update)):
                    yield event
            final_result = result[0]
            async for event in emit(ToolExecutionEndEvent(call.id, call.name, final_result)):
                yield event
            tool_message = ToolResultMessage(
                tool_call_id=call.id,
                tool_name=call.name,
                content=final_result.content,
                is_error=final_result.is_error,
                details=final_result.details,
            )
            messages.append(tool_message)
            results.append(tool_message)
            async for event in emit(MessageStartEvent(tool_message)):
                yield event
            async for event in emit(MessageEndEvent(tool_message)):
                yield event
        async for event in emit(TurnEndEvent(assistant, tuple(results))):
            yield event
        if not assistant.tool_calls:
            async for event in emit(AgentEndEvent(tuple(messages), reason="completed")):
                yield event
            return


async def _execute_tool(
    call: ToolCall,
    registry: ToolRegistry,
    context: ToolContext,
    signal: CancellationToken,
    timeout: float | None,
) -> tuple[AgentToolResult, list[AgentToolResult]]:
    tool = registry.get(call.name)
    if tool is None:
        return AgentToolResult(f"unknown tool: {call.name}", is_error=True), []
    updates: list[AgentToolResult] = []

    def on_update(result: AgentToolResult) -> None:
        updates.append(result)

    try:
        signal.raise_if_cancelled()
        registry.validate_arguments(call.name, call.arguments)
        pending = resolve_tool_result(tool.execute(call.arguments, context, on_update))
        result = await asyncio.wait_for(pending, timeout=timeout) if timeout is not None else await pending
        if not isinstance(result, AgentToolResult):
            return AgentToolResult("tool returned an invalid result", is_error=True), updates
        return result, updates
    except asyncio.TimeoutError:
        return AgentToolResult("tool timed out", is_error=True), updates
    except Exception as error:
        return AgentToolResult(str(error), is_error=True), updates
