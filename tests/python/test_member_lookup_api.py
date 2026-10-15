"""Trusted QQ nickname observations reach a real desktop tool conversation."""
import asyncio
import json

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.agent_runtime.runtime import RuntimeManager
from backend.app.channels import ChannelHost
from backend.app.channels.group_tools import MultichannelGroupRepository
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.memory_config import MemorySettingsStore
from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from backend.app.model_config import ModelConfigurationStore
from backend.app.multichannel import MultichannelStore
from backend.app.role_store import RoleStore
from backend.app.session_manager import SessionManager
from backend.app.session_store import SessionStore


def test_qq_nickname_observations_are_queryable_from_desktop_when_account_is_offline(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    sessions = SessionStore(tmp_path / "sessions.db")
    channels = MultichannelStore(tmp_path / "sessions.db", sessions)
    settings = MemorySettingsStore(tmp_path / "memory.json")
    settings.update(enabled=False)
    host = ChannelHost(channels)

    async def avatar(*args, **kwargs):
        return None

    for name, value in {
        "store": roles, "session_store": sessions, "session_manager": SessionManager(roles, sessions),
        "multichannel_store": channels, "channel_host": host,
        "group_repository": MultichannelGroupRepository(channels),
        "model_configuration_store": ModelConfigurationStore(tmp_path / "models.json"),
        "memory_settings_store": settings, "runtime_manager": RuntimeManager(sessions),
        "role_locks": {}, "channel_event_locks": {}, "_fetch_channel_avatar": avatar,
    }.items():
        monkeypatch.setattr(main, name, value)
    client = TestClient(main.app)
    role = client.post("/api/roles", json={"name": "成员资料", "profile": {"profile": "设定"}}).json()["role"]
    empty = client.get(f"/api/roles/{role['id']}/tools").json()
    unavailable = next(item for item in empty["tools"] if item["name"] == "lookup_group_member")
    assert not unavailable["enabled"] and not unavailable["modelVisible"]
    account = channels.register_channel_account(role["id"], "qq", account_id="observed-qq", enabled=True, allow_group=True)

    async def rpc(operation, params):
        assert operation in {"get_friend_list", "get_group_list", "get_group_member_list"}
        return {"data": []}

    adapter = NapCatAdapter(NapCatConfig(account.account_id), transport=rpc)
    host.register(adapter)
    monkeypatch.setattr(main, "channel_adapters", {account.account_id: adapter})

    async def observe():
        for number, name in enumerate(("Alice", "艾丽丝")):
            await main._dispatch_channel_payload(account.account_id, {
                "post_type": "message", "message_type": "group", "self_id": 99,
                "group_id": 100, "message_id": f"observed-{number}",
                "sender": {"user_id": 22, "nickname": name},
                "message": [{"type": "text", "data": {"text": "UNTRIGGERED_RAW_CHAT"}}],
            }, source_adapter=adapter)

    asyncio.run(observe())
    channels.set_account_state(account.account_id, enabled=False)
    catalog = client.get(f"/api/roles/{role['id']}/tools").json()
    lookup = next(item for item in catalog["tools"] if item["name"] == "lookup_group_member")
    assert lookup["enabled"] and lookup["authorized"] and lookup["modelVisible"]
    assert lookup["dependency"]["status"] == "ready"
    assert "本地" in lookup["dependency"]["reason"]

    class Model:
        calls = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            assert "lookup_group_member" in {tool["function"]["name"] for tool in tools}
            if self.calls == 1:
                yield ModelToolCall("find-nickname", "lookup_group_member", {"name": "Alice"})
            else:
                result = next(item for item in reversed(messages) if item["role"] == "tool")
                assert result["content"].startswith("{"), result["content"]
                data = json.loads(result["content"])
                assert "UNTRIGGERED_RAW_CHAT" not in result["content"]
                if self.calls == 2:
                    assert data == {"total": 1, "candidates": [{"channel": "qq", "member_id": "22", "name": "艾丽丝", "nicknames": ["艾丽丝", "Alice"]}]}
                    yield ModelToolCall("read-profile", "lookup_group_member", {"member_id": "22", "member_channel": "qq"})
                else:
                    assert data["found"] and data["member_id"] == "22" and data["name"] == "艾丽丝"
                    assert data["sources"][0]["chat_id"] == "100"
                    yield ModelTextDelta("已按旧昵称查到当前成员")

    model = Model()
    monkeypatch.setattr(main, "model_adapter", model)
    response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "查 Alice 的成员资料"})
    assert "已按旧昵称查到当前成员" in response.text and model.calls == 3
    results = [item for item in sessions.list_messages(f"role:{role['id']}") if item.messageType == "tool_result"]
    assert len(results) == 2 and all(not item.isError for item in results)
    assert all(item["resultCategory"] == "succeeded" for item in sessions.list_tool_audits(results[0].runId))
