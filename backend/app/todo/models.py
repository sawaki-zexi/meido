from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator


TodoStatus = Literal["inbox", "scheduled", "completed", "cancelled"]


def _timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("时区必须是有效的 IANA 标识") from error
    return value


class TodoCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    dueDate: date | None = None
    dueAt: datetime | None = None
    reminderAt: datetime | None = None
    reminderDate: date | None = None
    timezone: str = "Asia/Shanghai"

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("待办标题不能为空")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        return _timezone(value)

    @model_validator(mode="after")
    def validate_reminder(self):
        if self.reminderAt is not None and self.reminderDate is not None:
            raise ValueError("提醒不能同时使用具体时刻和日期")
        return self


class TodoUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    dueDate: date | None = None
    dueAt: datetime | None = None
    reminderAt: datetime | None = None
    reminderDate: date | None = None
    timezone: str | None = None

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("待办标题不能为空")
        value = value.strip()
        if not value:
            raise ValueError("待办标题不能为空")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str | None) -> str | None:
        return _timezone(value) if value is not None else None

    @model_validator(mode="after")
    def validate_reminder(self):
        if "reminderAt" in self.model_fields_set and "reminderDate" in self.model_fields_set:
            if self.reminderAt is not None and self.reminderDate is not None:
                raise ValueError("提醒不能同时使用具体时刻和日期")
        return self


class TodoStatusInput(BaseModel):
    status: TodoStatus


class TodoSettingsInput(BaseModel):
    enabled: bool | None = None
    remindersEnabled: bool | None = None
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str | None) -> str | None:
        return _timezone(value) if value is not None else None


class TodoReminderRoleInput(BaseModel):
    enabled: bool


class TodoList(BaseModel):
    todos: list[dict[str, object]]


class TodoReminderList(BaseModel):
    reminders: list[dict[str, object]]
    unreadCount: int
