from fastapi.testclient import TestClient
from starlette.requests import Request
from datetime import datetime, timezone
import asyncio
import json
import sqlite3
import pytest

from backend.app import main
from backend.app.agent_runtime import AgentToolResult, PluginContribution, PluginManifest, PluginRegistry, ToolContext, ToolDefinition
from backend.app.agent_runtime.types import ToolResultMessage
from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from backend.app.models import AgentRun, AgentToolConfig, Message, ModelConfigurationInput, RoleInput, RoleProfile, RoleUpdateInput, SendMessageInput, ShellToolConfig
from backend.app.model_config import ModelConfigurationStore
from backend.app.role_store import RoleStore
from backend.app.session_manager import SessionManager
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.memory_service import MemoryService, MemoryWorker
from backend.app.memory_store import MemoryStore


class FailingAdapter:
    async def stream_reply(self, role, history, configuration=None):
        yield "部分内容"
        raise RuntimeError("模型连接中断")


class SuccessAdapter:
    def __init__(self):
        self.histories = []

    async def stream_reply(self, role, history, configuration=None):
        assert role.profile.profile == "核心设定"
        assert history[-1].content == "你好"
        self.histories.append(history)
        yield "你好，"
        yield "主人。"


class RecordingAdapter:
    def __init__(self):
        self.profiles = []

    async def stream_reply(self, role, history, configuration=None):
        self.profiles.append(role.profile.profile)
        yield "回复"


class ConfigurationRecordingAdapter:
    def __init__(self):
        self.configuration_ids = []

    async def stream_reply(self, role, history, configuration=None):
        self.configuration_ids.append(configuration.id if configuration else None)
        yield "回复"


class RebindingAdapter:
    def __init__(self, roles, role_id, next_configuration_id):
        self.roles = roles
        self.role_id = role_id
        self.next_configuration_id = next_configuration_id
        self.configuration_ids = []

    async def stream_reply(self, role, history, configuration=None):
        self.configuration_ids.append(configuration.id if configuration else None)
        if len(self.configuration_ids) == 1:
            self.roles.set_model_configuration(self.role_id, self.next_configuration_id)
        yield "回复"


def model_configuration_payload(model="test-model"):
    return ModelConfigurationInput(
        providerId="openai",
        provider="openai",
        baseUrl="https://api.openai.com/v1",
        model=model,
    )


def configure_session_api(tmp_path, monkeypatch, adapter):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="测试女仆", profile=RoleProfile(profile="核心设定", personality="温柔")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", SessionManager(roles, sessions))
    monkeypatch.setattr(main, "model_adapter", adapter)
    monkeypatch.setattr(main, "model_configuration_store", ModelConfigurationStore(tmp_path / "data" / "model-config.json"))
    monkeypatch.setattr(main, "role_locks", {})
    return role, sessions, TestClient(main.app)
def test_role_api_crud_and_duplicate_names(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "store", RoleStore(tmp_path / "roles"))
    client = TestClient(main.app)
    payload = {"name": "测试角色", "profile": {"profile": "核心设定"}}

    first_response = client.post("/api/roles", json=payload)
    second_response = client.post("/api/roles", json=payload)
    assert first_response.status_code == 201
    assert second_response.status_code == 201
    first = first_response.json()["role"]
    second = second_response.json()["role"]
    assert first["id"] != second["id"]

    updated_response = client.put(
        f"/api/roles/{first['id']}",
        json={"name": "改名", "profile": {"profile": "更新设定"}},
    )
    assert updated_response.status_code == 200
    assert updated_response.json()["role"]["id"] == first["id"]
    assert client.get(f"/api/roles/{first['id']}").json()["role"]["name"] == "改名"
    assert len(client.get("/api/roles").json()["roles"]) == 2


def test_role_avatar_can_be_uploaded_replaced_loaded_and_removed(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="测试角色", profile=RoleProfile(profile="核心设定")))
    monkeypatch.setattr(main, "store", roles)
    client = TestClient(main.app)
    png = b"\x89PNG\r\n\x1a\nportrait"
    jpeg = b"\xff\xd8\xffportrait"
    endpoint = f"/api/roles/{role.id}/avatar"

    uploaded = client.post(endpoint, content=png, headers={"content-type": "image/png"})

    assert uploaded.status_code == 200
    assert uploaded.json()["role"]["avatarUrl"].startswith(endpoint)
    assert client.get(endpoint).content == png
    assert client.get(endpoint).headers["content-type"] == "image/png"
    assert RoleStore(tmp_path / "roles").get(role.id).avatarUrl.startswith(endpoint)

    replaced = client.post(endpoint, content=jpeg, headers={"content-type": "image/jpeg"})
    assert replaced.status_code == 200
    assert client.get(endpoint).content == jpeg
    assert client.get(endpoint).headers["content-type"] == "image/jpeg"

    removed = client.delete(endpoint)
    assert removed.status_code == 204
    assert client.get(endpoint).status_code == 404
    assert client.get(f"/api/roles/{role.id}").json()["role"]["avatarUrl"] is None


def test_role_card_image_is_independent_persistent_and_removable(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="测试角色", profile=RoleProfile(profile="核心设定")))
    monkeypatch.setattr(main, "store", roles)
    client = TestClient(main.app)
    avatar = b"\x89PNG\r\n\x1a\navatar"
    card_image = b"\xff\xd8\xffcard"
    avatar_endpoint = f"/api/roles/{role.id}/avatar"
    card_endpoint = f"/api/roles/{role.id}/card-image"
    client.post(avatar_endpoint, content=avatar, headers={"content-type": "image/png"})

    uploaded = client.post(card_endpoint, content=card_image, headers={"content-type": "image/jpeg"})

    assert uploaded.status_code == 200
    assert uploaded.json()["role"]["avatarUrl"].startswith(avatar_endpoint)
    assert uploaded.json()["role"]["cardImageUrl"].startswith(card_endpoint)
    assert client.get(avatar_endpoint).content == avatar
    assert client.get(card_endpoint).content == card_image
    assert RoleStore(tmp_path / "roles").get(role.id).cardImageUrl.startswith(card_endpoint)

    removed = client.delete(card_endpoint)
    assert removed.status_code == 204
    assert client.get(card_endpoint).status_code == 404
    assert client.get(f"/api/roles/{role.id}").json()["role"]["avatarUrl"].startswith(avatar_endpoint)
    assert client.get(f"/api/roles/{role.id}").json()["role"]["cardImageUrl"] is None


