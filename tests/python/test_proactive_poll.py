"""Polling uses its own finite Runtime owner, without starting a model turn."""
import asyncio
import json
import sqlite3
import sys
import threading

import pytest

from backend.app.mcp_autonomy import declaration_hash
from backend.app.mcp_runtime import McpHost
from backend.app.proactive_runtime import ProactiveApplication
from backend.app.proactive_runtime.config import AutonomyConfig
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskRuntime, TaskOrigin, TaskBudget, TaskOutcome
from test_proactive_mcp import source, FIXTURE
from test_proactive_snapshot import grant_for
from test_proactive_delivery import ReplyModel


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


async def settle_all(app):
    async with asyncio.timeout(10):
        while True:
            await app.recover()
            if not app.query("role")["cleanupPending"] and all(
                    row["runtimeAcknowledged"] for row in app.query("role")["runs"]):
                return
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


def test_delayed_first_clock_waits_for_initial_poll_terminal_before_decision(polling):
    app, now, root, models, *_ = polling
    (root / "events.json").write_text("[]", encoding="utf-8")

    async def exercise():
        now[0] += 1800
        # Submitting a poll is insufficient: the first decision needs the
        # real poll's terminal result and owned cleanup, then a clock turn.
        assert await app.on_clock() == 0
        assert {row["kind"] for row in app.query("role")["runs"]} == {"autonomy_poll"}
        first = await settle(app, "autonomy_poll")
        assert first["status"] == "polled"
        assert await app.on_clock() == 1
        assert (await settle(app, "autonomy_tick"))["status"] == "no_events"
        assert [json.loads(line)["name"] for line in (root / "calls.jsonl").read_text().splitlines()] == ["poll", "get_events"]
        assert models == []

    asyncio.run(exercise())


def test_same_source_save_preserves_poll_admission_interval(polling):
    app, now, root, *_ = polling

    async def exercise():
        await app.on_clock()
        await settle_all(app)
        current = AutonomyConfig.model_validate(app.query("role")["config"])
        saved = app.configure("role", current, expected_revision=current.revision)
        assert saved["config"]["sourceGrants"][0]["revision"] == current.sourceGrants[0].revision
        await settle_all(app)
        await app.on_clock()
        await settle_all(app)
        assert [json.loads(line)["name"] for line in (root / "calls.jsonl").read_text().splitlines()] == ["poll"]
        now[0] += 149
        await app.on_clock()
        await settle_all(app)
        assert len(app.query("role")["runs"]) == 1
        now[0] += 1
        await app.on_clock()
        await settle_all(app)
        assert [json.loads(line)["name"] for line in (root / "calls.jsonl").read_text().splitlines()] == ["poll", "poll"]

    asyncio.run(exercise())


def install_poll_profile(polling, source, tmp_path, *, wait=False, fail=False):
    """Use an actual owned stdio server with a controlled poll response."""
    app, _, root, *_ = polling
    _, store, data, *_ = source
    code = FIXTURE.read_text(encoding="utf-8")
    if wait:
        code = code.replace('payload = {"polled": True}',
            'import time\n                (root / "poll-entered").write_text("entered")\n'
            '                while not (root / "release-poll").exists(): time.sleep(.01)\n'
            '                payload = {"polled": True}')
    if fail:
        code = code.replace('failure = root / "fail-ack-count"',
            'if params["name"] == "poll" and (root / "poll-fails").exists(): result["isError"] = True\n'
            '            failure = root / "fail-ack-count"')
        (root / "poll-fails").write_text("fail")
    script = tmp_path / "controlled-poll-source.py"
    script.write_text(code, encoding="utf-8")
    saved = store.save(data.model_copy(update={"command": [sys.executable, "-I", str(script), str(root)]}))
    current = AutonomyConfig.model_validate(app.query("role")["config"])
    grant = current.sourceGrants[0].model_copy(update={"configRevision": saved.revision,
        "grantRevision": saved.grant_id})
    app.configure("role", current.model_copy(update={"sourceGrants": [grant]}), expected_revision=current.revision)


def test_initial_poll_submit_does_not_admit_decision_while_stdio_is_pending(polling, source, tmp_path):
    app, now, root, *_ = polling
    (root / "events.json").write_text("[]", encoding="utf-8")
    install_poll_profile(polling, source, tmp_path, wait=True)

    async def exercise():
        try:
            now[0] += 1800
            assert await app.on_clock() == 0
            async with asyncio.timeout(5):
                while not (root / "poll-entered").exists():
                    await asyncio.sleep(.01)
            for _ in range(3):
                assert await app.on_clock() == 0
                assert {row["kind"] for row in app.query("role")["runs"]} == {"autonomy_poll"}
            (root / "release-poll").write_text("release")
            await settle_all(app)
            assert await app.on_clock() == 1
            await settle_all(app)
            assert [json.loads(line)["name"] for line in (root / "calls.jsonl").read_text().splitlines()] == ["poll", "get_events"]
        finally:
            (root / "release-poll").write_text("release")

    asyncio.run(exercise())


