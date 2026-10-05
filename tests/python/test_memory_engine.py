import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from backend.app.memory_engine import (
    DefaultMemoryEngine,
    MemoryEngine,
    MemoryMutation,
    MemoryQuery,
    MemoryQueryFilters,
    MemoryScope,
)
from backend.app.memory_service import MemoryService
from backend.app.memory_store import MemoryStore
from backend.app.models import MemorySourceRef


@pytest.mark.parametrize("role_id", ["", "   "])
def test_memory_scope_requires_a_role_id(role_id):
    with pytest.raises(ValueError, match="role_id"):
        MemoryScope(role_id=role_id, session_key="role:role-a")


def test_memory_scope_requires_a_session_key():
    with pytest.raises(ValueError, match="session_key"):
        MemoryScope(role_id="role-a", session_key="")


def test_memory_scope_rejects_session_from_another_role():
    with pytest.raises(ValueError, match="session_key"):
        MemoryScope(role_id="role-a", session_key="role:role-b")


def test_memory_query_filter_copies_mutable_hints():
    hints = {"rerank": True}
    filters = MemoryQueryFilters(hints=hints)
    hints["rerank"] = False

    assert filters.hints["rerank"] is True
    assert filters.time_start is None


def test_memory_schema_migrates_existing_items_and_can_run_again(tmp_path):
    database = tmp_path / "legacy-memory.db"
    source = json.dumps(
        {
            "kind": "manual",
            "sessionKey": "role:role-a",
            "messageIds": [],
            "stableSourceKey": "legacy-source",
            "sourceKeys": ["legacy-source"],
            "sources": [],
        }
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE memory_items (
                id TEXT PRIMARY KEY, role_id TEXT NOT NULL, memory_type TEXT NOT NULL,
                summary TEXT NOT NULL, extra_json TEXT NOT NULL, source_ref TEXT NOT NULL,
                happened_at TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, reinforcement INTEGER NOT NULL, content_hash TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO memory_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-1", "role-a", "preference", "喜欢海边", "{}", source,
                None, "active", "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00", 3, "legacy-hash",
            ),
        )

    store = MemoryStore(database)
    assert store.get("role-a", "legacy-1").summary == "喜欢海边"
    MemoryStore(database)

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(memory_items)")}
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        row = connection.execute(
            "SELECT id, reinforcement, content_hash, emotional_weight FROM memory_items"
        ).fetchone()

    assert {"embedding_json", "emotional_weight"} <= columns
    assert {"consolidation_events", "memory_replacements", "memory_embedding_spaces"} <= tables
    assert row == ("legacy-1", 3, "legacy-hash", 0)


