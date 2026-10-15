"""Feed Skill behavior at the public owner HTTP / provider boundary."""
import json
from pathlib import Path
import sys

import pytest

from backend.app import main
from backend.app.ai.model_adapter import ModelTextDelta, ModelToolCall
from test_mcp_http import save_server, setup_host


def test_explicit_feed_skill_reaches_model_without_granting_mcp_or_shell(tmp_path, monkeypatch):
    client, role, sessions, _ = setup_host(tmp_path, monkeypatch)

    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            system = json.dumps([item for item in messages if item["role"] == "system"], ensure_ascii=False)
            assert "Feed 订阅管理与查询" in system
            names = {item["function"]["name"] for item in tools}
            assert not names & {"tool_search", "mcp_list", "mcp_add", "shell"}
            assert not any(name.startswith("mcp_") for name in names)
            yield ModelTextDelta("需要主人在 MCP 设置中授权服务和工具发现")

    monkeypatch.setattr(main, "model_adapter", Adapter())
    try:
        response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "$feed-manage 你订阅了什么？"})
        assert "需要主人在 MCP 设置中授权服务和工具发现" in response.text, response.text
        messages = sessions.list_messages(f"role:{role['id']}")
        run = sessions.get_run(messages[-1].runId)
        skills = run.modelSnapshot["capabilities"]["skills"]
        assert [(item["id"], item["source"], item["tools"]) for item in skills] == [("feed-manage", "builtin", [])]
        assert not any(item.messageType == "tool_result" for item in messages)
    finally:
        client.close()


def enable_tools(client, role, names):
    response = client.put(f"/api/roles/{role['id']}", json={"name": role["name"], "profile": role["profile"],
        "agentConfig": {"enabledTools": names}})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("configuration", ["missing", "other-role", "disabled"])
def test_feed_skill_cannot_connect_an_ungranted_service(tmp_path, monkeypatch, configuration):
    client, role, sessions, _ = setup_host(tmp_path, monkeypatch)
    if configuration != "missing":
        changes = {"roleIds": []} if configuration == "other-role" else {"enabled": False}
        assert save_server(client, role, tmp_path, **changes).status_code == 201
    enable_tools(client, role, ["tool_search", "mcp_list", "mcp_add"])

    class Adapter:
        turn = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.turn += 1
            if self.turn == 1:
                yield ModelToolCall("find", "tool_search", {"query": "select:mcp_list,mcp_add"})
            elif self.turn == 2:
                yield ModelToolCall("list", "mcp_list", {})
            elif self.turn == 3:
                assert json.loads(messages[-1]["content"])["servers"] == []
                # A model guessing a familiar ID still cannot grant itself access.
                yield ModelToolCall("guess", "mcp_add", {"server_id": "notes"})
            else:
                yield ModelTextDelta("当前角色没有获准的 Feed 服务，不能连接")

    monkeypatch.setattr(main, "model_adapter", Adapter())
    try:
        response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "$feed-manage 查看当前来源"})
        assert "当前角色没有获准的 Feed 服务，不能连接" in response.text, response.text
        result = next(item for item in sessions.list_messages(f"role:{role['id']}")
            if item.toolCallId == "guess" and item.messageType == "tool_result")
        assert result.isError
        assert not (tmp_path / "peer/pid").exists()
    finally:
        client.close()


