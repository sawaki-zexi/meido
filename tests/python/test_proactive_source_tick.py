"""One real Runtime tick owns source snapshot, no-event choice and cleanup."""
import asyncio
import json

import pytest

from backend.app.mcp_runtime import McpHost
from backend.app.proactive_runtime import ProactiveApplication
from backend.app.proactive_runtime.config import AutonomyConfig
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskRuntime
from test_proactive_mcp import source
from test_proactive_snapshot import grant_for


pytestmark = pytest.mark.usefixtures("local_platform_only")


@pytest.mark.parametrize("payload,status", [([], "no_events"), ({"bad": "shape"}, "sources_unavailable")])
def test_tick_collects_once_and_failure_never_becomes_empty(source, tmp_path, payload, status):
    root, store, *_ = source
    (root / "events.json").write_text(json.dumps(payload), encoding="utf-8")
    data = tmp_path / "application"
    initialize_databases(data)
    sessions, tasks = SessionStore(data / "sessions.db"), TaskRuntime(data / "tasks")
    now, host = [1780000000.0], McpHost(store)
    app = ProactiveApplication(sessions, tasks, clock=lambda: now[0], role_getter=lambda key: {"id": key},
        model_getter=lambda key: {"id": key}, mcp_host=host)
    config = AutonomyConfig(activeDecisionEnabled=True, decisionModelConfigurationId="explicit-model",
        consentRevision=1, sourceGrants=[grant_for(source)])
    app.configure("role", config, expected_revision=0)
    assert not (root / "calls.jsonl").exists()

    async def exercise():
        try:
            now[0] += 1800
            assert await app.on_clock() == 1
            async with asyncio.timeout(10):
                while True:
                    await app.recover()
                    run = app.query("role")["runs"][0]
                    if run["runtimeAcknowledged"]:
                        break
                    await asyncio.sleep(.01)
            assert run["status"] == status
            assert run["sources"][0]["status"] == ("empty" if status == "no_events" else "failed")
            assert len((root / "calls.jsonl").read_text().splitlines()) == 1
            assert sessions.list_messages("role:role") == []
        finally:
            await app.close()
            await host.close()
    try:
        asyncio.run(exercise())
    finally:
        tasks.close()
