import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.memory_service import MemoryService, MemoryWorker
from backend.app.memory_maintenance import MemoryMaintenance
from backend.app import memory_maintenance
from backend.app.embeddings import OpenAICompatibleEmbeddingAdapter
from backend.app.memory_store import MemoryStore
from backend.app.models import MemorySourceRef, Message, RoleInput, RoleProfile
from backend.app.role_store import RoleStore
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


class ContextAdapter:
    def __init__(self):
        self.contexts = []

    async def stream_reply(self, role, history, configuration=None, memory_context=""):
        self.contexts.append(memory_context)
        yield "收到"


class FakeEmbeddingProvider:
    def __init__(self, vectors):
        self.vectors = vectors

    def embed(self, role_id, text):
        return self.vectors[(role_id, text)]


def test_hybrid_recall_finds_semantic_match_without_shared_words_and_is_role_scoped(tmp_path):
    vectors = {
        ("role-a", "主人喜欢海边散步"): [1.0, 0.0],
        ("role-a", "适合去哪里放松"): [0.98, 0.02],
        ("role-b", "主人喜欢海边散步"): [0.0, 1.0],
    }
    provider = FakeEmbeddingProvider(vectors)
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store, provider)
    first = service.remember("role-a", "主人喜欢海边散步", "preference", stable_source_key="a")
    service.remember("role-b", "主人喜欢海边散步", "preference", stable_source_key="b")

    recalled = service.recall("role-a", "适合去哪里放松")

    assert [item.id for item in recalled] == [first.id]
    assert service.recall("role-b", "适合去哪里放松") == []


def test_structured_consolidation_batch_rolls_back_as_one_transaction(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "memory.db")
    original = store._add_or_reinforce_connection
    calls = 0

    def fail_second(connection, role_id, memory_type, summary, source_ref, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated batch failure")
        return original(connection, role_id, memory_type, summary, source_ref, **kwargs)

    monkeypatch.setattr(store, "_add_or_reinforce_connection", fail_second)
    with pytest.raises(RuntimeError, match="batch failure"):
        store.add_or_reinforce_batch(
            "role-a",
            [
                ("fact", "第一条", MemorySourceRef(kind="consolidation", sessionKey="role:role-a", stableSourceKey="source-1")),
                ("fact", "第二条", MemorySourceRef(kind="consolidation", sessionKey="role:role-a", stableSourceKey="source-2")),
            ],
        )
    assert store.list_active("role-a") == []


def test_consolidation_supersedes_extracted_items_covered_by_message_window(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    extracted = store.add_or_reinforce(
        "role-a",
        "preference",
        "主人喜欢海边散步",
        MemorySourceRef(
            kind="turn",
            sessionKey="role:role-a",
            messageIds=["user-1", "assistant-1"],
            stableSourceKey="turn:user-1:assistant-1",
        ),
    )
    consolidated = store.consolidate_batch(
        "role-a",
        [
            (
                "preference",
                "主人喜欢海边散步并欣赏海风",
                MemorySourceRef(
                    kind="consolidation",
                    sessionKey="role:role-a",
                    messageIds=["user-1"],
                    messageRange=(1, 2),
                    stableSourceKey="consolidation:role-a:1-2:digest:user-1:preference",
                ),
            )
        ],
    )
    assert store.get("role-a", extracted.id).status == "superseded"
    assert consolidated[0].status == "active"
    assert consolidated[0].sourceRef.messageIds == ["user-1"]


def test_hybrid_recall_fuses_keyword_and_semantic_rankings(tmp_path):
    provider = FakeEmbeddingProvider({
        ("role-a", "喜欢红茶"): [1.0, 0.0],
        ("role-a", "红茶"): [0.0, 1.0],
    })
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store, provider)
    semantic = service.remember("role-a", "喜欢红茶", "preference", stable_source_key="semantic")
    keyword = service.remember("role-a", "红茶", "fact", stable_source_key="keyword")

    result = service.recall("role-a", "红茶")

    assert {item.id for item in result} == {semantic.id, keyword.id}


def test_embedding_adapter_uses_configured_openai_compatible_endpoint(monkeypatch):
    from backend.app.models import ModelConfiguration

    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"embedding": [0.1, 0.2]}]}

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("backend.app.embeddings.httpx.post", post)
    config = ModelConfiguration(providerId="custom", provider="custom", baseUrl="https://model.test/v1", model="embed-model", apiKey="secret")
    adapter = OpenAICompatibleEmbeddingAdapter(lambda role_id: config)

    assert adapter.embed("role-a", "query") == [0.1, 0.2]
    assert calls[0][0] == "https://model.test/v1/embeddings"
    assert calls[0][1]["json"] == {"model": "embed-model", "input": "query"}


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


