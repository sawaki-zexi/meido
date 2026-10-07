from datetime import datetime
from typing import Any, Literal
import uuid

from pydantic import BaseModel, Field, field_validator


class RoleProfile(BaseModel):
    profile: str = ""
    personality: str = ""
    behaviorRules: str = ""
    responseConstraints: str = ""
    nickname: str = ""


class ShellToolConfig(BaseModel):
    """Role-scoped policy for the optional process execution tool."""

    enabled: bool = False
    allowedCommands: list[str] = Field(default_factory=list)
    timeoutSeconds: float = Field(default=30.0, gt=0, le=300)
    maxOutputChars: int = Field(default=20000, gt=0, le=200000)

    @field_validator("allowedCommands")
    @classmethod
    def validate_allowed_commands(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip()))


class AgentToolConfig(BaseModel):
    shell: ShellToolConfig = Field(default_factory=ShellToolConfig)


class RoleInput(BaseModel):
    name: str
    description: str = ""
    profile: RoleProfile
    modelConfig: dict[str, Any] = Field(default_factory=dict)
    proactiveConfig: dict[str, Any] = Field(default_factory=dict)
    agentConfig: AgentToolConfig = Field(default_factory=AgentToolConfig)

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
    agentConfig: AgentToolConfig | None = None

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
    avatarUrl: str | None = None
    avatarMediaType: str | None = None
    avatarOriginalUrl: str | None = None
    avatarOriginalMediaType: str | None = None
    cardImageUrl: str | None = None
    cardImageMediaType: str | None = None


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
    messageType: str = "text"
    toolCallId: str | None = None
    toolName: str | None = None
    toolArguments: dict[str, Any] | None = None
    toolResult: Any | None = None
    isError: bool = False
    runId: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentRun(BaseModel):
    runId: str
    roleId: str
    sessionKey: str
    status: Literal["created", "running", "completed", "failed", "cancelled", "max_turns"]
    modelConfigurationId: str | None = None
    modelSnapshot: dict[str, Any] = Field(default_factory=dict)
    turnCount: int = 0
    startedAt: datetime
    endedAt: datetime | None = None
    cancelReason: str | None = None
    error: str | None = None


class SessionResponse(BaseModel):
    session: SessionSummary
    messages: list[Message]


class SendMessageInput(BaseModel):
    content: str
    toolMemoryIds: list[str] = Field(default_factory=list)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("消息不能为空")
        return value

    @field_validator("toolMemoryIds")
    @classmethod
    def validate_tool_memory_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip()))


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
    emotionalWeight: int = 0
    hasEmbedding: bool = False


class MemoryList(BaseModel):
    memories: list[MemoryItem]
    total: int | None = None
    page: int | None = None
    pageSize: int | None = None
    # Optional diagnostic payload populated for recall queries. Keeping the
    # original memories field makes this additive for existing clients.
    hits: list[dict[str, Any]] = Field(default_factory=list)
    trace: dict[str, Any] | None = None


class MemoryAdminUpdateInput(BaseModel):
    status: str | None = None
    extraJson: dict[str, Any] | None = None
    sourceRef: MemorySourceRef | None = None
    happenedAt: datetime | None = None
    emotionalWeight: int | None = Field(default=None, ge=0, le=10)


class MemoryBatchDeleteInput(BaseModel):
    ids: list[str] = Field(min_length=1)

    @field_validator("ids")
    @classmethod
    def validate_ids(cls, value: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if not cleaned:
            raise ValueError("至少需要一个记忆 ID")
        return cleaned


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


class UpdateMemoryInput(BaseModel):
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
