import asyncio
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.memory_service import MemoryService, MemoryWorker
from backend.app.memory_store import MemoryStore
from backend.app.models import Message, RoleInput, RoleProfile
from backend.app.role_store import RoleStore
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


class ContextAdapter:
    def __init__(self):
        self.contexts = []

    async def stream_reply(self, role, history, configuration=None, memory_context=""):
        self.contexts.append(memory_context)
        yield "收到"


def test_memory_store_is_role_scoped_and_source_idempotent(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store)
    first = service.remember("role-a", "喜欢红茶", "preference", stable_source_key="turn-1", supersede_key="preference")
    repeated = service.remember("role-a", "喜欢红茶", "preference", stable_source_key="turn-1", supersede_key="preference")
    other = service.remember("role-b", "喜欢红茶", "preference", stable_source_key="turn-1", supersede_key="preference")

    assert first.id == repeated.id
    assert repeated.reinforcement == 1
    assert other.id != first.id
    assert [item.id for item in service.recall("role-a", "红茶")] == [first.id]
    assert service.recall("role-b", "红茶")[0].id == other.id


def test_memory_store_new_preference_supersedes_old(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store)
    old = service.remember("role-a", "喜欢红茶", "preference", stable_source_key="turn-1", supersede_key="preference")
    new = service.remember("role-a", "喜欢咖啡", "preference", stable_source_key="turn-2", supersede_key="preference")

    assert new.id != old.id
    assert [item.summary for item in store.list_active("role-a")] == ["喜欢咖啡"]
    assert store.get("role-a", old.id).status == "superseded"


def test_unrelated_preference_does_not_supersede_without_explicit_change(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store)
    old = service.process_turn(
        "role-a",
        "role:role-a",
        Message(id="m1", sessionKey="role:role-a", sequence=1, role="user", content="我喜欢红茶", status="completed", createdAt=datetime.now(timezone.utc)),
        Message(id="m2", sessionKey="role:role-a", sequence=2, role="assistant", content="好", status="completed", createdAt=datetime.now(timezone.utc)),
    )[0]
    new = service.process_turn(
        "role-a",
        "role:role-a",
        Message(id="m3", sessionKey="role:role-a", sequence=3, role="user", content="我喜欢咖啡", status="completed", createdAt=datetime.now(timezone.utc)),
        Message(id="m4", sessionKey="role:role-a", sequence=4, role="assistant", content="好", status="completed", createdAt=datetime.now(timezone.utc)),
    )[0]
    assert old.id != new.id
    assert {item.summary for item in store.list_active("role-a")} == {"喜欢红茶", "喜欢咖啡"}


def test_same_source_with_changed_extraction_is_idempotent(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store)
    first = service.remember("role-a", "主人住在海边", "fact", stable_source_key="turn-1")
    second = service.remember("role-a", "主人住在海边附近", "fact", stable_source_key="turn-1")
    assert second.id == first.id
    assert len(store.list_active("role-a")) == 1
    assert store.get("role-a", first.id).sourceRef.sourceKeys == ["turn-1"]


def test_memory_worker_extracts_explicit_and_implicit_memory_without_blocking(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store)
    worker = MemoryWorker(service)
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    session_store = SessionStore(tmp_path / "data" / "sessions.db")
    session = session_store.open_role_session(role.id)
    user = session_store.append_message(session.sessionKey, "user", "请记住我喜欢乌龙茶")
    assistant = session_store.append_message(session.sessionKey, "assistant", "好的")

    async def run_worker() -> None:
        worker.submit(role.id, session.sessionKey, user, assistant)
        await worker.drain()

    asyncio.run(run_worker())
    memories = store.list_active(role.id)
    assert len(memories) == 1
    assert memories[0].summary == "喜欢乌龙茶"
    assert memories[0].sourceRef.messageIds == [user.id, assistant.id]
    assert memories[0].sourceRef.kind == "turn"


def test_memory_api_is_scoped_and_manual_memory_is_recalled(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    first = roles.create(RoleInput(name="甲", profile=RoleProfile(profile="设定")))
    second = roles.create(RoleInput(name="乙", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "memory_store", MemoryStore(tmp_path / "data" / "memory.db"))
    monkeypatch.setattr(main, "memory_service", MemoryService(main.memory_store))
    monkeypatch.setattr(main, "memory_worker", MemoryWorker(main.memory_service))
    client = TestClient(main.app)

    created = client.post(f"/api/roles/{first.id}/memories", json={"summary": "主人住在海边", "memoryType": "fact"})
    assert created.status_code == 201
    assert client.get(f"/api/roles/{first.id}/memories", params={"q": "海边"}).json()["memories"][0]["summary"] == "主人住在海边"
    assert client.get(f"/api/roles/{second.id}/memories", params={"q": "海边"}).json()["memories"] == []


def test_empty_recall_does_not_inject_all_memories(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store)
    service.remember("role-a", "主人住在海边", "fact", stable_source_key="one")
    assert service.recall("role-a", "") == []


def test_delete_role_cleans_structured_memory(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="甲", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    memory_store = MemoryStore(tmp_path / "data" / "memory.db")
    memory_service = MemoryService(memory_store)
    memory_service.remember(role.id, "主人住在海边", "fact", stable_source_key="one")
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "memory_store", memory_store)
    monkeypatch.setattr(main, "memory_service", memory_service)
    monkeypatch.setattr(main, "role_locks", {})
    response = TestClient(main.app).delete(f"/api/roles/{role.id}")
    assert response.status_code == 204
    assert memory_store.list_active(role.id) == []


def test_recalled_memory_is_injected_only_for_related_current_role(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    memory_store = MemoryStore(tmp_path / "data" / "memory.db")
    memory_service = MemoryService(memory_store)
    memory_service.remember(role.id, "主人喜欢海边散步", "preference", stable_source_key="manual:walk")
    adapter = ContextAdapter()
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", main.SessionManager(roles, sessions))
    monkeypatch.setattr(main, "memory_store", memory_store)
    monkeypatch.setattr(main, "memory_service", memory_service)
    monkeypatch.setattr(main, "memory_worker", MemoryWorker(memory_service))
    monkeypatch.setattr(main, "model_adapter", adapter)
    client = TestClient(main.app)

    response = client.post(f"/api/roles/{role.id}/messages", json={"content": "我们去海边吧"})
    assert response.status_code == 200
    assert "海边散步" in adapter.contexts[0]
