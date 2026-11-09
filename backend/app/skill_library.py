"""Ordinary, non-executable skills: owner management and frozen run readers.

Files remain the source of guidance; the small settings file contains only
owner choices. Public authoring writes application data, never a release tree.
"""
from __future__ import annotations

import hashlib
import base64
import json
import os
import shutil
import threading
import uuid
from pathlib import Path

from .agent_runtime.skills import SkillRegistry, _path_has_link, _validate_relative_path, validate_skill_text

_LOCK = threading.RLock()
# Explicitly reviewed built-in methods. Old user-authored explicit skills never
# inherit this migration choice. Original activation survives rollback.
BUILTIN_AUTOMATIC = frozenset({"skill-creater", "weather", "summarize", "feed-manage", "meme-manage",
    "conversation-recall", "conversation-continuity", "codex-delegate", "create-drift-skill", "create-proactive-source"})


class SkillLibrary:
    def __init__(self, roles, role_id: str, shared_root: Path, project_root: Path, builtin_root: Path, *, dependency_check=None):
        self.roles, self.role_id = roles, role_id
        self.private_root = roles.root / role_id / "workspace" / "skills"
        self.shared_root = Path(shared_root)
        self.roots = ((self.private_root, "role"), (roles.root / role_id / "skills", "role"),
            (self.shared_root, "shared"), (Path(project_root), "project"), (Path(builtin_root), "builtin"))
        self.settings_path = roles.root / role_id / "skill-settings.json"
        self.dependency_check = dependency_check

    def require_role(self):
        if self.roles.get(self.role_id) is None:
            raise KeyError("角色不存在")

    def settings(self):
        self.require_role()
        if _path_has_link(self.settings_path):
            raise ValueError("技能设置不能使用链接")
        try:
            raw = self.settings_path.read_bytes()
        except FileNotFoundError:
            return {"enabled": False, "revision": 0, "overrides": {}}
        if len(raw) > 256 * 1024:
            raise ValueError("技能设置过大")
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool) or not isinstance(value.get("overrides"), dict):
            raise ValueError("技能设置无效")
        return value

    def update_settings(self, *, enabled=None, ref=None, active=None, policy=None, expected_revision=None):
        with _LOCK:
            current = self.settings()
            if expected_revision is not None and current["revision"] != expected_revision:
                raise ValueError("保存冲突：技能设置已变化，请刷新后重试")
            value = {**current, "overrides": {key: dict(item) for key, item in current["overrides"].items()},
                     "revision": current["revision"] + 1}
            if enabled is not None:
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be boolean")
                value["enabled"] = enabled
            if ref is not None:
                if not any(self.reference(item) == ref for item in self.discover().all_skills):
                    raise ValueError("技能引用不存在或不属于当前角色")
                setting = value["overrides"].setdefault(ref, {})
                if active is not None:
                    if not isinstance(active, bool):
                        raise ValueError("active must be boolean")
                    setting["enabled"] = active
                if policy is not None:
                    if policy not in {"automatic", "manual", "constant"}:
                        raise ValueError("未知调用策略")
                    setting["policy"] = policy
            self.require_role()
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_text(self.settings_path, json.dumps(value, ensure_ascii=False))
            return value

    def discover(self):
        self.require_role()
        return SkillRegistry.discover_many(self.roots)

    def reference(self, skill):
        # The digest separates legacy/workspace roots and different roles. A
        # caller receives no host path and cannot manufacture another scope.
        scope = self.role_id if skill.source == "role" else "shared"
        slot = ('workspace' if skill.source == 'role' and skill.root_path is not None
                and Path(skill.root_path).resolve() == self.private_root.resolve() else 'legacy' if skill.source == 'role' else skill.source)
        identity = f"{scope}:{skill.source}:{slot}:{skill.path}"
        return f"skill:{skill.source}:{skill.skill_id}:{hashlib.sha256(identity.encode()).hexdigest()[:16]}"

    def policy(self, skill, settings):
        override = settings["overrides"].get(self.reference(skill), {}).get("policy")
        if override:
            return override
        if skill.allow_implicit_invocation is False or skill.trust_level in {"installed", "external"}:
            return "manual"
        if skill.activation == "role_default":
            return "constant"
        if skill.activation in {"keyword", "model"} or skill.allow_implicit_invocation is True:
            return "automatic"
        if skill.source == "builtin" and skill.skill_id in BUILTIN_AUTOMATIC:
            return "automatic"
        return "manual"

    def view(self):
        settings, discovered = self.settings(), self.discover()
        selected = {}
        rows = []
        diagnostics = [dict(item) for item in discovered.diagnostics if item.get('status') != 'shadowed']
        for skill in discovered.all_skills:
            ref = self.reference(skill)
            active = settings["overrides"].get(ref, {}).get("enabled", True)
            shadowed = active and skill.skill_id in selected
            if active and not shadowed:
                selected[skill.skill_id] = skill
            if shadowed:
                diagnostics.append({'skillId': skill.skill_id, 'status': 'shadowed', 'source': skill.source,
                    'version': skill.version, 'selectedSource': selected[skill.skill_id].source})
            rows.append({**skill.to_dict(), "ref": ref, "enabled": active, "effective": active and not shadowed,
                "status": "disabled" if not active else "shadowed" if shadowed else "available",
                "policy": self.policy(skill, settings), "legacyPolicy": skill.activation,
                "editable": skill.source == "role", "scope": self.role_id if skill.source == "role" else "shared"})
        return settings, SkillRegistry(selected.values(), diagnostics=diagnostics,
            all_skills=discovered.all_skills), rows

    def describe(self, dependency_check=None):
        dependency_check = dependency_check or self.dependency_check
        settings, registry, rows = self.view()
        by_ref = {self.reference(item): item for item in registry.all_skills}
        for row in rows:
            skill = by_ref[row["ref"]]
            reasons = dependencies(skill, dependency_check)
            try:
                skill.materialize()
            except (OSError, ValueError, UnicodeError) as error:
                reasons.append(str(error))
            row["problems"] = reasons
            if reasons and row["status"] == "available":
                row["status"] = "unavailable"
        diagnostics = [{key: value for key, value in item.items() if key not in {"path"}}
                       for item in registry.diagnostics if item.get("status") != "shadowed"]
        return {"enabled": settings["enabled"], "revision": settings["revision"], "skills": rows,
            "diagnostics": diagnostics,
            "migration": [{"ref": row["ref"], "name": row["name"], "from": row["legacyPolicy"], "to": row["policy"]}
                          for row in rows if row["effective"]]}

    def document(self, ref):
        _, registry, _ = self.view()
        skill = next((item for item in registry.all_skills if self.reference(item) == ref), None)
        if skill is None:
            raise ValueError("技能引用不存在或不属于当前角色")
        if _path_has_link(Path(skill.body_path)):
            raise ValueError("技能文件不能使用链接")
        with Path(skill.body_path).open('rb') as stream:
            raw = stream.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024 or hashlib.sha256(raw).hexdigest() != skill.content_hash:
            raise ValueError("技能内容已变化，请刷新后重试")
        return {"ref": ref, "content": raw.decode('utf-8'), "contentHash": skill.content_hash, "source": skill.source,
                "ignoredMetadata": list(skill.ignored_metadata)}

    def save(self, content, *, ref=None, expected_hash=None, resources=None):
        metadata = validate_skill_text(content)
        with _LOCK:
            self.require_role()
            target_root = self.private_root
            if ref is not None:
                document = self.document(ref)
                if document["source"] != "role":
                    raise ValueError("公共和内置技能只读；请创建角色私有副本")
                _, registry, _ = self.view()
                descriptor = next(item for item in registry.all_skills if self.reference(item) == ref)
                if descriptor.skill_id != metadata["id"]:
                    raise ValueError("更新不能改变技能 ID；请另存新技能")
                if not expected_hash or document["contentHash"] != expected_hash:
                    raise ValueError("保存冲突：技能已变化，请重新读取")
                target = Path(descriptor.body_path)
            else:
                target = target_root / metadata["id"] / "SKILL.md"
                if target.exists():
                    raise ValueError("保存冲突：同 ID 技能已存在，请选择编辑或使用新名称")
            if _path_has_link(target):
                raise ValueError("技能文件不能使用链接")
            target.parent.mkdir(parents=True, exist_ok=True)
            resource_bytes = {}
            for name, value in (resources or {}).items():
                _validate_relative_path(name, "resource")
                if name.split("/")[0] not in {"references", "assets", "scripts"}:
                    raise ValueError("支持资源须位于 references、assets 或 scripts 下")
                if isinstance(value, str):
                    raw = value.encode('utf-8')
                elif isinstance(value, dict) and value.get('encoding') in {'utf-8', 'base64'} and isinstance(value.get('content'), str):
                    try:
                        raw = (base64.b64decode(value['content'], validate=True) if value['encoding'] == 'base64'
                               else value['content'].encode('utf-8'))
                    except ValueError:
                        raise ValueError("支持文件的编码无效") from None
                else:
                    raise ValueError("支持文件须提供明确的文本或 base64 编码")
                path = target.parent / name
                if _path_has_link(path) or len(raw) > 256 * 1024:
                    raise ValueError("支持资源不安全或过大")
                resource_bytes[name] = raw
            for name, raw in resource_bytes.items():
                path = target.parent / name
                path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_bytes(path, raw)
            _atomic_text(target, content)
            # Read back the real saved bytes and rediscover with the same parser
            # used by chat; never treat a successful upload as installation.
            if target.read_bytes() != content.encode('utf-8'):
                raise OSError("技能保存后的核验失败")
            _, registry, _ = self.view()
            saved = next((item for item in registry.all_skills if item.body_path == str(target.resolve())), None)
            if saved is None:
                raise ValueError("技能保存后无法发现")
            result = self.document(self.reference(saved))
            problems = dependencies(saved, self.dependency_check)
            try:
                saved.materialize()
            except (ValueError, OSError) as error:
                problems.append(str(error))
            return {**result, "saved": True, "verified": True, "problems": problems}

    def remove(self, ref, expected_hash):
        with _LOCK:
            document = self.document(ref)
            if document["source"] != "role" or document["contentHash"] != expected_hash:
                raise ValueError("移除冲突或该技能不是角色私有技能")
            registry = self.discover()
            item = next(item for item in registry.all_skills if self.reference(item) == ref)
            directory = Path(item.body_path).parent
            resolved = directory.resolve()
            role_roots = tuple(root.resolve() for root, source in self.roots if source == 'role')
            if _path_has_link(directory) or not any(resolved != root and resolved.is_relative_to(root) for root in role_roots):
                raise ValueError("技能移除路径无效")
            # Only this explicitly selected bundle, never the skills root.
            shutil.rmtree(directory)

    def publish(self, ref, expected_hash):
        with _LOCK:
            doc = self.document(ref)
            if doc["source"] != "role" or doc["contentHash"] != expected_hash:
                raise ValueError("发布冲突：请重新读取角色私有技能")
            skill = next(item for item in self.discover().all_skills if self.reference(item) == ref)
            skill.materialize()
            target = self.shared_root / skill.skill_id
            if _path_has_link(target) or target.exists():
                raise ValueError("发布冲突：公共库已有同 ID 技能，请使用新 ID")
            self.shared_root.mkdir(parents=True, exist_ok=True)
            # Keep incomplete bundles outside discovery roots; rename from
            # the same data directory/filesystem only after the whole copy.
            staging = self.shared_root.parent / f".publish-{uuid.uuid4().hex}"
            try:
                staging.mkdir()
                _atomic_text(staging / "SKILL.md", doc["content"])
                root = Path(skill.root_path)
                for resource in skill.supporting_files:
                    source = root / resource.path
                    relative = source.relative_to(Path(skill.body_path).parent)
                    destination = staging / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with source.open('rb') as stream:
                        raw = stream.read(256 * 1024 + 1)
                    if len(raw) != resource.size or hashlib.sha256(raw).hexdigest() != resource.content_hash:
                        raise ValueError("发布时支持资源已变化")
                    destination.write_bytes(raw)
                self.require_role()
                staging.rename(target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            return {"published": True, "id": skill.skill_id, "source": "shared"}


def dependencies(skill, check=None):
    problems = []
    for name in skill.required_bins:
        reason = check(skill, name) if check else None if shutil.which(name) else "command is unavailable"
        if reason:
            problems.append(f"missingBins: {name}: {reason}")
    # Ordinary tools never inherit arbitrary backend/API credentials.
    if skill.required_env:
        problems.append("missingEnv: " + ", ".join(skill.required_env))
    return problems


def _atomic_text(path, text):
    _atomic_bytes(path, text.encode('utf-8'))


def _atomic_bytes(path, data):
    if _path_has_link(path):
        raise ValueError("技能文件不能使用链接")
    temporary = path.parent / f".skill-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
