from fastapi.testclient import TestClient
import pytest

from backend.app import main
from backend.app.embedding_config import EmbeddingConfigurationStore
from backend.app.model_config import ModelConfigurationStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "model_configuration_store", ModelConfigurationStore(tmp_path / "models.json"))
    monkeypatch.setattr(main, "embedding_configuration_store", EmbeddingConfigurationStore(tmp_path / "embedding.json"))
    return TestClient(main.app)


def model_payload(**values):
    return {
        "providerId": "custom",
        "provider": "local-test",
        "baseUrl": "http://127.0.0.1:11434/v1",
        "model": "test-model",
        "apiKey": "model-secret",
        **values,
    }


def test_model_key_is_revealed_only_for_the_requested_connection_without_caching(client):
    saved = client.post("/api/model/configurations", json=model_payload())
    configuration_id = saved.json()["configuration"]["id"]
    client.post("/api/model/configurations", json=model_payload(model="another-model", apiKey="another-secret"))

    response = client.post(f"/api/model/configurations/{configuration_id}/reveal-key")

    assert response.status_code == 200
    assert response.json() == {"apiKey": "model-secret"}
    assert response.headers["cache-control"] == "no-store"
    assert "apiKey" not in saved.json()["configuration"]
    listed = client.get("/api/model/configurations").json()
    assert all("apiKey" not in item for item in listed["configurations"])
    assert "apiKey" not in client.get("/api/model/configuration").json()["configuration"]


def test_embedding_key_is_revealed_explicitly_without_changing_public_configuration(client):
    saved = client.put("/api/embedding/configuration", json={"apiKey": "embedding-secret"})

    response = client.post("/api/embedding/configuration/reveal-key")

    assert response.status_code == 200
    assert response.json() == {"apiKey": "embedding-secret"}
    assert response.headers["cache-control"] == "no-store"
    assert "apiKey" not in saved.json()["configuration"]
    public = client.get("/api/embedding/configuration").json()["configuration"]
    assert public["apiKeyConfigured"] is True
    assert "apiKey" not in public
    memory_settings = client.get("/api/memory/settings").json()
    assert "apiKey" not in memory_settings["embedding"]["configuration"]


def test_connection_without_a_key_reveals_an_empty_string(client):
    saved = client.post("/api/model/configurations", json=model_payload(apiKey=""))
    configuration_id = saved.json()["configuration"]["id"]

    response = client.post(f"/api/model/configurations/{configuration_id}/reveal-key")

    assert response.status_code == 200
    assert response.json() == {"apiKey": ""}
    assert response.headers["cache-control"] == "no-store"


def test_unconfigured_embedding_reveals_an_empty_string(client):
    response = client.post("/api/embedding/configuration/reveal-key")

    assert response.status_code == 200
    assert response.json() == {"apiKey": ""}
    assert response.headers["cache-control"] == "no-store"


def test_unknown_connection_cannot_reveal_the_active_connection_key(client):
    client.post("/api/model/configurations", json=model_payload())

    response = client.post("/api/model/configurations/unknown-connection/reveal-key")

    assert response.status_code == 404
    assert response.json() == {"detail": "模型配置不存在"}
    assert response.headers["cache-control"] == "no-store"
    assert "model-secret" not in response.text


def test_reveal_requires_an_explicit_post_request(client):
    saved = client.post("/api/model/configurations", json=model_payload())
    configuration_id = saved.json()["configuration"]["id"]
    client.put("/api/embedding/configuration", json={"apiKey": "embedding-secret"})

    assert client.get(f"/api/model/configurations/{configuration_id}/reveal-key").status_code == 405
    assert client.get("/api/embedding/configuration/reveal-key").status_code == 405
