import json

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.advanced_settings import AdvancedSettingsStore
from backend.app.agent_runtime import RuntimeManager
from backend.app.channels.group_tools import MultichannelGroupRepository
from backend.app.channels.host import ChannelHost
from backend.app.memory_config import MemorySettingsStore
from backend.app.model_config import ModelConfigurationStore
from backend.app.model_adapter import ModelResponseDone, ModelTextDelta, ModelToolCall
from backend.app.multichannel import MultichannelStore
from backend.app.session_manager import SessionManager
from backend.app.session_store import SessionStore
from backend.app.role_store import RoleStore


def test_offline_group_notes_match_catalog_schema_and_reach_the_next_provider_request(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    sessions = SessionStore(tmp_path / "sessions.db")
    channels = MultichannelStore(tmp_path / "sessions.db", sessions)
    groups = MultichannelGroupRepository(channels)
    settings = MemorySettingsStore(tmp_path / "memory.json")
    settings.update(enabled=False)
    advanced = AdvancedSettingsStore(tmp_path / "advanced.db")
    advanced.update(expected_revision=0, operation_id="direct-tools", values={"searchEnabled": False})
    for name, value in {
        "store": roles, "session_store": sessions, "session_manager": SessionManager(roles, sessions),
        "multichannel_store": channels, "channel_host": ChannelHost(channels), "group_repository": groups,
        "model_configuration_store": ModelConfigurationStore(tmp_path / "models.json"),
        "memory_settings_store": settings, "runtime_manager": RuntimeManager(sessions),
        "advanced_settings": advanced,
        "role_locks": {}, "channel_event_locks": {}, "channel_adapters": {},
    }.items():
        monkeypatch.setattr(main, name, value)
    client = TestClient(main.app)
    role = client.post("/api/roles", json={"name": "群资料", "profile": {"profile": "设定"}}).json()["role"]
    empty = client.get(f"/api/roles/{role['id']}/tools").json()
    for name in ("lookup_group_context", "update_group_context"):
        entry = next(item for item in empty["tools"] if item["name"] == name)
        assert not entry["enabled"] and not entry["modelVisible"]
    channels.register_channel_account(role["id"], "qq", account_id="saved", enabled=True, allow_group=True)
    thread = channels.ensure_channel_thread(role["id"], "qq", "saved", "100", "group", title="Old Group")
    channels.append_event(thread, direction="inbound", content="RAW_GROUP_MUST_STAY_PRIVATE",
        external_message_id="raw", metadata={"senderId": "22"})
    channels.set_group_listening(role["id"], "saved", "100", True)
    channels.listening.hear(thread, "RAW_LISTENING_MUST_STAY_PRIVATE", "heard", {"senderId": "22"})
    groups.update_context(role["id"], thread.thread_id, group_note="离线笔记", summary="已有摘要")
    raw_before = sessions.list_messages(thread.session_key)
    heard_before = channels.listening.page(role["id"], thread.thread_id)
    channels.set_account_state("saved", enabled=False)
    catalog = client.get(f"/api/roles/{role['id']}/tools").json()
    for name in ("lookup_group_context", "update_group_context"):
        entry = next(item for item in catalog["tools"] if item["name"] == name)
        assert entry["enabled"] and entry["authorized"] and entry["modelVisible"]
        assert entry["dependency"]["status"] == "ready" and "本地" in entry["dependency"]["reason"]
    assert not next(item for item in catalog["tools"] if item["name"] == "set_group_listening")["modelVisible"]

    class Model:
        calls = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in tools}
            assert schemas["lookup_group_context"] == main.LookupGroupContextTool.definition.input_schema
            assert schemas["update_group_context"] == main.UpdateGroupContextTool.definition.input_schema
            if self.calls == 1:
                yield ModelToolCall("find", "lookup_group_context", {"name": "old group"})
                yield ModelResponseDone("tool_use")
                return
            result = next(item for item in reversed(messages) if item["role"] == "tool")
            assert "RAW_GROUP" not in result["content"] and "RAW_LISTENING" not in result["content"]
            data = json.loads(result["content"])
            if self.calls == 2:
                assert data["total"] == 1 and data["candidates"][0]["groupThreadId"] == thread.thread_id
                yield ModelToolCall("read", "lookup_group_context", {"group_thread_id": thread.thread_id})
                yield ModelResponseDone("tool_use")
            elif self.calls == 3:
                assert data["groupNote"] == "离线笔记" and data["summary"] == "已有摘要"
                yield ModelToolCall("patch", "update_group_context", {"group_thread_id": thread.thread_id, "summary": ""})
                yield ModelResponseDone("tool_use")
            else:
                assert data["groupNote"] == "离线笔记" and data["summary"] == "" and data["summaryUpdatedAt"]
                yield ModelTextDelta("离线群资料已更新")

    model = Model()
    monkeypatch.setattr(main, "model_adapter", model)
    response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "查询并清空旧群摘要"})
    assert "离线群资料已更新" in response.text and model.calls == 4
    assert sessions.list_messages(thread.session_key) == raw_before
    assert channels.listening.page(role["id"], thread.thread_id) == heard_before
    assert groups.read_context(role["id"], thread.thread_id)["summary"] == ""
