from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Iterable


_SKILL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_ACTIVATIONS = frozenset({"explicit", "role_default", "keyword"})
_TRUST_LEVELS = frozenset({"builtin", "project", "installed", "external"})
_IMPLICIT_ACTIVATION_TRUST_LEVELS = frozenset({"installed", "external"})
_TRUST_RANK = {"external": 0, "installed": 1, "project": 2, "builtin": 3}
_MAX_SKILL_BYTES = 256 * 1024
_FRONT_MATTER_DIAGNOSTIC_BYTES = 8 * 1024
_SOURCE_TRUST_LEVELS = {
    "builtin": "builtin",
    "role": "project",
    "project": "project",
    "installed": "installed",
    "external": "external",
}


@dataclass(frozen=True, slots=True)
class SkillResourceDescriptor:
    path: str
    size: int
    content_hash: str
    trust_level: str

    def __post_init__(self) -> None:
        if self.trust_level not in _TRUST_LEVELS:
            raise ValueError(f"unsupported skill resource trust level: {self.trust_level}")
        _validate_relative_path(self.path, "skill resource path")
        if self.size < 0:
            raise ValueError("skill resource size must be non-negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size": self.size,
            "contentHash": self.content_hash,
            "trustLevel": self.trust_level,
        }


@dataclass(frozen=True, slots=True)
class SkillDescriptor:
    skill_id: str
    version: str
    description: str
    tools: tuple[str, ...]
    activation: str
    source: str
    path: str
    content_hash: str
    body: str
    trust_level: str = "project"
    # Discovered skills keep only descriptor metadata in the registry.  These
    # fields are intentionally omitted from ``to_dict`` and are populated only
    # for file-backed descriptors.
    body_path: str | None = None
    root_path: str | None = None
    supporting_files: tuple[SkillResourceDescriptor, ...] = ()

    def __post_init__(self) -> None:
        if self.trust_level not in _TRUST_LEVELS:
            raise ValueError(f"unsupported skill trust level: {self.trust_level}")
        _validate_relative_path(self.path, "skill path")
        source_trust = _SOURCE_TRUST_LEVELS.get(self.source)
        if source_trust is None:
            raise ValueError(f"unsupported skill source: {self.source}")
        if _TRUST_RANK[self.trust_level] > _TRUST_RANK[source_trust]:
            raise ValueError("skill trust level exceeds source trust")
        if any(_TRUST_RANK[item.trust_level] > _TRUST_RANK[self.trust_level] for item in self.supporting_files):
            raise ValueError("skill resource trust level exceeds skill trust")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.skill_id,
            "version": self.version,
            "description": self.description,
            "tools": list(self.tools),
            "activation": self.activation,
            "source": self.source,
            "trustLevel": self.trust_level,
            "path": self.path,
            "contentHash": self.content_hash or None,
            "supportingFiles": [item.to_dict() for item in self.supporting_files],
        }

    def materialize(self) -> SkillDescriptor:
        """Load and verify a file-backed Skill body when it is activated."""

        if self.body_path is None:
            return self
        path = Path(self.body_path)
        root = Path(self.root_path) if self.root_path is not None else path.parent
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("symlinked skills are not supported")
        resolved = path.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError as error:
            raise ValueError("skill path escapes discovery root") from error
        body, content_hash = _read_skill_body(resolved)
        if self.content_hash and content_hash != self.content_hash:
            raise ValueError("skill changed after discovery")
        for resource in self.supporting_files:
            resource_path = root / resource.path
            if resource_path.is_symlink() or resource_path.parent.is_symlink():
                raise ValueError("symlinked skill resource is not supported")
            resolved_resource = resource_path.resolve()
            try:
                resolved_resource.relative_to(root.resolve())
            except ValueError as error:
                raise ValueError("skill resource path escapes discovery root") from error
            current_size, current_hash = _hash_bounded_file(resolved_resource)
            if current_size != resource.size or current_hash != resource.content_hash:
                raise ValueError("skill supporting file changed after discovery")
        return replace(
            self,
            body=body,
            content_hash=content_hash,
            body_path=None,
            root_path=None,
            supporting_files=self.supporting_files,
        )


@dataclass(frozen=True, slots=True)
class SkillResolution:
    skills: tuple[SkillDescriptor, ...]
    prompt_sections: tuple[str, ...]
    diagnostics: tuple[MappingProxyType, ...] = ()


