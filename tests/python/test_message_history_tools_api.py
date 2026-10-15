import json

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from backend.app.model_config import ModelConfigurationStore
from backend.app.multichannel import MultichannelStore
from backend.app.role_store import RoleStore
from backend.app.session_manager import SessionManager
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


def test_http_history_tools_are_opt_in_and_results_reach_next_model_request(tmp_path, monkeypatch):
    class Adapter:
        phase = "off"
        source_ref = None

        async def stream_structured_messages(self, messages, configuration, tools):
            names = {tool["function"]["name"] for tool in tools}
            results = [message for message in messages if message["role"] == "tool"]
            if self.phase == "off":
                assert not {"search_messages", "fetch_messages"} & names
                yield ModelTextDelta("尚未授权")
            elif self.phase == "search":
                assert {"search_messages", "fetch_messages"} <= names
                self.phase = "fetch"
                yield ModelToolCall("search-evidence", "search_messages", {"query": "暗号"})
            elif self.phase == "fetch":
                found = json.loads(results[-1]["content"])
                assert found["messages"][0]["preview"] == "暗号是月光"
                self.source_ref = found["messages"][0]["source_ref"]
                self.phase = "answer"
                yield ModelToolCall("fetch-evidence", "fetch_messages", {"source_ref": self.source_ref})
            elif self.phase == "answer":
                found = json.loads(results[-1]["content"])
                assert found["messages"][0]["content"] == "暗号是月光"
                yield ModelTextDelta("核验原文完成")
            elif self.phase == "revoked":
                assert not {"search_messages", "fetch_messages"} & names
                self.phase = "denied"
                yield ModelToolCall("forged-fetch", "fetch_messages", {"source_ref": self.source_ref})
            else:
                assert "denied" in results[-1]["content"].lower()
                yield ModelTextDelta("读取已拒绝")

    adapter = Adapter()
    roles = RoleStore(tmp_path / "roles")
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data/sessions.db")
    channels = MultichannelStore(tmp_path / "data/sessions.db", sessions)
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "multichannel_store", channels)
    monkeypatch.setattr(main, "session_manager", SessionManager(roles, sessions))
    monkeypatch.setattr(main, "model_adapter", adapter)
    monkeypatch.setattr(main, "model_configuration_store", ModelConfigurationStore(tmp_path / "data/models.json"))
    monkeypatch.setattr(main, "role_locks", {})
    client = TestClient(main.app)
    created = client.post("/api/roles", json={"name": "原文女仆", "profile": {"profile": "核心设定"}})
    assert created.status_code == 201
    role_id = created.json()["role"]["id"]
    sessions.append_message(f"role:{role_id}", "user", "暗号是月光")
    assert "尚未授权" in client.post(f"/api/roles/{role_id}/messages", json={"content": "工具状态"}).text
    for names, phase, expected in ((["search_messages", "fetch_messages"], "search", "核验原文完成"), ([], "revoked", "读取已拒绝")):
        response = client.put(f"/api/roles/{role_id}", json={
            "name": "原文女仆", "profile": {"profile": "核心设定"}, "agentConfig": {"enabledTools": names},
        })
        assert response.status_code == 200
        adapter.phase = phase
        result = client.post(f"/api/roles/{role_id}/messages", json={"content": "核验之前的消息"})
        assert "event: assistant_completed" in result.text and expected in result.text
    persisted = sessions.list_messages(f"role:{role_id}")
    assert any(message.toolName == "fetch_messages" and "暗号是月光" in message.content for message in persisted)
