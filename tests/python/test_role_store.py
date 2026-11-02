import json
import pytest
from pydantic import ValidationError

from backend.app.role_store import RoleStore
from backend.app.models import AgentToolConfig, ModelConfigurationInput, RoleInput, RoleProfile, RoleUpdateInput, ShellToolConfig
from backend.app.model_config import ModelConfigurationStore


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
        modelConfigurationId="model-local",
        proactiveConfig={"enabled": True},
    ))

    updated = store.update(role.id, RoleUpdateInput(
        name="新名字",
        description="新简介",
        profile=RoleProfile(profile="新设定"),
    ))

    assert updated.modelConfigurationId == "model-local"
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


def test_natural_chat_is_role_owned_defaults_off_and_survives_other_settings(tmp_path):
    store = RoleStore(tmp_path / "roles")
    role = store.create(role_input())
    assert role.agentConfig.naturalChatEnabled is False
    store.update(role.id, RoleUpdateInput(name=role.name, profile=role.profile,
        agentConfig={"naturalChatEnabled": True}))
    updated = store.update(role.id, RoleUpdateInput(name="新名字", profile=role.profile,
        agentConfig={"enabledTools": ["web_search"]}))
    assert updated.agentConfig.naturalChatEnabled is True
    reloaded = RoleStore(store.root).get(role.id)
    assert reloaded.agentConfig.naturalChatEnabled is True
    assert reloaded.agentConfig.enabledTools == ["web_search"]
    store.update(role.id, RoleUpdateInput(name=updated.name, profile=updated.profile,
        agentConfig={"naturalChatEnabled": False}))
    assert RoleStore(store.root).get(role.id).agentConfig.naturalChatEnabled is False


@pytest.mark.parametrize("value", [1, "true", None])
def test_natural_chat_switch_requires_an_explicit_boolean(value):
    with pytest.raises(ValidationError):
        AgentToolConfig(naturalChatEnabled=value)


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


def test_role_creation_preserves_canonical_binding_and_discards_inline_credentials(tmp_path):
    store = RoleStore(tmp_path / "roles")
    role = store.create(RoleInput(
        name="角色", profile=RoleProfile(profile="设定"),
        modelConfigurationId="  model-chosen  ",
        modelConfig={"apiKey": "synthetic-inline-secret", "headers": {"Authorization": "synthetic-bearer"}},
        modelBindingRequired=True,
    ))

    assert role.modelConfigurationId == "model-chosen"
    assert role.modelBindingRequired is False
    assert "modelConfig" not in role.model_dump()
    assert "synthetic-" not in store.manifest_path.read_text(encoding="utf-8")
    assert RoleStore(store.root).get(role.id).modelConfigurationId == "model-chosen"


@pytest.mark.parametrize("value", ["", "  ", {"apiKey": "synthetic-secret"}])
def test_role_creation_rejects_invalid_canonical_binding(value):
    with pytest.raises(ValidationError) as caught:
        RoleInput(name="角色", profile=RoleProfile(profile="设定"), modelConfigurationId=value)
    assert "synthetic-secret" not in str(caught.value)


def _configuration_catalog(tmp_path, **overrides):
    catalog = ModelConfigurationStore(tmp_path / "model-config.json")
    values = {
        "providerId": "custom", "provider": "test-provider",
        "baseUrl": "https://model.invalid/v1", "model": "test-model",
        "api": "openai-completions", "apiKey": "synthetic-catalog-secret",
        **overrides,
    }
    configuration = catalog.create(ModelConfigurationInput(**values))
    return catalog, configuration


def _legacy_manifest(tmp_path, legacy, **overrides):
    root = tmp_path / "roles"
    root.mkdir()
    values = {
        "id": "role-legacy", "name": "旧角色", "description": "旧简介",
        "profile": {"profile": "原设定", "personality": "原性格", "behaviorRules": "原规则",
                    "responseConstraints": "原约束", "nickname": "原称呼"},
        "createdAt": "2026-01-02T03:04:05Z", "updatedAt": "2026-02-03T04:05:06Z",
        "modelConfig": legacy,
        **overrides,
    }
    (root / "roles.json").write_text(json.dumps({"version": 1, "roles": [values]}, ensure_ascii=False), encoding="utf-8")
    return root, values


def _inline_configuration(configuration, **overrides):
    return {
        key: getattr(configuration, key)
        for key in ("providerId", "provider", "baseUrl", "model", "api", "apiOptions")
    } | {"apiKey": "synthetic-legacy-secret", **overrides}