def test_invalid_role_avatar_is_rejected_without_replacing_existing_image(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="测试角色", profile=RoleProfile(profile="核心设定")))
    monkeypatch.setattr(main, "store", roles)
    client = TestClient(main.app)
    endpoint = f"/api/roles/{role.id}/avatar"
    original = b"\x89PNG\r\n\x1a\nportrait"
    client.post(endpoint, content=original, headers={"content-type": "image/png"})

    wrong_type = client.post(endpoint, content=b"not an image", headers={"content-type": "text/plain"})
    oversized = client.post(
        endpoint,
        content=b"\x89PNG\r\n\x1a\n" + b"x" * (10 * 1024 * 1024),
        headers={"content-type": "image/png"},
    )

    assert wrong_type.status_code == 415
    assert oversized.status_code == 413
    assert client.get(endpoint).content == original


def test_avatar_storage_failure_restores_the_previous_image(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="测试角色", profile=RoleProfile(profile="核心设定")))
    monkeypatch.setattr(main, "store", roles)
    client = TestClient(main.app)
    endpoint = f"/api/roles/{role.id}/avatar"
    original = b"\x89PNG\r\n\x1a\noriginal"
    client.post(endpoint, content=original, headers={"content-type": "image/png"})

    def fail_save(_roles=None):
        raise OSError("磁盘写入失败")

    monkeypatch.setattr(roles, "_save", fail_save)
    failed = client.post(endpoint, content=b"\xff\xd8\xffreplacement", headers={"content-type": "image/jpeg"})

    assert failed.status_code == 500
    assert client.get(endpoint).content == original
    assert roles.get(role.id).avatarMediaType == "image/png"


def test_role_model_configuration_api_persists_and_only_changes_selected_role(tmp_path, monkeypatch):
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    other = main.store.create(RoleInput(name="另一个角色", profile=RoleProfile(profile="其他设定")))
    client.post(f"/api/roles/{role.id}/messages", json={"content": "历史消息"})
    before_messages = sessions.list_messages(f"role:{role.id}")
    configuration = main.model_configuration_store.create(model_configuration_payload())

    response = client.put(
        f"/api/roles/{role.id}/model-configuration",
        json={"configurationId": configuration.id},
    )

    assert response.status_code == 200
    assert response.json()["configurationId"] == configuration.id
    assert client.get(f"/api/roles/{role.id}/model-configuration").json()["configurationId"] == configuration.id
    assert client.get(f"/api/roles/{other.id}/model-configuration").json()["configurationId"] is None
    assert RoleStore(tmp_path / "roles").get(role.id).modelConfigurationId == configuration.id
    assert client.get(f"/api/roles/{role.id}/session").json()["session"]["sessionKey"] == f"role:{role.id}"
    assert [item.model_dump() for item in sessions.list_messages(f"role:{role.id}")] == [
        item.model_dump() for item in before_messages
    ]


def test_role_model_configuration_api_rejects_missing_configuration_without_changing_binding(tmp_path, monkeypatch):
    role, _, client = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    configuration = main.model_configuration_store.create(model_configuration_payload())
    client.put(
        f"/api/roles/{role.id}/model-configuration",
        json={"configurationId": configuration.id},
    )

    response = client.put(
        f"/api/roles/{role.id}/model-configuration",
        json={"configurationId": "missing-model"},
    )

    assert response.status_code == 404
    assert client.get(f"/api/roles/{role.id}/model-configuration").json()["configurationId"] == configuration.id


def test_role_model_configuration_api_failure_keeps_previous_binding(tmp_path, monkeypatch):
    role, _, client = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    first = main.model_configuration_store.create(model_configuration_payload("first"))
    second = main.model_configuration_store.create(model_configuration_payload("second"))
    client.put(f"/api/roles/{role.id}/model-configuration", json={"configurationId": first.id})

    def fail_save(roles=None):
        raise OSError("磁盘写入失败")

    monkeypatch.setattr(main.store, "_save", fail_save)
    response = client.put(
        f"/api/roles/{role.id}/model-configuration",
        json={"configurationId": second.id},
    )

    assert response.status_code == 500
    assert client.get(f"/api/roles/{role.id}/model-configuration").json()["configurationId"] == first.id


def test_role_messages_use_bound_configuration_and_reject_deleted_binding(tmp_path, monkeypatch):
    adapter = ConfigurationRecordingAdapter()
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, adapter)
    other = main.store.create(RoleInput(name="另一个角色", profile=RoleProfile(profile="其他设定")))
    first = main.model_configuration_store.create(model_configuration_payload("first"))
    second = main.model_configuration_store.create(model_configuration_payload("second"))
    client.put(f"/api/roles/{role.id}/model-configuration", json={"configurationId": first.id})
    client.put(f"/api/roles/{other.id}/model-configuration", json={"configurationId": second.id})

    assert "event: assistant_completed" in client.post(f"/api/roles/{role.id}/messages", json={"content": "第一个"}).text
    assert "event: assistant_completed" in client.post(f"/api/roles/{other.id}/messages", json={"content": "第二个"}).text
    assert adapter.configuration_ids == [first.id, second.id]

    main.model_configuration_store.delete(first.id)
    response = client.post(f"/api/roles/{role.id}/messages", json={"content": "不应写入"})
    assert response.status_code == 409
    assert [item.content for item in sessions.list_messages(f"role:{role.id}")] == ["第一个", "回复"]


