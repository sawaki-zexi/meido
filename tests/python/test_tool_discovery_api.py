import json
import asyncio
import httpx
import shlex
import os
import shutil
import sys
import pytest

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.browser_use import BrowserSessions, BrowserUseConfig
from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from backend.app.model_config import ModelConfigurationStore
from backend.app.role_store import RoleStore
from backend.app.session_manager import SessionManager
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.multichannel import MultichannelStore
from backend.app.channels.host import ChannelHost
from backend.app.memory_config import MemorySettingsStore
from backend.app.task_runtime import TaskRuntime


@pytest.fixture(autouse=True)
def enable_browser_plugin_for_discovery_tests(tmp_path, monkeypatch):
    """These discovery tests exercise a browser-enabled role explicitly."""
    from backend.app.plugin_configuration import PluginConfigurationStore

    plugins = PluginConfigurationStore(tmp_path / "plugins.json")
    state = plugins.prepare("browser", expected_revision=0, enabled=True)
    plugins.finish("browser", state.revision)
    monkeypatch.setattr(main, "plugin_configuration", plugins)


@pytest.fixture
def owned_tasks(tmp_path, monkeypatch):
    tasks = TaskRuntime(tmp_path / "tasks")
    monkeypatch.setattr(main, "task_runtime", tasks)
    yield tasks
    tasks.close()


@pytest.mark.skipif(os.name == "nt" and shutil.which("pwsh") is None,
                    reason="default Windows Shell requires PowerShell 7")
@pytest.mark.parametrize("change", ["remove", "add", "retain"])
def test_http_same_run_shell_uses_default_runtime_after_role_config_change(
    tmp_path, monkeypatch, change, owned_tasks
):
    roles = RoleStore(tmp_path / "roles")
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data/sessions.db")
    settings = MemorySettingsStore(tmp_path / "data/memory-settings.json")
    settings.update(enabled=False)
    for name, value in {
        "store": roles,
        "session_store": sessions,
        "session_manager": SessionManager(roles, sessions),
        "model_configuration_store": ModelConfigurationStore(
            tmp_path / "data/models.json"
        ),
        "role_locks": {},
        "memory_settings_store": settings,
    }.items():
        monkeypatch.setattr(main, name, value)
    client = TestClient(main.app)
    role = client.post(
        "/api/roles",
        json={
            "name": "收紧命令授权",
            "profile": {"profile": "设定"},
            "agentConfig": {
                "enabledTools": ["tool_search"],
                "shell": {
                    "enabled": True,
                    "allowedCommands": ["whoami"]
                    if change == "add"
                    else [sys.executable, "whoami"],
                },
            },
        },
    ).json()["role"]
    workspace = roles.workspace_path(role["id"])
    effect = workspace / "revoked-command.txt"
    if os.name == "nt":
        # The ordinary Shell tool invokes PowerShell on Windows.  Keep the
        # test command valid for that parser instead of passing POSIX argv
        # quoting through PowerShell.
        command = "Set-Content -Path 'revoked-command.txt' -Value 'revoked-command-ran'"
    else:
        command = shlex.join(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('revoked-command.txt').write_text('revoked-command-ran')",
            ]
        )

    class Adapter:
        count = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.count += 1
            if self.count == 1:
                changed = client.put(
                    f"/api/roles/{role['id']}",
                    json={
                        "name": role["name"],
                        "profile": role["profile"],
                        "agentConfig": {
                            "enabledTools": ["tool_search"],
                            "shell": {
                                "enabled": True,
                                "allowedCommands": ["whoami"]
                                if change == "remove"
                                else [sys.executable],
                            },
                        },
                    },
                )
                assert changed.status_code == 200
                yield ModelToolCall("removed-command", "shell", {"command": command})
            else:
                yield ModelTextDelta("命令权限已收紧")

    monkeypatch.setattr(main, "model_adapter", Adapter())
    response = client.post(
        f"/api/roles/{role['id']}/messages", json={"content": "执行命令"}
    )
    assert "命令权限已收紧" in response.text
    # Shell is a default built-in command runner.  The legacy allowlist is
    # retained for restricted child tasks, while an ordinary run only checks
    # the current role-level shell switch.
    assert effect.exists()
    result = next(
        item
        for item in sessions.list_messages(f"role:{role['id']}")
        if item.toolCallId == "removed-command" and item.messageType == "tool_result"
    )
    assert not result.isError
    assert result.toolResult["status"] == "succeeded"