@pytest.mark.parametrize("disabled", ["tool_search", "mcp_add"])
def test_feed_skill_does_not_reenable_owner_disabled_tools(tmp_path, monkeypatch, disabled):
    client, role, sessions, _ = setup_host(tmp_path, monkeypatch)
    assert save_server(client, role, tmp_path).status_code == 201
    enable_tools(client, role, sorted({"tool_search", "mcp_list", "mcp_add"} - {disabled}))

    class Adapter:
        turn = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.turn += 1
            assert disabled not in {item["function"]["name"] for item in tools}
            if self.turn == 1:
                if disabled == "mcp_add":
                    yield ModelToolCall("load-disabled", "tool_search", {"query": "select:mcp_add"})
                else:
                    yield ModelToolCall("disabled", "tool_search", {"query": "select:mcp_add"})
            elif self.turn == 2 and disabled == "mcp_add":
                yield ModelToolCall("disabled", "mcp_add", {"server_id": "notes"})
            else:
                yield ModelTextDelta("Feed 所需工具已关闭，请主人检查角色配置")

    monkeypatch.setattr(main, "model_adapter", Adapter())
    try:
        response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "$feed-manage 管理来源"})
        assert "Feed 所需工具已关闭" in response.text, response.text
        result = next(item for item in sessions.list_messages(f"role:{role['id']}")
            if item.toolCallId == "disabled" and item.messageType == "tool_result")
        assert result.isError and not (tmp_path / "peer/pid").exists()
    finally:
        client.close()


def test_feed_skill_exposes_multiple_choices_without_connecting_them(tmp_path, monkeypatch):
    client, role, _, _ = setup_host(tmp_path, monkeypatch)
    assert save_server(client, role, tmp_path, serverId="work", name="工作订阅").status_code == 201
    assert save_server(client, role, tmp_path, serverId="personal", name="个人订阅").status_code == 201

    class Adapter:
        turn = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.turn += 1
            if self.turn == 1:
                system = json.dumps([item for item in messages if item["role"] == "system"], ensure_ascii=False)
                assert "多个服务" in system and "不批量连接" in system
                yield ModelToolCall("find", "tool_search", {"query": "select:mcp_list"})
            elif self.turn == 2:
                yield ModelToolCall("choices", "mcp_list", {})
            else:
                servers = json.loads(messages[-1]["content"])["servers"]
                assert {(item["serverId"], item["name"]) for item in servers} == {("work", "工作订阅"), ("personal", "个人订阅")}
                yield ModelTextDelta("请选择工作订阅（work）或个人订阅（personal）")

    monkeypatch.setattr(main, "model_adapter", Adapter())
    try:
        response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "$feed-manage 查看订阅"})
        assert "请选择工作订阅" in response.text, response.text
        assert all(item["activeProcesses"] == item["connectionCount"] == 0 for item in client.get("/api/mcp/servers").json()["servers"])
        assert not (tmp_path / "peer/pid").exists()
    finally:
        client.close()


def test_feed_skill_keeps_connection_failure_as_error_without_remote_calls(tmp_path, monkeypatch):
    client, role, sessions, _ = setup_host(tmp_path, monkeypatch)
    fixture = Path(__file__).resolve().parents[1] / "fixtures/mcp_stdio_server.py"
    assert save_server(client, role, tmp_path, command=[sys.executable, str(fixture), str(tmp_path / "peer"), "wrong_version"]).status_code == 201

    class Adapter:
        turn = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.turn += 1
            if self.turn == 1:
                yield ModelToolCall("load", "tool_search", {"query": "select:mcp_add"})
            elif self.turn == 2:
                yield ModelToolCall("connection-failed", "mcp_add", {"server_id": "notes"})
            else:
                assert not any(item["function"]["name"].startswith("mcp_notes__") for item in tools)
                yield ModelTextDelta("Feed 连接失败，尚未读取或修改订阅")

    monkeypatch.setattr(main, "model_adapter", Adapter())
    try:
        response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "$feed-manage 读取来源"})
        assert "Feed 连接失败" in response.text, response.text
        result = next(item for item in sessions.list_messages(f"role:{role['id']}")
            if item.toolCallId == "connection-failed" and item.messageType == "tool_result")
        assert result.isError
        assert not (tmp_path / "peer/business.jsonl").exists()
        assert client.get("/api/mcp/servers").json()["servers"][0]["activeProcesses"] == 0
    finally:
        client.close()