class SkillRegistry:
    """Discover and resolve non-executable SKILL.md resources."""

    def __init__(
        self,
        skills: Iterable[SkillDescriptor] = (),
        *,
        diagnostics: Iterable[dict[str, object]] = (),
    ) -> None:
        self._skills: dict[str, SkillDescriptor] = {}
        for skill in skills:
            self.register(skill)
        self.diagnostics = tuple(dict(item) for item in diagnostics)

    @property
    def skills(self) -> tuple[SkillDescriptor, ...]:
        return tuple(self._skills.values())

    def register(self, skill: SkillDescriptor) -> None:
        if skill.skill_id in self._skills:
            raise ValueError(f"duplicate skill: {skill.skill_id}")
        self._skills[skill.skill_id] = skill

    @classmethod
    def discover(cls, root: str | Path, *, source: str = "project") -> SkillRegistry:
        if source not in _SOURCE_TRUST_LEVELS:
            raise ValueError(f"unsupported skill source: {source}")
        root_path = Path(root).resolve()
        if not root_path.exists() or not root_path.is_dir():
            return cls()
        candidates = (
            [root_path / "SKILL.md"]
            if root_path.name.lower() == "skills" and (root_path / "SKILL.md").is_file()
            else sorted(path / "SKILL.md" for path in root_path.iterdir() if path.is_dir())
        )
        loaded: list[SkillDescriptor] = []
        diagnostics: list[dict[str, object]] = []
        for path in candidates:
            if path.is_symlink() or path.parent.is_symlink():
                diagnostics.append({
                    "path": str(path),
                    "source": source,
                    "version": None,
                    "error": "symlinked skills are not supported",
                })
                continue
            try:
                loaded.append(
                    _parse_skill(
                        path,
                        root_path=root_path,
                        source=source,
                        trust_level=_SOURCE_TRUST_LEVELS[source],
                    )
                )
            except (OSError, UnicodeError, ValueError) as error:
                skill_id, version = _front_matter_metadata(path)
                diagnostics.append({
                    "path": str(path),
                    "skillId": skill_id,
                    "source": source,
                    "version": version,
                    "error": str(error),
                })
        try:
            return cls(loaded, diagnostics=diagnostics)
        except ValueError as error:
            diagnostics.append({"error": str(error)})
            unique: dict[str, SkillDescriptor] = {}
            for skill in loaded:
                unique.setdefault(skill.skill_id, skill)
            return cls(unique.values(), diagnostics=diagnostics)

    @classmethod
    def discover_many(cls, roots: Iterable[tuple[str | Path, str]]) -> SkillRegistry:
        loaded: list[SkillDescriptor] = []
        diagnostics: list[dict[str, object]] = []
        for root, source in roots:
            registry = cls.discover(root, source=source)
            loaded.extend(registry.skills)
            diagnostics.extend(registry.diagnostics)
        try:
            return cls(loaded, diagnostics=diagnostics)
        except ValueError as error:
            diagnostics.append({"error": str(error)})
            unique: dict[str, SkillDescriptor] = {}
            for skill in loaded:
                unique.setdefault(skill.skill_id, skill)
            return cls(unique.values(), diagnostics=diagnostics)

    def resolve(
        self,
        *,
        prompt: str,
        explicit_ids: set[str] | frozenset[str] = frozenset(),
        available_tools: set[str] | frozenset[str] | None = None,
        enabled_tools: set[str] | frozenset[str] | None = None,
    ) -> SkillResolution:
        selected: list[SkillDescriptor] = []
        diagnostics = [dict(item) for item in self.diagnostics]
        unknown_ids = set(explicit_ids) - set(self._skills)
        diagnostics.extend(
            {
                "skillId": skill_id,
                "status": "unknown",
                "error": "skill is not registered",
            }
            for skill_id in sorted(unknown_ids)
        )
        for skill in self._skills.values():
            if (
                skill.trust_level in _IMPLICIT_ACTIVATION_TRUST_LEVELS
                and skill.skill_id not in explicit_ids
                and skill.activation != "explicit"
            ):
                diagnostics.append({
                    "skillId": skill.skill_id,
                    "status": "denied",
                    "error": "skill requires explicit activation for its trust level",
                })
                continue
            if not _is_active(skill, prompt=prompt, explicit_ids=explicit_ids):
                continue
            allowed_tools: list[str] = []
            for name in skill.tools:
                if available_tools is not None and name not in available_tools:
                    diagnostics.append({
                        "skillId": skill.skill_id,
                        "tool": name,
                        "error": "skill tool is unavailable",
                    })
                    continue
                if enabled_tools is not None and name not in enabled_tools:
                    diagnostics.append({
                        "skillId": skill.skill_id,
                        "tool": name,
                        "error": "skill tool is disabled",
                    })
                    continue
                allowed_tools.append(name)
            try:
                materialized = skill.materialize()
            except (OSError, UnicodeError, ValueError) as error:
                diagnostics.append({
                    "skillId": skill.skill_id,
                    "path": skill.path,
                    "status": "unavailable",
                    "error": str(error),
                })
                continue
            selected.append(
                replace(
                    materialized,
                    tools=tuple(allowed_tools),
                )
            )
        return SkillResolution(
            skills=tuple(selected),
            prompt_sections=tuple(skill.body for skill in selected if skill.body),
            diagnostics=tuple(MappingProxyType(item) for item in diagnostics),
        )


