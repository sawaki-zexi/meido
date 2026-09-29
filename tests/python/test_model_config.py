from backend.app import main
from backend.app.model_config import ModelConfigurationStore
from backend.app.models import ModelConfigurationInput
from fastapi.testclient import TestClient
import httpx
import json


def configure_api(tmp_path, monkeypatch):
    store = ModelConfigurationStore(tmp_path / "data" / "model-config.json")
    monkeypatch.setattr(main, "model_configuration_store", store)
    return store, TestClient(main.app)


def payload(**values):
    return {
        "providerId": "deepseek",
        "provider": "deepseek",
        "baseUrl": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "apiKey": "secret-token",
        **values,
    }


def test_provider_presets_and_saved_configuration_are_publicly_masked(tmp_path, monkeypatch):
    store, client = configure_api(tmp_path, monkeypatch)

    providers = client.get("/api/model/providers").json()["providers"]
    assert [item["id"] for item in providers] == [
        "openai", "deepseek", "dashscope", "moonshot", "zhipu", "siliconflow", "openrouter", "ollama",
    ]
    assert providers[1]["baseUrl"] == "https://api.deepseek.com"
    assert client.get("/api/model/configuration").json()["configuration"] is None

    saved = client.put("/api/model/configuration", json=payload())
    assert saved.status_code == 200
    assert saved.json()["configuration"]["apiKeyConfigured"] is True
    assert "secret-token" not in saved.text
    assert "apiKey" not in saved.json()["configuration"]
    assert ModelConfigurationStore(store.path).get().apiKey == "secret-token"
    assert client.get("/api/model/configuration").json()["configuration"]["apiKeyConfigured"] is True


def test_invalid_configuration_does_not_replace_active_configuration(tmp_path, monkeypatch):
    store, client = configure_api(tmp_path, monkeypatch)
    assert client.put("/api/model/configuration", json=payload()).status_code == 200
    before = store.get()

    response = client.put("/api/model/configuration", json=payload(baseUrl="file:///tmp/model"))

    assert response.status_code == 422
    assert store.get() == before


def test_provider_id_must_match_preset(tmp_path, monkeypatch):
    _, client = configure_api(tmp_path, monkeypatch)

    response = client.put("/api/model/configuration", json=payload(provider="custom-provider"))

    assert response.status_code == 422


