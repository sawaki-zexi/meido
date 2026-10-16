"""Source acceptance and the independent Runtime ACK never repeat a message."""
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import threading

import pytest

from backend.app.mcp_autonomy import declaration_hash
from backend.app.mcp_runtime import McpHost
from backend.app.proactive_runtime import ProactiveApplication
from backend.app.proactive_runtime.config import AutonomyConfig
from backend.app.proactive_runtime.source_validation import SourceContractRegistry, ensure_schema
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskRuntime
from test_proactive_delivery import ReplyModel
from test_proactive_mcp import FIXTURE, source
from test_proactive_snapshot import grant_for


pytestmark = pytest.mark.usefixtures("local_platform_only")


@pytest.fixture(autouse=True)
def ack_environment(tmp_path, monkeypatch, local_platform_only):
    monkeypatch.setenv("MEIDO_DATA_DIR", str(tmp_path / "isolated-data"))
    monkeypatch.setenv("MEIDO_ROLES_DIR", str(tmp_path / "isolated-roles"))
    monkeypatch.setenv("MEIDO_OWNER_KNOWLEDGE_DATA_DIR", str(tmp_path / "isolated-owner"))
    monkeypatch.delenv("MEIDO_PROJECT_ROOT", raising=False)
    yield


def write_manifest(directory):
    manifest = {"profile": "meido-source-contract-v1", "version": 1,
                "entrypoint": "source.py", "files": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                    for name in ("source.py", "catalog.json")}}
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def acknowledgements(source, tmp_path):
    root, store, server_input, *_ = source
    declaration = {"name": "ack", "description": "persistent fixed-expiry acknowledgement",
        "inputSchema": {"type": "object", "properties": {
            "event_ids": {"type": "array", "items": {"type": "string"}},
            "ack_id": {"type": "string"}, "expires_at": {"type": ["number", "null"]},
            "ttl_hours": {"type": "integer", "minimum": 0}},
            "required": ["event_ids", "ttl_hours"], "additionalProperties": False}}
    catalog = json.loads((root / "catalog.json").read_text())
    (root / "catalog.json").write_text(json.dumps([*catalog, declaration]))
    artifact = root / "contract-artifact"
    artifact.mkdir()
    (artifact / "source.py").write_bytes(FIXTURE.read_bytes())
    (artifact / "catalog.json").write_bytes((root / "catalog.json").read_bytes())
    write_manifest(artifact)
    # ACK scenarios include native launch and schema validation; keep a finite
    # setup budget within the production 30-second autonomous RPC cap.
    server = store.save(server_input.model_copy(update={"command": [sys.executable, "-I", str(artifact / "source.py"), str(root)],
        "timeoutSeconds": 10}))
    grant = grant_for(source).model_copy(update={"ackTool": "ack", "ackDefinitionHash": declaration_hash(declaration),
        "ackContract": "absolute-idempotent", "configRevision": server.revision, "grantRevision": server.grant_id})
    data = tmp_path / "acknowledgements"
    initialize_databases(data)
    sessions, tasks, host = SessionStore(data / "sessions.db"), TaskRuntime(data / "tasks"), McpHost(store)
    now, adapter = [1780000000.0], ReplyModel()
    app = ProactiveApplication(sessions, tasks, clock=lambda: now[0],
        role_getter=lambda key: {"id": key}, model_getter=lambda key: {"id": key}, mcp_host=host,
        adapter_factory=lambda config: adapter)
    app.configure("role", AutonomyConfig(activeDecisionEnabled=True,
        decisionModelConfigurationId="explicit-model", consentRevision=1,
        sourceGrants=[grant]), expected_revision=0)
    yield app, now, root, adapter, sessions, tasks, host
    asyncio.run(app.close())
    asyncio.run(host.close())
    tasks.close()


async def prove_contract(acknowledgements):
    app, _, root, _, sessions, _, host = acknowledgements
    config = AutonomyConfig.model_validate(app.query("role")["config"])
    grant = config.sourceGrants[0]
    manifest = root / "contract-artifact/manifest.json"
    registry = SourceContractRegistry(sessions.database_path, host.store)
    with sqlite3.connect(sessions.database_path) as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_schema(db)
    try:
        proof = await registry.validate_local("role", grant, manifest_path=manifest,
            expected_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
            authorized=lambda: app.role_getter("role") is not None and host.store.get(grant.serverId).enabled)
    finally:
        await registry.close()
    app.configure("role", config.model_copy(update={"sourceGrants": [grant.model_copy(update={"ackProofId": proof["proofId"]})]}),
                  expected_revision=config.revision)
    return proof


async def settle_ack(app, status):
    async with asyncio.timeout(10):
        while True:
            await app.recover()
            rows = app.query("role")["acknowledgements"]
            if rows and rows[0]["status"] == status:
                return rows[0]
            await asyncio.sleep(.01)