def test_role_message_keeps_configuration_snapshot_when_binding_changes_during_stream(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="测试角色", profile=RoleProfile(profile="核心设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    configurations = ModelConfigurationStore(tmp_path / "data" / "model-config.json")
    first = configurations.create(model_configuration_payload("first"))
    second = configurations.create(model_configuration_payload("second"))
    roles.set_model_configuration(role.id, first.id)
    adapter = RebindingAdapter(roles, role.id, second.id)
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", SessionManager(roles, sessions))
    monkeypatch.setattr(main, "model_adapter", adapter)
    monkeypatch.setattr(main, "model_configuration_store", configurations)
    monkeypatch.setattr(main, "role_locks", {})
    client = TestClient(main.app)

    first_response = client.post(f"/api/roles/{role.id}/messages", json={"content": "配置 A"})
    second_response = client.post(f"/api/roles/{role.id}/messages", json={"content": "配置 B"})

    assert "event: assistant_completed" in first_response.text
    assert "event: assistant_completed" in second_response.text
    assert adapter.configuration_ids == [first.id, second.id]


def test_unbound_role_uses_active_model_configuration(tmp_path, monkeypatch):
    adapter = ConfigurationRecordingAdapter()
    role, _, client = configure_session_api(tmp_path, monkeypatch, adapter)
    configuration = main.model_configuration_store.create(model_configuration_payload())

    response = client.post(f"/api/roles/{role.id}/messages", json={"content": "默认模型"})

    assert "event: assistant_completed" in response.text
    assert adapter.configuration_ids == [configuration.id]


def test_role_message_snapshot_does_not_persist_legacy_role_secrets(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(
        name="带旧配置的角色",
        profile=RoleProfile(profile="核心设定"),
        modelConfig={"apiKey": "legacy-secret", "provider": "openai"},
    ))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", SessionManager(roles, sessions))
    monkeypatch.setattr(main, "model_adapter", RecordingAdapter())
    monkeypatch.setattr(main, "model_configuration_store", ModelConfigurationStore(tmp_path / "data" / "model-config.json"))
    monkeypatch.setattr(main, "role_locks", {})
    client = TestClient(main.app)

    response = client.post(f"/api/roles/{role.id}/messages", json={"content": "你好"})

    assert response.status_code == 200
    run_id = sessions.list_messages(f"role:{role.id}")[0].runId
    assert run_id is not None
    run = sessions.get_run(run_id)
    assert run is not None
    assert "legacy-secret" not in str(run.modelSnapshot)
    assert "modelConfig" not in run.modelSnapshot["role"]
    assert run.modelSnapshot["context"]["toolAllowlist"] == []
    assert run.modelSnapshot["capabilities"]["tools"] == []
    assert run.modelSnapshot["context"]["messages"] == [{"content": "你好", "role": "user"}]


def test_enabled_role_shell_runs_through_http_runtime_and_returns_tool_result(tmp_path, monkeypatch):
    import sys
    from pathlib import Path

    executable = str(Path(sys.executable)).replace("\\", "/")
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(
        name="可执行角色",
        profile=RoleProfile(profile="核心设定"),
        agentConfig=AgentToolConfig(shell=ShellToolConfig(enabled=True, allowedCommands=[executable])),
    ))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")

    class StructuredAdapter:
        def __init__(self):
            self.requests = []

        async def stream_structured_messages(self, messages, configuration, tools):
            del configuration
            self.requests.append((messages, tools))
            if len(self.requests) == 1:
                yield ModelToolCall("call-shell", "shell", {
                    "command": f'{executable} -c "print(\'shell-ok\')"',
                })
            else:
                assert any(
                    message.get("role") == "tool"
                    and isinstance(message.get("content"), str)
                    and message["content"].strip() == "shell-ok"
                    for message in messages
                )
                yield ModelTextDelta("完成")

    adapter = StructuredAdapter()
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", SessionManager(roles, sessions))
    monkeypatch.setattr(main, "model_adapter", adapter)
    monkeypatch.setattr(main, "model_configuration_store", ModelConfigurationStore(tmp_path / "data" / "model-config.json"))
    monkeypatch.setattr(main, "role_locks", {})

    response = TestClient(main.app).post(f"/api/roles/{role.id}/messages", json={"content": "执行命令"})

    assert response.status_code == 200
    assert "event: assistant_completed" in response.text
    assert len(adapter.requests) == 2
    assert adapter.requests[0][1][0]["function"]["name"] == "shell"
    messages = sessions.runtime_messages(f"role:{role.id}")
    assert any(isinstance(message, ToolResultMessage) and message.content.strip() == "shell-ok" for message in messages)
    run = sessions.list_messages(f"role:{role.id}")[-1].runId
    assert run is not None
    stored_run = sessions.get_run(run)
    assert stored_run is not None
    assert stored_run.modelSnapshot["context"]["toolAllowlist"] == [executable]
    assert stored_run.modelSnapshot["capabilities"]["tools"][0]["name"] == "shell"
    assert stored_run.modelSnapshot["capabilities"]["tools"][0]["source"] == "role"
    assert stored_run.modelSnapshot["capabilities"]["tools"][0]["timeoutSeconds"] == 30.0
    assert stored_run.modelSnapshot["capabilities"]["tools"][0]["outputLimit"] == 20000


def test_role_capability_config_controls_plugin_resolution(tmp_path, monkeypatch):
    role, _, _ = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())

    class PluginEchoTool:
        definition = ToolDefinition(
            name="plugin.echo",
            description="Echo a message",
            input_schema={"type": "object", "properties": {"message": {"type": "string"}}},
            source="plugin:demo",
        )

        def execute(self, arguments, context: ToolContext, on_update=None):
            del context, on_update
            return AgentToolResult(str(arguments["message"]))

    registry = PluginRegistry([
        (
            PluginManifest("demo", "1.0.0"),
            lambda context: PluginContribution(tools=(PluginEchoTool(),)),
        ),
    ])
    monkeypatch.setattr(main, "plugin_registry", registry)

    updated = main.store.update(
        role.id,
        RoleUpdateInput(
            name=role.name,
            profile=role.profile,
            agentConfig=AgentToolConfig(
                enabledTools=["plugin.echo"],
                enabledPlugins=["demo"],
            ),
        ),
    )
    capabilities = asyncio.run(main._runtime_capabilities(
        updated,
        session_key=f"role:{role.id}",
        run_id="run-plugin-config",
        prompt_text="使用插件",
    ))

    assert [definition.name for definition in capabilities.snapshot.tools] == ["plugin.echo"]
    assert capabilities.snapshot.plugins[0].manifest.plugin_id == "demo"
    assert capabilities.snapshot.policy_decisions["plugin.echo"] == "allowed"

    updated.agentConfig.enabledTools.clear()
    assert capabilities.get("plugin.echo") is not None
    asyncio.run(capabilities.close())


