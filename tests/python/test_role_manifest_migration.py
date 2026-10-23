import json

import pytest

from backend.app import role_store as role_store_module
from backend.app.models import RoleInput, RoleProfile, RoleUpdateInput
from backend.app.role_store import RoleStore


def test_upgrade_preserves_existing_roles_and_persists_version_once(tmp_path):
    root = tmp_path / "roles"
    root.mkdir()
    manifest = root / "roles.json"
    original = {
        "version": 1,
        "extensionState": {"plugin": "opaque"},
        "roles": [{
            "id": "role-existing", "name": "小葵", "description": "简介",
            "profile": {"profile": "照料日常", "personality": "温柔",
                        "behaviorRules": "认真", "responseConstraints": "简洁",
                        "nickname": "主人", "extension": {"opaque": True}},
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-02-02T00:00:00Z",
            "modelConfigurationId": "model-existing",
            "avatarUrl": "/api/roles/role-existing/avatar?v=1",
            "cardImageUrl": "/api/roles/role-existing/card-image?v=2",
            "agentConfig": {"browserUse": {"enabled": True}, "extensionPolicy": {"x": 1}},
            "extensionState": {"private": "preserve"},
        }, {
            "id": "role-existing-2", "name": "小葵", "description": "同名角色",
            "profile": {"profile": "另一份角色设定"},
            "createdAt": "2026-03-03T00:00:00Z", "updatedAt": "2026-03-04T00:00:00Z",
        }],
    }
    manifest.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

    store = RoleStore(root)
    role = store.get("role-existing")
    upgraded = json.loads(manifest.read_text(encoding="utf-8"))
    assert upgraded["version"] == 2
    assert upgraded["roles"][0]["profile"] == {"version": 1, "character": original["roles"][0]["profile"]}
    assert upgraded["roles"][0]["createdAt"] == original["roles"][0]["createdAt"]
    assert role.id == "role-existing"
    assert role.profile.character.personality == "温柔"
    assert role.modelConfigurationId == "model-existing"
    assert role.avatarUrl.endswith("?v=1")
    assert role.agentConfig.browserUse.enabled is True
    assert [(item.id, item.name) for item in store.list()] == [
        ("role-existing", "小葵"), ("role-existing-2", "小葵"),
    ]

    upgraded_bytes = manifest.read_bytes()
    modified_at = manifest.stat().st_mtime_ns
    assert RoleStore(root).get(role.id).profile.character.profile == "照料日常"
    assert manifest.read_bytes() == upgraded_bytes
    assert manifest.stat().st_mtime_ns == modified_at

    store.update(role.id, RoleUpdateInput(name="改名", profile=RoleProfile(profile="新设定")))
    saved = json.loads(manifest.read_text(encoding="utf-8"))
    assert saved["extensionState"] == {"plugin": "opaque"}
    assert saved["roles"][0]["extensionState"] == {"private": "preserve"}
    assert saved["roles"][0]["profile"]["character"]["extension"] == {"opaque": True}
    assert saved["roles"][0]["agentConfig"]["extensionPolicy"] == {"x": 1}


def test_unsupported_manifest_version_is_rejected_without_rewriting_source(tmp_path):
    root = tmp_path / "roles"
    root.mkdir()
    manifest = root / "roles.json"
    manifest.write_text('{"version":99,"roles":[]}', encoding="utf-8")
    original = manifest.read_bytes()

    with pytest.raises(ValueError, match="版本不支持"):
        RoleStore(root)

    assert manifest.read_bytes() == original


def test_legacy_role_missing_identity_metadata_gets_one_persisted_identity(tmp_path):
    manifest = tmp_path / "roles.json"
    manifest.write_text(json.dumps({"version": 1, "roles": [{
        "name": "旧角色", "profile": {"profile": "背景"},
    }]}), encoding="utf-8")

    first = RoleStore(tmp_path).list()[0]
    persisted = json.loads(manifest.read_text(encoding="utf-8"))["roles"][0]
    second = RoleStore(tmp_path).list()[0]

    assert first.id.startswith("role-")
    assert second.id == first.id == persisted["id"]
    assert first.createdAt.isoformat() == persisted["createdAt"].replace("Z", "+00:00")
    assert first.updatedAt.isoformat() == persisted["updatedAt"].replace("Z", "+00:00")


def test_delete_rollback_restores_opaque_role_extensions(tmp_path):
    store = RoleStore(tmp_path)
    role = store.create(RoleInput(name="角色", profile={"profile": "背景"}))
    manifest = tmp_path / "roles.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["roles"][0]["extensionState"] = {"plugin": "preserve-on-rollback"}
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    store = RoleStore(tmp_path)

    deleted = store.delete(role.id)
    store.restore_deleted(deleted)

    saved = json.loads(manifest.read_text(encoding="utf-8"))
    assert saved["roles"][0]["extensionState"] == {"plugin": "preserve-on-rollback"}


def test_failed_atomic_upgrade_preserves_original_manifest_and_cleans_temporary_file(tmp_path, monkeypatch):
    root = tmp_path / "roles"
    root.mkdir()
    manifest = root / "roles.json"
    manifest.write_text(json.dumps({"version": 1, "roles": [{
        "id": "role-1", "name": "小葵", "profile": {"profile": "背景"},
        "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z",
    }]}), encoding="utf-8")
    original = manifest.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(role_store_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        RoleStore(root)

    assert manifest.read_bytes() == original
    assert list(root.glob(".roles.json.*.tmp")) == []


@pytest.mark.parametrize("payload", [
    [], {"version": 1, "roles": {}}, {"version": 1, "roles": [None]},
    {"version": True, "roles": []}, {"version": "1", "roles": []},
])
def test_malformed_manifest_is_rejected_instead_of_becoming_an_empty_store(tmp_path, payload):
    manifest = tmp_path / "roles.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    original = manifest.read_bytes()
    with pytest.raises(ValueError, match="角色清单"):
        RoleStore(tmp_path)
    assert manifest.read_bytes() == original


@pytest.mark.parametrize("raw", [b'{invalid-json', bytes([255, 254])])
def test_corrupt_json_and_invalid_utf8_leave_original_bytes_untouched(tmp_path, raw):
    manifest = tmp_path / "roles.json"
    manifest.write_bytes(raw)
    with pytest.raises(ValueError, match="UTF-8 JSON"):
        RoleStore(tmp_path)
    assert manifest.read_bytes() == raw


@pytest.mark.parametrize("damage", ["missing-id", "duplicate-id", "missing-timestamp", "future-profile", "flat-v2"])
def test_invalid_role_never_changes_identity_or_overwrites_original(tmp_path, damage):
    role = {
        "id": "role-preserved", "name": "小葵",
        "profile": {"version": 1, "character": {"profile": "背景"}},
        "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z",
    }
    payload = {"version": 2, "roles": [role]}
    if damage == "missing-id":
        role.pop("id")
    elif damage == "missing-timestamp":
        role.pop("createdAt")
    elif damage == "duplicate-id":
        payload["roles"].append(dict(role))
    elif damage == "future-profile":
        role["profile"]["version"] = 99
    elif damage == "flat-v2":
        role["profile"] = {"profile": "背景"}
    manifest = tmp_path / "roles.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    original = manifest.read_bytes()

    with pytest.raises(ValueError, match="角色索引"):
        RoleStore(tmp_path)

    assert manifest.read_bytes() == original
