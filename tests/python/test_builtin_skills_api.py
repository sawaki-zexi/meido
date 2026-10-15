import json
import io

from fastapi.testclient import TestClient
from PIL import Image

from backend.app import main
from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from backend.app.model_config import ModelConfigurationStore
from backend.app.role_store import RoleStore
from backend.app.session_manager import SessionManager
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


def skill_client(tmp_path, monkeypatch, adapter, enabled_tools=()):
    roles = RoleStore(tmp_path / "roles")
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    for name, value in {
        "store": roles, "roles_root": tmp_path / "roles", "session_store": sessions,
        "session_manager": SessionManager(roles, sessions), "model_adapter": adapter,
        "model_configuration_store": ModelConfigurationStore(tmp_path / "data" / "models.json"),
        "role_locks": {},
    }.items():
        monkeypatch.setattr(main, name, value)
    client = TestClient(main.app)
    response = client.post("/api/roles", json={"name": "技能角色", "profile": {"profile": "核心设定"},
        "agentConfig": {"enabledTools": list(enabled_tools)}})
    assert response.status_code == 201
    return client, response.json()["role"]["id"], roles, sessions


def test_skill_created_by_real_file_tool_is_explicitly_usable_next_turn_without_granting_tools(tmp_path, monkeypatch):
    class Adapter:
        calls = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            names = {item["function"]["name"] for item in tools}
            assert "write_file" in names and "shell" not in names
            if self.calls == 1:
                assert "skills/<skill-id>/SKILL.md" in json.dumps(
                    [item for item in messages if item["role"] == "system"], ensure_ascii=False)
                yield ModelToolCall("create-skill", "write_file", {"path": "skills/briefing/SKILL.md", "content":
                    "---\nid: briefing\nversion: 1.0.0\ndescription: 当前角色的简报技能\n"
                    "tools: [shell]\nactivation: explicit\n---\n简报须注明当前已验证的来源。"})
            elif self.calls == 2:
                assert "Wrote" in next(item["content"] for item in reversed(messages) if item["role"] == "tool")
                yield ModelTextDelta("技能已创建")
            else:
                assert "简报须注明当前已验证的来源。" in json.dumps(
                    [item for item in messages if item["role"] == "system"], ensure_ascii=False)
                yield ModelTextDelta("已使用新技能")

    client, role_id, roles, sessions = skill_client(tmp_path, monkeypatch, Adapter(), ["write_file"])
    first = client.post(f"/api/roles/{role_id}/messages", json={"content": "创建简报技能", "skillIds": ["skill-creater"]})
    assert "技能已创建" in first.text
    assert (roles.workspace_path(role_id) / "skills/briefing/SKILL.md").is_file()
    second = client.post(f"/api/roles/{role_id}/messages", json={"content": "做一份简报", "skillIds": ["briefing"]})
    assert "已使用新技能" in second.text
    run_id = sessions.list_messages(f"role:{role_id}")[-1].runId
    snapshot = sessions.get_run(run_id).modelSnapshot["capabilities"]
    assert [(item["id"], item["source"], item["tools"]) for item in snapshot["skills"]] == [("briefing", "role", [])]
    assert any(item.get("tool") == "shell" for item in snapshot["skillDiagnostics"])


def test_meme_skill_updates_role_manifest_and_checks_actual_image_with_file_tools(tmp_path, monkeypatch):
    manifest = {"version": 1, "categories": {"happy": {"desc": "高兴", "aliases": ["开心"], "enabled": True}}}

    class Adapter:
        calls = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            assert "表情包维护" in json.dumps([item for item in messages if item["role"] == "system"], ensure_ascii=False)
            if self.calls == 1:
                yield ModelToolCall("list-memes", "list_dir", {"path": "memes"})
            elif self.calls == 2:
                assert "happy" in next(item["content"] for item in reversed(messages) if item["role"] == "tool")
                yield ModelToolCall("save-manifest", "write_file", {"path": "memes/manifest.json", "content": json.dumps(manifest, ensure_ascii=False)})
            elif self.calls == 3:
                yield ModelToolCall("check-manifest", "read_file", {"path": "memes/manifest.json"})
                yield ModelToolCall("check-image", "read_file", {"path": "memes/happy/001.png"})
            else:
                assert "开心" in json.dumps([item for item in messages if item["role"] == "tool"], ensure_ascii=False)
                assert any(block.get("type") == "image_url" for item in messages if isinstance(item.get("content"), list) for block in item["content"])
                yield ModelTextDelta("已更新类别并核验真实图片")

    client, role_id, roles, _ = skill_client(tmp_path, monkeypatch, Adapter(), ["read_file", "write_file", "list_dir"])
    directory = roles.workspace_path(role_id) / "memes" / "happy"
    directory.mkdir(parents=True)
    image = io.BytesIO()
    Image.new("RGB", (3, 2), "yellow").save(image, "PNG")
    (directory / "001.png").write_bytes(image.getvalue())
    response = client.post(f"/api/roles/{role_id}/messages", json={"content": "更新高兴类别的描述与别名并核验图片", "skillIds": ["meme-manage"]})
    assert "已更新类别并核验真实图片" in response.text
    assert json.loads((directory.parent / "manifest.json").read_text(encoding="utf-8")) == manifest
