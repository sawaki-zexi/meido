from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
import uuid
from zoneinfo import ZoneInfo

from ..session_store import SessionStore
from .models import TodoCreate, TodoStatusInput, TodoUpdate
from .store import TodoStore


class TodoApplicationService:
    """Shared owner-scoped rules for web and agent todo operations."""

    def __init__(self, store: TodoStore, sessions: SessionStore) -> None:
        self.store = store
        self.sessions = sessions

    def search(self, query: str, limit: int = 5) -> list[dict[str, object]]:
        return self.store.read_port(query, limit=limit)

    def write(
        self,
        operation: str,
        arguments: Mapping[str, object],
        *,
        role_id: str,
        session_key: str,
        run_id: str,
        tool_call_id: str | None,
    ) -> dict[str, object]:
        if session_key != f"role:{role_id}":
            raise ValueError("待办操作只能作用于当前角色会话所属的主人清单")
        if not tool_call_id:
            raise ValueError("待办操作缺少稳定的工具调用标识")
        source = self._source_message(role_id, session_key, run_id)

        candidate: dict[str, object] = {"operation": operation}
        if operation == "create":
            values = {key: arguments[key] for key in (
                "title", "description", "dueDate", "dueAt", "reminderAt", "reminderDate",
            ) if key in arguments}
            model = TodoCreate.model_validate({**values, "timezone": self.store.settings()["timezone"]})
            candidate.update(model.model_dump(mode="json"))
        elif operation == "update":
            todo_id = arguments.get("todoId")
            target_title = arguments.get("targetTitle")
            if not (isinstance(todo_id, str) and todo_id) and not (isinstance(target_title, str) and target_title.strip()):
                raise ValueError("修改待办需要指定待办 ID 或准确标题")
            values = {key: arguments[key] for key in (
                "title", "description", "dueDate", "dueAt", "reminderAt", "reminderDate",
            ) if key in arguments}
            if not values:
                raise ValueError("修改待办至少需要提供一个新内容")
            model = TodoUpdate.model_validate(values)
            candidate.update(
                **({"todoId": todo_id} if isinstance(todo_id, str) and todo_id else {"targetTitle": target_title}),
                **model.model_dump(mode="json", exclude_unset=True),
            )
        elif operation in {"complete", "cancel", "restore"}:
            todo_id = arguments.get("todoId")
            target_title = arguments.get("targetTitle")
            if isinstance(todo_id, str) and todo_id:
                candidate["todoId"] = todo_id
            elif isinstance(target_title, str) and target_title.strip():
                candidate["targetTitle"] = target_title.strip()
            else:
                raise ValueError(f"{operation} 操作需要指定待办 ID 或准确标题")
        else:
            raise ValueError("不支持的待办操作")

        event_id = f"tool:{run_id}:{tool_call_id}"
        applied = self.store.apply_event(
            event_id,
            role_id,
            session_key,
            source.id,
            [candidate],
            timezone_name=str(self.store.settings()["timezone"]),
        )
        if not applied:
            raise ValueError("待办未修改。请确认目标事项仍可操作，并补充不明确的信息。")
        return applied[0]

    def control_reminders(
        self,
        action: str,
        *,
        role_id: str,
        session_key: str,
        run_id: str,
    ) -> dict[str, object]:
        if session_key != f"role:{role_id}":
            raise ValueError("提醒设置只能由当前角色会话操作")
        self._source_message(role_id, session_key, run_id)

        settings = self.store.settings()
        today = datetime.now(ZoneInfo(str(settings["timezone"]))).date()
        if action in {"join", "leave"}:
            effective_date = today + timedelta(days=1)
            active = action == "join"
            self.store.set_reminder_role(role_id, active, effective_date=effective_date)
            return {
                "roleId": role_id,
                "enabled": active,
                "effectiveDate": effective_date.isoformat(),
            }
        if action in {"pause_today", "resume_today"}:
            paused = action == "pause_today"
            self.store.set_reminders_paused(today if paused else None)
            return {"pausedToday": paused, "date": today.isoformat()}
        raise ValueError("不支持的提醒操作")

    def _source_message(self, role_id: str, session_key: str, run_id: str):
        source = self.sessions.user_message_for_run(run_id)
        if source is None or source.sessionKey != session_key or session_key != f"role:{role_id}":
            raise ValueError("无法确认待办操作的来源消息")
        return source

    def create(self, payload: dict[str, object], *, source: dict[str, str] | None = None) -> dict[str, object]:
        model = TodoCreate.model_validate({**payload, "timezone": payload.get("timezone") or self.store.settings()["timezone"]})
        return self._apply(
            "web:" + uuid.uuid4().hex,
            {"operation": "create", **model.model_dump(mode="json")},
            role_id="",
            session_key="",
            source_message_id=(source or {}).get("messageId", "web"),
        )

    def update(self, todo_id: str, payload: dict[str, object]) -> dict[str, object]:
        model = TodoUpdate.model_validate(payload)
        values = model.model_dump(mode="json", exclude_unset=True)
        if not values:
            raise ValueError("修改待办至少需要提供一个新内容")
        return self._apply(
            "web:" + uuid.uuid4().hex,
            {"operation": "update", "todoId": todo_id, **values},
            role_id="",
            session_key="",
            source_message_id="web",
        )

    def set_status(self, todo_id: str, status: str) -> dict[str, object]:
        operation = {"completed": "complete", "cancelled": "cancel"}.get(status, "restore" if status in {"inbox", "scheduled"} else "")
        if not operation:
            raise ValueError("不支持的待办状态")
        TodoStatusInput.model_validate({"status": status})
        candidate: dict[str, object] = {"operation": operation, "todoId": todo_id}
        if operation == "restore":
            candidate["restoreStatus"] = status
        return self._apply(
            "web:" + uuid.uuid4().hex,
            candidate,
            role_id="",
            session_key="",
            source_message_id="web",
        )

    def _apply(self, event_id: str, candidate: dict[str, object], *, role_id: str, session_key: str, source_message_id: str) -> dict[str, object]:
        applied = self.store.apply_event(
            event_id,
            role_id,
            session_key,
            source_message_id,
            [candidate],
            timezone_name=str(self.store.settings()["timezone"]),
        )
        if not applied:
            raise ValueError("待办未修改。请确认目标事项仍可操作，并补充不明确的信息。")
        return applied[0]