@pytest.mark.parametrize("query", ["select:agent_browser_get_title", "填表 title"])
def test_http_search_loads_browser_schema_in_same_run_and_audits_actual_versions(
    tmp_path, monkeypatch, local_platform_only, query
):
    class BrowserDriver:
        async def call(self, name, arguments, **kwargs):
            return {"text": "Actual driver title"}

        async def close(self):
            pass

    class Adapter:
        count = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.count += 1
            names = {item["function"]["name"] for item in tools}
            if self.count == 1:
                assert "tool_search" in names and "agent_browser_get_title" not in names
                yield ModelToolCall(
                    "search-browser",
                    "tool_search",
                    {"query": query, "top_k": 1},
                )
                yield ModelToolCall("premature-browser", "agent_browser_get_title", {})
            elif self.count == 2:
                assert "agent_browser_get_title" in names
                results = [item for item in messages if item["role"] == "tool"]
                assert json.loads(results[-2]["content"])["unlocked"] == [
                    "agent_browser_get_title"
                ]
                assert "tool_search" in results[-1]["content"]
                yield ModelToolCall("title-browser", "agent_browser_get_title", {})
            else:
                assert messages[-1]["content"] == "Actual driver title"
                yield ModelTextDelta("已在同次对话发现并调用")

    roles = RoleStore(tmp_path / "roles")
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data/sessions.db")
    memory_settings = MemorySettingsStore(tmp_path / 'data/memory-settings.json')
    memory_settings.update(enabled=False)
    monkeypatch.setattr(main, 'memory_settings_store', memory_settings)
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", SessionManager(roles, sessions))
    monkeypatch.setattr(main, "model_adapter", Adapter())
    monkeypatch.setattr(
        main,
        "model_configuration_store",
        ModelConfigurationStore(tmp_path / "data/models.json"),
    )
    monkeypatch.setattr(main, "role_locks", {})
    monkeypatch.setattr(
        main,
        "browser_use_sessions",
        BrowserSessions(
            tmp_path / "browser", BrowserUseConfig(), transport=BrowserDriver()
        ),
    )
    client = TestClient(main.app)
    role = client.post(
        "/api/roles",
        json={
            "name": "发现工具",
            "profile": {"profile": "设定"},
            "agentConfig": {
                "browserUse": {"enabled": True},
            },
        },
    ).json()["role"]
    before = client.get(f"/api/roles/{role['id']}/tools").json()
    assert next(item for item in before["tools"] if item["name"] == "tool_search")["enabled"]
    saved = client.put(f"/api/roles/{role['id']}", json={
        "name": role["name"], "profile": role["profile"],
        "agentConfig": {"enabledTools": ["tool_search"], "browserUse": {"enabled": True}},
    })
    assert saved.status_code == 200
    assert client.get(f"/api/roles/{role['id']}").json()["role"]["agentConfig"]["enabledTools"] == ["tool_search"]
    catalog = client.get(f"/api/roles/{role['id']}/tools").json()
    title = next(
        item for item in catalog["tools"] if item["name"] == "agent_browser_get_title"
    )
    assert title["authorized"] and not title["modelVisible"]
    response = client.post(
        f"/api/roles/{role['id']}/messages", json={"content": "读取浏览器标题"}
    )
    assert "已在同次对话发现并调用" in response.text
    messages = sessions.list_messages(f"role:{role['id']}")
    run_id = next(
        item.runId for item in messages if item.toolCallId == "search-browser"
    )
    audits = {item["callId"]: item for item in sessions.list_tool_audits(run_id)}
    versions = sessions.get_run(run_id).modelSnapshot["capabilityRevisions"]
    assert (
        audits["search-browser"]["snapshotId"]
        == audits["premature-browser"]["snapshotId"]
        == versions[0]["snapshotId"]
    )
    assert audits["title-browser"]["snapshotId"] == versions[1]["snapshotId"]
    assert audits["premature-browser"]["resultCategory"] == "denied"
    assert audits["premature-browser"]["toolSource"] == "plugin:browser_use"
    # Reopen the persistent store after revocation; old activation evidence must
    # never restore browser authority into the new run.
    updated = client.put(
        f"/api/roles/{role['id']}",
        json={
            "name": role["name"],
            "profile": role["profile"],
            "agentConfig": {
                "enabledTools": ["tool_search"],
                "browserUse": {"enabled": False},
            },
        },
    )
    assert updated.status_code == 200
    plugin_state = client.get("/api/plugin-management/browser").json()
    disabled_plugin = client.put("/api/plugin-management/browser/enabled", json={
        "enabled": False, "expectedRevision": plugin_state["revision"],
    })
    assert disabled_plugin.status_code == 200
    reopened = SessionStore(tmp_path / "data/sessions.db")
    monkeypatch.setattr(main, "session_store", reopened)
    monkeypatch.setattr(main, "session_manager", SessionManager(roles, reopened))

    class RevokedAdapter:
        count = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.count += 1
            assert "agent_browser_get_title" not in {
                item["function"]["name"] for item in tools
            }
            if self.count == 1:
                yield ModelToolCall(
                    "search-revoked",
                    "tool_search",
                    {"query": "select:agent_browser_get_title"},
                )
            elif self.count == 2:
                assert json.loads(messages[-1]["content"])["unlocked"] == []
                yield ModelToolCall("call-revoked", "agent_browser_get_title", {})
            else:
                assert (
                    "denied" in messages[-1]["content"]
                    or "unknown" in messages[-1]["content"]
                )
                yield ModelTextDelta("重启后仍已撤权")

    monkeypatch.setattr(main, "model_adapter", RevokedAdapter())
    assert (
        "重启后仍已撤权"
        in client.post(
            f"/api/roles/{role['id']}/messages", json={"content": "再试旧工具"}
        ).text
    )


