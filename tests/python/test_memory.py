import asyncio
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.memory_service import MemoryService, MemoryWorker
from backend.app.memory_maintenance import MemoryMaintenance
from backend.app import memory_maintenance
from backend.app.embeddings import OpenAICompatibleEmbeddingAdapter
from backend.app.memory_store import MemoryStore
from backend.app.memory_events import MemoryEventBus, MemoryWritten
from backend.app.models import MemorySourceRef, Message, RoleInput, RoleProfile
from backend.app.role_store import RoleStore
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


def test_memory_worker_blocks_new_tasks_during_role_deletion(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store)
    worker = MemoryWorker(service)
    user = Message(
        id="user-1",
        sessionKey="role:role-a",
        sequence=1,
        role="user",
        content="请记住我喜欢海边",
        status="completed",
        createdAt=datetime.now(timezone.utc),
    )
    assistant = user.model_copy(update={"id": "assistant-1", "sequence": 2, "role": "assistant", "content": "好的"})

    async def run() -> None:
        await worker.begin_role_deletion("role-a")
        worker.submit("role-a", "role:role-a", user, assistant)
        await worker.drain()

    asyncio.run(run())
    assert store.list_active("role-a") == []


def test_memory_worker_accepts_tasks_after_role_deletion_finishes(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store)
    worker = MemoryWorker(service)
    user = Message(
        id="user-1",
        sessionKey="role:role-a",
        sequence=1,
        role="user",
        content="请记住我喜欢海边",
        status="completed",
        createdAt=datetime.now(timezone.utc),
    )
    assistant = user.model_copy(update={"id": "assistant-1", "sequence": 2, "role": "assistant", "content": "好的"})

    async def run() -> None:
        await worker.begin_role_deletion("role-a")
        worker.end_role_deletion("role-a")
        worker.submit("role-a", "role:role-a", user, assistant)
        await worker.drain()

    asyncio.run(run())
    assert [item.summary for item in store.list_active("role-a")] == ["喜欢海边"]


def test_memory_worker_close_drains_and_rejects_new_tasks(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store)
    worker = MemoryWorker(service)
    user = Message(
        id="user-1",
        sessionKey="role:role-a",
        sequence=1,
        role="user",
        content="请记住我喜欢海边",
        status="completed",
        createdAt=datetime.now(timezone.utc),
    )
    assistant = user.model_copy(update={"id": "assistant-1", "sequence": 2, "role": "assistant", "content": "好的"})

    async def run() -> None:
        assert worker.submit("role-a", "role:role-a", user, assistant) is True
        await worker.close()
        assert worker.submit("role-a", "role:role-a", user, assistant) is False

    asyncio.run(run())
    assert [item.summary for item in store.list_active("role-a")] == ["喜欢海边"]


def test_memory_worker_ignores_uncommitted_assistant_replies(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store)
    worker = MemoryWorker(service)
    user = Message(
        id="user-1",
        sessionKey="role:role-a",
        sequence=1,
        role="user",
        content="请记住我喜欢海边",
        status="completed",
        createdAt=datetime.now(timezone.utc),
    )
    assistant = user.model_copy(update={"id": "assistant-1", "sequence": 2, "role": "assistant", "content": "", "status": "failed"})

    async def run() -> None:
        assert worker.submit("role-a", "role:role-a", user, assistant) is False
        await worker.drain()

    asyncio.run(run())
    assert store.list_all("role-a") == []