def test_role_skill_is_injected_into_provider_and_run_snapshot(tmp_path, monkeypatch):
    class SkillRecordingAdapter:
        def __init__(self):
            self.memory_contexts = []

        async def stream_reply(self, role, history, configuration=None, *, memory_context=""):
            del role, history, configuration
            self.memory_contexts.append(memory_context)
            yield "技能回复"

    adapter = SkillRecordingAdapter()
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, adapter)
    monkeypatch.setattr(main, "roles_root", tmp_path / "roles")
    skill_dir = tmp_path / "roles" / role.id / "skills" / "study-plan"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
id: study-plan
version: 1.0.0
description: 制定学习计划
tools:
  - memory.read
activation: role_default
---
请先参考已有记忆，再给出分阶段学习计划。
""",
        encoding="utf-8",
    )

    response = client.post(f"/api/roles/{role.id}/messages", json={"content": "制定学习计划"})

    assert response.status_code == 200
    assert "event: assistant_completed" in response.text
    assert adapter.memory_contexts == ["请先参考已有记忆，再给出分阶段学习计划。"]
    run_id = sessions.list_messages(f"role:{role.id}")[0].runId
    assert run_id is not None
    run = sessions.get_run(run_id)
    assert run is not None
    snapshot = run.modelSnapshot["capabilities"]
    assert snapshot["skills"][0]["id"] == "study-plan"
    assert snapshot["skills"][0]["version"] == "1.0.0"
    assert snapshot["skills"][0]["source"] == "role"
    assert snapshot["skills"][0]["tools"] == []
    assert "promptSections" not in snapshot
    assert "请先参考已有记忆，再给出分阶段学习计划。" not in json.dumps(snapshot, ensure_ascii=False)
    assert any(
        item["skillId"] == "study-plan" and item["tool"] == "memory.read"
        for item in snapshot["skillDiagnostics"]
    )


def test_role_message_can_explicitly_activate_skill(tmp_path, monkeypatch):
    adapter = RecordingAdapter()
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, adapter)
    monkeypatch.setattr(main, "roles_root", tmp_path / "roles")
    skill_dir = tmp_path / "roles" / role.id / "skills" / "explicit-help"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
id: explicit-help
version: 1.0.0
description: 仅显式启用
activation: explicit
---
显式规则。
""",
        encoding="utf-8",
    )

    response = client.post(
        f"/api/roles/{role.id}/messages",
        json={"content": "普通问题", "skillIds": ["explicit-help"]},
    )

    assert response.status_code == 200
    run_id = sessions.list_messages(f"role:{role.id}")[0].runId
    assert run_id is not None
    run = sessions.get_run(run_id)
    assert run is not None
    assert run.modelSnapshot["capabilities"]["skills"][0]["id"] == "explicit-help"


def test_role_update_preserves_session_messages_and_changes_future_prompt(tmp_path, monkeypatch):
    adapter = RecordingAdapter()
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, adapter)
    session_key = f"role:{role.id}"
    first = client.post(f"/api/roles/{role.id}/messages", json={"content": "第一条"})
    assert "event: assistant_completed" in first.text
    messages_before_update = sessions.list_messages(session_key)

    updated = client.put(
        f"/api/roles/{role.id}",
        json={
            "name": "改名",
            "description": "新简介",
            "profile": {
                "profile": "新设定",
                "personality": "活泼",
                "behaviorRules": "先倾听",
                "responseConstraints": "简洁",
            },
        },
    )
    assert updated.status_code == 200
    assert updated.json()["role"]["id"] == role.id
    assert RoleStore(tmp_path / "roles").get(role.id).profile.profile == "新设定"
    assert [message.model_dump() for message in sessions.list_messages(session_key)] == [
        message.model_dump() for message in messages_before_update
    ]

    second = client.post(f"/api/roles/{role.id}/messages", json={"content": "第二条"})
    assert "event: assistant_completed" in second.text
    assert adapter.profiles == ["核心设定", "新设定"]
    assert client.get(f"/api/roles/{role.id}/session").json()["session"]["sessionKey"] == session_key


