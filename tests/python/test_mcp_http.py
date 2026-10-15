import json
import asyncio
import ctypes
import os
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import httpx
import pytest

from backend.app import main
from backend.app.mcp_config import McpConfigStore
from backend.app.mcp_runtime import McpHost
from backend.app.memory_config import MemorySettingsStore
from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from backend.app.model_config import ModelConfigurationStore
from backend.app.role_store import RoleStore
from backend.app.session_manager import SessionManager
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


class Credentials:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        self.values[key] = value


def setup_host(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data/sessions.db")
    settings = MemorySettingsStore(tmp_path / "data/memory-settings.json")
    settings.update(enabled=False)
    configs = McpConfigStore(tmp_path / "data/mcp.db", Credentials())
    host = McpHost(configs)
    for key, value in {"store": roles, "session_store": sessions, "session_manager": SessionManager(roles, sessions), "model_configuration_store": ModelConfigurationStore(tmp_path / "data/models.json"), "memory_settings_store": settings, "mcp_host": host, "role_locks": {}}.items():
        monkeypatch.setattr(main, key, value)
    client = TestClient(main.app)
    role = client.post("/api/roles", json={"name": "MCP 主人授权", "profile": {"profile": "测试"}}).json()["role"]
    return client, role, sessions, configs


def save_server(client, role, tmp_path, **changes):
    fixture = Path(__file__).resolve().parents[1] / "fixtures/mcp_stdio_server.py"
    values = {"serverId": "notes", "name": "本地证据", "command": [sys.executable, str(fixture), str(tmp_path / "peer")], "cwd": str(tmp_path), "env": {"MCP_TEST_SECRET": "vault-sensitive-token"}, "roleIds": [role["id"]], "risk": "external", "timeoutSeconds": 3}
    values.update(changes)
    return client.post("/api/mcp/servers", json=values)


def test_owner_http_configuration_does_not_launch_and_rejects_model_launch_arguments(tmp_path, monkeypatch):
    client, role, sessions, configs = setup_host(tmp_path, monkeypatch)
    response = save_server(client, role, tmp_path)
    assert response.status_code == 201, response.text
    assert "vault-sensitive-token" not in response.text
    assert not (tmp_path / "peer/pid").exists()
    catalog = client.get("/api/mcp/servers")
    assert "vault-sensitive-token" not in catalog.text
    assert catalog.json()["servers"][0]["connectionCount"] == 0
    tools = client.get(f"/api/roles/{role['id']}/tools").json()
    for name in ("mcp_add", "mcp_list", "mcp_remove"):
        assert next(item for item in tools["tools"] if item["name"] == name)["authorized"]
    assert not (tmp_path / "peer/pid").exists(), "catalog preview must remain observational"

    class Adapter:
        count = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.count += 1
            if self.count == 1:
                yield ModelToolCall("load", "tool_search", {"query": "select:mcp_add"})
            elif self.count == 2:
                yield ModelToolCall("injected", "mcp_add", {"server_id": "notes", "command": [sys.executable], "env": {"TOKEN": "x"}})
            else:
                yield ModelTextDelta("配置只由主人管理")

    monkeypatch.setattr(main, "model_adapter", Adapter())
    result = client.post(f"/api/roles/{role['id']}/messages", json={"content": "连接"})
    assert "配置只由主人管理" in result.text
    assert not (tmp_path / "peer/pid").exists()
    message = next(item for item in sessions.list_messages(f"role:{role['id']}") if item.toolCallId == "injected" and item.messageType == "tool_result")
    assert message.isError and message.toolResult["status"] == "invalid_arguments"


def test_public_http_real_stdio_same_run_revision_remove_and_restart_intent(tmp_path, monkeypatch):
    client, role, sessions, configs = setup_host(tmp_path, monkeypatch)
    saved = save_server(client, role, tmp_path)
    assert saved.status_code == 201, saved.text
    remote = "mcp_notes__echo"

    class Adapter:
        count = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.count += 1
            names = {item["function"]["name"] for item in tools}
            if self.count == 1:
                assert remote not in names
                yield ModelToolCall("load-management", "tool_search", {"query": "select:mcp_add,mcp_list,mcp_remove"})
            elif self.count == 2:
                yield ModelToolCall("connect", "mcp_add", {"server_id": "notes"})
                yield ModelToolCall("same-reply", remote, {"text": "evidence too early"})
            elif self.count == 3:
                assert remote not in names
                yield ModelToolCall("discover", "tool_search", {"query": f"select:{remote}"})
            elif self.count == 4:
                assert remote in names
                yield ModelToolCall("actual", remote, {"text": "evidence actual"})
            else:
                assert "evidence actual" in messages[-1]["content"]
                assert "vault-sensitive-token" not in json.dumps(messages, ensure_ascii=False)
                yield ModelTextDelta("真实 MCP 已调用")

    adapter = Adapter()
    monkeypatch.setattr(main, "model_adapter", adapter)
    response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "连接已授权服务并读取证据"})
    assert "真实 MCP 已调用" in response.text, response.text
    assert adapter.count == 5
    rows = [json.loads(line) for line in (tmp_path / "peer/business.jsonl").read_text().splitlines()]
    assert rows == [{"name": "echo", "value": "evidence actual"}]
    messages = sessions.list_messages(f"role:{role['id']}")
    run_id = next(item.runId for item in messages if item.toolCallId == "actual")
    audits = {item["callId"]: item for item in sessions.list_tool_audits(run_id)}
    revisions = sessions.get_run(run_id).modelSnapshot["capabilityRevisions"]
    assert [item["visibilityVersion"] for item in revisions] == [0, 1, 2, 3]
    assert revisions[2]["parentSnapshotId"] == revisions[1]["snapshotId"]
    assert audits["same-reply"]["resultCategory"] in {"unknown", "denied"}
    assert audits["actual"]["toolSource"] == "mcp:notes"
    definition = next(item for item in revisions[-1]["tools"] if item["name"] == remote)
    assert definition["risk"] == "external", "remote readOnlyHint must not lower the owner grant"
    assert "vault-sensitive-token" not in json.dumps(sessions.get_run(run_id).model_dump(), default=str)
    assert client.get("/api/mcp/servers").json()["servers"][0]["connectionCount"] == 1
    # New host + reopened SQLite proves saved intent, not an in-memory shortcut.
    monkeypatch.setattr(main, "mcp_host", McpHost(McpConfigStore(configs.path, configs.credentials)))

    class RestartAdapter:
        count = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.count += 1
            names = {item["function"]["name"] for item in tools}
            if self.count == 1:
                assert remote in names, "same authorized source should reconnect and restore exact schema activation"
                yield ModelToolCall("remove", "mcp_remove", {"server_id": "notes"})
                yield ModelToolCall("after-remove", remote, {"text": "evidence revoked"})
            else:
                assert remote not in names
                yield ModelTextDelta("连接意愿已移除")

    monkeypatch.setattr(main, "model_adapter", RestartAdapter())
    response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "断开服务"})
    assert "连接意愿已移除" in response.text, response.text
    assert len((tmp_path / "peer/business.jsonl").read_text().splitlines()) == 1, "reconnect must never replay business calls"
    assert client.get("/api/mcp/servers").json()["servers"][0]["connectionCount"] == 0


