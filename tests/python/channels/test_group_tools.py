from __future__ import annotations

import asyncio
import json
import pytest
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Event

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
from backend.app.channels.group_access import GroupToolAccess
from backend.app.role_store import RoleStore
from backend.app.models import RoleInput


def _context(role: str = "role", session: str = "role:role") -> ToolContext:
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
    owner = LookupGroupContextTool(repo, role_id="role", is_owner=True)
    result = asyncio.run(owner.execute({"group_thread_id": thread_id}, _context()))
    assert json.loads(result.content)["groupNote"] == ""
    update = UpdateGroupContextTool(repo, role_id="role", is_owner=True)
    asyncio.run(update.execute({"group_thread_id": thread_id, "group_note": "note"}, _context()))
    data = json.loads(asyncio.run(owner.execute({"group_thread_id": thread_id}, _context())).content)
    assert data["groupNote"] == "note"
    guest = LookupGroupContextTool(repo, role_id="role", is_owner=False)
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
    tool = SetGroupListeningTool(repo, role_id="role", is_owner=True)
    result = asyncio.run(tool.execute({"enabled": True, "channel": "qq", "chat_id": "g-1"}, _context()))
    assert json.loads(result.content)["listening"] is True
    assert store.group_listening("role", "qq-1", "g-1") is True


def test_group_directory_keeps_old_groups_beyond_recent_mixed_threads(tmp_path: Path) -> None:
    store, old_id = _store(tmp_path)
    for number in range(501):
        store.ensure_channel_thread("role", "qq", "qq-1", f"private-{number}", "private")
    for number in range(21):
        store.ensure_channel_thread("role", "qq", "qq-1", f"group-{number}", "group", title=f"Saved {number:02}")
    store.register_channel_account("other", "qq", account_id="other-account")
    store.ensure_channel_thread("other", "qq", "other-account", "foreign", "group")
    tool = LookupGroupContextTool(MultichannelGroupRepository(store), role_id="role")
    context = _context(session="role:role")
    old = json.loads(asyncio.run(tool.execute({"name": "test group"}, context)).content)
    assert old["total"] == 1 and old["candidates"][0]["groupThreadId"] == old_id
    first = json.loads(asyncio.run(tool.execute({}, context)).content)
    second = json.loads(asyncio.run(tool.execute({"page": 2}, context)).content)
    assert first["total"] == second["total"] == 22
    assert len(first["candidates"]) == 20 and first["nextPage"] == 2
    assert len(second["candidates"]) == 2 and second["nextPage"] is None
    assert first == json.loads(asyncio.run(tool.execute({}, context)).content)


@pytest.mark.parametrize("tool_class,arguments", [
    (LookupGroupContextTool, {"name": ["Test"]}),
    (LookupGroupContextTool, {"group_thread_id": 4}),
    (LookupGroupContextTool, {"name": "Test", "is_owner": True}),
    (LookupGroupContextTool, {"page": True}),
    (LookupGroupContextTool, {"page": 0}),
    (LookupGroupContextTool, {"page": 1.5}),
    (UpdateGroupContextTool, {"group_thread_id": "ID"}),
    (UpdateGroupContextTool, {"group_thread_id": 4, "summary": "invalid"}),
    (UpdateGroupContextTool, {"group_thread_id": "ID", "summary": False}),
    (UpdateGroupContextTool, {"group_thread_id": "ID", "group_note": "note", "role_id": "foreign"}),
    (SetGroupListeningTool, {"enabled": True, "senderIsOwner": True}),
    (SetGroupListeningTool, {"enabled": "true"}),
])
def test_group_tools_reject_invalid_types_and_identity_arguments(tmp_path, tool_class, arguments):
    store, thread_id = _store(tmp_path)
    arguments = {key: thread_id if value == "ID" else value for key, value in arguments.items()}
    result = asyncio.run(tool_class(MultichannelGroupRepository(store), role_id="role").execute(arguments, _context(session="role:role")))
    assert result.is_error


