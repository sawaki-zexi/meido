from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..session_store import SessionStore
from ..todo_intent import explicit_todo_cue, explicit_todo_operation
from .models import TodoCreate, TodoUpdate
from .store import TodoStore


TodoExtractor = Callable[[dict[str, object], list[dict[str, object]]], list[dict[str, object]]]


class TodoPlugin:
    """Application lifecycle for turn ingestion and reminder scheduling."""

    POLL_SECONDS = 2

    def __init__(self, store: TodoStore, sessions: SessionStore, extractor: TodoExtractor | None = None) -> None:
        self.store = store
        self.sessions = sessions
        self.extractor = extractor or self.extract_explicit_intent
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
                await asyncio.to_thread(self.process_pending_once)
                await asyncio.to_thread(self.schedule_reminders_once)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.errors.append(f"todo worker: {type(error).__name__}: {error}")
                del self.errors[:-100]
            await asyncio.sleep(self.POLL_SECONDS)

    def process_pending_once(self, *, role_id: str | None = None) -> int:
        if not self.store.settings()["enabled"]:
            return 0
        self._processing.acquire()
        try:
            if not self.store.settings()["enabled"]:
                return 0
            return self._process_pending(role_id=role_id)
        finally:
            self._processing.release()

    def schedule_reminders_once(self) -> list[dict[str, object]]:
        settings = self.store.settings()
        if not settings["enabled"] or not settings["remindersEnabled"]:
            return []
        self._processing.acquire()
        try:
            settings = self.store.settings()
            if not settings["enabled"] or not settings["remindersEnabled"]:
                return []
            self.store.schedule_due_reminders()
            return self.store.deliver_pending_reminders()
        finally:
            self._processing.release()

    def _process_pending(self, *, role_id: str | None = None) -> int:
        completed = 0
        for event in self.sessions.list_todo_outbox(role_id=role_id):
            event_id = str(event["eventId"])
            try:
                if not self.store.settings()["enabled"]:
                    break
                active = self.store.list()
                enriched_event = {**event, "timezone": self.store.settings()["timezone"]}
                if explicit_todo_cue(str(event.get("userContent", ""))) is None:
                    candidates = []
                else:
                    candidates = self.extractor(enriched_event, active)
                if not isinstance(candidates, list):
                    raise ValueError("待办抽取结果必须是数组")
                candidates = self._validate_candidates(enriched_event, active, candidates)
                if not self.store.settings()["enabled"]:
                    break
                self.store.apply_event(
                    event_id,
                    str(event["roleId"]),
                    str(event["sessionKey"]),
                    str(event["userMessageId"]),
                    candidates,
                    timezone_name=str(self.store.settings()["timezone"]),
                )
                self.sessions.ack_todo_outbox(event_id)
                completed += 1
            except Exception as error:
                self.sessions.fail_todo_outbox(event_id, f"{type(error).__name__}: {error}")
                self.errors.append(f"{event_id}: {type(error).__name__}: {error}")
                del self.errors[:-100]
        return completed

    @staticmethod
    def _validate_candidates(
        event: dict[str, object], active: list[dict[str, object]], candidates: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        message = str(event.get("userContent", ""))
        expected_operation = explicit_todo_operation(message)
        active_by_id = {str(item["id"]): item for item in active}
        allowed = {"operation", "todoId", "title", "description", "dueDate", "dueAt", "reminderAt", "reminderDate", "timezone"}
        validated: list[dict[str, object]] = []
        for candidate in candidates[:8]:
            if not isinstance(candidate, dict) or set(candidate) - allowed:
                raise ValueError("待办操作包含未知字段")
            candidate = {key: value for key, value in candidate.items() if value is not None}
            operation = candidate.get("operation")
            if operation not in {"create", "update", "complete", "cancel", "restore"}:
                raise ValueError("待办操作类型无效")
            if expected_operation is None or operation != expected_operation:
                continue
            if operation == "create":
                create_values = {
                    key: candidate[key] for key in (
                        "title", "description", "dueDate", "dueAt", "reminderAt", "reminderDate", "timezone",
                    ) if key in candidate
                }
                TodoPlugin._remove_unsubstantiated_dates(create_values, message, event)
                TodoPlugin._remove_unquoted_description(create_values, message)
                model = TodoCreate.model_validate(create_values)
                message_bigrams = {
                    message[index:index + 2].casefold()
                    for index in range(max(0, len(message) - 1))
                    if message[index].isalnum() and message[index + 1].isalnum()
                }
                title = model.title.casefold()
                title_bigrams = [title[index:index + 2] for index in range(len(title) - 1)]
                overlap = sum(term in message_bigrams for term in title_bigrams)
                if title_bigrams and overlap / len(title_bigrams) < 0.5:
                    continue
                if len(title) == 1 and title not in message.casefold():
                    continue
                validated.append({
                    "operation": "create", **model.model_dump(mode="json"),
                    "timezone": str(event.get("timezone") or "Asia/Shanghai"),
                })
                continue

            todo_id = candidate.get("todoId")
            target = active_by_id.get(str(todo_id)) if todo_id is not None else None
            allowed_target_statuses = {"restore": {"completed", "cancelled"}}.get(operation, {"inbox", "scheduled"})
            if target is not None and target.get("status") not in allowed_target_statuses:
                target = None
            if operation == "update":
                patch_fields = {key: candidate[key] for key in (
                    "title", "description", "dueDate", "dueAt", "reminderAt", "reminderDate", "timezone",
                ) if key in candidate}
                TodoPlugin._remove_unsubstantiated_dates(patch_fields, message, event, prefer_replacement_date=True)
                TodoPlugin._remove_unquoted_description(patch_fields, message)
                title_value = patch_fields.get("title")
                if title_value is not None and str(title_value).casefold() not in message.casefold():
                    patch_fields.pop("title", None)
                if not patch_fields:
                    clean = {"operation": "update"}
                else:
                    patch = TodoUpdate.model_validate(patch_fields)
                    clean = {"operation": "update", **patch.model_dump(mode="json", exclude_unset=True)}
            else:
                if set(candidate) - {"operation", "todoId"}:
                    raise ValueError(f"{operation} 操作包含无关字段")
                clean = {"operation": operation}

            # The model may resolve a target only when the user's wording identifies it.
            title_matches = [
                item for item in active
                if str(item["title"]).strip().casefold() in message.casefold()
            ]
            unique_match = title_matches[0] if len(title_matches) == 1 else None
            if unique_match is not None and unique_match.get("status") not in allowed_target_statuses:
                unique_match = None
            if target is not None:
                if unique_match is not None and unique_match["id"] != target["id"]:
                    target = None
                elif not title_matches and len(active) > 1:
                    target = None
            if target is None:
                clean["todoId"] = None
            else:
                clean["todoId"] = str(target["id"])
            validated.append(clean)
        return validated

    @staticmethod
    def _remove_unsubstantiated_dates(
        candidate: dict[str, object], message: str, event: dict[str, object], *, prefer_replacement_date: bool = False,
    ) -> None:
        created = datetime.fromisoformat(str(event.get("userCreatedAt") or datetime.now().astimezone().isoformat()))
        timezone_name = str(event.get("timezone") or "Asia/Shanghai")
        zone = ZoneInfo(timezone_name)
        target_date = TodoPlugin._intent_date(message, created.astimezone(zone).date(), prefer_replacement_date=prefer_replacement_date)
        time_match = re.search(r"(?<!\d)(\d{1,2})\s*(?:点|时)(?:\s*(\d{1,2})\s*分?|半|一刻)?", message)
        if time_match:
            hour, minute = int(time_match.group(1)), int(time_match.group(2) or 0)
            if "半" in time_match.group(0):
                minute = 30
            elif "一刻" in time_match.group(0):
                minute = 15
            if "下午" in message or "晚上" in message or ("中午" in message and hour < 11):
                hour = hour + 12 if hour < 12 else hour
            elif "上午" in message and hour == 12:
                hour = 0
        else:
            hour, minute = -1, -1

        for key in ("dueDate", "reminderDate"):
            value = candidate.get(key)
            if value is not None:
                parsed = date.fromisoformat(str(value))
                if target_date is None or parsed != target_date:
                    candidate.pop(key, None)

        for key in ("dueAt", "reminderAt"):
            value = candidate.get(key)
            if value is None:
                continue
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=zone)
            local = parsed.astimezone(zone)
            explicit_reminder = key != "reminderAt" or "提醒" in message
            if target_date is None or local.date() != target_date or hour < 0 or (local.hour, local.minute) != (hour, minute) or not explicit_reminder:
                candidate.pop(key, None)

    @staticmethod
    def _remove_unquoted_description(candidate: dict[str, object], message: str) -> None:
        description = candidate.get("description")
        if description is not None and str(description).strip() not in message:
            candidate.pop("description", None)

    @staticmethod
    def _intent_date(message: str, today: date, *, prefer_replacement_date: bool = False) -> date | None:
        if prefer_replacement_date:
            replacement = re.search(r"(?:改到|改成|延期到|推迟到|提前到|改期到|修改为|更新为)(.{0,50})", message)
            if replacement:
                target = TodoPlugin._extract_date(replacement.group(1), today)
                if target is not None:
                    return target
        return TodoPlugin._extract_date(message, today)

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
        return [
            {
                "eventId": row["eventId"], "roleId": row["roleId"], "attempts": row["attempts"],
                "error": row["lastError"], "status": "retrying" if row["lastError"] else "pending",
            }
            for row in self.sessions.list_todo_outbox(include_deferred=True)
        ] + self.store.operation_diagnostics()

    @staticmethod
    def extract_explicit_intent(event: dict[str, object], active: list[dict[str, object]]) -> list[dict[str, object]]:
        text = str(event.get("userContent", "")).strip()
        cue = explicit_todo_cue(text)
        if cue is None:
            return []
        operation = explicit_todo_operation(text)
        if operation in {"complete", "cancel", "restore", "update"}:
            matching = [item for item in active if str(item.get("title", "")).casefold() in text.casefold()]
            if not matching and len(active) == 1 and re.search(r"(?:这个|它|该待办|这件事)", text):
                matching = [active[0]]
            target = matching[0]["id"] if len(matching) == 1 else None
            candidate: dict[str, object] = {"operation": operation, "todoId": target}
            if operation == "update":
                created = datetime.fromisoformat(str(event.get("userCreatedAt") or datetime.now().astimezone().isoformat()))
                zone = ZoneInfo(str(event.get("timezone") or "Asia/Shanghai"))
                target_date = TodoPlugin._intent_date(text, created.astimezone(zone).date(), prefer_replacement_date=True)
                if target_date is not None:
                    candidate["dueDate"] = target_date.isoformat()
                    if "提醒" in text:
                        candidate["reminderDate"] = target_date.isoformat()
            return [candidate]
        content = text.split(cue, 1)[1].strip(" ：:，,。！？!? ") if cue in text else text
        if not content:
            return []
        created = datetime.fromisoformat(str(event.get("userCreatedAt") or datetime.now().astimezone().isoformat()))
        timezone_name = str(event.get("timezone") or "Asia/Shanghai")
        zone = ZoneInfo(timezone_name)
        local_now = created.astimezone(zone)
        target_date = TodoPlugin._extract_date(content, local_now.date())
        time_match = re.search(r"(?<!\d)(\d{1,2})\s*(?:点|时)(?:\s*(\d{1,2})\s*分?)?", content)
        exact_reminder = None
        if target_date is not None and time_match and "提醒" in text:
            hour, minute = int(time_match.group(1)), int(time_match.group(2) or 0)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                exact_reminder = datetime.combine(target_date, time(hour, minute), zone).isoformat()
        title = content
        for pattern in (
            r"\d{4}[-年/]\d{1,2}[-月/]\d{1,2}日?",
            r"\d{1,2}月\d{1,2}日?",
            r"(?:今天|明天|后天|大后天|下周[一二三四五六日天]|周[一二三四五六日天]|星期[一二三四五六日天])",
            r"\d{1,2}\s*(?:点|时)(?:\s*\d{1,2}\s*分?)?",
        ):
            title = re.sub(pattern, "", title)
        title = re.sub(r"^(?:要|去|在|于|帮我|把)\s*", "", title).strip(" ：:，,。！？!? ")
        title = re.sub(r"^(?:交|提交|完成|处理|买|预约|联系|续费)(?=\S)", lambda match: match.group(0), title)
        if not title:
            return []
        candidate: dict[str, object] = {
            "operation": "create",
            "title": title[:200],
            "timezone": timezone_name,
        }
        if target_date is not None:
            candidate["dueDate"] = target_date.isoformat()
            if "提醒" in text:
                if exact_reminder is not None:
                    candidate["reminderAt"] = exact_reminder
                else:
                    candidate["reminderDate"] = target_date.isoformat()
        return [candidate]

    @staticmethod
    def _extract_date(text: str, today: date) -> date | None:
        for word, delta in (("今天", 0), ("明天", 1), ("后天", 2), ("大后天", 3)):
            if word in text:
                return today + timedelta(days=delta)
        weekday = re.search(r"(?:下周|周|星期)([一二三四五六日天])", text)
        if weekday:
            target = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}[weekday.group(1)]
            current = today.weekday()
            delta = (target - current) % 7
            if delta == 0 or "下周" in weekday.group(0):
                delta += 7
            return today + timedelta(days=delta)
        absolute = re.search(r"(?:(\d{4})[-年/])?(\d{1,2})[-月/](\d{1,2})日?", text)
        if absolute:
            year = int(absolute.group(1) or today.year)
            try:
                return date(year, int(absolute.group(2)), int(absolute.group(3)))
            except ValueError:
                return None
        month_day = re.search(r"(\d{1,2})月(\d{1,2})日?", text)
        if month_day:
            try:
                return date(today.year, int(month_day.group(1)), int(month_day.group(2)))
            except ValueError:
                return None
        return None
