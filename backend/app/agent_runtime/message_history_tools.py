"""Original-message evidence, bound to the host's conversation scope."""

from __future__ import annotations

import asyncio
import json
from threading import Event
from collections.abc import Mapping
from typing import cast

from ..models import Message
from ..multichannel import MultichannelStore
from ..session_store import SessionStore
from ..conversation_history import ConversationHistoryView
from .tools import AgentToolResult, ToolContext, ToolDefinition, ToolRegistry

MESSAGE_HISTORY_TOOL_NAMES = frozenset({"search_messages", "fetch_messages"})


class ConversationHistory:
    def __init__(self, sessions: SessionStore, channels: MultichannelStore, *, role_id: str,
                 source_thread_id: str | None, is_owner: bool, view=None) -> None:
        self.sessions = sessions
        self.channels = channels
        self.role_id = role_id
        self.thread_id = source_thread_id
        self.is_owner = is_owner
        self.view = view

    def validate(self, context):
        context.signal.raise_if_cancelled()
        if self.view is not None:
            self.view.require_snapshot_current()

    def read(self, context: ToolContext) -> list[Message]:
        context.signal.raise_if_cancelled()
        if context.role_id != self.role_id:
            raise PermissionError("message history scope does not match the current conversation")
        expected = f"role:{self.role_id}" if self.thread_id in {None, f"desktop:{self.role_id}"} else f"thread:{self.thread_id}"
        if context.session_key != expected or (expected.startswith("role:") and not self.is_owner):
            raise PermissionError("message history scope does not match the current conversation")
        if self.view is None:
            current = self.sessions.user_message_for_run(context.run_id)
            if current is not None and current.sessionKey != expected:
                raise PermissionError("message history run does not match the current conversation")
            self.view = ConversationHistoryView(self.channels, role_id=self.role_id, thread_id=self.thread_id,
                owner=self.is_owner, through_message_id=current.id if current else None)
        messages = self.view.read()
        return [message for message in messages
                if message.status == "completed" and message.messageType == "text"
                and message.role in {"user", "assistant"}]


