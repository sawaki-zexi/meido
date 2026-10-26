"""Real production mcp_add callback with an owned fake-transport barrier."""
import asyncio
import json

import pytest

from backend.app import mcp_runtime
from backend.app.mcp_runtime import McpHost
from test_mcp_owner_cleanup import Peer, bundle, context
from test_mcp_stdio import configure


def test_mcp_add_emits_safe_actual_connect_phase_before_transport_completes(tmp_path, monkeypatch):
    store, _ = configure(tmp_path)
    host = McpHost(store)
    async def exercise():
        run = bundle(host)
        peer = Peer()
        peer.release.clear()
        monkeypatch.setattr(mcp_runtime, "McpStdioClient", lambda *args: peer)
        updates = []
        add = next(tool for tool in run.tools if tool.definition.name == "mcp_add")
        pending = asyncio.create_task(add.execute({"server_id": "notes"}, context(run), updates.append))
        try:
            await asyncio.wait_for(peer.started.wait(), 1)
            assert not pending.done()
            assert len(updates) == 1
            assert updates[0].content == "正在连接 MCP 服务并准备工具目录。"
            assert updates[0].details == {"phase": "connecting"}
            assert not updates[0].images and not updates[0].is_error
            safe = json.dumps({"content": updates[0].content, "details": updates[0].details})
            assert "vault-sensitive-token" not in safe and "notes" not in safe and str(tmp_path) not in safe
            peer.release.set()
            assert not (await pending).is_error
            await add.execute({"server_id": "notes"}, context(run, "already-connected"), updates.append)
            assert len(updates) == 1, "an already connected catalog has no new connection phase"
        finally:
            peer.release.set()
            await asyncio.gather(pending, return_exceptions=True)
            await run.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("denial", ["unknown_server", "revoked", "cancelled"])
def test_mcp_add_never_reports_connecting_before_admission(tmp_path, monkeypatch, denial):
    store, _ = configure(tmp_path)
    host = McpHost(store)
    async def exercise():
        run = bundle(host, denial=lambda: "MCP source was revoked" if denial == "revoked" else None)
        created = []
        monkeypatch.setattr(mcp_runtime, "McpStdioClient", lambda *args: created.append(Peer()))
        updates = []
        add = next(tool for tool in run.tools if tool.definition.name == "mcp_add")
        call = context(run)
        if denial == "cancelled":
            call.signal.cancel()
        try:
            with pytest.raises((PermissionError, asyncio.CancelledError, RuntimeError)):
                await add.execute({"server_id": "unknown" if denial == "unknown_server" else "notes"}, call, updates.append)
            assert updates == [] and created == []
        finally:
            await run.close()
    asyncio.run(exercise())


from test_chat_stop import chat_server


def test_production_mcp_connect_phase_reaches_http_sse_before_handshake_finishes(chat_server, tmp_path, monkeypatch):
    import threading
    from backend.app import main
    from backend.app.agent_runtime import CapabilityRegistry
    from backend.app.ai.model_types import (ModelAssistantMessage, ModelResponseMetadata,
        ModelStreamDone, ModelTextBlock, ModelToolCallBlock)

    client, role, _ = chat_server
    store, config = configure(tmp_path, roleIds=[role.id])
    host = McpHost(store)
    monkeypatch.setattr(main, "mcp_host", host)
    release, settled = threading.Event(), threading.Event()
    peers = []

    class ConnectingPeer(Peer):
        async def connect(self):
            self.started.set()
            try:
                assert await asyncio.to_thread(release.wait, 5), "the SSE consumer must release this handshake"
                return ()
            finally:
                settled.set()

    def factory(*args):
        peer = ConnectingPeer()
        peers.append(peer)
        return peer
    monkeypatch.setattr(mcp_runtime, "McpStdioClient", factory)

    class Adapter:
        requests = 0
        async def stream_rich_messages(self, *args, **kwargs):
            self.requests += 1
            yield ModelStreamDone(ModelAssistantMessage(
                (ModelToolCallBlock("connect", "mcp_add", {"server_id": "notes"}),),
                ModelResponseMetadata(stop_reason="tool_use")) if self.requests == 1 else ModelAssistantMessage(
                (ModelTextBlock("已连接"),), ModelResponseMetadata(stop_reason="stop")))

    async def capabilities(owner, *, session_key, run_id, **kwargs):
        run = host.freeze(role_id=owner.id, session_key=session_key, run_id=run_id, source=None,
            source_denial=lambda: None, tool_enabled=lambda _: True, owner=True)
        result = CapabilityRegistry(run.tools).resolve(role_id=owner.id, session_key=session_key, run_id=run_id,
            enabled_tools={"mcp_add"}, activated_tools={"mcp_add"}, discovery_enabled=True,
            service_grants=run.grants, resource_close=run.close)
        run.bind(result)
        return result
    monkeypatch.setattr(main, "model_adapter", Adapter())
    monkeypatch.setattr(main, "_runtime_capabilities", capabilities)
    events = []
    try:
        with client.stream("POST", f"/api/roles/{role.id}/messages", json={"content": "连接已授权服务"}) as response:
            assert response.status_code == 200
            kind = None
            for line in response.iter_lines():
                if line.startswith("event: "):
                    kind = line[7:]
                if not line.startswith("data: "):
                    continue
                payload = json.loads(line[6:])
                events.append((kind, payload))
                if kind == "tool_execution_update":
                    assert peers and peers[0].started.is_set() and not settled.is_set()
                    assert payload["result"] == "正在连接 MCP 服务并准备工具目录。"
                    assert payload["status"] == "running" and payload["droppedUpdates"] == 0
                    assert payload["roleId"] == role.id and payload["sessionKey"] == f"role:{role.id}"
                    assert payload["toolCallId"] == "connect" and payload["toolName"] == "mcp_add"
                    release.set()
        assert release.is_set() and settled.is_set()
        tool_events = [(kind, value) for kind, value in events if kind.startswith("tool_execution")]
        assert [kind for kind, _ in tool_events] == ["tool_execution_start", "tool_execution_update", "tool_execution_end"]
        assert "arguments" not in tool_events[0][1]
        assert tool_events[-1][1]["status"] == "succeeded"
        assert "vault-sensitive-token" not in json.dumps(events)
        assert len({value["runId"] for _, value in tool_events}) == 1
        assert [value["sequence"] for _, value in events] == sorted({value["sequence"] for _, value in events})
        saved = main.session_store.list_messages(f"role:{role.id}")
        results = [item for item in saved if item.messageType == "tool_result"]
        assert len(results) == 1 and json.loads(results[0].content)["status"] == "connected"
        assert all("正在连接 MCP 服务并准备工具目录。" not in item.content for item in saved)
        assert peers[0].exit_confirmed and not host._runs
    finally:
        release.set()
