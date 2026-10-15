"""Group writes obey the parent runtime's cancellation while SQLite is busy."""
import asyncio
import sqlite3
from threading import Event, Timer

import pytest

from backend.app.agent_runtime import (
    AssistantDoneEvent,
    AssistantMessage,
    CancellationToken,
    ToolCall,
    ToolCallEndEvent,
    UserMessage,
    run_agent_loop,
)
from backend.app.channels.group_access import GroupToolAccess
from backend.app.channels.group_tools import (
    MultichannelGroupRepository,
    SetGroupListeningTool,
    UpdateGroupContextTool,
)
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore


@pytest.mark.parametrize("kind", ["update", "listening"])
@pytest.mark.parametrize("cancel_delay,unlock_delay", [(0.1, 0.4), (0.01, 0.02)])
def test_parent_runtime_cancellation_cannot_commit_a_group_write_waiting_for_sqlite(tmp_path, kind, cancel_delay, unlock_delay):
    database = tmp_path / "sessions.db"
    store = MultichannelStore(database, SessionStore(database))
    store.register_channel_account("role", "qq", account_id="a", enabled=True, allow_group=True)
    group = store.ensure_channel_thread("role", "qq", "a", "100", "group")
    repository = MultichannelGroupRepository(store)
    parent = CancellationToken()
    blocker = sqlite3.connect(database, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")
    timers = []
    cancelled_before_unlock = []
    cancellation_requested = Event()

    def cancel():
        cancelled_before_unlock.append(blocker.in_transaction)
        parent.cancel()
        cancellation_requested.set()

    def unlock():
        cancellation_requested.wait(timeout=5)
        blocker.commit()

    def authorized(context):
        if not timers:
            for delay, operation in ((cancel_delay, cancel), (unlock_delay, unlock)):
                timer = Timer(delay, operation)
                timers.append(timer)
                timer.start()
        return True

    access = GroupToolAccess(store, role_id="role", authorized=authorized)
    tool = (UpdateGroupContextTool if kind == "update" else SetGroupListeningTool)(
        repository, role_id="role", access=access)
    arguments = {"group_thread_id": group.thread_id, "group_note": "MUST_NOT_COMMIT"} if kind == "update" else {
        "channel": "qq", "chat_id": "100", "enabled": True}
    call = ToolCall("write", tool.definition.name, arguments)

    class Provider:
        async def stream_response(self, **kwargs):
            yield ToolCallEndEvent(call)
            yield AssistantDoneEvent(AssistantMessage(tool_calls=(call,), stop_reason="tool_use"))

    async def run():
        return [event async for event in run_agent_loop(
            provider=Provider(), model="fixture", system="", messages=[UserMessage("write")],
            tools=[tool], signal=parent, role_id="role", session_key="role:role", run_id="run",
        )]

    try:
        events = asyncio.run(run())
    finally:
        for timer in timers:
            timer.join(timeout=5)
        blocker.close()
    assert cancelled_before_unlock == [True]
    assert parent.cancelled and events[-1].reason == "cancelled"
    assert repository.read_context("role", group.thread_id)["groupNote"] == ""
    assert not store.group_listening("role", "a", "100")
    assert store.listening.state("role", group.thread_id)["toggles"] == []


@pytest.mark.parametrize("kind", ["update", "listening"])
def test_repeated_runtime_cancellation_waits_for_the_group_worker_to_stop(tmp_path, kind):
    database = tmp_path / "sessions.db"
    store = MultichannelStore(database, SessionStore(database))
    store.register_channel_account("role", "qq", account_id="a", enabled=True, allow_group=True)
    group = store.ensure_channel_thread("role", "qq", "a", "100", "group")
    repository = MultichannelGroupRepository(store)
    parent = CancellationToken()
    blocker = sqlite3.connect(database, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")
    entered, unlocked = Event(), Event()

    def authorized(context):
        entered.set()
        return True

    access = GroupToolAccess(store, role_id="role", authorized=authorized)
    tool = (UpdateGroupContextTool if kind == "update" else SetGroupListeningTool)(
        repository, role_id="role", access=access)
    arguments = {"group_thread_id": group.thread_id, "group_note": "MUST_NOT_COMMIT"} if kind == "update" else {
        "channel": "qq", "chat_id": "100", "enabled": True}
    call = ToolCall("write", tool.definition.name, arguments)

    class Provider:
        async def stream_response(self, **kwargs):
            yield AssistantDoneEvent(AssistantMessage(tool_calls=(call,), stop_reason="tool_use"))

    async def run():
        return [event async for event in run_agent_loop(
            provider=Provider(), model="fixture", system="", messages=[UserMessage("write")],
            tools=[tool], signal=parent, role_id="role", session_key="role:role", run_id="run",
        )]

    def unlock():
        blocker.commit()
        unlocked.set()

    async def exercise():
        task = asyncio.create_task(run())
        assert await asyncio.to_thread(entered.wait, 5)
        parent.cancel()
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not unlocked.is_set() and not task.done()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert unlocked.is_set()

    release = Timer(0.5, unlock)
    release.start()
    try:
        asyncio.run(exercise())
    finally:
        release.join(timeout=5)
        blocker.close()
    assert repository.read_context("role", group.thread_id)["groupNote"] == ""
    assert not store.group_listening("role", "a", "100")
    assert store.listening.state("role", group.thread_id)["toggles"] == []
