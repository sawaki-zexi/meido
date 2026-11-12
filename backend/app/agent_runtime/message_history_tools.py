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
from ..conversation_history import ConversationHistoryView, _quote_lookup, _user_projection
from ..context_windows import _stamp
from ..channels.conversation_context import message_source
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
        self._listening_scope = None
        self._listening_ids = None
        self._listening_fingerprint = None
        self.source_aliases = {}

    def validate(self, context):
        context.signal.raise_if_cancelled()
        if self.view is not None:
            self.view.require_snapshot_current()
        if self._listening_scope is not None:
            self._listening_messages()

    def _listening_messages(self):
        thread = self.channels.get_thread(self.role_id, self.thread_id) if self.thread_id else None
        if thread is None or thread.channel != 'qq' or thread.chat_type != 'group':
            if self._listening_scope is not None:
                raise PermissionError('旁听历史来源已失效')
            return []
        enabled = self.channels.group_listening(self.role_id, thread.account_id, thread.chat_id)
        bindings = [{key: binding[key] for key in ('identityId', 'pairedAt', 'platformUserId', 'scope', 'accountId')}
            for binding in self.channels.list_identities() if binding['channel'] == thread.channel
                and (binding['scope'] == 'platform' or binding['accountId'] == thread.account_id)]
        scope = (thread.account_id, thread.session_key, thread.chat_id,
            self.channels.listening.revision(thread.thread_id), enabled, bindings)
        if self._listening_scope is not None and scope != self._listening_scope:
            raise PermissionError('旁听历史许可已变化')
        with self.channels._connect() as db:
            rows = db.execute('SELECT * FROM channel_listening_records WHERE thread_id=? ORDER BY sequence',
                (thread.thread_id,)).fetchall()
        records = [self.channels.listening._message(row) for row in rows]
        if enabled:
            records.extend(record for record in self.channels.listening.recent(self.role_id, thread.thread_id)
                if record['sequence'] is None)
        messages = [Message.model_validate({**record, 'sessionKey': thread.session_key,
            'sequence': record['sequence'] or 0, 'metadata': {**record['metadata'],
                'roleId': thread.role_id, 'accountId': thread.account_id, 'threadId': thread.thread_id,
                'channel': thread.channel, 'chatId': thread.chat_id, 'chatType': thread.chat_type,
                'listening': True}}) for record in records]
        messages = [message for message in messages if message.createdAt <= self.view.admitted_at
            and (self._listening_ids is None or message.id in self._listening_ids)]
        fingerprint = _stamp([message.model_dump(mode='json') for message in messages])
        if self._listening_fingerprint is not None and fingerprint != self._listening_fingerprint:
            raise PermissionError('旁听原文已变化或离开临时窗口')
        self._listening_scope = scope
        self._listening_ids = {message.id for message in messages}
        self._listening_fingerprint = fingerprint
        return messages

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
        messages = [message for message in messages
                if message.status == "completed" and message.messageType == "text"
                and message.role in {"user", "assistant"}
                and not (message.role == 'user' and message.metadata.get('listening') is True)]
        listening = self._listening_messages()
        if self._listening_scope is not None:
            unique = {}
            for message in [*messages, *listening]:
                key = (message.sessionKey, message.metadata.get('accountId'),
                    message.metadata.get('platformMessageId') or message.id)
                canonical = unique.setdefault(key, message)
                self.source_aliases[message.id] = canonical.id
            messages = sorted(unique.values(), key=lambda message: (message.createdAt.timestamp(), message.sequence, message.id))
        quotes = _quote_lookup(messages)
        messages = [message.model_copy(update={'metadata': _user_projection(message, quotes)[1]})
            if message.role == 'user' and message.metadata.get('replyTo') else message for message in messages]
        self.validate(context)
        return messages


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
            "content_offset": {"type": "integer", "minimum": 0},
            "content_limit": {"type": "integer", "minimum": 1, "maximum": 10000},
        })
        self.definition = ToolDefinition(
            name="search_messages" if search else "fetch_messages",
            description=("按关键词定位获准历史，返回预览和source_ref；以原话作证前用fetch_messages核验。" if search
                         else "按source_ref读取获准消息原文，可带前后文；长正文按next_content_offset续读，missing_source_refs表示缺失，omitted_source_refs表示本页预算省略，可减少引用或调低content_limit后重试。"),
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
            canonical_refs = {self.history.source_aliases.get(ref, ref) for ref in refs}
            matched = {message.id for message in messages if message.id in canonical_refs}
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
            offset = cast(int, arguments.get('content_offset', 0))
            limit = cast(int, arguments.get('content_limit', 10000))
            payload = {"count": len(selected), "matched_count": len(matched),
                       "messages": [_original(message, matched, offset, limit) for message in selected]}
            missing = sorted(ref for ref in refs if self.history.source_aliases.get(ref, ref) not in matched)
            if missing:
                payload.update(missing_source_refs=_bounded_refs(missing), missing_count=len(missing))
        payload = _bounded_payload(payload)
        _check_cancelled(context, stopped)
        self.history.validate(context)
        return AgentToolResult(json.dumps(payload, ensure_ascii=False), details={"status": "succeeded", "count": payload["count"]})


def _public(message: Message) -> dict[str, object]:
    result = {"id": message.id, "source_ref": message.id, "session_key": message.sessionKey,
            "seq": message.sequence, "role": message.role, "timestamp": message.createdAt.isoformat()}
    if message.metadata.get('channel'):
        source = message_source(message.metadata, source_id=message.id, created_at=message.createdAt,
            session_key=message.sessionKey, role=message.role)
        # A cached quote body has a separate original-source visibility check.
        source.pop('replyToContent', None)
        result['source'] = source
    return result


def _original(message, matched, offset, limit):
    content = message.content[offset:offset + limit]
    result = {**_public(message), 'content': content, 'in_source_ref': message.id in matched}
    if offset or len(content) < len(message.content):
        next_offset = offset + len(content)
        result.update(content_offset=offset, content_length=len(message.content),
            truncated=next_offset < len(message.content),
            next_content_offset=next_offset if next_offset < len(message.content) else None)
    return result


def _bounded_refs(refs):
    result = []
    for ref in refs:
        if len(json.dumps([*result, ref], ensure_ascii=False)) > 4000:
            break
        result.append(ref)
    return result


def _bounded_payload(payload):
    original = payload['messages']
    # Exact requested originals have priority over their optional neighbors.
    priority = sorted(original, key=lambda item: not item.get('in_source_ref', True))
    result = {**payload, 'messages': []}
    remaining = 24000 - len(json.dumps(result, ensure_ascii=False)) - 4200
    selected = set()
    for item in priority:
        size = len(json.dumps(item, ensure_ascii=False)) + 2
        if size <= remaining and len(selected) < 50:
            selected.add(item['id'])
            remaining -= size
    result['messages'] = [item for item in original if item['id'] in selected]
    result['count'] = len(result['messages'])
    omitted = [item['id'] for item in original if item['id'] not in selected]
    if omitted:
        result.update(omitted_source_refs=_bounded_refs(omitted), omitted_count=len(omitted), truncated=True)
    return result


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
