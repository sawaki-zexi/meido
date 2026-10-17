"""Host proofs come from trusted, isolated, real stdio contract executions."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from backend.app import mcp_stdio
from backend.app.mcp_config import McpServerInput
from backend.app.mcp_autonomy import declaration_hash
from backend.app.proactive_runtime.config import AutonomyConfig
from backend.app.proactive_runtime.source_validation import SourceContractRegistry, retry_allowed
from test_mcp_http import process_alive
from test_proactive_ack import acknowledgements, ack_environment, source, write_manifest  # noqa: F401


pytestmark = pytest.mark.usefixtures("local_platform_only")


@pytest.fixture
def contract(acknowledgements):
    app, _, root, _, sessions, _, host = acknowledgements
    grant = AutonomyConfig.model_validate(app.query("role")["config"]).sourceGrants[0]
    artifact = root / "contract-artifact"
    manifest = artifact / "manifest.json"
    return SimpleNamespace(app=app, root=root, sessions=sessions, store=host.store, grant=grant,
        artifact=artifact, manifest=manifest,
        authorized=lambda: app.role_getter("role") is not None and host.store.get(grant.serverId).enabled)


async def validate(value, registry, **overrides):
    return await registry.validate_local("role", value.grant,
        **{"manifest_path": value.manifest,
           "expected_manifest_sha256": hashlib.sha256(value.manifest.read_bytes()).hexdigest(),
           "authorized": value.authorized, **overrides})


def allowed(value, grant):
    with sqlite3.connect(value.sessions.database_path) as db:
        return retry_allowed(db, "role", grant)


def proof_count(value):
    with sqlite3.connect(value.sessions.database_path) as db:
        return db.execute("SELECT COUNT(*) FROM autonomy_source_contracts").fetchone()[0]


def test_real_isolated_restart_proof_binds_identity_tools_artifact_and_interpreter(contract, monkeypatch):
    value, launched, ack_results = contract, [], []
    existing = value.store.get(value.grant.serverId)
    server = value.store.save(McpServerInput(serverId=existing.server_id, name=existing.name,
        command=list(existing.command), cwd=existing.cwd, roleIds=list(existing.role_ids),
        env={"FIXTURE_ACCOUNT_SECRET": "synthetic-vault-value-must-not-be-read"}))
    config = AutonomyConfig.model_validate(value.app.query("role")["config"])
    catalog = json.loads((value.artifact / "catalog.json").read_text())
    grant = value.grant.model_copy(update={"configRevision": server.revision, "grantRevision": server.grant_id,
        "pollTool": "poll", "pollDefinitionHash": declaration_hash(next(item for item in catalog if item["name"] == "poll"))})
    saved = value.app.configure("role", config.model_copy(update={"sourceGrants": [grant]}), expected_revision=config.revision)
    value.grant = AutonomyConfig.model_validate(saved["config"]).sourceGrants[0]
    original_spawn = mcp_stdio.spawn_process_tree if os.name == "nt" else asyncio.create_subprocess_exec
    original_call = mcp_stdio.McpStdioClient.call

    async def record(*args, **kwargs):
        result = await original_spawn(*args, **kwargs)
        process = result[0] if os.name == "nt" else result
        launched.append((args, kwargs, process))
        return result

    async def record_call(client, name, arguments):
        response = await original_call(client, name, arguments)
        if name == "ack":
            ack_results.append((dict(arguments), bool(response.get("isError"))))
        return response

    def no_vault(*args):
        raise AssertionError("local source contract validation cannot read production credentials")

    async def exercise():
        registry = SourceContractRegistry(value.sessions.database_path, value.store)
        with monkeypatch.context() as patch:
            if os.name == "nt":
                patch.setattr(mcp_stdio, "spawn_process_tree", record)
            else:
                patch.setattr(asyncio, "create_subprocess_exec", record)
            patch.setattr(mcp_stdio.McpStdioClient, "call", record_call)
            patch.setattr(value.store, "environment", no_vault)
            patch.setattr(value.store.credentials, "get", no_vault)
            patch.setenv("API_KEY", "fixture-host-secret-must-not-inherit")
            try:
                proof = await validate(value, registry)
            finally:
                await registry.close()
        assert not registry.cleanup_pending
        assert len(launched) == 2
        assert ack_results[0][1] is True
        original_ack = ack_results[0][0]
        retries = [arguments for arguments, _ in ack_results if arguments["ack_id"] == original_ack["ack_id"]
                   and arguments["expires_at"] == original_ack["expires_at"]]
        assert retries == [original_ack, original_ack, original_ack]
        for arguments, options, process in launched:
            assert arguments[-2] == "--meido-contract-test"
            assert str(value.root) not in arguments
            assert Path(arguments[-3]).name == "source.py"
            assert "API_KEY" not in options["env"]
            assert "FIXTURE_ACCOUNT_SECRET" not in options["env"]
            assert "fixture-host-secret-must-not-inherit" not in options["env"].values()
            assert not Path(arguments[-1]).exists()
            assert not process_alive(process.pid)
        assert not (value.root / "calls.jsonl").exists()
        assert proof["verifierVersion"] == "meido-source-contract-v1"
        assert len(proof["artifactDigest"]) == 64
        proved = value.grant.model_copy(update={"ackProofId": proof["proofId"]})
        assert allowed(value, proved)
        for changed in ({"ackProofId": "0" * 32}, {"revision": proved.revision + 1},
                        {"eventDomain": "different-events"}, {"getDefinitionHash": "0" * 64},
                        {"pollDefinitionHash": "0" * 64}, {"ackDefinitionHash": "0" * 64},
                        {"configRevision": "changed"}, {"grantRevision": "changed"}):
            assert not allowed(value, proved.model_copy(update=changed))
        with sqlite3.connect(value.sessions.database_path) as db:
            assert not retry_allowed(db, "another-role", proved)
        script = value.artifact / "source.py"
        script.write_bytes(script.read_bytes() + b"\n# changed trusted artifact\n")
        assert not allowed(value, proved)
        assert proof_count(value) == 1

    asyncio.run(exercise())


def test_owner_trusted_artifact_still_requires_the_frozen_tool_definition_hashes(contract):
    value = contract
    catalog_path = value.artifact / "catalog.json"
    catalog = json.loads(catalog_path.read_text())
    catalog[0]["description"] = "different trusted file, not the frozen source tool"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    write_manifest(value.artifact)

    async def exercise():
        registry = SourceContractRegistry(value.sessions.database_path, value.store)
        try:
            with pytest.raises(PermissionError, match="definition"):
                await validate(value, registry)
        finally:
            await registry.close()
        assert not registry.cleanup_pending and proof_count(value) == 0
        assert not (value.root / "calls.jsonl").exists()

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["untrusted_digest", "support_changed", "path_escape", "entry_not_command"])
def test_manifest_trust_and_local_entrypoint_are_checked_before_launch(contract, failure):
    value = contract
    expected = hashlib.sha256(value.manifest.read_bytes()).hexdigest()
    if failure == "untrusted_digest":
        expected = "0" * 64
    elif failure == "support_changed":
        (value.artifact / "catalog.json").write_text("[]", encoding="utf-8")
    else:
        manifest = json.loads(value.manifest.read_text())
        if failure == "path_escape":
            manifest["files"]["../outside.py"] = "0" * 64
        else:
            (value.artifact / "other.py").write_text("raise AssertionError('must not run')")
            manifest["files"]["other.py"] = hashlib.sha256((value.artifact / "other.py").read_bytes()).hexdigest()
            manifest["entrypoint"] = "other.py"
        value.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        expected = hashlib.sha256(value.manifest.read_bytes()).hexdigest()

    async def exercise():
        registry = SourceContractRegistry(value.sessions.database_path, value.store)
        try:
            with pytest.raises((ValueError, PermissionError)):
                await validate(value, registry, expected_manifest_sha256=expected)
        finally:
            await registry.close()
        assert not registry.cleanup_pending
        assert proof_count(value) == 0
        assert not (value.root / "pid").exists()

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["extends_expiry", "loses_state_on_restart"])
def test_real_source_contract_rejects_semantic_idempotence_or_restart_failure(contract, failure):
    value = contract
    script = value.artifact / "source.py"
    body = script.read_text(encoding="utf-8")
    if failure == "extends_expiry":
        body = body.replace('old.setdefault(identity, params["arguments"])', 'old[identity] = params["arguments"]')
    else:
        body = body.replace('root.mkdir(parents=True, exist_ok=True)',
            'root.mkdir(parents=True, exist_ok=True)\n    if contract:\n        (root / "acks.json").unlink(missing_ok=True)')
    script.write_text(body, encoding="utf-8")
    write_manifest(value.artifact)

    async def exercise():
        registry = SourceContractRegistry(value.sessions.database_path, value.store)
        try:
            with pytest.raises(ValueError, match="extended|restart"):
                await validate(value, registry)
        finally:
            await registry.close()
        assert not registry.cleanup_pending and proof_count(value) == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("stop", ["cancel", "revoke"])
def test_cancel_or_revocation_during_real_rpc_joins_without_publishing_proof(contract, monkeypatch, stop):
    value, revoked, signal = contract, threading.Event(), threading.Event()
    original = mcp_stdio.McpStdioClient.call
    launched = []
    original_spawn = mcp_stdio.spawn_process_tree if os.name == "nt" else asyncio.create_subprocess_exec

    async def record(*args, **kwargs):
        result = await original_spawn(*args, **kwargs)
        process = result[0] if os.name == "nt" else result
        launched.append(process)
        return result

    async def lose_authority(client, name, arguments):
        result = await original(client, name, arguments)
        if name == "meido_test_seed":
            (signal if stop == "cancel" else revoked).set()
        return result

    async def exercise():
        registry = SourceContractRegistry(value.sessions.database_path, value.store)
        with monkeypatch.context() as patch:
            if os.name == "nt":
                patch.setattr(mcp_stdio, "spawn_process_tree", record)
            else:
                patch.setattr(asyncio, "create_subprocess_exec", record)
            patch.setattr(mcp_stdio.McpStdioClient, "call", lose_authority)
            try:
                with pytest.raises((PermissionError, asyncio.CancelledError)):
                    await validate(value, registry, signal=signal,
                        authorized=lambda: not revoked.is_set() and value.authorized())
            finally:
                await registry.close()
        assert launched and all(not process_alive(process.pid) for process in launched)
        assert not registry.cleanup_pending and proof_count(value) == 0

    asyncio.run(exercise())


def test_failed_temp_cleanup_retains_owned_root_for_close_without_restarting_rpc(contract, monkeypatch):
    from backend.app.proactive_runtime import source_validation
    value = contract
    roots = []
    original = source_validation.shutil.rmtree

    def disk_failure(path):
        roots.append(Path(path))
        raise OSError("controlled owned temporary root cleanup failure")

    async def exercise():
        registry = SourceContractRegistry(value.sessions.database_path, value.store)
        with monkeypatch.context() as patch:
            patch.setattr(source_validation.shutil, "rmtree", disk_failure)
            with pytest.raises(OSError):
                await validate(value, registry)
        assert registry.cleanup_pending and roots[0].exists()
        assert proof_count(value) == 0
        await registry.close()
        assert not roots[0].exists() and not registry.cleanup_pending

    asyncio.run(exercise())


def test_revocation_after_both_real_peers_close_prevents_final_proof_publication(contract, monkeypatch):
    value, revoked, closed = contract, threading.Event(), []
    original = mcp_stdio.McpStdioClient.close

    async def close_then_revoke(client):
        await original(client)
        closed.append(client)
        if len(closed) == 2:
            revoked.set()

    async def exercise():
        registry = SourceContractRegistry(value.sessions.database_path, value.store)
        with monkeypatch.context() as patch:
            patch.setattr(mcp_stdio.McpStdioClient, "close", close_then_revoke)
            try:
                with pytest.raises((PermissionError, asyncio.CancelledError)):
                    await validate(value, registry, authorized=lambda: not revoked.is_set() and value.authorized())
            finally:
                await registry.close()
        assert len(closed) == 2
        assert not registry.cleanup_pending and proof_count(value) == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["sqlite", "storage_quota"])
def test_proof_publish_failure_rolls_back_after_owned_stdio_and_directory_cleanup(contract, monkeypatch, failure):
    from backend.app.proactive_runtime import retention
    value = contract

    async def exercise():
        registry = SourceContractRegistry(value.sessions.database_path, value.store)
        try:
            with monkeypatch.context() as patch:
                if failure == "sqlite":
                    with sqlite3.connect(value.sessions.database_path) as db:
                        db.execute("CREATE TRIGGER contract_proof_disk_failure BEFORE INSERT ON autonomy_source_contracts BEGIN SELECT RAISE(ABORT,'controlled proof publication failure'); END")
                    expected = sqlite3.Error
                else:
                    patch.setattr(retention, "DEFAULT_LIMITS", retention.Limits(global_bytes=1))
                    expected = retention.StorageLimitError
                with pytest.raises(expected):
                    await validate(value, registry)
        finally:
            await registry.close()
            if failure == "sqlite":
                with sqlite3.connect(value.sessions.database_path) as db:
                    db.execute("DROP TRIGGER contract_proof_disk_failure")
        assert not registry.cleanup_pending and proof_count(value) == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("directory", ["state", "artifact"])
def test_real_test_profile_storage_overflow_is_stopped_and_owned_files_are_joined(contract, directory):
    value = contract
    script = value.artifact / "source.py"
    body = script.read_text(encoding="utf-8")
    target = 'root / "oversized-contract-state"' if directory == "state" else 'Path(__file__).parent / "oversized-contract-artifact"'
    body = body.replace('payload = {"seeded": True,',
        f'({target}).write_bytes(b"x" * (8 * 1024 * 1024 + 1))\n                payload = {{"seeded": True,')
    script.write_text(body, encoding="utf-8")
    write_manifest(value.artifact)

    async def exercise():
        registry = SourceContractRegistry(value.sessions.database_path, value.store)
        try:
            with pytest.raises((PermissionError, asyncio.CancelledError)):
                await validate(value, registry)
        finally:
            await registry.close()
        assert not registry.cleanup_pending and proof_count(value) == 0

    asyncio.run(exercise())