def process_alive(pid):
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            assert kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
            return code.value == 259
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.mark.parametrize("change", ["delete", "grant", "discovery", "stop"])
def test_http_revocation_or_stop_cancels_remote_work_and_owned_descendant(tmp_path, monkeypatch, change):
    client, role, sessions, configs = setup_host(tmp_path, monkeypatch)
    assert save_server(client, role, tmp_path, timeoutSeconds=60).status_code == 201

    class Adapter:
        count = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.count += 1
            if self.count == 1:
                yield ModelToolCall("load-management", "tool_search", {"query": "select:mcp_add"})
            elif self.count == 2:
                yield ModelToolCall("connect", "mcp_add", {"server_id": "notes"})
            elif self.count == 3:
                yield ModelToolCall("discover", "tool_search", {"query": "select:mcp_notes__slow"})
            elif self.count == 4:
                yield ModelToolCall("slow", "mcp_notes__slow", {})
            else:
                yield ModelTextDelta("远端任务已清理")

    monkeypatch.setattr(main, "model_adapter", Adapter())

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://meido.test") as http:
            request = asyncio.create_task(http.post(f"/api/roles/{role['id']}/messages", json={"content": "等待远端调用"}))
            try:
                async with asyncio.timeout(5):
                    while not (tmp_path / "peer/child_pid").exists():
                        await asyncio.sleep(.01)
                parent = int((tmp_path / "peer/pid").read_text())
                child = int((tmp_path / "peer/child_pid").read_text())
                assert process_alive(parent) and process_alive(child)
                if change == "delete":
                    response = await http.delete("/api/mcp/servers/notes")
                    assert response.status_code == 204
                elif change == "grant":
                    values = configs.get("notes").public()
                    values.pop("revision")
                    values.pop("envKeys")
                    values.update(roleIds=[], env=None)
                    response = await http.put("/api/mcp/servers/notes", json=values)
                    assert response.status_code == 200, response.text
                elif change == "discovery":
                    response = await http.put(f"/api/roles/{role['id']}", json={"name": role["name"], "profile": role["profile"], "agentConfig": {"enabledTools": []}})
                    assert response.status_code == 200
                else:
                    active = sessions.active_run(role["id"])
                    response = await http.post(f"/api/roles/{role['id']}/runs/{active.runId}/stop")
                    assert response.status_code == 200
                if change == "stop":
                    # ASGITransport expects a terminal response body even when
                    # Stop cancels its streaming task; inspect the public run
                    # and real resources, as the browser receives EOF here.
                    await asyncio.wait_for(asyncio.gather(request, return_exceptions=True), 5)
                    assert sessions.get_run(active.runId).status == "cancelled"
                else:
                    await asyncio.wait_for(request, 5)
                assert not process_alive(parent) and not process_alive(child), "host must reap its own entire tree before the run completes"
                assert not (tmp_path / "peer/effect.txt").exists()
                assert not (tmp_path / "peer/business.jsonl").exists()
            finally:
                if not request.done():
                    request.cancel()
                await asyncio.gather(request, return_exceptions=True)
                await main.mcp_host.close()

    asyncio.run(run())


