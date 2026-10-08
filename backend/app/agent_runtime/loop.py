from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import nullcontext
import math
import time
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
from .capabilities import CapabilityResolution
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
    capabilities: CapabilityResolution | None = None,
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
    registry = ToolRegistry(tools) if capabilities is None else capabilities
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
            tool_context = (
                ToolContext(
                    role_id=registry.snapshot.role_id,
                    session_key=registry.snapshot.session_key,
                    run_id=registry.snapshot.run_id,
                    signal=token,
                )
                if isinstance(registry, CapabilityResolution)
                else ToolContext(role_id=role_id, session_key=session_key, run_id=run_id, signal=token)
            )
            result = await _execute_tool(
                call,
                registry,
                tool_context,
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
    registry: ToolRegistry | CapabilityResolution,
    context: ToolContext,
    signal: CancellationToken,
    timeout: float | None,
) -> tuple[AgentToolResult, list[AgentToolResult]]:
    started = time.monotonic()
    tool = registry.get(call.name)
    if tool is None:
        reason = registry.denial_reason(call.name) if isinstance(registry, CapabilityResolution) else None
        content = f"tool denied: {call.name}: {reason}" if reason else f"unknown tool: {call.name}"
        status = "denied" if reason else "unknown"
        return _finalize_tool_result(
            AgentToolResult(content, details={"tool": call.name, "reason": reason or "unknown"}, is_error=True),
            started=started,
            status=status,
        ), []
    updates: list[AgentToolResult] = []

    def bounded(result: AgentToolResult) -> tuple[AgentToolResult, int]:
        output_chars = _result_output_chars(result)
        definition = (
            registry.definition(call.name)
            if isinstance(registry, CapabilityResolution)
            else tool.definition
        ) or tool.definition
        output_limit = definition.output_limit
        if output_limit is None or len(result.content) <= output_limit:
            return result, output_chars
        details = _details_mapping(result.details)
        details["truncated"] = True
        details["outputLimit"] = output_limit
        return AgentToolResult(
            content=result.content[:output_limit],
            details=details,
            is_error=result.is_error,
        ), output_chars

    def on_update(result: AgentToolResult) -> None:
        bounded_result, output_chars = bounded(result)
        updates.append(_finalize_tool_result(
            bounded_result,
            started=started,
            status=_detail_status(bounded_result.details) or "running",
            output_chars=output_chars,
        ))

    def finish(result: AgentToolResult, *, status: str | None = None) -> AgentToolResult:
        bounded_result, output_chars = bounded(result)
        return _finalize_tool_result(
            bounded_result,
            started=started,
            status=status,
            output_chars=output_chars,
        )

    try:
        signal.raise_if_cancelled()
        arguments = dict(call.arguments)
        if isinstance(registry, CapabilityResolution):
            arguments, hook_error = await registry.prepare_tool_call(call.name, arguments, context)
            if hook_error is not None:
                return finish(hook_error), updates
        try:
            registry.validate_arguments(call.name, arguments)
        except ValueError as error:
            return finish(
                AgentToolResult(
                    str(error),
                    details={"errorType": type(error).__name__, "message": "invalid tool arguments"},
                    is_error=True,
                ),
                status="invalid_arguments",
            ), updates
        pending = resolve_tool_result(tool.execute(arguments, context, on_update))
        definition = (
            registry.definition(call.name)
            if isinstance(registry, CapabilityResolution)
            else tool.definition
        ) or tool.definition
        tool_timeout = definition.timeout_seconds
        effective_timeout = (
            min(timeout, tool_timeout)
            if timeout is not None and tool_timeout is not None
            else tool_timeout if tool_timeout is not None else timeout
        )
        result = await asyncio.wait_for(pending, timeout=effective_timeout) if effective_timeout is not None else await pending
        if not isinstance(result, AgentToolResult):
            return finish(AgentToolResult("tool returned an invalid result", is_error=True), status="failed"), updates
        return finish(result), updates
    except asyncio.TimeoutError:
        return finish(
            AgentToolResult(
                "tool timed out",
                details={"errorType": "TimeoutError", "message": "tool timed out"},
                is_error=True,
            ),
            status="timed_out",
        ), updates
    except RuntimeError as error:
        if str(error) == "agent run cancelled":
            return finish(
                AgentToolResult(
                    "tool cancelled",
                    details={"errorType": type(error).__name__, "message": "tool cancelled"},
                    is_error=True,
                ),
                status="cancelled",
            ), updates
        return finish(
            AgentToolResult(
                "tool execution failed",
                details={"errorType": type(error).__name__, "message": "tool execution failed"},
                is_error=True,
            ),
            status="failed",
        ), updates
    except Exception as error:
        return finish(
            AgentToolResult(
                "tool execution failed",
                details={"errorType": type(error).__name__, "message": "tool execution failed"},
                is_error=True,
            ),
            status="failed",
        ), updates


_TOOL_RESULT_STATUSES = frozenset({
    "running",
    "unknown",
    "denied",
    "invalid_arguments",
    "timed_out",
    "cancelled",
    "failed",
    "succeeded",
})


def _details_mapping(details: object | None) -> dict[str, object]:
    if isinstance(details, Mapping):
        return dict(details)
    if details is None:
        return {}
    return {"value": details}


def _detail_status(details: object | None) -> str | None:
    if not isinstance(details, Mapping):
        return None
    status = details.get("status")
    return status if isinstance(status, str) and status in _TOOL_RESULT_STATUSES else None


def _result_output_chars(result: AgentToolResult) -> int:
    if isinstance(result.details, Mapping):
        output_chars = result.details.get("outputChars")
        if isinstance(output_chars, int) and not isinstance(output_chars, bool) and output_chars >= 0:
            return output_chars
    return len(result.content)


def _finalize_tool_result(
    result: AgentToolResult,
    *,
    started: float,
    status: str | None = None,
    output_chars: int | None = None,
) -> AgentToolResult:
    details = _details_mapping(result.details)
    effective_status = status or _detail_status(details) or ("failed" if result.is_error else "succeeded")
    details["status"] = effective_status
    measured_duration = round(max(0.0, time.monotonic() - started), 3)
    existing_duration = details.get("durationSeconds")
    if (
        isinstance(existing_duration, bool)
        or not isinstance(existing_duration, (int, float))
        or not math.isfinite(existing_duration)
        or existing_duration < 0
    ):
        details["durationSeconds"] = measured_duration
    existing_output_chars = details.get("outputChars")
    if (
        isinstance(existing_output_chars, bool)
        or not isinstance(existing_output_chars, int)
        or existing_output_chars < 0
    ):
        details["outputChars"] = output_chars if output_chars is not None else len(result.content)
    if not isinstance(details.get("truncated"), bool):
        details["truncated"] = False
    return AgentToolResult(
        result.content,
        details=details,
        is_error=result.is_error or effective_status in {"unknown", "denied", "invalid_arguments", "timed_out", "cancelled", "failed"},
    )
