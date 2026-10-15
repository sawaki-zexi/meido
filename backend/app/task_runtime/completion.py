"""Durable internal completions, coordinated only after the parent role turn."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from ..session_store import SessionStore
from .runtime import TaskOrigin, TaskRuntime, TaskSnapshot


class TaskCompletionWorker:
    def __init__(self, tasks: TaskRuntime, sessions: SessionStore, *, lock_for: Callable[[str], asyncio.Lock],
                 authorized: Callable[[TaskOrigin], bool], summarize: Callable[[TaskSnapshot], Awaitable[str]],
                 deliver: Callable[[TaskSnapshot, str], Awaitable[tuple[str, str | None]]]):
        self.tasks, self.sessions, self.lock_for = tasks, sessions, lock_for
        self.authorized, self.summarize, self.deliver = authorized, summarize, deliver
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="owned-task-completions")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        for snapshot in self.tasks.uncertain_completions():
            retained = self.tasks.completion(snapshot.task_id, snapshot.origin)
            message_id = retained.get("message_id") if retained else None
            message = next((item for item in self.sessions.list_messages(snapshot.origin.session_key) if item.id == message_id), None)
            if message is not None and message.metadata.get("deliveryStatus") == "pending":
                self.sessions.update_message(message.id, message.content, "completed", metadata={**message.metadata,
                    "deliveryStatus": "uncertain"})
        while True:
            for snapshot in self.tasks.pending_completions():
                lock = self.lock_for(snapshot.origin.role_id)
                try:
                    await asyncio.wait_for(lock.acquire(), timeout=.05)
                except TimeoutError:
                    continue
                try:
                    await self._complete(snapshot)
                finally:
                    lock.release()
            await asyncio.sleep(.05)

    async def _complete(self, snapshot: TaskSnapshot) -> None:
        task_id, origin = snapshot.task_id, snapshot.origin
        content = ""
        message = None
        if not self.authorized(origin):
            self.tasks.update_completion(task_id, status="failed")
            return
        try:
            retained = self.tasks.completion(task_id, origin)
            content = str(retained["content"]) if retained and retained["content"] else ""
            if not content:
                content = await asyncio.wait_for(self.summarize(snapshot), timeout=20)
                content = f"[内部子任务完成 / {task_id} / {snapshot.status}]\n{content[:8000]}"
                self.tasks.update_completion(task_id, status="pending", content=content)
            if not self.authorized(origin):
                self.tasks.update_completion(task_id, status="failed", content=content)
                return
            message = self.sessions.append_task_completion(origin.role_id, origin.session_key, task_id, content,
                                                           task_status=snapshot.status)
            if not origin.source_account_id:
                self.tasks.update_completion(task_id, status="sent", content=content, message_id=message.id)
                return
            # Persist uncertainty *before* entering an external send. A crash
            # cannot replay it, even between platform receipt and local ack.
            self.tasks.update_completion(task_id, status="sending", content=content, message_id=message.id)
            channel = str(json.loads(origin.source_authority_json).get("channel", ""))
            self.sessions.update_message(message.id, content, "completed", metadata={**message.metadata,
                "channel": channel, "deliveryStatus": "pending"})
            try:
                status, delivery_id = await asyncio.wait_for(self.deliver(snapshot, content), timeout=30)
            except TimeoutError:
                self.tasks.update_completion(task_id, status="uncertain", content=content, message_id=message.id)
                self.sessions.update_message(message.id, content, "completed", metadata={**message.metadata,
                    "channel": channel, "deliveryStatus": "uncertain"})
                return
            except asyncio.CancelledError:
                self.tasks.update_completion(task_id, status="uncertain", content=content, message_id=message.id)
                self.sessions.update_message(message.id, content, "completed", metadata={**message.metadata,
                    "channel": channel, "deliveryStatus": "uncertain"})
                raise
            self.tasks.update_completion(task_id, status=status, content=content, message_id=message.id, delivery_id=delivery_id)
            self.sessions.update_message(message.id, content, "completed", metadata={**message.metadata,
                "channel": channel, "deliveryStatus": status, "deliveryAttemptId": delivery_id})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            retained = self.tasks.completion(task_id, origin)
            status = "uncertain" if retained and retained["status"] == "sending" else "failed"
            self.tasks.update_completion(task_id, status=status, content=content, error_type=type(error).__name__)
            if message is not None and origin.source_account_id:
                self.sessions.update_message(message.id, content, "completed", metadata={**message.metadata,
                    "channel": str(json.loads(origin.source_authority_json).get("channel", "")), "deliveryStatus": status})