async def reply_and_ack_scenario(acknowledgements):
    app, now, root, adapter, sessions, *_ = acknowledgements
    now[0] += 1800
    await app.on_clock()
    async with asyncio.timeout(10):
        ack = await settle_ack(app, "acknowledged")
        # The business receipt precedes the independent Runtime terminal result.
        while True:
            await app.recover()
            if all(run["runtimeAcknowledged"] for run in app.query("role")["runs"]):
                break
            await asyncio.sleep(.01)
    assert ack["attempts"] == 1
    assert ack["expiresAt"] == ack["decisionAt"] + 168 * 3600
    calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
    assert [call["name"] for call in calls] == ["get_events", "ack"]
    assert calls[1]["arguments"]["ack_id"] == ack["id"]
    assert calls[1]["arguments"]["expires_at"] == ack["expiresAt"]
    assert len(sessions.list_messages("role:role")) == 1
    assert len(adapter.requests) == 4
    runs = app.query("role")["runs"]
    assert all(run["runtimeAcknowledged"] for run in runs)
    assert next(run for run in runs if run["kind"] == "autonomy_ack")["timeoutSeconds"] == 30


def test_reply_then_absolute_ack_uses_fixed_receipt_and_releases_both_runtime_results(acknowledgements):
    asyncio.run(reply_and_ack_scenario(acknowledgements))


def test_reply_ack_preserves_business_guarantees_with_a_bounded_slow_first_connect(acknowledgements, monkeypatch):
    from backend.app.mcp_stdio import McpStdioClient

    original = McpStdioClient.connect
    connected, delayed = [], []

    async def slow_first(client):
        result = await original(client)
        connected.append(client)
        if len(connected) == 1:
            await asyncio.sleep(3.05)
            delayed.append(client)
        return result

    monkeypatch.setattr(McpStdioClient, 'connect', slow_first)
    asyncio.run(reply_and_ack_scenario(acknowledgements))
    assert len(connected) == 2 and len(delayed) == 1
    assert all(client.exit_confirmed for client in connected)


def test_reply_waits_for_the_original_runtime_result_after_the_business_ack_is_visible(acknowledgements, monkeypatch):
    from backend.app.proactive_runtime.execution import AutonomyExecutor

    app, _, _, _, sessions, tasks, _ = acknowledgements
    entered, release = threading.Event(), threading.Event()
    original = AutonomyExecutor.run
    observed = {}

    def hold_after_real_execution(executor, execution):
        outcome = original(executor, execution)
        if executor.origin.allowed_modes == ('source_ack',):
            observed.update(task_id=execution.task_id, key=executor.origin.tick_id)
            entered.set()
            assert release.wait(5), 'owned terminal release did not complete'
        return outcome

    monkeypatch.setattr(AutonomyExecutor, 'run', hold_after_real_execution)

    async def release_after_observing_pending_runtime():
        try:
            async with asyncio.timeout(8):
                while not entered.is_set():
                    await asyncio.sleep(.01)
            state = app.query('role')
            assert state['acknowledgements'][0]['status'] == 'acknowledged'
            pending = next(item for item in state['runs'] if item['kind'] == 'autonomy_ack')
            assert pending['runtimeAcknowledged'] is False
            with sqlite3.connect(tasks.root / 'tasks.db') as db:
                before = db.execute('SELECT status,ended_at,retain_until_ack FROM tasks WHERE task_id=?', (observed['task_id'],)).fetchone()
            assert before == ('running', None, 1)
            observed['receipt'] = state['acknowledgements'][0]
            # An independently owned task releases while the scenario waits.
            await asyncio.sleep(.15)
        finally:
            release.set()

    async def exercise():
        releaser = asyncio.create_task(release_after_observing_pending_runtime())
        try:
            await reply_and_ack_scenario(acknowledgements)
        finally:
            release.set()
            await releaser
            async with asyncio.timeout(10):
                while True:
                    await app.recover()
                    if all(item['runtimeAcknowledged'] for item in app.query('role')['runs']):
                        break
                    await asyncio.sleep(.01)
        assert app.query('role')['acknowledgements'][0] == observed['receipt']
    asyncio.run(exercise())
    with sqlite3.connect(tasks.root / 'tasks.db') as db:
        assert db.execute('SELECT status,retain_until_ack FROM tasks WHERE task_id=?', (observed['task_id'],)).fetchone() == ('succeeded', 0)
        assert db.execute('SELECT submission_key,task_id FROM task_submissions WHERE task_id=?', (observed['task_id'],)).fetchall() == [(observed['key'], observed['task_id'])]
    with sqlite3.connect(sessions.database_path) as db:
        copied, dirty, acknowledged = db.execute('SELECT runtime_result,result_ack_pending,runtime_acknowledged FROM autonomy_runs WHERE tick_id=?', (observed['key'],)).fetchone()
        assert copied is not None and (dirty, acknowledged) == (0, 1)
        assert db.execute('SELECT remaining_bytes FROM autonomy_storage_reservations WHERE operation_id=?', (observed['key'],)).fetchone() == (0,)


