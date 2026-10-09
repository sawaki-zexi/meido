from __future__ import annotations

import json
import random
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_datetime(value: object, timezone_name: str) -> str | None:
    if value is None or value == "":
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(timezone.utc).isoformat()


def _iso_date(value: object) -> str | None:
    if value is None or value == "":
        return None
    return value.isoformat() if isinstance(value, date) else date.fromisoformat(str(value)).isoformat()


def _retry_at(attempts: int, *, base: datetime | None = None) -> str:
    """Return a bounded retry time so a failing model cannot spin the worker."""

    delay_seconds = min(3600, 60 * (2 ** max(0, min(attempts, 6))))
    moment = base or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment.astimezone(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()


class TodoStore:
    """SQLite authority for owner-scoped todos and their reminder inbox."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS todos (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    due_date TEXT,
                    due_at TEXT,
                    reminder_at TEXT,
                    reminder_date TEXT,
                    timezone TEXT NOT NULL,
                    source_role_id TEXT,
                    source_session_key TEXT,
                    source_message_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    cancelled_at TEXT
                );
                CREATE INDEX IF NOT EXISTS todos_status_due ON todos(status, due_date, due_at);
                CREATE INDEX IF NOT EXISTS todos_source_role ON todos(source_role_id);
                CREATE TABLE IF NOT EXISTS todo_events (
                    event_id TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS todo_operations (
                    id TEXT PRIMARY KEY,
                    todo_id TEXT,
                    event_id TEXT NOT NULL,
                    source_message_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    result TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(event_id, id)
                );
                CREATE TABLE IF NOT EXISTS todo_reminders (
                    id TEXT PRIMARY KEY,
                    todo_id TEXT NOT NULL,
                    due_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    role_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    delivered_at TEXT,
                    read_at TEXT,
                    next_attempt_at TEXT
                );
                CREATE INDEX IF NOT EXISTS todo_reminders_inbox ON todo_reminders(status, read_at, due_at);
                CREATE TABLE IF NOT EXISTS todo_reminder_inbox (
                    reminder_id TEXT PRIMARY KEY,
                    read_at TEXT
                );
                CREATE TABLE IF NOT EXISTS todo_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS todo_unavailable_sources (
                    role_id TEXT PRIMARY KEY,
                    deleted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS todo_reminder_roles (
                    role_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL,
                    effective_date TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS todo_reminder_role_changes (
                    role_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    effective_date TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(role_id, effective_date)
                );
                CREATE TABLE IF NOT EXISTS todo_reminder_days (
                    local_date TEXT PRIMARY KEY,
                    role_id TEXT,
                    digest_status TEXT NOT NULL DEFAULT 'not_due',
                    digest_payload_json TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    delivered_at TEXT,
                    next_attempt_at TEXT
                );
            """)
            reminder_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(todo_reminders)")}
            if "role_id" not in reminder_columns:
                connection.execute("ALTER TABLE todo_reminders ADD COLUMN role_id TEXT")
            if "next_attempt_at" not in reminder_columns:
                connection.execute("ALTER TABLE todo_reminders ADD COLUMN next_attempt_at TEXT")
            day_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(todo_reminder_days)")}
            if "next_attempt_at" not in day_columns:
                connection.execute("ALTER TABLE todo_reminder_days ADD COLUMN next_attempt_at TEXT")
            connection.execute(
                "INSERT OR IGNORE INTO todo_reminder_role_changes(role_id,enabled,effective_date,updated_at) "
                "SELECT role_id,enabled,effective_date,updated_at FROM todo_reminder_roles"
            )
            connection.execute(
                "INSERT OR IGNORE INTO todo_reminder_inbox(reminder_id,read_at) "
                "SELECT id,read_at FROM todo_reminders WHERE status='delivered'"
            )

    def settings(self) -> dict[str, object]:
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute("SELECT key, value FROM todo_settings").fetchall()
        values = {str(key): str(value) for key, value in rows}
        return {
            "enabled": values.get("enabled", "true") == "true",
            "remindersEnabled": values.get("remindersEnabled", "true") == "true",
            "timezone": values.get("timezone", "Asia/Shanghai"),
            "timezoneConfigured": "timezone" in values,
        }

    def update_settings(
        self,
        *,
        enabled: bool | None = None,
        reminders_enabled: bool | None = None,
        timezone_name: str | None = None,
    ) -> dict[str, object]:
        with sqlite3.connect(self.database_path) as connection:
            if enabled is not None:
                connection.execute(
                    "INSERT INTO todo_settings(key, value) VALUES('enabled', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("true" if enabled else "false",),
                )
            if reminders_enabled is not None:
                connection.execute(
                    "INSERT INTO todo_settings(key, value) VALUES('remindersEnabled', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("true" if reminders_enabled else "false",),
                )
            if timezone_name is not None:
                connection.execute(
                    "INSERT INTO todo_settings(key, value) VALUES('timezone', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (timezone_name,),
                )
        return self.settings()

    def set_reminder_role(self, role_id: str, enabled: bool, *, effective_date: date) -> None:
        now = _now()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "DELETE FROM todo_reminder_role_changes WHERE role_id=? AND effective_date>=?",
                (role_id, effective_date.isoformat()),
            )
            connection.execute(
                "INSERT INTO todo_reminder_role_changes(role_id,enabled,effective_date,updated_at) VALUES(?,?,?,?)",
                (role_id, int(enabled), effective_date.isoformat(), now),
            )

    def reminder_role_ids(self, *, on_date: date, available_role_ids: Iterable[str] | None = None) -> list[str]:
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT role_id,enabled FROM todo_reminder_role_changes c "
                "WHERE effective_date<=? AND NOT EXISTS("
                "SELECT 1 FROM todo_reminder_role_changes newer "
                "WHERE newer.role_id=c.role_id AND newer.effective_date<=? "
                "AND (newer.effective_date>c.effective_date OR (newer.effective_date=c.effective_date AND newer.updated_at>c.updated_at))"
                ") ORDER BY role_id",
                (on_date.isoformat(), on_date.isoformat()),
            ).fetchall()
        available = set(available_role_ids) if available_role_ids is not None else None
        return [str(role_id) for role_id, enabled in rows if bool(enabled) and (available is None or str(role_id) in available)]

    def reminder_role_settings(self, role_ids: Iterable[str], *, on_date: date) -> list[dict[str, object]]:
        identifiers = list(dict.fromkeys(str(role_id) for role_id in role_ids))
        if not identifiers:
            return []
        placeholders = ",".join("?" for _ in identifiers)
        with sqlite3.connect(self.database_path) as connection:
            current_rows = connection.execute(
                f"SELECT role_id,enabled,effective_date FROM todo_reminder_role_changes c "
                f"WHERE role_id IN ({placeholders}) AND effective_date<=? "
                "AND NOT EXISTS(SELECT 1 FROM todo_reminder_role_changes newer "
                "WHERE newer.role_id=c.role_id AND newer.effective_date<=? "
                "AND (newer.effective_date>c.effective_date OR (newer.effective_date=c.effective_date AND newer.updated_at>c.updated_at))) "
                "ORDER BY role_id",
                (*identifiers, on_date.isoformat(), on_date.isoformat()),
            ).fetchall()
            next_rows = connection.execute(
                f"SELECT role_id,enabled,effective_date FROM todo_reminder_role_changes c "
                f"WHERE role_id IN ({placeholders}) AND effective_date>? "
                "AND NOT EXISTS(SELECT 1 FROM todo_reminder_role_changes newer "
                "WHERE newer.role_id=c.role_id AND newer.effective_date<=? "
                "AND (newer.effective_date>c.effective_date OR (newer.effective_date=c.effective_date AND newer.updated_at>c.updated_at))) "
                "ORDER BY role_id,effective_date,updated_at",
                (*identifiers, on_date.isoformat(), on_date.isoformat()),
            ).fetchall()
        current = {str(role_id): (bool(enabled), date.fromisoformat(str(effective_date))) for role_id, enabled, effective_date in current_rows}
        upcoming: dict[str, tuple[bool, date]] = {}
        for role_id, enabled, effective_date in next_rows:
            identifier = str(role_id)
            candidate = (bool(enabled), date.fromisoformat(str(effective_date)))
            current_candidate = upcoming.get(identifier)
            if current_candidate is None or candidate[1] < current_candidate[1]:
                upcoming[identifier] = candidate
        result = []
        for role_id in identifiers:
            active_today, current_effective = current.get(role_id, (False, on_date))
            future = upcoming.get(role_id)
            enabled, effective = future if future is not None else (active_today, current_effective)
            result.append({
                "roleId": role_id,
                "enabled": enabled,
                "activeToday": active_today,
                "effectiveDate": effective.isoformat(),
            })
        return result

    def set_reminders_paused(self, paused_date: date | None) -> None:
        with sqlite3.connect(self.database_path) as connection:
            if paused_date is None:
                connection.execute("DELETE FROM todo_settings WHERE key='remindersPausedDate'")
            else:
                connection.execute(
                    "INSERT INTO todo_settings(key,value) VALUES('remindersPausedDate',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (paused_date.isoformat(),),
                )
                self._skip_pending_in(connection, paused_date)

    def _skip_pending_in(self, connection: sqlite3.Connection, local_date: date) -> None:
        zone = ZoneInfo(str(self.settings()["timezone"]))
        start = datetime.combine(local_date, datetime.min.time(), zone).astimezone(timezone.utc).isoformat()
        end = datetime.combine(local_date + timedelta(days=1), datetime.min.time(), zone).astimezone(timezone.utc).isoformat()
        now = _now()
        connection.execute(
            "UPDATE todo_reminders SET status='skipped',last_error='当天提醒已暂停',next_attempt_at=NULL,updated_at=? "
            "WHERE status IN ('pending','failed') AND due_at>=? AND due_at<?",
            (now, start, end),
        )
        connection.execute(
            "UPDATE todo_reminder_days SET digest_status='skipped',last_error='当天提醒已暂停',next_attempt_at=NULL,updated_at=? "
            "WHERE local_date=? AND digest_status IN ('not_due','pending','failed')",
            (now, local_date.isoformat()),
        )
        connection.execute(
            "INSERT OR IGNORE INTO todo_reminder_days(local_date,role_id,digest_status,digest_payload_json,last_error,created_at,updated_at) "
            "VALUES(?,NULL,'skipped','[]','当天提醒已暂停',?,?)",
            (local_date.isoformat(), now, now),
        )

    def reminders_paused(self, on_date: date) -> bool:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute("SELECT value FROM todo_settings WHERE key='remindersPausedDate'").fetchone()
        return row is not None and str(row[0]) == on_date.isoformat()

    def ensure_reminder_day(self, local_date: date, available_role_ids: Iterable[str]) -> str | None:
        available = self.reminder_role_ids(on_date=local_date, available_role_ids=available_role_ids)
        now = _now()
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT role_id,digest_status FROM todo_reminder_days WHERE local_date=?",
                (local_date.isoformat(),),
            ).fetchone()
            if row is not None and row[0] is None:
                return None
            if row is not None and row[0] in available:
                return str(row[0])
            role_id = random.choice(available) if available else None
            digest_status = "skipped" if role_id is None else "not_due"
            connection.execute(
                "INSERT INTO todo_reminder_days(local_date,role_id,digest_status,created_at,updated_at,last_error) "
                "VALUES(?,?,?, ?, ?, ?) ON CONFLICT(local_date) DO UPDATE SET role_id=excluded.role_id,"
                "digest_status=CASE WHEN excluded.role_id IS NULL AND todo_reminder_days.digest_status IN "
                "('not_due','pending','failed') THEN 'skipped' ELSE todo_reminder_days.digest_status END,"
                "last_error=CASE WHEN excluded.role_id IS NULL AND todo_reminder_days.digest_status IN "
                "('not_due','pending','failed') THEN excluded.last_error ELSE todo_reminder_days.last_error END,"
                "next_attempt_at=CASE WHEN excluded.role_id IS NULL THEN NULL ELSE todo_reminder_days.next_attempt_at END,"
                "updated_at=excluded.updated_at",
                (local_date.isoformat(), role_id, digest_status, now, now, "当天没有可用的提醒角色" if role_id is None else None),
            )
            zone = ZoneInfo(str(self.settings()["timezone"]))
            start = datetime.combine(local_date, datetime.min.time(), zone).astimezone(timezone.utc).isoformat()
            end = datetime.combine(local_date + timedelta(days=1), datetime.min.time(), zone).astimezone(timezone.utc).isoformat()
            connection.execute(
                "UPDATE todo_reminders SET role_id=? WHERE status IN ('pending','failed') AND due_at>=? AND due_at<?",
                (role_id, start, end),
            )
        return role_id

    def reminder_day(self, local_date: date) -> dict[str, object] | None:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT local_date,role_id,digest_status,digest_payload_json,attempts,last_error,created_at,updated_at,delivered_at "
                "FROM todo_reminder_days WHERE local_date=?",
                (local_date.isoformat(),),
            ).fetchone()
        if row is None:
            return None
        return {
            "date": str(row[0]), "roleId": row[1], "digestStatus": str(row[2]),
            "digestPayload": json.loads(str(row[3])) if row[3] else None,
            "attempts": int(row[4]), "lastError": row[5], "createdAt": str(row[6]),
            "updatedAt": str(row[7]), "deliveredAt": row[8],
        }

    def todos_for_daily_summary(self, local_date: date) -> list[dict[str, object]]:
        active = self.list(statuses=("inbox", "scheduled"))
        owner_zone = ZoneInfo(str(self.settings()["timezone"]))
        result = []
        for item in active:
            due_date = str(item.get("dueDate") or "")
            due_at = str(item.get("dueAt") or "")
            reminder_at = str(item.get("reminderAt") or "")
            reminder_date = str(item.get("reminderDate") or "")
            due_at_date = datetime.fromisoformat(due_at).astimezone(owner_zone).date().isoformat() if due_at else ""
            reminder_at_date = datetime.fromisoformat(reminder_at).astimezone(owner_zone).date().isoformat() if reminder_at else ""
            if any(value and value <= local_date.isoformat() for value in (due_date, due_at_date, reminder_at_date, reminder_date)):
                result.append(item)
        return result

    def schedule_daily_summary(self, local_date: date, role_id: str, now: datetime, *, refresh_pending: bool = False) -> dict[str, object] | None:
        todos = self.todos_for_daily_summary(local_date)
        now_text = now.astimezone(timezone.utc).isoformat()
        payload = [{key: item.get(key) for key in ("id", "title", "description", "dueDate", "dueAt", "reminderAt", "reminderDate", "timezone")} for item in todos]
        status = "pending" if payload else "skipped"
        with sqlite3.connect(self.database_path) as connection:
            if refresh_pending:
                connection.execute(
                    "UPDATE todo_reminder_days SET role_id=?,digest_payload_json=? "
                    "WHERE local_date=? AND digest_status IN ('not_due','pending','failed')",
                    (role_id, json.dumps(payload, ensure_ascii=False), local_date.isoformat()),
                )
            connection.execute(
                "INSERT OR IGNORE INTO todo_reminder_days(local_date,role_id,digest_status,digest_payload_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (local_date.isoformat(), role_id, status, json.dumps(payload, ensure_ascii=False), now_text, now_text),
            )
            connection.execute(
                "UPDATE todo_reminder_days SET role_id=?,digest_status=?,digest_payload_json=?,updated_at=? "
                "WHERE local_date=? AND digest_status='not_due'",
                (role_id, status, json.dumps(payload, ensure_ascii=False), now_text, local_date.isoformat()),
            )
            row = connection.execute(
                "SELECT local_date,role_id,digest_status,digest_payload_json,attempts,last_error,created_at,updated_at,delivered_at "
                "FROM todo_reminder_days WHERE local_date=?",
                (local_date.isoformat(),),
            ).fetchone()
        assert row is not None
        return self.reminder_day(local_date)

    def pending_daily_summaries(self, *, now: datetime | None = None) -> list[dict[str, object]]:
        now_text = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT local_date,role_id,digest_status,digest_payload_json,attempts,last_error,created_at,updated_at,delivered_at "
                "FROM todo_reminder_days WHERE digest_status IN ('pending','failed') "
                "AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY local_date LIMIT 100",
                (now_text,),
            ).fetchall()
        return [
            {
                "date": str(row[0]), "roleId": row[1], "digestStatus": str(row[2]),
                "digestPayload": json.loads(str(row[3])) if row[3] else [], "attempts": int(row[4]),
                "lastError": row[5], "createdAt": str(row[6]), "updatedAt": str(row[7]), "deliveredAt": row[8],
            }
            for row in rows
        ]

    def finish_daily_summary(self, local_date: str, *, error: str | None = None, retry_base: datetime | None = None) -> None:
        now = (retry_base or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with sqlite3.connect(self.database_path) as connection:
            if error is None:
                connection.execute(
                    "UPDATE todo_reminder_days SET digest_status='delivered',attempts=attempts+1,last_error=NULL,next_attempt_at=NULL,updated_at=?,delivered_at=? WHERE local_date=? AND digest_status IN ('pending','failed')",
                    (now, now, local_date),
                )
            else:
                row = connection.execute("SELECT attempts FROM todo_reminder_days WHERE local_date=?", (local_date,)).fetchone()
                attempts = int(row[0]) if row else 0
                connection.execute(
                    "UPDATE todo_reminder_days SET digest_status='failed',attempts=attempts+1,last_error=?,next_attempt_at=?,updated_at=? WHERE local_date=? AND digest_status IN ('pending','failed')",
                    (error[:1000], _retry_at(attempts, base=retry_base), now, local_date),
                )

    def skip_daily_summary(self, local_date: str, reason: str) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE todo_reminder_days SET digest_status='skipped',last_error=?,next_attempt_at=NULL,updated_at=? "
                "WHERE local_date=? AND digest_status IN ('not_due','pending','failed')",
                (reason[:1000], _now(), local_date),
            )

    def daily_summary_payload(self, local_date: date) -> list[dict[str, object]]:
        """Recompute digest facts at delivery time so stale retries cannot remind with old state."""

        todos = self.todos_for_daily_summary(local_date)
        return [{key: item.get(key) for key in ("id", "title", "description", "dueDate", "dueAt", "reminderAt", "reminderDate", "timezone")} for item in todos]

    def pending_role_reminders(
        self,
        *,
        limit: int = 100,
        reminder_ids: Iterable[str] | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, object]]:
        identifiers = [str(identifier) for identifier in reminder_ids] if reminder_ids is not None else None
        now_text = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with sqlite3.connect(self.database_path) as connection:
            sql = (
                "SELECT r.id,r.todo_id,r.due_key,r.due_at,r.role_id,t.title,t.description,t.due_date,t.due_at,t.timezone "
                "FROM todo_reminders r JOIN todos t ON t.id=r.todo_id "
                "WHERE r.status IN ('pending','failed') AND r.due_key LIKE '%:at:%' AND t.status IN ('inbox','scheduled') "
                "AND (r.next_attempt_at IS NULL OR r.next_attempt_at<=?) "
            )
            parameters: list[object] = [now_text]
            if identifiers is not None:
                if not identifiers:
                    return []
                sql += f" AND r.id IN ({','.join('?' for _ in identifiers)})"
                parameters.extend(identifiers)
            sql += " ORDER BY r.due_at,r.created_at LIMIT ?"
            parameters.append(max(1, min(limit, 500)))
            rows = connection.execute(sql, parameters).fetchall()
        return [
            {"id": str(row[0]), "todoId": str(row[1]), "dueKey": str(row[2]), "dueAt": str(row[3]), "roleId": row[4],
             "title": str(row[5]), "description": str(row[6]), "dueDate": row[7], "todoDueAt": row[8], "timezone": str(row[9])}
            for row in rows
        ]

    def assign_reminder_role(self, reminder_id: str, role_id: str) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE todo_reminders SET role_id=? WHERE id=? AND status IN ('pending','failed')",
                (role_id, reminder_id),
            )

    def finish_role_reminder(self, reminder_id: str, *, error: str | None = None, retry_base: datetime | None = None) -> None:
        now = (retry_base or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with sqlite3.connect(self.database_path) as connection:
            if error is None:
                connection.execute(
                    "UPDATE todo_reminders SET status='delivered',attempts=attempts+1,last_error=NULL,next_attempt_at=NULL,updated_at=?,delivered_at=? WHERE id=? AND status IN ('pending','failed')",
                    (now, now, reminder_id),
                )
            else:
                row = connection.execute("SELECT attempts FROM todo_reminders WHERE id=?", (reminder_id,)).fetchone()
                attempts = int(row[0]) if row else 0
                connection.execute(
                    "UPDATE todo_reminders SET status='failed',attempts=attempts+1,last_error=?,next_attempt_at=?,updated_at=? WHERE id=? AND status IN ('pending','failed')",
                    (error[:1000], _retry_at(attempts, base=retry_base), now, reminder_id),
                )

    def skip_role_reminder(self, reminder_id: str, reason: str) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE todo_reminders SET status='skipped',last_error=?,next_attempt_at=NULL,updated_at=? "
                "WHERE id=? AND status IN ('pending','failed')",
                (reason[:1000], _now(), reminder_id),
            )

    def create(self, payload: dict[str, object], *, source: dict[str, str] | None = None) -> dict[str, object]:
        source = source or {}
        title = str(payload.get("title", "")).strip()
        if not title or len(title) > 200:
            raise ValueError("待办标题不能为空且不能超过 200 个字符")
        timezone_name = str(payload.get("timezone") or self.settings()["timezone"])
        due_date = _iso_date(payload.get("dueDate"))
        due_at = _iso_datetime(payload.get("dueAt"), timezone_name)
        reminder_at = _iso_datetime(payload.get("reminderAt"), timezone_name)
        reminder_date = _iso_date(payload.get("reminderDate"))
        if reminder_at and reminder_date:
            raise ValueError("提醒不能同时使用具体时刻和日期")
        now = _now()
        todo_id = uuid.uuid4().hex
        status = "scheduled" if any((due_date, due_at, reminder_at, reminder_date)) else "inbox"
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO todos(id,title,description,status,due_date,due_at,reminder_at,reminder_date,timezone,source_role_id,source_session_key,source_message_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    todo_id, title, str(payload.get("description", ""))[:2000], status,
                    due_date, due_at, reminder_at, reminder_date, timezone_name,
                    source.get("roleId"), source.get("sessionKey"), source.get("messageId"), now, now,
                ),
            )
        return self.get(todo_id)  # type: ignore[return-value]

    def update(self, todo_id: str, payload: dict[str, object]) -> dict[str, object]:
        current = self.get(todo_id)
        if current is None:
            raise KeyError(todo_id)
        timezone_name = str(payload.get("timezone") or current["timezone"])
        editable = {
            "title": current["title"],
            "description": current["description"],
            "dueDate": current["dueDate"],
            "dueAt": current["dueAt"],
            "reminderAt": current["reminderAt"],
            "reminderDate": current["reminderDate"],
            "timezone": timezone_name,
        }
        editable.update(payload)
        if "reminderAt" in payload and payload.get("reminderAt") is None and "reminderDate" not in payload:
            editable["reminderDate"] = None
        if "reminderDate" in payload and payload.get("reminderDate") is None and "reminderAt" not in payload:
            editable["reminderAt"] = None
        title = str(editable["title"]).strip()
        if not title or len(title) > 200:
            raise ValueError("待办标题不能为空且不能超过 200 个字符")
        reminder_at = _iso_datetime(editable["reminderAt"], timezone_name)
        reminder_date = _iso_date(editable["reminderDate"])
        if reminder_at and reminder_date:
            raise ValueError("提醒不能同时使用具体时刻和日期")
        now = _now()
        next_status = current["status"]
        if next_status in {"inbox", "scheduled"}:
            next_status = "scheduled" if any((_iso_date(editable["dueDate"]), _iso_datetime(editable["dueAt"], timezone_name), reminder_at, reminder_date)) else "inbox"
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE todos SET title=?,description=?,status=?,due_date=?,due_at=?,reminder_at=?,reminder_date=?,timezone=?,updated_at=? WHERE id=?",
                (
                    title, str(editable["description"])[:2000], next_status, _iso_date(editable["dueDate"]),
                    _iso_datetime(editable["dueAt"], timezone_name), reminder_at, reminder_date,
                    timezone_name, now, todo_id,
                ),
            )
            connection.execute(
                "UPDATE todo_reminders SET status='cancelled', updated_at=? WHERE todo_id=? AND status IN ('pending','failed')",
                (now, todo_id),
            )
        return self.get(todo_id)  # type: ignore[return-value]

    def get(self, todo_id: str) -> dict[str, object] | None:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        return self._todo(row) if row else None

    def list(self, *, statuses: Iterable[str] | None = None, query: str | None = None) -> list[dict[str, object]]:
        sql = "SELECT * FROM todos WHERE 1=1"
        parameters: list[object] = []
        selected = tuple(statuses or ())
        if selected:
            sql += f" AND status IN ({','.join('?' for _ in selected)})"
            parameters.extend(selected)
        if query and query.strip():
            sql += " AND (title LIKE ? OR description LIKE ?)"
            pattern = f"%{query.strip()}%"
            parameters.extend((pattern, pattern))
        sql += " ORDER BY CASE status WHEN 'inbox' THEN 0 WHEN 'scheduled' THEN 1 WHEN 'completed' THEN 2 ELSE 3 END, COALESCE(due_at, due_date, reminder_date, created_at), created_at DESC"
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [self._todo(row) for row in rows]

    def set_status(self, todo_id: str, status: str) -> dict[str, object]:
        if status not in {"inbox", "scheduled", "completed", "cancelled"}:
            raise ValueError("不支持的待办状态")
        now = _now()
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute("SELECT due_date,due_at,reminder_at,reminder_date FROM todos WHERE id=?", (todo_id,)).fetchone()
            if row is None:
                raise KeyError(todo_id)
            if status == "scheduled" and not any(row):
                raise ValueError("没有日期或提醒的待办不能移入已安排")
            connection.execute(
                "UPDATE todos SET status=?,updated_at=?,completed_at=?,cancelled_at=? WHERE id=?",
                (status, now, now if status == "completed" else None, now if status == "cancelled" else None, todo_id),
            )
            if status in {"completed", "cancelled"}:
                connection.execute(
                    "UPDATE todo_reminders SET status='cancelled',updated_at=? WHERE todo_id=? AND status IN ('pending','failed')",
                    (now, todo_id),
                )
        result = self.get(todo_id)
        if result is None:
            raise KeyError(todo_id)
        return result

    def apply_event(
        self,
        event_id: str,
        role_id: str,
        session_key: str,
        user_message_id: str,
        candidates: list[dict[str, object]],
        *,
        timezone_name: str | None = None,
    ) -> list[dict[str, object]]:
        with sqlite3.connect(self.database_path) as connection:
            existing = connection.execute("SELECT result_json FROM todo_events WHERE event_id=?", (event_id,)).fetchone()
            if existing is not None:
                return json.loads(str(existing[0]))
            applied: list[dict[str, object]] = []
            for index, candidate in enumerate(candidates[:8]):
                operation = str(candidate.get("operation", ""))
                todo_id = candidate.get("todoId")
                result = "ignored"
                error = None
                item: dict[str, object] | None = None
                try:
                    if operation == "create":
                        title = candidate.get("title")
                        if not isinstance(title, str) or not title.strip():
                            raise ValueError("create 操作缺少标题")
                        payload = {key: candidate[key] for key in ("title", "description", "dueDate", "dueAt", "reminderAt", "reminderDate") if key in candidate}
                        payload["timezone"] = str(candidate.get("timezone") or timezone_name or self.settings()["timezone"])
                        matching = connection.execute("SELECT id,title FROM todos WHERE status IN ('inbox','scheduled') AND lower(trim(title))=lower(trim(?))", (title.strip(),)).fetchall()
                        if len(matching) > 1:
                            result, error = "needs_review", "存在多个同名待办，无法安全合并"
                        else:
                            duplicate_id = str(matching[0][0]) if matching else None
                            source = (
                                {"roleId": role_id, "sessionKey": session_key, "messageId": user_message_id}
                                if role_id and session_key and user_message_id != "web"
                                else None
                            )
                            item = self._update_in(connection, duplicate_id, payload) if duplicate_id else self._create_in(connection, payload, {
                                **(source or {}),
                            })
                            result = "applied"
                    elif operation in {"update", "complete", "cancel", "restore"}:
                        target = str(todo_id or "")
                        if not target:
                            title = str(candidate.get("targetTitle", "")).strip()
                            allowed_statuses = {
                                "update": ("inbox", "scheduled"),
                                "complete": ("inbox", "scheduled"),
                                "cancel": ("inbox", "scheduled"),
                                "restore": ("completed", "cancelled"),
                            }[operation]
                            matching = connection.execute(
                                f"SELECT id FROM todos WHERE status IN ({','.join('?' for _ in allowed_statuses)}) "
                                "AND lower(trim(title))=lower(trim(?)) ORDER BY created_at,id",
                                (*allowed_statuses, title),
                            ).fetchall() if title else []
                            if len(matching) == 1:
                                target = str(matching[0][0])
                                todo_id = target
                            elif len(matching) > 1:
                                result, error = "needs_review", "存在多个同名待办，无法安全选择"
                        row = connection.execute("SELECT * FROM todos WHERE id=?", (target,)).fetchone() if target else None
                        if row is None:
                            if error is None:
                                result, error = "needs_review", "操作没有指向当前主人可访问的待办"
                        else:
                            current_status = str(row[3])
                            allowed_statuses = {
                                "update": {"inbox", "scheduled"},
                                "complete": {"inbox", "scheduled"},
                                "cancel": {"inbox", "scheduled"},
                                "restore": {"completed", "cancelled"},
                            }[operation]
                            if current_status not in allowed_statuses:
                                result, error = "needs_review", f"{operation} 操作不适用于 {current_status} 状态"
                            elif operation == "update":
                                patch = {key: candidate[key] for key in ("title", "description", "dueDate", "dueAt", "reminderAt", "reminderDate") if key in candidate}
                                item = self._update_in(connection, target, patch)
                                result = "applied"
                            else:
                                next_status = {"complete": "completed", "cancel": "cancelled"}.get(operation)
                                if operation == "restore":
                                    requested_status = candidate.get("restoreStatus")
                                    next_status = requested_status if requested_status in {"inbox", "scheduled"} else (
                                        "scheduled" if any(row[index] for index in (4, 5, 6, 7)) else "inbox"
                                    )
                                assert next_status is not None
                                item = self._status_in(connection, target, next_status)
                                result = "applied"
                    else:
                        result, error = "ignored", "不支持的待办操作"
                except (ValueError, TypeError) as failure:
                    result, error = "failed", str(failure)[:500]
                operation_id = f"{event_id}:{index}"
                connection.execute(
                    "INSERT OR IGNORE INTO todo_operations(id,todo_id,event_id,source_message_id,operation,result,error,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (operation_id, str(item["id"]) if item else str(todo_id or "") or None, event_id, user_message_id, operation, result, error, _now()),
                )
                if result == "applied" and item is not None:
                    applied.append(item)
            encoded = json.dumps(applied, ensure_ascii=False)
            connection.execute("INSERT INTO todo_events(event_id,result_json,created_at) VALUES(?,?,?)", (event_id, encoded, _now()))
            return applied

    def schedule_due_reminders(self, now: datetime | None = None, *, skip_dates: set[date] | None = None) -> list[dict[str, object]]:
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(timezone.utc)
        now_text = moment.isoformat()
        owner_zone = ZoneInfo(str(self.settings()["timezone"]))
        created: list[dict[str, object]] = []
        with sqlite3.connect(self.database_path) as connection:
            todos = connection.execute(
                "SELECT * FROM todos WHERE status IN ('inbox','scheduled') AND reminder_at IS NOT NULL"
            ).fetchall()
            for row in todos:
                todo = self._todo(row)
                reminder_at = todo["reminderAt"]
                local_date = datetime.fromisoformat(str(reminder_at)).astimezone(owner_zone).date() if reminder_at else None
                due_key = f"{todo['id']}:at:{reminder_at}" if reminder_at else None
                if reminder_at and local_date in (skip_dates or set()):
                    # Materialize the skipped event even when its exact time is
                    # still in the future, so resuming today cannot backfill it.
                    connection.execute(
                        "INSERT OR IGNORE INTO todo_reminders(id,todo_id,due_key,status,due_at,last_error,created_at,updated_at) "
                        "VALUES(?,?,?,'skipped',?,'当天提醒已暂停',?,?)",
                        (uuid.uuid4().hex, todo["id"], due_key, reminder_at, now_text, now_text),
                    )
                    continue
                if due_key is None or str(reminder_at) > now_text:
                    continue
                reminder_id = uuid.uuid4().hex
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO todo_reminders(id,todo_id,due_key,status,due_at,created_at,updated_at) VALUES(?,?,?,'pending',?,?,?)",
                    (reminder_id, todo["id"], due_key, reminder_at, now_text, now_text),
                ).rowcount
                if inserted:
                    created.append({
                        "id": reminder_id,
                        "todoId": todo["id"],
                        "title": todo["title"],
                        "dueKey": due_key,
                        "status": "pending",
                        "dueAt": reminder_at,
                        "createdAt": now_text,
                        "readAt": None,
                    })
            connection.execute(
                "UPDATE todo_reminders SET status='cancelled',updated_at=? WHERE status='pending' AND todo_id IN (SELECT id FROM todos WHERE status IN ('completed','cancelled'))",
                (now_text,),
            )
        return created

    def cancel_completed_reminders(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE todo_reminders SET status='cancelled',updated_at=? "
                "WHERE status IN ('pending','failed') AND todo_id IN (SELECT id FROM todos WHERE status IN ('completed','cancelled'))",
                (_now(),),
            )

    def discard_pending_before(self, local_date: date) -> None:
        """Mark stale work as skipped so a paused day never fires after it ends."""

        zone = ZoneInfo(str(self.settings()["timezone"]))
        boundary = datetime.combine(local_date, datetime.min.time(), zone).astimezone(timezone.utc).isoformat()
        now = _now()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE todo_reminders SET status='skipped',last_error='提醒日期已过',next_attempt_at=NULL,updated_at=? "
                "WHERE status IN ('pending','failed') AND due_at<?",
                (now, boundary),
            )
            connection.execute(
                "UPDATE todo_reminder_days SET digest_status='skipped',last_error='提醒日期已过',next_attempt_at=NULL,updated_at=? "
                "WHERE local_date<? AND digest_status IN ('not_due','pending','failed')",
                (now, local_date.isoformat()),
            )

    def deliver_pending_reminders(self, *, limit: int = 100, now: datetime | None = None) -> list[dict[str, object]]:
        with sqlite3.connect(self.database_path) as connection:
            pending = connection.execute(
                "SELECT r.id,r.todo_id,r.due_key,r.due_at,t.title FROM todo_reminders r "
                "JOIN todos t ON t.id=r.todo_id WHERE r.status='pending' AND t.status IN ('inbox','scheduled') "
                "ORDER BY r.due_at,r.created_at LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        delivered: list[dict[str, object]] = []
        for row in pending:
            reminder_id = str(row[0])
            now_text = now.astimezone(timezone.utc).isoformat() if now is not None else _now()
            try:
                with sqlite3.connect(self.database_path) as connection:
                    current = connection.execute(
                        "SELECT status FROM todos WHERE id=?", (str(row[1]),),
                    ).fetchone()
                    if current is None or current[0] not in {"inbox", "scheduled"}:
                        connection.execute(
                            "UPDATE todo_reminders SET status='cancelled',updated_at=? WHERE id=? AND status='pending'",
                            (now_text, reminder_id),
                        )
                        continue
                    connection.execute("INSERT OR IGNORE INTO todo_reminder_inbox(reminder_id) VALUES(?)", (reminder_id,))
                    connection.execute(
                        "UPDATE todo_reminders SET status='delivered',delivered_at=?,updated_at=?,last_error=NULL "
                        "WHERE id=? AND status='pending'",
                        (now_text, now_text, reminder_id),
                    )
                delivered.append({
                    "id": reminder_id, "todoId": str(row[1]), "title": str(row[4]),
                    "dueKey": str(row[2]), "status": "delivered", "dueAt": str(row[3]),
                    "deliveredAt": now_text,
                })
            except Exception as error:
                self.fail_reminder(reminder_id, f"{type(error).__name__}: {error}")
        return delivered

    def list_reminders(self, *, unread_only: bool = False) -> list[dict[str, object]]:
        condition = " AND r.status='delivered' AND i.read_at IS NULL" if unread_only else ""
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT r.id,r.todo_id,r.due_key,r.status,r.due_at,r.attempts,r.last_error,r.created_at,r.updated_at,r.delivered_at,i.read_at,t.title,t.status "
                "FROM todo_reminders r JOIN todos t ON t.id=r.todo_id LEFT JOIN todo_reminder_inbox i ON i.reminder_id=r.id "
                "WHERE r.status IN ('pending','delivered','failed')" + condition +
                " ORDER BY r.due_at DESC,r.created_at DESC LIMIT 100"
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            due_at = str(row[4])
            try:
                if ":date:" in str(row[2]):
                    local_date = date.fromisoformat(str(row[2]).split(":date:", 1)[1])
                    timezone_name = due_at.rsplit("[", 1)[1].rstrip("]")
                    due_moment = datetime.combine(local_date, datetime.min.time(), ZoneInfo(timezone_name)).astimezone(timezone.utc)
                else:
                    due_moment = datetime.fromisoformat(due_at).astimezone(timezone.utc)
                delivered_moment = datetime.fromisoformat(str(row[9])).astimezone(timezone.utc) if row[9] else None
                delayed = delivered_moment is not None and delivered_moment - due_moment > timedelta(minutes=2)
            except (ValueError, IndexError, ZoneInfoNotFoundError):
                delayed = False
            result.append({
                "id": str(row[0]), "todoId": str(row[1]), "dueKey": str(row[2]), "status": str(row[3]),
                "dueAt": due_at, "attempts": int(row[5]), "lastError": row[6], "createdAt": str(row[7]),
                "updatedAt": str(row[8]), "deliveredAt": row[9], "readAt": row[10],
                "title": str(row[11]), "todoStatus": str(row[12]), "delayed": delayed,
            })
        return result

    def unread_reminder_count(self) -> int:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM todo_reminders r JOIN todo_reminder_inbox i ON i.reminder_id=r.id "
                "WHERE r.status='delivered' AND i.read_at IS NULL"
            ).fetchone()
        return int(row[0])

    def mark_reminder_read(self, reminder_id: str) -> None:
        with sqlite3.connect(self.database_path) as connection:
            now = _now()
            connection.execute("UPDATE todo_reminder_inbox SET read_at=? WHERE reminder_id=?", (now, reminder_id))
            connection.execute("UPDATE todo_reminders SET updated_at=? WHERE id=?", (now, reminder_id))

    def fail_reminder(self, reminder_id: str, error: str) -> bool:
        now = _now()
        with sqlite3.connect(self.database_path) as connection:
            cursor = connection.execute(
                "UPDATE todo_reminders SET status='failed',attempts=attempts+1,last_error=?,updated_at=? WHERE id=? AND status IN ('pending','failed')",
                (error[:1000], now, reminder_id),
            )
        return cursor.rowcount > 0

    def retry_reminder(self, reminder_id: str) -> bool:
        now = _now()
        with sqlite3.connect(self.database_path) as connection:
            cursor = connection.execute(
                "UPDATE todo_reminders SET status='pending',last_error=NULL,next_attempt_at=NULL,updated_at=? WHERE id=? AND status='failed'",
                (now, reminder_id),
            )
        return cursor.rowcount > 0

    def mark_source_unavailable(self, role_id: str) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("INSERT OR REPLACE INTO todo_unavailable_sources(role_id,deleted_at) VALUES(?,?)", (role_id, _now()))

    def operation_diagnostics(self, limit: int = 50) -> list[dict[str, object]]:
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT id,todo_id,event_id,source_message_id,operation,result,error,created_at "
                "FROM todo_operations WHERE result IN ('needs_review','failed') ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [
            {"id": row[0], "todoId": row[1], "eventId": row[2], "sourceMessageId": row[3], "operation": row[4], "result": row[5], "error": row[6], "createdAt": row[7]}
            for row in rows
        ]

    def reminder_diagnostics(self, limit: int = 50) -> list[dict[str, object]]:
        with sqlite3.connect(self.database_path) as connection:
            reminder_rows = connection.execute(
                "SELECT id,todo_id,due_at,attempts,last_error,status,updated_at FROM todo_reminders "
                "WHERE status IN ('pending','failed') ORDER BY due_at LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
            day_rows = connection.execute(
                "SELECT local_date,role_id,attempts,last_error,digest_status,updated_at FROM todo_reminder_days "
                "WHERE digest_status IN ('not_due','pending','failed') ORDER BY local_date LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [
            {
                "id": row[0], "todoId": row[1], "dueAt": row[2], "attempts": int(row[3]),
                "error": row[4], "status": str(row[5]), "kind": "exact", "updatedAt": row[6],
            }
            for row in reminder_rows
        ] + [
            {
                "id": row[0], "roleId": row[1], "attempts": int(row[2]), "error": row[3],
                "status": str(row[4]), "kind": "daily", "updatedAt": row[5],
            }
            for row in day_rows
        ]

    def read_port(self, query: str, *, limit: int = 5) -> list[dict[str, object]]:
        normalized_query = query.casefold()
        now = datetime.now(timezone.utc)
        query_terms = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]{2,}", normalized_query))
        query_terms.update(
            normalized_query[index:index + 2]
            for index in range(max(0, len(normalized_query) - 1))
            if "\u4e00" <= normalized_query[index] <= "\u9fff" and "\u4e00" <= normalized_query[index + 1] <= "\u9fff"
        )
        query_terms -= {"今天", "明天", "后天", "什么", "怎么", "时候", "现在", "是否", "待办", "提醒", "一下", "逾期", "到期", "截止", "安排", "哪些"}
        overdue_request = any(word in normalized_query for word in ("逾期", "过期", "没完成", "未完成"))
        date_request = any(word in normalized_query for word in ("什么时候", "何时", "哪天", "到期", "截止", "日期", "今天", "明天", "后天"))
        list_request = any(word in normalized_query for word in ("有哪些待办", "什么待办", "待办清单", "所有待办", "我的待办"))
        todos = self.list(statuses=("inbox", "scheduled"))
        ranked: list[tuple[int, int, str, dict[str, object]]] = []
        for item in todos:
            title = str(item["title"]).casefold()
            description = str(item["description"]).casefold()
            text = f"{title} {description}"
            item_terms = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]{2,}", text))
            item_terms.update(
                title[index:index + 2]
                for index in range(max(0, len(title) - 1))
                if "\u4e00" <= title[index] <= "\u9fff" and "\u4e00" <= title[index + 1] <= "\u9fff"
            )
            score = len(query_terms & item_terms)
            if title and title in normalized_query:
                score += 3
            due_date = str(item.get("dueDate") or "")
            due_at = str(item.get("dueAt") or "")
            local_today = now.astimezone(ZoneInfo(str(item["timezone"]))).date().isoformat()
            due_at_date = datetime.fromisoformat(due_at).astimezone(ZoneInfo(str(item["timezone"]))).date().isoformat() if due_at else ""
            overdue = bool((due_date and due_date < local_today) or (due_at_date and due_at_date < local_today))
            temporal_match = (overdue and overdue_request) or (date_request and bool(due_date or due_at))
            if score or temporal_match or list_request:
                ranked.append((score, int(overdue and overdue_request), due_at or due_date, item))
        ranked.sort(key=lambda pair: (-pair[0], -pair[1], pair[2] or "9999-99-99"))
        return [item for _, _, _, item in ranked[:max(0, min(limit, 10))]]

    def _create_in(self, connection: sqlite3.Connection, payload: dict[str, object], source: dict[str, str] | None = None) -> dict[str, object]:
        source = source or {}
        title = str(payload.get("title", "")).strip()
        if not title or len(title) > 200:
            raise ValueError("待办标题不能为空且不能超过 200 个字符")
        timezone_name = str(payload.get("timezone") or self.settings()["timezone"])
        due_date = _iso_date(payload.get("dueDate"))
        due_at = _iso_datetime(payload.get("dueAt"), timezone_name)
        reminder_at = _iso_datetime(payload.get("reminderAt"), timezone_name)
        reminder_date = _iso_date(payload.get("reminderDate"))
        if reminder_at and reminder_date:
            raise ValueError("提醒不能同时使用具体时刻和日期")
        now = _now()
        todo_id = uuid.uuid4().hex
        status = "scheduled" if any((due_date, due_at, reminder_at, reminder_date)) else "inbox"
        connection.execute(
            "INSERT INTO todos(id,title,description,status,due_date,due_at,reminder_at,reminder_date,timezone,source_role_id,source_session_key,source_message_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (todo_id,title,str(payload.get("description", ""))[:2000],status,due_date,due_at,reminder_at,reminder_date,timezone_name,source.get("roleId"),source.get("sessionKey"),source.get("messageId"),now,now),
        )
        row = connection.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        return self._todo(row)

    def _update_in(self, connection: sqlite3.Connection, todo_id: str, payload: dict[str, object]) -> dict[str, object]:
        row = connection.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        if row is None:
            raise KeyError(todo_id)
        current = self._todo(row)
        timezone_name = str(payload.get("timezone") or current["timezone"])
        merged = {
            "title": current["title"], "description": current["description"], "dueDate": current["dueDate"],
            "dueAt": current["dueAt"], "reminderAt": current["reminderAt"], "reminderDate": current["reminderDate"],
        }
        merged.update(payload)
        if "reminderAt" in payload and payload.get("reminderAt") is None and "reminderDate" not in payload:
            merged["reminderDate"] = None
        if "reminderDate" in payload and payload.get("reminderDate") is None and "reminderAt" not in payload:
            merged["reminderAt"] = None
        title = str(merged["title"]).strip()
        reminder_at = _iso_datetime(merged["reminderAt"], timezone_name)
        reminder_date = _iso_date(merged["reminderDate"])
        if not title or len(title) > 200 or (reminder_at and reminder_date):
            raise ValueError("待办内容或提醒时间无效")
        now = _now()
        next_status = current["status"]
        if next_status in {"inbox", "scheduled"}:
            next_status = "scheduled" if any((_iso_date(merged["dueDate"]), _iso_datetime(merged["dueAt"], timezone_name), reminder_at, reminder_date)) else "inbox"
        connection.execute(
            "UPDATE todos SET title=?,description=?,status=?,due_date=?,due_at=?,reminder_at=?,reminder_date=?,timezone=?,updated_at=? WHERE id=?",
            (title,str(merged["description"])[:2000],next_status,_iso_date(merged["dueDate"]),_iso_datetime(merged["dueAt"],timezone_name),reminder_at,reminder_date,timezone_name,now,todo_id),
        )
        connection.execute("UPDATE todo_reminders SET status='cancelled',updated_at=? WHERE todo_id=? AND status IN ('pending','failed')", (now,todo_id))
        return self._todo(connection.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone())

    def _status_in(self, connection: sqlite3.Connection, todo_id: str, status: str) -> dict[str, object]:
        if status not in {"inbox", "scheduled", "completed", "cancelled"}:
            raise ValueError("不支持的待办状态")
        now = _now()
        row = connection.execute("SELECT due_date,due_at,reminder_at,reminder_date FROM todos WHERE id=?", (todo_id,)).fetchone()
        if row is None:
            raise KeyError(todo_id)
        if status == "scheduled" and not any(row):
            raise ValueError("没有日期或提醒的待办不能移入已安排")
        connection.execute("UPDATE todos SET status=?,updated_at=?,completed_at=?,cancelled_at=? WHERE id=?", (status,now,now if status == "completed" else None,now if status == "cancelled" else None,todo_id))
        if status in {"completed", "cancelled"}:
            connection.execute("UPDATE todo_reminders SET status='cancelled',updated_at=? WHERE todo_id=? AND status IN ('pending','failed')", (now,todo_id))
        return self._todo(connection.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone())

    def _todo(self, row: tuple[Any, ...]) -> dict[str, object]:
        with sqlite3.connect(self.database_path) as connection:
            unavailable = connection.execute("SELECT 1 FROM todo_unavailable_sources WHERE role_id=?", (row[9],)).fetchone() if row[9] else None
        return {
            "id": str(row[0]), "title": str(row[1]), "description": str(row[2]), "status": str(row[3]),
            "dueDate": row[4], "dueAt": row[5], "reminderAt": row[6], "reminderDate": row[7], "timezone": str(row[8]),
            "sourceRoleId": row[9], "sourceSessionKey": row[10], "sourceMessageId": row[11],
            "createdAt": str(row[12]), "updatedAt": str(row[13]), "completedAt": row[14], "cancelledAt": row[15],
            "sourceAvailable": unavailable is None,
        }
