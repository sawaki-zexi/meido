"""Run-local discovery: freeze admission, stage activation, publish after persistence."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from collections.abc import Mapping, Sequence

from .tools import AgentTool, AgentToolResult, ToolContext, ToolDefinition, ToolRegistry
from .search_metadata import SearchDocument, build_search_document
from .search_budget import select_schema_budget

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
        self._current: CapabilityResolution | None = None
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
        self._current = initial
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
        assert self._current is not None
        return self._current

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
        view = context.capability_binding
        if (view is None and version == current.snapshot.snapshot_id):
            view = current
        if (view is None or view._discovery is not self
                or view.snapshot.snapshot_id != version):
            raise PermissionError("unknown provider capability binding")
        return view

    def stage_service_tools(
        self,
        context: ToolContext,
        *,
        grant_id: str,
        parent_snapshot_id: str,
        tools: Sequence[AgentTool],
        activate: bool = False,
        rebind: bool = False,
    ) -> tuple[str, ...]:
        from .capabilities import _freeze_json, _definition_payload

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
            if (definition.name in seen or definition.name in self.definitions and not rebind
                    or definition.name in names and self.name_sources.get(definition.name) != f"mcp:{grant.service_id}"):
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
        if rebind:
            expected = {name: item for name, item in self.definitions.items() if item.source == f"mcp:{grant.service_id}"}
            if ({item.name for item in definitions} != set(expected)
                    or any(_definition_payload(item) != _definition_payload(expected[item.name]) for item in definitions)):
                raise ValueError("service reconnect changed an admitted definition; start a new run")
        if activate:
            # Direct MCP additions/Drift mounts still obey the cumulative bound.
            # These callers promise the complete declared set, so fail atomically
            # rather than silently publishing only part of a service contract.
            from .search_budget import select_schema_budget
            visible = self.current.snapshot.tools
            pending_names = {name for key, values in self.pending.items() if key != context.tool_call_id for name in values}
            pending_definitions = {**self.definitions, **{item.name: item for extension in self.extensions.values() for item in extension.definitions}}
            selected = select_schema_budget(definitions,
                baseline=[item for item in visible if item.exposure != "deferred"],
                loaded=[item for item in visible if item.exposure == "deferred"],
                pending=[pending_definitions[name] for name in pending_names if name in pending_definitions],
                new_limit=None)
            if selected.skipped:
                raise ValueError("MCP tool declarations exceed the cumulative schema budget")
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
            definition_fingerprint,
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
        rebound = {item.name for item in extension.definitions if item.name in visible} if extension else set()
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
                {item.name: "denied:deferred" for item in extension.definitions if item.name not in added and item.name not in rebound}
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
                    for item in extension.definitions if item.name not in added and item.name not in rebound
                }
            )
        return replace(
            current,
            snapshot=snapshot,
            tools=tuple(candidate_tools[item.name] for item in definitions),
            denied_tools=MappingProxyType(denied),
            _candidate_tools=MappingProxyType(candidate_tools),
            _executors_by_name=MappingProxyType({**{name: method for name, method in current._executors_by_name.items() if name not in removed},
                **({tool.definition.name: tool.execute for tool in extension.tools} if extension else {})}),
            _definition_fingerprints=MappingProxyType({**{name: value for name, value in current._definition_fingerprints.items() if name not in removed},
                **({tool.definition.name: definition_fingerprint(tool.definition) for tool in extension.tools} if extension else {})}),
            _tools_by_name=MappingProxyType(
                {
                    **{name: tool for name, tool in current._tools_by_name.items() if name not in removed},
                    **{name: candidate_tools[name] for name in (*added, *rebound)},
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
                        for name in (*added, *rebound)
                    },
                }
            ),
        )

    def discard_pending(self, call_id: str | None = None) -> None:
        if call_id is None:
            self.pending.clear()
            self.extensions.clear()
            self.removals.clear()
        else:
            self.pending.pop(call_id, None)
            self.extensions.pop(call_id, None)
            self.removals.pop(call_id, None)

    def publish(self, revision: CapabilityResolution) -> None:
        if (
            revision._discovery is not self
            or revision.snapshot.parent_snapshot_id != self.current.snapshot.snapshot_id
        ):
            raise ValueError("capability revision parent does not match")
        self._current = revision
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
        current = self.discovery.current
        authorized = {
            item.name: item for item in current.snapshot.candidates
            if current.authorization_denial(item, context) is None
        }
        eligible = {
            definition.name: definition
            for definition in view.snapshot.candidates
            if (not allowed or definition.risk in allowed)
            and authorized.get(definition.name) == definition
            and view.authorization_denial(definition, context) is None
        }
        explanations: dict[str, list[str]] = {}
        documents: dict[str, SearchDocument] = {}
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
                documents[name] = build_search_document(definition)
                score = _keyword_score(definition, terms, documents[name])
                if score:
                    ranked.append((score, name))
                    explanations[name] = _match_reasons(definition, terms, documents[name])
            ranked.sort(key=lambda item: (-item[0], item[1]))
            found = [name for _, name in ranked[: cast(int, arguments.get("top_k", 5))]]
            loaded = []
        # Account against committed current state, not just this request's older
        # execution view. Pending names reserve once across same-reply searches.
        # Already-issued declarations still occupy provider context after revocation.
        # Count their bytes, but never expose their names/metadata as new results.
        current_definitions = list(current.snapshot.tools)
        baseline = [item for item in current_definitions if item.exposure != "deferred"]
        loaded_definitions = [item for item in current_definitions if item.exposure == "deferred"]
        pending = [authorized[name] for call_id, names in self.discovery.pending.items()
                   if call_id != context.tool_call_id for name in names if name in authorized]
        selection = select_schema_budget([eligible[name] for name in found],
            baseline=baseline, loaded=loaded_definitions, pending=pending)
        found = [item.name for item in selection.selected]
        skipped = [item.name for item in selection.skipped]
        rejected_count = len(skipped)
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
            name = cast(str, item["name"])
            document = documents.get(name) or build_search_document(eligible[name])
            if document.diagnostics:
                item["metadata_diagnostics"] = list(document.diagnostics)
            reasons = cast(list[str], item["why_matched"])
            if len(reasons) > 8 or any(len(reason) > 160 for reason in reasons):
                item["why_matched"] = [reason if len(reason) <= 160 else reason[:159] + "…" for reason in reasons[:8]]
                item["explanation_truncated"] = True
        payload: dict[str, object] = {
            "matched": matched,
            "unlocked": found,
            "already_loaded": loaded,
            "skipped_due_to_budget": skipped,
            "budget": selection.report,
            "tip": "结果持久化后下一次模型请求加载完整 schema；直接调用 unlocked 工具。"
            if found
            else "已加载，可直接调用。"
            if loaded
            else "匹配工具超过声明预算；现有工具仍可使用。"
            if skipped
            else "没有匹配的已授权工具；检查名称、风险过滤或换关键词。",
        }
        def render() -> str:
            # Output trimming is part of selection: removed rows reserve no bytes.
            measured = select_schema_budget([eligible[name] for name in found],
                baseline=baseline, loaded=loaded_definitions, pending=pending)
            payload["budget"] = {**measured.report, "rejected_count": rejected_count}
            return json.dumps(payload, ensure_ascii=False)

        content = render()
        output_limit = self.definition.output_limit
        assert output_limit is not None
        for description_limit in (80, 40, 0):
            if len(content) <= output_limit:
                break
            payload["truncated"] = True
            for item in matched:
                item["description"] = cast(str, item["description"])[:description_limit]
                item["summary"] = cast(str, item["summary"])[:description_limit]
            content = render()
        while len(content) > output_limit and (found or loaded or skipped):
            payload["truncated"] = True
            if found:
                found.pop()
                matched.pop()
            elif loaded:
                loaded.pop()
            else:
                skipped.pop()
                payload["skipped_names_truncated"] = True
            content = render()
        context.signal.raise_if_cancelled()
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


def _keyword_score(definition: ToolDefinition, terms: set[str], document: SearchDocument | None = None) -> int:
    """Fixed Shiori name-part, MCP, hint and description weights (MIT)."""
    document = document or build_search_document(definition)
    name = document.name
    parts = [part for part in name.split("_") if part]
    hint = document.hint
    description = document.description
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
        if term in document.parameters:
            score += 3
    # A service's shared summary is weak routing evidence, not 100 copies of
    # authoritative instructions that can outrank exact names/parameter hits.
    score += min(2, sum(term in document.namespace for term in terms))
    return score


def _match_reasons(definition: ToolDefinition, terms: set[str], document: SearchDocument | None = None) -> list[str]:
    """Explain matching fields independently of numeric ranking."""
    document = document or build_search_document(definition)
    name = document.name
    parts = [part for part in name.split("_") if part]
    hint = document.hint
    description = document.description
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
        if term in document.parameters:
            reasons.append(f"参数:{term}")
        if term in document.namespace:
            reasons.append(f"namespace:{term}")
    return reasons
