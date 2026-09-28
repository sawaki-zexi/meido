from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class RoleProfile(BaseModel):
    profile: str = ""
    personality: str = ""
    behaviorRules: str = ""
    responseConstraints: str = ""
    nickname: str = ""


class RoleInput(BaseModel):
    name: str
    description: str = ""
    profile: RoleProfile
    modelConfig: dict[str, Any] = Field(default_factory=dict)
    proactiveConfig: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("角色名称不能为空")
        return value

    @field_validator("profile")
    @classmethod
    def validate_profile(cls, value: RoleProfile) -> RoleProfile:
        if not value.profile.strip():
            raise ValueError("角色设定不能为空")
        return value


class Role(RoleInput):
    id: str
    createdAt: datetime
    updatedAt: datetime


class RoleList(BaseModel):
    roles: list[Role]


class RoleResponse(BaseModel):
    role: Role


class SessionSummary(BaseModel):
    sessionKey: str
    roleId: str
    createdAt: datetime
    updatedAt: datetime


class Message(BaseModel):
    id: str
    sessionKey: str
    sequence: int
    role: str
    content: str
    status: str
    createdAt: datetime


class SessionResponse(BaseModel):
    session: SessionSummary
    messages: list[Message]


class SendMessageInput(BaseModel):
    content: str

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("消息不能为空")
        return value