def test_memory_schema_refuses_to_downgrade_a_newer_database(tmp_path):
    database = tmp_path / "future-memory.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 2")

    with pytest.raises(RuntimeError, match="newer than this application"):
        MemoryStore(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_embedding_dimension_change_invalidates_only_that_roles_vectors(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    source_a = MemorySourceRef(kind="manual", sessionKey="role:role-a", stableSourceKey="a")
    source_b = MemorySourceRef(kind="manual", sessionKey="role:role-b", stableSourceKey="b")
    item_a = store.add_or_reinforce("role-a", "fact", "A", source_a)
    item_b = store.add_or_reinforce("role-b", "fact", "B", source_b)
    store.set_embedding("role-a", item_a.id, [1.0, 0.0])
    store.set_embedding("role-b", item_b.id, [1.0, 0.0])

    store.set_embedding("role-a", item_a.id, [0.0, 1.0, 0.0])

    assert store.get("role-a", item_a.id).hasEmbedding is True
    assert store.get("role-b", item_b.id).hasEmbedding is True
    with sqlite3.connect(tmp_path / "memory.db") as connection:
        vectors = dict(connection.execute("SELECT role_id, embedding_json FROM memory_items"))
    assert json.loads(vectors["role-a"]) == [0.0, 1.0, 0.0]
    assert json.loads(vectors["role-b"]) == [1.0, 0.0]


def test_recall_detects_a_query_embedding_dimension_change(tmp_path):
    class QueryEmbeddingProvider:
        def embed(self, role_id, text):
            return [1.0, 0.0, 0.0]

    store = MemoryStore(tmp_path / "memory.db")
    source = MemorySourceRef(kind="manual", sessionKey="role:role-a", stableSourceKey="source")
    item = store.add_or_reinforce("role-a", "fact", "喜欢海边", source)
    store.set_embedding("role-a", item.id, [1.0, 0.0])
    service = MemoryService(store, QueryEmbeddingProvider())

    service.recall("role-a", "喜欢海边")

    assert store.list_without_embeddings("role-a") == [store.get("role-a", item.id)]


def test_async_recall_detects_a_query_embedding_dimension_change(tmp_path):
    class QueryEmbeddingProvider:
        def embed(self, role_id, text):
            return [1.0, 0.0, 0.0]

    store = MemoryStore(tmp_path / "memory.db")
    source = MemorySourceRef(kind="manual", sessionKey="role:role-a", stableSourceKey="source")
    item = store.add_or_reinforce("role-a", "fact", "喜欢海边", source)
    store.set_embedding("role-a", item.id, [1.0, 0.0])
    service = MemoryService(store, QueryEmbeddingProvider())

    asyncio.run(service.recall_async("role-a", "喜欢海边"))

    assert store.list_without_embeddings("role-a") == [store.get("role-a", item.id)]


def test_memory_store_rejects_missing_role_scope(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    source = MemorySourceRef(kind="manual", sessionKey="role:role-a", stableSourceKey="source")

    with pytest.raises(ValueError, match="role_id"):
        store.add_or_reinforce("  ", "fact", "记忆", source)


def test_memory_store_rejects_a_source_from_another_role(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    source = MemorySourceRef(kind="manual", sessionKey="role:role-b", stableSourceKey="source")

    with pytest.raises(ValueError, match="sessionKey"):
        store.add_or_reinforce("role-a", "fact", "记忆", source)

    assert store.list_all("role-a") == []


def test_default_engine_exposes_the_complete_memory_engine_contract(tmp_path):
    engine = DefaultMemoryEngine(
        MemoryService(MemoryStore(tmp_path / "memory.db")),
        role_exists=lambda _: True,
    )

    assert isinstance(engine, MemoryEngine)


def test_migration_discards_mixed_embedding_dimensions_without_losing_items(tmp_path):
    database = tmp_path / "mixed-vectors.db"
    source = json.dumps({"kind": "manual", "sessionKey": "role:role-a", "stableSourceKey": "legacy"})
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE memory_items (
                id TEXT PRIMARY KEY, role_id TEXT NOT NULL, memory_type TEXT NOT NULL,
                summary TEXT NOT NULL, extra_json TEXT NOT NULL, source_ref TEXT NOT NULL,
                happened_at TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, reinforcement INTEGER NOT NULL, content_hash TEXT NOT NULL,
                embedding_json TEXT
            )"""
        )
        for item_id, vector in (("one", "[1.0, 0.0]"), ("two", "[1.0, 0.0, 0.0]")):
            connection.execute(
                "INSERT INTO memory_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item_id, "role-a", "fact", item_id, "{}", source, None, "active", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", 1, item_id, vector),
            )

    store = MemoryStore(database)

    assert {item.id for item in store.list_all("role-a")} == {"one", "two"}
    unembedded = store.list_without_embeddings("role-a", limit=5)
    assert len(unembedded) == 2
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_memory_engine_keeps_identical_content_isolated_between_roles(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    engine = DefaultMemoryEngine(
        MemoryService(store),
        role_exists=lambda role_id: role_id in {"role-a", "role-b"},
    )

    async def exercise():
        first = await engine.mutate(
            MemoryMutation(
                kind="remember",
                scope=MemoryScope("role-a", "role:role-a"),
                summary="喜欢海边",
                memory_kind="preference",
                source_ref="manual-a",
            )
        )
        second = await engine.mutate(
            MemoryMutation(
                kind="remember",
                scope=MemoryScope("role-b", "role:role-b"),
                summary="喜欢海边",
                memory_kind="preference",
                source_ref="manual-b",
            )
        )
        result = await engine.query(
            MemoryQuery("喜欢海边", intent="context", scope=MemoryScope("role-a", "role:role-a"))
        )
        return first, second, result

    first, second, result = asyncio.run(exercise())

    assert first.accepted is True
    assert second.accepted is True
    assert first.item_id != second.item_id
    assert [record.id for record in result.records] == [first.item_id]
    assert result.records[0].source["stable_source_key"] == "manual-a"
    assert result.records[0].status == "active"
    assert result.records[0].has_embedding is False


def test_memory_engine_mutation_can_change_item_state(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    engine = DefaultMemoryEngine(MemoryService(store), role_exists=lambda _: True)
    scope = MemoryScope("role-a", "role:role-a")

    async def exercise():
        created = await engine.mutate(
            MemoryMutation(kind="remember", scope=scope, summary="喜欢海边", memory_kind="preference")
        )
        changed = await engine.mutate(
            MemoryMutation(kind="state_change", scope=scope, ids=(created.item_id,), status="rejected")
        )
        return created, changed

    created, changed = asyncio.run(exercise())

    assert changed.accepted is True
    assert changed.status == "rejected"
    assert changed.raw["items"][0]["status"] == "rejected"
    assert store.get("role-a", created.item_id).status == "rejected"


def test_memory_engine_rejects_unknown_roles(tmp_path):
    engine = DefaultMemoryEngine(MemoryService(MemoryStore(tmp_path / "memory.db")), role_exists=lambda _: False)

    async def exercise():
        await engine.query(MemoryQuery("hello", scope=MemoryScope("unknown", "role:unknown")))

    with pytest.raises(ValueError, match="role_id"):
        asyncio.run(exercise())


def test_memory_engine_applies_interest_filters_and_answer_lanes(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    engine = DefaultMemoryEngine(MemoryService(store), role_exists=lambda _: True)
    source = MemorySourceRef(kind="manual", sessionKey="role:role-a", stableSourceKey="source")
    store.add_or_reinforce("role-a", "preference", "喜欢海边", source)
    store.add_or_reinforce("role-a", "fact", "海边工作地点", source)

    async def exercise():
        interest = await engine.query(MemoryQuery(
            "海边", intent="interest", scope=MemoryScope("role-a", "role:role-a")
        ))
        answer = await engine.query(MemoryQuery(
            "海边", intent="answer", scope=MemoryScope("role-a", "role:role-a")
        ))
        return interest, answer

    interest, answer = asyncio.run(exercise())

    assert [record.kind for record in interest.records] == ["preference"]
    assert [lane["query"] for lane in answer.trace["lanes"]] == ["海边"]
    assert answer.trace["rrf"] is True


def test_memory_engine_uses_real_hyde_provider_and_falls_back(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    calls = []

    def hyde(query):
        calls.append(query)
        return ["用户过去的事件", "用户的一般事实"]

    engine = DefaultMemoryEngine(MemoryService(store), role_exists=lambda _: True, hyde_provider=hyde)
    result = asyncio.run(engine.query(MemoryQuery("最近发生了什么", intent="answer", scope=MemoryScope("role-a", "role:role-a"))))

    assert calls == ["最近发生了什么"]
    assert [lane["query"] for lane in result.trace["lanes"]] == ["最近发生了什么", "用户过去的事件", "用户的一般事实"]


def test_procedure_rule_schema_filters_unsafe_memory(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    source = MemorySourceRef(kind="manual", sessionKey="role:role-a", stableSourceKey="procedure")
    store.add_or_reinforce("role-a", "procedure", "发送邮件前确认收件人", source, extra={"procedureTags": ["communication"], "confidence": 0.9})
    store.add_or_reinforce("role-a", "procedure", "自动删除草稿", MemorySourceRef(kind="manual", sessionKey="role:role-a", stableSourceKey="unsafe"), extra={"procedureTags": ["dangerous"], "confidence": 0.2})
    engine = DefaultMemoryEngine(MemoryService(store), role_exists=lambda _: True)

    result = asyncio.run(engine.query(MemoryQuery(
        "发送邮件", intent="procedure", scope=MemoryScope("role-a", "role:role-a"),
        filters=MemoryQueryFilters(hints={"procedure_rule": {"requiredTags": ["communication"], "minConfidence": 0.8}}),
    )))

    assert [record.summary for record in result.records] == ["发送邮件前确认收件人"]


def test_memory_engine_timeline_requires_range_and_filters_event_time(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    engine = DefaultMemoryEngine(MemoryService(store), role_exists=lambda _: True)
    source = MemorySourceRef(kind="manual", sessionKey="role:role-a", stableSourceKey="source")
    store.add_or_reinforce(
        "role-a", "event", "搬到海边", source,
        happened_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    store.add_or_reinforce(
        "role-a", "event", "参加会议", source,
        happened_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
    )

    async def exercise():
        with pytest.raises(ValueError, match="time_start"):
            await engine.query(MemoryQuery(
                "", intent="timeline", scope=MemoryScope("role-a", "role:role-a")
            ))
        return await engine.query(MemoryQuery(
            "", intent="timeline", scope=MemoryScope("role-a", "role:role-a"),
            filters=MemoryQueryFilters(
                time_start=datetime(2025, 6, 1, tzinfo=timezone.utc),
                time_end=datetime(2026, 6, 1, tzinfo=timezone.utc),
            ),
        ))

    result = asyncio.run(exercise())

    assert [record.summary for record in result.records] == ["搬到海边"]


def test_memory_engine_returns_empty_result_when_retrieval_lane_fails(tmp_path):
    class FailingService(MemoryService):
        async def recall_async(self, role_id, query, limit=8):
            raise RuntimeError("embedding unavailable")

    engine = DefaultMemoryEngine(FailingService(MemoryStore(tmp_path / "memory.db")), role_exists=lambda _: True)

    result = asyncio.run(engine.query(MemoryQuery(
        "任何内容", scope=MemoryScope("role-a", "role:role-a")
    )))

    assert result.records == []
    assert "error" in result.trace["lanes"][0]


def test_metadata_update_preserves_content_hash_and_embedding(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    source = MemorySourceRef(kind="manual", sessionKey="role:role-a", stableSourceKey="source")
    item = store.add_or_reinforce("role-a", "fact", "记住这件事", source)
    store.set_embedding("role-a", item.id, [0.5, 0.5])

    updated = store.update_metadata("role-a", item.id, emotional_weight=7)

    assert updated is not None
    assert updated.contentHash == item.contentHash
    assert updated.emotionalWeight == 7
    assert updated.hasEmbedding is True
    assert store.embedding_for("role-a", item.id) == [0.5, 0.5]


def test_role_snapshot_restores_only_that_roles_events_and_replacements(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    with sqlite3.connect(tmp_path / "memory.db") as connection:
        for role_id in ("role-a", "role-b"):
            connection.execute(
                "INSERT INTO consolidation_events(role_id, source_ref, item_id, created_at) VALUES (?, ?, ?, ?)",
                (role_id, f"source:{role_id}", None, "2026-01-01T00:00:00+00:00"),
            )
            connection.execute(
                """INSERT INTO memory_replacements(
                       role_id, old_item_id, old_memory_type, old_summary,
                       new_item_id, new_memory_type, new_summary, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (role_id, f"old:{role_id}", "preference", "旧", f"new:{role_id}", "preference", "新", "2026-01-01T00:00:00+00:00"),
            )

    snapshot = store.snapshot_role("role-a")
    store.delete_role("role-a")
    store.restore_role("role-a", snapshot)

    with sqlite3.connect(tmp_path / "memory.db") as connection:
        rows = connection.execute(
            "SELECT role_id, source_ref FROM consolidation_events ORDER BY role_id"
        ).fetchall()
        replacements = connection.execute(
            "SELECT role_id, old_item_id FROM memory_replacements ORDER BY role_id"
        ).fetchall()
    assert rows == [("role-a", "source:role-a"), ("role-b", "source:role-b")]
    assert replacements == [("role-a", "old:role-a"), ("role-b", "old:role-b")]


def test_memory_admin_filters_updates_metadata_and_keeps_embedding(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    engine = DefaultMemoryEngine(MemoryService(store), role_exists=lambda role_id: role_id == "role-a")
    scope = MemoryScope("role-a", "role:role-a")
    created = asyncio.run(engine.mutate(MemoryMutation(
        kind="remember", scope=scope, summary="喜欢红茶", memory_kind="preference",
        metadata={"memory_domain": "relationship", "source_note": "manual"},
    )))
    store.set_embedding("role-a", created.item_id, [1.0, 0.0])

    rows, total = engine.list_items_for_admin(
        role_id="role-a", memory_domain="relationship", has_embedding=True, page=1, page_size=10
    )
    assert total == 1
    assert rows[0]["id"] == created.item_id
    updated = engine.update_item_for_admin(
        "role-a", created.item_id, emotional_weight=8, extra_json={"memory_domain": "relationship", "reviewed": True}
    )
    assert updated is not None
    assert updated["emotionalWeight"] == 8
    assert updated["contentHash"] == rows[0]["contentHash"]
    assert store.embedding_for("role-a", created.item_id) == [1.0, 0.0]


def test_memory_admin_supports_similar_batch_delete_and_invalidation_without_cross_role_access(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    engine = DefaultMemoryEngine(MemoryService(store), role_exists=lambda role_id: role_id in {"role-a", "role-b"})
    source_a = MemorySourceRef(kind="manual", sessionKey="role:role-a", stableSourceKey="a")
    source_b = MemorySourceRef(kind="manual", sessionKey="role:role-b", stableSourceKey="b")
    first = store.add_or_reinforce("role-a", "fact", "主人喜欢海边", source_a)
    second = store.add_or_reinforce(
        "role-a", "fact", "主人常去海边", MemorySourceRef(kind="manual", sessionKey="role:role-a", stableSourceKey="a-2")
    )
    other = store.add_or_reinforce("role-b", "fact", "主人喜欢海边", source_b)

    similar = engine.find_similar_items_for_admin("role-a", first.id, top_k=5)
    assert all(item["roleId"] == "role-a" for item in similar)
    assert engine.delete_items_batch("role-a", [second.id]) == 1
    assert store.get("role-a", second.id) is None
    assert engine.invalidate_role_memories("role-a") == 1
    assert store.get("role-a", first.id).status == "forgotten"
    assert store.get("role-b", other.id).status == "active"