def test_matching_memory_keeps_each_source_for_traceability(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store)
    first = service.remember(
        "role-a", "主人喜欢海边散步", "preference", session_key="role:role-a",
        message_ids=["message-1"], stable_source_key="turn-1",
    )
    reinforced = service.remember(
        "role-a", "主人喜欢海边散步", "preference", session_key="role:role-a",
        message_ids=["message-8"], stable_source_key="turn-2",
    )
    assert reinforced.id == first.id
    assert [source.messageIds for source in reinforced.sourceRef.sources] == [["message-1"], ["message-8"]]


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


def test_memory_maintenance_updates_recent_context_and_is_idempotent(tmp_path):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    session = sessions.open_role_session(role.id)
    user = sessions.append_message(session.sessionKey, "user", "请记住我喜欢乌龙茶")
    assistant = sessions.append_message(session.sessionKey, "assistant", "好的")
    maintenance = MemoryMaintenance(tmp_path / "roles", sessions)

    maintenance.maintain(role.id, session.sessionKey)
    recent_path = tmp_path / "roles" / role.id / "memory" / "RECENT_CONTEXT.md"
    assert "喜欢乌龙茶" in recent_path.read_text(encoding="utf-8")
    first = recent_path.read_text(encoding="utf-8")
    maintenance.maintain(role.id, session.sessionKey)
    assert recent_path.read_text(encoding="utf-8") == first


def test_memory_maintenance_consolidates_once_and_keeps_source_window(tmp_path):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    session = sessions.open_role_session(role.id)
    for index in range(10):
        sessions.append_message(session.sessionKey, "user", f"请记住第{index}项")
        sessions.append_message(session.sessionKey, "assistant", "收到")
    maintenance = MemoryMaintenance(tmp_path / "roles", sessions)
    maintenance.maintain(role.id, session.sessionKey)
    history_path = tmp_path / "roles" / role.id / "memory" / "HISTORY.md"
    pending_path = tmp_path / "roles" / role.id / "memory" / "PENDING.md"
    history = history_path.read_text(encoding="utf-8")
    pending = pending_path.read_text(encoding="utf-8")
    maintenance.maintain(role.id, session.sessionKey)
    assert history_path.read_text(encoding="utf-8") == history
    assert pending_path.read_text(encoding="utf-8") == pending
    assert history.count("<!-- source:") == 1
    assert "consolidation:" in history
    assert "第9项" in pending


def test_memory_worker_serially_runs_markdown_maintenance(tmp_path):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    session = sessions.open_role_session(role.id)
    user = sessions.append_message(session.sessionKey, "user", "请记住我喜欢红茶")
    assistant = sessions.append_message(session.sessionKey, "assistant", "好的")
    store = MemoryStore(tmp_path / "data" / "memory.db")
    service = MemoryService(store)
    worker = MemoryWorker(service, MemoryMaintenance(tmp_path / "roles", sessions))

    async def run_worker() -> None:
        worker.submit(role.id, session.sessionKey, user, assistant)
        await worker.drain()

    asyncio.run(run_worker())
    assert (tmp_path / "roles" / role.id / "memory" / "RECENT_CONTEXT.md").exists()


