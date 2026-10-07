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
        self._deleting: set[str] = set()
        self.recovery_errors: list[str] = []
        self.root.mkdir(parents=True, exist_ok=True)
        self._roles: list[Role] = self._load()
        self.cleanup_staged_role_files()

    def list(self) -> list[Role]:
        with self._lock:
            return deepcopy(self._roles)

    def get(self, role_id: str) -> Role | None:
        with self._lock:
            for role in self._roles:
                if role.id == role_id:
                    return role.model_copy(deep=True)
        return None

    def workspace_path(self, role_id: str) -> Path:
        """Return the role-owned workspace, creating it for legacy roles."""
        role = self.get(role_id)
        if role is None:
            raise KeyError(role_id)
        workspace = (self.root / role_id / "workspace").resolve()
        if workspace.parent.parent != self.root or workspace.parent.name != role_id:
            raise ValueError("角色 workspace 路径无效")
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

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
            self._require_writable(role_id)
            index = next((i for i, role in enumerate(self._roles) if role.id == role_id), None)
            if index is None:
                raise KeyError(role_id)
            current = self._roles[index]
            values = current.model_dump(exclude={"id", "createdAt", "updatedAt"})
            values.update(data.model_dump(exclude_unset=True, exclude_none=True))
            updated = Role(**values, id=current.id, createdAt=current.createdAt, updatedAt=utc_now())
            candidate = [*self._roles]
            candidate[index] = updated
            self._save(candidate)
            self._roles = candidate
            return updated.model_copy(deep=True)

    def set_model_configuration(self, role_id: str, configuration_id: str | None) -> Role:
        with self._lock:
            self._require_writable(role_id)
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

    def set_avatar(self, role_id: str, content: bytes, media_type: str, *, original: bool = False, card: bool = False) -> Role:
        with self._lock:
            self._require_writable(role_id)
            index = next((i for i, role in enumerate(self._roles) if role.id == role_id), None)
            if index is None:
                raise KeyError(role_id)
            role_dir = self.root / role_id
            file_name = "avatar-original" if original else "card-image" if card else "avatar"
            avatar_path = role_dir / file_name
            temporary_path = role_dir / f".avatar-{uuid.uuid4().hex}.tmp"
            previous_path = role_dir / f".avatar-{uuid.uuid4().hex}.bak"
            had_previous = avatar_path.exists()
            staged_previous = False
            installed_new = False
            try:
                temporary_path.write_bytes(content)
                if had_previous:
                    os.replace(avatar_path, previous_path)
                    staged_previous = True
                os.replace(temporary_path, avatar_path)
                installed_new = True
                current = self._roles[index]
                updated_at = utc_now()
                updated = current.model_copy(update={
                    ("avatarOriginalUrl" if original else "cardImageUrl" if card else "avatarUrl"): f"/api/roles/{role_id}/avatar-original?v={updated_at.timestamp()}" if original else f"/api/roles/{role_id}/card-image?v={updated_at.timestamp()}" if card else f"/api/roles/{role_id}/avatar?v={updated_at.timestamp()}",
                    ("avatarOriginalMediaType" if original else "cardImageMediaType" if card else "avatarMediaType"): media_type,
                    "updatedAt": updated_at,
                })
                candidate = [*self._roles]
                candidate[index] = updated
                self._save(candidate)
            except OSError:
                temporary_path.unlink(missing_ok=True)
                if installed_new:
                    avatar_path.unlink(missing_ok=True)
                if staged_previous and previous_path.exists():
                    os.replace(previous_path, avatar_path)
                raise
            self._roles = candidate
            try:
                previous_path.unlink(missing_ok=True)
            except OSError:
                pass
            return updated.model_copy(deep=True)

    def remove_avatar(self, role_id: str) -> Role:
        with self._lock:
            self._require_writable(role_id)
            index = next((i for i, role in enumerate(self._roles) if role.id == role_id), None)
            if index is None:
                raise KeyError(role_id)
            role_dir = self.root / role_id
            avatar_path = role_dir / "avatar"
            original_path = role_dir / "avatar-original"
            staged_path = role_dir / f".avatar-{uuid.uuid4().hex}.bak"
            had_avatar = avatar_path.exists()
            had_original = original_path.exists()
            if had_avatar:
                os.replace(avatar_path, staged_path)
            staged_original = role_dir / f".avatar-original-{uuid.uuid4().hex}.bak"
            if had_original:
                os.replace(original_path, staged_original)
            current = self._roles[index]
            updated = current.model_copy(update={
                "avatarUrl": None,
                "avatarMediaType": None,
                "avatarOriginalUrl": None,
                "avatarOriginalMediaType": None,
                "updatedAt": utc_now(),
            })
            candidate = [*self._roles]
            candidate[index] = updated
            try:
                self._save(candidate)
            except OSError:
                if had_avatar and staged_path.exists():
                    os.replace(staged_path, avatar_path)
                if had_original and staged_original.exists():
                    os.replace(staged_original, original_path)
                raise
            self._roles = candidate
            try:
                staged_path.unlink(missing_ok=True)
                staged_original.unlink(missing_ok=True)
            except OSError:
                pass
            return updated.model_copy(deep=True)

    def avatar_path(self, role_id: str) -> Path:
        role_dir = (self.root / role_id).resolve()
        if role_dir.parent != self.root or not role_dir.is_dir():
            raise KeyError(role_id)
        return role_dir / "avatar"

    def avatar_original_path(self, role_id: str) -> Path:
        role_dir = (self.root / role_id).resolve()
        if role_dir.parent != self.root or not role_dir.is_dir():
            raise KeyError(role_id)
        return role_dir / "avatar-original"

    def remove_card_image(self, role_id: str) -> Role:
        with self._lock:
            self._require_writable(role_id)
            index = next((i for i, role in enumerate(self._roles) if role.id == role_id), None)
            if index is None:
                raise KeyError(role_id)
            role_dir = self.root / role_id
            image_path = role_dir / "card-image"
            staged = role_dir / f".card-image-{uuid.uuid4().hex}.bak"
            had_image = image_path.exists()
            if had_image:
                os.replace(image_path, staged)
            current = self._roles[index]
            updated = current.model_copy(update={"cardImageUrl": None, "cardImageMediaType": None, "updatedAt": utc_now()})
            candidate = [*self._roles]
            candidate[index] = updated
            try:
                self._save(candidate)
            except OSError:
                if had_image and staged.exists():
                    os.replace(staged, image_path)
                raise
            self._roles = candidate
            staged.unlink(missing_ok=True)
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

    def begin_role_deletion(self, role_id: str) -> Path:
        """Persist an intent marker so an interrupted deletion can resume safely."""
        with self._lock:
            if self.get(role_id) is None:
                raise KeyError(role_id)
            marker = self.root / f".deleting-{role_id}-{uuid.uuid4().hex}.json"
            marker.write_text(json.dumps({"roleId": role_id}) + "\n", encoding="utf-8")
            self._deleting.add(role_id)
        return marker

    def _require_writable(self, role_id: str) -> None:
        if role_id in self._deleting:
            raise KeyError(role_id)

    def end_role_deletion(self, role_id: str) -> None:
        with self._lock:
            self._deleting.discard(role_id)

    def complete_role_deletion(self, marker: Path | None) -> None:
        if marker is None:
            return
        role_id = self._deletion_marker_role(marker)
        if any(self.root.glob(f".deleted-{role_id}-*")):
            raise OSError("角色文件暂存目录尚未清理")
        if self.get(role_id) is None and (self.root / role_id).exists():
            raise OSError("已删除角色的文件目录尚未清理")
        marker.unlink(missing_ok=True)
        self.end_role_deletion(role_id)

    def pending_role_deletions(self) -> list[str]:
        """Return deletion intents whose role manifest entry is already gone."""
        pending: list[str] = []
        with self._lock:
            for marker in self.root.glob(".deleting-*.json"):
                try:
                    role_id = self._deletion_marker_role(marker)
                except OSError as error:
                    self.recovery_errors.append(f"{marker.name}: {error}")
                    continue
                if self.get(role_id) is None and role_id not in pending:
                    pending.append(role_id)
        return pending

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

    def cleanup_staged_role_files(self) -> int:
        """Recover or remove staged role directories after an interrupted delete."""
        handled = 0
        with self._lock:
            role_ids = {role.id for role in self._roles}
            for path in sorted(self.root.glob(".deleted-*")):
                if not path.is_dir() or path.is_symlink() or path.resolve().parent != self.root:
                    continue
                role_id = path.name.removeprefix(".deleted-").rsplit("-", 1)[0]
                try:
                    role_path = self.root / role_id
                    if role_id in role_ids:
                        if role_path.exists():
                            # Keep both copies: an incomplete rollback may
                            # have recreated an empty canonical directory.
                            raise OSError("角色目录和删除暂存同时存在，需要恢复检查")
                        os.replace(path, role_path)
                    else:
                        shutil.rmtree(path)
                except OSError as error:
                    self.recovery_errors.append(f"{path.name}: {error}")
                    continue
                handled += 1
            for marker in self.root.glob(".deleting-*.json"):
                try:
                    role_id = self._deletion_marker_role(marker)
                    if role_id not in role_ids:
                        role_path = self.root / role_id
                        if role_path.exists():
                            shutil.rmtree(role_path)
                    elif not any(self.root.glob(f".deleted-{role_id}-*")):
                        self.complete_role_deletion(marker)
                except OSError as error:
                    self.recovery_errors.append(f"{marker.name}: {error}")
        return handled

    def _deletion_marker_role(self, marker: Path) -> str:
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            role_id = payload.get("roleId") if isinstance(payload, dict) else None
        except (OSError, json.JSONDecodeError) as error:
            raise OSError("角色删除标记无效") from error
        if (
            not isinstance(role_id, str) or not role_id
            or (self.root / role_id).resolve().parent != self.root
            or any(character in role_id for character in "*/\\?[]")
            or not marker.name.startswith(f".deleting-{role_id}-")
        ):
            raise OSError("角色删除标记缺少有效 roleId")
        return role_id

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
            "avatarUrl": item.get("avatarUrl"),
            "avatarMediaType": item.get("avatarMediaType"),
            "avatarOriginalUrl": item.get("avatarOriginalUrl"),
            "avatarOriginalMediaType": item.get("avatarOriginalMediaType"),
            "cardImageUrl": item.get("cardImageUrl"),
            "cardImageMediaType": item.get("cardImageMediaType"),
            "proactiveConfig": item.get("proactiveConfig") or {},
            "agentConfig": item.get("agentConfig") or {},
            "createdAt": item.get("createdAt") or utc_now(),
            "updatedAt": item.get("updatedAt") or utc_now(),
        }

    def _ensure_role_dirs(self, role_id: str) -> None:
        (self.root / role_id / "memory").mkdir(parents=True, exist_ok=True)
        (self.root / role_id / "state").mkdir(parents=True, exist_ok=True)
        (self.root / role_id / "workspace").mkdir(parents=True, exist_ok=True)

    def _save(self, roles: list[Role] | None = None) -> None:
        persisted_roles = roles if roles is not None else self._roles
        payload = {"version": 1, "roles": [role.model_dump(mode="json") for role in persisted_roles]}
        temporary = self.manifest_path.with_name(f".{self.manifest_path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.manifest_path)
