"""Lossless upgrades of role manifests, inspired by Shiori role migrations."""

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
import uuid

CURRENT_MANIFEST_VERSION = 2


def _upgrade_v1(payload: dict[str, Any]) -> dict[str, Any]:
    migrated_at = datetime.now(timezone.utc).isoformat()
    for role in payload["roles"]:
        if not role.get("id"):
            role["id"] = f"role-{uuid.uuid4().hex}"
        for field in ("createdAt", "updatedAt"):
            if not role.get(field):
                role[field] = migrated_at
        profile = role.get("profile")
        if isinstance(profile, dict) and "character" not in profile and "version" not in profile:
            role["profile"] = {"version": 1, "character": profile}
    payload["version"] = 2
    return payload


MIGRATIONS = {1: _upgrade_v1}


def migrate_manifest(payload: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(payload, dict):
        raise ValueError("角色清单格式无效：根节点必须是对象")
    version = payload.get("version", 1)
    if type(version) is not int or not 1 <= version <= CURRENT_MANIFEST_VERSION:
        raise ValueError(f"角色清单版本不支持（当前支持 1–{CURRENT_MANIFEST_VERSION}）")
    roles = payload.get("roles")
    if not isinstance(roles, list):
        raise ValueError(f"角色清单 v{version} 格式无效：roles 必须是数组")
    for index, role in enumerate(roles):
        if not isinstance(role, dict):
            raise ValueError(f"角色清单 v{version} 的角色索引 {index} 必须是对象")
    result = deepcopy(payload)
    while result.get("version", 1) < CURRENT_MANIFEST_VERSION:
        result = MIGRATIONS[result.get("version", 1)](result)
    return result, version != CURRENT_MANIFEST_VERSION


def overlay_fields(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Keep opaque stored extensions without exposing them in API/runtime models."""
    result = deepcopy(previous)
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = overlay_fields(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result