def test_memory_maintenance_discards_stale_snapshot(tmp_path):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    session = sessions.open_role_session(role.id)
    sessions.append_message(session.sessionKey, "user", "第一条")
    sessions.append_message(session.sessionKey, "assistant", "回复一")

    class MessageDuringPreparation(MemoryMaintenance):
        def __init__(self, root, session_store):
            super().__init__(root, session_store)
            self.injected = False

        def _before_commit(self):
            if not self.injected:
                self.injected = True
                sessions.append_message(session.sessionKey, "user", "新消息")
                sessions.append_message(session.sessionKey, "assistant", "回复二")

    maintenance = MessageDuringPreparation(tmp_path / "roles", sessions)
    recent_path = tmp_path / "roles" / role.id / "memory" / "RECENT_CONTEXT.md"
    maintenance.maintain(role.id, session.sessionKey)
    assert not recent_path.exists()
    maintenance.maintain(role.id, session.sessionKey)
    assert "新消息" in recent_path.read_text(encoding="utf-8")


def test_memory_maintenance_restores_files_when_commit_fails(tmp_path, monkeypatch):
    recent_path = tmp_path / "memory" / "RECENT_CONTEXT.md"
    history_path = tmp_path / "memory" / "HISTORY.md"
    cursor_path = tmp_path / "memory" / ".maintenance.json"
    recent_path.parent.mkdir()
    recent_path.write_text("旧近期\n", encoding="utf-8")
    history_path.write_text("旧历史\n", encoding="utf-8")
    cursor_path.write_text('{"lastSequence": 2}\n', encoding="utf-8")
    original_replace = memory_maintenance.os.replace
    replacements = 0

    def fail_second_replace(source, target):
        nonlocal replacements
        replacements += 1
        if replacements == 2:
            raise OSError("simulated write failure")
        return original_replace(source, target)

    monkeypatch.setattr(memory_maintenance.os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="simulated write failure"):
        MemoryMaintenance._commit(
            {recent_path: "新近期\n", history_path: "新历史\n"},
            cursor_path,
            {"lastSequence": 20},
        )
    assert recent_path.read_text(encoding="utf-8") == "旧近期\n"
    assert history_path.read_text(encoding="utf-8") == "旧历史\n"
    assert cursor_path.read_text(encoding="utf-8") == '{"lastSequence": 2}\n'


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


