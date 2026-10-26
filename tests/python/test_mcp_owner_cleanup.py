"""Fault/barrier probes of ordinary ownership; no user MCP or network peers."""
import asyncio
from datetime import datetime, timezone

import pytest

from backend.app import mcp_runtime
from backend.app.agent_runtime import (AssistantDoneEvent, AssistantMessage, CapabilityRegistry,
    CancellationToken, RuntimeManager, ToolContext)
from backend.app.mcp_runtime import McpHost
from backend.app.mcp_stdio import McpProtocolError
from backend.app.models import AgentRun
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from test_mcp_stdio import configure


class Peer:
    failure = None

    def __init__(self, *args, fail=0, **kwargs):
        self.closed = self.exit_confirmed = False
        self.attempts = 0
        self.fail = fail
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def connect(self):
        self.started.set()
        await self.release.wait()
        return ()

    async def close(self):
        self.closed = True
        self.attempts += 1
        if self.attempts <= self.fail:
            raise OSError("secret argv vault-sensitive-token")
        self.exit_confirmed = True


def bundle(host, *, run_id="run", role="r", session="role:r", denial=lambda: None):
    run = host.freeze(role_id=role, session_key=session, run_id=run_id, source=None,
        source_denial=denial, tool_enabled=lambda _: True, owner=True)
    capabilities = CapabilityRegistry(run.tools).resolve(role_id=role, session_key=session,
        run_id=run_id, enabled_tools={"tool_search", "mcp_add", "mcp_remove", "mcp_list"},
        discovery_enabled=True, service_grants=run.grants, resource_close=run.close)
    run.bind(capabilities)
    return run


def context(run, call="add"):
    return ToolContext(run.role_id, run.session_key, run.run_id, CancellationToken(), call)


def test_failed_cleanup_retains_exact_owner_retries_all_peers_and_is_idempotent(tmp_path):
    store, _ = configure(tmp_path)
    host = McpHost(store)

    async def exercise():
        run = bundle(host)
        bad, good = Peer(fail=1), Peer()
        run.clients.update(notes=bad, other=good)
        original_loop = run.loop
        try:
            with pytest.raises(McpProtocolError, match="cleanup") as error:
                await run.close()
            assert "secret" not in str(error.value)
            assert run.closed and run.cleanup_failed and not run.exit_confirmed
            assert host._runs[run.run_id] is run and run.loop is original_loop
            assert bad.attempts == good.attempts == 1
            assert good.exit_confirmed and not bad.exit_confirmed
            state = host.public()[0]
            assert state["cleanupOwners"][0]["runId"] == run.run_id
            assert state["cleanupOwners"][0]["state"] == "cleanup_failed"
            assert "secret" not in str(state)
            await asyncio.gather(run.close(), run.close())
            assert run.exit_confirmed and not run.cleanup_failed
            assert run.run_id not in host._runs
            assert bad.attempts == 2 and good.attempts == 1
            await run.close()
            assert bad.attempts == 2
        finally:
            await run.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("source", [True, False])
def test_watcher_read_exception_fails_closed_owns_cleanup_and_never_revives(tmp_path, monkeypatch, source):
    store, _ = configure(tmp_path)
    host = McpHost(store)
    broken = [False]
    def denial():
        if source and broken[0]:
            raise OSError("secret authority failure")
        return None
    original_get = store.get
    def get(key):
        if not source and broken[0]:
            raise OSError("secret config failure")
        return original_get(key)
    monkeypatch.setattr(store, "get", get)

    async def exercise():
        run = bundle(host, denial=denial)
        peer = Peer(fail=1)
        run.clients["notes"] = peer
        broken[0] = True
        try:
            await asyncio.wait_for(run._watchdog, 1)
            assert run.closed
            await asyncio.gather(run._closing, return_exceptions=True)
            assert run.cleanup_failed and host._runs[run.run_id] is run
            broken[0] = False
            with pytest.raises(PermissionError):
                run.require_context(context(run))
            await run.close()
            assert run.exit_confirmed
        finally:
            broken[0] = False
            await run.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("running_loop", [False, True])
