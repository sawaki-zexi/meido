import asyncio
from types import SimpleNamespace

import pytest

from backend.app import main
from backend.app.agent_runtime import AssistantDoneEvent, AssistantMessage, RuntimeManager
from backend.app.agent_runtime.capabilities import CapabilityRegistry
from backend.app.channels import ChannelAdapter, ChannelHost
from backend.app.channels.group_tools import MultichannelGroupRepository
from backend.app.channels.models import DeliveryResult, DeliveryStatus, TargetKind, target_from_chat
from backend.app.model_config import ModelConfigurationStore
from backend.app.models import RoleInput, RoleProfile
from backend.app.multichannel import MultichannelStore
from backend.app.role_store import RoleStore
from backend.app.session_store import SessionStore
from backend.app.task_runtime import TaskRuntime


class MemoryCredentials:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        self.values[key] = value


@pytest.fixture
def owned_tasks(tmp_path, monkeypatch):
    tasks = TaskRuntime(tmp_path / 'tasks')
    monkeypatch.setattr(main, 'task_runtime', tasks)
    yield tasks
    tasks.close()


@pytest.mark.parametrize("delivery_status", [DeliveryStatus.SENT, DeliveryStatus.FAILED, DeliveryStatus.UNCERTAIN])
def test_real_channel_runtime_commits_reply_only_after_confirmed_delivery(tmp_path, monkeypatch, owned_tasks, delivery_status):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="role", profile=RoleProfile(profile="profile")))
    sessions = SessionStore(tmp_path / "sessions.db")
    channels = MultichannelStore(tmp_path / "sessions.db", sessions, MemoryCredentials())
    account = channels.register_channel_account(role.id, "qq", account_id="qq", enabled=True)
    thread = channels.ensure_channel_thread(role.id, "qq", account.account_id, "99", "private")
    code = channels.create_pairing_code(role.id)
    assert channels.consume_pairing_code(code, role_id=role.id, channel="qq", scope="platform", platform_user_id="99", account_id=account.account_id, chat_id="99")
    _, user = channels.append_event(thread, direction="inbound", content="hello", external_message_id="1", metadata={"senderId": "99", "senderIsOwner": True})
    target = target_from_chat(account_id=account.account_id, chat_id="99", kind=TargetKind.PRIVATE)
    channels.remember_targets(role.id, [target])

    class Adapter(ChannelAdapter):
        channel = "qq"
        account_id = "qq"
        def parse_event(self, payload):
            return None
        async def list_targets(self):
            return [target]
        async def send(self, message):
            return DeliveryResult(delivery_status, message.target, "receipt" if delivery_status is DeliveryStatus.SENT else None)

    class Provider:
        def __init__(self, adapter, role, configuration, *_args, **_kwargs):
            self.adapter, self.role, self.configuration = adapter, role, configuration

        async def stream_response(self, **kwargs):
            yield AssistantDoneEvent(AssistantMessage("actual runtime reply"))

    async def resolve(_role, *, session_key, run_id, **kwargs):
        return CapabilityRegistry(()).resolve(role_id=role.id, session_key=session_key, run_id=run_id)

    async def commit(*args):
        committed.append(args[-1])

    host = ChannelHost(channels)
    adapter = Adapter()
    host.register(adapter)
    committed = []
    worker_lock = asyncio.Lock()
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "multichannel_store", channels)
    monkeypatch.setattr(main, "runtime_manager", RuntimeManager(sessions))
    monkeypatch.setattr(main, "role_locks", {})
    monkeypatch.setattr(main, "model_configuration_store", ModelConfigurationStore(tmp_path / "models.json"))
    monkeypatch.setattr(main, "group_repository", MultichannelGroupRepository(channels))
    monkeypatch.setattr(main, "channel_host", host)
    monkeypatch.setattr(main, "channel_adapters", {account.account_id: adapter})
    monkeypatch.setattr(main, "MeidoProvider", Provider)
    monkeypatch.setattr(main, "_runtime_tool_candidates", lambda *args, **kwargs: ())
    monkeypatch.setattr(main, "_runtime_capabilities", resolve)
    monkeypatch.setattr(main, "_role_memory_context", lambda *_args: "")
    monkeypatch.setattr(main, "memory_worker", SimpleNamespace(role_deletion_blocked=lambda *_args: False, role_lock_for=lambda *_args: worker_lock))
    monkeypatch.setattr(main, "channel_memory_maintenance", SimpleNamespace(commit_turn=commit))

    asyncio.run(main._handle_channel_message(thread, "hello", {"messageId": user.id, "senderId": "99", "platformMessageId": "1"}, True))
    reply = sessions.list_messages(thread.session_key)[-1]
    assert reply.content == "actual runtime reply"
    assert reply.status == ("completed" if delivery_status is DeliveryStatus.SENT else "failed")
    assert reply.metadata["deliveryStatus"] == delivery_status.value
    assert sessions.get_run(reply.runId).status == "completed"
    history = sessions.runtime_messages(thread.session_key)
    owner_history = channels.owner_context(role.id, exclude_thread_id="desktop")
    if delivery_status is DeliveryStatus.SENT:
        assert history[-1].content == "actual runtime reply"
        assert owner_history[-1][2] == "actual runtime reply"
    else:
        assert len(history) == 1
        assert all(item[2] != "actual runtime reply" for item in owner_history)
    assert committed[-1].status == reply.status
