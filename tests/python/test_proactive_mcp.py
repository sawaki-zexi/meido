"""Autonomous MCP uses explicit frozen operations, never ordinary owner=True."""
import asyncio
import json
from pathlib import Path
import sys
from dataclasses import replace

import pytest

from backend.app.mcp_autonomy import AutonomyMcpGrant, declaration_hash
from backend.app.mcp_config import McpConfigStore, McpServerInput
from backend.app.mcp_runtime import McpHost
from backend.app.proactive_runtime.identity import AutonomyOrigin
from test_mcp_stdio import Credentials
from test_mcp_http import process_alive
from backend.app import mcp_stdio
from backend.app.browser_use.process import WindowsJob


pytestmark = pytest.mark.usefixtures("local_platform_only")
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/proactive_source.py"


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    declarations = [{"name": name, "description": name,
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}
        for name in ("get_events", "poll")]
    (root / "catalog.json").write_text(json.dumps(declarations), encoding="utf-8")
    (root / "events.json").write_text(json.dumps([{"id": "event", "kind": "content", "title": "Fixture"}]), encoding="utf-8")
    store = McpConfigStore(tmp_path / "mcp.db", Credentials())
    data = McpServerInput(serverId="source", name="Synthetic", command=[sys.executable, "-I", str(FIXTURE), str(root)],
        cwd=str(root), roleIds=["role"], risk="external", timeoutSeconds=3)
    config = store.save(data)
    grant = AutonomyMcpGrant(config.server_id, config.revision, config.grant_id,
                            (("get_events", declaration_hash(declarations[0])),))
    origin = AutonomyOrigin("role", "tick", 1, 1, ("active_decision",))
    return root, store, data, grant, origin


def test_real_stdio_only_calls_frozen_operation_and_closes_process(source):
    root, store, _, grant, origin = source
    host = McpHost(store)

    async def exercise():
        run = host.freeze_autonomy(origin, (grant,), authorized=lambda: True)
        try:
            with pytest.raises(PermissionError):
                await run.call("source", "poll", {})
            response = await run.call("source", "get_events", {})
            assert "Fixture" in response["content"][0]["text"]
            with pytest.raises(ValueError):
                await run.call("source", "get_events", {"owner_context": True})
        finally:
            await run.close()
            await host.close()
        assert not process_alive(int((root / "pid").read_text()))
        calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
        assert len(calls) == 1
    asyncio.run(exercise())


def test_changed_declaration_is_denied_before_any_business_call(source):
    root, store, _, grant, origin = source
    catalog = json.loads((root / "catalog.json").read_text())
    catalog[0]["description"] = "different definition"
    (root / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    host = McpHost(store)

    async def exercise():
        run = host.freeze_autonomy(origin, (grant,), authorized=lambda: True)
        try:
            with pytest.raises(PermissionError, match="definition"):
                await run.call("source", "get_events", {})
        finally:
            await host.close()
        assert not (root / "calls.jsonl").exists()
        assert not process_alive(int((root / "pid").read_text()))
    asyncio.run(exercise())


def test_revoked_host_origin_does_not_revive_when_boolean_restored(source):
    root, store, _, grant, origin = source
    host, allowed = McpHost(store), [True]

    async def exercise():
        run = host.freeze_autonomy(origin, (grant,), authorized=lambda: allowed[0])
        try:
            await run.call("source", "get_events", {})
            allowed[0] = False
            with pytest.raises(PermissionError):
                await run.call("source", "get_events", {})
            allowed[0] = True
            with pytest.raises(PermissionError):
                await run.call("source", "get_events", {})
        finally:
            await host.close()
        assert len((root / "calls.jsonl").read_text().splitlines()) == 1
    asyncio.run(exercise())


def test_idle_revocation_closes_and_joins_without_another_tool_call(source):
    root, store, _, grant, origin = source
    host, allowed = McpHost(store), [True]

    async def exercise():
        run = host.freeze_autonomy(origin, (grant,), authorized=lambda: allowed[0])
        try:
            await run.call("source", "get_events", {})
            allowed[0] = False
            async with asyncio.timeout(3):
                while not run.exit_confirmed:
                    await asyncio.sleep(.01)
            assert not process_alive(int((root / "pid").read_text()))
        finally:
            await host.close()
    asyncio.run(exercise())


def test_close_joins_owned_startup_without_waiting_on_callers_finally(source,monkeypatch):
    root,store,_,grant,origin=source
    host=McpHost(store)
    async def exercise():
        entered,release=asyncio.Event(),asyncio.Event()
        original=mcp_stdio.McpStdioClient.connect
        async def delayed(client):
            entered.set()
            await release.wait()
            return await original(client)
        monkeypatch.setattr(mcp_stdio.McpStdioClient,"connect",delayed)
        run=host.freeze_autonomy(origin,(grant,),authorized=lambda:True)
        async def caller():
            try:
                await run.call("source","get_events",{})
            finally:
                await run.close()
        task=asyncio.create_task(caller())
        try:
            await entered.wait()
            await asyncio.wait_for(run.close(),3)
            release.set()
            with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task,3)
            assert run.exit_confirmed and not host.autonomy_cleanup_pending("role")
            assert not (root/"pid").exists()
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            await host.close()
    asyncio.run(exercise())


