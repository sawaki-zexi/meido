from __future__ import annotations

import json
import hashlib
import os
import shutil
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from collections.abc import Callable

from .models import Role, RoleInput, RoleUpdateInput, RoleIllustration


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
            if data.agentConfig is not None:
                # Purpose-specific settings submit only edited fields. Preserve
                # every omitted tool policy, including legacy extension grants.
                settings = current.agentConfig.model_dump()
                for field, change in data.agentConfig.model_dump(exclude_unset=True).items():
                    if isinstance(change, dict) and isinstance(settings.get(field), dict):
                        settings[field] = {**settings[field], **change}
                    else:
                        settings[field] = change
                values['agentConfig'] = settings
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

    def _save_asset_role(self, role: Role) -> Role:
        """Caller holds the role lock; manifest commit is the visibility boundary."""
        candidate = [role if current.id == role.id else current for current in self._roles]
        self._save(candidate)
        self._roles = candidate
        return role.model_copy(deep=True)

    def illustration_path(self, role_id: str, identifier: str) -> Path:
        role = self.get(role_id)
        if role is None or not any(item.id == identifier for item in role.illustrations):
            raise KeyError('角色插画不存在')
        root = self.root / role_id / 'illustrations'
        path = root / identifier
        if root.is_symlink() or root.resolve().parent != self.root / role_id or path.is_symlink() or path.resolve().parent != root.resolve():
            raise ValueError('角色插画路径无效')
        return path

    def add_illustration(self, role_id: str, content: bytes, media_type: str, name: str = '插画', *,
                         category: str | None = None, set_background: bool = False,
                         operation_id: str | None = None, authorized: Callable[[], bool] | None = None,
                         expected_background_id: str | None = None) -> Role:
        from .novelai_generation.media import validate_image
        if not content or len(content) > 10 * 1024 * 1024:
            raise ValueError('插画不能超过 10 MiB')
        detected, _ = validate_image(content, max_pixels=16 * 1024 * 1024)
        if media_type not in {'image/png', 'image/jpeg', 'image/webp'} or detected != media_type:
            raise ValueError('插画必须为 PNG、JPEG 或 WebP 图片')
        if operation_id is not None and (len(operation_id) > 80 or not operation_id.isascii()):
            raise ValueError('插画操作标识无效')
        identifier = hashlib.sha256(operation_id.encode() if operation_id else content).hexdigest()[:32]
        with self._lock:
            self._require_writable(role_id)
            role = self.get(role_id)
            if role is None:
                raise KeyError(role_id)
            existing = next((item for item in role.illustrations if item.id == identifier), None)
            # A lost local acknowledgement only observes the prior operation;
            # it must never restore its background over a later owner edit.
            if existing and operation_id is not None:
                return role
            if authorized is not None and (not authorized() or (set_background and role.chatBackgroundId != expected_background_id)):
                raise PermissionError('插画写回许可或聊天背景已改变')
            category = category or role.illustrationCategories[0]
            if category not in role.illustrationCategories:
                raise ValueError('插画分类不存在')
            if existing:
                if set_background:
                    return self.set_chat_background(role_id, identifier)
                return role
            if len(role.illustrations) >= 250 or sum(item.size for item in role.illustrations) + len(content) > 1024 * 1024 * 1024:
                raise ValueError('角色插画库已达到 250 张或 1 GiB 上限')
            root = self.root / role_id / 'illustrations'
            root.mkdir(exist_ok=True)
            if root.is_symlink() or root.resolve().parent != self.root / role_id:
                raise ValueError('插画目录路径无效')
            target = root / identifier
            if target.is_symlink():
                raise ValueError('插画目标路径无效')
            temporary = root / ('.' + uuid.uuid4().hex + '.tmp')
            try:
                temporary.write_bytes(content)
                os.replace(temporary, target)
                item = RoleIllustration(id=identifier, url=f'/api/roles/{role_id}/illustrations/{identifier}',
                    name=Path(name).name[:160] or '插画', mediaType=media_type, size=len(content), category=category)
                updated = role.model_copy(update={'illustrations': [*role.illustrations, item], 'updatedAt': utc_now(),
                    **({'chatBackgroundId': identifier, 'chatBackgroundUrl': item.url} if set_background else {})})
                if authorized is not None and not authorized():
                    raise PermissionError('插画写回许可已撤销')
                return self._save_asset_role(updated)
            except BaseException:
                temporary.unlink(missing_ok=True)
                # An unsuccessful manifest write never removes an already visible image.
                if not any(item.id == identifier for item in (self.get(role_id) or role).illustrations):
                    target.unlink(missing_ok=True)
                raise

    def reconcile_generated_illustration(self, role_id: str, *, operation_id: str, digest: str, size: int) -> bool | None:
        """Read-only acknowledgement; never restores a later changed background."""
        if (not operation_id or len(operation_id) > 80 or not operation_id.isascii()
                or len(digest) != 64 or any(char not in '0123456789abcdef' for char in digest)
                or type(size) is not int or not 1 <= size <= 10 * 1024 * 1024):
            raise ValueError('插画恢复标识无效')
        identifier = hashlib.sha256(operation_id.encode()).hexdigest()[:32]
        with self._lock:
            role = self.get(role_id)
            if role is None:
                raise KeyError(role_id)
            item = next((item for item in role.illustrations if item.id == identifier), None)
            if item:
                path = self.illustration_path(role_id, identifier)
                if not path.is_file():
                    return None
                with path.open('rb') as handle:
                    content = handle.read(size + 1)
                return len(content) == size and hashlib.sha256(content).hexdigest() == digest
            # An explicitly removed file proves the earlier write completed,
            # but is not permission to make it visible again.
            recovery = self.root / role_id / 'illustrations-removed'
            for path in recovery.glob(identifier + '-*'):
                if path.is_symlink() or path.resolve().parent != recovery.resolve():
                    continue
                with path.open('rb') as handle:
                    content = handle.read(size + 1)
                if len(content) == size and hashlib.sha256(content).hexdigest() == digest:
                    return True
            return False

    def set_chat_background(self, role_id: str, identifier: str | None) -> Role:
        with self._lock:
            self._require_writable(role_id)
            role = self.get(role_id)
            if role is None:
                raise KeyError(role_id)
            item = next((item for item in role.illustrations if item.id == identifier), None)
            if identifier is not None and (item is None or not self.illustration_path(role_id, identifier).is_file()):
                raise KeyError('角色插画不存在')
            return self._save_asset_role(role.model_copy(update={'chatBackgroundId': identifier,
                'chatBackgroundUrl': item.url if item else None, 'updatedAt': utc_now()}))

    def remove_illustration(self, role_id: str, identifier: str) -> Role:
        with self._lock:
            self._require_writable(role_id)
            role = self.get(role_id)
            if role is None:
                raise KeyError(role_id)
            path = self.illustration_path(role_id, identifier)
            recovery = self.root / role_id / 'illustrations-removed'
            recovery.mkdir(exist_ok=True)
            if recovery.is_symlink() or recovery.resolve().parent != self.root / role_id:
                raise ValueError('插画恢复目录路径无效')
            backup = recovery / (identifier + '-' + uuid.uuid4().hex)
            moved = path.exists()
            if moved:
                os.replace(path, backup)
            try:
                moods = {mood: image for mood, image in role.moodIllustrationBindings.items() if image != identifier}
                return self._save_asset_role(role.model_copy(update={
                    'illustrations': [item for item in role.illustrations if item.id != identifier],
                    'moodIllustrationBindings': moods,
                    'defaultMood': role.defaultMood if role.defaultMood in moods else next(iter(moods), '平静'),
                    **({'currentMood': None, 'currentMoodUpdatedAt': None} if role.currentMood not in moods else {}),
                    **({'chatBackgroundId': None, 'chatBackgroundUrl': None} if role.chatBackgroundId == identifier else {}),
                    'updatedAt': utc_now()}))
            except BaseException:
                if moved:
                    os.replace(backup, path)
                raise

    def update_illustrations(self, role_id: str, *, categories: list[str] | None = None,
                             bindings: dict[str, str] | None = None, category_send: dict[str, bool] | None = None,
                             mood_bindings: dict[str, str] | None = None, default_mood: str | None = None) -> Role:
        with self._lock:
            self._require_writable(role_id)
            role = self.get(role_id)
            if role is None:
                raise KeyError(role_id)
            names = categories if categories is not None else role.illustrationCategories
            if (not 1 <= len(names) <= 32 or any(not isinstance(name, str) or not name.strip() or len(name) > 80 for name in names)
                    or len({name.strip().casefold() for name in names}) != len(names)):
                raise ValueError('需要 1–32 个不重复的插画分类，每个最多 80 字符')
            names = [name.strip() for name in names]
            changes = bindings or {}
            if not set(changes) <= {item.id for item in role.illustrations}:
                raise ValueError('不能修改其他角色的插画分类')
            items = [item.model_copy(update={'category': changes.get(item.id, item.category)}) for item in role.illustrations]
            if any(item.category not in names for item in items):
                raise ValueError('分类仍被插画使用，请先移动插画')
            permissions = {name: role.illustrationCategorySend.get(name, False) for name in names}
            if category_send is not None:
                if not set(category_send) <= set(names) or any(type(value) is not bool for value in category_send.values()):
                    raise ValueError('插画分类发送许可无效')
                permissions.update(category_send)
            moods = mood_bindings if mood_bindings is not None else role.moodIllustrationBindings
            if (len(moods) > 32 or any(not isinstance(mood, str) or not mood.strip() or mood != mood.strip() or len(mood) > 40 for mood in moods)
                    or not set(moods.values()) <= {item.id for item in items} or len(set(moods.values())) != len(moods)):
                raise ValueError('最多绑定 32 种心情，每张本角色插画只能绑定一种心情')
            default = (default_mood if default_mood is not None else role.defaultMood).strip()
            if not default or len(default) > 40 or (moods and default not in moods):
                default = next(iter(moods), '平静')
            return self._save_asset_role(role.model_copy(update={'illustrationCategories': names,
                'illustrationCategorySend': permissions, 'moodIllustrationBindings': moods, 'defaultMood': default,
                **({'currentMood': None, 'currentMoodUpdatedAt': None} if role.currentMood not in moods else {}),
                'illustrations': items, 'updatedAt': utc_now()}))

    def set_current_mood(self, role_id: str, mood: str, *, authorized: Callable[[], bool]) -> Role:
        with self._lock:
            self._require_writable(role_id)
            role = self.get(role_id)
            if role is None:
                raise KeyError(role_id)
            if not authorized():
                raise PermissionError('当前角色心情回合已失效')
            if mood not in role.moodIllustrationBindings:
                raise ValueError('只能选择主人已绑定的角色心情')
            return self._save_asset_role(role.model_copy(update={'currentMood': mood,
                'currentMoodUpdatedAt': utc_now(), 'updatedAt': utc_now()}))

    def set_avatar(self, role_id: str, content: bytes, media_type: str, *, original: bool = False, card: bool = False,
                   expected_card_url: str | None = None, authorized: Callable[[], bool] | None = None,
                   operation_id: str | None = None) -> Role:
        with self._lock:
            self._require_writable(role_id)
            index = next((i for i, role in enumerate(self._roles) if role.id == role_id), None)
            if index is None:
                raise KeyError(role_id)
            if authorized is not None and (not card or not authorized() or self._roles[index].cardImageUrl != expected_card_url):
                raise PermissionError('role card permission or expected version changed')
            if operation_id is not None and (len(operation_id) != 32 or any(char not in '0123456789abcdef' for char in operation_id)):
                raise ValueError('invalid generated asset operation')
            role_dir = self.root / role_id
            file_name = "avatar-original" if original else "card-image" if card else "avatar"
            avatar_path = role_dir / file_name
            temporary_path = role_dir / f".avatar-{operation_id or uuid.uuid4().hex}.tmp"
            previous_path = role_dir / f".avatar-{operation_id or uuid.uuid4().hex}.bak"
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
                if authorized is not None and not authorized():
                    raise PermissionError('role card permission was revoked before commit')
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
                if authorized is not None:
                    raise
            return updated.model_copy(deep=True)

    def reconcile_generated_card(self, role_id: str, *, operation_id: str,
                                 expected_card_url: str | None, digest: str, size: int) -> bool | None:
        """Reconcile only this operation's local files under the asset lock.

        A new image without its manifest commit is a rollback obligation, not
        proof that the card was applied. A later owner edit always wins.
        """
        if (len(operation_id) != 32 or any(char not in '0123456789abcdef' for char in operation_id)
                or len(digest) != 64 or any(char not in '0123456789abcdef' for char in digest)
                or type(size) is not int or not 1 <= size <= 64 * 1024 * 1024):
            raise ValueError('invalid generated card recovery identity')
        with self._lock:
            self._require_writable(role_id)
            role = self.get(role_id)
            if role is None:
                raise KeyError(role_id)
            root = self.avatar_path(role_id).parent
            card = root / 'card-image'
            previous = root / ('.avatar-' + operation_id + '.bak')
            temporary = root / ('.avatar-' + operation_id + '.tmp')
            if any(path.is_symlink() or path.resolve().parent != root for path in (card, previous, temporary)):
                raise PermissionError('generated card recovery must remain in its role')
            matches = False
            if card.is_file() and card.stat().st_size == size:
                with card.open('rb') as handle:
                    matches = hashlib.sha256(handle.read(size + 1)).hexdigest() == digest
            applied = matches and role.cardImageUrl != expected_card_url
            if role.cardImageUrl == expected_card_url and (matches or not card.exists()):
                if previous.is_file():
                    os.replace(previous, card)
                elif expected_card_url is None:
                    card.unlink(missing_ok=True)
                else:
                    return None  # Missing old bytes cannot be invented.
            previous.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
            return applied

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
            "illustrations": item.get("illustrations") or [],
            "illustrationCategories": item.get("illustrationCategories") or ['未分类'],
            "illustrationCategorySend": item.get("illustrationCategorySend") or {},
            "moodIllustrationBindings": item.get("moodIllustrationBindings") or {},
            "defaultMood": item.get("defaultMood") or '平静',
            "currentMood": item.get("currentMood"),
            "currentMoodUpdatedAt": item.get("currentMoodUpdatedAt"),
            "chatBackgroundId": item.get("chatBackgroundId"),
            "chatBackgroundUrl": item.get("chatBackgroundUrl"),
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