def test_unavailable_original_loop_reports_unconfirmed_and_retains_owner(tmp_path, running_loop):
    store, _ = configure(tmp_path)
    host = McpHost(store)
    loop = asyncio.new_event_loop()
    async def setup():
        run = bundle(host)
        run.clients["notes"] = Peer()
        run._watchdog.cancel()
        await asyncio.gather(run._watchdog, return_exceptions=True)
        return run
    run = loop.run_until_complete(setup())
    if not running_loop:
        loop.close()
    async def exercise():
        with pytest.raises(McpProtocolError, match="original loop"):
            await asyncio.wait_for(run.close_server_from_any_loop("notes"), .2)
        assert run.clients["notes"].attempts == 0
        assert host._runs[run.run_id] is run
        state = host.public()[0]["cleanupOwners"][0]
        assert state["state"] == "cleanup_unconfirmed"
    asyncio.run(exercise())
    if running_loop:
        loop.run_until_complete(run.close())
        loop.close()


def test_cleanup_quarantine_blocks_only_same_role_source_server(tmp_path, monkeypatch):
    store, _ = configure(tmp_path, roleIds=["r", "r2"])
    host = McpHost(store)
    monkeypatch.setattr(mcp_runtime, "McpStdioClient", Peer)
    async def exercise():
        old = bundle(host)
        old.clients["notes"] = Peer(fail=1)
        with pytest.raises(McpProtocolError):
            await old.close()
        same = bundle(host, run_id="same")
        different = bundle(host, run_id="different", session="role:other")
        role = bundle(host, run_id="role", role="r2")
        try:
            with pytest.raises(McpProtocolError, match="cleanup"):
                await same.add("notes", context(same))
            assert "notes" not in same.clients
            await different.add("notes", context(different))
            await role.add("notes", context(role))
            await old.close()
            await same.add("notes", context(same))
            assert same.clients["notes"].started.is_set()
        finally:
            await host.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("action", ["close", "remove", "revoke"])
def test_opening_is_owned_until_stop_remove_or_revoke_finishes(tmp_path, monkeypatch, action):
    store, config = configure(tmp_path)
    host = McpHost(store)
    async def exercise():
        peer = Peer()
        peer.release.clear()
        monkeypatch.setattr(mcp_runtime, "McpStdioClient", lambda *args: peer)
        run = bundle(host)
        add = asyncio.create_task(run.add("notes", context(run)))
        await peer.started.wait()
        cleanup = asyncio.create_task(run.close() if action == "close" else
            run.remove("notes", context(run, "remove")) if action == "remove" else host.revoke("notes"))
        try:
            await asyncio.wait_for(cleanup, 1)
            peer.release.set()
            result = (await asyncio.gather(add, return_exceptions=True))[0]
            assert isinstance(result, BaseException)
            assert peer.exit_confirmed
            assert not run.remote_tools
            assert not store.has_intent(run.scope_id, config)
        finally:
            peer.release.set()
            await asyncio.gather(add, cleanup, return_exceptions=True)
            await run.close()
    asyncio.run(exercise())


def test_concurrent_add_has_single_owned_handshake_and_complete_directory(tmp_path, monkeypatch):
    store, _ = configure(tmp_path)
    host = McpHost(store)
    async def exercise():
        peers = []
        def factory(*args):
            peer = Peer()
            peer.release.clear()
            peers.append(peer)
            return peer
        monkeypatch.setattr(mcp_runtime, "McpStdioClient", factory)
        run = bundle(host)
        first = asyncio.create_task(run.add("notes", context(run)))
        while not peers:
            await asyncio.sleep(0)
        await peers[0].started.wait()
        second = asyncio.create_task(run.add("notes", context(run, "second")))
        await asyncio.sleep(0)
        try:
            assert not second.done(), "an opening client is not a connected catalog"
            peers[0].release.set()
            await asyncio.gather(first, second)
            assert len(peers) == 1 and not peers[0].closed
        finally:
            for peer in peers:
                peer.release.set()
            await asyncio.gather(first, second, return_exceptions=True)
            await run.close()
    asyncio.run(exercise())


