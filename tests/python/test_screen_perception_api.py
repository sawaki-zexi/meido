import json
import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from backend.app import main
from backend.app.agent_runtime import RuntimeManager
from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
from backend.app.model_config import ModelConfigurationStore
from backend.app.memory_config import MemorySettingsStore
from backend.app.models import ModelConfigurationInput
from backend.app.role_store import RoleStore
from backend.app.session_manager import SessionManager

from test_screen_perception import observation_host, vision_server


def test_screen_catalog_explains_closed_and_missing_binding_without_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "store", RoleStore(tmp_path / "roles"))
    monkeypatch.setattr(main, "model_configuration_store", ModelConfigurationStore(tmp_path / "models.json"))
    client = TestClient(main.app)
    role = client.post("/api/roles", json={"name": "observer", "profile": {"profile": "role"}, "agentConfig": {"enabledTools": ["observe_screen"]}}).json()["role"]
    response = client.get(f"/api/roles/{role['id']}/tools")
    assert response.status_code == 200
    catalog = response.json()
    entry = next(item for item in catalog["tools"] if item["name"] == "observe_screen")
    assert entry["registered"] is True
    assert entry["authorized"] is False
    assert "关闭" in json.dumps(entry, ensure_ascii=False)


def test_role_screen_binding_defaults_and_omitted_update_survive_reload(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    models = ModelConfigurationStore(tmp_path / "models.json")
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "model_configuration_store", models)
    client = TestClient(main.app)
    profile = {"name": "观察者", "profile": {"profile": "角色设定"}}
    role = client.post("/api/roles", json=profile).json()["role"]
    assert role["agentConfig"]["screenPerception"] == {"enabled": False, "visionModelConfigurationId": None}
    missing = {"enabled": True, "visionModelConfigurationId": "missing"}
    assert client.put(f"/api/roles/{role['id']}", json={**profile, "agentConfig": {"screenPerception": missing}}).status_code == 422
    configuration = client.post("/api/model/configurations", json={"providerId": "custom", "provider": "fixture", "baseUrl": "http://127.0.0.1:9/v1", "model": "vision", "apiKey": "fixture-key"}).json()["configuration"]
    binding = {"enabled": True, "visionModelConfigurationId": configuration["id"]}
    result = client.put(f"/api/roles/{role['id']}", json={**profile, "agentConfig": {"screenPerception": binding}})
    assert result.status_code == 200, result.text
    # An old client sends another AgentToolConfig but omits this new setting.
    result = client.put(f"/api/roles/{role['id']}", json={**profile, "agentConfig": {"enabledTools": ["web_fetch"]}})
    assert result.json()["role"]["agentConfig"]["screenPerception"] == binding
    assert RoleStore(roles.root).get(role["id"]).agentConfig.screenPerception.model_dump() == binding
    disabled = {**binding, "enabled": False}
    assert client.put(f"/api/roles/{role['id']}", json={**profile, "agentConfig": {"screenPerception": disabled}}).json()["role"]["agentConfig"]["screenPerception"] == disabled
    assert client.put(f"/api/roles/{role['id']}", json={**profile, "agentConfig": {"screenPerception": {"enabled": True}}}).json()["role"]["agentConfig"]["screenPerception"] == binding
    assert client.put(f"/api/roles/{role['id']}", json={**profile, "agentConfig": {"screenPerception": {"enabled": False}}}).json()["role"]["agentConfig"]["screenPerception"] == disabled


def test_saved_local_vision_connection_can_use_existing_no_key_auth_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "store", RoleStore(tmp_path / "roles"))
    monkeypatch.setattr(main, "model_configuration_store", ModelConfigurationStore(tmp_path / "models.json"))
    client = TestClient(main.app)
    config = client.post("/api/model/configurations", json={"providerId": "custom", "provider": "local", "baseUrl": "http://127.0.0.1:9/v1", "model": "vision"}).json()["configuration"]
    response = client.post("/api/roles", json={"name": "observer", "profile": {"profile": "role"}, "agentConfig": {"screenPerception": {"enabled": True, "visionModelConfigurationId": config["id"]}}})
    assert response.status_code == 201, response.text