def test_failed_initial_poll_finishes_prerequisite_without_requiring_success(polling, source, tmp_path):
    app, now, root, *_ = polling
    (root / "events.json").write_text("[]", encoding="utf-8")
    install_poll_profile(polling, source, tmp_path, fail=True)
    current = AutonomyConfig.model_validate(app.query("role")["config"])
    app.configure("role", current.model_copy(update={"driftEnabled": True,
        "driftModelConfigurationId": "explicit-model"}), expected_revision=current.revision)

    async def exercise():
        now[0] += 1800
        assert await app.on_clock() == 0
        first = await settle(app, "autonomy_poll")
        assert first["status"] == "aborted" and first["error"] == "failed"
        assert await app.on_clock() == 1
        result = await settle(app, "autonomy_tick")
        assert result["status"] == "sources_unavailable", "failed refresh cannot authorize empty-source Drift"
        assert result["sources"][0]["status"] == "failed"
        assert [json.loads(line)["name"] for line in (root / "calls.jsonl").read_text().splitlines()] == ["poll", "get_events"]
        (root / "poll-fails").unlink()
        now[0] += 150
        await app.on_clock()
        await settle_all(app)
        now[0] += 1650
        assert await app.on_clock() == 1
        await settle_all(app)
        ticks = [row for row in app.query("role")["runs"] if row["kind"] == "autonomy_tick"]
        assert ticks[0]["status"] == "drift_dependency_pending"
        assert ticks[0]["sources"][0]["status"] == "empty"

    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["revision", "disable_reenable", "stop", "reopen", "clock_start"])
def test_generation_and_restart_keep_original_poll_not_before(polling, change):
    app, now, root, _, sessions, tasks, host = polling
    reopened = None

    async def exercise():
        nonlocal reopened
        await app.on_clock()
        await settle_all(app)
        current = AutonomyConfig.model_validate(app.query("role")["config"])
        active = app
        if change == "revision":
            grant = current.sourceGrants[0].model_copy(update={"eventDomain": "new-domain"})
            saved = app.configure("role", current.model_copy(update={"sourceGrants": [grant]}), expected_revision=current.revision)
            assert saved["config"]["sourceGrants"][0]["revision"] != current.sourceGrants[0].revision
        elif change == "disable_reenable":
            off = current.model_copy(update={"activeDecisionEnabled": False})
            app.configure("role", off, expected_revision=current.revision)
            app.configure("role", current, expected_revision=current.revision + 1)
        elif change == "stop":
            app.stop("role")
        elif change == "clock_start":
            app.start()
        else:
            await app.close()
            reopened = ProactiveApplication(sessions, tasks, clock=lambda: now[0],
                role_getter=lambda key: {"id": key}, model_getter=lambda key: {"id": key}, mcp_host=host)
            active = reopened
        try:
            await settle_all(active)
            await active.on_clock()
            await settle_all(active)
            assert [json.loads(line)["name"] for line in (root / "calls.jsonl").read_text().splitlines()] == ["poll"]
            now[0] += 149
            await active.on_clock()
            await settle_all(active)
            assert len(active.query("role")["runs"]) == 1
            now[0] += 1
            await active.on_clock()
            await settle_all(active)
            assert [json.loads(line)["name"] for line in (root / "calls.jsonl").read_text().splitlines()] == ["poll", "poll"]
        finally:
            if reopened is not None:
                await reopened.close()

    asyncio.run(exercise())


def test_runtime_rejection_does_not_complete_initial_poll_prerequisite(polling):
    app, now, root, _, _, tasks, _ = polling
    (root / "events.json").write_text("[]", encoding="utf-8")
    released = threading.Event()

    class Held:
        def run(self, execution):
            assert released.wait(15)
            return TaskOutcome("succeeded")

        def cancel(self):
            released.set()

    tasks.max_concurrent = 1
    owner = TaskOrigin("role", "role:role", "held", "held")
    held = tasks.submit(owner, Held(), kind="fixture",
        budget=TaskBudget(timeout_seconds=30, log_bytes=1024, output_chars=1000))

    async def exercise():
        try:
            now[0] += 1800
            assert await app.on_clock() == 0
            await settle_all(app)
            rows = app.query("role")["runs"]
            assert len(rows) == 1 and rows[0]["status"] == "rejected"
            assert await app.on_clock() == 0
            assert not (root / "calls.jsonl").exists()
            released.set()
            assert tasks.query(held, owner, wait_seconds=3).ended_at is not None
            now[0] += 149
            assert await app.on_clock() == 0
            assert not (root / "calls.jsonl").exists()
            now[0] += 1
            assert await app.on_clock() == 0
            await settle_all(app)
            assert await app.on_clock() == 1
            await settle_all(app)
            assert [json.loads(line)["name"] for line in (root / "calls.jsonl").read_text().splitlines()] == ["poll", "get_events"]
        finally:
            released.set()
            tasks.stop(held, owner)

    asyncio.run(exercise())