def test_authority_read_failure_revokes_and_joins_real_idle_peer(source):
    root,store,_,grant,origin=source
    host,broken=McpHost(store),[False]
    def authorized():
        if broken[0]: raise OSError("authority read unavailable")
        return True
    async def exercise():
        run=host.freeze_autonomy(origin,(grant,),authorized=authorized)
        try:
            await run.call("source","get_events",{})
            broken[0]=True
            async with asyncio.timeout(3):
                while not run.exit_confirmed: await asyncio.sleep(.01)
            assert not process_alive(int((root/"pid").read_text()))
            broken[0]=False
            with pytest.raises(PermissionError): await run.call("source","get_events",{})
        finally:
            await host.close()
    asyncio.run(exercise())


@pytest.mark.skipif(sys.platform!="win32",reason="owned Windows Job fault")
def test_failed_base_transport_close_preserves_quarantine_without_claiming_retry(source,monkeypatch):
    root,store,_,grant,origin=source
    host=McpHost(store)
    async def exercise():
        run=host.freeze_autonomy(origin,(grant,),authorized=lambda:True)
        spawned=[]
        original_spawn=mcp_stdio.spawn_process_tree
        original_close=WindowsJob.close
        async def record(*args,**kwargs):
            owned=await original_spawn(*args,**kwargs)
            spawned.append(owned)
            return owned
        def unavailable(owner): raise OSError("injected Job cleanup failure")
        try:
            with monkeypatch.context() as patch:
                patch.setattr(mcp_stdio,"spawn_process_tree",record)
                await run.call("source","get_events",{})
                patch.setattr(WindowsJob,"close",unavailable)
                with pytest.raises(OSError): await run.close()
                with pytest.raises(OSError): await run.close()
            assert not run.exit_confirmed and host.autonomy_cleanup_pending("role")
            with pytest.raises(RuntimeError,match="cleanup"):
                host.freeze_autonomy(replace(origin,tick_id="next-tick"),(grant,),authorized=lambda:True)
        finally:
            # 30002a9 transport cannot retry its failed close. This test keeps
            # ownership of the exact spawned handles and reaps them itself.
            for process,owner in spawned:
                await asyncio.to_thread(original_close,owner)
                await process.wait()
    asyncio.run(exercise())
    with pytest.raises(RuntimeError,match="original loop"):
        asyncio.run(host.close_autonomy_role("role"))
    assert host.autonomy_cleanup_pending("role")


def test_role_autonomy_cleanup_preserves_ordinary_bundle_and_intent(source):
    root,store,_,grant,origin=source
    host=McpHost(store)
    config=store.get("source")
    store.set_intent("ordinary-scope","role","role:role",config)
    async def exercise():
        ordinary=host.freeze(role_id="role",session_key="role:role",run_id="ordinary-run",source=None,
            source_denial=lambda:None,tool_enabled=lambda _:True,owner=True)
        run=host.freeze_autonomy(origin,(grant,),authorized=lambda:True)
        try:
            await run.call("source","get_events",{})
            await host.close_autonomy_role("role")
            assert run.exit_confirmed and not ordinary.closed
            assert store.has_intent("ordinary-scope",config)
            assert not process_alive(int((root/"pid").read_text()))
        finally:
            await ordinary.close()
            await host.close()
    asyncio.run(exercise())
