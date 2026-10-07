from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType

from .tools import AgentTool, ToolDefinition, ToolRegistry


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """Serializable description of the capabilities used by one run."""

    snapshot_id: str
    run_id: str
    role_id: str
    session_key: str
    tools: tuple[ToolDefinition, ...]
    policy_decisions: MappingProxyType

    def provider_schemas(self) -> list[dict[str, object]]:
        return [definition.as_provider_schema() for definition in self.tools]

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshotId": self.snapshot_id,
            "runId": self.run_id,
            "roleId": self.role_id,
            "sessionKey": self.session_key,
            "tools": [
                {
                    "name": definition.name,
                    "version": definition.version,
                    "source": definition.source,
                    "risk": definition.risk,
                    "exposure": definition.exposure,
                    "approval": definition.approval,
                    "timeoutSeconds": definition.timeout_seconds,
                    "outputLimit": definition.output_limit,
                    "inputSchema": deepcopy(definition.input_schema),
                    "description": definition.description,
                }
                for definition in self.tools
            ],
            "policyDecisions": dict(self.policy_decisions),
        }


@dataclass(frozen=True, slots=True)
class CapabilityResolution:
    """A frozen snapshot plus the process-local tools that implement it."""

    snapshot: CapabilitySnapshot
    tools: tuple[AgentTool, ...]
    denied_tools: MappingProxyType

    def provider_schemas(self) -> list[dict[str, object]]:
        return self.snapshot.provider_schemas()

    def get(self, name: str) -> AgentTool | None:
        return next((tool for tool in self.tools if tool.definition.name == name), None)

    def denial_reason(self, name: str) -> str | None:
        reason = self.denied_tools.get(name)
        return str(reason) if reason is not None else None

    def validate_arguments(self, name: str, arguments: dict[str, object]) -> None:
        ToolRegistry(self.tools).validate_arguments(name, arguments)


class CapabilityRegistry:
    """Resolve registered tools into a stable, role-scoped run snapshot."""

    def __init__(self, tools: tuple[AgentTool, ...] | list[AgentTool] = ()) -> None:
        self._tools: dict[str, AgentTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: AgentTool) -> None:
        name = tool.definition.name
        if name in self._tools:
            raise ValueError(f"duplicate tool: {name}")
        self._tools[name] = tool

    def resolve(
        self,
        *,
        role_id: str,
        session_key: str,
        run_id: str,
        enabled_tools: set[str] | frozenset[str] | None = None,
        allowed_risks: set[str] | frozenset[str] | None = None,
    ) -> CapabilityResolution:
        selected: list[AgentTool] = []
        decisions: dict[str, str] = {}
        denied: dict[str, str] = {}
        for tool in self._tools.values():
            definition = tool.definition
            if enabled_tools is not None and definition.name not in enabled_tools:
                denied[definition.name] = "tool is disabled for this run"
                decisions[definition.name] = "denied:disabled"
                continue
            if allowed_risks is not None and definition.risk not in allowed_risks:
                denied[definition.name] = "tool risk is not allowed for this run"
                decisions[definition.name] = "denied:risk"
                continue
            if definition.exposure == "hidden":
                denied[definition.name] = "tool is hidden"
                decisions[definition.name] = "denied:hidden"
                continue
            selected.append(tool)
            decisions[definition.name] = "allowed"

        snapshot_definitions = tuple(
            ToolDefinition(
                name=tool.definition.name,
                description=tool.definition.description,
                input_schema=deepcopy(tool.definition.input_schema),
                risk=tool.definition.risk,
                source=tool.definition.source,
                version=tool.definition.version,
                exposure=tool.definition.exposure,
                approval=tool.definition.approval,
                timeout_seconds=tool.definition.timeout_seconds,
                output_limit=tool.definition.output_limit,
            )
            for tool in selected
        )
        snapshot = CapabilitySnapshot(
            snapshot_id=f"cap-{run_id}",
            run_id=run_id,
            role_id=role_id,
            session_key=session_key,
            tools=snapshot_definitions,
            policy_decisions=MappingProxyType(dict(decisions)),
        )
        return CapabilityResolution(
            snapshot=snapshot,
            tools=tuple(selected),
            denied_tools=MappingProxyType(dict(denied)),
        )
