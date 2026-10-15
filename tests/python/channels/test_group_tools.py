from __future__ import annotations

import asyncio
import json
from pathlib import Path
from datetime import datetime, timezone

from backend.app.channels.group_tools import (
    LookupGroupContextTool,
    LookupGroupMemberTool,
    MultichannelGroupRepository,
    SetGroupListeningTool,
    UpdateGroupContextTool,
)
from backend.app.channels.models import TargetKind, target_from_chat
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.tools import ToolContext
from backend.app.models import AgentRun


def _context(role: str = "role", session: str = "thread") -> ToolContext:
    return ToolContext(role, session, "run", CancellationToken())


def _store(tmp_path: Path) -> tuple[MultichannelStore, str]:
    sessions = SessionStore(tmp_path / "sessions.db")
    store = MultichannelStore(tmp_path / "sessions.db", sessions)
    account = store.register_channel_account("role", "qq", account_id="qq-1", enabled=True, allow_group=True)
    group = store.ensure_channel_thread("role", "qq", account.account_id, "g-1", "group", title="Test Group")
    store.remember_targets("role", [
        target_from_chat(account_id="qq-1", chat_id="g-1", kind=TargetKind.GROUP, display_name="Test Group"),
        target_from_chat(account_id="qq-1", chat_id="u-1", kind=TargetKind.GROUP_MEMBER, display_name="Alice", group_id="g-1"),
    ])
    return store, group.thread_id


def test_group_notes_are_owner_only_and_never_raw_history(tmp_path: Path) -> None:
    store, thread_id = _store(tmp_path)
    repo = MultichannelGroupRepository(store)
    owner = LookupGroupContextTool(repo, is_owner=True)
    result = asyncio.run(owner.execute({"group_thread_id": thread_id}, _context()))
    assert json.loads(result.content)["groupNote"] == ""
    update = UpdateGroupContextTool(repo, is_owner=True)
    asyncio.run(update.execute({"group_thread_id": thread_id, "group_note": "note"}, _context()))
    data = json.loads(asyncio.run(owner.execute({"group_thread_id": thread_id}, _context())).content)
    assert data["groupNote"] == "note"
    guest = LookupGroupContextTool(repo, is_owner=False)
    assert asyncio.run(guest.execute({}, _context())).is_error


def test_group_member_lookup_is_current_group_only(tmp_path: Path) -> None:
    store, thread_id = _store(tmp_path)
    repo = MultichannelGroupRepository(store)
    thread = store.get_thread("role", thread_id)
    _, message = store.append_event(thread, direction="inbound", content="查成员", external_message_id="query",
        metadata={"senderId": "u-1", "senderName": "Alice", "senderIsOwner": False})
    run = AgentRun(runId="run", roleId="role", sessionKey=thread.session_key,
        status="created", startedAt=datetime.now(timezone.utc))
    store.sessions.create_run_with_existing_user_message(run, message.id)
    tool = LookupGroupMemberTool(repo, role_id="role", source_thread_id=thread_id, is_owner=False)
    result = json.loads(asyncio.run(tool.execute({"name": "ali"}, _context(session=thread.session_key))).content)
    assert result["candidates"][0]["name"] == "Alice"


def test_listening_tool_restricts_to_napcat_group(tmp_path: Path) -> None:
    store, thread_id = _store(tmp_path)
    repo = MultichannelGroupRepository(store)
    tool = SetGroupListeningTool(repo, is_owner=True, source_thread_id=thread_id)
    result = asyncio.run(tool.execute({"enabled": True}, _context()))
    assert json.loads(result.content)["listening"] is True
    assert store.group_listening("role", "qq-1", "g-1") is True
