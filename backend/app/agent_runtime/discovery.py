"""Run-local discovery: freeze admission, stage activation, publish after persistence."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from collections.abc import Mapping, Sequence

from .tools import AgentTool, AgentToolResult, ToolContext, ToolDefinition, ToolRegistry

if TYPE_CHECKING:
    from .capabilities import CapabilityResolution


@dataclass(frozen=True)
class ServiceGrant:
    """A host-admitted service permission, never supplied by model arguments."""

    grant_id: str
    service_id: str
    role_id: str
    allowed_risks: frozenset[str]
    external_allowed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_risks", frozenset(self.allowed_risks))
        if not all(
            value.strip() for value in (self.grant_id, self.service_id, self.role_id)
        ):
            raise ValueError("service grant identity is required")
        if not self.allowed_risks or self.allowed_risks - {
            "read_only",
            "mutating",
            "external",
        }:
            raise ValueError("invalid service risk ceiling")

    def to_dict(self) -> dict[str, object]:
        return {
            "grantId": self.grant_id,
            "serviceId": self.service_id,
            "roleId": self.role_id,
            "allowedRisks": sorted(self.allowed_risks),
            "externalAllowed": self.external_allowed,
        }


@dataclass(frozen=True)
class _ServiceExtension:
    parent: str
    tools: tuple[AgentTool, ...]
    definitions: tuple[ToolDefinition, ...]


class DiscoveryState:
    def __init__(
        self,
        *,
        service_grants: Sequence[ServiceGrant] = (),
        external_only: bool = False,
    ) -> None:
        self.revisions: list[CapabilityResolution] = []
        self.pending: dict[str | None, tuple[str, ...]] = {}
        self.extensions: dict[str, _ServiceExtension] = {}
        self.removals: dict[str, tuple[str, str]] = {}
        self.grants = {item.grant_id: item for item in service_grants}
        if len(self.grants) != len(service_grants):
            raise ValueError("duplicate service grant")
        self.external_only = external_only

    def bind(self, initial: CapabilityResolution, reserved_names: set[str]) -> None:
        if any(
            item.role_id != initial.snapshot.role_id for item in self.grants.values()
        ):
            raise PermissionError("service grant belongs to another role")
        self.revisions.append(initial)
        self.reserved_names = frozenset(reserved_names)
        self.name_sources = {item.name: item.source for item in initial.snapshot.candidates}

    @property
    def tools(self) -> Mapping[str, AgentTool]:
        return self.current._candidate_tools

    @property
    def definitions(self) -> dict[str, ToolDefinition]:
        return {item.name: item for item in self.current.snapshot.candidates}

    @property
    def current(self) -> CapabilityResolution:
        return self.revisions[-1]

    def view(self, context: ToolContext) -> CapabilityResolution:
        current = self.current
        if (context.role_id, context.session_key, context.run_id) != (
            current.snapshot.role_id,
            current.snapshot.session_key,
            current.snapshot.run_id,
        ):
            raise PermissionError("tool discovery scope does not match this run")
        version = getattr(context, "capability_snapshot_id", None)
        if version is None:
            return current
        view = next(
            (item for item in self.revisions if item.snapshot.snapshot_id == version),
            None,
        )
        if view is None:
            raise PermissionError("unknown provider capability version")
        return view

    def stage_service_tools(
        self,
        context: ToolContext,
        *,
        grant_id: str,
        parent_snapshot_id: str,
        tools: Sequence[AgentTool],
        activate: bool = False,
    ) -> tuple[str, ...]:
        from .capabilities import _freeze_json

        self.view(context)
        context.signal.raise_if_cancelled()
        grant = self.grants.get(grant_id)
        if grant is None or grant.role_id != context.role_id:
            raise PermissionError("service was not authorized at run start")
        if parent_snapshot_id != self.current.snapshot.snapshot_id:
            raise ValueError("service capability revision has a stale parent")
        if not context.tool_call_id or not tools:
            raise ValueError(
                "service revision requires a successful host tool call and concrete tools"
            )
        definitions = []
        names = set(self.definitions) | set(self.reserved_names)
        seen: set[str] = set()
        for tool in tools:
            definition = tool.definition
            if definition.name in seen or definition.name in self.definitions or (definition.name in names and self.name_sources.get(definition.name) != f"mcp:{grant.service_id}"):
                raise ValueError("service tool name collides with the frozen directory")
            names.add(definition.name)
            seen.add(definition.name)
            if (
                definition.source != f"mcp:{grant.service_id}"
                or definition.risk not in grant.allowed_risks
                or definition.approval != "auto"
                or self.external_only
                and not (grant.external_allowed and definition.external_allowed)
            ):
                raise PermissionError(
                    "service tool exceeds the frozen source/risk/origin grant"
                )
            definitions.append(
                replace(
                    definition,
                    exposure="deferred",
                    external_allowed=grant.external_allowed
                    and definition.external_allowed,
                    input_schema=cast(
                        dict[str, object], _freeze_json(definition.input_schema)
                    ),
                )
            )
        self.extensions[context.tool_call_id] = _ServiceExtension(
            parent_snapshot_id, tuple(tools), tuple(definitions)
        )
        if activate:
            self.pending[context.tool_call_id] = tuple(item.name for item in definitions)
        return tuple(item.name for item in definitions)

    def stage_service_removal(self, context: ToolContext, *, grant_id: str, parent_snapshot_id: str) -> None:
        self.view(context)
        context.signal.raise_if_cancelled()
        grant = self.grants.get(grant_id)
        if grant is None or grant.role_id != context.role_id:
            raise PermissionError("service was not authorized at run start")
        if parent_snapshot_id != self.current.snapshot.snapshot_id or not context.tool_call_id:
            raise ValueError("service removal requires a current parent and host call")
        self.removals[context.tool_call_id] = (parent_snapshot_id, f"mcp:{grant.service_id}")

    def propose(self, call_id: str) -> CapabilityResolution | None:
        from .capabilities import (
            _thaw_json,
            _snapshot_generation,
            _candidate_catalog_id,
        )

        names = self.pending.pop(call_id, ())
        extension = self.extensions.pop(call_id, None)
        removal = self.removals.pop(call_id, None)
        current = self.current
        candidate_definitions = self.definitions
        candidate_tools = dict(self.tools)
        removed: set[str] = set()
        if removal is not None:
            if removal[0] != current.snapshot.snapshot_id:
                raise ValueError("service capability revision has a stale parent")
            removed = {name for name, definition in candidate_definitions.items() if definition.source == removal[1]}
            for name in removed:
                candidate_definitions.pop(name)
                candidate_tools.pop(name, None)
        if extension is not None:
            if extension.parent != current.snapshot.snapshot_id:
                raise ValueError("service capability revision has a stale parent")
            candidate_definitions.update(
                {item.name: item for item in extension.definitions}
            )
            candidate_tools.update(
                {
                    definition.name: tool
                    for definition, tool in zip(
                        extension.definitions, extension.tools, strict=True
                    )
                }
            )
        visible = {item.name for item in current.snapshot.tools}
        added = [name for name in names if name not in visible]
        added = [name for name in added if name in candidate_definitions]
        if not added and extension is None and not removed:
            return None
        definitions = (
            *(item for item in current.snapshot.tools if item.name not in removed),
            *(candidate_definitions[name] for name in added),
        )
        version = current.snapshot.visibility_version + 1
        decisions = {
            **current.snapshot.policy_decisions,
            **{name: "allowed" for name in added},
            **{name: "denied:service_removed" for name in removed},
        }
        if extension is not None:
            decisions.update(
                {item.name: "denied:deferred" for item in extension.definitions if item.name not in added}
            )
        snapshot = replace(
            current.snapshot,
            snapshot_id=f"cap-{current.snapshot.run_id}-v{version}",
            visibility_version=version,
            parent_snapshot_id=current.snapshot.snapshot_id,
            tools=definitions,
            policy_decisions=MappingProxyType(decisions),
            candidates=tuple(candidate_definitions.values()),
        )
        snapshot = replace(
            snapshot,
            generation=_snapshot_generation(
                tools=snapshot.tools,
                skills=snapshot.skills,
                plugins=snapshot.plugins,
                policy_decisions=snapshot.policy_decisions,
            ),
        )
        if extension is not None or removed:
            snapshot = replace(
                snapshot, candidate_catalog_id=_candidate_catalog_id(snapshot)
            )
        denied = {
            name: reason
            for name, reason in current.denied_tools.items()
            if name not in added
        }
        denied.update({name: "MCP service connection was removed" for name in removed})
        if extension is not None:
            denied.update(
                {
                    item.name: f"tool is deferred; use tool_search(query='select:{item.name}')"
                    for item in extension.definitions if item.name not in added
                }
            )
        return replace(
            current,
            snapshot=snapshot,
            tools=(*(tool for tool in current.tools if tool.definition.name not in removed), *(candidate_tools[name] for name in added)),
            denied_tools=MappingProxyType(denied),
            _candidate_tools=MappingProxyType(candidate_tools),
            _tools_by_name=MappingProxyType(
                {
                    **{name: tool for name, tool in current._tools_by_name.items() if name not in removed},
                    **{name: candidate_tools[name] for name in added},
                }
            ),
            _definitions_by_name=MappingProxyType(
                {
                    **{name: definition for name, definition in current._definitions_by_name.items() if name not in removed},
                    **{
                        name: replace(
                            candidate_definitions[name],
                            input_schema=cast(
                                dict[str, object],
                                _thaw_json(candidate_definitions[name].input_schema),
                            ),
                        )
                        for name in added
                    },
                }
            ),
        )

    def publish(self, revision: CapabilityResolution) -> None:
        if (
            revision._discovery is not self
            or revision.snapshot.parent_snapshot_id != self.current.snapshot.snapshot_id
        ):
            raise ValueError("capability revision parent does not match")
        self.revisions.append(revision)
        self.name_sources.update({item.name: item.source for item in revision.snapshot.candidates})


class ToolSearchTool:
    definition = ToolDefinition(
        "tool_search",
        "搜索已授权工具；select:A,B 精确加载。持久化后下一次模型请求可调用。",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 2000},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
                "allowed_risk": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {
                        "type": "string",
                        "enum": ["read-only", "write", "external-side-effect"],
                    },
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        source="builtin:discovery",
        external_allowed=True,
        timeout_seconds=5,
        output_limit=16000,
    )

    def __init__(self, discovery: DiscoveryState) -> None:
        self.discovery = discovery

    async def execute(
        self, arguments: Mapping[str, object], context: ToolContext, on_update=None
    ) -> AgentToolResult:
        context.signal.raise_if_cancelled()
        ToolRegistry.validate_definition(self.definition, dict(arguments))
        view = self.discovery.view(context)
        query = cast(str, arguments["query"]).strip()
        visible = {item.name for item in view.snapshot.tools}
        risks = {
            "read-only": "read_only",
            "write": "mutating",
            "external-side-effect": "external",
        }
        allowed = {
            risks[value] for value in cast(list[str], arguments.get("allowed_risk", []))
        }
        eligible = {
            definition.name: definition
            for definition in view.snapshot.candidates
            if (not allowed or definition.risk in allowed)
            and view.authorization_denial(definition, context) is None
        }
        explanations: dict[str, list[str]] = {}
        if query.lower().startswith("select:"):
            names = list(
                dict.fromkeys(
                    name.strip() for name in query[7:].split(",") if name.strip()
                )
            )
            if len(names) > 50:
                raise ValueError("select accepts at most 50 tool names")
            found = [name for name in names if name in eligible and name not in visible]
            loaded = [name for name in names if name in eligible and name in visible]
        else:
            terms = _search_terms(query)
            ranked = []
            for name, definition in eligible.items():
                context.signal.raise_if_cancelled()
                if name in visible:
                    continue
                if query in eligible and query not in visible:
                    if name == query:
                        ranked = [(1, name)]
                        explanations[name] = ["名称:精确匹配"]
                        break
                    continue
                score = _keyword_score(definition, terms)
                if score:
                    ranked.append((score, name))
                    explanations[name] = _match_reasons(definition, terms)
            ranked.sort(key=lambda item: (-item[0], item[1]))
            found = [name for _, name in ranked[: cast(int, arguments.get("top_k", 5))]]
            loaded = []
        matched: list[dict[str, object]] = [
            {
                "name": name,
                "description": eligible[name].description[:160],
                "summary": eligible[name].description[:120],
                "why_matched": explanations.get(name, ["名称:精确匹配"]),
                "always_on": eligible[name].exposure != "deferred",
                "risk": next(
                    key for key, value in risks.items() if value == eligible[name].risk
                ),
            }
            for name in found
        ]
        for item in matched:
            reasons = cast(list[str], item["why_matched"])
            if len(reasons) > 8 or any(len(reason) > 160 for reason in reasons):
                item["why_matched"] = [reason if len(reason) <= 160 else reason[:159] + "…" for reason in reasons[:8]]
                item["explanation_truncated"] = True
        payload: dict[str, object] = {
            "matched": matched,
            "unlocked": found,
            "already_loaded": loaded,
            "tip": "结果持久化后下一次模型请求加载完整 schema；直接调用 unlocked 工具。"
            if found
            else "已加载，可直接调用。"
            if loaded
            else "没有匹配的已授权工具；检查名称、风险过滤或换关键词。",
        }
        content = json.dumps(payload, ensure_ascii=False)
        output_limit = self.definition.output_limit
        assert output_limit is not None
        for description_limit in (80, 40, 0):
            if len(content) <= output_limit:
                break
            payload["truncated"] = True
            for item in matched:
                item["description"] = cast(str, item["description"])[:description_limit]
                item["summary"] = cast(str, item["summary"])[:description_limit]
            content = json.dumps(payload, ensure_ascii=False)
        while len(content) > output_limit and (found or loaded):
            payload["truncated"] = True
            if found:
                found.pop()
                matched.pop()
            else:
                loaded.pop()
            content = json.dumps(payload, ensure_ascii=False)
        self.discovery.pending[context.tool_call_id] = tuple(found)
        return AgentToolResult(content, details=payload)


def _search_terms(query: str) -> set[str]:
    """Shiori's keyword normalization, including mixed text and CJK bigrams.

    Adapted from Shiori-Agent 4f266ae search_backend.py under MIT;
    see third_party/shiori-agent/LICENSE.
    """
    lowered = query.lower().strip()
    terms = {lowered, *lowered.split()}
    terms.update(segment.strip() for segment in re.split(r"([\u4e00-\u9fff]+)", lowered))
    cjk = [character for character in lowered if "\u4e00" <= character <= "\u9fff"]
    terms.update(cjk)
    terms.update(left + right for left, right in zip(cjk, cjk[1:]))
    terms.discard("")
    return terms


def _keyword_score(definition: ToolDefinition, terms: set[str]) -> int:
    """Fixed Shiori name-part, MCP, hint and description weights (MIT)."""
    name = definition.name.lower()
    parts = [part for part in name.split("_") if part]
    hint = definition.search_hint.lower()
    description = definition.description.lower()
    mcp = definition.source.startswith("mcp:")
    score = 0
    for term in terms:
        if term in parts:
            score += 12 if mcp else 10
        elif any(term in part or part in term for part in parts):
            score += 6 if mcp else 5
        elif term in name:
            score += 3
        if hint and term in hint:
            score += 4
        if term in description:
            score += 2
    return score


def _match_reasons(definition: ToolDefinition, terms: set[str]) -> list[str]:
    """Explain matching fields independently of numeric ranking."""
    name = definition.name.lower()
    parts = [part for part in name.split("_") if part]
    hint = definition.search_hint.lower()
    description = definition.description.lower()
    reasons = []
    for term in sorted(terms):
        if term in parts:
            reasons.append(f"名称精确:{term}")
        elif any(term in part or part in term for part in parts):
            reasons.append(f"名称部分:{term}")
        elif term in name:
            reasons.append(f"名称:{term}")
        if hint and term in hint:
            reasons.append(f"提示:{term}")
        if term in description:
            reasons.append(f"描述:{term}")
    return reasons
