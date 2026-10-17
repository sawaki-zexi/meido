import json
import asyncio
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.agent_runtime.types import ToolResultMessage
from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from backend.app.model_config import ModelConfigurationStore
from backend.app.models import AgentToolConfig, RoleInput, RoleProfile, ShellToolConfig
from backend.app.role_store import RoleStore
from backend.app.session_manager import SessionManager
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.network_tools import NetworkToolsConfig


@pytest.mark.parametrize("tool_name,arguments,response", [
    ("web_fetch", {"url": "https://article.example/read", "format": "text", "timeout": 2},
        httpx.Response(200, text="来源网页正文", headers={"content-type": "text/plain"})),
    ("web_search", {"query": "最近新闻", "num_results": 1},
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"content": [
            {"type": "text", "text": "最新消息 https://article.example/read"}]}})),
])
def test_authorized_role_network_tool_completes_http_conversation_and_persists_sources(tmp_path, monkeypatch, tool_name, arguments, response):
    requests = []

    class Provider:
        def __init__(self):
            self.requests = []

        async def stream_structured_messages(self, messages, configuration, tools):
            self.requests.append((messages, tools))
            if len(self.requests) == 1:
                yield ModelToolCall("call-network", tool_name, arguments)
            else:
                results = [json.loads(item["content"]) for item in messages if item.get("role") == "tool"]
                assert results and "error" not in results[-1]
                yield ModelTextDelta("已依据实际来源回答")

    async def handler(request):
        requests.append(request)
        if tool_name == "web_fetch":
            await asyncio.sleep(1.1)
        return response

    original_dns = socket.getaddrinfo

    def dns(host, port, *args, **kwargs):
        if host in {"article.example", "mcp.exa.ai"}:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port))]
        return original_dns(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(httpx._client, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handler))
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="联网角色", profile=RoleProfile(profile="设定"),
        agentConfig=AgentToolConfig(shell=ShellToolConfig(timeoutSeconds=1))))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    provider = Provider()
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", SessionManager(roles, sessions))
    monkeypatch.setattr(main, "model_adapter", provider)
    monkeypatch.setattr(main, "model_configuration_store", ModelConfigurationStore(tmp_path / "data" / "models.json"))
    monkeypatch.setattr(main, "role_locks", {})

    client = TestClient(main.app)
    catalog = {item["name"]: item for item in client.get(f"/api/roles/{role.id}/tools").json()["tools"]}
    assert not catalog[tool_name]["enabled"]
    saved = client.put(f"/api/roles/{role.id}", json={"name": role.name, "profile": role.profile.model_dump(),
        "agentConfig": {"enabledTools": [tool_name], "shell": {"timeoutSeconds": 1}}})
    assert saved.status_code == 200
    assert client.get(f"/api/roles/{role.id}").json()["role"]["agentConfig"]["enabledTools"] == [tool_name]
    reply = client.post(f"/api/roles/{role.id}/messages", json={"content": "查询来源"})
    assert reply.status_code == 200
    assert "event: assistant_completed" in reply.text
    assert len(requests) == 1
    assert tool_name in {schema["function"]["name"] for schema in provider.requests[0][1]}
    results = [item for item in sessions.runtime_messages(f"role:{role.id}") if isinstance(item, ToolResultMessage)]
    assert len(results) == 1
    assert not results[0].is_error
    assert "来源网页正文" in results[0].content or "https://article.example/read" in results[0].content


@pytest.mark.parametrize("role_enabled,host_enabled", [(False, True), (True, False)])
def test_unapproved_network_tools_are_hidden_and_forged_calls_cannot_execute(tmp_path, monkeypatch, role_enabled, host_enabled):
    class Provider:
        requests = []

        async def stream_structured_messages(self, messages, configuration, tools):
            self.requests.append((messages, tools))
            if len(self.requests) == 1:
                yield ModelToolCall("forged-call", "web_fetch", {"url": "https://example.org"})
            else:
                yield ModelTextDelta("当前没有联网许可")

    def unexpected(request):
        raise AssertionError("unapproved network call reached transport")

    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="无许可角色", profile=RoleProfile(profile="设定"),
        agentConfig=AgentToolConfig(enabledTools=["web_fetch", "web_search"] if role_enabled else [])))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    provider = Provider()
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", SessionManager(roles, sessions))
    monkeypatch.setattr(main, "model_adapter", provider)
    monkeypatch.setattr(main, "network_tools_config", NetworkToolsConfig(enabled=host_enabled))
    monkeypatch.setattr(main, "model_configuration_store", ModelConfigurationStore(tmp_path / "data" / "models.json"))
    monkeypatch.setattr(main, "role_locks", {})
    monkeypatch.setattr(httpx._client, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(unexpected))

    client = TestClient(main.app)
    if role_enabled and not host_enabled:
        catalog = {item["name"]: item for item in client.get(f"/api/roles/{role.id}/tools").json()["tools"]}
        for name in ("web_fetch", "web_search"):
            assert catalog[name]["enabled"] and not catalog[name]["authorized"]
            assert catalog[name]["reason"] == "宿主联网已关闭"
            assert catalog[name]["dependency"]["status"] == "unavailable"
    response = client.post(f"/api/roles/{role.id}/messages", json={"content": "联网"})
    assert "event: assistant_completed" in response.text
    assert {"web_fetch", "web_search"}.isdisjoint({item["function"]["name"] for item in provider.requests[0][1]})
    results = [item for item in sessions.runtime_messages(f"role:{role.id}") if isinstance(item, ToolResultMessage)]
    assert len(results) == 1 and results[0].is_error
