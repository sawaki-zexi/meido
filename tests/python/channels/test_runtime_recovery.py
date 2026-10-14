import asyncio

from backend.app import main
from backend.app.model_config import ModelConfigurationStore
from backend.app.models import RoleInput, RoleProfile
from backend.app.multichannel import MultichannelStore
from backend.app.role_store import RoleStore
from backend.app.session_store import SessionStore


def test_channel_provider_setup_failure_releases_role_and_publishes_failed_reply(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="role", profile=RoleProfile(profile="profile")))
    sessions = SessionStore(tmp_path / "sessions.db")
    channels = MultichannelStore(tmp_path / "sessions.db", sessions)
    channels.register_channel_account(role.id, "qq", account_id="qq", enabled=True)
    thread = channels.ensure_channel_thread(role.id, "qq", "qq", "visitor", "private")
    _, message = channels.append_event(thread, direction="inbound", content="hello", external_message_id="hello", metadata={"senderId": "visitor"})
    channels.append_event(thread, direction="inbound", content="future sender request", external_message_id="later", metadata={"senderId": "another-visitor"})
    replayed = []
    original_replay = sessions.runtime_messages
    def capture_replay(*args, **kwargs):
        result = original_replay(*args, **kwargs)
        replayed.extend(result)
        return result
    monkeypatch.setattr(sessions, "runtime_messages", capture_replay)
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "multichannel_store", channels)
    monkeypatch.setattr(main, "model_configuration_store", ModelConfigurationStore(tmp_path / "model.json"))
    monkeypatch.setattr(main, "runtime_manager", main.RuntimeManager(sessions))
    monkeypatch.setattr(main, "role_locks", {})
    monkeypatch.setattr(main, "_runtime_tool_candidates", lambda *_, **__: ())

    class Capabilities:
        closed = False

        class Snapshot:
            def to_dict(self, **kwargs):
                return {"tools": []}

        snapshot = Snapshot()

        def prompt_context(self):
            return ""

        async def close(self):
            self.closed = True

    capabilities = Capabilities()

    async def resolve(*_, **__):
        return capabilities

    def fail_provider(*_, **__):
        raise RuntimeError("provider setup failed")

    monkeypatch.setattr(main, "_runtime_capabilities", resolve)
    monkeypatch.setattr(main, "MeidoProvider", fail_provider)

    async def scenario():
        queue = asyncio.Queue()
        channels._subscribers[role.id] = {queue}
        await main._handle_channel_message(thread, "hello", {"messageId": message.id, "senderId": "visitor"}, False)
        assert sessions.active_run(role.id) is None
        assert not main.runtime_manager.lock_for(role.id).locked()
        assert sessions.list_messages(thread.session_key)[-1].status == "failed"
        assert queue.get_nowait()["message"]["status"] == "failed"
        assert capabilities.closed
        assert replayed[-1].content.endswith("hello")
        assert all("future sender request" not in item.content for item in replayed)

    asyncio.run(scenario())