def test_role_update_failure_keeps_persisted_role_unchanged(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="原角色", profile=RoleProfile(profile="原设定")))
    manifest_before = (tmp_path / "roles" / "roles.json").read_bytes()
    monkeypatch.setattr(main, "store", roles)

    def fail_save(candidate=None):
        raise OSError("磁盘写入失败")

    monkeypatch.setattr(roles, "_save", fail_save)
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.put(
        f"/api/roles/{role.id}",
        json={"name": "新角色", "profile": {"profile": "新设定"}},
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "角色保存失败：磁盘写入失败"
    assert roles.get(role.id).name == "原角色"
    assert (tmp_path / "roles" / "roles.json").read_bytes() == manifest_before


def test_delete_role_removes_role_session_messages_and_files(tmp_path, monkeypatch):
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    other = main.store.create(RoleInput(name="其他角色", profile=RoleProfile(profile="其他设定")))
    client.post(f"/api/roles/{role.id}/messages", json={"content": "待删除消息"})
    client.get(f"/api/roles/{other.id}/session")

    response = client.delete(f"/api/roles/{role.id}")

    assert response.status_code == 204
    assert client.get(f"/api/roles/{role.id}").status_code == 404
    assert client.get(f"/api/roles/{role.id}/session").status_code == 404
    assert sessions.list_messages(f"role:{role.id}") == []
    assert not (tmp_path / "roles" / role.id).exists()
    assert client.get(f"/api/roles/{other.id}").status_code == 200
    assert client.get(f"/api/roles/{other.id}/session").status_code == 200
    assert all(item.id != role.id for item in main.store.list())


def test_delete_role_cleans_optimizer_maintenance_state_and_only_target_sqlite_rows(tmp_path, monkeypatch):
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    other = main.store.create(RoleInput(name="其他角色", profile=RoleProfile(profile="其他设定")))
    memory_store = MemoryStore(tmp_path / "data" / "memory.db")
    memory_service = MemoryService(memory_store)
    from backend.app.memory_optimizer import MemoryOptimizer, MemoryOptimizerWorker

    optimizer_worker = MemoryOptimizerWorker(MemoryOptimizer(tmp_path / "roles"))
    worker = MemoryWorker(memory_service, optimizer=optimizer_worker)
    monkeypatch.setattr(main, "memory_store", memory_store)
    monkeypatch.setattr(main, "memory_service", memory_service)
    monkeypatch.setattr(main, "memory_worker", worker)
    doomed = memory_service.remember(role.id, "待删除记忆", "fact", stable_source_key="shared-source")
    survivor = memory_service.remember(other.id, "保留记忆", "fact", stable_source_key="shared-source")
    with sqlite3.connect(tmp_path / "data" / "memory.db") as connection:
        connection.execute(
            "INSERT INTO consolidation_events(role_id, source_ref, item_id, created_at) VALUES (?, ?, ?, ?)",
            (role.id, "shared-source", doomed.id, "2026-01-01T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO memory_replacements(role_id, old_item_id, old_memory_type, old_summary, new_item_id, new_memory_type, new_summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (role.id, "old", "fact", "旧", doomed.id, "fact", "新", "2026-01-01T00:00:00+00:00"),
        )
    memory_dir = tmp_path / "roles" / role.id / "memory"
    (memory_dir / "PENDING.snapshot.md").write_text("待恢复候选", encoding="utf-8")
    (memory_dir / ".maintenance.json").write_text('{"pendingEvent": {}}', encoding="utf-8")
    (memory_dir / ".optimizer.json").write_text('{"self_update_pending": true}', encoding="utf-8")
    (memory_dir / "journal").mkdir(exist_ok=True)
    (memory_dir / "journal" / "2026-01-01.md").write_text("日志", encoding="utf-8")

    assert client.delete(f"/api/roles/{role.id}").status_code == 204
    assert not (tmp_path / "roles" / role.id).exists()
    assert not list((tmp_path / "roles").glob(f".deleted-{role.id}-*"))
    assert memory_store.get(role.id, doomed.id) is None
    assert memory_store.get(other.id, survivor.id).summary == "保留记忆"
    with sqlite3.connect(tmp_path / "data" / "memory.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM consolidation_events WHERE role_id = ?", (role.id,)).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM memory_replacements WHERE role_id = ?", (role.id,)).fetchone()[0] == 0
    assert worker.submit(role.id, "role:" + role.id, Message(id="u", sessionKey="role:" + role.id, sequence=1, role="user", content="记住", status="completed", createdAt=datetime.now(timezone.utc)), Message(id="a", sessionKey="role:" + role.id, sequence=2, role="assistant", content="好", status="completed", createdAt=datetime.now(timezone.utc))) is False


def test_role_store_removes_stale_deleted_directory_on_restart(tmp_path):
    roles_root = tmp_path / "roles"
    stale = roles_root / ".deleted-role-old-token" / "memory"
    stale.mkdir(parents=True)
    (stale / "PENDING.snapshot.md").write_text("旧候选", encoding="utf-8")
    RoleStore(roles_root)
    assert not stale.parent.exists()


def test_role_store_restores_staged_directory_when_manifest_still_contains_role(tmp_path):
    roles_root = tmp_path / "roles"
    roles = RoleStore(roles_root)
    role = roles.create(RoleInput(name="待恢复", profile=RoleProfile(profile="设定")))
    role_file = roles_root / role.id / "memory" / "MEMORY.md"
    role_file.write_text("保留", encoding="utf-8")
    staged = roles.stage_role_files_for_deletion(role.id)
    assert staged is not None
    restarted = RoleStore(roles_root)
    assert restarted.get(role.id) is not None
    assert (roles_root / role.id / "memory" / "MEMORY.md").read_text(encoding="utf-8") == "保留"
    assert not staged.exists()


def test_role_store_keeps_interrupted_deletion_marker_until_database_recovery(tmp_path):
    roles_root = tmp_path / "roles"
    roles = RoleStore(roles_root)
    role = roles.create(RoleInput(name="待清理", profile=RoleProfile(profile="设定")))
    marker = roles.begin_role_deletion(role.id)
    staged = roles.stage_role_files_for_deletion(role.id)
    roles.delete(role.id)
    restarted = RoleStore(roles_root)
    assert restarted.pending_role_deletions() == [role.id]
    assert staged is not None and not staged.exists()
    restarted.complete_role_deletion(marker)
    assert restarted.pending_role_deletions() == []


def test_role_store_removes_canonical_directory_when_manifest_delete_precedes_staging(tmp_path):
    roles_root = tmp_path / "roles"
    roles = RoleStore(roles_root)
    role = roles.create(RoleInput(name="孤儿", profile=RoleProfile(profile="设定")))
    marker = roles.begin_role_deletion(role.id)
    (roles_root / role.id / "memory" / "PENDING.md").write_text("候选", encoding="utf-8")
    roles.delete(role.id)
    restarted = RoleStore(roles_root)
    assert not (roles_root / role.id).exists()
    assert restarted.pending_role_deletions() == [role.id]
    restarted.complete_role_deletion(marker)


def test_startup_finishes_interrupted_role_deletion_without_recreating_data(tmp_path, monkeypatch):
    role, sessions, _ = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    roles = main.store
    other = roles.create(RoleInput(name="保留", profile=RoleProfile(profile="设定")))
    memories = MemoryStore(tmp_path / "data" / "memory.db")
    service = MemoryService(memories)
    service.remember(role.id, "同一记忆", "fact", stable_source_key="same")
    survivor = service.remember(other.id, "同一记忆", "fact", stable_source_key="same")
    sessions.open_role_session(role.id)
    marker = roles.begin_role_deletion(role.id)
    roles.stage_role_files_for_deletion(role.id)
    roles.delete(role.id)
    # Crash before either SQLite store was cleaned. Reopen every component.
    restarted = RoleStore(tmp_path / "roles")
    memories = MemoryStore(tmp_path / "data" / "memory.db")
    service = MemoryService(memories)
    from backend.app.memory_optimizer import MemoryOptimizer, MemoryOptimizerWorker, MemoryOptimizerLoop
    from backend.app.memory_maintenance import MemoryMaintenance

    optimizer = MemoryOptimizerWorker(MemoryOptimizer(tmp_path / "roles"))
    worker = MemoryWorker(service, optimizer=optimizer)
    monkeypatch.setattr(main, "store", restarted)
    monkeypatch.setattr(main, "memory_store", memories)
    monkeypatch.setattr(main, "memory_worker", worker)
    monkeypatch.setattr(main, "memory_maintenance", MemoryMaintenance(tmp_path / "roles", sessions))
    monkeypatch.setattr(main, "memory_optimizer_loop", MemoryOptimizerLoop(optimizer, tmp_path / "roles", enabled=False))
    asyncio.run(main._start_memory_workers())
    assert all(not rows for rows in memories.snapshot_role(role.id).values())
    assert sessions.snapshot_role_session(role.id) == (None, [], [])
    assert memories.get(other.id, survivor.id) is not None
    assert restarted.get(role.id) is None
    assert not (tmp_path / "roles" / role.id).exists()
    assert not marker.exists()
    assert restarted.pending_role_deletions() == []


def test_role_file_mutations_cannot_recreate_directory_during_deletion(tmp_path):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="删除中", profile=RoleProfile(profile="设定")))
    marker = roles.begin_role_deletion(role.id)
    staged = roles.stage_role_files_for_deletion(role.id)
    with pytest.raises(KeyError):
        roles.set_avatar(role.id, b"avatar", "image/png")
    assert not (tmp_path / "roles" / role.id).exists()
    roles.restore_staged_role_files(role.id, staged)
    roles.complete_role_deletion(marker)
    assert roles.set_avatar(role.id, b"avatar", "image/png") is not None


