from __future__ import annotations

import asyncio
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import main
from backend.app.agent_runtime import RuntimeManager
from backend.app.channels import ChannelHost
from backend.app.channels.consolidation import ChannelMemoryMaintenance
from backend.app.channels.credentials import credential_key
from backend.app.channels.group_tools import MultichannelGroupRepository
from backend.app.channels.models import DeliveryResult, DeliveryStatus, TargetKind, target_from_chat
from backend.app.channels.push import ChannelPushService
from backend.app.memory_service import MemoryService, MemoryWorker
from backend.app.memory_maintenance import MemoryMaintenance
from backend.app.memory_store import MemoryStore
from backend.app.models import RoleInput, RoleProfile
from backend.app.multichannel import MultichannelStore
from backend.app.owner_knowledge import OwnerKnowledgeStore
from backend.app.role_store import RoleStore
from backend.app.session_store import SessionStore


class Credentials:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        if value:
            self.values[key] = value
        else:
            self.values.pop(key, None)


def seed(tmp_path, role_id="r1"):
    sessions = SessionStore(tmp_path / "sessions.db")
    credentials = Credentials()
    store = MultichannelStore(tmp_path / "sessions.db", sessions, credentials)
    account = store.register_channel_account(role_id, "qq", account_id=f"qq-{role_id}", enabled=True, allow_group=True)
    credentials.set(credential_key("qq", account.account_id, "access_token"), "vault-secret")
    store.set_channel_config(account.account_id, {"endpoint": "ws://napcat.invalid:6700"})
    private = store.ensure_channel_thread(role_id, "qq", account.account_id, "owner", "private")
    group = store.ensure_channel_thread(role_id, "qq", account.account_id, "group", "group")
    store.append_event(private, direction="inbound", content="保留的私聊", external_message_id="private", metadata={"senderId": "owner"})
    store.append_event(group, direction="inbound", content="保留的群聊", external_message_id="group", metadata={"senderId": "member"})
    code = store.create_pairing_code(role_id, "qq")
    store.consume_pairing_code(code, role_id=role_id, channel="qq", scope="platform", platform_user_id="owner", account_id=account.account_id, chat_id="owner")
    target = target_from_chat(account_id=account.account_id, chat_id="owner", kind=TargetKind.PRIVATE)
    store.remember_targets(role_id, [target])
    push = ChannelPushService(store, ChannelHost(store))
    push.configure(role_id, account.account_id, target.id)
    groups = MultichannelGroupRepository(store)
    groups.update_context(role_id, group.thread_id, group_note="保留的群笔记")
    store.set_group_listening(role_id, account.account_id, "group", True)
    store.listening.set_cap(role_id, group.thread_id, 7)
    for index in range(8):
        store.listening.hear(group, f"保留的旁听 {index}", f"heard-{index}", {"senderId": "member"})
    maintenance = ChannelMemoryMaintenance(lambda: store, lambda: groups, None, None)
    maintenance._initialize()
    with store._connect() as db:
        db.execute("INSERT INTO channel_member_profiles VALUES(?,?,?,?,?,?,?)", (role_id, group.thread_id, "member", "成员", "保留的成员档案", "短档案", "2026-10-15"))
        db.execute("INSERT INTO channel_memory_cursors VALUES(?,1)", (group.thread_id,))
        db.execute("INSERT INTO channel_listening_cursors VALUES(?,2)", (group.thread_id,))
        db.execute("INSERT INTO channel_push_events VALUES(?,?,?,?,?,?,?,?)", (role_id, "reminder-1", account.account_id, target.id, "sent", "attempt-1", None, "2026-10-15"))
    return store, sessions, credentials, account, private, group


