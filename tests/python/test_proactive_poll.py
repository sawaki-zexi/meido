"""Polling uses its own finite Runtime owner, without starting a model turn."""
import asyncio
import json

import pytest

from backend.app.mcp_autonomy import declaration_hash
from backend.app.mcp_runtime import McpHost
from backend.app.proactive_runtime import ProactiveApplication
from backend.app.proactive_runtime.config import AutonomyConfig
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskRuntime
from test_proactive_mcp import source
from test_proactive_snapshot import grant_for


pytestmark = pytest.mark.usefixtures("local_platform_only")


@pytest.fixture
def polling(source, tmp_path):
    root, store, *_ = source
    declaration = json.loads((root / "catalog.json").read_text())[1]
    grant = grant_for(source).model_copy(update={"pollTool": "poll",
        "pollDefinitionHash": declaration_hash(declaration)})
    data = tmp_path / "polling"
    initialize_databases(data)
    sessions, tasks, host = SessionStore(data / "sessions.db"), TaskRuntime(data / "tasks"), McpHost(store)
    now, models = [1780000000.0], []
    app = ProactiveApplication(sessions, tasks, clock=lambda: now[0],
        role_getter=lambda key: {"id": key}, model_getter=lambda key: {"id": key}, mcp_host=host,
        adapter_factory=lambda config: models.append(config))
    app.configure("role", AutonomyConfig(activeDecisionEnabled=True,
        decisionModelConfigurationId="explicit-model", consentRevision=1,
        sourceGrants=[grant]), expected_revision=0)
    yield app, now, root, models, sessions, tasks, host
    asyncio.run(app.close())
    asyncio.run(host.close())
    tasks.close()


async def settle(app, kind):
    async with asyncio.timeout(10):
        while True:
            await app.recover()
            rows = [row for row in app.query("role")["runs"] if row["kind"] == kind]
            if rows and rows[0]["runtimeAcknowledged"]:
                return rows[0]
            await asyncio.sleep(.01)


def test_enable_polls_immediately_then_waits_150_seconds_without_model_or_chat(polling):
    app, now, root, models, sessions, *_ = polling

    async def exercise():
        assert await app.on_clock() == 0
        first = await settle(app, "autonomy_poll")
        assert first["status"] == "polled"
        assert first["timeoutSeconds"] == 120
        assert first["origin"]["allowedModes"] == ["source_poll"]
        now[0] += 149
        await app.on_clock()
        assert len(app.query("role")["runs"]) == 1
        now[0] += 1
        await app.on_clock()
        await settle(app, "autonomy_poll")
        assert len(app.query("role")["runs"]) == 2
        assert [json.loads(line)["name"] for line in (root / "calls.jsonl").read_text().splitlines()] == ["poll", "poll"]
        assert models == []
        assert sessions.list_messages("role:role") == []
    asyncio.run(exercise())


def test_clock_jump_never_replays_old_poll_intervals(polling):
    app, now, root, *_ = polling

    async def exercise():
        # Avoid decision admission here; isolate the source host clock.
        with app._transaction() as db:
            db.execute("UPDATE autonomy_roles SET next_at=?", (now[0] + 100000,))
        now[0] += 7200
        await app.on_clock()
        await settle(app, "autonomy_poll")
        assert len((root / "calls.jsonl").read_text().splitlines()) == 1
        await app.on_clock()
        assert len(app.query("role")["runs"]) == 1
    asyncio.run(exercise())
