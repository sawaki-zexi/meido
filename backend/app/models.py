from datetime import datetime
from typing import Any, Literal
import uuid

from pydantic import BaseModel, Field, field_validator, model_validator
from .novelai_generation.config import NovelaiRoleConfig

from .coding_tasks.config import CodingRoleConfig


# Host-owned ordinary tools. Legacy empty/partial opt-in lists cannot hide them.
DEFAULT_BUILTIN_TOOLS = (
    'read_file', 'list_dir', 'write_file', 'edit_file', 'web_search', 'web_fetch',
    'search_messages', 'fetch_messages', 'memorize', 'forget_memory', 'tool_search',
    'shell', 'task_output', 'task_stop', 'message_push', 'spawn', 'spawn_manage',
    'schedule', 'list_schedules', 'cancel_schedule', 'mcp_add', 'mcp_list', 'mcp_remove',
)


class RoleCharacterDefinition(BaseModel):
    profile: str = ""
    personality: str = ""
    behaviorRules: str = ""
    responseConstraints: str = ""
    nickname: str = ""


class RoleImportProvenance(BaseModel):
    """Attribution only; never a source of runtime instructions."""

    format: Literal["json", "png", "apng", "charx"]
    source: str = Field(min_length=1, max_length=255)
    importedAt: datetime

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str) -> str:
        return value.replace(chr(92), "/").rsplit("/", 1)[-1].strip() or "role-card"


class RoleProfile(BaseModel):
    version: int = Field(default=1, strict=True, ge=1, le=1)
    character: RoleCharacterDefinition = Field(default_factory=RoleCharacterDefinition)
    importProvenance: RoleImportProvenance | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_fields(cls, value: Any) -> Any:
        if isinstance(value, dict) and "character" not in value and "version" not in value:
            return {"character": value}
        return value

    @property
    def profile(self) -> str:
        return self.character.profile

    @property
    def personality(self) -> str:
        return self.character.personality

    @property
    def behaviorRules(self) -> str:
        return self.character.behaviorRules

    @property
    def responseConstraints(self) -> str:
        return self.character.responseConstraints

    @property
    def nickname(self) -> str:
        return self.character.nickname


class ShellToolConfig(BaseModel):
    """Budgets and legacy restricted-use policy; ordinary Shell is host-provided."""

    enabled: bool = False
    allowedCommands: list[str] = Field(default_factory=list)
    timeoutSeconds: float = Field(default=30.0, gt=0, le=300)
    maxOutputChars: int = Field(default=20000, gt=0, le=200000)

    @field_validator("allowedCommands")
    @classmethod
    def validate_allowed_commands(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip()))


class MemoryRecallToolConfig(BaseModel):
    """Role policy for the optional model-initiated memory recall tool."""

    enabled: bool = False


class BrowserUseToolConfig(BaseModel):
    """Role policy for the optional Shiori-compatible browser plugin."""

    enabled: bool = False


class ComputerUseToolConfig(BaseModel):
    enabled: bool = False


class SpawnToolConfig(BaseModel):
    enabled: bool = False
    timeoutSeconds: float = Field(default=120, gt=0, le=600)
    maxTurns: int = Field(default=12, ge=1, le=30)
    maxOutputChars: int = Field(default=20000, ge=100, le=100000)
    maxModelOutputChars: int = Field(default=60000, ge=100, le=200000)


class ScreenPerceptionConfig(BaseModel):
    enabled: bool = False
    visionModelConfigurationId: str | None = None

    @field_validator("visionModelConfigurationId")
    @classmethod
    def validate_binding(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("视觉模型连接 ID 不能为空")
        return value.strip() if value is not None else None


class AgentToolConfig(BaseModel):
    novelai: NovelaiRoleConfig = Field(default_factory=NovelaiRoleConfig)
    shell: ShellToolConfig = Field(default_factory=ShellToolConfig)
    memoryRecall: MemoryRecallToolConfig = Field(default_factory=MemoryRecallToolConfig)
    browserUse: BrowserUseToolConfig = Field(default_factory=BrowserUseToolConfig)
    computerUse: ComputerUseToolConfig = Field(default_factory=ComputerUseToolConfig)
    spawn: SpawnToolConfig = Field(default_factory=SpawnToolConfig)
    screenPerception: ScreenPerceptionConfig = Field(default_factory=ScreenPerceptionConfig)
    coding: CodingRoleConfig = Field(default_factory=CodingRoleConfig)
    enabledTools: list[str] = Field(default_factory=list)
    enabledPlugins: list[str] = Field(default_factory=list)
    grantedCapabilities: list[str] = Field(default_factory=list)

    @field_validator("enabledTools", "enabledPlugins", "grantedCapabilities")
    @classmethod
    def validate_capability_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip()))


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
        if not value.character.profile.strip():
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
        if not value.character.profile.strip():
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
    attachments: list[str] = Field(default_factory=list)
    toolMemoryIds: list[str] = Field(default_factory=list)
    skillIds: list[str] = Field(default_factory=list)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_message(self) -> "SendMessageInput":
        if not self.content and not self.attachments:
            raise ValueError("消息不能为空")
        return self

    @field_validator("toolMemoryIds")
    @classmethod
    def validate_tool_memory_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip()))

    @field_validator("skillIds")
    @classmethod
    def validate_skill_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip()))

    @field_validator("attachments")
    @classmethod
    def validate_attachments(cls, value: list[str]) -> list[str]:
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
    api: str = "openai-completions"
    apiOptions: dict[str, str] = Field(default_factory=dict)
    contextWindowTokens: int | None = Field(default=None, strict=True, gt=0, le=10000000)
    modelAutoCompactTokenLimit: int | None = Field(default=None, strict=True, gt=0, le=10000000)

    @model_validator(mode='after')
    def validate_context_capacity(self):
        if self.modelAutoCompactTokenLimit is not None and (
            self.contextWindowTokens is None or self.modelAutoCompactTokenLimit >= self.contextWindowTokens
        ):
            raise ValueError('自动压缩阈值必须小于已填写的模型上下文容量')
        return self

    @field_validator("providerId", "provider", "baseUrl", "model", "api")
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

    @field_validator("apiOptions")
    @classmethod
    def normalize_api_options(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            key.strip(): item.strip()
            for key, item in value.items()
            if key.strip() and item.strip()
        }


class ModelConfiguration(ModelConfigurationInput):
    id: str = Field(default_factory=lambda: f"model-{uuid.uuid4().hex}")


class EmbeddingConfigurationInput(BaseModel):
    provider: str = "dashscope"
    baseUrl: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: str = "text-embedding-v3"
    dimensions: int = Field(default=1024, gt=0, le=4096)
    apiKey: str = ""

    @field_validator("provider", "baseUrl", "model")
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


class EmbeddingConfiguration(EmbeddingConfigurationInput):
    pass


class ProviderPreset(BaseModel):
    id: str
    label: str
    provider: str
    baseUrl: str
    modelHint: str
    api: str = "openai-completions"


class ProviderPresetList(BaseModel):
    providers: list[ProviderPreset]