def test_channel_role_snapshot_restores_history_credentials_profiles_listening_push_and_avatar(tmp_path):
    async def scenario():
        store, sessions, credentials, account, private, group = seed(tmp_path)
        output = io.BytesIO()
        Image.new("RGB", (128, 128), "green").save(output, format="PNG")

        async def photo():
            return output.getvalue()

        await store.avatars.refresh(account, "sender", "owner", photo)
        avatar_id = store.avatars.key(account.account_id, "sender", "owner")
        original_photo = store.avatars.path(avatar_id).read_bytes()
        before = [message.model_dump() for message in sessions.list_messages(private.session_key)]
        snapshot = store.snapshot_role("r1")
        assert "vault-secret" not in repr(snapshot)
        store.delete_role("r1")
        ChannelPushService(store, ChannelHost(store)).clear_role("r1")
        assert store.get_account(account.account_id) is None
        store.restore_role("r1", snapshot)
        assert store.get_account(account.account_id).enabled
        assert credentials.get(credential_key("qq", account.account_id, "access_token")) == "vault-secret"
        assert [message.model_dump() for message in sessions.list_messages(private.session_key)] == before
        assert store.group_listening("r1", account.account_id, "group")
        assert store.listening.state("r1", group.thread_id)["dailyCap"] == 7
        assert len(store.listening.page("r1", group.thread_id)["messages"]) == 7
        assert store.listening._windows[group.thread_id][-1]["content"] == "保留的旁听 7"
        assert store.avatars.path(avatar_id).read_bytes() == original_photo
        with store._connect() as db:
            assert db.execute("SELECT profile FROM channel_member_profiles WHERE role_id='r1'").fetchone()[0] == "保留的成员档案"
            assert db.execute("SELECT target_id FROM channel_push_targets WHERE role_id='r1'").fetchone()[0]
            assert db.execute("SELECT status FROM channel_push_events WHERE role_id='r1'").fetchone()[0] == "sent"
            assert db.execute("SELECT sequence FROM channel_listening_cursors WHERE thread_id=?", (group.thread_id,)).fetchone()[0] == 2
            assert db.execute("SELECT sequence FROM channel_memory_cursors WHERE thread_id=?", (group.thread_id,)).fetchone()[0] == 1
    asyncio.run(scenario())


@pytest.fixture
def deletion_api(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="保留角色", profile=RoleProfile(profile="设定")))
    store, sessions, credentials, account, private, group = seed(tmp_path, role.id)
    memories = MemoryStore(tmp_path / "memory.db")
    worker = MemoryWorker(MemoryService(memories))
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "roles_root", tmp_path / "roles")
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "runtime_manager", RuntimeManager(sessions))
    monkeypatch.setattr(main, "role_locks", {})
    monkeypatch.setattr(main, "memory_store", memories)
    monkeypatch.setattr(main, "memory_worker", worker)
    monkeypatch.setattr(main, "owner_knowledge_store", OwnerKnowledgeStore(tmp_path / "owner.db"))
    monkeypatch.setattr(main, "multichannel_store", store)
    monkeypatch.setattr(main, "channel_host", ChannelHost(store))
    monkeypatch.setattr(main, "channel_adapters", {})
    monkeypatch.setattr(main, "channel_transports", {})
    monkeypatch.setattr(main, "group_repository", MultichannelGroupRepository(store))
    maintenance = ChannelMemoryMaintenance(lambda: store, lambda: main.group_repository, None, None)
    monkeypatch.setattr(main, "channel_memory_maintenance", maintenance)
    return role, roles, store, sessions, credentials, account, private, TestClient(main.app)


