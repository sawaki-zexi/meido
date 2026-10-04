"""Stable memory port shared by dialogue, tools, and administrative callers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
import asyncio
import uuid
from typing import Callable, Literal, Protocol, runtime_checkable

from .memory_service import MemoryService
from .models import MemoryItem, MemorySourceRef


MemoryQueryIntent = Literal["context", "answer", "timeline", "interest", "procedure"]
MemoryQueryEffect = Literal["stateful", "read_only"]
MemoryDomain = Literal["role_self", "relationship", "shared"]


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

    def __init__(self, service: MemoryService, role_exists: Callable[[str], bool]) -> None:
        self.service = service
        self.store = service.store
        self._role_exists = role_exists
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
            raise ValueError("timeline queries require time_start and time_end")
        if request.limit == 0:
            return MemoryQueryResult(
                trace={"intent": request.intent, "effect": request.effect, "role_id": scope.role_id, "lanes": []},
                raw={"candidate_count": 0, "items": []},
            )

        lanes = self._query_lanes(request)
        lane_results = await asyncio.gather(
            *(self._retrieve_lane(scope.role_id, text, max(request.limit * 4, 16)) for text in lanes),
            return_exceptions=True,
        )
        fused: dict[str, tuple[MemoryItem, float, dict[str, object]]] = {}
        lane_trace: list[dict[str, object]] = []
        for lane_index, (lane, result) in enumerate(zip(lanes, lane_results, strict=True)):
            if isinstance(result, BaseException):
                lane_trace.append({"query": lane, "error": str(result)})
                continue
            lane_trace.append({"query": lane, "rank": len(result)})
            for rank, item in enumerate(result, 1):
                if not self._matches_filters(item, request):
                    continue
                # RRF is used only for ordering; source lane/rank remain observable.
                contribution = 1.0 / (60 + rank)
                current = fused.get(item.id)
                if current is None:
                    fused[item.id] = (item, contribution, {"lanes": [lane], "ranks": {lane: rank}})
                else:
                    current[2]["lanes"].append(lane)
                    current[2]["ranks"][lane] = rank
                    fused[item.id] = (item, current[1] + contribution, current[2])

        ranked = sorted(fused.values(), key=lambda value: (value[1], value[0].updatedAt), reverse=True)
        if request.intent == "timeline":
            ranked.sort(key=lambda value: value[0].happenedAt or value[0].updatedAt, reverse=True)
        selected = ranked[: request.limit]
        items = [item for item, _, _ in selected]
        records = [self._record(item, index, score=score, signals=signals) for index, (item, score, signals) in enumerate(selected)]
        return MemoryQueryResult(
            text_block=self.service.context_block(items),
            records=records,
            trace={"intent": request.intent, "effect": request.effect, "role_id": scope.role_id, "lanes": lane_trace, "rrf": True},
            raw={"candidate_count": len(records), "items": [item.model_dump(mode="json") for item in items]},
        )

    async def _retrieve_lane(self, role_id: str, text: str, limit: int) -> list[MemoryItem]:
        if not text.strip():
            return await asyncio.to_thread(self.store.list_active, role_id)
        return await self.service.recall_async(role_id, text, limit=limit)

    @staticmethod
    def _query_lanes(request: MemoryQuery) -> list[str]:
        text = request.text.strip()
        if request.intent == "answer":
            # Shiori's answer path keeps the lexical original and uses two bounded
            # semantic hypotheses. A caller may provide reviewed hypotheses through hints.
            configured = request.filters.hints.get("hyde_queries", ())
            hypotheses = [str(value).strip() for value in configured if str(value).strip()][:2]
            if not hypotheses:
                hypotheses = [f"event: {text}", f"general: {text}"]
            return [text, *hypotheses]
        if request.intent == "procedure":
            return [f"执行步骤和规则：{text}", text]
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
        return {
            "memory_types": sorted({item.memoryType for item in items}),
            "statuses": sorted({item.status for item in items}),
            "domains": sorted({str(item.extra.get("memory_domain", "")) for item in items if item.extra.get("memory_domain")}),
            "sources": sorted({item.sourceRef.kind for item in items}),
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
        items = self.store.list_all(role_id)
        needle = q.casefold().strip()
        selected = [
            item for item in items
            if (not needle or needle in item.summary.casefold())
            and (not memory_type or item.memoryType == memory_type)
            and (not memory_domain or item.extra.get("memory_domain") == memory_domain)
            and (not status or item.status == status)
            and (not source_ref or source_ref in item.sourceRef.stableSourceKey)
            and (has_embedding is None or item.hasEmbedding is has_embedding)
        ]
        if sort_by == "reinforcement":
            selected.sort(key=lambda item: item.reinforcement, reverse=sort_order != "asc")
        elif sort_by == "happened_at":
            selected.sort(key=lambda item: (item.happenedAt is not None, item.happenedAt), reverse=sort_order != "asc")
        else:
            selected.sort(key=lambda item: getattr(item, sort_by), reverse=sort_order != "asc")
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
    ) -> dict[str, object] | None:
        if not self._role_exists(role_id):
            raise ValueError(f"unknown role_id: {role_id}")
        item = self.store.update_metadata(
            role_id, item_id, status=status, extra_json=extra_json, source_ref=source_ref,
            happened_at=happened_at, emotional_weight=emotional_weight,
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
            metadata={"session_key": source.sessionKey, "kind": source.kind},
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
