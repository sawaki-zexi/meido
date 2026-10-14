import asyncio
import contextvars

from backend.app import main
from backend.app.channels.host import ChannelHost
from backend.app.channels.intake import ChannelIntake
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore


def test_handover_buffers_until_resume_with_fresh_context_and_capacity():
    async def scenario():
        intake = ChannelIntake(capacity=2)
        calls = []
        inherited = contextvars.ContextVar("source_lease", default=False)
        async def replay():
            calls.append(("replay", inherited.get()))
        async def reject():
            calls.append(("reject", inherited.get()))
        assert not intake.defer("a", replay, reject)
        intake.pause("a")
        assert intake.defer("a", replay, reject)
        assert intake.defer("a", replay, reject)
        assert not intake.defer("a", replay, reject)
        assert calls == []
        inherited.set(True)
        intake.resume("a")
        await intake.drain("a")
        assert calls == [("replay", False), ("replay", False)]
    asyncio.run(scenario())


def test_paused_rpc_event_replays_once_through_replacement_adapter(tmp_path, monkeypatch):
    async def scenario():
        sessions = SessionStore(tmp_path / "sessions.db")
        store = MultichannelStore(tmp_path / "sessions.db", sessions)
        account = store.register_channel_account("role", "qq", account_id="qq", enabled=True)
        host = ChannelHost(store)
        original = NapCatAdapter(NapCatConfig(account_id=account.account_id))
        replacement = NapCatAdapter(NapCatConfig(account_id=account.account_id))
        host.register(original)
        adapters = {account.account_id: original}
        monkeypatch.setattr(main, "multichannel_store", store)
        monkeypatch.setattr(main, "channel_host", host)
        monkeypatch.setattr(main, "channel_adapters", adapters)
        observed = []
        async def receive(event):
            observed.append((event.content, host.adapter(event.account_id)))
        monkeypatch.setattr(main, "_handle_channel_event", receive)
        await host.pause_and_drain(account.account_id)
        payload = {"post_type": "message", "message_type": "private", "self_id": 123, "user_id": 99, "message_id": 1, "message": [{"type": "text", "data": {"text": "queued message"}}]}
        await main._dispatch_channel_payload(account.account_id, payload, source_adapter=original)
        assert observed == []
        assert store.get_account(account.account_id).platform_id is None
        adapters[account.account_id] = replacement
        host.register(replacement)
        await host.resume(account.account_id)
        await host.intake.drain(account.account_id)
        assert observed == [("queued message", replacement)]
        assert store.get_account(account.account_id).platform_id == "123"
    asyncio.run(scenario())


def test_disable_rejects_buffer_once_and_never_replays_it():
    async def scenario():
        intake = ChannelIntake()
        calls = []
        async def replay():
            calls.append("replayed")
        async def reject():
            calls.append("rejected")
        intake.pause("a")
        assert intake.defer("a", replay, reject)
        await intake.close("a")
        assert not intake.defer("a", replay, reject)
        await intake.close("a")
        intake.resume("a")
        await intake.drain("a")
        assert calls == ["rejected"]
    asyncio.run(scenario())
