import asyncio
from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.embedding_config import EmbeddingConfigurationStore
from backend.app.embeddings import SharedEmbeddingProvider
from backend.app.memory_config import MemorySettings, MemorySettingsStore
from backend.app.memory_engine import DefaultMemoryEngine, MemoryQuery, MemoryScope
from backend.app.memory_service import MemoryService, MemoryWorker
from backend.app.memory_optimizer import MemoryOptimizer, MemoryOptimizerWorker
from backend.app.memory_store import MemoryStore
from backend.app.models import EmbeddingConfigurationInput, MemorySourceRef, Message
from backend.app.owner_knowledge import DocumentScan, OwnerKnowledgePlugin, OwnerKnowledgeStore, RemoteDocument


class _Reader:
    def __init__(self):
        self.document = RemoteDocument(
            document_id="doc-1", node_id="node-1", object_token="token-1", object_type="docx",
            title="学习记录", url="https://example.test/doc-1", updated_at="2026-01-01",
        )

    async def scan(self):
        return DocumentScan(documents=[self.document], complete=True)

    async def read_document(self, document):
        return "主人喜欢海边散步。"


class _FailingEmbedder:
    model_id = "test-embedder-v1"
    is_enabled = True

    def embed_documents(self, texts):
        raise RuntimeError("provider unavailable")

    def embed_query(self, text):
        raise RuntimeError("provider unavailable")


def test_settings_api_persists_independent_switches_without_exposing_key(tmp_path, monkeypatch):
    settings = MemorySettingsStore(tmp_path / "memory-settings.json")
    embedding = EmbeddingConfigurationStore(tmp_path / "embedding-config.json")
    embedding.save(EmbeddingConfigurationInput(apiKey="secret-key"))
    provider = SharedEmbeddingProvider(embedding.get, enabled=lambda: settings.get().embeddingEnabled)
    monkeypatch.setattr(main, "memory_settings_store", settings)
    monkeypatch.setattr(main, "embedding_configuration_store", embedding)
    monkeypatch.setattr(main, "embedding_provider", provider)
    client = TestClient(main.app)
    initial = client.get("/api/memory/settings")
    assert initial.json()["enabled"] is True
    response = client.put("/api/memory/settings", json={"enabled": False, "embeddingEnabled": False})
    assert response.status_code == 200
    assert response.json()["embedding"]["status"]["ready"] is False
    assert "secret-key" not in response.text
    assert MemorySettingsStore(settings.path).get() == MemorySettings(enabled=False, embeddingEnabled=False)
    assert embedding.get().apiKey == "secret-key"


def test_paused_memory_skips_queries_and_background_tasks_preserving_records(tmp_path):
    settings = MemorySettingsStore(tmp_path / "settings.json")
    settings.save(MemorySettings(enabled=False))
    store = MemoryStore(tmp_path / "memory.db")
    item = store.add_or_reinforce("role-a", "fact", "海边", MemorySourceRef(kind="manual", sessionKey="role:role-a", stableSourceKey="manual:test"))
    service = MemoryService(store, memory_enabled=lambda: settings.get().enabled)
    engine = DefaultMemoryEngine(service, role_exists=lambda _: True)
    worker = MemoryWorker(service)
    optimizer = MemoryOptimizerWorker(MemoryOptimizer(tmp_path / "roles"), enabled=lambda: settings.get().enabled)
    user = Message(id="user", sessionKey="role:role-a", sequence=1, role="user", content="记住我喜欢海边", status="completed", createdAt=datetime.now(timezone.utc))
    assistant = user.model_copy(update={"id": "assistant", "sequence": 2, "role": "assistant", "content": "好"})

    async def run():
        result = await engine.query(MemoryQuery(text="海边", scope=MemoryScope("role-a", "role:role-a")))
        assert result.records == []
        assert result.text_block == ""
        assert result.trace["disabled"] is True
        assert worker.submit("role-a", "role:role-a", user, assistant) is False
        assert optimizer.submit("role-a") is False
        settings.save(MemorySettings())
        assert worker.submit("role-a", "role:role-a", user, assistant) is True
        await worker.drain()
        assert (await engine.query(MemoryQuery(text="海边", scope=MemoryScope("role-a", "role:role-a")))).records

    asyncio.run(run())
    assert store.get("role-a", item.id) is not None


def test_vector_pause_keeps_keyword_recall_without_provider_requests(tmp_path, monkeypatch):
    settings = MemorySettingsStore(tmp_path / "settings.json")
    settings.save(MemorySettings(embeddingEnabled=False))
    config = EmbeddingConfigurationStore(tmp_path / "embedding.json")
    config.save(EmbeddingConfigurationInput(apiKey="key", dimensions=2))
    provider = SharedEmbeddingProvider(config.get, enabled=lambda: settings.get().embeddingEnabled)
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, request=httpx.Request("POST", url), json={"data": [{"index": 0, "embedding": [1., 0.]}]})

    monkeypatch.setattr("backend.app.embeddings.httpx.post", post)
    service = MemoryService(MemoryStore(tmp_path / "memory.db"), provider)
    service.remember("role-a", "主人喜欢海边", "preference")
    assert asyncio.run(service.recall_async("role-a", "海边"))
    assert calls == []
    assert provider.ready is False
    settings.save(MemorySettings())
    assert asyncio.run(service.recall_async("role-a", "海边"))
    assert calls


def test_paused_vector_syncs_owner_documents_for_lexical_search(tmp_path):
    settings = MemorySettingsStore(tmp_path / "settings.json")
    settings.save(MemorySettings(embeddingEnabled=False))
    config = EmbeddingConfigurationStore(tmp_path / "embedding.json")
    config.save(EmbeddingConfigurationInput(apiKey="key", dimensions=2))
    provider = SharedEmbeddingProvider(config.get, enabled=lambda: settings.get().embeddingEnabled)
    store = OwnerKnowledgeStore(tmp_path / "owner-knowledge.db")
    store.set_state("feishu_access_token", "token")
    plugin = OwnerKnowledgePlugin(store, _Reader(), provider)

    async def run():
        report = await plugin.enable()
        assert report.complete is True
        hits = await plugin.search("海边", role_id="role-a")
        assert hits and "海边" in hits[0].text
        assert store.count_chunks() == 1

    asyncio.run(run())


def test_failed_owner_embedding_keeps_full_text_index_and_can_retry(tmp_path):
    store = OwnerKnowledgeStore(tmp_path / "owner-knowledge.db")
    store.set_state("feishu_access_token", "token")
    plugin = OwnerKnowledgePlugin(store, _Reader(), _FailingEmbedder())

    async def run():
        report = await plugin.enable()
        assert report.complete is True
        hits = await plugin.search("海边", role_id="role-a")
        assert hits and "海边" in hits[0].text
        assert store.list_document_statuses()[0]["status"] == "synced"
        assert store.list_document_statuses()[0]["error"] == ""
        assert plugin.embedding_last_error.startswith("向量索引暂不可用")

    asyncio.run(run())
