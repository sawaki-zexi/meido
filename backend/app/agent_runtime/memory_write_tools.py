"""Explicit role memory tools over the existing MemoryEngine contract.

Shiori-Agent 4f266ae business arguments/result shapes adapted for Meido.
Copyright (c) 2026 YinFengWindy; third_party/shiori-agent/LICENSE.
"""
from __future__ import annotations

import hashlib
import asyncio
import math
import inspect
import json
from collections.abc import Callable, Mapping

from ..memory_engine import MemoryMutation, MemoryScope
from ..models import MemorySourceRef
from .tools import AgentToolResult, ToolContext, ToolDefinition, ToolRegistry
from .memory_io import run_memory_io, run_async_memory_io


class RoleMemoryAccess:
    def __init__(self, engine, sessions, role_id: str, *, session_key: str | None = None,
                 enabled: Callable[[], bool] = lambda: True, authorize=None, mutate=None, io_timeout_seconds: float = 15):
        self.engine = engine
        self.sessions = sessions
        self.scope = MemoryScope(role_id, f"role:{role_id}")
        self.session_key = session_key or self.scope.session_key
        self.enabled = enabled
        self.authorize = authorize
        self.mutate_operation = mutate
        if isinstance(io_timeout_seconds, bool) or not math.isfinite(io_timeout_seconds) or not 0 < io_timeout_seconds <= 15:
            raise ValueError("memory IO timeout must be positive and at most 15 seconds")
        self.io_timeout_seconds = io_timeout_seconds
        self._mutation_lock = asyncio.Lock()

    def check_context(self, context: ToolContext):
        context.signal.raise_if_cancelled()
        if context.role_id != self.scope.role_id or context.session_key != self.session_key:
            raise PermissionError("memory scope does not match the current role/session")
        if self.session_key != self.scope.session_key and self.authorize is None:
            raise PermissionError("external origin requires host owner authorization")
        if not self.enabled() or (self.authorize is not None and not self.authorize(context)):
            raise PermissionError("memory is disabled or owner context is not authorized")

    def user_source(self, context: ToolContext):
        self.check_context(context)
        run = self.sessions.get_run(context.run_id)
        message = self.sessions.user_message_for_run(context.run_id)
        if (run is None or run.roleId != context.role_id or run.sessionKey != context.session_key
                or run.status not in {"created", "running"} or message is None
                or message.sessionKey != context.session_key or message.messageType != "text"
                or message.role != "user" or message.status != "completed"):
            raise PermissionError("current run has no authorized user message source")
        return message

    async def query(self, request):
        if request.scope != self.scope or request.effect != "read_only" or not self.enabled():
            raise PermissionError("memory query scope/effect is not authorized")
        return await run_async_memory_io(lambda: self.engine.query(request), timeout=self.io_timeout_seconds)

    async def query_with_context(self, request, context):
        self.check_context(context)
        if request.scope != self.scope or request.effect != "read_only":
            raise PermissionError("memory query scope/effect is not authorized")
        result = await run_async_memory_io(lambda: self.engine.query(request), signal=context.signal, timeout=self.io_timeout_seconds)
        self.check_context(context)
        return result

    async def mutate(self, mutation, context):
        self.check_context(context)
        if self.mutate_operation is not None:
            result = self.mutate_operation(mutation, context)
            return await result if inspect.isawaitable(result) else result
        async with self._mutation_lock:
            self.check_context(context)
            snapshot = self.engine.store.snapshot_role(self.scope.role_id)
            try:
                return await self.run_mutation_io(mutation, context)
            except BaseException:
                await run_memory_io(lambda: self.engine.store.restore_role(self.scope.role_id, snapshot))
                raise

    async def run_mutation_io(self, mutation, context):
        self.check_context(context)

        result = await run_async_memory_io(lambda: self.engine.mutate(mutation), signal=context.signal, timeout=self.io_timeout_seconds)
        self.check_context(context)
        return result


