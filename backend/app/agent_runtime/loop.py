from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import aclosing, nullcontext
from dataclasses import replace
import math
import inspect
import json
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
from .capabilities import CapabilityResolution, CapabilitySnapshot
from .progress import ToolProgressBuffer
from .provider import (
    AssistantDoneEvent,
    CancellationToken,
    ModelProvider,
    ProviderErrorEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallEndEvent,
)
from .tools import AgentTool, AgentToolResult, ToolContext, ToolDefinition, ToolRegistry, resolve_tool_result
from .types import AgentMessage, AssistantMessage, ToolCall, ToolResultMessage, UserMessage


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
    on_capability_revision: Callable[[CapabilitySnapshot], Awaitable[None] | None] | None = None,
    on_tool_result: Callable[[ToolResultMessage], Awaitable[None] | None] | None = None,
    can_continue: Callable[[], bool] | None = None,
) -> AsyncIterator[AgentEvent]:
    """Run the provider/tool loop while keeping the transcript mutable.

    The loop deliberately knows nothing about FastAPI, SQLite, roles or memory.
    Consumers persist messages at ``message_end`` boundaries and may translate
    the events to a transport-specific stream.
    """

    token = signal or CancellationToken()
    registry = ToolRegistry(tools) if capabilities is None else capabilities
    sequence = 0
    used_call_ids = {call.id for message in messages if isinstance(message, AssistantMessage) for call in message.tool_calls}

    async def emit(event: AgentEvent) -> AsyncIterator[AgentEvent]:
        nonlocal sequence
        sequence += 1
        yield stamp_event(event, run_id=run_id, sequence=sequence)

    async for event in emit(AgentStartEvent()):
        yield event
    turn = 0
    recovery_messages: tuple[AgentMessage, ...] = ()
    empty_reply_usage = None

    while True:
        if can_continue is not None and not can_continue():
            token.cancel()
        if capabilities is not None:
            registry = capabilities.current()
        if token.is_cancelled():
            aborted = AssistantMessage(stop_reason="aborted", metadata={"completion_origin": "host_cancel"})
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
        provider_timed_out = False
        started = False

        try:
            managed_timeout = getattr(provider, 'manages_request_timeout', False)
            source = provider.stream_response(
                model=model,
                system=system,
                messages=(*messages, *recovery_messages),
                tools=tuple(registry.provider_schemas()),
                signal=token,
                session_id=session_id,
                **({'provider_timeout': provider_timeout} if managed_timeout else {}),
            )
            timeout_context = asyncio.timeout(provider_timeout) if provider_timeout is not None and not managed_timeout else nullcontext()
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
                    elif isinstance(provider_event, ThinkingDeltaEvent):
                        if not started:
                            started = True
                            async for event in emit(MessageStartEvent(AssistantMessage())):
                                yield event
                        async for event in emit(MessageUpdateEvent(AssistantMessage(content=content), '', thinking_delta=provider_event.delta)):
                            yield event
                    elif isinstance(provider_event, ToolCallEndEvent):
                        if not started:
                            started = True
                            async for event in emit(MessageStartEvent(AssistantMessage())):
                                yield event
                        tool_calls.append(provider_event.tool_call)
                    elif isinstance(provider_event, AssistantDoneEvent):
                        assistant = provider_event.message
                        if assistant.stop_reason == "error":
                            terminal_error = str(assistant.metadata.get("error_message") or "模型调用失败")
                    elif isinstance(provider_event, ProviderErrorEvent):
                        terminal_error = provider_event.error
                        assistant = AssistantMessage(content=content, tool_calls=tuple(tool_calls), stop_reason="error")
                        break
        except TimeoutError:
            provider_timed_out = True
            terminal_error = "provider timed out"
            assistant = AssistantMessage(content=content, tool_calls=tuple(tool_calls), stop_reason="error")
        except asyncio.CancelledError:
            token.cancel()
            raise
        except Exception as error:
            terminal_error = str(error)
            assistant = AssistantMessage(content=content, tool_calls=tuple(tool_calls), stop_reason="error")
        finally:
            # Images from a tool result are valid for exactly the provider
            # request that follows that tool call. Keep text/details in the
            # transcript while dropping the potentially large image bytes.
            _expire_ephemeral_images(messages)

        if can_continue is not None and not can_continue():
            token.cancel()

        if token.is_cancelled():
            assistant = AssistantMessage(content=content, tool_calls=tuple(tool_calls), stop_reason="aborted", metadata={"completion_origin": "host_cancel"})
        elif assistant is None:
            terminal_error = terminal_error or "provider ended without assistant_done"
            assistant = AssistantMessage(content=content, tool_calls=tuple(tool_calls), stop_reason="error")
        elif not assistant.content and content:
            assistant = AssistantMessage(
                content=content,
                tool_calls=assistant.tool_calls or tuple(tool_calls),
                stop_reason=assistant.stop_reason,
                metadata=dict(assistant.metadata),
            )
        elif tool_calls and not assistant.tool_calls:
            assistant = AssistantMessage(
                content=assistant.content,
                tool_calls=tuple(tool_calls),
                stop_reason=assistant.stop_reason,
                metadata=dict(assistant.metadata),
            )
        # Batch admission precedes every execution event and execution hook.
        # Repaired JSON may be displayed, but never supplies executable evidence.
        incomplete_arguments = any(not call.arguments_complete for call in assistant.tool_calls)
        if assistant.tool_calls and (assistant.stop_reason not in {"stop", "tool_use"} or incomplete_arguments):
            terminal_error = "回复未完成，本批工具未执行"
            rejected = [{"id": call.id, "name": call.name, "arguments_complete": call.arguments_complete}
                        for call in assistant.tool_calls]
            assistant = replace(assistant, tool_calls=(),
                stop_reason=assistant.stop_reason if assistant.stop_reason in {"length", "aborted", "error", "incomplete"} else "incomplete",
                metadata={**assistant.metadata, "provider_stop_reason": assistant.metadata.get("stop_reason", assistant.stop_reason),
                          "tool_batch_rejected": True, "rejected_tool_calls": rejected,
                          "completion_origin": "host_cancel" if token.is_cancelled() else "provider"})
        if assistant.stop_reason not in {"stop", "tool_use", "length", "incomplete", "error", "aborted"}:
            assistant = replace(assistant, stop_reason="incomplete", metadata={
                **assistant.metadata, "provider_stop_reason": assistant.stop_reason})
        if assistant.stop_reason in {"length", "incomplete"}:
            terminal_error = terminal_error or "回复未完成"

        if assistant.tool_calls and assistant.stop_reason == "stop":
            assistant = AssistantMessage(
                content=assistant.content,
                tool_calls=assistant.tool_calls,
                stop_reason="tool_use",
                metadata=dict(assistant.metadata),
            )

        if (assistant.stop_reason == "stop" and not assistant.tool_calls and not assistant.content.strip()
                and not assistant.metadata.get('replyReaction')):
            if not recovery_messages:
                # Retry this empty request once with the same tool visibility.
                # The transient instruction is never a durable user message.
                empty_reply_usage = assistant.metadata.get("usage")
                recovery_messages = (assistant, UserMessage("你刚才没有给出回复或工具调用。请继续处理当前任务；直接给出正式回复或需要的工具调用。"))
                async for event in emit(TurnEndEvent(assistant)):
                    yield event
                continue
            terminal_error = "模型未返回正文或工具调用（已重试一次）"
            assistant = replace(assistant, stop_reason="error", metadata={
                **assistant.metadata, "error_message": terminal_error,
                "emptyReplyRecovery": {"attempts": 2, "outcome": "empty", "initialUsage": empty_reply_usage},
            })
        elif recovery_messages:
            assistant = replace(assistant, metadata={**assistant.metadata,
                "emptyReplyRecovery": {"attempts": 2, "outcome": "recovered", "initialUsage": empty_reply_usage}})
        recovery_messages = ()
        empty_reply_usage = None

        assistant = _assign_call_identities(assistant, used_call_ids, run_id=run_id, turn=turn)
        messages.append(assistant)
        if not started:
            async for event in emit(MessageStartEvent(assistant)):
                yield event
        async for event in emit(MessageEndEvent(assistant)):
            yield event
        if assistant.stop_reason in {"length", "incomplete", "error", "aborted"}:
            reason: Literal["failed", "cancelled"] = "cancelled" if assistant.stop_reason == "aborted" else "failed"
            async for event in emit(TurnEndEvent(assistant)):
                yield event
            async for event in emit(AgentEndEvent(tuple(messages), reason=reason, error=terminal_error,
                                                       failure_kind="provider_timeout" if provider_timed_out else None)):
                yield event
            return

        results: list[ToolResultMessage] = []
        for call in assistant.tool_calls:
            if can_continue is not None and not can_continue():
                token.cancel()
                break
            definition = registry.candidate_definition(call.name) if isinstance(registry, CapabilityResolution) else None
            async for event in emit(ToolExecutionStartEvent(
                call.id, call.name, dict(call.arguments),
                snapshot_id=registry.snapshot.snapshot_id if isinstance(registry, CapabilityResolution) else None,
                tool_source=definition.source if definition is not None else None,
                tool_version=definition.version if definition is not None else None,
                policy_decision=registry.snapshot.policy_decisions.get(call.name) if isinstance(registry, CapabilityResolution) else None,
                capability_binding=registry.call_record(call.name) if isinstance(registry, CapabilityResolution) else None,
            )):
                yield event
            tool_context = (
                ToolContext(
                    role_id=registry.snapshot.role_id,
                    session_key=registry.snapshot.session_key,
                    run_id=registry.snapshot.run_id,
                    signal=token,
                    tool_call_id=call.id,
                    capability_snapshot_id=registry.snapshot.snapshot_id,
                    capability_binding=registry,
                )
                if isinstance(registry, CapabilityResolution)
                else ToolContext(role_id=role_id, session_key=session_key, run_id=run_id, signal=token, tool_call_id=call.id)
            )
            tool_message: ToolResultMessage | None = None

            async def settle_result(final_result: AgentToolResult) -> None:
                nonlocal tool_message
                tool_message = ToolResultMessage(
                    tool_call_id=call.id,
                    tool_name=call.name,
                    content=final_result.content,
                    is_error=final_result.is_error,
                    details=final_result.details,
                    images=final_result.images,
                )
                messages.append(tool_message)
                if on_tool_result is not None:
                    persisted = on_tool_result(tool_message)
                    if inspect.isawaitable(persisted):
                        await persisted

            async with aclosing(_execute_tool_events(call, registry, tool_context, token, tool_timeout, settle_result)) as execution:
                async for tool_event in execution:
                    async for event in emit(tool_event):
                        yield event
            assert tool_message is not None
            results.append(tool_message)
            async for event in emit(MessageStartEvent(tool_message)):
                yield event
            async for event in emit(MessageEndEvent(tool_message)):
                yield event
            if capabilities is not None and not tool_message.is_error and not token.is_cancelled():
                revision = capabilities.pending_revision(call.id)
                if revision is not None:
                    if on_capability_revision is not None:
                        persisted = on_capability_revision(revision.snapshot)
                        if inspect.isawaitable(persisted):
                            await persisted
                    if not token.is_cancelled():
                        capabilities.publish_revision(revision)
            elif capabilities is not None:
                capabilities.discard_pending(call.id)
            if can_continue is not None and not can_continue():
                token.cancel()
            if token.is_cancelled():
                break
        async for event in emit(TurnEndEvent(assistant, tuple(results))):
            yield event
        if not assistant.tool_calls:
            async for event in emit(AgentEndEvent(tuple(messages), reason="completed")):
                yield event
            return


