"""The owner HTTP boundary requires a specific, explicit local-code action."""
import asyncio
import hashlib
import json
import sqlite3
import sys

import pytest

from backend.app import main
from backend.app.mcp_runtime import McpHost
from backend.app.mcp_autonomy import declaration_hash
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401
from test_proactive_mcp import source, FIXTURE  # noqa: F401
from test_proactive_ack import write_manifest
from test_proactive_snapshot import grant_for


pytestmark = pytest.mark.usefixtures("local_platform_only")


@pytest.mark.parametrize("change", [
    {"trustLocalCode": False}, {"trustLocalCode": 1}, {"trustLocalCode": "true"},
    {"manifestSha256": "not-a-digest"}, {"requestNonce": "model-supplied-tool-call"},
    {"owner_context": True}, {"command": ["python", "arbitrary.py"]},
])
def test_verifier_http_rejects_implicit_or_forged_trust_without_history(api, change):
    client, _, sessions, _ = api
    role = client.post("/api/roles", json={"name": "验证边界", "profile": {"profile": "隔离"}}).json()["role"]
    body = {"requestNonce": "00000000-0000-4000-8000-000000000001", "sourceId": "source",
        "expectedRevision": 1, "manifestPath": "D:/trusted/manifest.json", "manifestSha256": "a" * 64,
        "trustLocalCode": True, **change}
    response = client.post(f"/api/roles/{role['id']}/autonomy/source-verifications", json=body)
    assert response.status_code == 422, response.text
    assert sessions.list_messages(f"role:{role['id']}") == []


def test_verifier_http_bounds_body_before_parsing_or_launching(api):
    client, *_ = api
    response = client.post("/api/roles/role/autonomy/source-verifications",
        content=b" " * (16 * 1024 + 1), headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def test_owner_http_verifies_real_isolated_source_and_explicitly_saves_proof_without_chat(api, source, monkeypatch):
    client, _, sessions, _ = api
    root, store, server_input, *_ = source
    role = client.post("/api/roles", json={"name": "验证真实来源", "profile": {"profile": "隔离"}}).json()["role"]
    role_id = role["id"]
    acknowledgement = {"name": "ack", "description": "persistent fixed-expiry acknowledgement",
        "inputSchema": {"type": "object", "properties": {"event_ids": {"type": "array", "items": {"type": "string"}},
            "ack_id": {"type": "string"}, "expires_at": {"type": ["number", "null"]},
            "ttl_hours": {"type": "integer", "minimum": 0}}, "required": ["event_ids", "ttl_hours"], "additionalProperties": False}}
    artifact = root / "contract-artifact"
    artifact.mkdir()
    (artifact / "source.py").write_bytes(FIXTURE.read_bytes())
    catalog = json.loads((root / "catalog.json").read_text())
    (artifact / "catalog.json").write_text(json.dumps([*catalog, acknowledgement]), encoding="utf-8")
    manifest, digest = write_manifest(artifact)
    server = store.save(server_input.model_copy(update={"roleIds": [role_id],
        "command": [sys.executable, "-I", str(artifact / "source.py"), str(root)]}))
    grant = grant_for(source).model_copy(update={"configRevision": server.revision, "grantRevision": server.grant_id,
        "ackTool": "ack", "ackDefinitionHash": declaration_hash(acknowledgement), "ackContract": "absolute-idempotent"})
    host = McpHost(store)
    monkeypatch.setattr(main, "mcp_host", host)
    monkeypatch.setattr(main, "_proactive", None)
    path = f"/api/roles/{role_id}/autonomy"
    try:
        saved = client.put(path, json={"expectedRevision": 0, "config": {"sourceGrants": [grant.model_dump(mode="json")]}})
        assert saved.status_code == 200, saved.text
        assert not (root / "pid").exists()
        request = {"requestNonce": "00000000-0000-4000-8000-000000000002", "sourceId": grant.sourceId,
            "expectedRevision": 1, "manifestPath": str(manifest), "manifestSha256": digest, "trustLocalCode": True}
        submitted = client.post(path + "/source-verifications", json=request)
        assert submitted.status_code == 202, submitted.text
        identifier = submitted.json()["id"]

        async def settle():
            async with asyncio.timeout(20):
                while True:
                    await main._proactive.source_verifications.recover()
                    result = main._proactive.source_verifications.query(role_id, identifier)
                    if result["runtimeAcknowledged"]:
                        return result
                    await asyncio.sleep(.02)
        verified = asyncio.run(settle())
        assert verified["status"] == "succeeded", verified
        proof = verified["proof"]
        assert proof and proof["sourceId"] == grant.sourceId and proof["sourceRevision"] == 1
        replay = client.post(path + "/source-verifications", json=request)
        assert replay.status_code == 202 and replay.json()["id"] == identifier
        request["manifestSha256"] = "0" * 64
        assert client.post(path + "/source-verifications", json=request).status_code == 409
        before = client.get(path).json()
        assert before["config"]["sourceGrants"][0]["ackProofId"] is None
        assert before["sourceVerifications"][0]["proof"] == proof
        config = before["config"]
        config["sourceGrants"][0]["ackProofId"] = proof["proofId"]
        used = client.put(path, json={"expectedRevision": 1, "config": config})
        assert used.status_code == 200, used.text
        assert used.json()["config"]["sourceGrants"][0]["revision"] == 1
        assert used.json()["config"]["sourceGrants"][0]["ackProofId"] == proof["proofId"]
        assert sessions.list_messages(f"role:{role_id}") == []
        assert not (root / "pid").exists() and not (root / "calls.jsonl").exists()
        with sqlite3.connect(sessions.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM autonomy_source_verifications").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
            assert db.execute("SELECT remaining_bytes FROM autonomy_storage_reservations WHERE operation_id=?", (identifier,)).fetchone()[0] == 0
    finally:
        if main._proactive is not None:
            asyncio.run(main._proactive.close())
        asyncio.run(host.close())
