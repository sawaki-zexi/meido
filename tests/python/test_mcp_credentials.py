"""Public MCP lifecycle with the real vault adapter and a purely in-memory keyring."""
import sys
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.app.credentials import SystemCredentialStore
from backend.app.mcp_api import mcp_router
from backend.app.mcp_config import McpConfigStore
from backend.app.mcp_runtime import McpHost
from backend.app.role_store import RoleStore


class MissingCredential(Exception):
    pass


class MemoryKeyring:
    errors = SimpleNamespace(PasswordDeleteError=MissingCredential)

    def __init__(self):
        self.values = {}
        self.fail_chunk = None
        self.delete_mode = "runtime"
        self.fail_write = False

    def get_password(self, service, key):
        return self.values.get((service, key))

    def set_password(self, service, key, value):
        self.values[service, key] = value
        if self.fail_write and ".chunk." in key:
            raise RuntimeError("protected-chunk-value")

    def delete_password(self, service, key):
        if ".chunk." in key and self.fail_chunk == int(key.rsplit(".", 1)[1]):
            if self.delete_mode == "password":
                raise MissingCredential()
            if self.delete_mode == "silent":
                return
            raise RuntimeError("protected-chunk-value")
        if (service, key) not in self.values:
            raise MissingCredential()
        del self.values[service, key]


def memory_owner(tmp_path, monkeypatch):
    keyring = MemoryKeyring()
    monkeypatch.setitem(sys.modules, "keyring", keyring)
    path = tmp_path / "mcp.db"
    roles = RoleStore(tmp_path / "roles")
    service = "mcp-test-memory-only"
    # Same explicit strict adapter used by the production MCP host.
    holder = {"host": McpHost(McpConfigStore(path, SystemCredentialStore(service, strict_cleanup=True)))}
    app = FastAPI()
    app.include_router(mcp_router(lambda: holder["host"], lambda: roles))
    client = TestClient(app)

    def reopen():
        holder["host"] = McpHost(McpConfigStore(path, SystemCredentialStore(service, strict_cleanup=True)))
        return holder["host"].store

    payload = {"serverId": "notes", "name": "内存凭据", "command": [sys.executable], "cwd": str(tmp_path),
        "env": {"TOKEN": "a" * 2501}, "enableDiscovery": False}
    return client, holder["host"].store, keyring, payload, service, reopen


@pytest.mark.parametrize("failed_index,delete_mode", [(0, "runtime"), (1, "runtime"), (1, "password"), (1, "silent")])
def test_http_chunk_delete_failure_preserves_manifest_and_retry_ownership_after_restart(tmp_path, monkeypatch, failed_index, delete_mode):
    client, store, keyring, payload, service, reopen = memory_owner(tmp_path, monkeypatch)
    assert client.post("/api/mcp/servers", json=payload).status_code == 201
    primary = store.get("notes").credential_key("TOKEN")
    assert len(keyring.values) == 4, "2501 characters require one manifest and three chunks"
    keyring.fail_chunk = failed_index
    keyring.delete_mode = delete_mode
    response = client.delete("/api/mcp/servers/notes")
    assert response.status_code == 503
    assert "protected-chunk-value" not in response.text
    saved = client.get("/api/mcp/servers").json()["servers"][0]
    assert saved["enabled"] is False and saved["errors"]
    assert store.cleanup_pending("notes")
    assert keyring.values[(service, primary)].startswith("meido.credential-chunks.v1:")
    assert any(".chunk." in key for (_, key) in keyring.values)
    store = reopen()
    keyring.fail_chunk = None
    assert client.delete("/api/mcp/servers/notes").status_code == 204
    assert keyring.values == {}
    assert not store.cleanup_pending("notes")
    assert client.get("/api/mcp/servers").json()["servers"] == []


def test_http_replacing_chunked_secret_preserves_old_manifest_until_save_retry_cleans_it(tmp_path, monkeypatch):
    client, store, keyring, payload, service, reopen = memory_owner(tmp_path, monkeypatch)
    assert client.post("/api/mcp/servers", json=payload).status_code == 201
    old_primary = store.get("notes").credential_key("TOKEN")
    keyring.fail_chunk = 0
    payload["env"] = {"TOKEN": "b" * 2601}
    response = client.put("/api/mcp/servers/notes", json=payload)
    assert response.status_code == 503
    assert store.cleanup_pending("notes")
    assert keyring.values[(service, old_primary)].startswith("meido.credential-chunks.v1:")
    intermediate = store.get("notes").credential_key("TOKEN")
    assert intermediate != old_primary
    store = reopen()
    keyring.fail_chunk = None
    payload["env"] = None
    assert client.put("/api/mcp/servers/notes", json=payload).status_code == 200
    assert not store.cleanup_pending("notes")
    assert all(not key.startswith(old_primary) and not key.startswith(intermediate) for (_, key) in keyring.values)
    assert store.environment(store.get("notes"))["TOKEN"] == "b" * 2601
    assert len(keyring.values) == 4


def test_http_partial_chunk_write_failure_keeps_manifest_until_restart_delete_retry(tmp_path, monkeypatch):
    client, store, keyring, payload, service, reopen = memory_owner(tmp_path, monkeypatch)
    keyring.fail_write = True
    keyring.fail_chunk = 0
    response = client.post("/api/mcp/servers", json=payload)
    assert response.status_code == 503
    assert "protected-chunk-value" not in response.text
    assert store.cleanup_pending("notes")
    manifests = [value for (_, key), value in keyring.values.items() if ".chunk." not in key]
    assert len(manifests) == 1 and manifests[0].startswith("meido.credential-chunks.v1:")
    store = reopen()
    keyring.fail_write = False
    keyring.fail_chunk = None
    assert client.delete("/api/mcp/servers/notes").status_code == 204
    assert not store.cleanup_pending("notes")
    assert keyring.values == {}
