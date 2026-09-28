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


class RecordingAdapter:
    def __init__(self):
        self.profiles = []

    async def stream_reply(self, role, history):
        self.profiles.append(role.profile.profile)
        yield "回复"


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


def test_role_update_preserves_session_messages_and_changes_future_prompt(tmp_path, monkeypatch):
    adapter = RecordingAdapter()
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, adapter)
    session_key = f"role:{role.id}"
    first = client.post(f"/api/roles/{role.id}/messages", json={"content": "第一条"})
    assert "event: assistant_completed" in first.text
    messages_before_update = sessions.list_messages(session_key)

    updated = client.put(
        f"/api/roles/{role.id}",
        json={
            "name": "改名",
            "description": "新简介",
            "profile": {
                "profile": "新设定",
                "personality": "活泼",
                "behaviorRules": "先倾听",
                "responseConstraints": "简洁",
            },
        },
    )
    assert updated.status_code == 200
    assert updated.json()["role"]["id"] == role.id
    assert RoleStore(tmp_path / "roles").get(role.id).profile.profile == "新设定"
    assert [message.model_dump() for message in sessions.list_messages(session_key)] == [
        message.model_dump() for message in messages_before_update
    ]

    second = client.post(f"/api/roles/{role.id}/messages", json={"content": "第二条"})
    assert "event: assistant_completed" in second.text
    assert adapter.profiles == ["核心设定", "新设定"]
    assert client.get(f"/api/roles/{role.id}/session").json()["session"]["sessionKey"] == session_key


def test_role_update_failure_keeps_persisted_role_unchanged(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="原角色", profile=RoleProfile(profile="原设定")))
    manifest_before = (tmp_path / "roles" / "roles.json").read_bytes()
    monkeypatch.setattr(main, "store", roles)

    def fail_save(candidate=None):
        raise OSError("磁盘写入失败")

    monkeypatch.setattr(roles, "_save", fail_save)
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.put(
        f"/api/roles/{role.id}",
        json={"name": "新角色", "profile": {"profile": "新设定"}},
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "角色保存失败，请稍后重试"
    assert roles.get(role.id).name == "原角色"
    assert (tmp_path / "roles" / "roles.json").read_bytes() == manifest_before


def test_delete_role_removes_role_session_messages_and_files(tmp_path, monkeypatch):
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    other = main.store.create(RoleInput(name="其他角色", profile=RoleProfile(profile="其他设定")))
    client.post(f"/api/roles/{role.id}/messages", json={"content": "待删除消息"})
    client.get(f"/api/roles/{other.id}/session")

    response = client.delete(f"/api/roles/{role.id}")

    assert response.status_code == 204
    assert client.get(f"/api/roles/{role.id}").status_code == 404
    assert client.get(f"/api/roles/{role.id}/session").status_code == 404
    assert sessions.list_messages(f"role:{role.id}") == []
    assert not (tmp_path / "roles" / role.id).exists()
    assert client.get(f"/api/roles/{other.id}").status_code == 200
    assert client.get(f"/api/roles/{other.id}/session").status_code == 200
    assert all(item.id != role.id for item in main.store.list())


def test_delete_role_failure_restores_role_and_chat_history(tmp_path, monkeypatch):
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    client.post(f"/api/roles/{role.id}/messages", json={"content": "保留消息"})
    before = sessions.list_messages(f"role:{role.id}")

    def fail_delete(role_id):
        raise OSError("数据库删除失败")

    monkeypatch.setattr(sessions, "delete_role_session", fail_delete)
    response = client.delete(f"/api/roles/{role.id}")

    assert response.status_code == 500
    assert response.json()["detail"] == "删除失败，角色和聊天记录已保留"
    assert client.get(f"/api/roles/{role.id}").status_code == 200
    assert [item.model_dump() for item in sessions.list_messages(f"role:{role.id}")] == [item.model_dump() for item in before]


def test_delete_role_rejects_while_generation_is_in_progress(tmp_path, monkeypatch):
    role, _, client = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())

    class LockedRole:
        def locked(self):
            return True

    monkeypatch.setitem(main.role_locks, role.id, LockedRole())
    response = client.delete(f"/api/roles/{role.id}")
    assert response.status_code == 409
    assert client.get(f"/api/roles/{role.id}").status_code == 200


def test_role_snapshot_is_stable_after_update_and_new_lookup_uses_latest(tmp_path, monkeypatch):
    role, _, _ = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    snapshot, _ = main.session_manager.role_and_history(role.id)
    response = TestClient(main.app).put(
        f"/api/roles/{role.id}",
        json={"name": role.name, "profile": {"profile": "新设定"}},
    )

    assert response.status_code == 200
    assert snapshot.profile.profile == "核心设定"
    current, _ = main.session_manager.role_and_history(role.id)
    assert current.profile.profile == "新设定"


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