def test_group_context_patch_preserves_omitted_fields_and_clears_explicit_empty_strings(tmp_path):
    store, thread_id = _store(tmp_path)
    repository = MultichannelGroupRepository(store)
    tool = UpdateGroupContextTool(repository, role_id="role")
    context = _context(session="role:role")
    first = json.loads(asyncio.run(tool.execute({"group_thread_id": thread_id, "group_note": "笔记", "summary": "摘要"}, context)).content)
    assert first["groupNote"] == "笔记" and first["summary"] == "摘要" and first["summaryUpdatedAt"]
    second = json.loads(asyncio.run(tool.execute({"group_thread_id": thread_id, "group_note": ""}, context)).content)
    assert second["groupNote"] == "" and second["summary"] == "摘要" and second["summaryUpdatedAt"] == first["summaryUpdatedAt"]
    third = json.loads(asyncio.run(tool.execute({"group_thread_id": thread_id, "summary": ""}, context)).content)
    assert third["groupNote"] == third["summary"] == "" and third["summaryUpdatedAt"] > first["summaryUpdatedAt"]


@pytest.mark.parametrize("tool_class,arguments", [
    (LookupGroupContextTool, {}),
    (UpdateGroupContextTool, {"group_thread_id": "ID", "group_note": "forged"}),
    (SetGroupListeningTool, {"channel": "qq", "chat_id": "g-1", "enabled": True}),
])
def test_owner_boolean_cannot_authorize_a_forged_or_cancelled_source(tmp_path, tool_class, arguments):
    store, thread_id = _store(tmp_path)
    arguments = {key: thread_id if value == "ID" else value for key, value in arguments.items()}
    tool = tool_class(MultichannelGroupRepository(store), role_id="role", is_owner=True)
    assert asyncio.run(tool.execute(arguments, _context(session="thread:invented"))).is_error
    cancelled = _context(session="role:role")
    cancelled.signal.cancel()
    assert asyncio.run(tool.execute(arguments, cancelled)).is_error


def _source(store, *, owner=True, chat_type="group"):
    if owner:
        code = store.create_pairing_code("role", "qq")
        assert store.consume_pairing_code(code, role_id="role", channel="qq", scope="platform",
            platform_user_id="sender", account_id="qq-1", chat_id="source")
    thread = store.ensure_channel_thread("role", "qq", "qq-1", "source", chat_type)
    _, message = store.append_event(thread, direction="inbound", content="RAW_SOURCE",
        external_message_id="source", metadata={"senderId": "sender", "senderIsOwner": owner})
    run = AgentRun(runId="source-run", roleId="role", sessionKey=thread.session_key,
        status="created", startedAt=datetime.now(timezone.utc))
    store.sessions.create_run_with_existing_user_message(run, message.id)
    return thread, ToolContext("role", thread.session_key, run.runId, CancellationToken())


@pytest.mark.parametrize("owner,chat_type", [(True, "group"), (True, "private"), (False, "group"), (False, "private")])
def test_group_notes_are_role_wide_for_real_owners_and_listening_is_current_group_only(tmp_path, owner, chat_type):
    store, other_id = _store(tmp_path)
    source, context = _source(store, owner=owner, chat_type=chat_type)
    repository = MultichannelGroupRepository(store)
    access = GroupToolAccess(store, role_id="role", source_thread_id=source.thread_id, is_owner=owner)
    lookup = LookupGroupContextTool(repository, role_id="role", access=access)
    update = UpdateGroupContextTool(repository, role_id="role", access=access)
    listening = SetGroupListeningTool(repository, role_id="role", access=access)
    result = asyncio.run(update.execute({"group_thread_id": other_id, "group_note": "OWNER_NOTE"}, context))
    assert result.is_error is not owner
    result = asyncio.run(lookup.execute({"group_thread_id": other_id}, context))
    assert result.is_error is not owner
    if owner:
        assert json.loads(result.content)["groupNote"] == "OWNER_NOTE"
    assert "RAW_SOURCE" not in result.content
    result = asyncio.run(listening.execute({"channel": "qq", "chat_id": "g-1", "enabled": True}, context))
    assert result.is_error is (not owner or chat_type == "group")
    if owner and chat_type == "group":
        assert not asyncio.run(listening.execute({"enabled": True}, context)).is_error
        assert store.listening.state("role", source.thread_id)["toggles"][0]["operator"] == "role"


