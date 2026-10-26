"""A provider request keeps its exact MCP executor, not just an equal schema."""
import asyncio
from dataclasses import replace

import pytest

from backend.app import mcp_runtime
from backend.app.mcp_runtime import McpHost
from test_mcp_owner_cleanup import Peer, bundle, context
from test_mcp_stdio import configure


class EchoPeer(Peer):
    def __init__(self, *args):
        super().__init__()
        self.calls = 0

    async def connect(self):
        return ({"name": "echo", "description": "echo", "inputSchema": {"type": "object", "properties": {}}},)

    async def call(self, name, arguments):
        self.calls += 1
        return {"content": [{"type": "text", "text": "actual reply"}]}


def publish(run, call_id):
    revision = run.capabilities.pending_revision(call_id)
    assert revision is not None
    run.capabilities.publish_revision(revision)
    return run.capabilities.current()


@pytest.mark.parametrize("pause_validation", [False, True])
def test_identical_reconnect_rejects_old_binding_and_publishes_new_executor_only_next_request(tmp_path, monkeypatch, pause_validation):
    store, _ = configure(tmp_path)
    host = McpHost(store)
    async def exercise():
        peers = []
        def factory(*args):
            peer = EchoPeer()
            peers.append(peer)
            return peer
        monkeypatch.setattr(mcp_runtime, "McpStdioClient", factory)
        entered, release = asyncio.Event(), asyncio.Event()
        async def valid(schema, arguments):
            entered.set()
            if pause_validation:
                await release.wait()
            return True
        monkeypatch.setattr(mcp_runtime, "validate_instance", valid)
        run = bundle(host)
        run.direct_exposure = True
        stale_call = None
        try:
            await run.add("notes", context(run, "first"))
            issued = publish(run, "first")
            old = issued.get("mcp_notes__echo")
            call = replace(context(run, "stale"), capability_snapshot_id=issued.snapshot.snapshot_id)
            if pause_validation:
                stale_call = asyncio.create_task(old.execute({}, call))
                await entered.wait()
            await run.close_server("notes")
            await run.add("notes", context(run, "reconnect"))
            assert run.capabilities.current().get("mcp_notes__echo") is old
            release.set()
            with pytest.raises(PermissionError, match="connection"):
                if stale_call is not None:
                    await stale_call
                else:
                    await old.execute({}, call)
            assert [peer.calls for peer in peers] == [0, 0]
            assert not peers[1].closed, "a stale call cannot dispose its replacement peer"
            refreshed = publish(run, "reconnect")
            assert issued.get("mcp_notes__echo") is old
            replacement = refreshed.get("mcp_notes__echo")
            assert replacement is not old
            assert refreshed.snapshot.policy_decisions["mcp_notes__echo"] == issued.snapshot.policy_decisions["mcp_notes__echo"]
            assert replacement.definition.version == old.definition.version
            result = await replacement.execute({}, replace(context(run, "new"), capability_snapshot_id=refreshed.snapshot.snapshot_id))
            assert result.content == "actual reply"
            assert [peer.calls for peer in peers] == [0, 1]
        finally:
            release.set()
            if stale_call is not None:
                await asyncio.gather(stale_call, return_exceptions=True)
            await run.close()
    asyncio.run(exercise())


def test_late_old_response_cannot_close_or_poison_replacement(tmp_path, monkeypatch):
    store, _ = configure(tmp_path)
    host = McpHost(store)
    async def exercise():
        peers = []
        entered, release = asyncio.Event(), asyncio.Event()
        class DelayedPeer(EchoPeer):
            async def call(self, name, arguments):
                self.calls += 1
                if self is peers[0]:
                    entered.set()
                    await release.wait()
                return {"content": [{"type": "text", "text": "actual reply"}]}
        def factory(*args):
            peer = DelayedPeer()
            peers.append(peer)
            return peer
        monkeypatch.setattr(mcp_runtime, "McpStdioClient", factory)
        async def valid(*args):
            return True
        monkeypatch.setattr(mcp_runtime, "validate_instance", valid)
        run = bundle(host)
        run.direct_exposure = True
        pending = None
        try:
            await run.add("notes", context(run, "first"))
            issued = publish(run, "first")
            pending = asyncio.create_task(issued.get("mcp_notes__echo").execute({}, context(run, "old")))
            await entered.wait()
            await run.close_server("notes")
            await run.add("notes", context(run, "reconnect"))
            refreshed = publish(run, "reconnect")
            release.set()
            with pytest.raises(PermissionError, match="connection"):
                await pending
            assert not peers[1].closed and "notes" not in run.errors
            assert not (await refreshed.get("mcp_notes__echo").execute({}, context(run, "new"))).is_error
            assert [peer.calls for peer in peers] == [1, 1], "cleanup never replays the old call"
        finally:
            release.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await run.close()
    asyncio.run(exercise())


def test_authority_is_rechecked_after_async_schema_validation(tmp_path, monkeypatch):
    store, _ = configure(tmp_path)
    host = McpHost(store)
    async def exercise():
        peer = EchoPeer()
        monkeypatch.setattr(mcp_runtime, "McpStdioClient", lambda *args: peer)
        revoked = [False]
        entered, release = asyncio.Event(), asyncio.Event()
        async def valid(*args):
            entered.set()
            await release.wait()
            return True
        monkeypatch.setattr(mcp_runtime, "validate_instance", valid)
        run = bundle(host, denial=lambda: "MCP source was revoked" if revoked[0] else None)
        pending = None
        try:
            await run.add("notes", context(run))
            pending = asyncio.create_task(run.remote_tools["notes"][0].execute({}, context(run, "remote")))
            await entered.wait()
            revoked[0] = True
            release.set()
            with pytest.raises(PermissionError, match="revoked"):
                await pending
            assert peer.calls == 0
        finally:
            release.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await run.close()
    asyncio.run(exercise())


def test_namespace_summary_uses_only_redacted_owner_label(tmp_path, monkeypatch):
    store, _ = configure(tmp_path, name="笔记 vault-sensitive-token")
    host = McpHost(store)
    async def exercise():
        monkeypatch.setattr(mcp_runtime, "McpStdioClient", EchoPeer)
        run = bundle(host)
        try:
            await run.add("notes", context(run))
            definition = run.remote_tools["notes"][0].definition
            assert definition.namespace_summary == "笔记 [redacted]"
            assert definition.source == "mcp:notes"
        finally:
            await run.close()
    asyncio.run(exercise())
