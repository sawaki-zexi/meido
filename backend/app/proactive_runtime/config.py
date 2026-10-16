"""Explicit autonomy grants; configuration never supplies a runtime identity."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DRIFT_TOOL_NAMES = frozenset({'read_file', 'write_file', 'edit_file', 'list_dir', 'recall_memory', 'web_fetch',
    'web_search', 'search_messages', 'fetch_messages', 'shell', 'message_push'})
DRIFT_RESOURCE_IDS = frozenset({'role-profile', 'proactive-context', 'builtin-create-drift-skill', 'builtin-meme-manage'})


class DriftAuthoringGrant(BaseModel):
    """Permission to author declarations, separate from future execution grants."""
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    enabled: bool = False
    scopeId: str = Field(default="", max_length=64)
    revision: int = Field(default=0, ge=0)
    allowedTools: list[str] = Field(default_factory=list, max_length=128)
    allowMcp: bool = False
    allowShell: bool = False
    allowAutonomousAuthoring: bool = False
    allowedResourceIds: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode='after')
    def finite_scope(self):
        if (len(set(self.allowedTools)) != len(self.allowedTools)
                or set(self.allowedTools) - DRIFT_TOOL_NAMES - {'finish_drift', 'activate_skill', 'mount_server'}
                or len(set(self.allowedResourceIds)) != len(self.allowedResourceIds)
                or set(self.allowedResourceIds) - DRIFT_RESOURCE_IDS):
            raise ValueError('author declaration scope must contain finite host tools and resources')
        return self


class DriftMcpOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    name: str = Field(min_length=1, max_length=64)
    definitionHash: str = Field(pattern=r"^[a-f0-9]{64}$")


class DriftMcpGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    serverId: str = Field(pattern=r"^[a-z][a-z0-9_]{0,23}$")
    configRevision: str = Field(min_length=1, max_length=128)
    grantRevision: str = Field(min_length=1, max_length=128)
    risk: Literal["read_only", "mutating", "external"] = "read_only"
    operations: list[DriftMcpOperation] = Field(min_length=1, max_length=128)

    @model_validator(mode='after')
    def unique_operations(self):
        if len({item.name for item in self.operations}) != len(self.operations):
            raise ValueError('duplicate MCP operation')
        return self


class SourceGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    sourceId: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    revision: int = Field(default=1, ge=1)
    channel: Literal["alert", "content", "context"]
    serverId: str = Field(pattern=r"^[a-z][a-z0-9_]{0,23}$")
    configRevision: str = Field(min_length=1, max_length=128)
    grantRevision: str = Field(min_length=1, max_length=128)
    getTool: str = Field(min_length=1, max_length=64)
    getDefinitionHash: str = Field(pattern=r"^[a-f0-9]{64}$")
    pollTool: str | None = Field(default=None, min_length=1, max_length=64)
    pollDefinitionHash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    ackTool: str | None = Field(default=None, min_length=1, max_length=64)
    ackDefinitionHash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    ackContract: Literal["none", "absolute-idempotent", "legacy-ttl"] = "none"
    ackProofId: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    eventDomain: str = Field(min_length=1, max_length=128)
    enabled: bool = False

    @model_validator(mode="after")
    def paired_definitions(self):
        if bool(self.pollTool) != bool(self.pollDefinitionHash):
            raise ValueError("poll tool and definition hash must be paired")
        if bool(self.ackTool) != bool(self.ackDefinitionHash):
            raise ValueError("ACK tool and definition hash must be paired")
        if bool(self.ackTool) != (self.ackContract != "none"):
            raise ValueError("ACK tool requires an explicit contract")
        return self


class AutonomyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    revision: int = Field(default=0, ge=0)
    activeDecisionEnabled: bool = False
    driftEnabled: bool = False
    decisionModelConfigurationId: str | None = Field(default=None, min_length=1, max_length=128)
    driftModelConfigurationId: str | None = Field(default=None, min_length=1, max_length=128)
    defaultTargetRef: str = Field(default="desktop", min_length=1, max_length=256)
    allowedTargetRefs: list[str] = Field(default_factory=lambda: ["desktop"], min_length=1, max_length=4)
    sourceGrants: list[SourceGrant] = Field(default_factory=list, max_length=8)
    driftToolGrants: list[str] = Field(default_factory=list, max_length=128)
    driftAuthoring: DriftAuthoringGrant = Field(default_factory=DriftAuthoringGrant)
    driftMcpGrants: list[DriftMcpGrant] = Field(default_factory=list, max_length=8)
    driftReadResourceIds: list[str] = Field(default_factory=list, max_length=16)
    driftAttachmentIds: list[str] = Field(default_factory=list, max_length=16)
    shellRuleIds: list[str] = Field(default_factory=list, max_length=32)
    consentRevision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def require_explicit_grants(self):
        if self.activeDecisionEnabled and not self.decisionModelConfigurationId:
            raise ValueError("active decision requires an explicit model")
        if self.driftEnabled and not self.driftModelConfigurationId:
            raise ValueError("Drift requires an explicit model")
        if (self.activeDecisionEnabled or self.driftEnabled) and self.consentRevision < 1:
            raise ValueError("enabled autonomy requires finite-operation consent")
        if len(set(self.allowedTargetRefs)) != len(self.allowedTargetRefs):
            raise ValueError("duplicate allowed targets")
        if self.defaultTargetRef not in self.allowedTargetRefs:
            raise ValueError("default target must have an explicit grant")
        if any(not value or len(value) > 256 for value in (*self.allowedTargetRefs, *self.driftToolGrants, *self.shellRuleIds)):
            raise ValueError("invalid autonomy grant identifier")
        if len({source.sourceId for source in self.sourceGrants}) != len(self.sourceGrants):
            raise ValueError("duplicate source identity")
        if (len({grant.serverId for grant in self.driftMcpGrants}) != len(self.driftMcpGrants)
                or len({source.serverId for source in self.sourceGrants} | {grant.serverId for grant in self.driftMcpGrants}) > 8):
            raise ValueError('Drift and source server union exceeds the frozen mount ceiling')
        for values in (self.driftToolGrants, self.shellRuleIds, self.driftReadResourceIds, self.driftAttachmentIds):
            if len(set(values)) != len(values) or any(not value or len(value) > 128 for value in values):
                raise ValueError('duplicate or invalid Drift grant')
        if set(self.driftToolGrants) - DRIFT_TOOL_NAMES or set(self.driftReadResourceIds) - DRIFT_RESOURCE_IDS:
            raise ValueError('unknown Drift host tool or resource')
        return self
