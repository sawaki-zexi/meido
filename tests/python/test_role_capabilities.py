import sys
import os

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


@pytest.mark.parametrize("granted", [False, True])
def test_owner_mail_permission_controls_real_chat_and_revokes_frozen_tool(role_api, tmp_path, monkeypatch, granted):
    from backend.app.mail.agent import MailManageTool
    from backend.app.mail.models import MailConnection
    from backend.app.mail.service import MailService
    from backend.app.mail.store import MailStore

    mail_store = MailStore(tmp_path / "mail.db")
    mail_store.save_connection(MailConnection("imap.qq.com", 993, "owner@qq.com", verified=True, auto_draft_ready=True))
    service = MailService(mail_store)
    service.update_preferences(writing_style="", role_access=granted)
    monkeypatch.setattr(main, "mail_service", service)
    monkeypatch.setattr(main, "mail_manage_tool", MailManageTool(service))

    class Adapter:
        calls = 0
        schemas = None

        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            if self.calls == 1:
                self.schemas = tools
                yield ModelToolCall("mail-first", "mail.manage", {"action": "status"})
            elif self.calls == 2 and granted:
                service.update_preferences(writing_style="", role_access=False)
                yield ModelToolCall("mail-revoked", "mail.manage", {"action": "status"})
            else:
                yield ModelTextDelta("邮件权限已核对")

    adapter = Adapter()
    monkeypatch.setattr(main, "model_adapter", adapter)
    role = role_api.post("/api/roles", json={"name": "邮件助手", "profile": {"profile": "设定"},
        "agentConfig": {"enabledTools": ["mail.manage"]}}).json()["role"]
    response = role_api.post(f"/api/roles/{role['id']}/messages", json={"content": "帮我查看邮件状态"})
    assert "assistant_completed" in response.text
    names = {tool["function"]["name"] for tool in adapter.schemas}
    assert ("mail.manage" in names) is granted
    results = [row for row in main.session_store.runtime_messages(f"role:{role['id']}") if isinstance(row, ToolResultMessage)]
    assert results[0].is_error is (not granted)
    if granted:
        assert results[1].is_error


@pytest.fixture
def role_api(tmp_path, monkeypatch, local_platform_only):
    from backend.app.task_runtime import TaskRuntime

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
    tasks = TaskRuntime(tmp_path / "tasks")
    monkeypatch.setattr(main, "task_runtime", tasks)
    try:
        yield TestClient(main.app)
    finally:
        tasks.close()


def test_legacy_empty_config_has_default_tools_without_enabling_special_plugins(role_api):
    role = role_api.post('/api/roles', json={
        'name': '默认工具', 'profile': {'profile': '设定'},
        'agentConfig': {'enabledTools': [], 'shell': {'enabled': False, 'allowedCommands': []}},
    }).json()['role']
    catalog = role_api.get(f"/api/roles/{role['id']}/tools").json()
    visible = {item['name'] for item in catalog['tools'] if item['modelVisible']}
    assert {'read_file', 'list_dir', 'write_file', 'edit_file', 'web_search', 'web_fetch',
            'search_messages', 'fetch_messages', 'tool_search',
            'shell', 'task_output', 'task_stop', 'message_push', 'spawn', 'spawn_manage',
            'schedule', 'list_schedules', 'cancel_schedule'} <= visible
    assert not {'agent_browser_open', 'computer_click', 'generate_image', 'pet_action', 'observe_screen'} & visible
    for name in ('memorize', 'forget_memory'):
        item = next(item for item in catalog['tools'] if item['name'] == name)
        assert item['enabled'] and item['reason'] == '全局记忆已关闭'
    # Saving unrelated profile data or an old client's empty list cannot hide defaults.
    saved = role_api.put(f"/api/roles/{role['id']}", json={
        'name': role['name'], 'profile': {'profile': '更新设定'},
        'agentConfig': {'enabledTools': ['custom.query'], 'enabledPlugins': ['custom']},
    }).json()['role']
    assert 'custom.query' in saved['agentConfig']['enabledTools']
    assert 'custom' in saved['agentConfig']['enabledPlugins']
    again = role_api.get(f"/api/roles/{role['id']}/tools").json()
    assert next(item for item in again['tools'] if item['name'] == 'read_file')['modelVisible']


