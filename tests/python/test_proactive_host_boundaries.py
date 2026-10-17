"""Current host identity and publication grants survive queued work."""
import asyncio
import sqlite3
import threading

import pytest

from backend.app import main
from backend.app.mcp_runtime import McpHost
from backend.app.models import RoleInput, RoleProfile
from backend.app.proactive_runtime import ProactiveApplication
from backend.app.proactive_runtime.config import AutonomyConfig
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskRuntime
from test_background_shell_api import api as shell_api  # noqa: F401
from test_proactive_delivery import ReplyModel
from test_proactive_mcp import source  # noqa: F401
from test_proactive_snapshot import grant_for
from test_spawn_api import api  # noqa: F401


pytestmark = pytest.mark.usefixtures("local_platform_only")


@pytest.mark.parametrize("unbind", [True, False], ids=["unpaired-while-queued", "paired-control"])
def test_channel_owner_priority_uses_pairing_after_acquiring_role_lock(api, monkeypatch, unbind):
    _, roles, sessions, _ = api
    role = roles.create(RoleInput(name="local-owner-race", profile=RoleProfile(profile="local")))
    channels = main.multichannel_store
    account = channels.register_channel_account(role.id, "qq", account_id="local-only", enabled=True)
    thread = channels.ensure_channel_thread(role.id, "qq", account.account_id, "owner", "private")
    code = channels.create_pairing_code(role.id)
    assert channels.consume_pairing_code(code, role_id=role.id, channel="qq", scope="platform",
        platform_user_id="owner", account_id=account.account_id, chat_id="owner")
    _, message = channels.append_event(thread, direction="inbound", content="queued local text",
        external_message_id="local-owner-before-unbind", metadata={"senderId": "owner"})
    monkeypatch.setattr(main, "_proactive", None)
    # The reservation and its current-identity callback execute; the ordinary
    # model turn is deliberately not started by this publication-boundary test.
    monkeypatch.setattr(main.runtime_manager, "attach_current_task", lambda _: False)
    application = main._proactive_application()
    application.configure(role.id, AutonomyConfig(), expected_revision=0)

    async def exercise():
        main.runtime_manager.role_locks = main.role_locks
        lock = main.runtime_manager.lock_for(role.id)
        await lock.acquire()
        pending = asyncio.create_task(main._handle_channel_message(thread, message.content,
            {"messageId": message.id, "senderId": "owner"}, True))
        try:
            await asyncio.sleep(.01)
            assert not pending.done()
            if unbind:
                channels.unbind_identity(str(channels.list_identities()[0]["identityId"]))
            assert channels.identity_is_owner(role_id=role.id, channel="qq", scope="platform",
                platform_user_id="owner", account_id=account.account_id) is (not unbind)
            lock.release()
            await pending
            with sqlite3.connect(sessions.database_path) as db:
                accepted = db.execute("SELECT run_id FROM autonomy_human_runs WHERE role_id=?", (role.id,)).fetchall()
            assert len(accepted) == (0 if unbind else 1)
        finally:
            if lock.locked():
                lock.release()
            await asyncio.gather(pending, return_exceptions=True)
            await application.close()

    asyncio.run(exercise())


def test_role_admission_closing_before_message_insert_prevents_late_autonomy(source, tmp_path, monkeypatch):
    _, store, *_ = source
    data = tmp_path / "publication"
    initialize_databases(data)
    sessions, tasks = SessionStore(data / "sessions.db"), TaskRuntime(data / "tasks")
    now, host, model = [1780000000.0], McpHost(store), ReplyModel()
    live, insert_reached = threading.Event(), threading.Event()
    live.set()
    app = ProactiveApplication(sessions, tasks, clock=lambda: now[0],
        role_getter=lambda role_id: {"id": role_id} if live.is_set() else None,
        model_getter=lambda key: {"id": key}, mcp_host=host, adapter_factory=lambda _: model)
    app.configure("role", AutonomyConfig(activeDecisionEnabled=True,
        decisionModelConfigurationId="explicit-model", consentRevision=1,
        sourceGrants=[grant_for(source)]), expected_revision=0)
    original_connect = sqlite3.connect

    def trace(statement):
        if "SELECT COALESCE(MAX(sequence),0)+1 FROM messages" in statement:
            live.clear()
            insert_reached.set()

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        if str(args[0]) == str(sessions.database_path):
            connection.set_trace_callback(trace)
        return connection

    async def exercise():
        try:
            with monkeypatch.context() as patch:
                patch.setattr(sqlite3, "connect", traced_connect)
                now[0] += 1800
                await app.on_clock()
                async with asyncio.timeout(8):
                    while not insert_reached.is_set():
                        await asyncio.sleep(.01)
                await app.close()
            assert not live.is_set()
            assert sessions.list_messages("role:role") == []
        finally:
            await app.close()
            await host.close()

    try:
        asyncio.run(exercise())
    finally:
        tasks.close()