def test_new_server_grant_cannot_expand_an_already_running_frozen_directory(tmp_path, monkeypatch):
    client, role, sessions, configs = setup_host(tmp_path, monkeypatch)
    assert save_server(client, role, tmp_path).status_code == 201

    class Adapter:
        count = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.count += 1
            if self.count == 1:
                yield ModelToolCall("load", "tool_search", {"query": "select:mcp_add"})
            elif self.count == 2:
                response = save_server(client, role, tmp_path, serverId="new_server")
                assert response.status_code == 201
                yield ModelToolCall("not-frozen", "mcp_add", {"server_id": "new_server"})
            else:
                yield ModelTextDelta("新授权下轮生效")

    monkeypatch.setattr(main, "model_adapter", Adapter())
    response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "连接新服务"})
    assert "新授权下轮生效" in response.text
    assert not (tmp_path / "peer/pid").exists()
    result = next(item for item in sessions.list_messages(f"role:{role['id']}") if item.toolCallId == "not-frozen" and item.messageType == "tool_result")
    assert result.isError


def test_remove_then_readd_in_same_run_uses_new_immutable_versions_and_same_frozen_grant(tmp_path, monkeypatch):
    client, role, sessions, configs = setup_host(tmp_path, monkeypatch)
    assert save_server(client, role, tmp_path).status_code == 201

    class Adapter:
        count = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.count += 1
            names = {item["function"]["name"] for item in tools}
            if self.count == 1:
                yield ModelToolCall("load", "tool_search", {"query": "select:mcp_add,mcp_remove"})
            elif self.count in {2, 5}:
                yield ModelToolCall(f"connect-{self.count}", "mcp_add", {"server_id": "notes"})
            elif self.count in {3, 6}:
                yield ModelToolCall(f"find-{self.count}", "tool_search", {"query": "select:mcp_notes__echo"})
            elif self.count == 4:
                assert "mcp_notes__echo" in names
                yield ModelToolCall("remove", "mcp_remove", {"server_id": "notes"})
            elif self.count == 7:
                assert "mcp_notes__echo" in names
                yield ModelToolCall("actual", "mcp_notes__echo", {"text": "evidence readded"})
            else:
                yield ModelTextDelta("同一授权已重新连接")

    monkeypatch.setattr(main, "model_adapter", Adapter())
    response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "连接、移除、再连接"})
    assert "同一授权已重新连接" in response.text, response.text
    result = next(item for item in sessions.list_messages(f"role:{role['id']}") if item.toolCallId == "actual")
    versions = sessions.get_run(result.runId).modelSnapshot["capabilityRevisions"]
    assert [item["visibilityVersion"] for item in versions] == list(range(7))
    assert "mcp_notes__echo" in {item["name"] for item in versions[3]["tools"]}
    assert "mcp_notes__echo" not in {item["name"] for item in versions[4]["candidates"]}
    assert versions[4]["candidateCatalogId"] != versions[3]["candidateCatalogId"]
    assert all(item["serviceGrants"] == versions[0]["serviceGrants"] for item in versions)