@pytest.mark.parametrize("change", ["platform", "disabled", "block", "scope", "unbind", "rebind", "recreate", "cancel"])
@pytest.mark.parametrize("kind", ["lookup", "update", "listening", "member"])
def test_all_group_tools_recheck_real_source_revocation(tmp_path, kind, change):
    store, thread_id = _store(tmp_path)
    source, context = _source(store, chat_type="private")
    repository = MultichannelGroupRepository(store)
    classes = {"lookup": LookupGroupContextTool, "update": UpdateGroupContextTool,
               "listening": SetGroupListeningTool, "member": LookupGroupMemberTool}
    tool = classes[kind](repository, role_id="role", source_thread_id=source.thread_id)
    arguments = {"lookup": {"group_thread_id": thread_id},
                 "update": {"group_thread_id": thread_id, "group_note": "kept"},
                 "listening": {"channel": "qq", "chat_id": "g-1", "enabled": True},
                 "member": {"name": "Alice"}}[kind]
    assert not asyncio.run(tool.execute(arguments, context)).is_error
    if change == "platform":
        store.set_channel_enabled("qq", False)
    elif change == "disabled":
        store.set_account_state("qq-1", enabled=False)
    elif change == "cancel":
        context.signal.cancel()
    elif change in {"unbind", "rebind"}:
        store.unbind_identity(store.list_identities()[0]["identityId"])
        if change == "rebind":
            code = store.create_pairing_code("role", "qq")
            assert store.consume_pairing_code(code, role_id="role", channel="qq", scope="platform",
                platform_user_id="sender", account_id="qq-1", chat_id="source")
    elif change == "recreate":
        store.delete_account("qq-1")
        store.register_channel_account("role", "qq", account_id="qq-1", enabled=True, allow_group=True)
    else:
        store.register_channel_account("role", "qq", account_id="qq-1", enabled=True, allow_group=True,
            **({"blocked_senders": ["sender"]} if change == "block" else {"allow_private": False}))
    assert asyncio.run(tool.execute(arguments, context)).is_error


@pytest.mark.parametrize("change", ["rebind", "recreate"])
def test_old_inbound_cannot_capture_a_new_binding_or_account_generation(tmp_path, change):
    store, _ = _store(tmp_path)
    source, context = _source(store, chat_type="private")
    if change == "rebind":
        store.unbind_identity(store.list_identities()[0]["identityId"])
        code = store.create_pairing_code("role", "qq")
        assert store.consume_pairing_code(code, role_id="role", channel="qq", scope="platform",
            platform_user_id="sender", account_id="qq-1", chat_id="source")
    else:
        store.delete_account("qq-1")
        store.register_channel_account("role", "qq", account_id="qq-1", enabled=True, allow_group=True)
    tool = LookupGroupContextTool(MultichannelGroupRepository(store), role_id="role", source_thread_id=source.thread_id)
    assert asyncio.run(tool.execute({}, context)).is_error


@pytest.mark.parametrize("tool_class,arguments", [
    (LookupGroupContextTool, {}),
    (UpdateGroupContextTool, {"group_thread_id": "ID", "summary": "forged"}),
    (SetGroupListeningTool, {"channel": "qq", "chat_id": "g-1", "enabled": True}),
])
def test_actual_guest_inbound_cannot_be_promoted_by_an_owner_constructor_claim(tmp_path, tool_class, arguments):
    store, thread_id = _store(tmp_path)
    source, context = _source(store, owner=False)
    arguments = {key: thread_id if value == "ID" else value for key, value in arguments.items()}
    tool = tool_class(MultichannelGroupRepository(store), role_id="role", source_thread_id=source.thread_id, is_owner=True)
    assert asyncio.run(tool.execute(arguments, context)).is_error