@pytest.mark.parametrize("repeat_ids", [False, True])
def test_http_discovers_non_plugin_account_tool_from_real_role_account_store(
    tmp_path, monkeypatch, repeat_ids
):
    class Adapter:
        count = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.count += 1
            names = {item["function"]["name"] for item in tools}
            if self.count == 1:
                assert "account_list" not in names
                yield ModelToolCall(
                    "provider-call" if repeat_ids else "find-account",
                    "tool_search",
                    {"query": "账号", "allowed_risk": ["read-only"]},
                )
            elif self.count == 2:
                assert "account_list" in names
                yield ModelToolCall(
                    "provider-call" if repeat_ids else "list-account",
                    "account_list",
                    {},
                )
            else:
                accounts = json.loads(messages[-1]["content"])
                assert accounts[0]["accountId"] == "test-account"
                if repeat_ids:
                    assert [
                        message["tool_call_id"]
                        for message in messages
                        if message["role"] == "tool"
                    ] == ["provider-call", "provider-call"]
                    assert [
                        call["id"]
                        for message in messages
                        for call in message.get("tool_calls", [])
                    ] == ["provider-call", "provider-call"]
                yield ModelTextDelta("已读取当前角色真实账号")

    roles = RoleStore(tmp_path / "roles")
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data/sessions.db")
    channels = MultichannelStore(tmp_path / "data/sessions.db", sessions)
    settings = MemorySettingsStore(tmp_path / "data/memory-settings.json")
    settings.update(enabled=False)
    for name, value in {
        "store": roles,
        "session_store": sessions,
        "session_manager": SessionManager(roles, sessions),
        "model_adapter": Adapter(),
        "model_configuration_store": ModelConfigurationStore(
            tmp_path / "data/models.json"
        ),
        "role_locks": {},
        "multichannel_store": channels,
        "channel_host": ChannelHost(channels),
        "memory_settings_store": settings,
    }.items():
        monkeypatch.setattr(main, name, value)
    client = TestClient(main.app)
    role = client.post(
        "/api/roles",
        json={
            "name": "账号发现",
            "profile": {"profile": "设定"},
            "agentConfig": {"enabledTools": ["tool_search"]},
        },
    ).json()["role"]
    channels.register_channel_account(
        role["id"],
        "qq",
        account_id="test-account",
        display_name="测试账号",
        enabled=True,
    )
    response = client.post(
        f"/api/roles/{role['id']}/messages", json={"content": "查看已配置账号"}
    )
    assert "已读取当前角色真实账号" in response.text
    if repeat_ids:
        results = [
            item
            for item in sessions.list_messages(f"role:{role['id']}")
            if item.messageType == "tool_result"
        ]
        assert len({item.toolCallId for item in results}) == 2
        audits = sessions.list_tool_audits(results[0].runId)
        assert len(audits) == 2 and len({item["snapshotId"] for item in audits}) == 2


