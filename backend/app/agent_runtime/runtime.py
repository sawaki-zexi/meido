from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, TypeAlias

from ..models import AgentRun, Message
from .events import AgentEndEvent, AgentEvent, MessageEndEvent, MessageUpdateEvent, TurnStartEvent
from .capabilities import CapabilityResolution
from .loop import run_agent_loop
from .provider import CancellationToken, ModelProvider
from .tools import AgentTool
from .types import AgentMessage, AssistantMessage

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

    def lock_for(self, role_id: str) -> asyncio.Lock:
        return self.role_locks.setdefault(role_id, asyncio.Lock())

    async def reserve_run(self, run: AgentRun, user_content: str) -> tuple[AgentRun, Message, Message]:
        """Reserve a role and atomically create its run and initial messages."""

        lock = self.lock_for(run.roleId)
        if lock.locked():
            raise ActiveRunError(f"role already has an active run: {run.roleId}")
        await lock.acquire()
        try:
            result = self.sessions.create_run_with_messages(run, user_content)
        except BaseException:
            lock.release()
            raise
        self._reserved_roles[run.runId] = run.roleId
        self._assistant_message_ids[run.runId] = result[2].id
        self._tokens[run.runId] = CancellationToken()
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
            async for event in run_agent_loop(
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
            ):
                if isinstance(event, MessageUpdateEvent) and isinstance(event.message, AssistantMessage):
                    assistant_content = event.message.content
                if isinstance(event, TurnStartEvent):
                    turn_count = event.turn
                    self.sessions.update_run(run.runId, status="running", turn_count=turn_count)
                if isinstance(event, MessageEndEvent):
                    if isinstance(event.message, AssistantMessage):
                        assistant_content = event.message.content
                    if on_message_end is not None:
                        persisted = on_message_end(event)
                        if inspect.isawaitable(persisted):
                            persisted = await persisted
                        if isinstance(event.message, AssistantMessage) and isinstance(persisted, Message):
                            self.set_assistant_message(run.runId, persisted.id)
                if isinstance(event, AgentEndEvent):
                    assistant = next(
                        (message for message in reversed(event.messages) if isinstance(message, AssistantMessage)),
                        None,
                    )
                    if assistant is not None:
                        assistant_content = assistant.content
                    self._finish_run(
                        run.runId,
                        status=event.reason,
                        assistant_content=assistant_content,
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
                assistant_content=assistant_content,
                turn_count=turn_count,
                cancel_reason="客户端断开",
            )
            raise
        except Exception as error:
            self._finish_run(
                run.runId,
                status="failed",
                assistant_content=assistant_content,
                turn_count=turn_count,
                error=str(error),
            )
            raise
        finally:
            run_state = self.sessions.get_run(run.runId)
            if run_state is not None and run_state.status in {"created", "running"}:
                token.cancel()
                self._finish_run(
                    run.runId,
                    status="cancelled",
                    assistant_content=assistant_content,
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
