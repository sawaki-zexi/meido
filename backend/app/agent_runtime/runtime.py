from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, TypeAlias
from contextlib import aclosing
from datetime import datetime, timezone

from ..models import AgentRun, Message
from .events import AgentEndEvent, AgentEvent, MessageEndEvent, MessageUpdateEvent, TurnStartEvent, ToolExecutionStartEvent, ToolExecutionEndEvent
from .capabilities import CapabilityResolution, CapabilitySnapshot
from .loop import run_agent_loop
from .provider import CancellationToken, ModelProvider, AssistantDoneEvent, ToolCallEndEvent, ProviderErrorEvent
from .tools import AgentTool, summarize_tool_arguments
from .types import AgentMessage, AssistantMessage, ToolResultMessage

if TYPE_CHECKING:
    from ..session_store import SessionStore

MessageEndHandler: TypeAlias = Callable[[MessageEndEvent], object | Awaitable[object]]


class ActiveRunError(RuntimeError):
    """Raised when a role already has a reserved or running response."""


class RuntimeManager:
    """Own role reservation, run lifecycle, cancellation and terminal state."""

    def __init__(self, sessions: SessionStore) -> None:
        self.sessions = sessions
        self.role_locks: dict[str, asyncio.Lock] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._tasks: dict[str, asyncio.Task[object]] = {}
        self._reserved_roles: dict[str, str] = {}
        self._assistant_message_ids: dict[str, str | None] = {}
        self._reservation_end: dict[str, Callable[[], None]] = {}
        self._queued_roles: dict[str, int] = {}

    def lock_for(self, role_id: str) -> asyncio.Lock:
        return self.role_locks.setdefault(role_id, asyncio.Lock())

    async def natural_response(
        self,
        run: AgentRun,
        *,
        provider: ModelProvider,
        model: str,
        system: str,
        messages: list[AgentMessage],
        provider_timeout: float = 60.0,
        can_start: Callable[[], bool] | None = None,
    ) -> AssistantMessage:
        """Make one no-tool judgment without fabricating transcript messages.

        The host owns scoped input preparation and any eventual delivery. This
        entry shares ordinary role exclusion, run identity and cancellation,
        but its decision is not itself an assistant message sent to the group.
        A busy role is never added to the ordinary FIFO by this low priority path.
        """
        if not 0 < provider_timeout <= 120:
            raise ValueError("natural provider timeout must be between 0 and 120 seconds")
        lock = self.lock_for(run.roleId)
        if lock.locked() or self._queued_roles.get(run.roleId, 0):
            raise ActiveRunError(f"role already has an active or queued run: {run.roleId}")
        await lock.acquire()
        token = CancellationToken()
        created = False
        try:
            if can_start is not None and not can_start():
                raise ActiveRunError("natural input is no longer admitted")
            self.sessions.create_run(run)
            created = True
            self._reserved_roles[run.runId] = run.roleId
            self._tokens[run.runId] = token
            task = asyncio.current_task()
            if task is not None:
                self._tasks[run.runId] = task
            self.sessions.update_run(run.runId, status="running", turn_count=1)
            result = None
            async with asyncio.timeout(provider_timeout):
                stream = provider.stream_response(
                    model=model, system=system, messages=messages, tools=(),
                    signal=token, session_id=run.sessionKey,
                )
                async with aclosing(stream):
                    async for event in stream:
                        if token.is_cancelled():
                            raise asyncio.CancelledError()
                        if isinstance(event, ToolCallEndEvent):
                            raise RuntimeError("natural judgments cannot call tools")
                        if isinstance(event, ProviderErrorEvent):
                            raise RuntimeError(event.error)
                        if isinstance(event, AssistantDoneEvent):
                            if event.message.tool_calls or event.message.stop_reason != "stop":
                                raise RuntimeError("natural judgment returned an invalid completion")
                            if result is not None:
                                raise RuntimeError("natural judgment returned multiple completions")
                            result = event.message
            if token.is_cancelled():
                raise asyncio.CancelledError()
            if result is None:
                raise RuntimeError("natural judgment ended without a completion")
            # Keep provider accounting without turning a participation decision
            # into transcript content. Read the current snapshot so caller-side
            # diagnostics added during the request are not overwritten.
            current = self.sessions.get_run(run.runId)
            snapshot = dict(current.modelSnapshot if current is not None else run.modelSnapshot)
            if "usage" in result.metadata:
                snapshot["naturalUsage"] = result.metadata["usage"]
            self.sessions.update_run(run.runId, status="completed", ended_at=datetime.now(timezone.utc),
                                     model_snapshot=snapshot)
            return result
        except asyncio.CancelledError:
            token.cancel()
            if created:
                self.sessions.update_run(run.runId, status="cancelled", ended_at=datetime.now(timezone.utc),
                                         cancel_reason="自然回合已取消")
            raise
        except Exception as error:
            if created:
                self.sessions.update_run(run.runId, status="failed", ended_at=datetime.now(timezone.utc), error=str(error))
            raise
        finally:
            if run.runId in self._reserved_roles:
                self._release_run(run.runId)
            else:
                lock.release()

    async def reserve_run(self, run: AgentRun, user_content: str, *, attachments: list[dict[str, object]] | None = None, received_at: float | None = None,
                          on_accepting: Callable[[], Callable[[], None] | None] | None = None) -> tuple[AgentRun, Message, Message]:
        """Reserve a role and atomically create its run and initial messages."""

        lock = self.lock_for(run.roleId)
        if lock.locked():
            raise ActiveRunError(f"role already has an active run: {run.roleId}")
        await lock.acquire()
        on_end = None
        try:
            on_end = on_accepting() if on_accepting is not None else None
            result = self.sessions.create_run_with_messages(run, user_content, attachments=attachments, received_at=received_at)
        except BaseException:
            lock.release()
            if on_end is not None:
                on_end()
            raise
        self._reserved_roles[run.runId] = run.roleId
        self._assistant_message_ids[run.runId] = result[2].id
        self._tokens[run.runId] = CancellationToken()
        if on_end is not None:
            self._reservation_end[run.runId] = on_end
        return result

    async def reserve_existing_user_message(self, run: AgentRun, message_id: str, *, can_start: Callable[[], bool] | None = None,
                                            on_accepting: Callable[[], Callable[[], None] | None] | None = None) -> tuple[AgentRun, Message, Message]:
        """Reserve a run around a channel message already written to SQLite."""

        lock = self.lock_for(run.roleId)
        # Network intake has already durably accepted this message. Queue it
        # behind the current role turn instead of dropping it as ActiveRunError.
        self._queued_roles[run.roleId] = self._queued_roles.get(run.roleId, 0) + 1
        try:
            await lock.acquire()
        finally:
            remaining = self._queued_roles[run.roleId] - 1
            if remaining:
                self._queued_roles[run.roleId] = remaining
            else:
                self._queued_roles.pop(run.roleId, None)
        on_end = None
        try:
            if can_start is not None and not can_start():
                raise ActiveRunError("message is no longer admitted")
            on_end = on_accepting() if on_accepting is not None else None
            result = self.sessions.create_run_with_existing_user_message(run, message_id)
        except BaseException:
            lock.release()
            if on_end is not None:
                on_end()
            raise
        self._reserved_roles[run.runId] = run.roleId
        self._assistant_message_ids[run.runId] = result[2].id
        self._tokens[run.runId] = CancellationToken()
        if on_end is not None:
            self._reservation_end[run.runId] = on_end
        return result

    def attach_current_task(self, run_id: str) -> bool:
        """Bind cancellation to the task consuming a reserved run's SSE stream."""

        token = self._tokens.get(run_id)
        if token is None or token.is_cancelled():
            return False
        task = asyncio.current_task()
        if task is None:
            return False
        self._tasks[run_id] = task
        return True

    def set_assistant_message(self, run_id: str, message_id: str | None) -> None:
        """Track the current user-visible assistant row for terminal recovery."""

        self._assistant_message_ids[run_id] = message_id

    def cancel_run(self, run_id: str, *, interrupt: bool = True) -> bool:
        token = self._tokens.get(run_id)
        task = self._tasks.get(run_id)
        if token is None and task is None:
            return False
        if token is not None:
            token.cancel()
        current = asyncio.current_task()
        if interrupt and task is not None and task is not current:
            task.cancel()
        return True

    def cancel_role(self, role_id: str) -> bool:
        active = self.sessions.active_run(role_id)
        if active is None:
            run_id = next((key for key, value in self._reserved_roles.items() if value == role_id), None)
            return self.cancel_run(run_id) if run_id is not None else False
        return self.cancel_run(active.runId)

    async def stop_run(self, run_id: str) -> bool:
        """Cancel one owned run and wait for its task and role lock to finish."""

        task = self._tasks.get(run_id)
        run = self.sessions.get_run(run_id)
        if run is not None and run.status not in {"created", "running"}:
            # Persistence precedes capability cleanup and releasing the lock.
            # Preserve a completed answer while waiting for its owner to exit.
            if task is not None and task is not asyncio.current_task():
                await asyncio.gather(task, return_exceptions=True)
            return True
        if not self.cancel_run(run_id):
            return False
        if task is not None and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)
        else:
            self.cancel_unstarted_run(run_id, "主人停止回复")
        return True

    def abandon_unstarted_run(self, run_id: str, reason: str = "SSE 流在启动前中断") -> None:
        """Fail a reserved run when its response iterator never begins."""

        try:
            run = self.sessions.get_run(run_id)
            if run is not None and run.status in {"created", "running"}:
                assistant_id = self._assistant_message_ids.get(run_id)
                assistant_content = ""
                if assistant_id is not None:
                    row = next((item for item in self.sessions.list_messages(run.sessionKey) if item.id == assistant_id), None)
                    if row is not None:
                        assistant_content = row.content
                self._finish_run(
                    run_id,
                    status="failed",
                    assistant_content=assistant_content,
                    turn_count=run.turnCount,
                    error=reason,
                )
        finally:
            self._release_run(run_id)

    def cancel_unstarted_run(self, run_id: str, reason: str = "客户端断开或请求被取消") -> None:
        """Cancel a reservation while pre-run work is still in progress."""

        try:
            run = self.sessions.get_run(run_id)
            if run is not None and run.status in {"created", "running"}:
                self._finish_run(
                    run_id,
                    status="cancelled",
                    assistant_content="",
                    turn_count=run.turnCount,
                    cancel_reason=reason,
                )
        finally:
            self._release_run(run_id)

    async def run(
        self,
        run: AgentRun,
        *,
        provider: ModelProvider,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: Sequence[AgentTool] = (),
        capabilities: CapabilityResolution | None = None,
        max_turns: int | None = None,
        provider_timeout: float | None = None,
        tool_timeout: float | None = None,
        on_message_end: MessageEndHandler | None = None,
        defer_reply: bool = False,
        can_continue: Callable[[], bool] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Execute a loop and finish run/message state through one idempotent path."""

        reserved_role = self._reserved_roles.get(run.runId)
        if reserved_role is None:
            lock = self.lock_for(run.roleId)
            if lock.locked():
                raise ActiveRunError(f"role already has an active run: {run.roleId}")
            await lock.acquire()
            self._reserved_roles[run.runId] = run.roleId
            self._assistant_message_ids.setdefault(run.runId, None)
        elif reserved_role != run.roleId:
            raise ValueError(f"run {run.runId} is reserved for role {reserved_role}")

        token = self._tokens.setdefault(run.runId, CancellationToken())
        task = asyncio.current_task()
        if task is not None:
            self._tasks[run.runId] = task
        turn_count = 0
        assistant_content = ""
        try:
            if self.sessions.get_run(run.runId) is None:
                self.sessions.create_run(run)
            self.sessions.update_run(run.runId, status="running")
            persisted_tool_calls: set[str] = set()

            async def persist_tool_result(message: ToolResultMessage) -> None:
                details = message.details if isinstance(message.details, dict) else {}
                self.sessions.finish_tool_audit(
                    run_id=run.runId, call_id=message.tool_call_id,
                    result_category=str(details.get("status") or ("error" if message.is_error else "succeeded")),
                    error_type=str(details["errorType"]) if details.get("errorType") else None,
                    error_message=str(details["message"]) if details.get("message") else None,
                )
                if on_message_end is not None:
                    persisted = on_message_end(MessageEndEvent(message, run_id=run.runId))
                    if inspect.isawaitable(persisted):
                        await persisted
                else:
                    self.sessions.append_agent_message(run.sessionKey, message, run_id=run.runId)
                persisted_tool_calls.add(message.tool_call_id)

            async with aclosing(run_agent_loop(
                provider=provider,
                model=model,
                system=system,
                messages=messages,
                tools=tools,
                capabilities=capabilities,
                max_turns=max_turns,
                signal=token,
                session_id=run.sessionKey,
                role_id=run.roleId,
                session_key=run.sessionKey,
                run_id=run.runId,
                provider_timeout=provider_timeout,
                tool_timeout=tool_timeout,
                on_tool_result=persist_tool_result,
                **({"can_continue": can_continue} if can_continue is not None else {}),
            )) as loop_stream:
                async for event in loop_stream:
                    if isinstance(event, MessageUpdateEvent) and isinstance(event.message, AssistantMessage):
                        assistant_content = event.message.content
                    if isinstance(event, TurnStartEvent):
                        turn_count = event.turn
                        self.sessions.update_run(run.runId, status="running", turn_count=turn_count)
                    if isinstance(event, ToolExecutionStartEvent):
                        self.sessions.start_tool_audit(
                            run_id=run.runId, role_id=run.roleId, tool_name=event.tool_name,
                            call_id=event.tool_call_id, argument_summary=summarize_tool_arguments(event.arguments),
                            snapshot_id=event.snapshot_id, tool_source=event.tool_source,
                            tool_version=event.tool_version, policy_decision=event.policy_decision,
                            capability_binding=event.capability_binding,
                        )
                    if isinstance(event, ToolExecutionEndEvent) and event.tool_call_id not in persisted_tool_calls:
                        details = event.result.details if isinstance(event.result.details, dict) else {}
                        self.sessions.finish_tool_audit(
                            run_id=run.runId, call_id=event.tool_call_id,
                            result_category=str(details.get("status") or ("error" if event.result.is_error else "succeeded")),
                            error_type=str(details["errorType"]) if details.get("errorType") else None,
                            error_message=str(details["message"]) if details.get("message") else None,
                        )
                    if isinstance(event, MessageEndEvent):
                        if isinstance(event.message, AssistantMessage):
                            assistant_content = event.message.content
                        if isinstance(event.message, ToolResultMessage) and event.message.tool_call_id in persisted_tool_calls:
                            pass
                        elif on_message_end is not None:
                            persisted = on_message_end(event)
                            if inspect.isawaitable(persisted):
                                persisted = await persisted
                            if isinstance(event.message, AssistantMessage) and isinstance(persisted, Message):
                                self.set_assistant_message(run.runId, persisted.id)
                        elif capabilities is not None and capabilities.snapshot.candidates and (
                                isinstance(event.message, ToolResultMessage) or isinstance(event.message, AssistantMessage) and event.message.tool_calls):
                            self.sessions.append_agent_message(run.sessionKey, event.message, run_id=run.runId)
                    if isinstance(event, AgentEndEvent):
                        if event.reason == "completed" and capabilities is not None and capabilities.snapshot.candidates:
                            self.sessions.save_tool_selection(run.runId, capabilities.current().selection_cache())
                        assistant = next(
                            (message for message in reversed(event.messages) if isinstance(message, AssistantMessage)),
                            None,
                        )
                        if assistant is not None:
                            assistant_content = assistant.content
                        self._finish_run(
                            run.runId,
                            status=event.reason,
                            assistant_content="" if defer_reply else assistant_content,
                            turn_count=turn_count,
                            cancel_reason="运行取消" if event.reason == "cancelled" else None,
                            error=event.error or ("Agent 达到 max_turns 上限" if event.reason == "max_turns" else None),
                        )
                    yield event
        except asyncio.CancelledError:
            token.cancel()
            self._finish_run(
                run.runId,
                status="cancelled",
                assistant_content="" if defer_reply else assistant_content,
                turn_count=turn_count,
                cancel_reason="客户端断开",
            )
            raise
        except Exception as error:
            self._finish_run(
                run.runId,
                status="failed",
                assistant_content="" if defer_reply else assistant_content,
                turn_count=turn_count,
                error=str(error),
            )
            raise
        finally:
            if capabilities is not None:
                try:
                    await capabilities.close()
                except Exception:
                    # Cleanup diagnostics must not hide the run's terminal state.
                    pass
            run_state = self.sessions.get_run(run.runId)
            if run_state is not None and run_state.status in {"created", "running"}:
                token.cancel()
                self._finish_run(
                    run.runId,
                    status="cancelled",
                    assistant_content="" if defer_reply else assistant_content,
                    turn_count=turn_count,
                    cancel_reason="Runtime 消费者在运行完成前退出",
                )
            self._release_run(run.runId)

    def _finish_run(
        self,
        run_id: str,
        *,
        status: str,
        assistant_content: str,
        turn_count: int,
        cancel_reason: str | None = None,
        error: str | None = None,
    ) -> Message | None:
        if status not in {"completed", "failed", "cancelled", "max_turns"}:
            raise ValueError(f"invalid terminal status: {status}")
        try:
            _, message = self.sessions.finish_run(
                run_id,
                status=status,  # type: ignore[arg-type]
                assistant_message_id=self._assistant_message_ids.get(run_id),
                assistant_content=assistant_content,
                turn_count=turn_count,
                cancel_reason=cancel_reason,
                error=error,
            )
            self._assistant_message_ids[run_id] = message.id
            self.sessions.finish_open_tool_audits(run_id, result_category=status,
                error_type="RuntimeError" if error else None, error_message=error)
            return message
        except BaseException:
            raise

    def _release_run(self, run_id: str) -> None:
        role_id = self._reserved_roles.pop(run_id, None)
        self._tokens.pop(run_id, None)
        self._tasks.pop(run_id, None)
        self._assistant_message_ids.pop(run_id, None)
        if role_id is not None:
            lock = self.lock_for(role_id)
            if lock.locked():
                lock.release()
        on_end = self._reservation_end.pop(run_id, None)
        if on_end is not None:
            on_end()