def test_full_remote_input_schema_blocks_calls_and_failure_does_not_expose_secrets(tmp_path, monkeypatch):
    client, role, sessions, configs = setup_host(tmp_path, monkeypatch)
    assert save_server(client, role, tmp_path).status_code == 201

    class Adapter:
        count = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.count += 1
            calls = {1: ("tool_search", {"query": "select:mcp_add"}), 2: ("mcp_add", {"server_id": "notes"}), 3: ("tool_search", {"query": "select:mcp_notes__echo"}), 4: ("mcp_notes__echo", {"text": "invalid against remote pattern"})}
            if self.count in calls:
                name, arguments = calls[self.count]
                yield ModelToolCall(f"step-{self.count}", name, arguments)
            else:
                assert "frozen input schema" in messages[-1]["content"]
                assert "vault-sensitive-token" not in json.dumps(messages)
                yield ModelTextDelta("参数被本地 schema 阻止")

    monkeypatch.setattr(main, "model_adapter", Adapter())
    response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "校验参数"})
    assert "参数被本地 schema 阻止" in response.text
    assert not (tmp_path / "peer/business.jsonl").exists()


@pytest.mark.parametrize("mode", ["secret_result", "secret_declaration"])
def test_escaped_environment_credentials_never_reach_model_or_persisted_results(tmp_path, monkeypatch, mode):
    client, role, sessions, configs = setup_host(tmp_path, monkeypatch)
    secret = 'vault-"quoted"-\\path-\n-密钥'
    fixture = Path(__file__).resolve().parents[1] / "fixtures/mcp_stdio_server.py"
    assert save_server(client, role, tmp_path, command=[sys.executable, str(fixture), str(tmp_path / "peer"), mode], env={"MCP_TEST_SECRET": secret}).status_code == 201

    class Adapter:
        count = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.count += 1
            def check(value):
                if isinstance(value, str):
                    assert secret not in value
                    try:
                        decoded = json.loads(value)
                    except ValueError:
                        return
                    if isinstance(decoded, (dict, list)):
                        check(decoded)
                elif isinstance(value, dict):
                    for key, item in value.items():
                        check(key)
                        check(item)
                elif isinstance(value, list):
                    for item in value:
                        check(item)
            check({"messages": messages, "tools": tools})
            if self.count == 1:
                yield ModelToolCall("load", "tool_search", {"query": "select:mcp_add"})
            elif self.count == 2:
                yield ModelToolCall("connect", "mcp_add", {"server_id": "notes"})
            elif self.count == 3 and mode == "secret_result":
                yield ModelToolCall("find", "tool_search", {"query": "select:mcp_notes__echo"})
            elif self.count == 4 and mode == "secret_result":
                yield ModelToolCall("actual", "mcp_notes__echo", {"text": "evidence safe"})
            else:
                yield ModelTextDelta("凭据已隔离")

    monkeypatch.setattr(main, "model_adapter", Adapter())
    response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "验证凭据隔离"})
    assert "凭据已隔离" in response.text, response.text
    results = [item for item in sessions.list_messages(f"role:{role['id']}") if item.messageType == "tool_result"]
    for item in results:
        assert secret not in item.content
        # Decode each JSON layer: redaction must apply to values before JSON escaping.
        if isinstance(item.toolResult, dict):
            structured = item.toolResult.get("structuredContent")
            if structured is not None:
                assert structured == {"nested": [{"secret": "[redacted]"}]}
    if mode == "secret_declaration":
        assert not (tmp_path / "peer/business.jsonl").exists()
        assert next(item for item in results if item.toolCallId == "connect").isError


