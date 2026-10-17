"""A transient cleanup failure cannot strand ownership on a dead event loop."""
import asyncio

import pytest

from backend.app.mcp_runtime import McpHost
from backend.app.mcp_stdio import McpStdioClient
from backend.app.proactive_runtime import ProactiveApplication
from backend.app.proactive_runtime.config import AutonomyConfig
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskRuntime
from test_proactive_mcp import source
from test_proactive_snapshot import grant_for
from test_mcp_http import process_alive


pytestmark = pytest.mark.usefixtures("local_platform_only")


def test_cleanup_receipt_loss_retries_on_original_live_loop_and_releases_runtime(source, tmp_path, monkeypatch):
    root, store, *_ = source
    (root / "events.json").write_text("[]")
    data = tmp_path / "owned-loop"
    initialize_databases(data)
    sessions, tasks, host = SessionStore(data / "sessions.db"), TaskRuntime(data / "tasks"), McpHost(store)
    now, calls = [1780000000.0], []
    app = ProactiveApplication(sessions, tasks, clock=lambda: now[0],
        role_getter=lambda key: {"id": key}, model_getter=lambda key: {"id": key}, mcp_host=host)
    app.configure("role", AutonomyConfig(activeDecisionEnabled=True,
        decisionModelConfigurationId="explicit", consentRevision=1, sourceGrants=[grant_for(source)]), expected_revision=0)
    original = McpStdioClient.close

    async def close_then_lose_receipt(client):
        calls.append(asyncio.get_running_loop())
        await original(client)
        if len(calls) == 1:
            raise OSError("owned peer exited but local cleanup confirmation was interrupted")
    monkeypatch.setattr(McpStdioClient, "close", close_then_lose_receipt)

    async def exercise():
        try:
            now[0] += 1800
            await app.on_clock()
            async with asyncio.timeout(5):
                while not app.query("role")["runs"][0]["runtimeAcknowledged"]:
                    await app.recover()
                    await asyncio.sleep(.01)
            assert len(calls) >= 2 and calls[0] is calls[1]
            assert not host.autonomy_cleanup_pending("role")
            assert not process_alive(int((root / "pid").read_text()))
        finally:
            await app.close()
            try:
                await host.close()
            except RuntimeError:
                # RED cleanup: the injected failure happened after the real
                # process and Job were already joined, so no peer is forgotten.
                assert not process_alive(int((root / "pid").read_text()))
    try:
        asyncio.run(exercise())
    finally:
        tasks.close()
