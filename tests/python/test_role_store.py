import json

from backend.app.role_store import RoleStore
from backend.app.models import AgentToolConfig, RoleInput, RoleProfile, RoleUpdateInput, ShellToolConfig


def role_input(name: str = "小葵") -> RoleInput:
    return RoleInput(name=name, profile=RoleProfile(profile="负责照料日常", personality="温柔"))


def test_role_store_persists_json_and_role_directories(tmp_path):
    store = RoleStore(tmp_path / "roles")
    role = store.create(role_input())
    assert json.loads((tmp_path / "roles" / "roles.json").read_text(encoding="utf-8"))["roles"][0]["id"] == role.id
    assert (tmp_path / "roles" / role.id / "memory").is_dir()
    assert (tmp_path / "roles" / role.id / "state").is_dir()


def test_role_names_can_repeat_and_id_survives_update_and_reload(tmp_path):
    root = tmp_path / "roles"
    first = RoleStore(root).create(role_input())
    second = RoleStore(root).create(role_input())
    assert first.id != second.id
    updated = RoleStore(root).update(
        first.id,
        RoleUpdateInput(name="改名", profile=RoleProfile(profile="更新设定")),
    )
    assert updated.id == first.id
    assert RoleStore(root).get(first.id).name == "改名"


def test_role_update_preserves_non_editable_configuration(tmp_path):
    store = RoleStore(tmp_path / "roles")
    role = store.create(RoleInput(
        name="小葵",
        profile=RoleProfile(profile="原设定"),
        modelConfig={"provider": "local"},
        proactiveConfig={"enabled": True},
    ))

    updated = store.update(role.id, RoleUpdateInput(
        name="新名字",
        description="新简介",
        profile=RoleProfile(profile="新设定"),
    ))

    assert updated.modelConfig == {"provider": "local"}
    assert updated.proactiveConfig == {"enabled": True}
    assert updated.profile.profile == "新设定"


def test_role_update_preserves_agent_tool_policy_when_profile_edit_omits_it(tmp_path):
    store = RoleStore(tmp_path / "roles")
    role = store.create(RoleInput(
        name="可执行角色",
        profile=RoleProfile(profile="设定"),
        agentConfig=AgentToolConfig(shell=ShellToolConfig(enabled=True, allowedCommands=["python"])),
    ))

    updated = store.update(role.id, RoleUpdateInput(name="改名", profile=RoleProfile(profile="新设定")))

    assert updated.agentConfig.shell.enabled is True
    assert updated.agentConfig.shell.allowedCommands == ["python"]
    assert (tmp_path / "roles" / role.id / "workspace").is_dir()


def test_role_store_persists_capability_enablement_and_grants(tmp_path):
    store = RoleStore(tmp_path / "roles")
    role = store.create(RoleInput(
        name="扩展角色",
        profile=RoleProfile(profile="设定"),
        agentConfig=AgentToolConfig(
            enabledTools=["plugin.echo", "plugin.echo"],
            enabledPlugins=["demo", "demo"],
            grantedCapabilities=["runtime.tool", "runtime.tool"],
        ),
    ))

    reloaded = RoleStore(tmp_path / "roles").get(role.id)

    assert reloaded.agentConfig.enabledTools == ["plugin.echo"]
    assert reloaded.agentConfig.enabledPlugins == ["demo"]
    assert reloaded.agentConfig.grantedCapabilities == ["runtime.tool"]


def test_role_model_configuration_binding_persists_and_is_independent(tmp_path):
    root = tmp_path / "roles"
    store = RoleStore(root)
    first = store.create(role_input("第一个"))
    second = store.create(role_input("第二个"))

    bound = store.set_model_configuration(first.id, "model-first")

    assert bound.modelConfigurationId == "model-first"
    assert store.get(second.id).modelConfigurationId is None
    reloaded = RoleStore(root)
    assert reloaded.get(first.id).modelConfigurationId == "model-first"
    assert reloaded.get(second.id).modelConfigurationId is None


def test_failed_model_configuration_binding_keeps_previous_value(tmp_path, monkeypatch):
    store = RoleStore(tmp_path / "roles")
    role = store.create(role_input())
    store.set_model_configuration(role.id, "model-old")

    def fail_save(roles=None):
        raise OSError("磁盘写入失败")

    monkeypatch.setattr(store, "_save", fail_save)
    try:
        store.set_model_configuration(role.id, "model-new")
    except OSError:
        pass
    else:
        raise AssertionError("failed persistence should raise")

    assert store.get(role.id).modelConfigurationId == "model-old"


def test_failed_role_update_keeps_memory_and_manifest_unchanged(tmp_path, monkeypatch):
    root = tmp_path / "roles"
    store = RoleStore(root)
    role = store.create(role_input())
    manifest_before = (root / "roles.json").read_bytes()

    def fail_save(roles=None):
        raise OSError("磁盘写入失败")

    monkeypatch.setattr(store, "_save", fail_save)
    try:
        store.update(role.id, RoleUpdateInput(name="未保存", profile=RoleProfile(profile="未保存设定")))
    except OSError:
        pass
    else:
        raise AssertionError("failed persistence should raise")

    assert store.get(role.id).name == role.name
    assert (root / "roles.json").read_bytes() == manifest_before


def test_failed_role_create_keeps_memory_and_manifest_unchanged(tmp_path, monkeypatch):
    root = tmp_path / "roles"
    store = RoleStore(root)
    manifest_before = (root / "roles.json").read_bytes() if (root / "roles.json").exists() else None

    def fail_save(roles=None):
        raise OSError("磁盘写入失败")

    monkeypatch.setattr(store, "_save", fail_save)
    try:
        store.create(role_input("未保存"))
    except OSError:
        pass
    else:
        raise AssertionError("failed persistence should raise")

    assert store.list() == []
    assert [path for path in root.iterdir() if path.is_dir()] == []
    if manifest_before is None:
        assert not (root / "roles.json").exists()
    else:
        assert (root / "roles.json").read_bytes() == manifest_before


def test_role_delete_persists_remaining_roles_and_can_restore(tmp_path):
    root = tmp_path / "roles"
    store = RoleStore(root)
    first = store.create(role_input("第一个"))
    second = store.create(role_input("第二个"))

    deleted = store.delete(first.id)
    assert deleted.id == first.id
    assert store.get(first.id) is None
    assert store.get(second.id) is not None
    assert RoleStore(root).get(first.id) is None

    store.restore_deleted(deleted)
    assert store.get(first.id).name == "第一个"


def test_role_validation_rejects_blank_name_and_profile(tmp_path):
    store = RoleStore(tmp_path / "roles")
    try:
        store.create(role_input("   "))
    except ValueError as error:
        assert "名称" in str(error)
    else:
        raise AssertionError("blank name should fail")
    try:
        store.create(RoleInput(name="有效", profile=RoleProfile(profile="  ")))
    except ValueError as error:
        assert "设定" in str(error)
    else:
        raise AssertionError("blank profile should fail")
