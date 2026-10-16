import sys

import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.agent_runtime.types import ToolResultMessage
from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from backend.app.model_config import ModelConfigurationStore
from backend.app.memory_config import MemorySettingsStore
from backend.app.role_store import RoleStore
from backend.app.session_manager import SessionManager
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.todo import TodoStore
from backend.app.browser_use import BrowserSessions, BrowserUseConfig
from backend.app.agent_runtime import PluginContribution, PluginManifest, PluginRegistry
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelTextBlock


@pytest.fixture
def role_api(tmp_path, monkeypatch, local_platform_only):
    initialize_databases(tmp_path / "data")
    roles = RoleStore(tmp_path / "roles")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", SessionManager(roles, sessions))
    monkeypatch.setattr(main, "model_configuration_store", ModelConfigurationStore(tmp_path / "data" / "models.json"))
    monkeypatch.setattr(main, "role_locks", {})
    memory = MemorySettingsStore(tmp_path / "data" / "memory-settings.json")
    memory.update(enabled=False)
    monkeypatch.setattr(main, "memory_settings_store", memory)
    todos = TodoStore(tmp_path / "data" / "todos.db")
    todos.update_settings(enabled=True)
    monkeypatch.setattr(main, "todo_store", todos)
    return TestClient(main.app)


@pytest.mark.parametrize("enabled,commands", [(False, [sys.executable]), (True, [])])
def test_shell_off_or_empty_allowlist_cannot_be_reenabled_by_generic_tool_list(role_api, monkeypatch, enabled, commands):
    class Adapter:
        requests = []

        async def stream_structured_messages(self, messages, configuration, tools):
            self.requests.append(tools)
            if len(self.requests) == 1:
                yield ModelToolCall("forged-shell", "shell", {"command": f'"{sys.executable}" -c "print(123)"'})
            else:
                yield ModelTextDelta("拒绝后继续对话")

    adapter = Adapter()
    monkeypatch.setattr(main, "model_adapter", adapter)
    role = role_api.post("/api/roles", json={
        "name": "关闭命令工具", "profile": {"profile": "设定"},
        "agentConfig": {"shell": {"enabled": enabled, "allowedCommands": commands}, "enabledTools": ["shell"]},
    }).json()["role"]
    catalog = role_api.get(f"/api/roles/{role['id']}/tools").json()
    assert not next(item for item in catalog["tools"] if item["name"] == "shell")["modelVisible"]
    response = role_api.post(f"/api/roles/{role['id']}/messages", json={"content": "试试执行命令"})
    assert response.status_code == 200
    assert "assistant_completed" in response.text
    assert "shell" not in {tool["function"]["name"] for tool in adapter.requests[0]}
    results = [item for item in main.session_store.runtime_messages(f"role:{role['id']}") if isinstance(item, ToolResultMessage)]
    assert results[0].tool_name == "shell"
    assert results[0].is_error


@pytest.mark.parametrize("granted", [False, True])
def test_tool_directory_explains_disabled_and_missing_dependencies_without_initializing_plugins(role_api, tmp_path, monkeypatch, granted):
    registry = PluginRegistry()
    main.register_todo_agent_plugin(registry)
    main.register_browser_use_plugin(registry)

    def forbidden_setup(context):
        pytest.fail("tool directory must not initialize plugin factories")
        return PluginContribution()

    registry.register(PluginManifest("custom", "1.0.0", requested_capabilities=("memory.read",), declared_tools=("custom.run",)), forbidden_setup)
    monkeypatch.setattr(main, "plugin_registry", registry)
    sessions = BrowserSessions(tmp_path / "browser", BrowserUseConfig(), resources=tmp_path / "missing-browser")
    monkeypatch.setattr(main, "browser_use_sessions", sessions)
    role = role_api.post("/api/roles", json={
        "name": "工具目录", "profile": {"profile": "设定"},
        "agentConfig": {"browserUse": {"enabled": True}, "enabledPlugins": ["custom"], "enabledTools": ["custom.run"], "grantedCapabilities": ["memory.read"] if granted else []},
    }).json()["role"]
    workspace = main.store.root / role["id"] / "workspace"
    workspace.rmdir()
    response = role_api.get(f"/api/roles/{role['id']}/tools")
    assert response.status_code == 200
    catalog = response.json()
    tools = {item["name"]: item for item in catalog["tools"]}
    assert tools["shell"]["enabled"] is False
    assert tools["shell"]["modelVisible"] is False
    assert tools["agent_browser_open"]["modelVisible"] is True
    assert tools["agent_browser_open"]["dependency"]["status"] == "unavailable"
    assert tools["agent_browser_open"]["dependency"]["reason"]
    assert tools["custom.run"]["modelVisible"] is (None if granted else False)
    assert tools["custom.run"]["dependency"]["status"] == "unchecked"
    assert {plugin["id"] for plugin in catalog["plugins"]} == {"todo", "browser_use", "custom"}
    assert not workspace.exists()
    assert not (tmp_path / "browser").exists()
    assert role_api.get("/api/roles/missing/tools").status_code == 404