def test_business_completed_stays_completed_when_cleanup_fails_then_retries(tmp_path):
    store, config = configure(tmp_path)
    host = McpHost(store)
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data/sessions.db")
    sessions.open_role_session("r")
    async def exercise():
        run = bundle(host)
        peer = Peer(fail=1)
        run.clients["notes"] = peer
        store.set_intent(run.scope_id, run.role_id, run.session_key, config)
        record = AgentRun(runId=run.run_id, roleId=run.role_id, sessionKey=run.session_key,
            status="created", startedAt=datetime.now(timezone.utc))
        sessions.create_run_with_messages(record, "完成本地假业务")
        class Provider:
            async def stream_response(self, **kwargs):
                yield AssistantDoneEvent(AssistantMessage(content="业务已完成"))
        _ = [event async for event in RuntimeManager(sessions).run(record, provider=Provider(),
            model="", system="", messages=[], capabilities=run.capabilities)]
        assert sessions.get_run(run.run_id).status == "completed"
        assert host.public()[0]["cleanupOwners"]
        await host.retry_cleanup("notes")
        assert run.exit_confirmed and not host.public()[0]["cleanupOwners"]
        assert store.has_intent(run.scope_id, config), "cleanup retry is not remove/reconnect"
        assert sessions.get_run(run.run_id).status == "completed"
    asyncio.run(exercise())


@pytest.mark.parametrize("stop", ["close", "cancel"])
def test_stdio_close_joins_an_unpublished_spawn_and_confirms_real_exit(tmp_path, monkeypatch, stop):
    import os
    from backend.app import mcp_stdio
    from test_mcp_http import process_alive
    store, config = configure(tmp_path)

    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()
        spawned = []
        original = mcp_stdio.spawn_process_tree if os.name == "nt" else asyncio.create_subprocess_exec
        async def delayed_spawn(*args, **kwargs):
            owned = await original(*args, **kwargs)
            spawned.append(owned)
            started.set()
            await release.wait()
            return owned
        if os.name == "nt":
            monkeypatch.setattr(mcp_stdio, "spawn_process_tree", delayed_spawn)
        else:
            monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
        client = mcp_stdio.McpStdioClient(config, store.environment(config))
        opening = asyncio.create_task(client.connect())
        closing = None
        try:
            await asyncio.wait_for(started.wait(), 2)
            if stop == "cancel":
                opening.cancel()
            closing = asyncio.create_task(client.close())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not client.exit_confirmed, "spawn has not returned its owned child yet"
            assert not closing.done()
            release.set()
            await asyncio.wait_for(closing, 5)
            result = (await asyncio.gather(opening, return_exceptions=True))[0]
            assert isinstance(result, BaseException)
            process = spawned[0][0] if os.name == "nt" else spawned[0]
            assert client.exit_confirmed and not process_alive(process.pid)
        finally:
            release.set()
            await asyncio.gather(opening, *([closing] if closing else []), return_exceptions=True)
            # Retain independent fixture ownership even for the red regression.
            for owned in spawned:
                process = owned[0] if os.name == "nt" else owned
                if os.name == "nt":
                    await asyncio.to_thread(owned[1].close)
                elif process.returncode is None:
                    process.kill()
                await process.wait()
            await client.close()
    asyncio.run(exercise())


def test_owner_http_cleanup_retry_preserves_intent_grant_and_retained_identity(tmp_path):
    import httpx
    from fastapi import FastAPI
    from backend.app.mcp_api import mcp_router
    store, config = configure(tmp_path)
    host = McpHost(store)
    app = FastAPI()
    app.include_router(mcp_router(lambda: host, lambda: None))
    async def exercise():
        run = bundle(host)
        peer = Peer(fail=2)
        run.clients["notes"] = peer
        store.set_intent(run.scope_id, run.role_id, run.session_key, config)
        with pytest.raises(McpProtocolError):
            await run.close()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            response = await client.get("/api/mcp/servers")
            owner = response.json()["servers"][0]["cleanupOwners"][0]
            assert owner["runId"] == run.run_id and owner["scopeId"] == run.scope_id
            assert owner["configRevision"] == config.revision and owner["grantRevision"] == config.grant_id
            failed = await client.post("/api/mcp/servers/notes/cleanup")
            assert failed.status_code == 503
            assert "secret" not in failed.text
            assert host._runs[run.run_id] is run
            response = await client.post("/api/mcp/servers/notes/cleanup")
            assert response.status_code == 200
            assert run.exit_confirmed and not host.public()[0]["cleanupOwners"]
            assert store.get("notes") == config and store.has_intent(run.scope_id, config)
            assert peer.attempts == 3
            assert (await client.post("/api/mcp/servers/notes/cleanup")).status_code == 200
            assert peer.attempts == 3
    asyncio.run(exercise())


