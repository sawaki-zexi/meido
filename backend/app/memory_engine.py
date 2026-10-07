"""Stable memory port shared by dialogue, tools, and administrative callers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import math
from enum import Enum
from types import MappingProxyType
import asyncio
import uuid
import inspect
from typing import Callable, Literal, Protocol, runtime_checkable

from .memory_service import MemoryService
from .memory_events import RetrievalCompleted
from .models import MemoryItem, MemorySourceRef


MemoryQueryIntent = Literal["context", "answer", "timeline", "interest", "procedure"]
MemoryQueryEffect = Literal["stateful", "read_only"]
MemoryDomain = Literal["role_self", "relationship", "shared"]


@dataclass(frozen=True)
class ProcedureRule:
    """Portable rule metadata for safe procedure memory injection."""

    required_tags: tuple[str, ...] = ()
    min_confidence: float = 0.0
    requires_explicit_user_signal: bool = False

    def as_hint(self) -> dict[str, object]:
        return {
            "requiredTags": list(self.required_tags),
            "minConfidence": max(0.0, min(1.0, self.min_confidence)),
            "requiresExplicitUserSignal": self.requires_explicit_user_signal,
        }


class EngineProfile(str, Enum):
    RICH_MEMORY_ENGINE = "rich_memory_engine"
    CLASSIC_MEMORY_SERVICE = "classic_memory_service"
    WORKFLOW_MEMORY_ENGINE = "workflow_memory_engine"
    CONTEXT_RESOURCE_ENGINE = "context_resource_engine"


class MemoryCapability(str, Enum):
    INGEST_TEXT = "ingest.text"
    INGEST_MESSAGES = "ingest.messages"
    INGEST_RESOURCE = "ingest.resource"
    RETRIEVE_SEMANTIC = "retrieve.semantic"
    RETRIEVE_CONTEXT_BLOCK = "retrieve.context_block"
    RETRIEVE_STRUCTURED_HITS = "retrieve.structured_hits"
    MANAGE_HISTORY = "manage.history"
    MANAGE_UPDATE = "manage.update"
    MANAGE_DELETE = "manage.delete"
    ENRICH_GRAPH_RELATIONS = "enrich.graph_relations"
    SEMANTICS_RICH_MEMORY = "semantics.rich_memory"


@dataclass(frozen=True)
class MemoryScope:
    """Required role and single-session scope for every memory operation."""

    role_id: str
    session_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.role_id, str) or not self.role_id.strip():
            raise ValueError("role_id is required for memory operations")
        if not isinstance(self.session_key, str) or not self.session_key.strip():
            raise ValueError("session_key is required for memory operations")
        if self.session_key != f"role:{self.role_id}":
            raise ValueError("session_key does not belong to role_id")


@dataclass(frozen=True)
class MemoryEngineDescriptor:
    name: str
    profile: EngineProfile
    capabilities: frozenset[MemoryCapability]
    notes: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "notes", MappingProxyType(dict(self.notes)))


@dataclass
class MemoryIngestRequest:
    content: object
    source_kind: str
    scope: MemoryScope
    hints: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class MemoryIngestResult:
    accepted: bool
    created_ids: list[str] = field(default_factory=list)
    summary: str = ""
    raw: dict[str, object] = field(default_factory=dict)


@dataclass
class EvidenceRef:
    kind: Literal["message", "message_range", "turn", "consolidation", "external"] = "message"
    refs: list[str] = field(default_factory=list)
    resolver: str = "session"
    source_ref: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class MemoryRecord:
    id: str
    kind: str
    summary: str
    score: float
    engine_kind: str
    evidence: list[EvidenceRef] = field(default_factory=list)
    source: dict[str, object] = field(default_factory=dict)
    signals: dict[str, object] = field(default_factory=dict)
    domain: str = ""
    extra: dict[str, object] = field(default_factory=dict)
    happened_at: str | None = None
    status: str = "active"
    emotional_weight: int = 0
    has_embedding: bool = False
    injected: bool = False


@dataclass(frozen=True)
class MemoryQueryFilters:
    kinds: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    time_start: datetime | None = None
    time_end: datetime | None = None
    hints: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "kinds", tuple(str(value) for value in self.kinds if str(value).strip()))
        object.__setattr__(self, "domains", tuple(str(value) for value in self.domains if str(value).strip()))
        object.__setattr__(self, "hints", MappingProxyType(dict(self.hints)))


@dataclass
class MemoryQuery:
    text: str
    intent: MemoryQueryIntent = "answer"
    effect: MemoryQueryEffect = "stateful"
    scope: MemoryScope | None = None
    filters: MemoryQueryFilters = field(default_factory=MemoryQueryFilters)
    context: dict[str, object] = field(default_factory=dict)
    limit: int = 8
    timestamp: datetime | None = None


@dataclass
class MemoryQueryResult:
    text_block: str = ""
    records: list[MemoryRecord] = field(default_factory=list)
    trace: dict[str, object] = field(default_factory=dict)
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryMutation:
    kind: Literal["remember", "forget", "reject", "state_change", "update", "delete"]
    scope: MemoryScope | None = None
    summary: str = ""
    memory_kind: str = ""
    memory_domain: str = ""
    source_ref: str = ""
    happened_at: str = ""
    status: str = ""
    ids: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "ids", tuple(value for item in self.ids if (value := str(item).strip())))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass
class MemoryMutationResult:
    accepted: bool
    item_id: str = ""
    actual_kind: str = ""
    status: str = ""
    affected_ids: list[str] = field(default_factory=list)
    missing_ids: list[str] = field(default_factory=list)
    items: list[dict[str, object]] = field(default_factory=list)
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryToolSpec:
    description: str
    parameters: dict[str, object]
    name: str = ""
    risk: Literal["read-only", "write", "external-side-effect"] = "read-only"
    search_hint: str = ""
    tool_class: type | None = field(default=None, compare=False, hash=False)


@dataclass(frozen=True)
class MemoryToolProfile:
    recall: MemoryToolSpec | None = None
    memorize: MemoryToolSpec | None = None
    forget: MemoryToolSpec | None = None
    tools: tuple[MemoryToolSpec, ...] = ()


@runtime_checkable
class MemoryIngestApi(Protocol):
    async def ingest(self, request: MemoryIngestRequest) -> MemoryIngestResult: ...


@runtime_checkable
class MemoryRetrievalApi(Protocol):
    async def query(self, request: MemoryQuery) -> MemoryQueryResult: ...


@runtime_checkable
class MemoryWriteApi(Protocol):
    async def mutate(self, request: MemoryMutation) -> MemoryMutationResult: ...

    def reinforce_items_batch(self, scope: MemoryScope, ids: list[str]) -> None: ...


@runtime_checkable
class MemoryAdminApi(Protocol):
    def list_role_filter_values(self, role_id: str) -> dict[str, list[str]]: ...

    def list_active_items(self, role_id: str, query: str = "") -> list[MemoryItem]: ...

    def describe(self) -> MemoryEngineDescriptor: ...

    def tool_profile(self) -> MemoryToolProfile: ...

    def list_events_by_time_range(
        self, role_id: str, time_start: datetime, time_end: datetime, *, limit: int = 200
    ) -> list[dict[str, object]]: ...

    def list_items_for_admin(
        self,
        *,
        role_id: str,
        q: str = "",
        memory_type: str = "",
        memory_domain: str = "",
        status: str = "",
        source_ref: str = "",
        has_embedding: bool | None = None,
        page: int = 1,
        page_size: int = 50,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[dict[str, object]], int]: ...

    def get_item_for_admin(
        self, role_id: str, item_id: str, *, include_embedding: bool = False
    ) -> dict[str, object] | None: ...

    def update_item_for_admin(
        self,
        role_id: str,
        item_id: str,
        *,
        status: str | None = None,
        extra_json: dict[str, object] | None = None,
        source_ref: str | None = None,
        happened_at: str | None = None,
        emotional_weight: int | None = None,
    ) -> dict[str, object] | None: ...

    def delete_item(self, role_id: str, item_id: str) -> bool: ...

    def delete_items_batch(self, role_id: str, ids: list[str]) -> int: ...

    def invalidate_role_memories(self, role_id: str) -> int: ...

    def find_similar_items_for_admin(
        self,
        role_id: str,
        item_id: str,
        *,
        top_k: int = 8,
        memory_type: str = "",
        score_threshold: float = 0.0,
        include_superseded: bool = False,
    ) -> list[dict[str, object]]: ...


@runtime_checkable
class MemoryEngine(MemoryIngestApi, MemoryRetrievalApi, MemoryWriteApi, MemoryAdminApi, Protocol):
    pass


class DefaultMemoryEngine:
    """Meido's role-isolated engine over the SQLite store and retrieval service."""

    DEFAULT_POLICIES: dict[str, dict[str, object]] = {
        "context": {"min_lane_score": 0.15, "type_limits": {}},
        "answer": {"min_lane_score": 0.10, "type_limits": {}},
        "timeline": {"min_lane_score": 0.0, "type_limits": {"event": 8}},
        "interest": {"min_lane_score": 0.10, "type_limits": {"preference": 4, "profile": 4}},
        "procedure": {"min_lane_score": 0.10, "type_limits": {"procedure": 4, "preference": 2}},
    }

    def __init__(
        self,
        service: MemoryService,
        role_exists: Callable[[str], bool],
        event_bus: object | None = None,
        hyde_provider: Callable[..., list[str]] | None = None,
    ) -> None:
        self.service = service
        self.store = service.store
        self._role_exists = role_exists
        self.event_bus = event_bus
        self.hyde_provider = hyde_provider
        self.admin = self

    def _validate_scope(self, scope: MemoryScope | None) -> MemoryScope:
        if scope is None:
            raise ValueError("MemoryScope with role_id is required")
        if not self._role_exists(scope.role_id):
            raise ValueError(f"unknown role_id: {scope.role_id}")
        return scope

    def describe(self) -> MemoryEngineDescriptor:
        return MemoryEngineDescriptor(
            name="meido-sqlite-memory",
            profile=EngineProfile.RICH_MEMORY_ENGINE,
            capabilities=frozenset(
                {
                    MemoryCapability.INGEST_TEXT,
                    MemoryCapability.INGEST_MESSAGES,
                    MemoryCapability.RETRIEVE_SEMANTIC,
                    MemoryCapability.RETRIEVE_CONTEXT_BLOCK,
                    MemoryCapability.RETRIEVE_STRUCTURED_HITS,
                    MemoryCapability.MANAGE_HISTORY,
                    MemoryCapability.MANAGE_UPDATE,
                    MemoryCapability.MANAGE_DELETE,
                    MemoryCapability.SEMANTICS_RICH_MEMORY,
                }
            ),
        )

    def tool_profile(self) -> MemoryToolProfile:
        return MemoryToolProfile(
            recall=MemoryToolSpec(
                name="recall_memory",
                description="检索当前角色的相关记忆",
                parameters={"type": "object", "properties": {"query": {"type": "string"}}},
                risk="read-only",
            ),
            memorize=MemoryToolSpec(
                name="memorize",
                description="保存用户明确要求记住的信息",
                parameters={"type": "object", "properties": {"summary": {"type": "string"}}},
                risk="write",
            ),
            forget=MemoryToolSpec(
                name="forget_memory",
                description="忘记当前角色的一条或多条记忆",
                parameters={"type": "object", "properties": {"ids": {"type": "array"}}},
                risk="write",
            ),
        )

    async def query(self, request: MemoryQuery) -> MemoryQueryResult:
        scope = self._validate_scope(request.scope)
        if request.limit < 0:
            raise ValueError("limit must be non-negative")
        if request.intent == "timeline" and (
            request.filters.time_start is None or request.filters.time_end is None
        ):
            if self.event_bus is not None:
                self.event_bus.publish(RetrievalCompleted(
                    scope.role_id,
                    scope.session_key,
                    request.text,
                    request.intent,
                    0,
                    error="timeline queries require time_start and time_end",
                ))
            raise ValueError("timeline queries require time_start and time_end")
        if request.limit == 0:
            return MemoryQueryResult(
                trace={"intent": request.intent, "effect": request.effect, "role_id": scope.role_id, "lanes": []},
                raw={"candidate_count": 0, "items": []},
            )

        lanes = self._query_lanes(request)
        if request.intent == "answer" and self.hyde_provider is not None and request.text.strip() and not request.filters.hints.get("hyde_queries"):
            try:
                try:
                    accepts_scope = len(inspect.signature(self.hyde_provider).parameters) >= 2
                except (TypeError, ValueError):
                    accepts_scope = False
                if accepts_scope:
                    generated = await asyncio.to_thread(self.hyde_provider, scope.role_id, request.text.strip())
                else:
                    generated = await asyncio.to_thread(self.hyde_provider, request.text.strip())
                if isinstance(generated, list):
                    hypotheses = [value.strip() for value in generated if isinstance(value, str) and value.strip()][:2]
                    if hypotheses:
                        lanes = [request.text.strip(), *hypotheses]
            except Exception:
                pass
        lane_results = await asyncio.gather(
            *(
                self._retrieve_lane(
                    scope.role_id,
                    text,
                    max(request.limit * 4, 16),
                    semantic_only=request.intent == "answer" and index > 0,
                )
                for index, text in enumerate(lanes)
            ),
            return_exceptions=True,
        )
        fused: dict[str, tuple[MemoryItem, float, dict[str, object]]] = {}
        lane_trace: list[dict[str, object]] = []
        raw_unique_ids: set[str] = set()
        filtered_occurrences = 0
        for lane_index, (lane, result) in enumerate(zip(lanes, lane_results, strict=True)):
            if isinstance(result, BaseException):
                lane_trace.append({
                    "query": lane,
                    "candidate_count": 0,
                    "filtered_count": 0,
                    "semantic_only": request.intent == "answer" and lane_index > 0,
                    "error": str(result),
                })
                continue
            lane_filtered = 0
            lane_trace_entry = {
                "query": lane,
                "candidate_count": len(result),
                # Retain the legacy field while exposing the unambiguous name.
                "rank": len(result),
                "semantic_only": request.intent == "answer" and lane_index > 0,
            }
            for rank, item in enumerate(result, 1):
                raw_unique_ids.add(item.id)
                if not self._matches_filters(item, request):
                    lane_filtered += 1
                    filtered_occurrences += 1
                    continue
                # RRF is used only for ordering; source lane/rank remain observable.
                contribution = 1.0 / (60 + rank)
                lane_hit = {
                    "query": lane,
                    "rank": rank,
                    "lane_score": 1.0 / rank,
                    "rrf_contribution": contribution,
                    "semantic_only": request.intent == "answer" and lane_index > 0,
                }
                current = fused.get(item.id)
                if current is None:
                    fused[item.id] = (
                        item,
                        contribution,
                        {
                            "lanes": [lane],
                            "lane_ranks": {lane: rank},
                            "lane_score": 1.0 / rank,
                            "lane_hits": [lane_hit],
                        },
                    )
                else:
                    current[2]["lanes"].append(lane)
                    current[2]["lane_ranks"][lane] = rank
                    current[2]["lane_hits"].append(lane_hit)
                    current[2]["lane_score"] = max(float(current[2].get("lane_score", 0.0)), 1.0 / rank)
                    fused[item.id] = (item, current[1] + contribution, current[2])
            lane_trace_entry["filtered_count"] = lane_filtered
            lane_trace.append(lane_trace_entry)

        # RRF remains the relevance score. Hotness is only a stable tie-breaker
        # and can never promote a weak lane match over a relevant candidate.
        ranked = sorted(
            fused.values(),
            key=lambda value: (value[1], self._hotness(value[0]), value[0].updatedAt),
            reverse=True,
        )
        if request.intent == "timeline":
            ranked.sort(key=lambda value: value[0].happenedAt or value[0].updatedAt, reverse=True)
        policy = self.DEFAULT_POLICIES.get(request.intent, self.DEFAULT_POLICIES["answer"])
        try:
            requested_min_score = float(request.filters.hints.get("min_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            requested_min_score = 0.0
        min_score = max(float(policy.get("min_lane_score", 0.0)), requested_min_score)
        try:
            min_score_value = max(0.0, float(min_score))
        except (TypeError, ValueError):
            min_score_value = 0.0
        threshold_selected = [entry for entry in ranked if float(entry[2].get("lane_score", 0.0)) >= min_score_value]
        below_threshold_count = len(ranked) - len(threshold_selected)
        selected = threshold_selected
        type_limits = dict(policy.get("type_limits", {}))
        requested_limits = request.filters.hints.get("type_limits", {})
        if isinstance(requested_limits, Mapping):
            for memory_type, requested_limit in requested_limits.items():
                try:
                    # Hints may tighten a default quota, never widen it.
                    requested_value = max(0, int(requested_limit))
                except (TypeError, ValueError):
                    continue
                default_value = type_limits.get(memory_type)
                type_limits[memory_type] = min(int(default_value), requested_value) if default_value is not None else requested_value
        quota_excluded_count = 0
        if isinstance(type_limits, Mapping):
            counts: dict[str, int] = {}
            limited: list[tuple[MemoryItem, float, dict[str, object]]] = []
            for entry in selected:
                memory_type = entry[0].memoryType
                limit_for_type = type_limits.get(memory_type)
                if limit_for_type is not None:
                    try:
                        type_limit = max(0, int(limit_for_type))
                    except (TypeError, ValueError):
                        type_limit = 0
                    if counts.get(memory_type, 0) >= type_limit:
                        quota_excluded_count += 1
                        continue
                    counts[memory_type] = counts.get(memory_type, 0) + 1
                limited.append(entry)
            selected = limited
        limited_selected = selected[: request.limit]
        limit_excluded_count = len(selected) - len(limited_selected)
        selected = limited_selected
        items = [item for item, _, _ in selected]
        records = [
            self._record(
                item,
                index,
                score=score,
                signals={
                    **signals,
                    "rrf_score": score,
                    "hotness": self._hotness(item),
                    "reasons": [
                        *(
                            "semantic match" if hit["semantic_only"] else "lexical match"
                            for hit in signals.get("lane_hits", [])
                        ),
                        "hotness considered as tie-breaker",
                    ],
                },
            )
            for index, (item, score, signals) in enumerate(selected)
        ]
        budget_hint = request.filters.hints.get("max_chars")
        try:
            budget = min(4000, max(0, int(budget_hint))) if budget_hint is not None else 4000
        except (TypeError, ValueError):
            budget = 4000
        text_block = self.service.context_block(items, max_chars=budget)
        for record, item in zip(records, items, strict=True):
            record.injected = f"- [{item.memoryType}] {item.summary}" in text_block
            record.signals["reasons"].append(
                "selected for injection" if record.injected else "excluded by injection budget"
            )
        injected_count = text_block.count("\n- [") + (1 if text_block.startswith("- [") else 0)
        result = MemoryQueryResult(
            text_block=text_block,
            records=records,
            trace={
                "intent": request.intent,
                "effect": request.effect,
                "role_id": scope.role_id,
                "session_key": scope.session_key,
                "lanes": lane_trace,
                "rrf": True,
                "ranking": {
                    "unique_candidates": len(fused),
                    "raw_unique_candidates": len(raw_unique_ids),
                    "filtered_occurrences": filtered_occurrences,
                    "below_threshold_count": below_threshold_count,
                    "quota_excluded_count": quota_excluded_count,
                    "limit_excluded_count": limit_excluded_count,
                    "min_lane_score": min_score_value,
                },
                "vector_index": self.store.vector_index_status(),
                "injection": {
                    "budget_chars": budget,
                    "candidate_count": len(items),
                    "injected_count": injected_count,
                    "trimmed_count": max(0, len(items) - injected_count),
                    "type_limits": type_limits,
                },
            },
            raw={"candidate_count": len(records), "items": [item.model_dump(mode="json") for item in items]},
        )
        if self.event_bus is not None:
            try:
                self.event_bus.publish(RetrievalCompleted(
                    scope.role_id,
                    scope.session_key,
                    request.text,
                    request.intent,
                    len(records),
                    injected_count,
                    result.trace,
                    "; ".join(str(lane.get("error")) for lane in lane_trace if lane.get("error")) or None,
                ))
            except Exception:
                pass
        return result

    @staticmethod
    def _hotness(item: MemoryItem) -> float:
        """Bounded recency/reinforcement signal used after relevance fusion."""
        try:
            age_days = max(0.0, (datetime.now(timezone.utc) - item.updatedAt).total_seconds() / 86400)
        except (TypeError, ValueError):
            age_days = 365.0
        recency = math.exp(-age_days / 30.0)
        reinforcement = min(1.0, max(0, item.reinforcement - 1) / 10.0)
        emotion = min(1.0, max(0, item.emotionalWeight) / 10.0)
        return 0.03 * recency + 0.02 * reinforcement + 0.01 * emotion

    async def _retrieve_lane(self, role_id: str, text: str, limit: int, *, semantic_only: bool = False) -> list[MemoryItem]:
        if not text.strip():
            return await asyncio.to_thread(self.store.list_active, role_id)
        return await (
            self.service.recall_vector_async(role_id, text, limit=limit)
            if semantic_only
            else self.service.recall_async(role_id, text, limit=limit)
        )

    @staticmethod
    def _query_lanes(request: MemoryQuery) -> list[str]:
        text = request.text.strip()
        if request.intent == "answer":
            # Shiori's answer path keeps the lexical original and uses two bounded
            # semantic hypotheses. A caller may provide reviewed hypotheses through hints.
            configured = request.filters.hints.get("hyde_queries", ())
            hypotheses = [str(value).strip() for value in configured if str(value).strip()][:2]
            return [text, *hypotheses]
        if request.intent == "procedure":
            rule = request.filters.hints.get("procedure_rule")
            tags = []
            if isinstance(rule, Mapping):
                raw_tags = rule.get("requiredTags", rule.get("required_tags", ()))
                if isinstance(raw_tags, (list, tuple, set)):
                    tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()]
            suffix = f" 标签：{'、'.join(tags)}" if tags else ""
            return [f"执行步骤和规则：{text}{suffix}", text]
        return [text]

    @staticmethod
    def _matches_filters(item: MemoryItem, request: MemoryQuery) -> bool:
        filters = request.filters
        allowed_kinds = filters.kinds
        if request.intent == "interest" and not allowed_kinds:
            allowed_kinds = ("preference", "profile")
        elif request.intent == "procedure" and not allowed_kinds:
            allowed_kinds = ("procedure", "preference")
        if allowed_kinds and item.memoryType not in allowed_kinds:
            return False
        domain = str(item.extra.get("memory_domain", ""))
        if filters.domains and domain not in filters.domains:
            return False
        if request.intent == "procedure":
            rule = filters.hints.get("procedure_rule")
            if isinstance(rule, Mapping):
                required = rule.get("requiredTags", rule.get("required_tags", ()))
                if isinstance(required, (list, tuple, set)) and required:
                    tags = item.extra.get("procedureTags", item.extra.get("procedure_tags", ()))
                    if not isinstance(tags, (list, tuple, set)):
                        return False
                    if not set(map(str, required)).issubset(set(map(str, tags))):
                        return False
                min_confidence = rule.get("minConfidence", rule.get("min_confidence"))
                if isinstance(min_confidence, (int, float)) and float(item.extra.get("confidence", 1.0)) < float(min_confidence):
                    return False
                if rule.get("requiresExplicitUserSignal", rule.get("requires_explicit_user_signal", False)) and not item.extra.get("explicit"):
                    return False
        if item.status != "active":
            return False
        happened = item.happenedAt
        if filters.time_start is not None and (happened is None or happened < filters.time_start):
            return False
        if filters.time_end is not None and (happened is None or happened >= filters.time_end):
            return False
        if request.intent == "timeline" and item.memoryType != "event" and not filters.kinds:
            return False
        return True

    async def mutate(self, request: MemoryMutation) -> MemoryMutationResult:
        scope = self._validate_scope(request.scope)
        if request.kind == "remember":
            if not request.summary.strip():
                raise ValueError("summary is required for remember")
            source = MemorySourceRef(
                kind="manual",
                sessionKey=scope.session_key,
                stableSourceKey=request.source_ref or f"manual:{uuid.uuid4().hex}",
            )
            item = self.store.add_or_reinforce(
                scope.role_id,
                request.memory_kind or "fact",
                request.summary,
                source,
                extra=dict(request.metadata),
                happened_at=datetime.fromisoformat(request.happened_at) if request.happened_at else None,
            )
            self.service.index_embedding(scope.role_id, item)
            return MemoryMutationResult(
                accepted=True,
                item_id=item.id,
                actual_kind=item.memoryType,
                status=item.status,
                affected_ids=[item.id],
                items=[asdict(self._record(item, 0))],
                raw={"item": item.model_dump(mode="json")},
            )
        if request.kind == "update":
            if len(request.ids) != 1:
                raise ValueError("update requires exactly one item id")
            item = self.store.update(
                scope.role_id,
                request.ids[0],
                request.summary,
                request.memory_kind or None,
                datetime.fromisoformat(request.happened_at) if request.happened_at else None,
            )
            return MemoryMutationResult(
                accepted=True,
                item_id=item.id,
                actual_kind=item.memoryType,
                status=item.status,
                affected_ids=[item.id],
                raw={"item": item.model_dump(mode="json")},
            )
        if request.kind == "delete":
            removed = self.store.remove_batch(scope.role_id, list(request.ids))
            return MemoryMutationResult(
                accepted=removed == len(request.ids),
                affected_ids=list(request.ids[:removed]),
                missing_ids=list(request.ids[removed:]),
            )
        if request.kind in ("reject", "state_change"):
            status = request.status or ("rejected" if request.kind == "reject" else "")
            if status not in {"active", "rejected", "forgotten", "superseded"}:
                raise ValueError("a supported target status is required")
            affected: list[str] = []
            missing: list[str] = []
            results: list[MemoryItem] = []
            for item_id in request.ids:
                try:
                    results.append(self.store.set_status(scope.role_id, item_id, status))
                except KeyError:
                    missing.append(item_id)
                else:
                    affected.append(item_id)
            return MemoryMutationResult(
                accepted=not missing,
                status=status if affected else "unchanged",
                affected_ids=affected,
                missing_ids=missing,
                raw={"items": [item.model_dump(mode="json") for item in results]},
            )
        affected: list[str] = []
        missing: list[str] = []
        for item_id in request.ids:
            try:
                self.store.set_status(scope.role_id, item_id, "forgotten")
            except KeyError:
                missing.append(item_id)
            else:
                affected.append(item_id)
        return MemoryMutationResult(
            accepted=not missing,
            status="forgotten" if affected else "unchanged",
            affected_ids=affected,
            missing_ids=missing,
        )

    async def ingest(self, request: MemoryIngestRequest) -> MemoryIngestResult:
        scope = self._validate_scope(request.scope)
        if request.source_kind != "text" or not isinstance(request.content, str):
            raise ValueError("the default engine currently accepts text ingests only")
        source_key = request.metadata.get("stable_source_key")
        if not isinstance(source_key, str) or not source_key.strip():
            raise ValueError("stable_source_key is required for idempotent ingest")
        item = self.store.add_or_reinforce(
            scope.role_id,
            str(request.hints.get("memory_type", "fact")),
            request.content,
            MemorySourceRef(
                kind=request.source_kind,
                sessionKey=scope.session_key,
                stableSourceKey=source_key,
            ),
            extra=request.metadata,
        )
        self.service.index_embedding(scope.role_id, item)
        return MemoryIngestResult(accepted=True, created_ids=[item.id], summary=item.summary)

    def reinforce_items_batch(self, scope: MemoryScope, ids: list[str]) -> None:
        self._validate_scope(scope)
        self.store.reinforce_items_batch(scope.role_id, ids)

    def list_role_filter_values(self, role_id: str) -> dict[str, list[str]]:
        if not self._role_exists(role_id):
            raise ValueError(f"unknown role_id: {role_id}")
        items = self.store.list_all(role_id)
        sources = {
            source_key
            for item in items
            for source_key in (item.sourceRef.sourceKeys or [item.sourceRef.stableSourceKey]) + [item.sourceRef.kind]
            if source_key
        }
        return {
            "memory_types": sorted({item.memoryType for item in items}),
            "statuses": sorted({item.status for item in items}),
            "domains": sorted({str(item.extra.get("memory_domain", "")) for item in items if item.extra.get("memory_domain")}),
            "sources": sorted(sources),
        }

    def list_active_items(self, role_id: str, query: str = "") -> list[MemoryItem]:
        if not self._role_exists(role_id):
            raise ValueError(f"unknown role_id: {role_id}")
        return self.store.query(role_id, query) if query.strip() else self.store.list_active(role_id)

    def list_events_by_time_range(
        self, role_id: str, time_start: datetime, time_end: datetime, *, limit: int = 200
    ) -> list[dict[str, object]]:
        if not self._role_exists(role_id):
            raise ValueError(f"unknown role_id: {role_id}")
        if time_end <= time_start:
            raise ValueError("time_end must be after time_start")
        return self.store.consolidation_events_by_time_range(
            role_id, time_start.isoformat(), time_end.isoformat(), limit
        )

    def list_items_for_admin(
        self,
        *,
        role_id: str,
        q: str = "",
        memory_type: str = "",
        memory_domain: str = "",
        status: str = "",
        source_ref: str = "",
        has_embedding: bool | None = None,
        page: int = 1,
        page_size: int = 50,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[dict[str, object]], int]:
        if not self._role_exists(role_id):
            raise ValueError(f"unknown role_id: {role_id}")
        allowed_sort = {"created_at", "updated_at", "happened_at", "reinforcement"}
        if sort_by not in allowed_sort:
            raise ValueError("invalid sort_by")
        if sort_order not in {"asc", "desc"}:
            raise ValueError("invalid sort_order")
        items = self.store.list_all(role_id)
        needle = q.casefold().strip()
        selected = [
            item for item in items
            if (not needle or needle in item.summary.casefold())
            and (not memory_type or item.memoryType == memory_type)
            and (not memory_domain or item.extra.get("memory_domain") == memory_domain)
            and (not status or item.status == status)
            and (not source_ref or source_ref in (item.sourceRef.sourceKeys or [item.sourceRef.stableSourceKey]))
            and (has_embedding is None or item.hasEmbedding is has_embedding)
        ]
        sort_attribute = {
            "created_at": "createdAt",
            "updated_at": "updatedAt",
            "happened_at": "happenedAt",
            "reinforcement": "reinforcement",
        }[sort_by]
        if sort_by == "reinforcement":
            selected.sort(key=lambda item: item.reinforcement, reverse=sort_order != "asc")
        elif sort_by == "happened_at":
            selected.sort(key=lambda item: (item.happenedAt is not None, item.happenedAt), reverse=sort_order != "asc")
        else:
            selected.sort(key=lambda item: getattr(item, sort_attribute), reverse=sort_order != "asc")
        total = len(selected)
        start = max(0, page - 1) * max(1, page_size)
        return [item.model_dump(mode="json") for item in selected[start : start + max(1, page_size)]], total

    def get_item_for_admin(
        self, role_id: str, item_id: str, *, include_embedding: bool = False
    ) -> dict[str, object] | None:
        if not self._role_exists(role_id):
            raise ValueError(f"unknown role_id: {role_id}")
        item = self.store.get(role_id, item_id)
        if item is None:
            return None
        result = item.model_dump(mode="json")
        if include_embedding:
            result["embedding"] = self.store.embedding_for(role_id, item_id)
        return result

    def update_item_for_admin(
        self, role_id: str, item_id: str, *, status: str | None = None,
        extra_json: dict[str, object] | None = None, source_ref: str | None = None,
        happened_at: str | None = None, emotional_weight: int | None = None,
        happened_at_provided: bool = False,
    ) -> dict[str, object] | None:
        if not self._role_exists(role_id):
            raise ValueError(f"unknown role_id: {role_id}")
        if status is not None and status not in {"active", "rejected", "forgotten", "superseded"}:
            raise ValueError("unsupported memory status")
        item = self.store.update_metadata(
            role_id, item_id, status=status, extra_json=extra_json, source_ref=source_ref,
            happened_at=happened_at, happened_at_provided=happened_at_provided,
            emotional_weight=emotional_weight,
        )
        return item.model_dump(mode="json") if item else None

    def delete_item(self, role_id: str, item_id: str) -> bool:
        if not self._role_exists(role_id):
            raise ValueError(f"unknown role_id: {role_id}")
        try:
            self.store.remove(role_id, item_id)
        except KeyError:
            return False
        return True

    def delete_items_batch(self, role_id: str, ids: list[str]) -> int:
        if not self._role_exists(role_id):
            raise ValueError(f"unknown role_id: {role_id}")
        return self.store.remove_batch(role_id, ids)

    def invalidate_role_memories(self, role_id: str) -> int:
        if not self._role_exists(role_id):
            raise ValueError(f"unknown role_id: {role_id}")
        return self.store.invalidate_role(role_id)

    def find_similar_items_for_admin(
        self, role_id: str, item_id: str, *, top_k: int = 8, memory_type: str = "",
        score_threshold: float = 0.0, include_superseded: bool = False,
    ) -> list[dict[str, object]]:
        if not self._role_exists(role_id):
            raise ValueError(f"unknown role_id: {role_id}")
        if top_k <= 0:
            return []
        if score_threshold < 0:
            raise ValueError("score_threshold must be non-negative")
        item = self.store.get(role_id, item_id)
        if item is None:
            return []
        candidates = (
            [candidate for candidate in self.store.list_all(role_id) if candidate.status == "active" or (include_superseded and candidate.status == "superseded")]
            if include_superseded
            else self.store.query(role_id, item.summary, limit=max(1, top_k + 1))
        )
        similar: list[dict[str, object]] = []
        for rank, candidate in enumerate(candidates):
            score = 1.0 / (rank + 2)
            if (
                candidate.id == item_id
                or (memory_type and candidate.memoryType != memory_type)
                or (candidate.status == "superseded" and not include_superseded)
                or score < score_threshold
            ):
                continue
            similar.append(candidate.model_dump(mode="json"))
            if len(similar) >= max(0, top_k):
                break
        return similar

    @staticmethod
    def _record(
        item: MemoryItem,
        rank: int,
        *,
        score: float | None = None,
        signals: dict[str, object] | None = None,
    ) -> MemoryRecord:
        source = item.sourceRef
        refs = list(source.messageIds)
        if source.messageRange:
            refs.extend(str(value) for value in source.messageRange)
        evidence_kind = (
            "consolidation" if source.kind == "consolidation"
            else "message_range" if source.messageRange
            else "message" if refs
            else "turn"
        )
        evidence = EvidenceRef(
            kind=evidence_kind,
            refs=refs,
            source_ref=source.stableSourceKey,
            metadata={
                "session_key": source.sessionKey,
                "kind": source.kind,
                "message_range": list(source.messageRange) if source.messageRange else None,
            },
        )
        return MemoryRecord(
            id=item.id,
            kind=item.memoryType,
            summary=item.summary,
            score=score if score is not None else 1.0 / (rank + 1),
            engine_kind="meido-sqlite-memory",
            evidence=[evidence],
            source={
                "kind": source.kind,
                "session_key": source.sessionKey,
                "message_ids": list(source.messageIds),
                "message_range": list(source.messageRange) if source.messageRange else None,
                "stable_source_key": source.stableSourceKey,
            },
            signals={"reinforcement": item.reinforcement, "status": item.status, **(signals or {})},
            domain=str(item.extra.get("memory_domain", "")),
            extra=dict(item.extra),
            happened_at=item.happenedAt.isoformat() if item.happenedAt else None,
            status=item.status,
            emotional_weight=item.emotionalWeight,
            has_embedding=item.hasEmbedding,
            injected=False,
        )