def test_late_role_file_purge_failure_restores_channel_history_secrets_and_enabled_account(deletion_api, monkeypatch):
    role, roles, store, sessions, credentials, account, private, client = deletion_api
    before = [message.model_dump() for message in sessions.list_messages(private.session_key)]
    stopped, restarted = [], []

    async def stop(account_id):
        stopped.append(account_id)

    async def start(account_id):
        assert store.get_account(account_id)
        assert credentials.get(credential_key("qq", account_id, "access_token"))
        restarted.append(account_id)

    def fail_purge(_path):
        raise OSError("cannot purge staged files")

    monkeypatch.setattr(main, "_stop_channel_account", stop)
    monkeypatch.setattr(main, "_start_channel_account", start)
    monkeypatch.setattr(roles, "purge_staged_role_files", fail_purge)
    response = client.delete(f"/api/roles/{role.id}")
    assert response.status_code == 500
    assert roles.get(role.id) is not None
    assert store.get_account(account.account_id) is not None
    assert store.get_account(account.account_id).enabled
    assert stopped == restarted == [account.account_id]
    assert [message.model_dump() for message in sessions.list_messages(private.session_key)] == before
    assert credentials.get(credential_key("qq", account.account_id, "access_token")) == "vault-secret"


def test_role_deletion_drains_external_note_save_before_snapshot_and_rollback(deletion_api, monkeypatch):
    import threading
    import httpx

    role, roles, channels, _, _, _, _, _ = deletion_api
    group = next(thread for thread in channels.list_threads(role.id) if thread.chat_type == "group")
    url = f"/api/roles/{role.id}/channels/threads/{group.thread_id}/external-memory"
    entered, release, snapshot_taken = threading.Event(), threading.Event(), threading.Event()
    deletion_entered = asyncio.Event()
    original = channels.external_memory.save_note
    before = channels.external_memory.context(role.id, group.thread_id)
    def delayed_save(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    async def deletion_preflight(*_args):
        deletion_entered.set()
    def fail_purge(_path):
        raise OSError("controlled deletion rollback")
    original_snapshot = channels.snapshot_role
    def snapshot(*args):
        result = original_snapshot(*args)
        snapshot_taken.set()
        return result
    monkeypatch.setattr(channels.external_memory, "save_note", delayed_save)
    monkeypatch.setattr(main.owner_knowledge_plugin, "begin_role_deletion", deletion_preflight)
    monkeypatch.setattr(channels, "snapshot_role", snapshot)
    monkeypatch.setattr(roles, "purge_staged_role_files", fail_purge)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            saving = asyncio.create_task(client.put(url + "/note", json={"note": "删除之前已提交的笔记", "expectedRevision": before["revision"]}))
            assert await asyncio.to_thread(entered.wait, 5)
            deleting = asyncio.create_task(client.delete(f"/api/roles/{role.id}"))
            try:
                await asyncio.wait_for(deletion_entered.wait(), 5)
                assert not snapshot_taken.is_set()
                late = await client.put(url + "/note", json={"note": "删除中晚到", "expectedRevision": before["revision"]})
                assert late.status_code == 409
            finally:
                release.set()
            saved, deleted = await asyncio.gather(saving, deleting)
            assert saved.status_code == 200
            assert deleted.status_code == 500
            restored = (await client.get(url)).json()
            assert restored["note"] == "删除之前已提交的笔记"
            assert restored["revision"] == saved.json()["revision"]
    asyncio.run(scenario())


def test_delete_role_rejects_channel_turn_before_starting_runtime_during_preflight(deletion_api, monkeypatch):
    role, _, store, sessions, _, account, private, _ = deletion_api
    before = sessions.list_messages(private.session_key)[0]
    reached, release = asyncio.Event(), asyncio.Event()

    async def disable_browser(_role_id, _enabled):
        reached.set()
        await release.wait()

    monkeypatch.setattr(main.browser_use_sessions, "set_role_enabled", disable_browser)

    async def scenario():
        deleting = asyncio.create_task(main.delete_role(role.id))
        await reached.wait()
        pending = asyncio.create_task(main._handle_channel_message(private, before.content, {"messageId": before.id}, False))
        await asyncio.wait_for(pending, timeout=0.5)
        release.set()
        await asyncio.wait_for(deleting, timeout=1)
        assert sessions.active_run(role.id) is None

    asyncio.run(scenario())


def test_channel_snapshot_restore_preserves_another_role_and_new_messages(tmp_path):
    store, sessions, _, first, private, _ = seed(tmp_path)
    second = store.register_channel_account("r2", "qq", account_id="qq-other")
    other = store.ensure_channel_thread("r2", "qq", second.account_id, "visitor", "private")
    original = store.append_event(other, direction="inbound", content="另一个角色的历史", external_message_id="other-before")[1]
    snapshot = store.snapshot_role("r1")
    store.delete_role("r1")
    new_message = store.append_event(other, direction="inbound", content="删除期间其他角色的新消息", external_message_id="other-during")[1]
    store.restore_role("r1", snapshot)
    assert store.get_account(first.account_id)
    assert store.get_account(second.account_id)
    assert [message.id for message in sessions.list_messages(other.session_key)] == [original.id, new_message.id]
    assert sessions.list_messages(private.session_key)[0].content == "保留的私聊"


def test_delete_role_drains_an_admitted_source_without_waiting_on_its_runtime_lock(deletion_api, monkeypatch):
    role, _, _, sessions, _, account, private, _ = deletion_api
    source = sessions.list_messages(private.session_key)[0]
    reached, release = asyncio.Event(), asyncio.Event()
    notices, stopped = [], []

    class Adapter:
        account_id = account.account_id
        channel = "qq"

        def remember_target(self, _target):
            pass

        async def send(self, outbound):
            notices.append(outbound.content)
            return DeliveryResult(DeliveryStatus.SENT, outbound.target, "notice")

        async def stop(self):
            stopped.append(self.account_id)

    adapter = Adapter()
    main.channel_host.register(adapter)
    main.channel_adapters[account.account_id] = adapter

    async def disable_browser(_role_id, _enabled):
        reached.set()
        await release.wait()

    monkeypatch.setattr(main.browser_use_sessions, "set_role_enabled", disable_browser)

    async def scenario():
        deleting = asyncio.create_task(main.delete_role(role.id))
        await reached.wait()

        async with main.channel_host.event_scope(account.account_id) as admitted:
            assert admitted
            await asyncio.wait_for(main._handle_channel_message(private, source.content, {"messageId": source.id, "senderId": "owner"}, False), timeout=0.5)
        release.set()
        await asyncio.wait_for(deleting, timeout=1)
        assert notices and "尚未处理" in notices[0] and "重新发送" in notices[0]
        assert stopped == [account.account_id]

    asyncio.run(scenario())


def test_partial_credential_cleanup_failure_restores_every_account_secret(deletion_api, monkeypatch):
    role, _, store, _, credentials, account, _, client = deletion_api
    feishu = store.register_channel_account(role.id, "feishu", account_id="feishu-restored", enabled=True, platform_id="app-restored")
    store.set_channel_config(feishu.account_id, {"appId": "app-restored"})
    original_secrets = {credential_key("qq", account.account_id, "access_token"): "vault-secret"}
    for name in ("app_secret", "verification_token", "encrypt_key"):
        key = credential_key("feishu", feishu.account_id, name)
        original_secrets[key] = f"synthetic-{name}"
        credentials.set(key, original_secrets[key])
    original_set = credentials.set

    def flaky_clear(key, value):
        if key == credential_key("feishu", feishu.account_id, "verification_token") and not value:
            raise OSError("vault cleanup failed")
        original_set(key, value)

    async def no_network(_account_id):
        pass

    credentials.set = flaky_clear
    monkeypatch.setattr(main, "_stop_channel_account", no_network)
    monkeypatch.setattr(main, "_start_channel_account", no_network)
    response = client.delete(f"/api/roles/{role.id}")
    assert response.status_code == 500
    assert store.get_account(account.account_id) and store.get_account(feishu.account_id)
    assert credentials.values == original_secrets


def quiet_startup(monkeypatch, sessions, tmp_path):
    async def no_work(*_args):
        return None

    monkeypatch.setattr(main.todo_plugin, "start", no_work)
    monkeypatch.setattr(main.memory_worker, "start", lambda: None)
    monkeypatch.setattr(main.memory_optimizer_loop, "start", lambda: None)
    monkeypatch.setattr(main.owner_knowledge_plugin, "startup", no_work)
    monkeypatch.setattr(main.owner_knowledge_scheduler, "start", lambda: None)
    # These short-lived loops exercise deletion recovery, not an autonomous
    # clock owner that must otherwise be closed on its original event loop.
    monkeypatch.setattr(main.ProactiveApplication, "start", lambda _self: None)
    monkeypatch.setattr(main, "memory_maintenance", MemoryMaintenance(tmp_path / "roles", sessions))


def interrupted_delete(role, roles):
    marker = roles.begin_role_deletion(role.id)
    roles.stage_role_files_for_deletion(role.id)
    roles.delete(role.id)
    return marker


def test_startup_deletion_recovery_cleans_channels_secrets_owner_knowledge_and_pushes(deletion_api, monkeypatch, tmp_path):
    role, roles, store, sessions, credentials, account, private, _ = deletion_api
    main.owner_knowledge_store.save_understanding(role.id, "应随删除清理的角色资料理解", [], "test")
    marker = interrupted_delete(role, roles)
    restarted = RoleStore(tmp_path / "roles")
    monkeypatch.setattr(main, "store", restarted)
    quiet_startup(monkeypatch, sessions, tmp_path)
    asyncio.run(main._start_memory_workers())
    assert not marker.exists()
    assert store.get_account(account.account_id) is None
    assert sessions.list_messages(private.session_key) == []
    assert credentials.values == {}
    assert main.owner_knowledge_store.get_understanding(role.id) is None
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM channel_push_targets WHERE role_id=?", (role.id,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM channel_push_events WHERE role_id=?", (role.id,)).fetchone()[0] == 0