def _assign_call_identities(assistant: AssistantMessage, used_ids: set[str], *, run_id: str, turn: int) -> AssistantMessage:
    if not assistant.tool_calls:
        return assistant
    calls = []
    provider_ids = {}
    for index, call in enumerate(assistant.tool_calls):
        identity = call.id
        counter = 0
        while identity in used_ids:
            identity = f"host-call-{run_id}-{turn}-{index}-{counter}"
            counter += 1
        used_ids.add(identity)
        provider_ids[identity] = call.id
        calls.append(replace(call, id=identity))
    return replace(assistant, tool_calls=tuple(calls), metadata={
        **assistant.metadata, "provider_tool_call_ids": provider_ids,
    })


async def _execute_tool_events(
    call: ToolCall, registry: ToolRegistry | CapabilityResolution, context: ToolContext,
    signal: CancellationToken, timeout: float | None,
    on_result: Callable[[AgentToolResult], Awaitable[None]],
) -> AsyncIterator[ToolExecutionUpdateEvent | ToolExecutionEndEvent]:
    tool = registry.get(call.name)
    definition = registry.definition(call.name) if isinstance(registry, CapabilityResolution) else tool.definition if tool else None
    identity = {"toolCallId": call.id, "toolName": call.name, "roleId": context.role_id,
                "sessionKey": context.session_key, "runId": context.run_id}
    # Avoid copying arbitrary provider IDs just to measure them; very large
    # identity envelopes get no disposable progress, but keep the final result.
    overhead = (len(json.dumps(identity, ensure_ascii=False).encode("utf-8")) + 1024
                if all(len(value) <= 2048 for value in identity.values()) else 32 * 1024)
    progress = ToolProgressBuffer(output_limit=definition.output_limit if definition else None,
                                  envelope_overhead=overhead)

    async def execute() -> AgentToolResult:
        try:
            result = await _execute_tool(call, registry, context, signal, timeout, progress.add)
            progress.close()
            # A settled result belongs to the host transcript, not to the
            # disposable subscriber queue. Join persistence even on close.
            finalized = asyncio.create_task(on_result(result))
            while not finalized.done():
                try:
                    await asyncio.shield(finalized)
                except asyncio.CancelledError:
                    signal.cancel()
            finalized.result()
            return result
        finally:
            progress.close()

    pending = asyncio.create_task(execute())
    wake: asyncio.Task[bool] | None = None
    try:
        while progress.pending_count or not pending.done():
            if progress.pending_count:
                update = progress.pop()
                yield ToolExecutionUpdateEvent(call.id, call.name, update, dropped_updates=progress.dropped_updates)
                continue
            if progress.closed:
                # Persistence can still be running after execute closes the
                # update window; do not spin on the already-set ready event.
                await asyncio.shield(pending)
                continue
            wake = asyncio.create_task(progress.ready.wait())
            await asyncio.wait((pending, wake), return_when=asyncio.FIRST_COMPLETED)
            if not wake.done():
                wake.cancel()
            await asyncio.gather(wake, return_exceptions=True)
            wake = None
        yield ToolExecutionEndEvent(call.id, call.name, pending.result(), dropped_updates=progress.dropped_updates)
    finally:
        progress.close(discard=True)
        if wake is not None:
            wake.cancel()
        if not pending.done():
            signal.cancel()
            pending.cancel()
        cleanup = asyncio.gather(pending, *([wake] if wake is not None else []), return_exceptions=True)
        interrupted: asyncio.CancelledError | None = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as error:
                interrupted = error
        if interrupted is not None:
            raise interrupted