def test_memory_worker_runs_maintenance_when_semantic_processing_fails(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")

    class FailingService(MemoryService):
        def process_turn(self, *args, **kwargs):
            raise RuntimeError("semantic failure")

    class RecordingMaintenance:
        def __init__(self):
            self.calls = 0

        def maintain(self, role_id, session_key):
            self.calls += 1

    maintenance = RecordingMaintenance()
    worker = MemoryWorker(FailingService(store), maintenance)  # type: ignore[arg-type]
    user = Message(id="u", sessionKey="role:role-a", sequence=1, role="user", content="你好", status="completed", createdAt=datetime.now(timezone.utc))
    assistant = user.model_copy(update={"id": "a", "sequence": 2, "role": "assistant", "content": "你好", "status": "completed"})

    async def run() -> None:
        assert worker.submit("role-a", "role:role-a", user, assistant) is True
        await worker.drain()

    asyncio.run(run())
    assert maintenance.calls == 1
    assert any("semantic" in error for error in worker.errors)


def test_memory_worker_runs_semantic_processing_when_maintenance_fails(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")

    class FailingMaintenance:
        def maintain(self, role_id, session_key):
            raise RuntimeError("maintenance failure")

    worker = MemoryWorker(MemoryService(store), FailingMaintenance())  # type: ignore[arg-type]
    user = Message(id="u", sessionKey="role:role-a", sequence=1, role="user", content="请记住我喜欢海边", status="completed", createdAt=datetime.now(timezone.utc))
    assistant = user.model_copy(update={"id": "a", "sequence": 2, "role": "assistant", "content": "好的", "status": "completed"})

    async def run() -> None:
        assert worker.submit("role-a", "role:role-a", user, assistant) is True
        await worker.drain()

    asyncio.run(run())
    assert [item.summary for item in store.list_active("role-a")] == ["喜欢海边"]
    assert any("maintenance" in error for error in worker.errors)


def test_fastapi_shutdown_closes_memory_worker(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    memory_store = MemoryStore(tmp_path / "data" / "memory.db")
    service = MemoryService(memory_store)
    worker = MemoryWorker(service)
    user = Message(
        id="user-1",
        sessionKey=f"role:{role.id}",
        sequence=1,
        role="user",
        content="请记住我喜欢海边",
        status="completed",
        createdAt=datetime.now(timezone.utc),
    )
    assistant = user.model_copy(update={"id": "assistant-1", "sequence": 2, "role": "assistant", "content": "好的"})
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "memory_store", memory_store)
    monkeypatch.setattr(main, "memory_service", service)
    monkeypatch.setattr(main, "memory_worker", worker)
    async def run() -> None:
        assert worker.submit(role.id, f"role:{role.id}", user, assistant) is True
        for callback in main.app.router.on_shutdown:
            await callback()

    asyncio.run(run())

    assert [item.summary for item in memory_store.list_active(role.id)] == ["喜欢海边"]
    assert worker.closed is True


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
    assert memories[0].memoryType == "preference"
    assert memories[0].sourceRef.messageIds == [user.id, assistant.id]
    assert memories[0].sourceRef.kind == "turn"


def test_post_response_marks_explicit_memory_as_protected(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store)
    user = Message(
        id="explicit-user",
        sessionKey="role:role-a",
        sequence=1,
        role="user",
        content="请记住我喜欢乌龙茶",
        status="completed",
        createdAt=datetime.now(timezone.utc),
    )
    assistant = user.model_copy(update={"id": "explicit-assistant", "sequence": 2, "role": "assistant", "content": "好的"})

    saved = service.process_turn("role-a", "role:role-a", user, assistant)

    assert len(saved) == 1
    assert saved[0].memoryType == "preference"
    assert saved[0].extra["explicit"] is True
    assert saved[0].extra["protectedTurn"] == "turn:role:role-a:explicit-user:explicit-assistant"


def test_replaying_a_committed_turn_is_idempotent(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store)
    user = Message(id="replay-user", sessionKey="role:role-a", sequence=1, role="user", content="请记住我喜欢乌龙茶", status="completed", createdAt=datetime.now(timezone.utc))
    assistant = user.model_copy(update={"id": "replay-assistant", "sequence": 2, "role": "assistant", "content": "好的"})

    first = service.process_turn("role-a", "role:role-a", user, assistant)
    second = service.process_turn("role-a", "role:role-a", user, assistant)

    assert [item.id for item in second] == [first[0].id]
    assert store.get("role-a", first[0].id).reinforcement == 1


def test_post_response_extracts_events_and_corrections(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store)
    event_user = Message(id="event-user", sessionKey="role:role-a", sequence=1, role="user", content="昨天我去了海边", status="completed", createdAt=datetime.now(timezone.utc))
    event_assistant = event_user.model_copy(update={"id": "event-assistant", "sequence": 2, "role": "assistant", "content": "听起来不错"})
    event = service.process_turn("role-a", "role:role-a", event_user, event_assistant)
    assert [(item.memoryType, item.summary) for item in event] == [("event", "昨天我去了海边")]

    first_user = event_user.model_copy(update={"id": "pref-user-1", "sequence": 3, "content": "我喜欢红茶"})
    first_assistant = event_assistant.model_copy(update={"id": "pref-assistant-1", "sequence": 4})
    service.process_turn("role-a", "role:role-a", first_user, first_assistant)
    correction_user = event_user.model_copy(update={"id": "pref-user-2", "sequence": 5, "content": "我以前喜欢红茶，现在喜欢咖啡"})
    correction_assistant = event_assistant.model_copy(update={"id": "pref-assistant-2", "sequence": 6})
    service.process_turn("role-a", "role:role-a", correction_user, correction_assistant)

    active = store.list_active("role-a")
    assert {item.summary for item in active} >= {"喜欢咖啡"}
    assert "喜欢红茶" not in {item.summary for item in active}


def test_turn_forgetting_memory_keeps_message_evidence_and_is_role_scoped(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store)
    target = service.remember("role-a", "喜欢红茶", "preference", stable_source_key="saved")
    other = service.remember("role-b", "喜欢红茶", "preference", stable_source_key="saved-other")
    user = Message(
        id="forget-message",
        sessionKey="role:role-a",
        sequence=3,
        role="user",
        content="请忘记我喜欢红茶",
        status="completed",
        createdAt=datetime.now(timezone.utc),
    )
    assistant = user.model_copy(update={"id": "forget-reply", "sequence": 4, "role": "assistant", "content": "好的"})

    service.process_turn("role-a", "role:role-a", user, assistant)
    service.process_turn("role-a", "role:role-a", user, assistant)

    assert store.get("role-a", target.id).status == "forgotten"
    assert store.get("role-b", other.id).status == "active"
    assert store.query("role-a", "红茶") == []


def test_worker_forgetting_turn_removes_memory_markdown_view(tmp_path):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    session = sessions.open_role_session(role.id)
    store = MemoryStore(tmp_path / "data" / "memory.db")
    service = MemoryService(store)
    maintenance = MemoryMaintenance(tmp_path / "roles", sessions)
    target = service.remember(role.id, "喜欢红茶", "preference", stable_source_key="saved")
    maintenance.sync_structured_memory(role.id, store.list_active(role.id))
    user = sessions.append_message(session.sessionKey, "user", "请忘记我喜欢红茶")
    assistant = sessions.append_message(session.sessionKey, "assistant", "好的")
    worker = MemoryWorker(service, maintenance)

    async def run() -> None:
        worker.submit(role.id, session.sessionKey, user, assistant)
        await worker.drain()

    asyncio.run(run())
    assert store.get(role.id, target.id).status == "forgotten"
    assert "喜欢红茶" not in (tmp_path / "roles" / role.id / "memory" / "MEMORY.md").read_text(encoding="utf-8")


def test_rejected_unstructured_candidate_is_removed_from_pending_markdown(tmp_path):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    store = MemoryStore(tmp_path / "data" / "memory.db")
    service = MemoryService(store)
    documents = MemoryMaintenance(tmp_path / "roles", SessionStore(tmp_path / "data" / "sessions.db"))
    memory_dir = tmp_path / "roles" / role.id / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "PENDING.md").write_text("- [preference] 喜欢红茶（来源：消息 1）\n  <!-- source: consolidation-key:message-1:preference -->\n", encoding="utf-8")
    source = MemorySourceRef(kind="turn", sessionKey=f"role:{role.id}", messageIds=["reject-1"], stableSourceKey="reject-turn")

    service.reject_matching(role.id, "喜欢红茶", source)
    documents.sync_structured_memory(role.id, store.list_all(role.id))

    assert "喜欢红茶" not in (memory_dir / "PENDING.md").read_text(encoding="utf-8")


def test_turn_rejection_is_persisted_without_becoming_recallable(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store)
    user = Message(
        id="reject-message",
        sessionKey="role:role-a",
        sequence=1,
        role="user",
        content="请不要记住我喜欢红茶",
        status="completed",
        createdAt=datetime.now(timezone.utc),
    )
    assistant = user.model_copy(update={"id": "reject-reply", "sequence": 2, "role": "assistant", "content": "好的"})

    rejected = service.process_turn("role-a", "role:role-a", user, assistant)

    assert len(rejected) == 1
    assert rejected[0].status == "rejected"
    assert store.query("role-a", "红茶") == []


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


def test_recent_context_respects_its_total_document_budget(tmp_path):
    maintenance = MemoryMaintenance(tmp_path / "roles", SessionStore.__new__(SessionStore))
    messages = [
        Message(
            id=f"m{index}",
            sessionKey="role:role-a",
            sequence=index,
            role="user",
            content="近期内容" * 500,
            status="completed",
            createdAt=datetime.now(timezone.utc),
        )
        for index in range(1, 4)
    ]

    assert len(maintenance._recent_context(messages)) <= maintenance.RECENT_CHAR_LIMIT


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
    assert "第3项" in pending
    recent_path = tmp_path / "roles" / role.id / "memory" / "RECENT_CONTEXT.md"
    assert "第9项" in recent_path.read_text(encoding="utf-8")


def test_memory_maintenance_writes_idempotent_journal_and_documents_endpoint(tmp_path, monkeypatch):
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
    maintenance.maintain(role.id, session.sessionKey)
    journal_files = list((tmp_path / "roles" / role.id / "memory" / "journal").glob("*.md"))
    assert len(journal_files) == 1
    journal = journal_files[0].read_text(encoding="utf-8")
    assert journal.count("<!-- source:") == 1

    monkeypatch.setattr(main, "roles_root", tmp_path / "roles")
    monkeypatch.setattr(main, "store", roles)
    response = TestClient(main.app).get(f"/api/roles/{role.id}/memory-documents")
    assert response.status_code == 200
    payload = response.json()
    assert {item["name"] for item in payload["documents"]} == {"SELF.md", "MEMORY.md", "HISTORY.md", "PENDING.md", "RECENT_CONTEXT.md"}
    assert payload["journals"][0]["content"] == journal


def test_memory_maintenance_does_not_consolidate_before_keep_window_is_exceeded(tmp_path):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    session = sessions.open_role_session(role.id)
    for index in range(3):
        sessions.append_message(session.sessionKey, "user", f"普通消息{index}")
        sessions.append_message(session.sessionKey, "assistant", "收到")

    MemoryMaintenance(tmp_path / "roles", sessions).maintain(role.id, session.sessionKey)

    memory_dir = tmp_path / "roles" / role.id / "memory"
    assert (memory_dir / "RECENT_CONTEXT.md").exists()
    assert not (memory_dir / "HISTORY.md").exists()
    assert json.loads((memory_dir / ".maintenance.json").read_text(encoding="utf-8"))["lastSequence"] == 0


def test_memory_maintenance_window_pressure_entry_point_consolidates_old_turns(tmp_path):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    session = sessions.open_role_session(role.id)
    for index in range(8):
        sessions.append_message(session.sessionKey, "user", f"窗口消息{index}")
        sessions.append_message(session.sessionKey, "assistant", "收到")

    maintenance = MemoryMaintenance(tmp_path / "roles", sessions)
    assert maintenance.ensure_memory_for_window(role.id, session.sessionKey, max_messages=12) is True
    memory_dir = tmp_path / "roles" / role.id / "memory"
    assert (memory_dir / "HISTORY.md").exists()
    assert json.loads((memory_dir / ".maintenance.json").read_text(encoding="utf-8"))["lastSequence"] > 0


def test_memory_maintenance_preserves_documents_changed_during_draft(tmp_path):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    session = sessions.open_role_session(role.id)
    for index in range(10):
        sessions.append_message(session.sessionKey, "user", f"消息{index}")
        sessions.append_message(session.sessionKey, "assistant", "收到")

    class UserEditDuringDraft(MemoryMaintenance):
        edited = False

        def _before_commit(self):
            if not self.edited:
                self.edited = True
                path = tmp_path / "roles" / role.id / "memory" / "HISTORY.md"
                path.write_text("用户保留内容\n", encoding="utf-8")

    maintenance = UserEditDuringDraft(tmp_path / "roles", sessions)
    maintenance.maintain(role.id, session.sessionKey)
    history_path = tmp_path / "roles" / role.id / "memory" / "HISTORY.md"
    assert history_path.read_text(encoding="utf-8") == "用户保留内容\n"
    cursor_path = tmp_path / "roles" / role.id / "memory" / ".maintenance.json"
    assert not cursor_path.exists() or json.loads(cursor_path.read_text(encoding="utf-8"))["lastSequence"] == 0


def test_memory_maintenance_keeps_pending_event_without_consumer(tmp_path):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    session = sessions.open_role_session(role.id)
    for index in range(10):
        sessions.append_message(session.sessionKey, "user", f"请记住第{index}项")
        sessions.append_message(session.sessionKey, "assistant", "收到")
    consumer = MemoryMaintenance(tmp_path / "roles", sessions, lambda event: None)
    consumer.maintain(role.id, session.sessionKey)
    # A maintenance process without a callback must leave the durable event;
    # this variant simulates restart before the consumer is available.
    cursor_path = tmp_path / "roles" / role.id / "memory" / ".maintenance.json"
    cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
    cursor["pendingEvent"] = cursor.get("pendingEvent") or {
        "roleId": role.id,
        "sessionKey": session.sessionKey,
        "sourceKey": "source",
        "messageIds": [],
        "messageRange": None,
        "candidates": [],
    }
    cursor_path.write_text(json.dumps(cursor, ensure_ascii=False), encoding="utf-8")
    without_consumer = MemoryMaintenance(tmp_path / "roles", sessions)
    without_consumer.maintain(role.id, session.sessionKey)
    assert "pendingEvent" in json.loads(cursor_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("result", [[], [{"bad": "shape"}], [{"memoryType": "", "summary": "摘要", "sourceKey": "source"}]])
def test_invalid_consolidation_provider_result_does_not_advance_cursor(tmp_path, result):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    session = sessions.open_role_session(role.id)
    for index in range(10):
        sessions.append_message(session.sessionKey, "user", f"请记住第{index}项")
        sessions.append_message(session.sessionKey, "assistant", "收到")
    maintenance = MemoryMaintenance(tmp_path / "roles", sessions, consolidation_provider=lambda source, window: result)

    maintenance.maintain(role.id, session.sessionKey)

    cursor_path = tmp_path / "roles" / role.id / "memory" / ".maintenance.json"
    assert not cursor_path.exists() or json.loads(cursor_path.read_text(encoding="utf-8"))["lastSequence"] == 0


def test_consolidation_committed_event_is_retried_and_acknowledged(tmp_path):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    session = sessions.open_role_session(role.id)
    for index in range(10):
        sessions.append_message(session.sessionKey, "user", f"请记住第{index}项")
        sessions.append_message(session.sessionKey, "assistant", "收到")

    events = []
    failures = 0

    def consume(event):
        nonlocal failures
        events.append(event)
        if failures == 0:
            failures += 1
            raise RuntimeError("consumer unavailable")

    maintenance = MemoryMaintenance(tmp_path / "roles", sessions, consume)
    with pytest.raises(RuntimeError, match="consumer unavailable"):
        maintenance.maintain(role.id, session.sessionKey)
    cursor_path = tmp_path / "roles" / role.id / "memory" / ".maintenance.json"
    assert "pendingEvent" in json.loads(cursor_path.read_text(encoding="utf-8"))

    maintenance.maintain(role.id, session.sessionKey)
    cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
    assert "pendingEvent" not in cursor
    assert len(events) == 2
    assert events[0].source_key == events[1].source_key


def test_maintenance_resumes_pending_event_after_restart(tmp_path):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    session = sessions.open_role_session(role.id)
    for index in range(10):
        sessions.append_message(session.sessionKey, "user", f"请记住第{index}项")
        sessions.append_message(session.sessionKey, "assistant", "收到")
    first_events = []

    def fail_once(event):
        first_events.append(event)
        raise RuntimeError("restart required")

    first = MemoryMaintenance(tmp_path / "roles", sessions, fail_once)
    with pytest.raises(RuntimeError, match="restart required"):
        first.maintain(role.id, session.sessionKey)

    resumed_events = []
    resumed = MemoryMaintenance(tmp_path / "roles", sessions, resumed_events.append)
    assert resumed.resume_pending() == 1
    assert len(resumed_events) == 1
    assert "pendingEvent" not in json.loads(
        (tmp_path / "roles" / role.id / "memory" / ".maintenance.json").read_text(encoding="utf-8")
    )


def test_consolidation_event_consumer_is_idempotent(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    source = MemorySourceRef(
        kind="consolidation",
        sessionKey="role:role-a",
        messageIds=["m1"],
        messageRange=(1, 2),
        stableSourceKey="consolidation:role-a:1-2:digest:m1:fact",
    )
    first = store.consume_consolidation_event(
        "role-a", "consolidation:role-a:1-2:digest", [("fact", "主人住在海边", source, None)]
    )
    second = store.consume_consolidation_event(
        "role-a", "consolidation:role-a:1-2:digest", [("fact", "主人住在海边", source, None)]
    )

    assert [item.id for item in second] == [first[0].id]
    assert store.get("role-a", first[0].id).reinforcement == 1
    assert len(store.consolidation_events_by_time_range("role-a", "0000", "9999")) == 1


def test_maintenance_does_not_resume_deleted_role_directory(tmp_path):
    roles_root = tmp_path / "roles"
    memory_dir = roles_root / "deleted-role" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / ".maintenance.json").write_text('{"pendingEvent": {}}', encoding="utf-8")
    calls: list[str] = []
    initialize_databases(tmp_path / "data")

    maintenance = MemoryMaintenance(
        roles_root,
        SessionStore(tmp_path / "data" / "sessions.db"),
        on_consolidation_committed=lambda event: calls.append(event.role_id),
        role_exists=lambda role_id: False,
    )
    assert maintenance.resume_pending() == 0
    assert calls == []


def test_memory_documents_reject_path_traversal(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    monkeypatch.setattr(main, "roles_root", tmp_path / "roles")
    monkeypatch.setattr(main, "store", roles)

    response = TestClient(main.app).get(f"/api/roles/{role.id}/memory-documents/../MEMORY.md")

    assert response.status_code in {400, 404}


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


def test_memory_maintenance_discards_draft_when_snapshot_message_changes(tmp_path):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    session = sessions.open_role_session(role.id)
    user = sessions.append_message(session.sessionKey, "user", "原始内容")
    sessions.append_message(session.sessionKey, "assistant", "回复")

    class MessageChangedDuringPreparation(MemoryMaintenance):
        changed = False

        def _before_commit(self):
            if not self.changed:
                self.changed = True
                sessions.update_message(user.id, "已修改内容", "completed")

    maintenance = MessageChangedDuringPreparation(tmp_path / "roles", sessions)
    maintenance.maintain(role.id, session.sessionKey)
    recent_path = tmp_path / "roles" / role.id / "memory" / "RECENT_CONTEXT.md"
    assert not recent_path.exists()
    maintenance.maintain(role.id, session.sessionKey)
    assert "已修改内容" in recent_path.read_text(encoding="utf-8")


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


def test_memory_api_can_reject_memory_in_current_role_only(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="甲", profile=RoleProfile(profile="设定")))
    other = roles.create(RoleInput(name="乙", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    memory_store = MemoryStore(tmp_path / "data" / "memory.db")
    service = MemoryService(memory_store)
    memory = service.remember(role.id, "喜欢红茶", "preference", stable_source_key="saved")
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "memory_store", memory_store)
    monkeypatch.setattr(main, "memory_service", service)
    client = TestClient(main.app)

    rejected = client.post(f"/api/roles/{role.id}/memories/{memory.id}/reject")

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert client.get(f"/api/roles/{role.id}/memories").json()["memories"] == []
    assert client.post(f"/api/roles/{other.id}/memories/{memory.id}/reject").status_code == 404


def test_memory_management_keeps_structured_and_markdown_views_in_sync(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    memory_store = MemoryStore(tmp_path / "data" / "memory.db")
    service = MemoryService(memory_store)
    memory_dir = tmp_path / "roles" / role.id / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    memory_path = memory_dir / "MEMORY.md"
    memory_path.write_text("# 主人手写\n\n只保留这段手写内容。\n", encoding="utf-8")
    pending_path = memory_dir / "PENDING.md"
    pending_path.write_text("- [preference] 喜欢红茶（来源：消息 1）\n  <!-- source: pending-source:message-1:preference -->\n", encoding="utf-8")
    service.remember(role.id, "喜欢红茶", "preference", stable_source_key="pending-source")
    monkeypatch.setattr(main, "roles_root", tmp_path / "roles")
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "memory_store", memory_store)
    monkeypatch.setattr(main, "memory_service", service)
    client = TestClient(main.app)

    created = client.post(f"/api/roles/{role.id}/memories", json={"summary": "喜欢红茶", "memoryType": "preference"})
    memory = created.json()
    document = memory_path.read_text(encoding="utf-8")
    assert "只保留这段手写内容。" in document
    assert "喜欢红茶" in document

    forgotten = client.post(f"/api/roles/{role.id}/memories/{memory['id']}/forget")

    assert forgotten.status_code == 200
    document = memory_path.read_text(encoding="utf-8")
    assert "只保留这段手写内容。" in document
    assert "喜欢红茶" not in document
    assert "喜欢红茶" not in pending_path.read_text(encoding="utf-8")
    assert client.get(f"/api/roles/{role.id}/memories").json()["memories"] == []


def test_memory_management_rolls_back_when_markdown_sync_fails(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    memory_store = MemoryStore(tmp_path / "data" / "memory.db")
    service = MemoryService(memory_store)
    memory = service.remember(role.id, "喜欢红茶", "preference", stable_source_key="saved")
    memory_path = tmp_path / "roles" / role.id / "memory" / "MEMORY.md"
    memory_path.write_text("原 Markdown\n", encoding="utf-8")
    monkeypatch.setattr(main, "roles_root", tmp_path / "roles")
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "memory_store", memory_store)
    monkeypatch.setattr(main, "memory_service", service)

    def fail_sync(self, role_id, memories):
        raise OSError("模拟同步失败")

    monkeypatch.setattr("backend.app.memory_documents.MemoryDocuments.sync_structured_memory", fail_sync)
    response = TestClient(main.app).post(f"/api/roles/{role.id}/memories/{memory.id}/forget")

    assert response.status_code == 500
    assert memory_store.get(role.id, memory.id).status == "active"
    assert memory_path.read_text(encoding="utf-8") == "原 Markdown\n"


def test_editing_memory_rebuilds_its_embedding_for_the_new_summary(tmp_path):
    provider = FakeEmbeddingProvider({
        ("role-a", "旧摘要"): [1.0, 0.0],
        ("role-a", "新摘要"): [0.0, 1.0],
        ("role-a", "旧内容查询"): [1.0, 0.0],
    })
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store, provider)
    memory = service.remember("role-a", "旧摘要", "fact", stable_source_key="source")

    store.update("role-a", memory.id, "新摘要", "fact", None)

    assert service.recall("role-a", "旧内容查询") == []
    assert store.get("role-a", memory.id).summary == "新摘要"


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


def test_memory_admin_api_filters_updates_metadata_and_supports_bulk_actions(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    other_role = roles.create(RoleInput(name="其他", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    memory_store = MemoryStore(tmp_path / "data" / "memory.db")
    service = MemoryService(memory_store)
    source = MemorySourceRef(kind="manual", sessionKey=f"role:{role.id}", stableSourceKey="admin-a")
    first = memory_store.add_or_reinforce(
        role.id, "preference", "主人喜欢红茶", source,
        extra={"memory_domain": "relationship", "note": "初始"},
    )
    second = memory_store.add_or_reinforce(
        role.id, "fact", "主人住在海边",
        MemorySourceRef(kind="manual", sessionKey=f"role:{role.id}", stableSourceKey="admin-b"),
    )
    other = service.remember(other_role.id, "其他角色的事实", "fact", stable_source_key="other")
    memory_store.set_embedding(role.id, first.id, [1.0, 0.0])
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "memory_store", memory_store)
    monkeypatch.setattr(main, "memory_service", service)
    monkeypatch.setattr(main, "memory_worker", MemoryWorker(service))
    client = TestClient(main.app)

    listed = client.get(
        f"/api/roles/{role.id}/memories",
        params={"memoryDomain": "relationship", "hasEmbedding": "true", "page": 1, "pageSize": 1},
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["memories"][0]["id"] == first.id

    details = client.get(f"/api/roles/{role.id}/memory-admin/items/{first.id}", params={"includeEmbedding": "true"})
    assert details.status_code == 200
    assert details.json()["embedding"] == [1.0, 0.0]
    updated = client.patch(
        f"/api/roles/{role.id}/memory-admin/items/{first.id}",
        json={"emotionalWeight": 8, "extraJson": {"memory_domain": "relationship", "reviewed": True}},
    )
    assert updated.status_code == 200
    assert updated.json()["emotionalWeight"] == 8
    assert memory_store.get(role.id, first.id).contentHash == first.contentHash
    assert memory_store.embedding_for(role.id, first.id) == [1.0, 0.0]
    happened = client.patch(
        f"/api/roles/{role.id}/memory-admin/items/{first.id}",
        json={"happenedAt": "2026-09-30T08:00:00+00:00"},
    )
    assert happened.status_code == 200
    assert memory_store.get(role.id, first.id).happenedAt is not None
    cleared = client.patch(
        f"/api/roles/{role.id}/memory-admin/items/{first.id}",
        json={"happenedAt": None},
    )
    assert cleared.status_code == 200
    assert memory_store.get(role.id, first.id).happenedAt is None

    similar = client.get(f"/api/roles/{role.id}/memory-admin/items/{first.id}/similar")
    assert similar.status_code == 200
    assert all(item["roleId"] == role.id for item in similar.json()["items"])
    assert client.post(f"/api/roles/{role.id}/memory-admin/items/batch-delete", json={"ids": [second.id]}).json() == {"deleted": 1}
    assert memory_store.get(role.id, second.id) is None
    assert client.post(f"/api/roles/{role.id}/memory-admin/invalidate").json() == {"invalidated": 1}
    all_items = client.get(f"/api/roles/{role.id}/memories", params={"status": ""}).json()["memories"]
    assert all_items[0]["status"] == "forgotten"
    assert memory_store.get(other_role.id, other.id).status == "active"


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
    memory_service.remember("other-role", "其他角色的记忆", "fact", stable_source_key="other")
    (tmp_path / "roles" / role.id / "memory" / "MEMORY.md").write_text("角色记忆", encoding="utf-8")
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "memory_store", memory_store)
    monkeypatch.setattr(main, "memory_service", memory_service)
    monkeypatch.setattr(main, "role_locks", {})
    response = TestClient(main.app).delete(f"/api/roles/{role.id}")
    assert response.status_code == 204
    assert memory_store.list_active(role.id) == []
    assert [item.summary for item in memory_store.list_active("other-role")] == ["其他角色的记忆"]
    assert not (tmp_path / "roles" / role.id / "memory").exists()


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


def test_context_block_never_exceeds_budget_and_keeps_complete_memory_items(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store)
    first = service.remember("role-a", "第一条完整记忆", "fact", stable_source_key="one")
    second = service.remember("role-a", "第二条完整记忆", "fact", stable_source_key="two")

    context = service.context_block(
        [first, second],
        "[MEMORY.md]\n" + "固定内容" * 20,
        max_chars=48,
    )

    assert len(context) <= 48
    assert "第一条完整记忆" not in context or "- [fact] 第一条完整记忆" in context
    assert "第二条完整记忆" not in context or "- [fact] 第二条完整记忆" in context
    assert "- [fact] 第一条完整记忆" not in context or "- [fact] 第二条完整记忆" not in context


def test_context_block_does_not_emit_empty_memory_heading(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store)
    memory = service.remember("role-a", "一条很长的记忆" * 20, "fact", stable_source_key="one")

    context = service.context_block([memory], "", max_chars=12)

    assert context == ""


def test_post_response_provider_is_structured_and_falls_back_on_invalid_output(tmp_path):
    calls = []

    def provider(role_id, session_key, user, assistant, active):
        calls.append((role_id, session_key, len(active)))
        return [{"memoryType": "preference", "summary": "喜欢绿茶", "supersedeKey": None}]

    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store, post_response_provider=provider)
    now = datetime.now(timezone.utc)
    user = Message(id="u1", role="user", content="今天聊聊", sessionKey="role:role-a", sequence=1, status="completed", createdAt=now)
    assistant = Message(id="a1", role="assistant", content="好的", sessionKey="role:role-a", sequence=2, status="completed", createdAt=now)
    saved = service.process_turn("role-a", "role:role-a", user, assistant)

    assert [item.summary for item in saved] == ["喜欢绿茶"]
    assert calls == [("role-a", "role:role-a", 0)]

    fallback = MemoryService(store, post_response_provider=lambda *args: {"bad": True})
    fallback_user = user.model_copy(update={"id": "u2", "sequence": 3, "content": "我叫小明"})
    fallback_assistant = assistant.model_copy(update={"id": "a2", "sequence": 4})
    result = fallback.process_turn("role-a", "role:role-a", fallback_user, fallback_assistant)
    assert any(item.summary == "名字是小明" for item in result)


def test_memory_event_bus_observers_are_best_effort():
    bus = MemoryEventBus()
    seen = []
    bus.subscribe(lambda event: seen.append(event))
    bus.subscribe(lambda event: (_ for _ in ()).throw(RuntimeError("telemetry down")))

    bus.publish(MemoryWritten("role-a", "role:role-a", "source", ("m1",)))

    assert isinstance(seen[0], MemoryWritten)
    assert bus.errors and "telemetry down" in bus.errors[0]


def test_post_response_protects_explicit_tool_memory_ids(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store)
    source = MemorySourceRef(kind="manual", sessionKey="role:role-a", stableSourceKey="tool:item")
    item = store.add_or_reinforce("role-a", "preference", "喜欢咖啡", source, extra={"explicit": True})
    now = datetime.now(timezone.utc)
    user = Message(
        id="u-tool", role="user", content="以前喜欢咖啡，现在改为喜欢茶",
        sessionKey="role:role-a", sequence=1, status="completed", createdAt=now,
    )
    assistant = Message(
        id="a-tool", role="assistant", content="知道了", sessionKey="role:role-a",
        sequence=2, status="completed", createdAt=now,
    )

    service.process_turn("role-a", "role:role-a", user, assistant, protected_item_ids=(item.id,))

    assert store.get("role-a", item.id).status == "active"


def test_memory_store_vector_index_survives_store_restart(tmp_path):
    database = tmp_path / "memory.db"
    source = MemorySourceRef(kind="manual", sessionKey="role:role-a", stableSourceKey="restart")
    first_store = MemoryStore(database)
    item = first_store.add_or_reinforce("role-a", "fact", "主人喜欢海边", source)
    first_store.set_embedding("role-a", item.id, [1.0, 0.0])

    restarted = MemoryStore(database)
    result = restarted.query_hybrid("role-a", "", [1.0, 0.0], limit=1)

    assert result and result[0].id == item.id
    assert restarted.vector_index_status()["backend"] in {"vector-index", "sqlite-scan"}


def test_memory_store_uses_optional_vector_index_and_keeps_sqlite_fallback(tmp_path):
    class Index:
        def __init__(self):
            self.rows = {}
        def upsert(self, role_id, item_id, vector):
            self.rows[(role_id, item_id)] = vector
        def search(self, role_id, vector, limit):
            return [(item_id, 0.9) for (stored_role, item_id), _ in self.rows.items() if stored_role == role_id][:limit]
        def delete_role(self, role_id):
            self.rows = {key: value for key, value in self.rows.items() if key[0] != role_id}

    index = Index()
    store = MemoryStore(tmp_path / "memory.db", vector_index=index)
    source = MemorySourceRef(kind="manual", sessionKey="role:role-a", stableSourceKey="vector")
    item = store.add_or_reinforce("role-a", "fact", "海边", source)
    store.set_embedding("role-a", item.id, [1.0, 0.0])

    assert store.vector_index_status()["available"] is True
    assert store.vector_index_status()["backend"] == "vector-index"
    assert store.query_hybrid("role-a", "海边", [1.0, 0.0], limit=1)[0].id == item.id
