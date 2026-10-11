import json

import httpx
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.embedding_config import EmbeddingConfigurationStore
from backend.app.embeddings import SharedEmbeddingProvider
from backend.app.memory_store import MemoryStore
from backend.app.models import EmbeddingConfiguration, EmbeddingConfigurationInput, MemorySourceRef


def test_shared_embedding_provider_batches_dashscope_requests(monkeypatch):
    requests = []

    def post(url, *, headers, json, timeout):
        requests.append((url, headers, json, timeout))
        inputs = json["input"]
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"data": [{"index": index, "embedding": [float(index), 1.0]} for index, _ in enumerate(inputs)]},
        )

    monkeypatch.setattr("backend.app.embeddings.httpx.post", post)
    configuration = EmbeddingConfiguration(apiKey="secret-key", dimensions=2)
    provider = SharedEmbeddingProvider(lambda: configuration)

    vectors = provider.embed_documents([f"text-{index}" for index in range(11)])

    assert len(vectors) == 11
    assert len(requests) == 2
    assert requests[0][0].endswith("/embeddings")
    assert requests[0][1] == {"Authorization": "Bearer secret-key"}
    assert requests[0][2] == {"model": "text-embedding-v3", "input": [f"text-{index}" for index in range(10)], "dimensions": 2}
    assert requests[1][2]["input"] == ["text-10"]


def test_shared_embedding_provider_matches_shiori_input_limit_and_normalization(monkeypatch):
    request = {}

    def post(url, *, headers, json, timeout):
        request.update(json)
        return httpx.Response(200, request=httpx.Request("POST", url), json={"data": [{"index": 0, "embedding": [3.0, 4.0]}]})

    monkeypatch.setattr("backend.app.embeddings.httpx.post", post)
    configuration = EmbeddingConfiguration(apiKey="secret-key", dimensions=2)
    vector = SharedEmbeddingProvider(lambda: configuration).embed_documents(["x" * 3000])[0]

    assert len(request["input"][0]) == 2000
    assert vector == [0.6, 0.8]


def test_embedding_configuration_store_uses_defaults_and_never_returns_key(tmp_path):
    store = EmbeddingConfigurationStore(tmp_path / "embedding-config.json")
    assert store.get().model == "text-embedding-v3"
    saved = store.save(EmbeddingConfigurationInput(apiKey="secret-key"))

    assert saved.apiKey == "secret-key"
    persisted = json.loads((tmp_path / "embedding-config.json").read_text(encoding="utf-8"))
    assert persisted["apiKey"] == "secret-key"


def test_embedding_configuration_api_masks_key_and_shares_provider(tmp_path, monkeypatch):
    store = EmbeddingConfigurationStore(tmp_path / "embedding-config.json")
    provider = SharedEmbeddingProvider(store.get)
    monkeypatch.setattr(main, "embedding_configuration_store", store)
    monkeypatch.setattr(main, "embedding_provider", provider)

    client = TestClient(main.app)
    response = client.put("/api/embedding/configuration", json={"apiKey": "secret-key", "dimensions": 1024})

    assert response.status_code == 200
    assert response.json()["configuration"]["apiKeyConfigured"] is True
    assert "apiKey" not in response.json()["configuration"]
    current = client.get("/api/embedding/configuration").json()
    assert current["configuration"]["model"] == "text-embedding-v3"
    assert current["status"]["ready"] is True


def test_owner_knowledge_and_memory_use_one_shared_provider():
    assert main.owner_knowledge_plugin.embedder is main.embedding_provider
    assert main.memory_service.embedding_provider is main.embedding_provider


def test_memory_embedding_model_change_invalidates_same_dimension_vectors(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    source = MemorySourceRef(kind="manual", sessionKey="role:role-a", stableSourceKey="source")
    item = store.add_or_reinforce("role-a", "fact", "主人喜欢红茶", source)
    store.set_embedding("role-a", item.id, [1.0, 0.0], "dashscope:text-embedding-v3:dimensions=1024")

    changed = store.observe_query_embedding("role-a", [0.0, 1.0], "dashscope:other-model:dimensions=1024")

    assert changed is True
    assert store.embedding_for("role-a", item.id) is None
