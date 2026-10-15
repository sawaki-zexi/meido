"""Trusted group context tools used by channel turns.

The Shiori group tools deliberately keep group notes and member profiles out
of raw transcripts.  This module follows that boundary and exposes a small
repository protocol so the runtime can use the durable multichannel store
without passing a database connection (or untrusted model metadata) to a
tool.  Constructors receive the trusted turn claims (owner/current thread),
while all group ownership checks happen again in the repository.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..agent_runtime.tools import AgentToolResult, ToolContext, ToolDefinition

_PAGE_SIZE = 20


@dataclass(frozen=True, slots=True)
class GroupRef:
    thread_id: str
    role_id: str
    channel: str
    account_id: str
    chat_id: str
    name: str


class GroupRepository(Protocol):
    def list_groups(self, role_id: str) -> list[GroupRef]: ...
    def get_group(self, role_id: str, thread_id: str) -> GroupRef | None: ...
    def read_context(self, role_id: str, thread_id: str) -> Mapping[str, Any]: ...
    def update_context(self, role_id: str, thread_id: str, **changes: str) -> Mapping[str, Any]: ...
    def set_listening(self, role_id: str, account_id: str, chat_id: str, enabled: bool) -> bool: ...
    def listening(self, role_id: str, account_id: str, chat_id: str) -> bool: ...
    def can_listen(self, role_id: str, account_id: str) -> bool: ...
    def list_members(self, role_id: str, thread_id: str, name: str = "") -> list[Mapping[str, Any]]: ...


def _result(value: Mapping[str, Any]) -> AgentToolResult:
    return AgentToolResult(json.dumps(dict(value), ensure_ascii=False))


class MultichannelGroupRepository:
    """Repository backed by :class:`MultichannelStore`.

    It creates only the two group-context tables it owns.  Group membership is
    read from the durable target directory, so a member is returned only
    after NapCat has observed or discovered it.
    """

    def __init__(self, store: Any) -> None:
        self.store = store
        with store._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS channel_group_context (
                    role_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                    group_note TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
                    summary_updated_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                    PRIMARY KEY(role_id, thread_id)
                );
                CREATE TABLE IF NOT EXISTS channel_member_profiles (
                    role_id TEXT NOT NULL, thread_id TEXT NOT NULL, sender_id TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '', profile TEXT NOT NULL DEFAULT '', shorthand TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL, PRIMARY KEY(role_id,thread_id,sender_id)
                );
                """
            )

    def list_groups(self, role_id: str) -> list[GroupRef]:
        return [
            GroupRef(t.thread_id, t.role_id, t.channel, t.account_id, t.chat_id, t.title or t.chat_id)
            for t in self.store.list_threads(role_id, limit=500)
            if t.chat_type == "group"
        ]

    def get_group(self, role_id: str, thread_id: str) -> GroupRef | None:
        thread = self.store.get_thread(role_id, thread_id)
        if thread is None or thread.chat_type != "group":
            return None
        return GroupRef(thread.thread_id, thread.role_id, thread.channel, thread.account_id, thread.chat_id, thread.title or thread.chat_id)

    def read_context(self, role_id: str, thread_id: str) -> Mapping[str, Any]:
        with self.store._connect() as db:
            row = db.execute("SELECT group_note,summary,summary_updated_at FROM channel_group_context WHERE role_id=? AND thread_id=?", (role_id, thread_id)).fetchone()
        if row is None:
            return {"groupNote": "", "summary": "", "summaryUpdatedAt": ""}
        return {"groupNote": str(row[0]), "summary": str(row[1]), "summaryUpdatedAt": str(row[2])}

    def update_context(self, role_id: str, thread_id: str, **changes: str) -> Mapping[str, Any]:
        current = dict(self.read_context(role_id, thread_id))
        note = str(changes.get("group_note", current["groupNote"]))
        summary = str(changes.get("summary", current["summary"]))
        stamp = datetime.now(timezone.utc).isoformat() if "summary" in changes else str(current["summaryUpdatedAt"])
        now = datetime.now(timezone.utc).isoformat()
        with self.store._connect() as db:
            db.execute(
                "INSERT INTO channel_group_context(role_id,thread_id,group_note,summary,summary_updated_at,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(role_id,thread_id) DO UPDATE SET group_note=excluded.group_note,summary=excluded.summary,summary_updated_at=excluded.summary_updated_at,updated_at=excluded.updated_at",
                (role_id, thread_id, note, summary, stamp, now),
            )
        return {"groupNote": note, "summary": summary, "summaryUpdatedAt": stamp}

    def set_listening(self, role_id: str, account_id: str, chat_id: str, enabled: bool) -> bool:
        self.store.set_group_listening(role_id, account_id, chat_id, enabled, operator="role")
        return bool(enabled)

    def listening(self, role_id: str, account_id: str, chat_id: str) -> bool:
        return self.store.group_listening(role_id, account_id, chat_id)

    def can_listen(self, role_id: str, account_id: str) -> bool:
        account = self.store.get_account(account_id)
        return bool(account and account.role_id == role_id and account.channel == "qq")

    def list_members(self, role_id: str, thread_id: str, name: str = "") -> list[Mapping[str, Any]]:
        group = self.get_group(role_id, thread_id)
        if group is None:
            return []
        with self.store._connect() as db:
            profiles = {str(row["sender_id"]): dict(row) for row in db.execute("SELECT sender_id,name,profile,shorthand FROM channel_member_profiles WHERE role_id=? AND thread_id=?", (role_id, thread_id))}
        result: list[Mapping[str, Any]] = []
        for target in self.store.list_targets(role_id, group.account_id):
            if getattr(getattr(target, "kind", None), "value", "") != "group_member":
                continue
            if str((getattr(target, "metadata", {}) or {}).get("group_id", "")) != group.chat_id:
                continue
            sender_id = str(getattr(target, "chat_id", ""))
            profile = profiles.get(sender_id, {})
            display = str(getattr(target, "display_name", "")) or str(profile.get("name") or "")
            if name and name.casefold() not in display.casefold() and name not in str(getattr(target, "chat_id", "")):
                continue
            result.append({"id": sender_id, "name": display, "targetId": str(getattr(target, "id", "")), "profile": str(profile.get("profile") or ""), "shorthand": str(profile.get("shorthand") or "")})
        return result

    def prompt_context(self, role_id: str, thread_id: str, sender_id: str = "") -> str:
        """Read only the current thread's notes and relevant member profiles."""

        thread = self.store.get_thread(role_id, thread_id)
        if thread is None or thread.channel == "desktop":
            return ""
        parts = []
        if thread.chat_type == "group":
            group_context = self.read_context(role_id, thread_id)
            if group_context.get("groupNote"):
                parts.append("[当前群笔记]\n" + str(group_context["groupNote"])[:12000])
            if group_context.get("summary"):
                parts.append("[当前群近期摘要]\n" + str(group_context["summary"])[:12000])
        visible_senders = {sender_id} if sender_id else set()
        if thread.chat_type == "group":
            for message in self.store.sessions.list_messages(thread.session_key)[-30:]:
                visible_senders.add(str(message.metadata.get("senderId") or ""))
                visible_senders.update(str(item) for item in message.metadata.get("mentionedIds", []) if isinstance(item, str))
            for message in self.store.listening.recent(role_id, thread_id)[-30:]:
                visible_senders.add(str(message["metadata"].get("senderId") or ""))
        with self.store._connect() as db:
            rows = db.execute("SELECT sender_id,name,profile,shorthand FROM channel_member_profiles WHERE role_id=? AND thread_id=? ORDER BY updated_at DESC", (role_id, thread_id)).fetchall()
        members = []
        for row in rows:
            if str(row["sender_id"]) in visible_senders and (row["profile"] or row["shorthand"]):
                members.append({"senderId": str(row["sender_id"]), "name": str(row["name"]), "profile": str(row["profile"])[:4000], "shorthand": str(row["shorthand"])[:200]})
                if len(members) >= 10:
                    break
        if members:
            parts.append("[当前会话成员资料；来源是此前在此会话观察到的信息]\n" + json.dumps(members, ensure_ascii=False))
        return "\n\n".join(parts)[:20000]