def test_default_tools_execute_file_and_platform_shell_in_real_http_turn(role_api, tmp_path, monkeypatch):
    from backend.app.task_runtime import TaskRuntime
    tasks = TaskRuntime(tmp_path / 'owned-default-shell')
    monkeypatch.setattr(main, 'task_runtime', tasks)

    class Adapter:
        requests = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.requests += 1
            names = {item['function']['name'] for item in tools}
            assert {'write_file', 'shell', 'tool_search'} <= names
            results = [message for message in messages if message['role'] == 'tool']
            if self.requests == 1:
                yield ModelToolCall('default-file', 'write_file', {'path': 'default.txt', 'content': '默认文件'})
            elif self.requests == 2:
                assert 'Wrote' in results[-1]['content']
                command = ('$number = 20 + 22; Write-Output "默认Shell:$number"' if os.name == 'nt'
                           else 'number=$((20+22)); printf "默认Shell:%s\\n" "$number"')
                yield ModelToolCall('default-shell', 'shell', {'command': command})
            else:
                assert '默认Shell:42' in results[-1]['content']
                yield ModelTextDelta('默认工具执行完成')

    adapter = Adapter()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    role = role_api.post('/api/roles', json={'name': '默认执行', 'profile': {'profile': '设定'}}).json()['role']
    try:
        response = role_api.post(f"/api/roles/{role['id']}/messages", json={'content': '写文件并计算'})
        assert response.status_code == 200 and '默认工具执行完成' in response.text
        assert adapter.requests == 3
        assert (main.store.workspace_path(role['id']) / 'default.txt').read_text(encoding='utf-8') == '默认文件'
    finally:
        tasks.close()


@pytest.mark.parametrize("enabled,commands", [(False, [sys.executable]), (True, [])])
def test_legacy_shell_switch_and_empty_allowlist_do_not_hide_ordinary_shell(role_api, monkeypatch, enabled, commands):
    class Adapter:
        requests = []

        async def stream_structured_messages(self, messages, configuration, tools):
            self.requests.append(tools)
            if len(self.requests) == 1:
                yield ModelToolCall("default-shell", "shell", {"command": "Write-Output 123" if os.name == "nt" else "printf 123"})
            else:
                yield ModelTextDelta("拒绝后继续对话")

    adapter = Adapter()
    monkeypatch.setattr(main, "model_adapter", adapter)
    role = role_api.post("/api/roles", json={
        "name": "关闭命令工具", "profile": {"profile": "设定"},
        "agentConfig": {"shell": {"enabled": enabled, "allowedCommands": commands}, "enabledTools": ["shell"]},
    }).json()["role"]
    catalog = role_api.get(f"/api/roles/{role['id']}/tools").json()
    assert next(item for item in catalog["tools"] if item["name"] == "shell")["modelVisible"]
    response = role_api.post(f"/api/roles/{role['id']}/messages", json={"content": "试试执行命令"})
    assert response.status_code == 200
    assert "assistant_completed" in response.text
    assert "shell" in {tool["function"]["name"] for tool in adapter.requests[0]}
    results = [item for item in main.session_store.runtime_messages(f"role:{role['id']}") if isinstance(item, ToolResultMessage)]
    assert results[0].tool_name == "shell"
    assert not results[0].is_error and "123" in results[0].content


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
    assert tools["shell"]["enabled"] is True
    assert tools["shell"]["modelVisible"] is True
    assert tools["agent_browser_open"]["modelVisible"] is False
    assert tools["agent_browser_open"]["exposure"] == "deferred"
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
    schema_names = {item["function"]["name"] for item in schemas}
    assert {"read_file", "write_file", "shell", "tool_search", "spawn", "schedule", "todo.write"} <= schema_names
    assert schema_names == {item["name"] for item in catalog["tools"] if item["modelVisible"]}
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
            "sceneCgPromptInstructions": "",
            "autoWritebackRoleAssets": False, "promptTagsEnabled": False,
            "allowedPromptTagRatings": ["general"], "consentRevision": 0},
        "coding": {"enabled": False, "repoGrantIds": [], "profileIds": [], "defaultHerdrProfileId": None,
                   "defaultCodexProfileId": None, "maxAttemptsPerDay": 5}}


def test_native_model_receives_default_capabilities_without_role_opt_in(role_api, monkeypatch):
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
    assert {"read_file", "shell", "tool_search"} <= {tool["function"]["name"] for tool in tools}
    assert "read_file" in messages[0].content[0].text
