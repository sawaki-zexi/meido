from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Role, RoleInput, RoleUpdateInput


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RoleStore:
    """Persistent role manifest modeled after Shiori-Agent's RoleStore."""

    def __init__(self, root: str | Path = "roles") -> None:
        self.root = Path(root).resolve()
        self.manifest_path = self.root / "roles.json"
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self._roles: list[Role] = self._load()

    def list(self) -> list[Role]:
        with self._lock:
            return deepcopy(self._roles)

    def get(self, role_id: str) -> Role | None:
        with self._lock:
            for role in self._roles:
                if role.id == role_id:
                    return role.model_copy(deep=True)
        return None

    def create(self, data: RoleInput) -> Role:
        with self._lock:
            now = utc_now()
            role = Role(
                **data.model_dump(),
                id=f"role-{uuid.uuid4().hex}",
                createdAt=now,
                updatedAt=now,
            )
            candidate = [*self._roles, role]
            try:
                self._ensure_role_dirs(role.id)
                self._save(candidate)
            except OSError:
                self.remove_role_files(role.id)
                raise
            self._roles = candidate
            return role.model_copy(deep=True)

    def update(self, role_id: str, data: RoleUpdateInput) -> Role:
        with self._lock:
            index = next((i for i, role in enumerate(self._roles) if role.id == role_id), None)
            if index is None:
                raise KeyError(role_id)
            current = self._roles[index]
            values = current.model_dump(exclude={"id", "createdAt", "updatedAt"})
            values.update(data.model_dump())
            updated = Role(**values, id=current.id, createdAt=current.createdAt, updatedAt=utc_now())
            candidate = [*self._roles]
            candidate[index] = updated
            self._save(candidate)
            self._roles = candidate
            return updated.model_copy(deep=True)

    def set_model_configuration(self, role_id: str, configuration_id: str | None) -> Role:
        with self._lock:
            index = next((i for i, role in enumerate(self._roles) if role.id == role_id), None)
            if index is None:
                raise KeyError(role_id)
            current = self._roles[index]
            updated = current.model_copy(update={
                "modelConfigurationId": configuration_id,
                "updatedAt": utc_now(),
            })
            candidate = [*self._roles]
            candidate[index] = updated
            self._save(candidate)
            self._roles = candidate
            return updated.model_copy(deep=True)

    def delete(self, role_id: str) -> Role:
        with self._lock:
            index = next((i for i, role in enumerate(self._roles) if role.id == role_id), None)
            if index is None:
                raise KeyError(role_id)
            deleted = self._roles[index]
            candidate = [*self._roles[:index], *self._roles[index + 1 :]]
            self._save(candidate)
            self._roles = candidate
            return deleted.model_copy(deep=True)

    def restore_deleted(self, role: Role) -> None:
        with self._lock:
            if any(current.id == role.id for current in self._roles):
                return
            candidate = [*self._roles, role.model_copy(deep=True)]
            self._save(candidate)
            self._ensure_role_dirs(role.id)
            self._roles = candidate

    def remove_role_files(self, role_id: str) -> None:
        role_path = (self.root / role_id).resolve()
        if role_path.parent != self.root or not role_path.exists():
            return
        shutil.rmtree(role_path)

    def stage_role_files_for_deletion(self, role_id: str) -> Path | None:
        role_path = (self.root / role_id).resolve()
        if role_path.parent != self.root or not role_path.exists():
            return None
        staged_path = self.root / f".deleted-{role_id}-{uuid.uuid4().hex}"
        os.replace(role_path, staged_path)
        return staged_path

    def restore_staged_role_files(self, role_id: str, staged_path: Path | None) -> None:
        if staged_path is None or not staged_path.exists():
            return
        role_path = (self.root / role_id).resolve()
        if role_path.parent != self.root:
            raise ValueError("角色路径无效")
        os.replace(staged_path, role_path)

    @staticmethod
    def purge_staged_role_files(staged_path: Path | None) -> None:
        if staged_path is not None and staged_path.exists():
            shutil.rmtree(staged_path)

    def _load(self) -> list[Role]:
        if not self.manifest_path.exists():
            return []
        raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        roles = raw.get("roles", []) if isinstance(raw, dict) else []
        return [Role.model_validate(self._normalize(item)) for item in roles if isinstance(item, dict)]

    @staticmethod
    def _normalize(item: dict[str, Any]) -> dict[str, Any]:
        profile = item.get("profile") or {}
        return {
            "id": item.get("id") or f"role-{uuid.uuid4().hex}",
            "name": item.get("name", ""),
            "description": item.get("description", ""),
            "profile": profile,
            "modelConfig": item.get("modelConfig") or {},
            "modelConfigurationId": item.get("modelConfigurationId"),
            "proactiveConfig": item.get("proactiveConfig") or {},
            "createdAt": item.get("createdAt") or utc_now(),
            "updatedAt": item.get("updatedAt") or utc_now(),
        }

    def _ensure_role_dirs(self, role_id: str) -> None:
        (self.root / role_id / "memory").mkdir(parents=True, exist_ok=True)
        (self.root / role_id / "state").mkdir(parents=True, exist_ok=True)

    def _save(self, roles: list[Role] | None = None) -> None:
        persisted_roles = roles if roles is not None else self._roles
        payload = {"version": 1, "roles": [role.model_dump(mode="json") for role in persisted_roles]}
        temporary = self.manifest_path.with_name(f".{self.manifest_path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.manifest_path)