def _parse_skill(path: Path, *, root_path: Path, source: str, trust_level: str) -> SkillDescriptor:
    resolved = path.resolve()
    if resolved.parent != root_path and resolved.parent.parent != root_path:
        raise ValueError("skill path escapes discovery root")
    try:
        if path.stat().st_size > _MAX_SKILL_BYTES:
            raise ValueError("skill exceeds 256 KiB")
    except OSError:
        raise
    front_matter = _read_skill_front_matter(path)
    skill_id = _required_text(front_matter, "id")
    version = _required_text(front_matter, "version")
    description = _required_text(front_matter, "description")
    activation = str(front_matter.get("activation", "explicit")).strip()
    if not _SKILL_ID.fullmatch(skill_id):
        raise ValueError("id must contain only lowercase letters, digits, '.', '_' or '-'")
    if activation not in _ACTIVATIONS:
        raise ValueError(f"unsupported activation: {activation}")
    tools = _tools(front_matter.get("tools", []))
    digest = _hash_skill(path)
    supporting_files = _discover_supporting_files(
        resolved.parent,
        root_path=root_path,
        trust_level=trust_level,
    )
    return SkillDescriptor(
        skill_id=skill_id,
        version=version,
        description=description,
        tools=tools,
        activation=activation,
        source=source,
        path=resolved.relative_to(root_path).as_posix(),
        content_hash=digest,
        body="",
        trust_level=trust_level,
        body_path=str(resolved),
        root_path=str(root_path),
        supporting_files=supporting_files,
    )


def _read_skill_front_matter(path: Path) -> dict[str, object]:
    """Read only the bounded YAML-like header during discovery."""

    lines: list[str] = []
    consumed = 0
    with path.open("rb") as stream:
        first = stream.readline(_MAX_SKILL_BYTES + 1)
        consumed += len(first)
        if not first:
            raise ValueError("SKILL.md must start with YAML front matter")
        if first.decode("utf-8").strip() != "---":
            raise ValueError("SKILL.md must start with YAML front matter")
        while True:
            line = stream.readline(_MAX_SKILL_BYTES + 1)
            if not line:
                raise ValueError("front matter is not terminated")
            consumed += len(line)
            if consumed > _MAX_SKILL_BYTES:
                raise ValueError("skill exceeds 256 KiB")
            decoded = line.decode("utf-8")
            if decoded.strip() == "---":
                break
            lines.append(decoded.rstrip("\r\n"))
    return _parse_front_matter(lines)


def _read_skill_body(path: Path) -> tuple[str, str]:
    """Read, decode and hash the complete document at activation time."""

    raw = path.read_bytes()
    if len(raw) > _MAX_SKILL_BYTES:
        raise ValueError("skill exceeds 256 KiB")
    text = raw.decode("utf-8")
    _, body = _split_front_matter(text)
    return body, hashlib.sha256(raw).hexdigest()


def _hash_skill(path: Path) -> str:
    _, digest = _hash_bounded_file(path, error_message="skill exceeds 256 KiB")
    return digest