class LookupGroupContextTool:
    definition = ToolDefinition(
        "lookup_group_context", "读取当前角色保存的群笔记和摘要；不返回群聊天原文。",
        {"type": "object", "properties": {"name": {"type": "string"}, "group_thread_id": {"type": "string"}, "page": {"type": "integer", "minimum": 1}}, "additionalProperties": False},
        external_allowed=False,
    )

    def __init__(self, repository: GroupRepository, *, is_owner: bool = True) -> None:
        self.repository, self.is_owner = repository, is_owner

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        if not self.is_owner:
            return AgentToolResult("群笔记仅主人回合可用。", is_error=True)
        thread_id = str(arguments.get("group_thread_id") or "").strip()
        name = str(arguments.get("name") or "").strip()
        page = arguments.get("page", 1)
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            return AgentToolResult("page 必须是从 1 开始的整数。", is_error=True)
        if thread_id:
            if name or page != 1:
                return AgentToolResult("按 group_thread_id 读取时不能同时按名称查找或翻页。", is_error=True)
            group = self.repository.get_group(context.role_id, thread_id)
            if group is None:
                return AgentToolResult("群不存在或不属于当前角色。", is_error=True)
            data = self.repository.read_context(context.role_id, group.thread_id)
            return _result({"groupThreadId": group.thread_id, "channel": group.channel, "name": group.name, **dict(data)})
        groups = [g for g in self.repository.list_groups(context.role_id) if not name or name.casefold() in g.name.casefold()]
        start = (page - 1) * _PAGE_SIZE
        rows = [{"groupThreadId": g.thread_id, "channel": g.channel, "name": g.name} for g in groups[start:start + _PAGE_SIZE]]
        return _result({"total": len(groups), "page": page, "nextPage": page + 1 if start + _PAGE_SIZE < len(groups) else None, "candidates": rows})