def test_unconfirmed_peer_is_not_success_and_repeated_stop_cannot_cancel_disposal(tmp_path):
    store, _ = configure(tmp_path)
    host = McpHost(store)
    async def exercise():
        run = bundle(host)
        started, release = asyncio.Event(), asyncio.Event()
        class UnconfirmedPeer(Peer):
            async def close(self):
                self.closed = True
                self.attempts += 1
                if self.attempts == 1:
                    return
                started.set()
                await release.wait()
                self.exit_confirmed = True
        peer = UnconfirmedPeer()
        run.clients["notes"] = peer
        with pytest.raises(McpProtocolError, match="cleanup"):
            await run.close()
        assert run.cleanup_failed and not run.exit_confirmed
        retry = asyncio.create_task(run.close())
        await started.wait()
        retry.cancel()
        await asyncio.sleep(0)
        retry.cancel()
        assert host._runs[run.run_id] is run and not run.exit_confirmed
        release.set()
        result = (await asyncio.gather(retry, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError)
        assert run.exit_confirmed and run.run_id not in host._runs
        assert peer.attempts == 2
    asyncio.run(exercise())


def test_dispatch_config_read_failure_is_safe_sticky_and_starts_owned_cleanup(tmp_path, monkeypatch):
    store, _ = configure(tmp_path)
    host = McpHost(store)
    async def exercise():
        run = bundle(host)
        peer = Peer()
        run.clients["notes"] = peer
        def broken(key):
            raise OSError("vault-sensitive-token")
        original = store.get
        monkeypatch.setattr(store, "get", broken)
        with pytest.raises(PermissionError, match="revoked") as error:
            await run.add("notes", context(run))
        assert "sensitive" not in str(error.value)
        monkeypatch.setattr(store, "get", original)
        await run.close()
        assert peer.exit_confirmed
        with pytest.raises(PermissionError):
            await run.add("notes", context(run))
    asyncio.run(exercise())


def test_stopped_loop_cleanup_seals_late_opening_before_loop_resumes(tmp_path, monkeypatch):
    store, config = configure(tmp_path)
    host = McpHost(store)
    loop = asyncio.new_event_loop()
    async def setup():
        peer = Peer()
        peer.release.clear()
        monkeypatch.setattr(mcp_runtime, "McpStdioClient", lambda *args: peer)
        run = bundle(host)
        pending = asyncio.create_task(run.add("notes", context(run)))
        await peer.started.wait()
        return run, peer, pending
    run, peer, pending = loop.run_until_complete(setup())
    async def unavailable():
        with pytest.raises(McpProtocolError, match="original loop"):
            await run.close_server_from_any_loop("notes")
    asyncio.run(unavailable())
    async def resume():
        peer.release.set()
        result = (await asyncio.gather(pending, return_exceptions=True))[0]
        try:
            assert isinstance(result, BaseException)
            assert not store.has_intent(run.scope_id, config)
            assert not run.remote_tools
        finally:
            await run.close()
    try:
        loop.run_until_complete(resume())
    finally:
        loop.close()


def test_failed_cleanup_of_cancelled_opening_is_not_hidden_by_implicit_retry(tmp_path, monkeypatch):
    store, _ = configure(tmp_path)
    host = McpHost(store)
    async def exercise():
        run = bundle(host)
        peer = Peer(fail=1)
        peer.release.clear()
        monkeypatch.setattr(mcp_runtime, "McpStdioClient", lambda *args: peer)
        opening = asyncio.create_task(run.add("notes", context(run)))
        await peer.started.wait()
        try:
            with pytest.raises(McpProtocolError, match="cleanup"):
                await run.close()
            await asyncio.gather(opening, return_exceptions=True)
            assert peer.attempts == 1 and not peer.exit_confirmed
            assert host._runs[run.run_id] is run
            await run.close()
            assert peer.attempts == 2 and run.exit_confirmed
        finally:
            peer.release.set()
            await asyncio.gather(opening, return_exceptions=True)
            await run.close()
    asyncio.run(exercise())