def test_http_stop_after_discovery_keeps_versions_durable_without_executing_new_tool(
    tmp_path, monkeypatch
):
    roles = RoleStore(tmp_path / "roles")
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data/sessions.db")
    settings = MemorySettingsStore(tmp_path / "data/memory-settings.json")
    settings.update(enabled=False)
    for name, value in {
        "store": roles,
        "session_store": sessions,
        "session_manager": SessionManager(roles, sessions),
        "model_configuration_store": ModelConfigurationStore(
            tmp_path / "data/models.json"
        ),
        "role_locks": {},
        "memory_settings_store": settings,
    }.items():
        monkeypatch.setattr(main, name, value)

    async def execute():
        ready = asyncio.Event()

        class Adapter:
            count = 0

            async def stream_structured_messages(self, messages, configuration, tools):
                self.count += 1
                if self.count == 1:
                    yield ModelToolCall(
                        "load-before-stop",
                        "tool_search",
                        {"query": "select:agent_browser_get_title"},
                    )
                else:
                    assert "agent_browser_get_title" in {
                        item["function"]["name"] for item in tools
                    }
                    ready.set()
                    await asyncio.Event().wait()

        monkeypatch.setattr(main, "model_adapter", Adapter())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://meido.test"
        ) as client:
            role = (
                await client.post(
                    "/api/roles",
                    json={
                        "name": "停止发现",
                        "profile": {"profile": "设定"},
                        "agentConfig": {
                            "enabledTools": ["tool_search"],
                            "browserUse": {"enabled": True},
                        },
                    },
                )
            ).json()["role"]
            request = asyncio.create_task(
                client.post(
                    f"/api/roles/{role['id']}/messages", json={"content": "发现后停止"}
                )
            )
            try:
                await asyncio.wait_for(ready.wait(), 5)
                run = sessions.active_run(role["id"])
                stopped = await client.post(
                    f"/api/roles/{role['id']}/runs/{run.runId}/stop"
                )
                assert stopped.status_code == 200
                terminal = sessions.get_run(run.runId)
                assert terminal.status == "cancelled"
                assert len(terminal.modelSnapshot["capabilityRevisions"]) == 2
                assert set(terminal.modelSnapshot["capabilityCalls"]) == {
                    "load-before-stop"
                }
            finally:
                if not request.done():
                    request.cancel()
                await asyncio.gather(request, return_exceptions=True)

    asyncio.run(execute())