class UpdateGroupContextTool:
    definition = ToolDefinition(
        "update_group_context", "更新当前角色的群笔记或摘要，不修改聊天原文。",
        {"type": "object", "properties": {"group_thread_id": {"type": "string"}, "group_note": {"type": "string"}, "summary": {"type": "string"}}, "required": ["group_thread_id"], "additionalProperties": False},
        risk="mutating", external_allowed=False,
    )

    def __init__(self, repository: GroupRepository, *, is_owner: bool = True) -> None:
        self.repository, self.is_owner = repository, is_owner

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        if not self.is_owner:
            return AgentToolResult("群笔记仅主人回合可修改。", is_error=True)
        thread_id = str(arguments.get("group_thread_id") or "").strip()
        changes = {key: arguments[key] for key in ("group_note", "summary") if key in arguments}
        if not thread_id or not changes or any(not isinstance(value, str) for value in changes.values()):
            return AgentToolResult("必须提供群 ID，以及 group_note 或 summary 字符串。", is_error=True)
        group = self.repository.get_group(context.role_id, thread_id)
        if group is None:
            return AgentToolResult("群不存在或不属于当前角色。", is_error=True)
        data = self.repository.update_context(context.role_id, thread_id, **changes)
        return _result({"groupThreadId": thread_id, "channel": group.channel, "name": group.name, **dict(data)})


class SetGroupListeningTool:
    definition = ToolDefinition(
        "set_group_listening", "打开或关闭 NapCat QQ 指定群的旁听。群友回合不能调用此工具。",
        {"type": "object", "properties": {"channel": {"type": "string"}, "chat_id": {"type": "string"}, "enabled": {"type": "boolean"}}, "required": ["enabled"], "additionalProperties": False},
        risk="mutating", external_allowed=False,
    )

    def __init__(self, repository: GroupRepository, *, is_owner: bool = True, source_thread_id: str | None = None) -> None:
        self.repository, self.is_owner, self.source_thread_id = repository, is_owner, source_thread_id

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        if not self.is_owner:
            return AgentToolResult("只有主人可以设置群旁听。", is_error=True)
        enabled = arguments.get("enabled")
        if not isinstance(enabled, bool):
            return AgentToolResult("enabled 必须是布尔值。", is_error=True)
        group = self.repository.get_group(context.role_id, str(self.source_thread_id or "")) if not arguments.get("chat_id") else None
        chat_id = str(arguments.get("chat_id") or (group.chat_id if group else ""))
        channel = str(arguments.get("channel") or (group.channel if group else ""))
        target = next((item for item in self.repository.list_groups(context.role_id) if item.channel == channel and item.chat_id == chat_id), None)
        if target is None or not self.repository.can_listen(context.role_id, target.account_id):
            return AgentToolResult("目标不是当前角色可旁听的 NapCat 群。", is_error=True)
        source_group = self.repository.get_group(context.role_id, str(self.source_thread_id or ""))
        if source_group is not None and source_group.thread_id != target.thread_id:
            return AgentToolResult("主人在群中只能设置当前群的旁听。", is_error=True)
        self.repository.set_listening(context.role_id, target.account_id, target.chat_id, enabled)
        return _result({"channel": channel, "chatId": chat_id, "listening": bool(enabled)})


class LookupGroupMemberTool:
    definition = ToolDefinition(
        "lookup_group_member", "查询已观察到的当前群成员资料。",
        {"type": "object", "properties": {"name": {"type": "string"}}, "additionalProperties": False},
        external_allowed=True,
    )

    def __init__(self, repository: GroupRepository, *, source_thread_id: str | None) -> None:
        self.repository, self.source_thread_id = repository, source_thread_id

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        group = self.repository.get_group(context.role_id, str(self.source_thread_id or ""))
        if group is None:
            return AgentToolResult("成员查询只能读取当前群。", is_error=True)
        members = self.repository.list_members(context.role_id, group.thread_id, str(arguments.get("name") or ""))
        return _result({"groupThreadId": group.thread_id, "members": [dict(item) for item in members]})
