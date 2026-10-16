"""Trusted group context tools used by channel turns.

The Shiori group tools deliberately keep group notes and member profiles out
of raw transcripts. Local structured data uses the durable multichannel
store; GroupToolAccess checks the actual run source and current authority.
Mutations recheck permission after obtaining the database write position.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..agent_runtime.tools import AgentToolResult, ToolContext, ToolDefinition, ToolRegistry
from .group_access import GroupToolAccess

_PAGE_SIZE = 20
_MEMBER_SOURCE_PAGE_SIZE = 5


@dataclass(frozen=True, slots=True)
class GroupRef:
    thread_id: str
    role_id: str
    channel: str
    account_id: str
    chat_id: str
    name: str


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
        with self.store._connect() as db:
            rows = db.execute(
                "SELECT thread_id,role_id,channel,account_id,chat_id,title FROM channel_threads "
                "WHERE role_id=? AND chat_type='group' ORDER BY thread_id", (role_id,),
            ).fetchall()
        return [GroupRef(str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]),
                         str(row[5] or row[4])) for row in rows]

    def has_group_data(self, role_id: str) -> bool:
        with self.store._connect() as db:
            return bool(db.execute(
                "SELECT EXISTS(SELECT 1 FROM channel_threads WHERE role_id=? AND chat_type='group')", (role_id,),
            ).fetchone()[0])

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

    def update_context(self, role_id: str, thread_id: str, *, authorize: Callable[[], None] | None = None,
                       **changes: str) -> Mapping[str, Any]:
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if authorize is not None:
                authorize()
            if self.get_group(role_id, thread_id) is None:
                raise ValueError("群不存在或不属于当前角色。")
            current = db.execute(
                "SELECT group_note,summary,summary_updated_at FROM channel_group_context WHERE role_id=? AND thread_id=?",
                (role_id, thread_id),
            ).fetchone()
            note = changes.get("group_note", str(current[0]) if current else "")
            summary = changes.get("summary", str(current[1]) if current else "")
            now = datetime.now(timezone.utc).isoformat()
            stamp = now if "summary" in changes else str(current[2]) if current else ""
            db.execute(
                "INSERT INTO channel_group_context(role_id,thread_id,group_note,summary,summary_updated_at,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(role_id,thread_id) DO UPDATE SET group_note=excluded.group_note,summary=excluded.summary,summary_updated_at=excluded.summary_updated_at,updated_at=excluded.updated_at",
                (role_id, thread_id, note, summary, stamp, now),
            )
        return {"groupNote": note, "summary": summary, "summaryUpdatedAt": stamp}

    def set_listening(self, role_id: str, account_id: str, chat_id: str, enabled: bool,
                      *, authorize: Callable[[], None]) -> bool:
        self.store.set_group_listening(role_id, account_id, chat_id, enabled, operator="role", authorize=authorize)
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

    def find_members(self, role_id: str, *, name: str, channel: str = "", thread_id: str | None = None) -> list[dict[str, Any]]:
        with self.store._connect() as db:
            rows = db.execute(
                "SELECT n.channel,n.sender_id,n.name FROM channel_member_names n "
                "LEFT JOIN channel_threads t ON t.role_id=n.role_id AND t.channel=n.channel AND t.account_id=n.account_id "
                "AND t.chat_id=n.chat_id AND t.chat_type=n.chat_type WHERE n.role_id=? "
                + ("AND t.thread_id=? " if thread_id is not None else "")
                + "ORDER BY n.last_seen DESC,n.rowid DESC", (role_id, thread_id) if thread_id is not None else (role_id,),
            ).fetchall()
        members: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (str(row["channel"]), str(row["sender_id"]))
            if channel and key[0] != channel:
                continue
            item = members.setdefault(key, {"channel": key[0], "member_id": key[1], "name": str(row["name"]), "nicknames": []})
            if row["name"] not in item["nicknames"]:
                item["nicknames"].append(str(row["name"]))
        return [item for item in members.values() if any(name.casefold() in nickname.casefold() for nickname in item["nicknames"])]

    def has_member_data(self, role_id: str) -> bool:
        with self.store._connect() as db:
            return bool(db.execute(
                "SELECT EXISTS(SELECT 1 FROM channel_member_names WHERE role_id=?) "
                "OR EXISTS(SELECT 1 FROM channel_member_profiles WHERE role_id=?)",
                (role_id, role_id),
            ).fetchone()[0])

    def member_profile(self, role_id: str, channel: str, member_id: str, *, thread_id: str | None = None,
                       source_page: int = 1) -> dict[str, Any]:
        member = next((item for item in self.find_members(role_id, name="", channel=channel, thread_id=thread_id)
                       if item["member_id"] == member_id), None)
        with self.store._connect() as db:
            observed = db.execute(
                "SELECT DISTINCT t.thread_id,n.account_id,n.chat_id,n.chat_type,"
                "COALESCE(p.profile,'') AS profile,COALESCE(p.shorthand,'') AS shorthand "
                "FROM channel_member_names n LEFT JOIN channel_threads t ON t.role_id=n.role_id AND t.channel=n.channel "
                "AND t.account_id=n.account_id AND t.chat_id=n.chat_id AND t.chat_type=n.chat_type "
                "LEFT JOIN channel_member_profiles p ON p.role_id=t.role_id AND p.thread_id=t.thread_id AND p.sender_id=n.sender_id "
                "WHERE n.role_id=? AND n.channel=? AND n.sender_id=?"
                + (" AND t.thread_id=?" if thread_id is not None else ""),
                (role_id, channel, member_id, thread_id) if thread_id is not None else (role_id, channel, member_id),
            ).fetchall()
            legacy = db.execute(
                "SELECT t.thread_id,t.account_id,t.chat_id,t.chat_type,p.profile,p.shorthand "
                "FROM channel_member_profiles p JOIN channel_threads t ON t.role_id=p.role_id AND t.thread_id=p.thread_id "
                "WHERE p.role_id=? AND t.channel=? AND p.sender_id=?"
                + (" AND t.thread_id=?" if thread_id is not None else ""),
                (role_id, channel, member_id, thread_id) if thread_id is not None else (role_id, channel, member_id),
            ).fetchall()
        sources = {(str(row["account_id"]), str(row["chat_id"]), str(row["chat_type"])): {
            "thread_id": str(row["thread_id"] or ""), "account_id": str(row["account_id"]),
            "chat_id": str(row["chat_id"]), "chat_type": str(row["chat_type"]),
            "profile": str(row["profile"]), "shorthand": str(row["shorthand"]),
        } for row in (*observed, *legacy)}
        ordered = [sources[key] for key in sorted(sources)]
        start = (source_page - 1) * _MEMBER_SOURCE_PAGE_SIZE
        end = start + _MEMBER_SOURCE_PAGE_SIZE
        return {**(member or {"channel": channel, "member_id": member_id, "name": "", "nicknames": []}),
                "found": bool(member or sources), "sources": ordered[start:end], "sources_total": len(ordered),
                "source_page": source_page, "next_source_page": source_page + 1 if end < len(ordered) else None}

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
        "lookup_group_context", "按 name（不区分大小写的群名片段）找候选，再按 group_thread_id 读取当前角色保存的群笔记、摘要及更新时间。省略名字和 ID 时分页列全部群，page 从1开始、每页20项；可查较早或离线记录，不返回聊天或旁听原文。",
        {"type": "object", "properties": {"name": {"type": "string"}, "group_thread_id": {"type": "string"}, "page": {"type": "integer", "minimum": 1}}, "additionalProperties": False},
        external_allowed=False, search_hint='群笔记 群摘要 群记忆 群动态 别的群 其他群 查群 找群',
    )

    def __init__(self, repository: MultichannelGroupRepository, *, role_id: str,
                 source_thread_id: str | None = None, is_owner: bool = True,
                 access: GroupToolAccess | None = None) -> None:
        self.repository = repository
        self.access = access or GroupToolAccess(repository.store, role_id=role_id,
            source_thread_id=source_thread_id, is_owner=is_owner)

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        try:
            ToolRegistry.validate_definition(self.definition, dict(arguments))
            self.access.owner(context)
            thread_id = str(arguments.get("group_thread_id") or "").strip()
            name = str(arguments.get("name") or "").strip()
            page = arguments.get("page", 1)
            if isinstance(page, bool) or not isinstance(page, int) or page < 1:
                raise ValueError("page 必须是从 1 开始的整数。")
            if thread_id:
                if name or page != 1:
                    raise ValueError("按 group_thread_id 读取时不能同时按名称查找或翻页。")
                group = self.repository.get_group(context.role_id, thread_id)
                if group is None:
                    raise ValueError("群不存在或不属于当前角色。")
                data = {"groupThreadId": group.thread_id, "channel": group.channel, "name": group.name,
                        **dict(self.repository.read_context(context.role_id, group.thread_id))}
            else:
                groups = [g for g in self.repository.list_groups(context.role_id) if not name or name.casefold() in g.name.casefold()]
                start = (page - 1) * _PAGE_SIZE
                rows = [{"groupThreadId": g.thread_id, "channel": g.channel, "name": g.name} for g in groups[start:start + _PAGE_SIZE]]
                data = {"total": len(groups), "page": page, "nextPage": page + 1 if start + _PAGE_SIZE < len(groups) else None, "candidates": rows}
            self.access.owner(context)
            return _result(data)
        except (ValueError, PermissionError) as error:
            return AgentToolResult(str(error), is_error=True)


class UpdateGroupContextTool:
    definition = ToolDefinition(
        "update_group_context", "更新当前角色的群笔记或摘要：至少提供 group_note/summary 之一，整篇替换；省略保留、空串清空；不修改聊天或旁听原文。",
        {"type": "object", "properties": {"group_thread_id": {"type": "string"}, "group_note": {"type": "string"}, "summary": {"type": "string"}}, "required": ["group_thread_id"], "additionalProperties": False},
        risk="mutating", external_allowed=False, search_hint='改群笔记 改群摘要 更新群记忆 修正群动态 清空群笔记',
    )

    def __init__(self, repository: MultichannelGroupRepository, *, role_id: str,
                 source_thread_id: str | None = None, is_owner: bool = True,
                 access: GroupToolAccess | None = None) -> None:
        self.repository = repository
        self.access = access or GroupToolAccess(repository.store, role_id=role_id,
            source_thread_id=source_thread_id, is_owner=is_owner)

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        try:
            ToolRegistry.validate_definition(self.definition, dict(arguments))
            self.access.owner(context)
            thread_id = str(arguments.get("group_thread_id") or "").strip()
            changes = {key: value for key, value in arguments.items() if key in {"group_note", "summary"} and isinstance(value, str)}
            if not thread_id or not changes:
                raise ValueError("必须提供群 ID，以及 group_note 或 summary 字符串。")
            group = self.repository.get_group(context.role_id, thread_id)
            if group is None:
                raise ValueError("群不存在或不属于当前角色。")
            data = await self.access.run_write(context, lambda: self.repository.update_context(
                context.role_id, thread_id, authorize=lambda: self.access.owner(context), **changes))
            return _result({"groupThreadId": thread_id, "channel": group.channel, "name": group.name, **dict(data)})
        except (ValueError, PermissionError) as error:
            return AgentToolResult(str(error), is_error=True)


class SetGroupListeningTool:
    definition = ToolDefinition(
        "set_group_listening", "打开或关闭 NapCat QQ 指定群的旁听。群友回合不能调用此工具；主人群回合只能设置当前群。",
        {"type": "object", "properties": {"channel": {"type": "string"}, "chat_id": {"type": "string"}, "enabled": {"type": "boolean"}}, "required": ["enabled"], "additionalProperties": False},
        risk="mutating", external_allowed=False, search_hint='旁听 看群 群消息',
    )

    def __init__(self, repository: MultichannelGroupRepository, *, role_id: str,
                 source_thread_id: str | None = None, is_owner: bool = True,
                 access: GroupToolAccess | None = None) -> None:
        self.repository = repository
        self.access = access or GroupToolAccess(repository.store, role_id=role_id,
            source_thread_id=source_thread_id, is_owner=is_owner)

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        try:
            ToolRegistry.validate_definition(self.definition, dict(arguments))
            source = self.access.source(context, owner=True)
            enabled = arguments.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("enabled 必须是布尔值。")
            source_group = self.repository.get_group(context.role_id, source.thread_id) if source and source.chat_type == "group" else None
            chat_id = str(arguments.get("chat_id") or (source_group.chat_id if source_group else ""))
            channel = str(arguments.get("channel") or (source_group.channel if source_group else ""))
            target = next((item for item in self.repository.list_groups(context.role_id) if item.channel == channel and item.chat_id == chat_id), None)
            if target is None or not self.repository.can_listen(context.role_id, target.account_id):
                raise ValueError("目标不是当前角色可旁听的 NapCat 群。")
            if source_group is not None and source_group.thread_id != target.thread_id:
                raise PermissionError("主人在群中只能设置当前群的旁听。")
            await self.access.run_write(context, lambda: self.repository.set_listening(
                context.role_id, target.account_id, target.chat_id, enabled,
                authorize=lambda: self.access.owner(context)))
            return _result({"channel": channel, "chatId": chat_id, "listening": enabled})
        except (ValueError, PermissionError, KeyError) as error:
            return AgentToolResult(str(error), is_error=True)


class LookupGroupMemberTool:
    definition = ToolDefinition(
        "lookup_group_member", "按 name（含旧昵称）找成员，或按 member_id 与 member_channel 读档案；二选一。主人桌面/有效私聊可跨渠道，群聊和访客只限当前线程。档案来源每页5条，用 source_page 继续。对应 Shiori lookup_member。",
        {"type": "object", "properties": {"name": {"type": "string"}, "member_id": {"type": "string"}, "member_channel": {"type": "string"},
            "source_page": {"type": "integer", "minimum": 1}}, "additionalProperties": False},
        search_hint="群友 昵称 是谁",
        external_allowed=True,
    )

    def __init__(self, repository: MultichannelGroupRepository, *, role_id: str, source_thread_id: str | None = None,
                 is_owner: bool = True, authorized: Callable[[ToolContext], bool] | None = None,
                 access: GroupToolAccess | None = None) -> None:
        self.repository = repository
        self.access = access or GroupToolAccess(repository.store, role_id=role_id,
            source_thread_id=source_thread_id, is_owner=is_owner, authorized=authorized)

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        try:
            ToolRegistry.validate_definition(self.definition, dict(arguments))
            name = str(arguments.get("name") or "").strip()
            member_id = str(arguments.get("member_id") or "").strip()
            channel = str(arguments.get("member_channel") or "").strip()
            source_page = arguments.get("source_page", 1)
            if bool(name) == bool(member_id):
                raise ValueError("member_id 与 name 必须且只能给一个。")
            if member_id and not channel:
                raise ValueError("按 member_id 查需要给出 member_channel。")
            if name and "source_page" in arguments:
                raise ValueError("source_page 只用于按 member_id 读取档案。")
            if isinstance(source_page, bool) or not isinstance(source_page, int) or source_page < 1:
                raise ValueError("source_page 必须从 1 开始。")
            thread_id = self.access.member_scope(context)
            if thread_id is not None:
                source = self.access.source(context)
                assert source is not None
                if channel and channel != source.channel:
                    raise PermissionError("当前回合只能查询当前线程的成员。")
                channel = source.channel
            if member_id:
                data = self.repository.member_profile(context.role_id, channel, member_id, thread_id=thread_id, source_page=source_page)
            else:
                members = self.repository.find_members(context.role_id, name=name, channel=channel, thread_id=thread_id)
                data = {"total": len(members), "candidates": members[:10]}
            self.access.member_scope(context)
            return _result(data)
        except (ValueError, PermissionError) as error:
            return AgentToolResult(str(error), is_error=True)
