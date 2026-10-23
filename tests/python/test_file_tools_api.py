import base64
import io
import pytest

from fastapi.testclient import TestClient
from PIL import Image

from backend.app import main
from backend.app.agent_runtime.file_tools import FILE_TOOL_NAMES
from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from backend.app.model_config import ModelConfigurationStore
from backend.app.role_store import RoleStore
from backend.app.session_manager import SessionManager
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.memory_config import MemorySettingsStore

pytestmark = pytest.mark.usefixtures('local_platform_only')


def file_client(tmp_path, monkeypatch, adapter):
    roles = RoleStore(tmp_path / "roles")
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    memory_settings = MemorySettingsStore(tmp_path / 'data' / 'memory-settings.json')
    memory_settings.update(enabled=False)
    monkeypatch.setattr(main, 'memory_settings_store', memory_settings)
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", SessionManager(roles, sessions))
    monkeypatch.setattr(main, "model_adapter", adapter)
    monkeypatch.setattr(main, "model_configuration_store", ModelConfigurationStore(tmp_path / "data" / "model-config.json"))
    monkeypatch.setattr(main, "role_locks", {})
    client = TestClient(main.app)
    created = client.post("/api/roles", json={"name": "文件女仆", "profile": {"profile": "核心设定"}})
    assert created.status_code == 201
    role_id = created.json()["role"]["id"]
    return client, role_id, roles.workspace_path(role_id)


def authorize(client, role_id, names):
    response = client.put(f"/api/roles/{role_id}", json={
        "name": "文件女仆", "profile": {"profile": "核心设定"}, "agentConfig": {"enabledTools": sorted(names)}
    })
    assert response.status_code == 200


def test_http_file_tools_are_default_and_survive_legacy_empty_selection(tmp_path, monkeypatch):
    class Adapter:
        phase = "disabled"
        requests = []

        async def stream_structured_messages(self, messages, configuration, tools):
            del configuration
            self.requests.append((messages, tools))
            names = {item["function"]["name"] for item in tools}
            current_results = [item for item in messages if item["role"] == "tool"]
            if self.phase == "disabled":
                assert FILE_TOOL_NAMES <= names
                yield ModelTextDelta("文件工具默认可用")
            elif self.phase == "execute" and len(current_results) == 0:
                assert FILE_TOOL_NAMES <= names
                yield ModelToolCall("file-write", "write_file", {"path": "work/notes.txt", "content": "hello\n"})
                yield ModelToolCall("file-read", "read_file", {"path": "work/notes.txt"})
                yield ModelToolCall("file-edit", "edit_file", {"path": "work/notes.txt", "old_text": "hello", "new_text": "world"})
                yield ModelToolCall("file-list", "list_dir", {"path": "work"})
            elif self.phase == "execute":
                assert len(current_results) == 4
                assert "Wrote 6 UTF-8 bytes" in current_results[0]["content"]
                assert "1→hello" in current_results[1]["content"]
                assert "-hello" in current_results[2]["content"] and "+world" in current_results[2]["content"]
                assert "notes.txt" in current_results[3]["content"]
                yield ModelTextDelta("已根据真实文件结果完成")
            else:
                assert FILE_TOOL_NAMES <= names
                yield ModelTextDelta("基础工具仍可用")

    adapter = Adapter()
    client, role_id, workspace = file_client(tmp_path, monkeypatch, adapter)
    response = client.post(f"/api/roles/{role_id}/messages", json={"content": "查看工具"})
    assert "文件工具默认可用" in response.text
    authorize(client, role_id, FILE_TOOL_NAMES)
    adapter.phase = "execute"
    response = client.post(f"/api/roles/{role_id}/messages", json={"content": "创建、阅读、修改并列出文件"})
    assert "event: assistant_completed" in response.text
    assert "已根据真实文件结果完成" in response.text
    assert (workspace / "work/notes.txt").read_bytes() == b"world\n"
    session = client.get(f"/api/roles/{role_id}/session")
    assert session.status_code == 200
    assert "1→hello" in response.text
    assert any("已根据真实文件结果完成" in item["content"] for item in session.json()["messages"])
    authorize(client, role_id, [])
    adapter.phase = "revoked"
    response = client.post(f"/api/roles/{role_id}/messages", json={"content": "关闭后再尝试"})
    assert "基础工具仍可用" in response.text
    assert not (workspace / "forbidden.txt").exists()


def test_http_read_image_reaches_next_provider_without_persisting_base64(tmp_path, monkeypatch):
    image = io.BytesIO()
    Image.new("RGB", (3, 2), "red").save(image, "PNG")
    encoded = base64.b64encode(image.getvalue()).decode("ascii")

    class Adapter:
        requests = []

        async def stream_structured_messages(self, messages, configuration, tools):
            del configuration, tools
            self.requests.append(messages)
            if len(self.requests) == 1:
                yield ModelToolCall("file-image", "read_file", {"path": "image.png"})
            else:
                assert any(
                    block.get("type") == "image_url" and block["image_url"]["url"] == f"data:image/png;base64,{encoded}"
                    for item in messages if isinstance(item.get("content"), list) for block in item["content"]
                )
                yield ModelTextDelta("已看图")

    adapter = Adapter()
    client, role_id, workspace = file_client(tmp_path, monkeypatch, adapter)
    (workspace / "image.png").write_bytes(image.getvalue())
    authorize(client, role_id, ["read_file"])
    response = client.post(f"/api/roles/{role_id}/messages", json={"content": "读取图片"})
    assert "已看图" in response.text
    assert encoded not in response.text
    session = client.get(f"/api/roles/{role_id}/session")
    assert encoded not in session.text