def test_model_capability_description_matches_actual_schemas_and_preserves_role_profile(role_api, monkeypatch):
    class Adapter:
        request = None

        async def stream_structured_messages(self, messages, configuration, tools):
            self.request = (messages, tools)
            yield ModelTextDelta("我会依据实际工具回答")

    adapter = Adapter()
    monkeypatch.setattr(main, "model_adapter", adapter)
    role = role_api.post("/api/roles", json={
        "name": "能力说明", "profile": {"profile": "核心身份设定"},
        "agentConfig": {
            "shell": {"enabled": True, "allowedCommands": [sys.executable]},
            "enabledTools": ["search_owner_knowledge", "mail.manage", "account_list"],
        },
    }).json()["role"]
    catalog = role_api.get(f"/api/roles/{role['id']}/tools").json()
    assert role_api.post(f"/api/roles/{role['id']}/messages", json={"content": "你能调用什么工具"}).status_code == 200
    messages, schemas = adapter.request
    prompt = messages[0]["content"]
    assert "[本次运行工具]" in prompt
    assert "shell" in prompt
    assert "todo.write" in prompt
    assert "agent_browser_open" not in prompt
    assert {item["function"]["name"] for item in schemas} == {"shell", "task_output", "task_stop", "todo.write", "todo.reminder_control"}
    assert {item["name"] for item in catalog["tools"] if item["modelVisible"]} == {"shell", "task_output", "task_stop", "todo.write", "todo.reminder_control"}
    assert role_api.get(f"/api/roles/{role['id']}").json()["role"]["profile"]["profile"] == "核心身份设定"


def test_saved_shell_and_capability_settings_survive_role_store_restart(role_api, tmp_path, monkeypatch):
    config = {
        "shell": {"enabled": True, "allowedCommands": [sys.executable], "timeoutSeconds": 47, "maxOutputChars": 32100},
        "memoryRecall": {"enabled": True}, "browserUse": {"enabled": False},
        "spawn": {"enabled": True, "timeoutSeconds": 45.0, "maxTurns": 5, "maxOutputChars": 12345, "maxModelOutputChars": 54321},
        "enabledTools": ["custom.query"], "enabledPlugins": ["custom"], "grantedCapabilities": ["memory.read"],
    }
    created = role_api.post("/api/roles", json={"name": "原名", "profile": {"profile": "设定"}, "agentConfig": config}).json()["role"]
    response = role_api.put(f"/api/roles/{created['id']}", json={"name": "新名", "profile": {"profile": "更新设定"}, "agentConfig": config})
    assert response.status_code == 200
    monkeypatch.setattr(main, "store", RoleStore(tmp_path / "roles"))
    reopened = role_api.get(f"/api/roles/{created['id']}").json()["role"]
    assert reopened["name"] == "新名"
    assert reopened["agentConfig"] == {**config, "screenPerception": {"enabled": False, "visionModelConfigurationId": None},
        "computerUse": {"enabled": False},
        "novelai": {"manualEnabled": False, "manualDailyAttempts": 10, "sceneCgEnabled": False,
            "sceneCgDailyAttempts": 3, "scenePromptModelConfigurationId": None,
            "autoWritebackRoleAssets": False, "consentRevision": 0}}


def test_native_model_without_tools_receives_an_explicit_empty_capability_list(role_api, monkeypatch):
    class Adapter:
        request = None

        async def stream_rich_messages(self, messages, configuration, tools):
            self.request = (messages, tools)
            yield ModelStreamDone(ModelAssistantMessage((ModelTextBlock("当前没有启用工具"),)))

    adapter = Adapter()
    monkeypatch.setattr(main, "model_adapter", adapter)
    role = role_api.post("/api/roles", json={"name": "空工具", "profile": {"profile": "设定"}}).json()["role"]
    response = role_api.post(f"/api/roles/{role['id']}/messages", json={"content": "你有什么工具"})
    assert "assistant_completed" in response.text
    messages, tools = adapter.request
    assert not tools
    assert "当前提供给模型的工具：无" in messages[0].content[0].text