def _result(data, *, error=False, limit=None):
    encoded = json.dumps(data, ensure_ascii=False)
    if limit is not None and len(encoded) > limit:
        data["truncated"] = True
        data["note"] = "返回列表已截断；count等总数保留实际执行结果，请使用较小ID批次核对。"
        lists = {key: value for key, value in data.items() if isinstance(value, list)}
        summary = data.get("summary", "")
        for size in (50, 20, 10, 5, 1, 0):
            data.update({key: value[:size] for key, value in lists.items()})
            if summary:
                data["summary"] = summary[:size * 10]
            encoded = json.dumps(data, ensure_ascii=False)
            if len(encoded) <= limit:
                break
    return AgentToolResult(encoded, details=data, is_error=error)


class MemorizeTool:
    def __init__(self, memory: RoleMemoryAccess):
        self.memory = memory
        self.definition = ToolDefinition(name="memorize", description="把主人明确要求当前角色记住的信息保存为长期记忆。",
            input_schema={"type": "object", "properties": {
                "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
                "memory_kind": {"type": "string", "enum": ["", "fact", "profile", "preference", "event", "procedure"]},
                "memory_domain": {"type": "string", "enum": ["", "role_self", "relationship", "shared"]},
                "tool_requirement": {"type": "string", "maxLength": 200},
                "steps": {"type": "array", "maxItems": 50, "items": {"type": "string", "minLength": 1, "maxLength": 1000}},
            }, "required": ["summary"], "additionalProperties": False}, risk="mutating", source="builtin:memory",
            approval="auto" if memory.enabled() else "deny", timeout_seconds=15, output_limit=12000)

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None):
        ToolRegistry.validate_definition(self.definition, dict(arguments))
        source = self.memory.user_source(context)
        summary = arguments["summary"].strip()
        if not summary:
            raise ValueError("summary must be non-empty")
        kind = arguments.get("memory_kind", "") or "fact"
        key = hashlib.sha256(f"{kind}:{summary}".encode()).hexdigest()[:20]
        result = await self.memory.mutate(MemoryMutation(kind="remember", scope=self.memory.scope,
            summary=summary, memory_kind=kind, memory_domain=arguments.get("memory_domain", ""),
            source_ref=f"{source.id}#memory-{key}", happened_at=source.createdAt.isoformat(),
            source=MemorySourceRef(kind="message", sessionKey=self.memory.scope.session_key,
                                   messageIds=[source.id], stableSourceKey=f"{source.id}#memory-{key}"),
            metadata={"explicit": True, "origin_session_key": source.sessionKey,
                      **{key: arguments[key] for key in ("tool_requirement", "steps") if key in arguments}}), context)
        return _result({"item_id": result.item_id, "memory_kind": result.actual_kind, "status": result.status,
                        "summary": summary}, error=not result.accepted, limit=self.definition.output_limit)


class ForgetMemoryTool:
    def __init__(self, memory: RoleMemoryAccess):
        self.memory = memory
        self.definition = ToolDefinition(name="forget_memory", description="将当前角色的错误记忆按ID标记失效；保留来源，不能撤销其他角色记忆。",
            input_schema={"type": "object", "properties": {
                "ids": {"type": "array", "maxItems": 200, "items": {"type": "string", "maxLength": 200}},
            }, "required": ["ids"], "additionalProperties": False}, risk="mutating", source="builtin:memory",
            approval="auto" if memory.enabled() else "deny", timeout_seconds=15, output_limit=16000)

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None):
        ToolRegistry.validate_definition(self.definition, dict(arguments))
        self.memory.user_source(context)
        ids = list(dict.fromkeys(value.strip() for value in arguments["ids"] if value.strip()))
        result = await self.memory.mutate(MemoryMutation(kind="forget", scope=self.memory.scope, ids=tuple(ids)), context)
        return _result({"requested_ids": ids, "requested_count": len(ids), "superseded_ids": result.affected_ids,
            "missing_ids": result.missing_ids, "missing_count": len(result.missing_ids),
            "already_forgotten_ids": result.raw.get("already_forgotten_ids", []), "count": len(result.affected_ids),
            "status": result.status, "items": [{"id": item_id, "status": "forgotten"} for item_id in result.affected_ids]},
            error=bool(result.missing_ids), limit=self.definition.output_limit)