def test_multiple_configurations_persist_list_active_selection_edit_and_delete(tmp_path, monkeypatch):
    store, client = configure_api(tmp_path, monkeypatch)
    first = client.post("/api/model/configurations", json=payload()).json()["configuration"]
    second = client.post("/api/model/configurations", json=payload(
        providerId="custom",
        provider="local-test",
        baseUrl="http://127.0.0.1:11434/v1",
        model="qwen3:8b",
        apiKey="",
    )).json()["configuration"]

    listed = client.get("/api/model/configurations").json()
    assert [item["id"] for item in listed["configurations"]] == [first["id"], second["id"]]
    assert listed["activeId"] == second["id"]
    assert client.get("/api/model/configuration").json()["configuration"]["id"] == second["id"]

    updated = client.put(
        f"/api/model/configurations/{first['id']}",
        json=payload(model="deepseek-reasoner", apiKey=""),
    )
    assert updated.status_code == 200
    assert store.get(first["id"]).model == "deepseek-reasoner"
    assert store.get(first["id"]).apiKey == "secret-token"
    assert client.get("/api/model/configuration").json()["configuration"]["id"] == first["id"]

    activated = client.post(f"/api/model/configurations/{second['id']}/activate")
    assert activated.status_code == 200
    assert client.get("/api/model/configuration").json()["configuration"]["id"] == second["id"]

    deleted = client.delete(f"/api/model/configurations/{second['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["activeId"] == first["id"]
    assert [item["id"] for item in client.get("/api/model/configurations").json()["configurations"]] == [first["id"]]


def test_legacy_single_configuration_is_read_and_migrated_on_save(tmp_path):
    path = tmp_path / "model-config.json"
    path.write_text(json.dumps(payload()), encoding="utf-8")
    store = ModelConfigurationStore(path)

    legacy = store.get()
    assert legacy is not None
    assert legacy.id == "model-default"
    assert store.active_id() == "model-default"

    store.save(ModelConfigurationInput(**payload(model="deepseek-reasoner")))
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["version"] == 2
    assert document["activeId"] == "model-default"
    assert len(document["configurations"]) == 1


def test_save_failure_keeps_active_configuration(tmp_path, monkeypatch):
    store, client = configure_api(tmp_path, monkeypatch)
    client.put("/api/model/configuration", json=payload())

    def fail_save(data):
        raise OSError("disk is read-only")

    monkeypatch.setattr(store, "save", fail_save)
    response = client.put("/api/model/configuration", json=payload(model="new-model"))

    assert response.status_code == 500
    assert response.json()["detail"] == "模型配置保存失败，当前生效配置未改变"
    assert ModelConfigurationStore(store.path).get().model == "deepseek-chat"


def test_connection_test_uses_draft_stream_and_does_not_save_it(tmp_path, monkeypatch):
    store, client = configure_api(tmp_path, monkeypatch)
    store.save(ModelConfigurationInput(**payload()))
    calls = []

    async def stream_messages(messages, configuration, *, max_tokens=None):
        calls.append((messages, configuration, max_tokens))
        yield "ok"

    monkeypatch.setattr(main.connection_test_adapter, "stream_messages", stream_messages)
    draft = payload(providerId="custom", provider="local-test", baseUrl="http://127.0.0.1:11434/v1", apiKey="draft-secret", model="test-model")

    response = client.post("/api/model/configuration/test", json=draft)

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert calls[0][0] == [{"role": "user", "content": "ping"}]
    assert calls[0][1].apiKey == "draft-secret"
    assert calls[0][2] == 8
    assert store.get().providerId == "deepseek"
    assert store.get().apiKey == "secret-token"


def test_connection_test_returns_elapsed_latency(tmp_path, monkeypatch):
    _, client = configure_api(tmp_path, monkeypatch)
    clock = iter((10.000, 10.688))
    monkeypatch.setattr(main, "monotonic", lambda: next(clock))

    async def stream_messages(messages, configuration, *, max_tokens=None):
        yield "ok"

    monkeypatch.setattr(main.connection_test_adapter, "stream_messages", stream_messages)

    result = client.post("/api/model/configuration/test", json=payload()).json()

    assert result == {"ok": True, "message": "连接成功", "latencyMs": 688}


def test_connection_test_scrubs_secret_from_failure_and_keeps_active_config(tmp_path, monkeypatch):
    store, client = configure_api(tmp_path, monkeypatch)
    store.save(ModelConfigurationInput(**payload()))

    async def fail(messages, configuration):
        raise RuntimeError(f"provider echoed {configuration.apiKey}")
        yield "unreachable"

    monkeypatch.setattr(main.connection_test_adapter, "stream_messages", fail)
    response = client.post("/api/model/configuration/test", json=payload(apiKey="draft-secret"))

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "draft-secret" not in response.text
    assert store.get().apiKey == "secret-token"


def test_connection_test_turns_unauthorized_into_actionable_auth_error(tmp_path, monkeypatch):
    _, client = configure_api(tmp_path, monkeypatch)
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx.Response(401, request=request)

    async def unauthorized(messages, configuration, *, max_tokens=None):
        raise httpx.HTTPStatusError("Unauthorized", request=request, response=response)
        yield "unreachable"

    monkeypatch.setattr(main.connection_test_adapter, "stream_messages", unauthorized)

    result = client.post("/api/model/configuration/test", json=payload()).json()

    assert result["ok"] is False
    assert result["message"] == "认证失败 (HTTP 401)：请检查 API Key 是否有效，以及是否属于当前服务商账号。"


def test_openai_compatible_adapter_posts_stream_to_preset_base_url(monkeypatch):
    from backend.app.model_adapter import OpenAICompatibleAdapter
    from backend.app.models import ModelConfiguration

    requests = []

    def handler(request):
        requests.append(request)
        body = (
            'data: {"choices":[{"delta":{"content":"流"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"式"}}]}\n\n'
            'data: [DONE]\n\n'
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )
    configuration = ModelConfiguration(
        providerId="openai",
        provider="openai",
        baseUrl="https://api.openai.com/v1/",
        model="gpt-4.1-mini",
        apiKey="test-key",
    )

    async def collect():
        return [delta async for delta in OpenAICompatibleAdapter(timeout=20).stream_messages(
            [{"role": "user", "content": "ping"}], configuration, max_tokens=8
        )]

    import asyncio
    assert asyncio.run(collect()) == ["流", "式"]
    assert requests[0].url == "https://api.openai.com/v1/chat/completions"
    assert requests[0].headers["Authorization"] == "Bearer test-key"
    body = json.loads(requests[0].content)
    assert body["stream"] is True
    assert body["max_tokens"] == 8


def test_blank_api_key_keeps_existing_key_for_same_connection(tmp_path, monkeypatch):
    store, client = configure_api(tmp_path, monkeypatch)
    client.put("/api/model/configuration", json=payload())

    response = client.put("/api/model/configuration", json=payload(apiKey="", model="deepseek-reasoner"))

    assert response.status_code == 200
    assert store.get().apiKey == "secret-token"
    assert store.get().model == "deepseek-reasoner"


def test_model_adapter_uses_request_configuration_snapshot(tmp_path, monkeypatch):
    from backend.app import main
    from backend.app.models import RoleInput, RoleProfile
    from backend.app.role_store import RoleStore
    from backend.app.session_manager import SessionManager
    from backend.app.session_store import SessionStore
    from backend.app.storage import initialize_databases

    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="测试角色", profile=RoleProfile(profile="测试设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    config_store = ModelConfigurationStore(tmp_path / "data" / "model-config.json")
    old = config_store.save(ModelConfigurationInput(**payload()))
    seen = []

    class SnapshotAdapter:
        async def stream_reply(self, role, history, configuration=None):
            seen.append(configuration)
            yield "回复"
            if len(seen) == 1:
                config_store.save(ModelConfigurationInput(**payload(model="new-model")))

    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", SessionManager(roles, sessions))
    monkeypatch.setattr(main, "model_configuration_store", config_store)
    monkeypatch.setattr(main, "model_adapter", SnapshotAdapter())
    monkeypatch.setattr(main, "role_locks", {})
    client = TestClient(main.app)

    assert "assistant_completed" in client.post(f"/api/roles/{role.id}/messages", json={"content": "第一条"}).text
    assert "assistant_completed" in client.post(f"/api/roles/{role.id}/messages", json={"content": "第二条"}).text
    assert seen == [old, config_store.get()]
    assert seen[0].model == "deepseek-chat"
    assert seen[1].model == "new-model"