def test_startup_cleanup_failure_retains_intent_marker_and_blocks_orphan_account_start(deletion_api, monkeypatch, tmp_path):
    role, roles, store, sessions, credentials, account, _, _ = deletion_api
    marker = interrupted_delete(role, roles)
    monkeypatch.setattr(main, "store", RoleStore(tmp_path / "roles"))
    quiet_startup(monkeypatch, sessions, tmp_path)
    original_set = credentials.set

    def unavailable(key, value):
        if not value:
            raise OSError("vault unavailable")
        original_set(key, value)

    credentials.set = unavailable
    asyncio.run(main._start_memory_workers())
    assert marker.exists()
    assert store.get_account(account.account_id) is not None
    assert not store.get_account(account.account_id).enabled

    def must_not_build(*_args, **_kwargs):
        raise AssertionError("an orphan account must never start a transport")

    monkeypatch.setattr(main, "_build_channel_adapter", must_not_build)
    asyncio.run(main._start_channel_account(account.account_id))
    credentials.set = original_set
    asyncio.run(main._start_memory_workers())
    assert not marker.exists()
    assert store.get_account(account.account_id) is None


@pytest.mark.parametrize("entrypoint", ["_start_channel_account", "_start_channel_account_unlocked", "_start_telegram_account"])
def test_every_channel_start_entrypoint_requires_a_live_owner_role(deletion_api, monkeypatch, entrypoint):
    role, roles, store, _, credentials, _, _, _ = deletion_api
    telegram = store.save_account(role.id, account_id="telegram-orphan", token="synthetic-token", enabled=True)
    roles.delete(role.id)

    def must_not_connect(*_args, **_kwargs):
        raise AssertionError("an orphan account must never connect")

    monkeypatch.setattr(main, "_build_channel_adapter", must_not_connect)
    monkeypatch.setattr(main, "_telegram_service", must_not_connect)
    asyncio.run(getattr(main, entrypoint)(telegram.account_id))
    assert not store.get_account(telegram.account_id).enabled
    assert credentials.get(credential_key("telegram", telegram.account_id, "bot_token")) == "synthetic-token"
