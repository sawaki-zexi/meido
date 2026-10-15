"""Role-scoped, read-only memory access for the Agent Runtime."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from dataclasses import replace
from zoneinfo import ZoneInfo
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, Protocol, cast

from .tools import AgentToolResult, ToolContext, ToolDefinition, ToolRegistry

if TYPE_CHECKING:
    from ..memory_engine import MemoryQuery, MemoryQueryResult, MemoryScope

MemoryRecallIntent = Literal["context", "answer", "timeline", "interest", "procedure"]
_MEMORY_INTENTS: tuple[MemoryRecallIntent, ...] = ("context", "answer", "timeline", "interest", "procedure")
_MEMORY_INTENT_VALUES = frozenset(_MEMORY_INTENTS)
_MAX_QUERY_LENGTH = 500
_MAX_RESULT_LIMIT = 200
_LOCAL_TZ = ZoneInfo("Asia/Shanghai")


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

    def check_context(self, context: ToolContext) -> None:
        if context.role_id != self._scope.role_id or context.session_key != self._scope.session_key:
            raise PermissionError("memory query scope does not match the current role/session")

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
                "memory_kind": {"type": "string", "enum": ["", "fact", "profile", "preference", "event", "procedure"]},
                "memory_domain": {"type": "string", "enum": ["", "role_self", "relationship", "shared"]},
                "time_filter": {"type": "string", "maxLength": 100,
                    "description": "today / yesterday / recent_3d / recent_7d / recent_30d / YYYY-MM-DD / YYYY-MM-DD~YYYY-MM-DD"},
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

    def __init__(self, memory: MemoryReadPort, *, now=None) -> None:
        self._memory = memory
        self._now = now or (lambda: datetime.now(_LOCAL_TZ))
        enabled = getattr(memory, "enabled", None)
        if callable(enabled) and not enabled():
            self.definition = replace(self.definition, approval="deny")

    async def execute(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
        on_update=None,
    ) -> AgentToolResult:
        del on_update
        context.signal.raise_if_cancelled()
        check_context = getattr(self._memory, "check_context", None)
        if callable(check_context):
            check_context(context)
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

        ToolRegistry.validate_definition(self.definition, dict(arguments))
        from ..memory_engine import MemoryQuery, MemoryQueryFilters, MemoryScope

        try:
            time_window = _time_window(arguments.get("time_filter", ""), self._now())
            if intent == "timeline" and time_window is None:
                raise ValueError("timeline requires time_filter")
        except ValueError:
            return AgentToolResult(_encode({"error": "invalid_time_filter", "count": 0, "items": [], "records": [], "text": ""}), is_error=True)

        bound_scope = getattr(self._memory, "scope", None)
        if bound_scope is not None and bound_scope.role_id != context.role_id:
            raise PermissionError("memory query scope does not match the current role")
        scope = bound_scope if bound_scope is not None else MemoryScope(context.role_id, context.session_key)
        request = MemoryQuery(
            text=query,
            intent=intent,
            effect="read_only",
            scope=scope,
            limit=limit,
            filters=MemoryQueryFilters(
                kinds=(arguments["memory_kind"],) if arguments.get("memory_kind") else (),
                domains=(arguments["memory_domain"],) if arguments.get("memory_domain") else (),
                time_start=time_window[0] if time_window else None,
                time_end=time_window[1] if time_window else None,
            ),
        )
        query_with_context = getattr(self._memory, "query_with_context", None)
        result = (await query_with_context(request, context) if callable(query_with_context)
                  else await self._memory.query(request))
        context.signal.raise_if_cancelled()
        if callable(check_context):
            check_context(context)
        records: list[dict[str, object]] = []
        payload: dict[str, object] = {
            "query": query,
            "intent": intent,
            "count": len(result.records),
            "text": str(result.text_block)[:4000],
            "records": records,
            "items": records,
            "cited_item_ids": [],
            "trace": {"intent": intent, "effect": "read_only"},
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
        payload["count"] = len(records)
        payload["cited_item_ids"] = [record["id"] for record in records]
        return AgentToolResult(
            content=_encode_bounded(payload, output_limit),
            details={"status": "succeeded", "count": len(result.records), "intent": intent},
        )


def _record_payload(record: object) -> dict[str, object]:
    """Expose evidence useful to the model without raw store/admin fields."""

    payload = {
        "id": _clip(getattr(record, "id", ""), 200),
        "kind": _clip(getattr(record, "kind", ""), 100),
        "memory_type": _clip(getattr(record, "kind", ""), 100),
        "source_ref": _clip(next((getattr(item, "source_ref", "") for item in getattr(record, "evidence", []) if getattr(item, "source_ref", "")), ""), 1000),
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
                **({"source_ref": _clip(getattr(evidence, "source_ref", ""), 1000)} if getattr(evidence, "source_ref", "") else {}),
            }
            for evidence in list(getattr(record, "evidence", []))[:10]
        ],
    }
    extra = getattr(record, "extra", {})
    if getattr(record, "kind", "") == "procedure" and isinstance(extra, Mapping):
        steps = extra.get("steps", [])
        payload["steps"] = [value[:1000] for value in steps[:50] if isinstance(value, str)] if isinstance(steps, list) else []
        payload["tool_requirement"] = str(extra.get("tool_requirement", ""))[:200]
    return payload


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
    payload["items"] = []
    payload["cited_item_ids"] = []
    payload["count"] = 0
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


def _time_window(value: str, now: datetime):
    """Shiori date presets, interpreted in the owner's Shanghai timezone.

    Adapted from Shiori-Agent 4f266ae recall_memory.py; MIT attribution under
    third_party/shiori-agent. Reversed ranges are explicitly invalid here.
    """
    text = value.strip()
    if not text:
        return None
    now = now.astimezone(_LOCAL_TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "today":
        return today, today + timedelta(days=1)
    if text == "yesterday":
        return today - timedelta(days=1), today
    recent = {"recent_3d": 3, "recent_7d": 7, "recent_30d": 30}
    if text in recent:
        return now - timedelta(days=recent[text]), now
    dates = text.split("~")
    if not 1 <= len(dates) <= 2 or any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item.strip()) for item in dates):
        raise ValueError("invalid time filter")
    start = datetime.strptime(dates[0].strip(), "%Y-%m-%d").replace(tzinfo=_LOCAL_TZ)
    end = datetime.strptime(dates[-1].strip(), "%Y-%m-%d").replace(tzinfo=_LOCAL_TZ) + timedelta(days=1)
    if end <= start:
        raise ValueError("invalid time range")
    return start, end