async def _execute_tool(
    call: ToolCall,
    registry: ToolRegistry | CapabilityResolution,
    context: ToolContext,
    signal: CancellationToken,
    timeout: float | None,
    on_progress: Callable[[AgentToolResult], None],
) -> AgentToolResult:
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
        )
    tool_signal: CancellationToken | None = None
    accepting_updates = True

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
            images=result.images,
        ), output_chars

    def on_update(result: AgentToolResult) -> None:
        # The local token closes the callback window before timeout/revocation
        # cleanup; the call-scoped buffer also rejects callbacks after settle.
        if not accepting_updates or signal.is_cancelled() or tool_signal is not None and tool_signal.is_cancelled():
            return
        on_progress(result)

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
            denial = registry.authorization_denial(registry.definition(call.name), context, arguments)
            if denial is not None:
                return finish(AgentToolResult(denial, details={"reason": denial}, is_error=True), status="denied")
            arguments, hook_error = await registry.prepare_tool_call(call.name, arguments, context)
            if hook_error is not None:
                return finish(hook_error)
            denial = registry.authorization_denial(registry.definition(call.name), context, arguments)
            if denial is not None:
                return finish(AgentToolResult(denial, details={"reason": denial}, is_error=True), status="denied")
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
            )
        tool_signal = CancellationToken(parent=signal)
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
        now = time.monotonic()
        if context.deadline is not None:
            remaining = max(0, context.deadline - now)
            effective_timeout = min(effective_timeout, remaining) if effective_timeout is not None else remaining
        tool_context = replace(context, signal=tool_signal,
            deadline=now + effective_timeout if effective_timeout is not None else None)
        execute = registry.executor(call.name) if isinstance(registry, CapabilityResolution) else tool.execute

        async def invoke() -> AgentToolResult:
            nonlocal accepting_updates
            try:
                tool_signal.raise_if_cancelled()
                return await resolve_tool_result(execute(arguments, tool_context, on_update))
            finally:
                # Close at execute settlement, not when the monitoring task
                # happens to resume (call_soon callbacks may race that resume).
                accepting_updates = False

        pending = invoke()
        result = await _await_authorized_tool(
            pending, registry=registry, definition=definition, context=context,
            signal=signal, tool_signal=tool_signal, timeout=effective_timeout, arguments=arguments,
        )
        if not isinstance(result, AgentToolResult):
            return finish(AgentToolResult("tool returned an invalid result", is_error=True), status="failed")
        return finish(result)
    except asyncio.TimeoutError:
        return finish(
            AgentToolResult(
                "tool timed out",
                details={"errorType": "TimeoutError", "message": "tool timed out"},
                is_error=True,
            ),
            status="timed_out",
        )
    except RuntimeError as error:
        if str(error) == "agent run cancelled":
            signal.cancel()
            return finish(
                AgentToolResult(
                    "tool cancelled",
                    details={"errorType": type(error).__name__, "message": "tool cancelled"},
                    is_error=True,
                ),
                status="cancelled",
            )
        return finish(
            AgentToolResult(
                "tool execution failed",
                details={"errorType": type(error).__name__, "message": "tool execution failed"},
                is_error=True,
            ),
            status="failed",
        )
    except Exception as error:
        return finish(
            AgentToolResult(
                "tool execution failed",
                details={"errorType": type(error).__name__, "message": "tool execution failed"},
                is_error=True,
            ),
            status="failed",
        )


