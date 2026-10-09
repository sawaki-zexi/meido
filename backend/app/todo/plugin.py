from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterable
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from ..session_store import SessionStore
from .store import TodoStore


ReminderSender = Callable[[str, str, str, list[dict[str, object]]], str]
ReminderRoleProvider = Callable[[], Iterable[str]]


class TodoPlugin:
    """Owns the todo reminder schedule; writes arrive through the application service."""

    POLL_SECONDS = 2
    DAILY_SUMMARY_TIME = time(4, 0)

    def __init__(
        self,
        store: TodoStore,
        sessions: SessionStore,
        *,
        reminder_sender: ReminderSender | None = None,
        available_roles: ReminderRoleProvider | None = None,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.reminder_sender = reminder_sender
        self.available_roles = available_roles or (lambda: ())
        self.errors: list[str] = []
        self._task: asyncio.Task[None] | None = None
        self._processing = threading.Lock()

    async def start(self) -> None:
        if self.store.settings()["enabled"] and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run(), name="todo-plugin")

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(self._processing.acquire)
        self._processing.release()

    async def configure(
        self,
        *,
        enabled: bool | None = None,
        reminders_enabled: bool | None = None,
        timezone_name: str | None = None,
    ) -> dict[str, object]:
        if enabled is False:
            await asyncio.to_thread(self._processing.acquire)
            try:
                settings = self.store.update_settings(
                    enabled=False,
                    reminders_enabled=reminders_enabled,
                    timezone_name=timezone_name,
                )
            finally:
                self._processing.release()
            await self.close()
        else:
            settings = self.store.update_settings(
                enabled=enabled,
                reminders_enabled=reminders_enabled,
                timezone_name=timezone_name,
            )
        if enabled is True:
            await self.start()
        return settings

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.schedule_reminders_once)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._record_error(f"todo worker: {type(error).__name__}: {error}")
            await asyncio.sleep(self.POLL_SECONDS)

    def schedule_reminders_once(self, now: datetime | None = None) -> list[dict[str, object]]:
        settings = self.store.settings()
        if not settings["enabled"] or not settings["remindersEnabled"]:
            return []
        self._processing.acquire()
        try:
            settings = self.store.settings()
            if not settings["enabled"] or not settings["remindersEnabled"]:
                return []
            moment = now or datetime.now(timezone.utc)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            moment = moment.astimezone(timezone.utc)
            zone = ZoneInfo(str(settings["timezone"]))
            local_now = moment.astimezone(zone)
            paused_today = self.store.reminders_paused(local_now.date())
            available = list(dict.fromkeys(str(role_id) for role_id in self.available_roles() if role_id))

            self.store.cancel_completed_reminders()
            self.store.schedule_due_reminders(moment, skip_dates={local_now.date()} if paused_today else set())

            if local_now.time() >= self.DAILY_SUMMARY_TIME and not paused_today:
                role_id = self.store.ensure_reminder_day(local_now.date(), available)
                if role_id:
                    self.store.schedule_daily_summary(local_now.date(), role_id, moment, refresh_pending=True)

            # Old or paused work must not fire after the pause window expires.
            self.store.discard_pending_before(local_now.date())

            delivered: list[dict[str, object]] = []
            for summary in self.store.pending_daily_summaries(now=moment):
                local_date = date.fromisoformat(str(summary["date"]))
                if local_date < local_now.date() or self.store.reminders_paused(local_date):
                    continue
                role_id = self.store.ensure_reminder_day(local_date, available)
                if not role_id:
                    self.store.finish_daily_summary(local_date.isoformat(), error="当天没有可用的提醒角色")
                    continue
                payload = self.store.daily_summary_payload(local_date)
                if not payload:
                    self.store.finish_daily_summary(local_date.isoformat())
                    continue
                delivered_item = self._deliver(
                    role_id,
                    f"daily:{local_date.isoformat()}",
                    "daily",
                    payload,
                    lambda error, key=local_date.isoformat(): self.store.finish_daily_summary(key, error=error, retry_base=moment),
                )
                if delivered_item is not None:
                    delivered.append({"eventId": f"daily:{local_date.isoformat()}", "roleId": role_id, "kind": "daily"})

            for reminder in self.store.pending_role_reminders(now=moment):
                local_due = datetime.fromisoformat(str(reminder["dueAt"])).astimezone(zone)
                if local_due.date() < local_now.date() or self.store.reminders_paused(local_due.date()):
                    continue
                role_id = self.store.ensure_reminder_day(local_due.date(), available)
                if not role_id:
                    self.store.finish_role_reminder(str(reminder["id"]), error="当天没有可用的提醒角色")
                    continue
                self.store.assign_reminder_role(str(reminder["id"]), role_id)
                payload = [
                    {key: reminder.get(key) for key in ("todoId", "title", "description", "dueDate", "todoDueAt", "dueAt", "timezone")}
                ]
                delivered_item = self._deliver(
                    role_id,
                    f"exact:{reminder['dueKey']}",
                    "exact",
                    payload,
                    lambda error, key=str(reminder["id"]): self.store.finish_role_reminder(key, error=error, retry_base=moment),
                )
                if delivered_item is not None:
                    delivered.append({"eventId": f"exact:{reminder['dueKey']}", "roleId": role_id, "kind": "exact"})
            return delivered
        finally:
            self._processing.release()

    def _deliver(
        self,
        role_id: str,
        event_id: str,
        kind: str,
        payload: list[dict[str, object]],
        finish: Callable[[str | None], None],
    ) -> dict[str, object] | None:
        if self.reminder_sender is None:
            finish("角色提醒生成器尚未配置")
            self._record_error(f"{event_id}: role reminder sender is not configured")
            return None
        try:
            content = self.reminder_sender(role_id, event_id, kind, payload).strip()
            if not content:
                raise ValueError("角色提醒内容为空")
            message = self.sessions.append_proactive_message_once(
                f"role:{role_id}",
                content,
                event_id=event_id,
                metadata={"proactiveType": "todo_reminder", "kind": kind},
            )
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
            finish(failure)
            self._record_error(f"{event_id}: {failure}")
            return None
        finish(None)
        return {"messageId": message.id, "roleId": role_id, "kind": kind}

    def context_block(self, query: str) -> str:
        if not self.store.settings()["enabled"]:
            return ""
        items = self.store.read_port(query, limit=5)
        if not items:
            return ""
        lines = ["[当前待办信息]"]
        for item in items:
            due = item.get("dueDate") or item.get("dueAt") or "未安排日期"
            lines.append(f"- {item['title']}（{item['status']}；{due}）")
        lines.append("这些是主人明确记录的待办；只在当前问题相关时使用，不要当作角色记忆或既成事实。")
        return "\n".join(lines)

    def diagnostics(self) -> list[dict[str, object]]:
        return self.store.operation_diagnostics() + self.store.reminder_diagnostics()

    def _record_error(self, message: str) -> None:
        self.errors.append(message)
        del self.errors[:-100]
