"""Durable internal completions, coordinated only after the parent role turn."""
from __future__ import annotations

import asyncio
import json
import sqlite3
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
        while True:
            try:
                recorded = self.tasks.recorded_completions()
                pending = self.tasks.pending_completions()
            except sqlite3.OperationalError:
                await asyncio.sleep(.05)
                continue
            for snapshot in recorded:
                try:
                    retained = self.tasks.completion(snapshot.task_id, snapshot.origin)
                    if retained is not None and snapshot.origin.source_account_id:
                        status = str(retained["status"])
                        if status == "sending":
                            status = "uncertain"
                            self.tasks.update_completion(snapshot.task_id, status=status, content=str(retained["content"]))
                        self.sessions.reconcile_task_completion(snapshot.origin.role_id, snapshot.origin.session_key,
                            snapshot.task_id, delivery_status=status, delivery_attempt_id=retained["delivery_id"])
                    self.tasks.acknowledge_completion_message(snapshot.task_id)
                except (sqlite3.OperationalError, KeyError):
                    # Keep the receipt dirty and retry. One unavailable local
                    # row must not permanently stop every other completion.
                    continue
            for snapshot in pending:
                lock = self.lock_for(snapshot.origin.role_id)
                try:
                    await asyncio.wait_for(lock.acquire(), timeout=.05)
                except TimeoutError:
                    continue
                try:
                    try:
                        await self._complete(snapshot)
                    except (sqlite3.OperationalError, KeyError):
                        continue
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
                self.tasks.acknowledge_completion_message(task_id)
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
                self.tasks.acknowledge_completion_message(task_id)
                return
            except asyncio.CancelledError:
                self.tasks.update_completion(task_id, status="uncertain", content=content, message_id=message.id)
                self.sessions.update_message(message.id, content, "completed", metadata={**message.metadata,
                    "channel": channel, "deliveryStatus": "uncertain"})
                self.tasks.acknowledge_completion_message(task_id)
                raise
            self.tasks.update_completion(task_id, status=status, content=content, message_id=message.id, delivery_id=delivery_id)
            self.sessions.update_message(message.id, content, "completed", metadata={**message.metadata,
                "channel": channel, "deliveryStatus": status, "deliveryAttemptId": delivery_id})
            self.tasks.acknowledge_completion_message(task_id)
        except asyncio.CancelledError:
            raise
        except sqlite3.OperationalError:
            # A temporary local lock is not a failed model task or delivery.
            # Pending events stay retryable; an entered send stays uncertain.
            retained = self.tasks.completion(task_id, origin)
            if retained is not None and retained["status"] == "sending":
                self.tasks.update_completion(task_id, status="uncertain", content=content)
        except Exception as error:
            retained = self.tasks.completion(task_id, origin)
            # A local message acknowledgement failure cannot contradict a
            # receipt already committed in the authoritative outbox.
            status = "failed"
            if retained is not None:
                if retained["status"] == "sending":
                    status = "uncertain"
                elif retained["dispatched"] or retained["status"] in {"sent", "failed", "uncertain"}:
                    status = str(retained["status"])
            self.tasks.update_completion(task_id, status=status, content=content, error_type=type(error).__name__)
            if message is not None and origin.source_account_id:
                self.sessions.update_message(message.id, content, "completed", metadata={**message.metadata,
                    "channel": str(json.loads(origin.source_authority_json).get("channel", "")), "deliveryStatus": status,
                    "deliveryAttemptId": retained["delivery_id"] if retained else None})
                self.tasks.acknowledge_completion_message(task_id)
