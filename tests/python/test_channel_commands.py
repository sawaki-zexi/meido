from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from backend.app import main
from backend.app.agent_runtime.runtime import RuntimeManager
from backend.app.channels import ChannelHost
from backend.app.channels.base import ChannelAdapter
from backend.app.channels.models import ChannelCapabilities, ChatType, DeliveryResult, DeliveryStatus, InboundEvent, TargetKind
from backend.app.models import AgentRun
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore


class Credentials:
    def get(self, key):
        return ""

    def set(self, key, value):
        pass


class Adapter(ChannelAdapter):
    channel = "qq"
    account_id = "qq-1"
    capabilities = ChannelCapabilities(private=True, group=True, targets=frozenset({TargetKind.PRIVATE, TargetKind.GROUP}))

    def __init__(self):
        super().__init__()
        self._targets = {}
        self.sent = []

    def parse_event(self, payload):
        return None

    async def send(self, message):
        self.sent.append(message)
        return DeliveryResult(DeliveryStatus.SENT, message.target, "response")

    async def list_targets(self):
        return list(self._targets.values())


def setup(tmp_path, monkeypatch):
    sessions = SessionStore(tmp_path / "sessions.db")
    channels = MultichannelStore(tmp_path / "sessions.db", sessions, Credentials())
    channels.register_channel_account("role-1", "qq", account_id="qq-1", enabled=True, allow_group=True)
    host = ChannelHost(channels)
    adapter = Adapter()
    host.register(adapter)
    runtime = RuntimeManager(sessions)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "multichannel_store", channels)
    monkeypatch.setattr(main, "channel_host", host)
    monkeypatch.setattr(main, "channel_adapters", {"qq-1": adapter})
    monkeypatch.setattr(main, "runtime_manager", runtime)
    monkeypatch.setattr(main, "channel_event_locks", {})
    return sessions, channels, runtime, adapter


def event(command, *, chat_id="group-a"):
    return InboundEvent(
        external_event_id="command", external_message_id="command-id", account_id="qq-1", channel="qq",
        chat_id=chat_id, chat_type=ChatType.GROUP, sender_id="sender", sender_name="Alice", content=command,
    )


def test_stop_interrupts_current_source_thread_before_account_event_lock(tmp_path, monkeypatch):
    sessions, channels, runtime, adapter = setup(tmp_path, monkeypatch)

    async def scenario():
        thread = channels.ensure_channel_thread("role-1", "qq", "qq-1", "group-a", "group")
        started = asyncio.Event()

        async def active_turn():
            run = AgentRun(runId="active", roleId="role-1", sessionKey=thread.session_key, status="created", startedAt=datetime.now(timezone.utc))
            await runtime.reserve_run(run, "hello")
            runtime.attach_current_task(run.runId)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                runtime.cancel_unstarted_run(run.runId)

        active = asyncio.create_task(active_turn())
        await started.wait()
        lock = main.channel_event_locks.setdefault("qq-1", asyncio.Lock())
        await lock.acquire()
        try:
            await asyncio.wait_for(main._handle_channel_event(event("/stop")), timeout=1)
            outcome = await asyncio.gather(active, return_exceptions=True)
            assert isinstance(outcome[0], asyncio.CancelledError)
        finally:
            lock.release()

        assert sessions.get_run("active").status == "cancelled"
        assert adapter.sent[0].reply_to_message_id == "command-id"
        assert all(message.content != "/stop" for message in sessions.list_messages(thread.session_key))

    asyncio.run(scenario())


def test_stop_cannot_interrupt_another_group_or_desktop_run(tmp_path, monkeypatch):
    _, channels, runtime, adapter = setup(tmp_path, monkeypatch)

    async def scenario():
        thread = channels.ensure_channel_thread("role-1", "qq", "qq-1", "group-a", "group")
        run = AgentRun(runId="active", roleId="role-1", sessionKey=thread.session_key, status="created", startedAt=datetime.now(timezone.utc))
        await runtime.reserve_run(run, "hello")
        try:
            await main._handle_channel_event(event("/stop", chat_id="group-b"))
            assert not runtime._tokens["active"].is_cancelled()
            assert "没有" in adapter.sent[0].content
        finally:
            runtime.cancel_unstarted_run("active")

    asyncio.run(scenario())


def test_identifier_commands_do_not_enter_channel_history(tmp_path, monkeypatch):
    sessions, channels, _, adapter = setup(tmp_path, monkeypatch)
    asyncio.run(main._handle_channel_event(event("/chatid")))
    thread = channels.get_thread_by_chat("role-1", "qq-1", "group-a")
    assert not sessions.list_messages(thread.session_key)
    assert "group-a" in adapter.sent[0].content


def test_paused_intake_buffers_then_replies_on_original_adapter_when_closed(tmp_path, monkeypatch):
    from backend.app.channels.host import CHANNEL_INTAKE_RETRY_NOTICE

    sessions, channels, runtime, adapter = setup(tmp_path, monkeypatch)
    adapter.parse_event = lambda payload: event("hello")

    async def scenario():
        ready, release = asyncio.Event(), asyncio.Event()

        async def original_turn():
            async with main.channel_host.event_scope("qq-1") as admitted:
                assert admitted
                ready.set()
                await release.wait()

        original = asyncio.create_task(original_turn())
        await ready.wait()
        drain = asyncio.create_task(main.channel_host.pause_and_drain("qq-1"))
        await asyncio.sleep(0)
        await main._dispatch_channel_payload("qq-1", {}, source_adapter=adapter)
        assert not drain.done()
        assert adapter.sent == []
        await main.channel_host.intake.close("qq-1")
        release.set()
        await asyncio.gather(original, drain)

    asyncio.run(scenario())
    assert adapter.sent[0].content == CHANNEL_INTAKE_RETRY_NOTICE
    assert adapter.sent[0].reply_to_message_id == "command-id"
    assert not channels.list_threads("role-1")


def test_napcat_start_waits_connection_and_discovers_actual_platform_identity(tmp_path, monkeypatch):
    _, channels, _, adapter = setup(tmp_path, monkeypatch)
    calls = []

    class Transport:
        async def wait_connected(self):
            calls.append("connected")

        async def __call__(self, action, params):
            assert action == "get_login_info"
            calls.append("identity")
            return {"user_id": 1234, "nickname": "QQ Bot"}

    async def start(account_id):
        calls.append("started")

    monkeypatch.setattr(main, "_build_channel_adapter", lambda account: adapter)
    monkeypatch.setattr(main.channel_host, "start", start)
    monkeypatch.setattr(main, "channel_transports", {"qq-1": Transport()})
    monkeypatch.setattr(main, "store", SimpleNamespace(get=lambda role_id: SimpleNamespace(id=role_id) if role_id == "role-1" else None, pending_role_deletions=lambda: []))
    monkeypatch.setattr(main, "_refresh_channel_avatars", lambda *_args: None)

    asyncio.run(main._start_channel_account_unlocked("qq-1"))

    assert calls == ["started", "connected", "identity"]
    assert channels.get_account("qq-1").platform_id == "1234"
