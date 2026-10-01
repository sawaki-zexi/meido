from datetime import datetime
from typing import Any
import uuid

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


class RoleUpdateInput(BaseModel):
    name: str
    description: str = ""
    profile: RoleProfile

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
    modelConfigurationId: str | None = None


class RoleModelConfigurationInput(BaseModel):
    configurationId: str | None

    @field_validator("configurationId")
    @classmethod
    def validate_configuration_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("模型连接 ID 不能为空")
        return value


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


class MemoryOrigin(BaseModel):
    kind: str
    sessionKey: str
    messageIds: list[str] = Field(default_factory=list)
    messageRange: tuple[int, int] | None = None
    stableSourceKey: str


class MemorySourceRef(MemoryOrigin):
    sourceKeys: list[str] = Field(default_factory=list)
    sources: list[MemoryOrigin] = Field(default_factory=list)


class MemoryItem(BaseModel):
    id: str
    roleId: str
    memoryType: str
    summary: str
    extra: dict[str, Any] = Field(default_factory=dict)
    sourceRef: MemorySourceRef
    happenedAt: datetime | None = None
    status: str
    createdAt: datetime
    updatedAt: datetime
    reinforcement: int = 1
    contentHash: str


class MemoryList(BaseModel):
    memories: list[MemoryItem]


class RememberMemoryInput(BaseModel):
    summary: str
    memoryType: str = "fact"
    happenedAt: datetime | None = None

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("记忆内容不能为空")
        return value


class ModelConfigurationInput(BaseModel):
    providerId: str
    provider: str
    baseUrl: str
    model: str
    apiKey: str = ""

    @field_validator("providerId", "provider", "baseUrl", "model")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("服务商、地址和模型均不能为空")
        return value

    @field_validator("apiKey")
    @classmethod
    def normalize_api_key(cls, value: str) -> str:
        return value.strip()


class ModelConfiguration(ModelConfigurationInput):
    id: str = Field(default_factory=lambda: f"model-{uuid.uuid4().hex}")


class ProviderPreset(BaseModel):
    id: str
    label: str
    provider: str
    baseUrl: str
    modelHint: str


class ProviderPresetList(BaseModel):
    providers: list[ProviderPreset]
