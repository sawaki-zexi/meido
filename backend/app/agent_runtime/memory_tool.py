"""Role-scoped, read-only memory access for the Agent Runtime."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, Protocol, cast

from .tools import AgentToolResult, ToolContext, ToolDefinition

if TYPE_CHECKING:
    from ..memory_engine import MemoryQuery, MemoryQueryResult, MemoryScope

MemoryRecallIntent = Literal["context", "answer", "interest", "procedure"]
_MEMORY_INTENTS: tuple[MemoryRecallIntent, ...] = ("context", "answer", "interest", "procedure")
_MEMORY_INTENT_VALUES = frozenset(_MEMORY_INTENTS)
_MAX_QUERY_LENGTH = 500
_MAX_RESULT_LIMIT = 8


class MemoryReadPort(Protocol):
    """Minimal host service exposed to a memory capability."""

    async def query(self, request: MemoryQuery) -> MemoryQueryResult: ...


class RoleScopedMemoryReadPort:
    """Bind the read-only port to one role before exposing it to a tool."""

    def __init__(self, engine: MemoryReadPort, role_id: str) -> None:
        from ..memory_engine import MemoryScope

        self._engine = engine
        self._scope = MemoryScope(role_id, f"role:{role_id}")

    @property
    def scope(self) -> MemoryScope:
        return self._scope

    async def query(self, request: MemoryQuery) -> MemoryQueryResult:
        if request.scope != self._scope:
            raise PermissionError("memory query scope does not match the current role")
        if request.effect != "read_only":
            raise PermissionError("memory recall tool only permits read-only queries")
        return await self._engine.query(request)


class MemoryRecallTool:
    """Read bounded memory records without exposing storage or mutations."""

    definition = ToolDefinition(
        name="recall_memory",
        description="检索当前角色的相关记忆；只能读取当前角色，不能写入或删除记忆。",
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要检索的事实、事件或偏好",
                    "minLength": 1,
                    "maxLength": _MAX_QUERY_LENGTH,
                },
                "intent": {
                    "type": "string",
                    "enum": list(_MEMORY_INTENTS),
                    "description": "检索目的，默认使用 answer",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_RESULT_LIMIT,
                    "description": "最多返回的记忆条数",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        risk="read_only",
        source="builtin:memory",
        version="1.0.0",
        exposure="direct",
        timeout_seconds=15.0,
        output_limit=12000,
    )

    def __init__(self, memory: MemoryReadPort) -> None:
        self._memory = memory

    async def execute(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
        on_update=None,
    ) -> AgentToolResult:
        del on_update
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        query = query.strip()
        if len(query) > _MAX_QUERY_LENGTH:
            raise ValueError("query is too long")

        intent_value = arguments.get("intent", "answer")
        if not isinstance(intent_value, str) or intent_value not in _MEMORY_INTENT_VALUES:
            raise ValueError("unsupported memory query intent")
        intent = cast(MemoryRecallIntent, intent_value)
        limit = arguments.get("limit", 5)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_RESULT_LIMIT:
            raise ValueError(f"limit must be an integer between 1 and {_MAX_RESULT_LIMIT}")

        from ..memory_engine import MemoryQuery, MemoryScope

        scope = MemoryScope(context.role_id, context.session_key)
        result = await self._memory.query(MemoryQuery(
            text=query,
            intent=intent,
            effect="read_only",
            scope=scope,
            limit=limit,
        ))
        records: list[dict[str, object]] = []
        payload: dict[str, object] = {
            "query": query,
            "intent": intent,
            "count": len(result.records),
            "text": str(result.text_block)[:4000],
            "records": records,
        }
        truncated = len(str(result.text_block)) > 4000
        output_limit = self.definition.output_limit
        for record in result.records:
            candidate = _record_payload(record)
            records.append(candidate)
            if output_limit is not None and len(_encode(payload)) > output_limit:
                records.pop()
                truncated = True
                break
        if truncated:
            payload["truncated"] = True
        return AgentToolResult(
            content=_encode_bounded(payload, output_limit),
            details={"status": "succeeded", "count": len(result.records), "intent": intent},
        )


def _record_payload(record: object) -> dict[str, object]:
    """Expose evidence useful to the model without raw store/admin fields."""

    return {
        "id": _clip(getattr(record, "id", ""), 200),
        "kind": _clip(getattr(record, "kind", ""), 100),
        "summary": _clip(getattr(record, "summary", ""), 1000),
        "score": getattr(record, "score", 0.0),
        "domain": _clip(getattr(record, "domain", ""), 100),
        "happenedAt": _clip(getattr(record, "happened_at", None), 100),
        "status": _clip(getattr(record, "status", "active"), 100),
        "evidence": [
            {
                "kind": _clip(getattr(evidence, "kind", ""), 100),
                "refs": [_clip(ref, 200) for ref in list(getattr(evidence, "refs", []))[:10]],
                "resolver": _clip(getattr(evidence, "resolver", ""), 100),
            }
            for evidence in list(getattr(record, "evidence", []))[:10]
        ],
    }


def _clip(value: object, limit: int) -> object:
    if value is None or not isinstance(value, str):
        return value
    return value[:limit]


def _encode(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _encode_bounded(payload: dict[str, object], limit: int | None) -> str:
    """Keep the final JSON representation within the tool output budget."""

    encoded = _encode(payload)
    if limit is None or len(encoded) <= limit:
        return encoded

    payload["truncated"] = True
    payload["records"] = []
    text = str(payload.get("text", ""))
    for size in (2000, 1000, 500, 250, 100, 0):
        payload["text"] = text[:size]
        encoded = _encode(payload)
        if len(encoded) <= limit:
            return encoded

    payload["query"] = ""
    payload["text"] = ""
    encoded = _encode(payload)
    if len(encoded) <= limit:
        return encoded

    return _encode({
        "intent": payload.get("intent", "answer"),
        "count": payload.get("count", 0),
        "records": [],
        "truncated": True,
    })
