import json

from backend.app.role_store import RoleStore
from backend.app.models import RoleInput, RoleProfile, RoleUpdateInput


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