def test_delete_waits_for_admin_write_and_rejects_late_mutation(tmp_path, monkeypatch):
    role, _, _ = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    memories = MemoryStore(tmp_path / "data" / "memory.db")
    worker = MemoryWorker(MemoryService(memories))
    monkeypatch.setattr(main, "memory_store", memories)
    monkeypatch.setattr(main, "memory_worker", worker)
    monkeypatch.setattr(main, "role_locks", {})
    monkeypatch.setattr(main, "roles_root", tmp_path / "roles")

    async def run():
        started = asyncio.Event()
        release = asyncio.Event()

        async def operation():
            started.set()
            await release.wait()
            return worker.service.remember(role.id, "先完成写入", "fact")

        mutation = asyncio.create_task(main._mutate_and_sync_memory(role.id, operation))
        await started.wait()
        deleting = asyncio.create_task(main.delete_role(role.id))
        await asyncio.sleep(0)
        assert not deleting.done()
        with pytest.raises(main.HTTPException) as error:
            await main._mutate_and_sync_memory(role.id, operation)
        assert error.value.status_code == 409
        release.set()
        await mutation
        await deleting
        with pytest.raises(main.HTTPException):
            await main._mutate_and_sync_memory(role.id, operation)

    asyncio.run(run())
    assert memories.list_all(role.id) == []
    assert not (tmp_path / "roles" / role.id).exists()


def test_cancelled_delete_clears_memory_deletion_barrier(tmp_path, monkeypatch):
    role, _, _ = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    worker = MemoryWorker(MemoryService(MemoryStore(tmp_path / "data" / "memory.db")))
    monkeypatch.setattr(main, "memory_worker", worker)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_begin(role_id):
        worker._deleting.add(role_id)
        entered.set()
        await release.wait()

    monkeypatch.setattr(worker, "begin_role_deletion", blocking_begin)

    async def run():
        deletion = asyncio.create_task(main.delete_role(role.id))
        await entered.wait()
        deletion.cancel()
        with pytest.raises(asyncio.CancelledError):
            await deletion

    asyncio.run(run())
    assert worker.role_deletion_blocked(role.id) is False


def test_delete_role_attempts_role_and_file_restore_when_database_rollback_fails(tmp_path, monkeypatch):
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    memory_store = MemoryStore(tmp_path / "data" / "memory.db")
    memory_service = MemoryService(memory_store)
    worker = MemoryWorker(memory_service)
    monkeypatch.setattr(main, "memory_store", memory_store)
    monkeypatch.setattr(main, "memory_service", memory_service)
    monkeypatch.setattr(main, "memory_worker", worker)
    memory_path = tmp_path / "roles" / role.id / "memory" / "MEMORY.md"
    memory_path.write_text("保留的文档", encoding="utf-8")

    def fail_delete(role_id):
        raise OSError("记忆数据库删除失败")

    def fail_session_restore(role_id, snapshot):
        raise OSError("聊天记录恢复失败")

    monkeypatch.setattr(memory_store, "delete_role", fail_delete)
    monkeypatch.setattr(sessions, "restore_role_session", fail_session_restore)
    response = client.delete(f"/api/roles/{role.id}")

    assert response.status_code == 500
    assert response.json()["detail"] == "删除失败，角色恢复也未能完成"
    assert client.get(f"/api/roles/{role.id}").status_code == 200
    assert memory_path.read_text(encoding="utf-8") == "保留的文档"


def test_delete_role_preserves_files_when_staging_fails(tmp_path, monkeypatch):
    role, _, client = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    memory_path = tmp_path / "roles" / role.id / "memory" / "MEMORY.md"
    memory_path.write_text("尚未暂存的文档", encoding="utf-8")

    def fail_stage(role_id):
        raise OSError("目录暂存失败")

    monkeypatch.setattr(main.store, "stage_role_files_for_deletion", fail_stage)
    response = client.delete(f"/api/roles/{role.id}")

    assert response.status_code == 500
    assert client.get(f"/api/roles/{role.id}").status_code == 200
    assert memory_path.read_text(encoding="utf-8") == "尚未暂存的文档"