def test_memory_management_updates_forgets_and_deletes_only_current_role(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="甲", profile=RoleProfile(profile="设定")))
    other = roles.create(RoleInput(name="乙", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    memory_store = MemoryStore(tmp_path / "data" / "memory.db")
    service = MemoryService(memory_store)
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "memory_store", memory_store)
    monkeypatch.setattr(main, "memory_service", service)
    client = TestClient(main.app)
    created = client.post(f"/api/roles/{role.id}/memories", json={"summary": "喜欢红茶", "memoryType": "preference"}).json()
    memory_id = created["id"]
    updated = client.put(f"/api/roles/{role.id}/memories/{memory_id}", json={"summary": "喜欢咖啡", "memoryType": "preference"})
    assert updated.status_code == 200
    forgotten = client.post(f"/api/roles/{role.id}/memories/{memory_id}/forget")
    assert forgotten.status_code == 200
    assert client.get(f"/api/roles/{role.id}/memories").json()["memories"] == []
    assert client.delete(f"/api/roles/{role.id}/memories/{memory_id}").status_code == 204
    assert client.post(f"/api/roles/{other.id}/memories/{memory_id}/forget").status_code == 404


def test_memory_api_lists_active_items_and_searches_with_all_sources(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    memory_store = MemoryStore(tmp_path / "data" / "memory.db")
    service = MemoryService(memory_store)
    service.remember(role.id, "喜欢海边散步", "preference", message_ids=["msg-a"], stable_source_key="source-a")
    service.remember(role.id, "喜欢海边散步", "preference", message_ids=["msg-b"], stable_source_key="source-b")
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "memory_store", memory_store)
    monkeypatch.setattr(main, "memory_service", service)
    client = TestClient(main.app)

    response = client.get(f"/api/roles/{role.id}/memories", params={"q": "海边"})
    memories = response.json()["memories"]
    assert response.status_code == 200
    assert len(memories) == 1
    assert memories[0]["status"] == "active"
    assert [origin["messageIds"] for origin in memories[0]["sourceRef"]["sources"]] == [["msg-a"], ["msg-b"]]


def test_memory_api_hides_inactive_items_and_other_role_sources(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="甲", profile=RoleProfile(profile="设定")))
    other_role = roles.create(RoleInput(name="乙", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    memory_store = MemoryStore(tmp_path / "data" / "memory.db")
    service = MemoryService(memory_store)
    active = service.remember(role.id, "主人喜欢海边", "preference", message_ids=["private-msg"], stable_source_key="active")
    superseded = service.remember(role.id, "主人住在海边", "fact", stable_source_key="superseded")
    forgotten = service.remember(role.id, "主人常去海边", "fact", stable_source_key="forgotten")
    service.remember(other_role.id, "主人喜欢海边", "preference", message_ids=["other-role-msg"], stable_source_key="other")
    with memory_store._connect() as connection:
        connection.execute("UPDATE memory_items SET status = 'superseded' WHERE id = ?", (superseded.id,))
        connection.execute("UPDATE memory_items SET status = 'forgotten' WHERE id = ?", (forgotten.id,))
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "memory_store", memory_store)
    monkeypatch.setattr(main, "memory_service", service)
    client = TestClient(main.app)

    visible = client.get(f"/api/roles/{role.id}/memories").json()["memories"]
    search = client.get(f"/api/roles/{role.id}/memories", params={"q": "海边"}).json()["memories"]
    other_role_search = client.get(f"/api/roles/{other_role.id}/memories", params={"q": "private-msg"}).json()["memories"]
    no_results = client.get(f"/api/roles/{role.id}/memories", params={"q": "不存在的词"}).json()["memories"]

    assert [item["id"] for item in visible] == [active.id]
    assert [item["sourceRef"]["sources"][0]["messageIds"] for item in search] == [["private-msg"]]
    assert all("private-msg" not in str(item["sourceRef"]) for item in other_role_search)
    assert no_results == []


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


def test_fixed_markdown_memory_is_injected_with_recalled_memory(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    memory_dir = tmp_path / "roles" / role.id / "memory"
    memory_dir.mkdir(exist_ok=True)
    (memory_dir / "SELF.md").write_text("角色认识主人", encoding="utf-8")
    (memory_dir / "MEMORY.md").write_text("主人喜欢清晨散步", encoding="utf-8")
    (memory_dir / "RECENT_CONTEXT.md").write_text("最近在讨论旅行", encoding="utf-8")
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    memory_store = MemoryStore(tmp_path / "data" / "memory.db")
    memory_service = MemoryService(memory_store)
    adapter = ContextAdapter()
    monkeypatch.setattr(main, "roles_root", tmp_path / "roles")
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", main.SessionManager(roles, sessions))
    monkeypatch.setattr(main, "memory_store", memory_store)
    monkeypatch.setattr(main, "memory_service", memory_service)
    monkeypatch.setattr(main, "memory_worker", MemoryWorker(memory_service))
    monkeypatch.setattr(main, "model_adapter", adapter)
    response = TestClient(main.app).post(f"/api/roles/{role.id}/messages", json={"content": "我们去哪里旅行"})
    assert response.status_code == 200
    assert "角色认识主人" in adapter.contexts[0]
    assert "主人喜欢清晨散步" in adapter.contexts[0]
    assert "最近在讨论旅行" in adapter.contexts[0]
