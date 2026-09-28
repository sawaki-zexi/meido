import json

from backend.app.role_store import RoleStore
from backend.app.models import RoleInput, RoleProfile


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
    updated = RoleStore(root).update(first.id, role_input("改名"))
    assert updated.id == first.id
    assert RoleStore(root).get(first.id).name == "改名"


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