def test_exited_peer_is_reported_unhealthy_and_add_reconnects_without_replaying_calls(tmp_path, monkeypatch):
    client, role, sessions, configs = setup_host(tmp_path, monkeypatch)
    fixture = Path(__file__).resolve().parents[1] / "fixtures/mcp_stdio_server.py"
    assert save_server(client, role, tmp_path, command=[sys.executable, str(fixture), str(tmp_path / "peer"), "exit_after_list"]).status_code == 201

    class Adapter:
        count = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.count += 1
            if self.count == 1:
                yield ModelToolCall("load", "tool_search", {"query": "select:mcp_add,mcp_list"})
            elif self.count == 2:
                yield ModelToolCall("connect", "mcp_add", {"server_id": "notes"})
            elif self.count == 3:
                await asyncio.sleep(.3)
                yield ModelToolCall("health", "mcp_list", {})
            elif self.count == 4:
                assert json.loads(messages[-1]["content"])["servers"][0]["status"] == "failed"
                yield ModelToolCall("reconnect", "mcp_add", {"server_id": "notes"})
            else:
                assert json.loads(messages[-1]["content"])["status"] == "connected"
                yield ModelTextDelta("进程已重新握手")

    monkeypatch.setattr(main, "model_adapter", Adapter())
    response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "检查服务健康"})
    assert "进程已重新握手" in response.text, response.text
    events = [json.loads(line) for line in (tmp_path / "peer/events.jsonl").read_text().splitlines()]
    assert [item["method"] for item in events].count("initialize") == 2
    assert not (tmp_path / "peer/business.jsonl").exists()


@pytest.mark.parametrize("mode,expected", [
    ("large_result", "bounded"),
    ("invalid_result", "MCP image encoding is invalid"),
    ("invalid_structured", "MCP structured result must be an object"),
    ("secret_image", "MCP image contains protected configuration"),
    ("schema_valid", "evidence result"),
    ("output_mismatch", "MCP structured result does not match output schema"),
    ("tool_error", "evidence result"),
    ("peer_error", "MCP peer rejected the request"),
])
def test_real_remote_results_preserve_bounded_content_and_safe_actionable_failures(tmp_path, monkeypatch, mode, expected):
    client, role, sessions, configs = setup_host(tmp_path, monkeypatch)
    fixture = Path(__file__).resolve().parents[1] / "fixtures/mcp_stdio_server.py"
    assert save_server(client, role, tmp_path, command=[sys.executable, str(fixture), str(tmp_path / "peer"), mode], maxOutputChars=1024, timeoutSeconds=10).status_code == 201

    class Adapter:
        count = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.count += 1
            calls = {1: ("tool_search", {"query": "select:mcp_add"}), 2: ("mcp_add", {"server_id": "notes"}), 3: ("tool_search", {"query": "select:mcp_notes__echo"}), 4: ("mcp_notes__echo", {"text": "evidence result"})}
            if self.count in calls:
                name, arguments = calls[self.count]
                yield ModelToolCall(f"result-{self.count}", name, arguments)
            else:
                assert expected in messages[-1]["content"]
                assert "vault-sensitive-token" not in json.dumps(messages)
                yield ModelTextDelta("远端结果已明确处理")

    monkeypatch.setattr(main, "model_adapter", Adapter())
    response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "处理远端结果"})
    assert "远端结果已明确处理" in response.text, response.text
    result = next(item for item in sessions.list_messages(f"role:{role['id']}") if item.toolCallId == "result-4" and item.messageType == "tool_result")
    assert result.isError == (mode not in {"large_result", "schema_valid"})
    assert len(result.content) <= 1024
    if mode == "large_result":
        assert result.toolResult["truncated"] is True
