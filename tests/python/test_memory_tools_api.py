import json

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.agent_runtime.types import ToolResultMessage
from backend.app.memory_config import MemorySettingsStore
from backend.app.memory_documents import MemoryDocuments
from backend.app.memory_engine import DefaultMemoryEngine
from backend.app.memory_service import MemoryService, MemoryWorker
from backend.app.memory_store import MemoryStore
from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from backend.app.model_config import ModelConfigurationStore
from backend.app.models import AgentToolConfig, MemoryRecallToolConfig, RoleInput, RoleProfile
from backend.app.role_store import RoleStore
from backend.app.session_manager import SessionManager
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


def test_owner_memory_write_recall_forget_roundtrip_uses_real_engine_and_updates_documents(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="记忆角色", profile=RoleProfile(profile="设定"), agentConfig=AgentToolConfig(
        enabledTools=["memorize", "recall_memory", "forget_memory"], memoryRecall=MemoryRecallToolConfig(enabled=True))))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    memories = MemoryStore(tmp_path / "data" / "memory.db")
    settings = MemorySettingsStore(tmp_path / "data" / "settings.json")
    service = MemoryService(memories, implicit_extraction_enabled=False, memory_enabled=lambda: settings.get().enabled)
    engine = DefaultMemoryEngine(service, role_exists=lambda value: roles.get(value) is not None)

    class Provider:
        requests = []

        async def stream_structured_messages(self, messages, configuration, tools):
            self.requests.append((messages, tools))
            turn = len(self.requests)
            if turn == 1:
                yield ModelToolCall("save", "memorize", {"summary": "主人喜欢海边", "memory_kind": "preference"})
            elif turn == 2:
                self.item_id = json.loads(next(item["content"] for item in reversed(messages) if item.get("role") == "tool"))["item_id"]
                assert "主人喜欢海边" in MemoryDocuments(tmp_path / "roles").read_document(role.id, "MEMORY.md")
                yield ModelToolCall("find", "recall_memory", {"query": "海边"})
            elif turn == 3:
                recalled = json.loads(next(item["content"] for item in reversed(messages) if item.get("role") == "tool"))
                assert recalled["items"][0]["id"] == self.item_id
                source = sessions.user_message_for_run(sessions.list_messages(f"role:{role.id}")[0].runId)
                assert recalled["items"][0]["evidence"][0]["refs"] == [source.id]
                yield ModelToolCall("forget", "forget_memory", {"ids": [self.item_id]})
            elif turn == 4:
                assert "主人喜欢海边" not in MemoryDocuments(tmp_path / "roles").read_document(role.id, "MEMORY.md")
                yield ModelToolCall("find-again", "recall_memory", {"query": "海边"})
            else:
                payload = json.loads(next(item["content"] for item in reversed(messages) if item.get("role") == "tool"))
                assert payload["count"] == 0
                yield ModelTextDelta("已保存、核验并撤销该记忆")

    provider = Provider()
    for name, value in {"store": roles, "session_store": sessions, "session_manager": SessionManager(roles, sessions),
            "model_adapter": provider, "model_configuration_store": ModelConfigurationStore(tmp_path / "data" / "models.json"),
            "roles_root": tmp_path / "roles", "role_locks": {}, "memory_store": memories, "memory_service": service,
            "memory_engine": engine, "memory_worker": MemoryWorker(service), "memory_settings_store": settings}.items():
        monkeypatch.setattr(main, name, value)
    client = TestClient(main.app)
    response = client.post(f"/api/roles/{role.id}/messages", json={"content": "我喜欢海边；用工具保存、核验再撤销该记录。"})
    assert "event: assistant_completed" in response.text
    results = [item for item in sessions.runtime_messages(f"role:{role.id}") if isinstance(item, ToolResultMessage)]
    assert len(results) == 4 and all(not item.is_error for item in results)
    assert "已保存、核验并撤销" in client.get(f"/api/roles/{role.id}/session").text


def test_global_memory_switch_hides_all_tools_and_rejects_manual_enabled_tools(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="关闭记忆", profile=RoleProfile(profile="设定"), agentConfig=AgentToolConfig(
        enabledTools=["memorize", "recall_memory", "forget_memory"], memoryRecall=MemoryRecallToolConfig(enabled=True))))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    settings = MemorySettingsStore(tmp_path / "data" / "settings.json")
    settings.update(enabled=False)
    memories = MemoryStore(tmp_path / "data" / "memory.db")
    service = MemoryService(memories, memory_enabled=lambda: settings.get().enabled)
    engine = DefaultMemoryEngine(service, role_exists=lambda value: roles.get(value) is not None)

    class Provider:
        calls = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            assert {"memorize", "recall_memory", "forget_memory"}.isdisjoint({item["function"]["name"] for item in tools})
            if self.calls == 1:
                yield ModelToolCall("forged-write", "memorize", {"summary": "不应保存"})
            elif self.calls == 2:
                yield ModelToolCall("forged-read", "recall_memory", {"query": "test"})
            elif self.calls == 3:
                yield ModelToolCall("forged-forget", "forget_memory", {"ids": ["unknown"]})
            else:
                yield ModelTextDelta("全局记忆关闭")

    for name, value in {"store": roles, "session_store": sessions, "session_manager": SessionManager(roles, sessions),
            "model_adapter": Provider(), "model_configuration_store": ModelConfigurationStore(tmp_path / "data" / "models.json"),
            "roles_root": tmp_path / "roles", "role_locks": {}, "memory_store": memories, "memory_service": service,
            "memory_engine": engine, "memory_worker": MemoryWorker(service), "memory_settings_store": settings}.items():
        monkeypatch.setattr(main, name, value)
    response = TestClient(main.app).post(f"/api/roles/{role.id}/messages", json={"content": "尝试调用"})
    assert "event: assistant_completed" in response.text
    results = [item for item in sessions.runtime_messages(f"role:{role.id}") if isinstance(item, ToolResultMessage)]
    assert len(results) == 3 and all(item.is_error for item in results)
    assert engine.list_active_items(role.id) == []
