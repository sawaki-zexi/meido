from fastapi.testclient import TestClient

from backend.app import main
from backend.app.models import RoleInput, RoleProfile
from backend.app.role_store import RoleStore
from backend.app.session_manager import SessionManager
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


class FailingAdapter:
    async def stream_reply(self, role, history):
        yield "部分内容"
        raise RuntimeError("模型连接中断")


class SuccessAdapter:
    def __init__(self):
        self.histories = []

    async def stream_reply(self, role, history):
        assert role.profile.profile == "核心设定"
        assert history[-1].content == "你好"
        self.histories.append(history)
        yield "你好，"
        yield "主人。"


def configure_session_api(tmp_path, monkeypatch, adapter):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="测试女仆", profile=RoleProfile(profile="核心设定", personality="温柔")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", SessionManager(roles, sessions))
    monkeypatch.setattr(main, "model_adapter", adapter)
    monkeypatch.setattr(main, "role_locks", {})
    return role, sessions, TestClient(main.app)
from backend.app.role_store import RoleStore


def test_role_api_crud_and_duplicate_names(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "store", RoleStore(tmp_path / "roles"))
    client = TestClient(main.app)
    payload = {"name": "测试角色", "profile": {"profile": "核心设定"}}

    first_response = client.post("/api/roles", json=payload)
    second_response = client.post("/api/roles", json=payload)
    assert first_response.status_code == 201
    assert second_response.status_code == 201
    first = first_response.json()["role"]
    second = second_response.json()["role"]
    assert first["id"] != second["id"]

    updated_response = client.put(
        f"/api/roles/{first['id']}",
        json={"name": "改名", "profile": {"profile": "更新设定"}},
    )
    assert updated_response.status_code == 200
    assert updated_response.json()["role"]["id"] == first["id"]
    assert client.get(f"/api/roles/{first['id']}").json()["role"]["name"] == "改名"
    assert len(client.get("/api/roles").json()["roles"]) == 2


def test_role_api_rejects_blank_required_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "store", RoleStore(tmp_path / "roles"))
    client = TestClient(main.app)
    assert client.post("/api/roles", json={"name": " ", "profile": {"profile": "设定"}}).status_code == 422
    assert client.post("/api/roles", json={"name": "角色", "profile": {"profile": " "}}).status_code == 422


def test_role_has_one_persistent_session_and_streams_completed_reply(tmp_path, monkeypatch):
    adapter = SuccessAdapter()
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, adapter)
    first = client.get(f"/api/roles/{role.id}/session").json()
    second = client.get(f"/api/roles/{role.id}/session").json()
    assert first["session"]["sessionKey"] == f"role:{role.id}"
    assert first["session"]["sessionKey"] == second["session"]["sessionKey"]

    response = client.post(f"/api/roles/{role.id}/messages", json={"content": "你好"})
    assert response.status_code == 200
    assert "event: assistant_delta" in response.text
    assert "event: assistant_completed" in response.text
    stored = sessions.list_messages(f"role:{role.id}")
    assert [(item.role, item.content, item.status) for item in stored] == [
        ("user", "你好", "completed"),
        ("assistant", "你好，主人。", "completed"),
    ]
    reopened = client.get(f"/api/roles/{role.id}/session").json()
    assert [message["sequence"] for message in reopened["messages"]] == [1, 2]


def test_stream_failure_keeps_partial_reply_failed_and_allows_retry(tmp_path, monkeypatch):
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, FailingAdapter())
    failed = client.post(f"/api/roles/{role.id}/messages", json={"content": "测试中断"})
    assert "event: assistant_failed" in failed.text
    messages = sessions.list_messages(f"role:{role.id}")
    assert messages[-1].content == "部分内容"
    assert messages[-1].status == "failed"

    retry_adapter = SuccessAdapter()
    monkeypatch.setattr(main, "model_adapter", retry_adapter)
    retry = client.post(f"/api/roles/{role.id}/messages", json={"content": "你好"})
    assert "event: assistant_completed" in retry.text
    assert sessions.list_messages(f"role:{role.id}")[-1].status == "completed"
    assert all(message.content != "部分内容" for message in retry_adapter.histories[0])


def test_sessions_database_migrates_legacy_role_messages(tmp_path):
    import sqlite3

    data = tmp_path / "old-data"
    data.mkdir()
    with sqlite3.connect(data / "sessions.db") as connection:
        connection.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, role_id TEXT, author TEXT, content TEXT, status TEXT, created_at TEXT)")
        connection.execute("INSERT INTO messages VALUES ('m1', 'role-1', 'user', '旧消息', 'completed', '2025-01-01T00:00:00+00:00')")
    initialize_databases(data)
    migrated = SessionStore(data / "sessions.db").list_messages("role:role-1")
    assert len(migrated) == 1
    assert migrated[0].content == "旧消息"
    assert migrated[0].role == "user"


def test_interrupted_streaming_message_recovers_as_failed(tmp_path):
    data = tmp_path / "recovery"
    initialize_databases(data)
    first_store = SessionStore(data / "sessions.db")
    session = first_store.open_role_session("role-recover")
    first_store.append_message(session.sessionKey, "assistant", "中断内容", "streaming")
    recovered_store = SessionStore(data / "sessions.db")
    recovered = recovered_store.list_messages(session.sessionKey)
    assert recovered[0].status == "failed"