def test_failed_source_poll_does_not_skip_other_source_or_block_its_a01(polling, source, tmp_path):
    app, now, root, _, sessions, *_ = polling
    _, store, data, *_ = source
    (root / "events.json").write_text("[]", encoding="utf-8")
    install_poll_profile(polling, source, tmp_path, fail=True)
    healthy = tmp_path / "healthy-source"
    healthy.mkdir()
    catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    (healthy / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    (healthy / "events.json").write_text(json.dumps([{"id": "healthy-event", "kind": "content",
        "title": "Available event", "body": "Available local content"}]), encoding="utf-8")
    server = store.save(data.model_copy(update={"serverId": "healthy",
        "command": [sys.executable, "-I", str(FIXTURE), str(healthy)], "cwd": str(healthy)}))
    current = AutonomyConfig.model_validate(app.query("role")["config"])
    grant = current.sourceGrants[0].model_copy(update={"sourceId": "healthy", "serverId": server.server_id,
        "configRevision": server.revision, "grantRevision": server.grant_id, "eventDomain": "healthy-domain"})
    app.configure("role", current.model_copy(update={"sourceGrants": [*current.sourceGrants, grant]}), expected_revision=current.revision)
    adapter = ReplyModel()
    app.adapter_factory = lambda _: adapter

    async def exercise():
        now[0] += 1800
        assert await app.on_clock() == 0
        first = await settle(app, "autonomy_poll")
        assert {state["sourceId"]: state["status"] for state in first["sources"]} == {"feed": "failed", "healthy": "ok"}
        assert await app.on_clock() == 1
        final = await settle(app, "autonomy_tick")
        assert final["status"] == "sent"
        assert {state["sourceId"]: state["status"] for state in final["sources"]} == {"feed": "failed", "healthy": "ok"}
        assert len(sessions.list_messages("role:role")) == 1 and len(adapter.requests) == 4
        for directory in (root, healthy):
            assert [json.loads(line)["name"] for line in (directory / "calls.jsonl").read_text().splitlines()] == ["poll", "get_events"]

    asyncio.run(exercise())


def test_shared_source_poll_operation_is_called_once_and_reports_each_source(polling):
    app, _, root, *_ = polling
    current = AutonomyConfig.model_validate(app.query("role")["config"])
    shared = current.sourceGrants[0].model_copy(update={"sourceId": "shared"})
    app.configure("role", current.model_copy(update={"sourceGrants": [*current.sourceGrants, shared]}), expected_revision=current.revision)

    async def exercise():
        await app.on_clock()
        first = await settle(app, "autonomy_poll")
        assert {state["sourceId"]: state["status"] for state in first["sources"]} == {"feed": "ok", "shared": "ok"}
        assert [json.loads(line)["name"] for line in (root / "calls.jsonl").read_text().splitlines()] == ["poll"]

    asyncio.run(exercise())


def test_poll_storage_failure_rolls_back_interval_and_admission_together(polling):
    app, _, root, _, sessions, *_ = polling

    async def exercise():
        with sqlite3.connect(sessions.database_path) as db:
            db.execute("""CREATE TRIGGER fail_poll_admission BEFORE INSERT ON autonomy_runs
                WHEN NEW.kind='autonomy_poll'
                BEGIN SELECT RAISE(ABORT,'controlled poll storage failure'); END""")
        try:
            with pytest.raises(sqlite3.IntegrityError):
                await app.on_clock()
            assert app.query("role")["runs"] == [] and not (root / "calls.jsonl").exists()
        finally:
            with sqlite3.connect(sessions.database_path) as db:
                db.execute("DROP TRIGGER fail_poll_admission")
        # No admission committed, so retrying this same clock has no consumed
        # poll interval or leftover reservation that could strand the source.
        await app.on_clock()
        await settle_all(app)
        assert len(app.query("role")["runs"]) == 1
        assert [json.loads(line)["name"] for line in (root / "calls.jsonl").read_text().splitlines()] == ["poll"]

    asyncio.run(exercise())


def test_legacy_schema_reopen_derives_hard_interval_from_actual_poll_history(polling):
    app, now, root, _, sessions, tasks, host = polling

    async def exercise():
        await app.on_clock()
        await settle_all(app)
        await app.close()
        # Preserve the real accepted/completed poll; only remove the newly
        # introduced schema columns to represent the pre-fix database.
        with sqlite3.connect(sessions.database_path) as db:
            for column in ("initial_poll_id", "poll_ready_generation", "last_poll_at", "poll_not_before", "poll_health"):
                db.execute(f"ALTER TABLE autonomy_roles DROP COLUMN {column}")
        reopened = ProactiveApplication(sessions, tasks, clock=lambda: now[0],
            role_getter=lambda key: {"id": key}, model_getter=lambda key: {"id": key}, mcp_host=host)
        try:
            await reopened.on_clock()
            await settle_all(reopened)
            assert len(reopened.query("role")["runs"]) == 1
            now[0] += 149
            await reopened.on_clock()
            assert len(reopened.query("role")["runs"]) == 1
            now[0] += 1
            await reopened.on_clock()
            await settle_all(reopened)
            assert [json.loads(line)["name"] for line in (root / "calls.jsonl").read_text().splitlines()] == ["poll", "poll"]
        finally:
            await reopened.close()

    asyncio.run(exercise())