def test_legacy_role_migrates_unique_public_connection_and_preserves_profile(tmp_path):
    catalog, configuration = _configuration_catalog(tmp_path)
    legacy = _inline_configuration(configuration, provider=" test-provider ", baseUrl="https://model.invalid/v1/")
    root, original = _legacy_manifest(tmp_path, legacy)
    original_bytes = (root / "roles.json").read_bytes()

    store = RoleStore(root, model_configurations=catalog.list)
    role = store.get("role-legacy")

    assert role.modelConfigurationId == configuration.id
    assert role.modelBindingRequired is False
    assert role.profile.model_dump() == original["profile"]
    assert role.createdAt.isoformat() == "2026-01-02T03:04:05+00:00"
    assert role.updatedAt.isoformat() == "2026-02-03T04:05:06+00:00"
    assert "modelConfig" not in role.model_dump()
    assert (root / "roles.json").read_bytes() == original_bytes


@pytest.mark.parametrize("identifier_field", ["id", "configurationId", "modelConfigurationId"])
def test_legacy_role_prefers_existing_explicit_connection_id(tmp_path, identifier_field):
    catalog, configuration = _configuration_catalog(tmp_path)
    root, _ = _legacy_manifest(tmp_path, {identifier_field: configuration.id, "apiKey": "synthetic-secret"})

    role = RoleStore(root, model_configurations=catalog.list).get("role-legacy")

    assert role.modelConfigurationId == configuration.id
    assert role.modelBindingRequired is False


def test_existing_canonical_binding_never_falls_back_to_matching_legacy_configuration(tmp_path):
    catalog, configuration = _configuration_catalog(tmp_path)
    root, _ = _legacy_manifest(tmp_path, _inline_configuration(configuration), modelConfigurationId="missing-canonical")

    role = RoleStore(root, model_configurations=catalog.list).get("role-legacy")

    assert role.modelConfigurationId == "missing-canonical"
    assert catalog.get_for_role(role) is None


@pytest.mark.parametrize("legacy_change", [
    {"model": "different"},
    {"baseUrl": "https://user:synthetic-url-secret@model.invalid/v1"},
    {"baseUrl": "https://model.invalid/v1?api_key=synthetic-url-secret"},
    {"apiOptions": {"apiKey": "synthetic-option-secret"}},
    {"apiOptions": "synthetic-malformed-secret"},
    {"providerId": "missing-provider"},
])
def test_unmatched_or_unsafe_legacy_role_requires_reselection_without_global_fallback(tmp_path, legacy_change):
    catalog, configuration = _configuration_catalog(tmp_path)
    root, _ = _legacy_manifest(tmp_path, _inline_configuration(configuration, **legacy_change))

    role = RoleStore(root, model_configurations=catalog.list).get("role-legacy")

    assert role.modelConfigurationId is None
    assert role.modelBindingRequired is True
    assert catalog.get_for_role(role) is None
    assert "synthetic-" not in role.model_dump_json()


def test_ambiguous_legacy_role_is_not_resolved_by_matching_api_key(tmp_path):
    catalog, configuration = _configuration_catalog(tmp_path)
    catalog.create(ModelConfigurationInput(**_inline_configuration(configuration, apiKey="synthetic-other-secret")))
    root, _ = _legacy_manifest(tmp_path, _inline_configuration(configuration, apiKey=configuration.apiKey))

    role = RoleStore(root, model_configurations=catalog.list).get("role-legacy")

    assert role.modelConfigurationId is None
    assert role.modelBindingRequired is True


@pytest.mark.parametrize("options, expected_binding", [
    ({"project": "test-project", "location": "test-region"}, True),
    ({"project": "different-project", "location": "test-region"}, False),
])
def test_legacy_matching_compares_protocol_options_exactly(tmp_path, options, expected_binding):
    catalog, configuration = _configuration_catalog(tmp_path, api="google-vertex",
        apiOptions={"project": "test-project", "location": "test-region"})
    root, _ = _legacy_manifest(tmp_path, _inline_configuration(configuration, apiOptions=options))

    role = RoleStore(root, model_configurations=catalog.list).get("role-legacy")

    assert role.modelConfigurationId == (configuration.id if expected_binding else None)
    assert role.modelBindingRequired is not expected_binding


@pytest.mark.parametrize("legacy", ["synthetic-malformed-secret", ["synthetic-malformed-secret"]])
def test_malformed_legacy_value_keeps_role_without_exposing_the_value(tmp_path, legacy):
    root, _ = _legacy_manifest(tmp_path, legacy)

    role = RoleStore(root).get("role-legacy")

    assert role.modelBindingRequired is True
    assert "synthetic-" not in role.model_dump_json()