def test_delete_role_failure_restores_role_and_chat_history(tmp_path, monkeypatch):
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    client.post(f"/api/roles/{role.id}/messages", json={"content": "保留消息"})
    before = sessions.list_messages(f"role:{role.id}")

    def fail_delete(role_id):
        raise OSError("数据库删除失败")

    monkeypatch.setattr(sessions, "delete_role_session", fail_delete)
    response = client.delete(f"/api/roles/{role.id}")

    assert response.status_code == 500
    assert response.json()["detail"] == "删除失败，角色和聊天记录已保留"
    assert client.get(f"/api/roles/{role.id}").status_code == 200
    assert [item.model_dump() for item in sessions.list_messages(f"role:{role.id}")] == [item.model_dump() for item in before]


def test_delete_role_memory_failure_restores_role_chat_and_memory(tmp_path, monkeypatch):
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    memory_store = MemoryStore(tmp_path / "data" / "memory.db")
    memory_service = MemoryService(memory_store)
    monkeypatch.setattr(main, "memory_store", memory_store)
    monkeypatch.setattr(main, "memory_service", memory_service)
    monkeypatch.setattr(main, "memory_worker", MemoryWorker(memory_service))
    client.post(f"/api/roles/{role.id}/messages", json={"content": "保留消息"})
    memory_service.remember(role.id, "保留记忆", "fact", stable_source_key="keep")
    before_messages = sessions.list_messages(f"role:{role.id}")

    def fail_delete(role_id):
        raise OSError("记忆数据库删除失败")

    monkeypatch.setattr(memory_store, "delete_role", fail_delete)
    response = client.delete(f"/api/roles/{role.id}")

    assert response.status_code == 500
    assert client.get(f"/api/roles/{role.id}").status_code == 200
    assert [item.model_dump() for item in sessions.list_messages(f"role:{role.id}")] == [item.model_dump() for item in before_messages]
    assert [item.summary for item in memory_store.list_active(role.id)] == ["保留记忆"]


def test_delete_role_downstream_key_error_is_reported_as_delete_failure(tmp_path, monkeypatch):
    role, _, client = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    memory_store = MemoryStore(tmp_path / "data" / "memory.db")
    memory_service = MemoryService(memory_store)
    monkeypatch.setattr(main, "memory_store", memory_store)
    monkeypatch.setattr(main, "memory_service", memory_service)
    monkeypatch.setattr(main, "memory_worker", MemoryWorker(memory_service))

    def fail_delete(role_id):
        raise KeyError("memory row")

    monkeypatch.setattr(memory_store, "delete_role", fail_delete)
    response = client.delete(f"/api/roles/{role.id}")

    assert response.status_code == 500
    assert response.json()["detail"] == "删除失败，角色和记忆已保留"
    assert client.get(f"/api/roles/{role.id}").status_code == 200


def test_delete_role_file_failure_restores_role_session_memory_and_documents(tmp_path, monkeypatch):
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    memory_store = MemoryStore(tmp_path / "data" / "memory.db")
    memory_service = MemoryService(memory_store)
    monkeypatch.setattr(main, "memory_store", memory_store)
    monkeypatch.setattr(main, "memory_service", memory_service)
    monkeypatch.setattr(main, "memory_worker", MemoryWorker(memory_service))
    client.post(f"/api/roles/{role.id}/messages", json={"content": "保留消息"})
    memory_service.remember(role.id, "保留记忆", "fact", stable_source_key="keep")
    memory_path = tmp_path / "roles" / role.id / "memory" / "MEMORY.md"
    memory_path.write_text("长期记忆文件", encoding="utf-8")
    original_purge = main.store.purge_staged_role_files

    def fail_purge(path):
        raise OSError("文件清理失败")

    monkeypatch.setattr(main.store, "purge_staged_role_files", fail_purge)
    response = client.delete(f"/api/roles/{role.id}")

    assert response.status_code == 500
    assert client.get(f"/api/roles/{role.id}").status_code == 200
    assert sessions.list_messages(f"role:{role.id}")
    assert memory_store.list_active(role.id)[0].summary == "保留记忆"
    assert memory_path.read_text(encoding="utf-8") == "长期记忆文件"
    monkeypatch.setattr(main.store, "purge_staged_role_files", original_purge)


def test_delete_role_rejects_while_generation_is_in_progress(tmp_path, monkeypatch):
    role, _, client = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    lock = main._runtime_role_lock(role.id)
    assert not lock.locked()
    asyncio.run(lock.acquire())
    response = client.delete(f"/api/roles/{role.id}")
    assert response.status_code == 409
    assert client.get(f"/api/roles/{role.id}").status_code == 200
    lock.release()


def test_delete_role_rejects_persisted_active_run_without_process_lock(tmp_path, monkeypatch):
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    session = sessions.open_role_session(role.id)
    sessions.create_run(AgentRun(
        runId="run-persisted-active",
        roleId=role.id,
        sessionKey=session.sessionKey,
        status="created",
        startedAt=datetime.now(timezone.utc),
    ))

    response = client.delete(f"/api/roles/{role.id}")

    assert response.status_code == 409
    assert client.get(f"/api/roles/{role.id}").status_code == 200


def test_unstarted_message_stream_is_failed_and_role_can_retry(tmp_path, monkeypatch):
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"/api/roles/{role.id}/messages",
        "raw_path": f"/api/roles/{role.id}/messages".encode(),
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("testclient", 12345),
        "root_path": "",
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def prepare_response():
        request = Request(scope, receive)
        return await main.send_role_message(role.id, SendMessageInput(content="第一次"), request)

    response = asyncio.run(prepare_response())
    assert main.runtime_manager.lock_for(role.id).locked()
    run_id = sessions.list_messages(f"role:{role.id}")[0].runId
    assert run_id is not None
    assert sessions.get_run(run_id).status == "created"
    assert response.background is not None

    async def abandon_before_first_chunk():
        await response.body_iterator.aclose()
        await response.background()

    asyncio.run(abandon_before_first_chunk())
    assert not main.runtime_manager.lock_for(role.id).locked()
    failed_run = sessions.get_run(sessions.list_messages(f"role:{role.id}")[0].runId)
    assert failed_run is not None and failed_run.status == "failed"
    stored = sessions.list_messages(f"role:{role.id}")
    assert [(item.role, item.status) for item in stored] == [("user", "completed"), ("assistant", "failed")]

    retry = client.post(f"/api/roles/{role.id}/messages", json={"content": "重试"})
    assert retry.status_code == 200
    assert "event: assistant_completed" in retry.text


