import json
import zlib
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.chat_attachments import public_attachment, resolve_attachment, save_attachment
from backend.app.models import AgentRun, RoleInput, RoleProfile
from backend.app.role_store import RoleStore
from backend.app.session_manager import SessionManager
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


def test_saved_attachment_has_a_public_metadata_contract_without_local_path(tmp_path: Path):
    internal = save_attachment(tmp_path, "notes.txt", "text/plain", "hello".encode())

    public = public_attachment(internal)

    assert public == {
        "id": internal["id"],
        "name": "notes.txt",
        "mediaType": "text/plain",
        "size": 5,
        "kind": "file",
    }
    assert "path" not in public
    assert "text" not in public
    assert resolve_attachment(tmp_path, str(internal["id"]))["path"] == internal["path"]


def test_pdf_attachment_extracts_text_from_a_flate_encoded_content_stream(tmp_path: Path):
    content = b"BT /F1 12 Tf (Compressed hello) Tj ET"
    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Length " + str(len(zlib.compress(content))).encode() + b" /Filter /FlateDecode >>\nstream\n"
        + zlib.compress(content)
        + b"\nendstream\nendobj\n%%EOF"
    )

    internal = save_attachment(tmp_path, "notes.pdf", "application/pdf", pdf)

    assert internal["text"] == "Compressed hello"


def test_image_attachment_is_resolved_only_inside_role_workspace(tmp_path: Path):
    internal = save_attachment(tmp_path, "photo.png", "image/png", b"\x89PNG\r\n\x1a\nimage")
    metadata_path = tmp_path / "attachments" / f"{internal['id']}.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["path"] = str(tmp_path.parent / "attachments" / "outside.png")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    assert resolve_attachment(tmp_path, str(internal["id"])) is None


def test_attachment_api_uploads_and_serves_role_scoped_content(tmp_path: Path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="测试角色", profile=RoleProfile(profile="设定")))
    monkeypatch.setattr(main, "store", roles)
    client = TestClient(main.app)

    uploaded = client.post(
        f"/api/roles/{role.id}/attachments",
        content=b"hello",
        headers={"content-type": "text/plain", "x-attachment-filename": "notes.txt"},
    )

    assert uploaded.status_code == 200
    metadata = uploaded.json()
    assert metadata == {
        "id": metadata["id"],
        "name": "notes.txt",
        "mediaType": "text/plain",
        "size": 5,
        "kind": "file",
    }
    assert "path" not in metadata
    downloaded = client.get(f"/api/roles/{role.id}/attachments/{metadata['id']}")
    assert downloaded.status_code == 200
    assert downloaded.content == b"hello"


def test_attachment_api_rejects_unsupported_extension_and_size_limit(tmp_path: Path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="测试角色", profile=RoleProfile(profile="设定")))
    monkeypatch.setattr(main, "store", roles)
    client = TestClient(main.app)

    unsupported = client.post(
        f"/api/roles/{role.id}/attachments",
        content=b"binary",
        headers={"content-type": "application/octet-stream", "x-attachment-filename": "program.exe"},
    )
    assert unsupported.status_code == 415

    monkeypatch.setattr("backend.app.chat_attachments.MAX_ATTACHMENT_BYTES", 3)
    oversized = client.post(
        f"/api/roles/{role.id}/attachments",
        content=b"hello",
        headers={"content-type": "text/plain", "x-attachment-filename": "notes.txt"},
    )
    assert oversized.status_code == 413


def test_pure_attachment_message_is_accepted_and_text_reaches_provider(tmp_path: Path, monkeypatch):
    class MultimodalAdapter:
        def __init__(self):
            self.messages = []

        async def stream_messages(self, messages, configuration=None):
            del configuration
            self.messages.append(messages)
            yield "收到"

    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="测试角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    adapter = MultimodalAdapter()
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", SessionManager(roles, sessions))
    monkeypatch.setattr(main, "model_adapter", adapter)
    monkeypatch.setattr(main, "role_locks", {})
    client = TestClient(main.app)
    uploaded = client.post(
        f"/api/roles/{role.id}/attachments",
        content=b"hello attachment",
        headers={"content-type": "text/plain", "x-attachment-filename": "notes.txt"},
    )
    attachment_id = uploaded.json()["id"]

    response = client.post(f"/api/roles/{role.id}/messages", json={"content": "", "attachments": [attachment_id]})

    assert response.status_code == 200
    assert adapter.messages[0][-1]["content"][0]["text"].endswith("[notes.txt]\nhello attachment")


def test_session_history_keeps_attachment_reference_without_local_path(tmp_path: Path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="测试角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    attachment = save_attachment(roles.workspace_path(role.id), "notes.txt", "text/plain", b"hello")
    now = datetime.now(timezone.utc)
    run = AgentRun(
        runId="run-attachment",
        roleId=role.id,
        sessionKey=f"role:{role.id}",
        status="created",
        startedAt=now,
    )
    sessions.open_role_session(role.id)
    sessions.create_run_with_messages(run, "", attachments=[attachment])
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", SessionManager(roles, sessions))

    response = TestClient(main.app).get(f"/api/roles/{role.id}/session")

    assert response.status_code == 200
    public = response.json()["messages"][0]["metadata"]["attachments"][0]
    assert public["id"] == attachment["id"]
    assert "path" not in public
    assert "text" not in public