class MessageHistoryTool:
    def __init__(self, history: ConversationHistory, *, search: bool) -> None:
        self.history = history
        self.search = search
        properties = ({
            "query": {"type": "string", "maxLength": 500},
            "session_key": {"type": "string", "maxLength": 500},
            "role": {"type": "string", "enum": ["user", "assistant"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "offset": {"type": "integer", "minimum": 0},
        } if search else {
            "source_ref": {"type": "string", "maxLength": 2000},
            "ids": {"type": "array", "items": {"type": "string", "maxLength": 2000}, "maxItems": 50},
            "source_refs": {"type": "array", "items": {"type": "string", "maxLength": 2000}, "maxItems": 50},
            "evidence": {"type": "array", "items": {"type": "object", "properties": {
                "source_ref": {"type": "string", "maxLength": 2000},
                "refs": {"type": "array", "items": {"type": "string", "maxLength": 2000}, "maxItems": 50},
            }}, "maxItems": 50},
            "context": {"type": "integer", "minimum": 0, "maximum": 10},
        })
        self.definition = ToolDefinition(
            name="search_messages" if search else "fetch_messages",
            description=("按关键词定位获准历史，返回预览和source_ref；以原话作证前用fetch_messages核验。" if search
                         else "按source_ref读取获准消息原文，可带前后文；用于核验记忆和历史事实。"),
            input_schema={"type": "object", "properties": properties,
                          "required": ["query"] if search else [], "additionalProperties": False},
            source="builtin:message_history", external_allowed=True, timeout_seconds=15, search_hint='你之前说 聊过什么 历史对话' if search else '消息回溯 按ID查对话原文 source_ref',
        )

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        ToolRegistry.validate_definition(self.definition, dict(arguments))
        stopped = Event()
        try:
            result = await asyncio.to_thread(self._execute, arguments, context, stopped)
            self.history.validate(context)
            return result
        finally:
            stopped.set()

    def _execute(self, arguments: Mapping[str, object], context: ToolContext, stopped: Event) -> AgentToolResult:
        messages = self.history.read(context)
        _check_cancelled(context, stopped)
        if self.search:
            term = str(arguments["query"]).strip()
            terms = term.casefold().split()
            ranked: list[tuple[int, Message]] = []
            for message in messages:
                _check_cancelled(context, stopped)
                if (not terms or (arguments.get("role") and message.role != arguments["role"])
                        or (arguments.get("session_key") and message.sessionKey != arguments["session_key"])):
                    continue
                folded = message.content.casefold()
                score = 0
                for word in terms:
                    _check_cancelled(context, stopped)
                    score += word in folded
                if score:
                    ranked.append((score, message))
            ranked.sort(key=lambda item: (item[0], item[1].createdAt, item[1].sequence, item[1].id), reverse=True)
            matches = [message for _, message in ranked]
            offset, limit = cast(int, arguments.get("offset", 0)), cast(int, arguments.get("limit", 10))
            selected = matches[offset:offset + limit]
            next_offset = offset + len(selected)
            payload = {"count": len(selected), "matched_count": len(matches), "offset": offset,
                       "limit": limit, "has_more": next_offset < len(matches),
                       "next_offset": next_offset if next_offset < len(matches) else None,
                       "messages": [_preview(message, terms) for message in selected]}
        else:
            refs = _source_ids(arguments)
            matched = {message.id for message in messages if message.id in refs}
            radius = cast(int, arguments.get("context", 0))
            selected_ids = set(matched)
            for session_key in {message.sessionKey for message in messages if message.id in matched}:
                _check_cancelled(context, stopped)
                transcript = [message for message in messages if message.sessionKey == session_key]
                for index, message in enumerate(transcript):
                    _check_cancelled(context, stopped)
                    if message.id in matched:
                        selected_ids.update(neighbor.id for neighbor in transcript[max(0, index-radius):index+radius+1])
            selected = [message for message in messages if message.id in selected_ids]
            payload = {"count": len(selected), "matched_count": len(matched),
                       "messages": [{**_public(message), "content": message.content,
                                     "in_source_ref": message.id in matched} for message in selected]}
        _check_cancelled(context, stopped)
        self.history.validate(context)
        return AgentToolResult(json.dumps(payload, ensure_ascii=False), details={"status": "succeeded", "count": payload["count"]})


def _public(message: Message) -> dict[str, object]:
    return {"id": message.id, "source_ref": message.id, "session_key": message.sessionKey,
            "seq": message.sequence, "role": message.role, "timestamp": message.createdAt.isoformat()}


def _check_cancelled(context: ToolContext, stopped: Event) -> None:
    context.signal.raise_if_cancelled()
    if stopped.is_set():
        raise RuntimeError("message history tool cancelled")


def _preview(message: Message, terms: list[str]) -> dict[str, object]:
    lines = message.content.splitlines()
    preview = "\n".join(lines[:50])
    truncated = len(lines) > 50 or len(preview) > 10000
    return {**_public(message), "preview": preview[:10000], "preview_line_count": min(len(lines), 50),
            "total_line_count": len(lines), "truncated": truncated,
            "matched_terms": [term for term in terms if term in message.content.casefold()]}


def _source_ids(arguments: Mapping[str, object]) -> set[str]:
    values = list(cast(list[str], arguments.get("ids", []))) + list(cast(list[str], arguments.get("source_refs", [])))
    if arguments.get("source_ref"):
        values.append(cast(str, arguments["source_ref"]))
    for evidence in cast(list[dict[str, object]], arguments.get("evidence", [])):
        if evidence.get("source_ref"):
            values.append(cast(str, evidence["source_ref"]))
        refs = evidence.get("refs", [])
        if isinstance(refs, list):
            values.extend(refs)
    result: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        raw = value.split("#", 1)[0].strip()
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = raw
        if isinstance(parsed, str):
            parsed = [parsed]
        if isinstance(parsed, list):
            result.update(item.strip() for item in parsed if isinstance(item, str) and item.strip())
    if len(result) > 50:
        raise ValueError("fetch_messages accepts at most 50 distinct message references")
    return result


def build_message_history_tools(sessions: SessionStore, channels: MultichannelStore, *, role_id: str,
                                source_thread_id: str | None = None, is_owner: bool = True, view=None) -> tuple[MessageHistoryTool, ...]:
    history = ConversationHistory(sessions, channels, role_id=role_id, source_thread_id=source_thread_id, is_owner=is_owner, view=view)
    return MessageHistoryTool(history, search=True), MessageHistoryTool(history, search=False)