def _hash_bounded_file(
    path: Path,
    *,
    error_message: str = "skill resource exceeds 256 KiB",
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            size += len(chunk)
            if size > _MAX_SKILL_BYTES:
                raise ValueError(error_message)
            digest.update(chunk)
    return size, digest.hexdigest()


def _discover_supporting_files(
    skill_directory: Path,
    *,
    root_path: Path,
    trust_level: str,
) -> tuple[SkillResourceDescriptor, ...]:
    resources: list[SkillResourceDescriptor] = []
    for directory_name in ("references", "assets"):
        directory = skill_directory / directory_name
        if not directory.exists():
            continue
        if directory.is_symlink():
            raise ValueError("symlinked skill resource directory is not supported")
        for candidate in sorted(directory.rglob("*")):
            if candidate.is_symlink() or candidate.parent.is_symlink():
                raise ValueError("symlinked skill resource is not supported")
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            try:
                relative = resolved.relative_to(root_path).as_posix()
            except ValueError as error:
                raise ValueError("skill resource path escapes discovery root") from error
            size, digest = _hash_bounded_file(resolved)
            resources.append(SkillResourceDescriptor(relative, size, digest, trust_level))
    return tuple(sorted(resources, key=lambda item: item.path))


def _validate_relative_path(value: str, label: str) -> None:
    path = Path(value)
    if not value.strip() or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must stay within the discovery root")


def _split_front_matter(raw: str) -> tuple[dict[str, object], str]:
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must start with YAML front matter")
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as error:
        raise ValueError("front matter is not terminated") from error
    return _parse_front_matter(lines[1:end]), "\n".join(lines[end + 1 :]).strip()


def _parse_front_matter(lines: list[str]) -> dict[str, object]:
    values: dict[str, object] = {}
    current_list: str | None = None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- "):
            if current_list is None:
                raise ValueError("list item has no field")
            current_value = values.setdefault(current_list, [])
            if not isinstance(current_value, list):
                raise ValueError(f"{current_list} must be a list")
            current_value.append(_scalar(line[2:].strip()))
            continue
        if ":" not in line:
            raise ValueError(f"invalid front matter line: {raw_line}")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        if key not in {"id", "version", "description", "tools", "activation"}:
            raise ValueError(f"unsupported front matter field: {key}")
        if key in values:
            raise ValueError(f"duplicate front matter field: {key}")
        if key == "tools":
            inline = raw_value.strip()
            if not inline:
                values[key] = []
                current_list = key
            elif inline.startswith("[") and inline.endswith("]"):
                values[key] = [_scalar(item) for item in _inline_items(inline[1:-1])]
                current_list = None
            else:
                values[key] = _scalar(inline)
                current_list = None
        else:
            values[key] = _scalar(raw_value.strip())
            current_list = None
    return values


def _scalar(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _inline_items(value: str) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for character in value:
        if character in {"'", '"'}:
            if quote == character:
                quote = None
            elif quote is None:
                quote = character
            current.append(character)
        elif character == "," and quote is None:
            item = "".join(current).strip()
            if item:
                items.append(item)
            current = []
        else:
            current.append(character)
    if quote is not None:
        raise ValueError("unterminated quoted front matter value")
    item = "".join(current).strip()
    if item:
        items.append(item)
    return items


def _required_text(values: dict[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value.strip()


def _tools(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("tools must be a list")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("tools must contain non-empty strings")
        name = item.strip()
        if name in cleaned:
            raise ValueError(f"duplicate tool reference: {name}")
        cleaned.append(name)
    return tuple(cleaned)


def _front_matter_metadata(path: Path) -> tuple[str | None, str | None]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(_FRONT_MATTER_DIAGNOSTIC_BYTES).decode("utf-8", errors="ignore")
    except OSError:
        return None, None
    id_match = re.search(r"(?m)^id:\s*([^\s#]+)", raw)
    version_match = re.search(r"(?m)^version:\s*([^\s#]+)", raw)
    return (
        id_match.group(1) if id_match else None,
        version_match.group(1) if version_match else None,
    )


def _is_active(skill: SkillDescriptor, *, prompt: str, explicit_ids: set[str] | frozenset[str]) -> bool:
    if skill.skill_id in explicit_ids:
        return True
    if skill.activation == "role_default":
        return True
    if skill.activation != "keyword":
        return False
    prompt_tokens = _keywords(prompt)
    description_tokens = _keywords(f"{skill.skill_id} {skill.description}")
    return bool(prompt_tokens & description_tokens)


def _keywords(text: str) -> set[str]:
    tokens: set[str] = set()
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]*|[\u4e00-\u9fff]{2,}", text.casefold()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            tokens.update(token[index : index + 2] for index in range(len(token) - 1))
        elif len(token) >= 2:
            tokens.add(token)
    return tokens