def setup_screen_api(tmp_path, monkeypatch, vision_url, *, code=None):
    service, tool, context, roles, models, sessions, processes = observation_host(tmp_path, vision_url, code=code)
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "model_configuration_store", models)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", SessionManager(roles, sessions))
    monkeypatch.setattr(main, "runtime_manager", RuntimeManager(sessions))
    monkeypatch.setattr(main, "role_locks", {})
    monkeypatch.setattr(main, "screen_perception", service)
    from backend.app.plugin_configuration import PluginConfigurationStore
    globals_ = PluginConfigurationStore(tmp_path / 'plugins.json')
    state = globals_.prepare('screen', expected_revision=0, enabled=True, values={
        'visionModelConfigurationId': roles.get(context.role_id).agentConfig.screenPerception.visionModelConfigurationId})
    globals_.finish('screen', state.revision)
    monkeypatch.setattr(main, 'plugin_configuration', globals_)
    memory_settings = MemorySettingsStore(tmp_path / "memory-settings.json")
    memory_settings.update(enabled=False, embedding_enabled=False)
    monkeypatch.setattr(main, "memory_settings_store", memory_settings)
    # Platform discovery is an OS dependency; capture remains a real fixture process.
    monkeypatch.setattr(main, "sys", SimpleNamespace(platform="win32"))
    return service, tool, context, roles, models, sessions, processes


def test_http_chat_next_provider_and_persisted_history_receive_only_validated_summary(tmp_path, monkeypatch):
    with vision_server() as (vision_url, vision_requests), vision_server(chat=True) as (chat_url, chat_requests):
        _, _, context, roles, models, sessions, processes = setup_screen_api(tmp_path, monkeypatch, vision_url)
        sessions.update_run(context.run_id, status="completed")
        chat_configuration = models.create(ModelConfigurationInput(providerId="custom", provider="chat", baseUrl=chat_url, model="chat-only", apiKey="chat-key"))
        roles.set_model_configuration(context.role_id, chat_configuration.id)
        monkeypatch.setattr(main, "model_adapter", RoutedModelAdapter(ModelAdapterRegistry(timeout=12)))
        client = TestClient(main.app)
        response = client.post(f"/api/roles/{context.role_id}/messages", json={"content": "observe the fixture"})
        assert response.status_code == 200 and "assistant_completed" in response.text, response.text
    assert len(vision_requests) == 1
    assert len(chat_requests) == 2
    next_payload = chat_requests[1][1]
    tool_result = next(item for item in next_payload["messages"] if item["role"] == "tool")
    assert json.loads(tool_result["content"]) == {"available": True, "interface_summary": "fixture editor", "activity_key": "writing"}
    transcript = json.dumps(chat_requests) + response.text
    history = sessions.list_messages(context.session_key)
    transcript += json.dumps([item.model_dump(mode="json") for item in history])
    run = next(item for item in history if item.role == "user" and item.content == "observe the fixture").runId
    transcript += json.dumps(sessions.list_tool_audits(run))
    assert "data:image" not in transcript and "iVBOR" not in transcript
    assert "screenPerceptionSource" not in json.dumps(chat_requests)
    assert processes[0].returncode == 0
    assert not list(tmp_path.rglob("*.png"))


@pytest.mark.parametrize("mutation", ["disable", "binding", "model", "delete_model"])
def test_http_configuration_mutation_waits_for_capture_and_revokes_old_candidate(tmp_path, monkeypatch, mutation):
    async def run():
        _, tool, context, roles, models, _, processes = setup_screen_api(tmp_path, monkeypatch, "http://127.0.0.1:9/v1", code="import time;time.sleep(30)")
        role = roles.get(context.role_id)
        config = models.get(role.agentConfig.screenPerception.visionModelConfigurationId)
        task = asyncio.create_task(tool.execute({}, context))
        while not processes:
            await asyncio.sleep(0.01)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://meido") as client:
            if mutation in {"disable", "binding"}:
                screen = role.agentConfig.screenPerception.model_dump()
                if mutation == "disable":
                    screen["enabled"] = False
                else:
                    second = models.create(ModelConfigurationInput(**config.model_dump(exclude={"id"})))
                    screen["visionModelConfigurationId"] = second.id
                response = await client.put(f"/api/roles/{role.id}", json={"name": role.name, "profile": role.profile.model_dump(), "agentConfig": {"screenPerception": screen}})
            elif mutation == "model":
                response = await client.put(f"/api/model/configurations/{config.id}", json={**config.model_dump(exclude={"id"}), "model": "changed-model"})
            else:
                response = await client.delete(f"/api/model/configurations/{config.id}")
        assert response.status_code == 200, response.text
        assert processes[0].returncode is not None
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await tool.execute({}, context)).is_error
        assert len(processes) == 1
    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["disable", "model", "delete_model"])