def test_capabilities_are_closed_when_provider_setup_fails(tmp_path, monkeypatch):
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())

    class FakeCapabilities:
        class Snapshot:
            def to_dict(self, **kwargs):
                assert kwargs == {"include_prompt_sections": False}
                return {"tools": []}

        snapshot = Snapshot()

        def prompt_context(self):
            return ""

        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    capabilities = FakeCapabilities()

    async def resolve_capabilities(*args, **kwargs):
        del args, kwargs
        return capabilities

    provider_called = False

    def fail_provider(*args, **kwargs):
        nonlocal provider_called
        provider_called = True
        del args, kwargs
        raise RuntimeError("provider setup failed")

    monkeypatch.setattr(main, "_runtime_capabilities", resolve_capabilities)
    monkeypatch.setattr(main, "MeidoProvider", fail_provider)

    response = client.post(f"/api/roles/{role.id}/messages", json={"content": "触发失败"})

    assert response.status_code == 200
    assert "event: assistant_failed" in response.text
    assert provider_called
    assert capabilities.closed
    run_id = sessions.list_messages(f"role:{role.id}")[0].runId
    assert run_id is not None
    run = sessions.get_run(run_id)
    assert run is not None and run.status == "failed"


def test_role_snapshot_is_stable_after_update_and_new_lookup_uses_latest(tmp_path, monkeypatch):
    role, _, _ = configure_session_api(tmp_path, monkeypatch, RecordingAdapter())
    snapshot, _ = main.session_manager.role_and_history(role.id)
    response = TestClient(main.app).put(
        f"/api/roles/{role.id}",
        json={"name": role.name, "profile": {"profile": "新设定"}},
    )

    assert response.status_code == 200
    assert snapshot.profile.profile == "核心设定"
    current, _ = main.session_manager.role_and_history(role.id)
    assert current.profile.profile == "新设定"


def test_role_api_rejects_blank_required_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "store", RoleStore(tmp_path / "roles"))
    client = TestClient(main.app)
    assert client.post("/api/roles", json={"name": " ", "profile": {"profile": "设定"}}).status_code == 422
    assert client.post("/api/roles", json={"name": "角色", "profile": {"profile": " "}}).status_code == 422


def test_role_create_failure_returns_clear_error_and_does_not_leave_phantom_role(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    monkeypatch.setattr(main, "store", roles)

    def fail_save(candidate=None):
        raise OSError("磁盘写入失败")

    monkeypatch.setattr(roles, "_save", fail_save)
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.post("/api/roles", json={"name": "未保存", "profile": {"profile": "核心设定"}})

    assert response.status_code == 500
    assert response.json()["detail"] == "角色保存失败：磁盘写入失败"
    assert roles.list() == []


def test_role_has_one_persistent_session_and_streams_completed_reply(tmp_path, monkeypatch):
    adapter = SuccessAdapter()
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, adapter)
    first = client.get(f"/api/roles/{role.id}/session").json()
    second = client.get(f"/api/roles/{role.id}/session").json()
    assert first["session"]["sessionKey"] == f"role:{role.id}"
    assert first["session"]["sessionKey"] == second["session"]["sessionKey"]

    response = client.post(f"/api/roles/{role.id}/messages", json={"content": "你好"})
    assert response.status_code == 200
    assert "event: assistant_delta" in response.text
    assert "event: assistant_completed" in response.text
    stored = sessions.list_messages(f"role:{role.id}")
    assert [(item.role, item.content, item.status) for item in stored] == [
        ("user", "你好", "completed"),
        ("assistant", "你好，主人。", "completed"),
    ]
    reopened = client.get(f"/api/roles/{role.id}/session").json()
    assert [message["sequence"] for message in reopened["messages"]] == [1, 2]


def test_stream_failure_keeps_partial_reply_failed_and_allows_retry(tmp_path, monkeypatch):
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, FailingAdapter())
    failed = client.post(f"/api/roles/{role.id}/messages", json={"content": "测试中断"})
    assert "event: assistant_failed" in failed.text
    messages = sessions.list_messages(f"role:{role.id}")
    assert messages[-1].content == "部分内容"
    assert messages[-1].status == "failed"

    retry_adapter = SuccessAdapter()
    monkeypatch.setattr(main, "model_adapter", retry_adapter)
    retry = client.post(f"/api/roles/{role.id}/messages", json={"content": "你好"})
    assert "event: assistant_completed" in retry.text
    assert sessions.list_messages(f"role:{role.id}")[-1].status == "completed"
    assert all(message.content != "部分内容" for message in retry_adapter.histories[0])


def test_sessions_database_migrates_legacy_role_messages(tmp_path):
    import sqlite3

    data = tmp_path / "old-data"
    data.mkdir()
    with sqlite3.connect(data / "sessions.db") as connection:
        connection.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, role_id TEXT, author TEXT, content TEXT, status TEXT, created_at TEXT)")
        connection.execute("INSERT INTO messages VALUES ('m1', 'role-1', 'user', '旧消息', 'completed', '2025-01-01T00:00:00+00:00')")
    initialize_databases(data)
    migrated = SessionStore(data / "sessions.db").list_messages("role:role-1")
    assert len(migrated) == 1
    assert migrated[0].content == "旧消息"
    assert migrated[0].role == "user"


def test_interrupted_streaming_message_recovers_as_failed(tmp_path):
    data = tmp_path / "recovery"
    initialize_databases(data)
    first_store = SessionStore(data / "sessions.db")
    session = first_store.open_role_session("role-recover")
    first_store.append_message(session.sessionKey, "assistant", "中断内容", "streaming")
    recovered_store = SessionStore(data / "sessions.db")
    recovered = recovered_store.list_messages(session.sessionKey)
    assert recovered[0].status == "failed"
