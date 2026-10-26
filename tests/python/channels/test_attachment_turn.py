"""Real QQ parsing, attachment storage and Runtime; only remote IO is a fixture."""
import asyncio
import json
import re
import pytest

from backend.app import main
from backend.app.agent_runtime.runtime import RuntimeManager
from backend.app.channels import ChannelHost
from backend.app.channels import media
from backend.app.channels.group_tools import MultichannelGroupRepository
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.memory_config import MemorySettingsStore
from backend.app.model_adapter import ModelResponseDone, ModelTextDelta, ModelToolCall
from backend.app.model_config import ModelConfigurationStore
from backend.app.models import RoleInput, RoleProfile
from backend.app.multichannel import MultichannelStore
from backend.app.role_store import RoleStore
from backend.app.session_store import SessionStore


@pytest.mark.parametrize("file_ids_only", [False, True])
def test_qq_current_and_quoted_attachment_ids_roundtrip_but_old_ids_are_rejected(tmp_path, monkeypatch, file_ids_only):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="附件角色", profile=RoleProfile(profile="设定")))
    sessions = SessionStore(tmp_path / "sessions.db")
    channels = MultichannelStore(tmp_path / "sessions.db", sessions)
    channels.register_channel_account(role.id, "qq", account_id="qq-attachments", enabled=True, allow_group=True)
    settings = MemorySettingsStore(tmp_path / "memory-settings.json")
    settings.update(enabled=False)
    operations = []
    downloads = []

    async def transport(operation, params):
        operations.append((operation, params))
        if operation == "get_group_file_url":
            # NapCat v4.18.28 requires group_id/file_id strings, not the old guide's `group`.
            assert params == {"group_id": "100", "file_id": params["file_id"]}
            assert params["file_id"] in {"current-id", "quoted-id"}
            return {"data": {"url": "https://qq.test/" + params["file_id"].removesuffix("-id")}}
        if operation == "get_msg":
            assert params == {"message_id": "quoted-message"}
            return {"data": {"group_id": 100, "sender": {"user_id": 22, "nickname": "引用者"}, "message": [
                {"type": "file", "data": ({"name": "quoted.md", "file_id": "quoted-id", "size": "17"}
                    if file_ids_only else {"file": "quoted.md", "url": "https://qq.test/quoted"})},
            ]}}
        if operation in {"get_friend_list", "get_group_list", "get_group_member_list"}:
            return {"data": []}
        assert operation == "send_group_msg"
        return {"data": {"message_id": "outbound-fixture"}}

    async def download(url, *, max_bytes, resolved):
        downloads.append(url)
        assert resolved is file_ids_only
        data = {"https://qq.test/current": ("中" * 4000).encode(), "https://qq.test/quoted": b"quoted attachment"}[url]
        assert len(data) <= max_bytes
        return data, "text/plain" if url.endswith("current") else "text/markdown"

    async def avatar(*args, **kwargs):
        return None

    adapter = NapCatAdapter(NapCatConfig("qq-attachments"), transport=transport)
    host = ChannelHost(channels)
    host.register(adapter)
    for name, value in {
        "store": roles, "session_store": sessions, "multichannel_store": channels,
        "channel_host": host, "channel_adapters": {adapter.account_id: adapter},
        "group_repository": MultichannelGroupRepository(channels),
        "model_configuration_store": ModelConfigurationStore(tmp_path / "models.json"),
        "runtime_manager": RuntimeManager(sessions), "role_locks": {},
        "channel_event_locks": {}, "memory_settings_store": settings,
        "_channel_attachment_usage": {}, "_fetch_channel_avatar": avatar,
    }.items():
        monkeypatch.setattr(main, name, value)
    monkeypatch.setattr(media, "download_qq_file", download)

    class Model:
        calls = 0
        ids = {}
        reached_result = False

        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            assert "read_attachment" in {tool["function"]["name"] for tool in tools}
            assert not {"read_file", "shell"} & {tool["function"]["name"] for tool in tools}
            if self.calls == 1:
                current = next(item for item in reversed(messages) if item["role"] == "user")
                self.ids = dict(re.findall(r"([\w.]+)（attachment_id=([a-f0-9]{32})）", json.dumps(current["content"], ensure_ascii=False)))
                assert set(self.ids) == {"current.txt", "quoted.md"}
                yield ModelToolCall("read-current", "read_attachment", {"attachment_id": self.ids["current.txt"]})
                yield ModelToolCall("read-quoted", "read_attachment", {"attachment_id": self.ids["quoted.md"]})
                yield ModelResponseDone("tool_use")
            elif self.calls == 2:
                results = {item["tool_call_id"]: item["content"] for item in messages if item["role"] == "tool"}
                assert results["read-current"].startswith("中" * 2000 + "\n")
                assert "character_offset=2000" in results["read-current"]
                assert results["read-quoted"].startswith("quoted attachment\n")
                yield ModelToolCall("continue-current", "read_attachment", {"attachment_id": self.ids["current.txt"], "character_offset": 2000})
                yield ModelResponseDone("tool_use")
            else:
                last = next(item for item in reversed(messages) if item["role"] == "tool")
                assert last["tool_call_id"] == "continue-current"
                assert last["content"].startswith("中" * 2000 + "\n")
                assert "已读到文件末尾" in last["content"]
                self.reached_result = True
                yield ModelTextDelta("已读取附件")

    class OldIdModel:
        calls = 0
        reached_result = False

        def __init__(self, attachment_id):
            self.attachment_id = attachment_id

        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            if self.calls == 1:
                yield ModelToolCall("old-file", "read_attachment", {"attachment_id": self.attachment_id})
                yield ModelResponseDone("tool_use")
            else:
                result = next(item for item in reversed(messages) if item["role"] == "tool")
                assert result["tool_call_id"] == "old-file"
                assert "不属于当前 QQ 回合" in result["content"]
                assert "中中中" not in result["content"]
                self.reached_result = True
                yield ModelTextDelta("旧附件无权读取")

    def payload(message_id, *, group=100, files=False):
        segments = [{"type": "at", "data": {"qq": "99"}}, {"type": "text", "data": {"text": "读取附件"}}]
        if files:
            segments.extend([
                {"type": "file", "data": ({"name": "current.txt", "file_id": "current-id", "size": "12000"}
                    if file_ids_only else {"file": "current.txt", "url": "https://qq.test/current"})},
                {"type": "reply", "data": {"id": "quoted-message"}},
            ])
        return {"post_type": "message", "message_type": "group", "self_id": 99, "group_id": group,
                "message_id": message_id, "sender": {"user_id": 11, "nickname": "访客"}, "message": segments}

    async def scenario():
        first = Model()
        monkeypatch.setattr(main, "model_adapter", first)
        await main._dispatch_channel_payload(adapter.account_id, payload("first", files=True), source_adapter=adapter)
        assert first.reached_result, "current and quoted results must reach the next model request"
        thread = channels.ensure_channel_thread(role.id, "qq", adapter.account_id, "100", "group")
        messages = sessions.list_messages(thread.session_key)
        user = next(item for item in messages if item.role == "user")
        assert {item["name"] for key in ("attachments", "quotedAttachments") for item in user.metadata[key]} == {"current.txt", "quoted.md"}
        results = [item for item in messages if item.messageType == "tool_result"]
        assert len(results) == 3 and all(not item.isError for item in results)
        assert sessions.get_run(results[0].runId).status == "completed"

        for group in (100, 200):
            later = OldIdModel(first.ids["current.txt"])
            monkeypatch.setattr(main, "model_adapter", later)
            await main._dispatch_channel_payload(adapter.account_id, payload(f"later-{group}", group=group), source_adapter=adapter)
            assert later.reached_result, "old IDs must be rejected in later turns and other groups"
            target = channels.ensure_channel_thread(role.id, "qq", adapter.account_id, str(group), "group")
            old_result = next(item for item in reversed(sessions.list_messages(target.session_key)) if item.messageType == "tool_result")
            assert old_result.isError

    asyncio.run(asyncio.wait_for(scenario(), timeout=15))
    assert downloads == ["https://qq.test/current", "https://qq.test/quoted"]
    assert [operation for operation, _ in operations if operation in {"get_msg", "send_group_msg"}] == ["get_msg", "send_group_msg", "send_group_msg", "send_group_msg"]