def test_private_or_foreign_group_ids_cannot_be_read_or_changed_and_deleted_accounts_keep_local_notes(tmp_path):
    store, thread_id = _store(tmp_path)
    private = store.ensure_channel_thread("role", "qq", "qq-1", "private", "private")
    store.register_channel_account("other", "qq", account_id="other")
    foreign = store.ensure_channel_thread("other", "qq", "other", "100", "group")
    repository = MultichannelGroupRepository(store)
    lookup = LookupGroupContextTool(repository, role_id="role")
    update = UpdateGroupContextTool(repository, role_id="role")
    for target_id in (private.thread_id, foreign.thread_id):
        assert asyncio.run(lookup.execute({"group_thread_id": target_id}, _context())).is_error
        assert asyncio.run(update.execute({"group_thread_id": target_id, "summary": "forbidden"}, _context())).is_error
    assert not asyncio.run(update.execute({"group_thread_id": thread_id, "group_note": "saved"}, _context())).is_error
    store.delete_account("qq-1")
    assert json.loads(asyncio.run(lookup.execute({"group_thread_id": thread_id}, _context())).content)["groupNote"] == "saved"
    assert not asyncio.run(update.execute({"group_thread_id": thread_id, "group_note": "after deletion"}, _context())).is_error


@pytest.mark.parametrize("kind", ["update", "listening"])
@pytest.mark.parametrize("change", ["cancel", "role"])
def test_waiting_group_write_rechecks_cancellation_and_role_before_commit(tmp_path, kind, change):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="测试", profile={"profile": "测试角色"}))
    sessions = SessionStore(tmp_path / "sessions.db")
    store = MultichannelStore(tmp_path / "sessions.db", sessions)
    store.register_channel_account(role.id, "qq", account_id="a", enabled=True, allow_group=True)
    group = store.ensure_channel_thread(role.id, "qq", "a", "100", "group")
    repository = MultichannelGroupRepository(store)
    entered = Event()

    def authorized(context):
        entered.set()
        return roles.get(role.id) is not None

    access = GroupToolAccess(store, role_id=role.id, authorized=authorized)
    tool = (UpdateGroupContextTool if kind == "update" else SetGroupListeningTool)(repository, role_id=role.id, access=access)
    context = _context(role.id, f"role:{role.id}")
    arguments = {"group_thread_id": group.thread_id, "group_note": "must not commit"} if kind == "update" else {
        "channel": "qq", "chat_id": "100", "enabled": True}
    with store._connect() as blocker, ThreadPoolExecutor(max_workers=1) as executor:
        blocker.execute("BEGIN IMMEDIATE")
        future = executor.submit(lambda: asyncio.run(tool.execute(arguments, context)))
        try:
            assert entered.wait(5), "工具未进入实际来源校验"
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
            if change == "cancel":
                context.signal.cancel()
            else:
                roles.delete(role.id)
        finally:
            blocker.commit()
        assert future.result(timeout=5).is_error
    assert repository.read_context(role.id, group.thread_id)["groupNote"] == ""
    assert not store.group_listening(role.id, "a", "100")
    assert store.listening.state(role.id, group.thread_id)["toggles"] == []


def test_concurrent_patches_never_replace_an_omitted_field_with_a_stale_value(tmp_path):
    store, thread_id = _store(tmp_path)
    repository = MultichannelGroupRepository(store)
    repository.update_context("role", thread_id, group_note="old note", summary="old summary")
    entered = [Event(), Event()]

    def tool_for(index):
        def authorized(context):
            entered[index].set()
            return True
        access = GroupToolAccess(store, role_id="role", authorized=authorized)
        return UpdateGroupContextTool(repository, role_id="role", access=access)

    tools = [tool_for(0), tool_for(1)]
    changes = [{"group_note": "new note"}, {"summary": "new summary"}]
    with store._connect() as blocker, ThreadPoolExecutor(max_workers=2) as executor:
        blocker.execute("BEGIN IMMEDIATE")
        futures = [executor.submit(lambda index=i: asyncio.run(tools[index].execute(
            {"group_thread_id": thread_id, **changes[index]}, _context()))) for i in range(2)]
        try:
            assert all(event.wait(5) for event in entered)
            assert all(not future.done() for future in futures)
        finally:
            blocker.commit()
        assert all(not future.result(timeout=5).is_error for future in futures)
    result = json.loads(asyncio.run(LookupGroupContextTool(repository, role_id="role").execute(
        {"group_thread_id": thread_id}, _context())).content)
    assert result["groupNote"] == "new note" and result["summary"] == "new summary"