def test_ack_receipt_loss_retries_only_new_ordinal_with_original_expiry(acknowledgements):
    app, now, root, adapter, sessions, *_ = acknowledgements
    (root / "fail-ack-count").write_text("1")

    async def exercise():
        await prove_contract(acknowledgements)
        now[0] += 1800
        await app.on_clock()
        first = await settle_ack(app, "retry_wait")
        assert first["attempts"] == 1
        now[0] += 59
        await app.recover()
        assert app.query("role")["acknowledgements"][0]["attempts"] == 1
        now[0] += 1
        second = await settle_ack(app, "acknowledged")
        assert second["attempts"] == 2
        assert (second["id"], second["decisionAt"], second["expiresAt"]) == (first["id"], first["decisionAt"], first["expiresAt"])
        calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines() if json.loads(line)["name"] == "ack"]
        assert len(calls) == 2
        assert calls[0]["arguments"]["ack_id"] == calls[1]["arguments"]["ack_id"]
        assert calls[0]["arguments"]["expires_at"] == calls[1]["arguments"]["expires_at"]
        assert len(json.loads((root / "acks.json").read_text())) == 1
        assert len(sessions.list_messages("role:role")) == 1
        assert len(adapter.requests) == 4
    asyncio.run(exercise())


def test_legacy_ack_is_one_attempt_and_never_replays_after_lost_receipt(acknowledgements):
    app, now, root, _, sessions, *_ = acknowledgements
    config = AutonomyConfig.model_validate(app.query("role")["config"])
    legacy = config.sourceGrants[0].model_copy(update={"ackContract": "legacy-ttl"})
    app.configure("role", config.model_copy(update={"sourceGrants": [legacy]}), expected_revision=config.revision)
    (root / "fail-ack-count").write_text("1")

    async def exercise():
        # A configuration update with no owned work has nothing to join.
        now[0] += 1800
        await app.on_clock()
        first = await settle_ack(app, "ack_uncertain")
        now[0] += 300
        for _ in range(3):
            await app.recover()
        calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines() if json.loads(line)["name"] == "ack"]
        assert len(calls) == 1
        assert calls[0]["arguments"] == {"event_ids": ["event"], "ttl_hours": 168}
        assert app.query("role")["acknowledgements"][0]["attempts"] == first["attempts"] == 1
        assert len(sessions.list_messages("role:role")) == 1
    asyncio.run(exercise())


def test_old_source_ack_cannot_follow_remove_and_recreate_aba(acknowledgements):
    app, now, root, *_ = acknowledgements
    (root / "fail-ack-count").write_text("1")

    async def exercise():
        await prove_contract(acknowledgements)
        now[0] += 1800
        await app.on_clock()
        await settle_ack(app, "retry_wait")
        config = AutonomyConfig.model_validate(app.query("role")["config"])
        app.configure("role", config.model_copy(update={"sourceGrants": []}), expected_revision=config.revision)
        await app.recover()
        recreated = app.configure("role", config, expected_revision=config.revision + 1)
        assert recreated["config"]["sourceGrants"][0]["revision"] > config.sourceGrants[0].revision
        now[0] += 60
        await app.recover()
        assert app.query("role")["acknowledgements"][0]["status"] == "revoked"
        calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines() if json.loads(line)["name"] == "ack"]
        assert len(calls) == 1
    asyncio.run(exercise())


@pytest.mark.parametrize("claimed_proof", [None, "0" * 32])
def test_unverified_absolute_wire_claim_does_not_authorize_another_ack(acknowledgements, claimed_proof):
    app, now, root, *_ = acknowledgements
    if claimed_proof is not None:
        config = AutonomyConfig.model_validate(app.query("role")["config"])
        grant = config.sourceGrants[0].model_copy(update={"ackProofId": claimed_proof})
        app.configure("role", config.model_copy(update={"sourceGrants": [grant]}), expected_revision=config.revision)
    (root / "fail-ack-count").write_text("1")

    async def exercise():
        now[0] += 1800
        await app.on_clock()
        first = await settle_ack(app, "ack_uncertain")
        now[0] += 300
        for _ in range(3):
            await app.recover()
        calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()
                 if json.loads(line)["name"] == "ack"]
        assert len(calls) == first["attempts"] == 1
        assert app.query("role")["acknowledgements"][0]["status"] == "ack_uncertain"

    asyncio.run(exercise())


def test_changed_artifact_revokes_a_real_proof_before_the_second_ack_ordinal(acknowledgements):
    app, now, root, *_ = acknowledgements
    (root / "fail-ack-count").write_text("1")

    async def exercise():
        await prove_contract(acknowledgements)
        now[0] += 1800
        await app.on_clock()
        first = await settle_ack(app, "retry_wait")
        script = root / "contract-artifact/source.py"
        script.write_bytes(script.read_bytes() + b"\n# artifact changed after first ACK\n")
        now[0] += 60
        final = await settle_ack(app, "ack_uncertain")
        calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()
                 if json.loads(line)["name"] == "ack"]
        assert final["attempts"] == first["attempts"] == len(calls) == 1
        assert final["expiresAt"] == first["expiresAt"]

    asyncio.run(exercise())