def test_http_mutation_closes_inflight_vision_stream_before_success(tmp_path, monkeypatch, mutation):
    with vision_server(hang=True) as (url, requests):
        async def run():
            _, tool, context, roles, models, _, processes = setup_screen_api(tmp_path, monkeypatch, url)
            role = roles.get(context.role_id)
            config = models.get(role.agentConfig.screenPerception.visionModelConfigurationId)
            task = asyncio.create_task(tool.execute({}, context))
            while not requests:
                await asyncio.sleep(0.01)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://meido") as client:
                if mutation == "disable":
                    response = await client.put(f"/api/roles/{role.id}", json={"name": role.name, "profile": role.profile.model_dump(), "agentConfig": {"screenPerception": {"enabled": False, "visionModelConfigurationId": config.id}}})
                elif mutation == "model":
                    response = await client.put(f"/api/model/configurations/{config.id}", json={**config.model_dump(exclude={"id"}), "apiKey": "new-key"})
                else:
                    response = await client.delete(f"/api/model/configurations/{config.id}")
            assert response.status_code == 200
            assert processes[0].returncode == 0
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await asyncio.to_thread(requests.disconnected.wait, 2)
        asyncio.run(run())


def test_http_stop_reaps_capture_before_role_can_be_deleted(tmp_path, monkeypatch):
    from backend.app.channels import ChannelHost
    from backend.app.channels.consolidation import ChannelMemoryMaintenance
    from backend.app.channels.group_tools import MultichannelGroupRepository
    from backend.app.memory_service import MemoryService, MemoryWorker
    from backend.app.memory_store import MemoryStore
    from backend.app.multichannel import MultichannelStore
    from backend.app.owner_knowledge import OwnerKnowledgeStore
    from backend.app.task_runtime import TaskRuntime
    from backend.app.todo import TodoStore

    class Credentials:
        def get(self, _key):
            return ""

        def set(self, _key, _value):
            pass

    with vision_server(chat=True) as (chat_url, _):
        _, _, context, roles, models, sessions, processes = setup_screen_api(tmp_path, monkeypatch, "http://127.0.0.1:9/v1", code="import time;time.sleep(30)")
        sessions.update_run(context.run_id, status="completed")
        chat_configuration = models.create(ModelConfigurationInput(providerId="custom", provider="chat", baseUrl=chat_url, model="chat-only", apiKey="chat-key"))
        roles.set_model_configuration(context.role_id, chat_configuration.id)
        monkeypatch.setattr(main, "model_adapter", RoutedModelAdapter(ModelAdapterRegistry(timeout=12)))
        memories = MemoryStore(tmp_path / "memories.db")
        monkeypatch.setattr(main, "memory_store", memories)
        monkeypatch.setattr(main, "memory_worker", MemoryWorker(MemoryService(memories)))
        monkeypatch.setattr(main, "owner_knowledge_store", OwnerKnowledgeStore(tmp_path / "owner.db"))
        channels = MultichannelStore(sessions.database_path, sessions, Credentials())
        groups = MultichannelGroupRepository(channels)
        monkeypatch.setattr(main, "multichannel_store", channels)
        monkeypatch.setattr(main, "group_repository", groups)
        monkeypatch.setattr(main, "channel_host", ChannelHost(channels))
        monkeypatch.setattr(main, "channel_memory_maintenance", ChannelMemoryMaintenance(lambda: channels, lambda: groups, None, None))
        monkeypatch.setattr(main, "todo_store", TodoStore(tmp_path / "todos.db"))
        tasks = TaskRuntime(tmp_path / "tasks")
        monkeypatch.setattr(main, "task_runtime", tasks)

        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://meido") as client:
                sending = asyncio.create_task(client.post(f"/api/roles/{context.role_id}/messages", json={"content": "observe the fixture"}))
                async with asyncio.timeout(5):
                    while not processes:
                        await asyncio.sleep(0.01)
                active = sessions.active_run(context.role_id)
                assert (await client.delete(f"/api/roles/{context.role_id}")).status_code == 409
                response = await client.post(f"/api/roles/{context.role_id}/runs/{active.runId}/stop")
                assert response.status_code == 200
                assert processes[0].returncode is not None
                assert sessions.get_run(active.runId).status == "cancelled"
                await asyncio.gather(sending, return_exceptions=True)
                deleted = await client.delete(f"/api/roles/{context.role_id}")
                assert deleted.status_code == 204, deleted.text
                assert roles.get(context.role_id) is None
        try:
            asyncio.run(run())
        finally:
            tasks.close()