@pytest.mark.parametrize("invalid_id", ["", "  ", {"apiKey": "synthetic-invalid-id-secret"}])
def test_invalid_stored_canonical_id_cannot_trigger_legacy_or_global_fallback(tmp_path, invalid_id):
    catalog, configuration = _configuration_catalog(tmp_path)
    root, _ = _legacy_manifest(tmp_path, _inline_configuration(configuration),
        modelConfigurationId=invalid_id)

    role = RoleStore(root, model_configurations=catalog.list).get("role-legacy")

    assert role.modelBindingRequired is True
    assert catalog.get_for_role(role) is None
    assert "synthetic-" not in role.model_dump_json()


def test_reselecting_model_clears_legacy_error_and_preserves_profile(tmp_path):
    catalog, configuration = _configuration_catalog(tmp_path)
    root, _ = _legacy_manifest(tmp_path, {"apiKey": "synthetic-secret"})
    store = RoleStore(root, model_configurations=catalog.list)
    before = store.get("role-legacy")
    assert before.modelBindingRequired is True

    edited = store.update(before.id, RoleUpdateInput(name="改名", profile=before.profile))
    assert edited.modelBindingRequired is True
    bound = store.set_model_configuration(before.id, configuration.id)

    assert bound.modelBindingRequired is False
    assert bound.profile == before.profile
    assert bound.name == "改名"
    assert RoleStore(root).get(before.id).modelConfigurationId == configuration.id


def test_unbound_role_inherits_active_configuration_and_returns_an_isolated_snapshot(tmp_path):
    catalog, configuration = _configuration_catalog(tmp_path)
    role = RoleStore(tmp_path / "roles").create(role_input())
    snapshot = catalog.get_for_role(role)

    assert snapshot.id == configuration.id
    catalog.update(configuration.id, ModelConfigurationInput(**_inline_configuration(configuration, model="updated")))
    assert snapshot.model == "test-model"
    assert catalog.get_for_role(role).model == "updated"
    assert catalog.get_for_role(None) is None


@pytest.mark.parametrize("legacy", [None, {}, [], "", {"apiKey": "synthetic-legacy-secret", "nested": {"token": "synthetic-token"}}])
def test_ordinary_saves_preserve_original_legacy_field_privately(tmp_path, legacy):
    root, _ = _legacy_manifest(tmp_path, legacy)
    original_bytes = (root / "roles.json").read_bytes()
    store = RoleStore(root)
    role = store.get("role-legacy")
    assert (root / "roles.json").read_bytes() == original_bytes
    assert "modelConfig" not in role.model_dump()

    store.update(role.id, RoleUpdateInput(name="新名字", profile=role.profile))
    record = json.loads((root / "roles.json").read_text(encoding="utf-8"))["roles"][0]
    assert "modelConfig" in record
    assert record["modelConfig"] == legacy
    store.set_model_configuration(role.id, "new-model")
    assert json.loads((root / "roles.json").read_text(encoding="utf-8"))["roles"][0]["modelConfig"] == legacy
    assert "modelConfig" not in RoleStore(root).get(role.id).model_dump()


def test_explicit_global_reselection_survives_reload_with_legacy_source_unchanged(tmp_path):
    catalog, configuration = _configuration_catalog(tmp_path)
    legacy = {"apiKey": "synthetic-legacy-secret"}
    root, _ = _legacy_manifest(tmp_path, legacy)
    store = RoleStore(root, model_configurations=catalog.list)
    assert store.get("role-legacy").modelBindingRequired is True

    store.set_model_configuration("role-legacy", None)
    role = RoleStore(root, model_configurations=catalog.list).get("role-legacy")

    assert role.modelConfigurationId is None
    assert role.modelBindingRequired is False
    assert catalog.get_for_role(role).id == configuration.id
    assert json.loads((root / "roles.json").read_text(encoding="utf-8"))["roles"][0]["modelConfig"] == legacy


def test_failed_legacy_profile_save_preserves_original_manifest_bytes(tmp_path, monkeypatch):
    root, _ = _legacy_manifest(tmp_path, {"apiKey": "synthetic-legacy-secret"})
    store = RoleStore(root)
    before = (root / "roles.json").read_bytes()
    role = store.get("role-legacy")

    def fail_replace(*_args):
        raise OSError("synthetic write failure")

    monkeypatch.setattr("backend.app.role_store.os.replace", fail_replace)
    with pytest.raises(OSError, match="synthetic write failure"):
        store.update(role.id, RoleUpdateInput(name="新名字", profile=role.profile))

    assert (root / "roles.json").read_bytes() == before
    assert store.get(role.id) == role
    assert list(root.glob(".roles.json.*.tmp")) == []


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