async def _await_authorized_tool(
    pending: Awaitable[AgentToolResult], *, registry: ToolRegistry | CapabilityResolution,
    definition: ToolDefinition, context: ToolContext, signal: CancellationToken,
    tool_signal: CancellationToken, timeout: float | None, arguments: Mapping[str, object],
) -> AgentToolResult:
    task = asyncio.ensure_future(pending)
    deadline = time.monotonic() + timeout if timeout is not None else None
    try:
        while True:
            # A settled result is a fact, not permission to start another action.
            if task.done():
                return task.result()
            signal.raise_if_cancelled()
            if tool_signal.is_cancelled():
                signal.cancel()
                signal.raise_if_cancelled()
            if isinstance(registry, CapabilityResolution):
                denial = registry.authorization_denial(definition, context, arguments)
                if denial is not None:
                    return AgentToolResult(denial, details={"reason": denial, "status": "denied"}, is_error=True)
            remaining = deadline - time.monotonic() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                raise asyncio.TimeoutError
            await asyncio.wait((task,), timeout=min(0.05, remaining) if remaining is not None else 0.05)
    except asyncio.CancelledError:
        # The subscriber may cancel the monitor after execute already settled.
        # Preserve that known fact; the outer owner still propagates cancellation.
        if task.done() and not task.cancelled():
            return task.result()
        raise
    finally:
        tool_signal.cancel()
        if not task.done():
            task.cancel()
        cleanup = asyncio.gather(task, return_exceptions=True)
        interrupted: asyncio.CancelledError | None = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as error:
                interrupted = error
        if interrupted is not None:
            raise interrupted


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
        images=result.images,
    )


def _expire_ephemeral_images(messages: list[AgentMessage]) -> None:
    for index, message in enumerate(messages):
        if isinstance(message, ToolResultMessage) and message.images:
            messages[index] = replace(message, images=())
