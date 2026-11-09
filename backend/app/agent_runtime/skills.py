from __future__ import annotations

import hashlib
import json
import re
import shutil
import yaml
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable


_SKILL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_ACTIVATIONS = frozenset({"explicit", "role_default", "keyword", "model"})
_TRUST_LEVELS = frozenset({"builtin", "project", "installed", "external"})
_IMPLICIT_ACTIVATION_TRUST_LEVELS = frozenset({"installed", "external"})
_TRUST_RANK = {"external": 0, "installed": 1, "project": 2, "builtin": 3}
_MAX_SKILL_BYTES = 256 * 1024
_FRONT_MATTER_DIAGNOSTIC_BYTES = 8 * 1024
_SOURCE_TRUST_LEVELS = {
    "builtin": "builtin",
    "role": "project",
    "project": "project",
    "shared": "project",
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
    required_bins: tuple[str, ...] = ()
    required_env: tuple[str, ...] = ()
    name: str = ""
    allow_implicit_invocation: bool | None = None
    ignored_metadata: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_bins", _dependencies(self.required_bins, environment=False))
        object.__setattr__(self, "required_env", _dependencies(self.required_env, environment=True))
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
            "requires": {"bins": list(self.required_bins), "env": list(self.required_env)},
            "name": self.name or self.skill_id,
            "allowImplicitInvocation": self.allow_implicit_invocation,
            "ignoredMetadata": list(self.ignored_metadata),
        }

    def materialize(self) -> SkillDescriptor:
        """Load and verify a file-backed Skill body when it is activated."""

        if self.body_path is None:
            return self
        path = Path(self.body_path)
        root = Path(self.root_path) if self.root_path is not None else path.parent
        if _path_has_link(root) or _path_has_link(path):
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
            if _path_has_link(resource_path):
                raise ValueError("symlinked skill resource is not supported")
            resolved_resource = resource_path.resolve()
            try:
                resolved_resource.relative_to(root.resolve())
            except ValueError as error:
                raise ValueError("skill resource path escapes discovery root") from error
            current_size, current_hash = _hash_bounded_file(resolved_resource)
            if current_size != resource.size or current_hash != resource.content_hash:
                raise ValueError("skill supporting file changed after discovery")
        _check_referenced_resources(body, self)
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
        all_skills: Iterable[SkillDescriptor] | None = None,
    ) -> None:
        self._skills: dict[str, SkillDescriptor] = {}
        for skill in skills:
            self.register(skill)
        self.diagnostics = tuple(dict(item) for item in diagnostics)
        self.all_skills = tuple(all_skills) if all_skills is not None else self.skills

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
        configured_root = Path(root).absolute()
        if _path_has_link(configured_root):
            return cls(diagnostics=({"path": str(configured_root), "source": source,
                "error": "linked skill discovery root is not supported"},))
        root_path = configured_root.resolve()
        if not root_path.exists() or not root_path.is_dir():
            return cls()
        candidates = (
            [root_path / "SKILL.md"]
            if root_path.name.lower() == "skills" and (root_path / "SKILL.md").is_file()
            else sorted(path / "SKILL.md" for path in root_path.iterdir() if path.is_dir() and not path.name.startswith('.'))
        )
        loaded: list[SkillDescriptor] = []
        diagnostics: list[dict[str, object]] = []
        for path in candidates:
            if _is_link(path) or _is_link(path.parent):
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
                        discovery_root=configured_root,
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
        # Root order breaks ties within one source (workspace before legacy).
        # A role's customization takes precedence over project and bundled text.
        priority = {"role": 0, "shared": 1, "project": 2, "installed": 3, "external": 4, "builtin": 5}
        unique: dict[str, SkillDescriptor] = {}
        for skill in sorted(loaded, key=lambda item: priority[item.source]):
            if skill.skill_id in unique:
                diagnostics.append({"skillId": skill.skill_id, "status": "shadowed", "source": skill.source,
                                    "version": skill.version, "selectedSource": unique[skill.skill_id].source})
            else:
                unique[skill.skill_id] = skill
        return cls(unique.values(), diagnostics=diagnostics, all_skills=loaded)

    def resolve(
        self,
        *,
        prompt: str,
        explicit_ids: set[str] | frozenset[str] = frozenset(),
        available_tools: set[str] | frozenset[str] | None = None,
        enabled_tools: set[str] | frozenset[str] | None = None,
        available_env: set[str] | frozenset[str] = frozenset(),
        dependency_check: Callable[[SkillDescriptor, str], str | None] | None = None,
        model_selection: bool = False,
    ) -> SkillResolution:
        explicit_ids = set(explicit_ids) | explicit_skill_ids(prompt)
        selected: list[SkillDescriptor] = []
        dependency_sections: list[str] = []
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
            if not (skill.skill_id in explicit_ids or skill.activation == "role_default"
                    or not model_selection and _is_active(skill, prompt=prompt, explicit_ids=explicit_ids)):
                continue
            reasons = {name: dependency_check(skill, name) if dependency_check is not None
                       else None if shutil.which(name) is not None else 'command is unavailable'
                       for name in skill.required_bins}
            missing_bins = [name for name, reason in reasons.items() if reason is not None]
            # Only names explicitly exposed by the host count. Shell's minimal
            # environment does not inherit arbitrary backend credentials.
            missing_env = [name for name in skill.required_env if name not in available_env]
            if missing_bins or missing_env:
                diagnostic: dict[str, object] = {
                    "skillId": skill.skill_id,
                    "status": "unavailable",
                    "error": "skill dependencies are unavailable",
                    "missingBins": missing_bins,
                    "missingEnv": missing_env,
                }
                diagnostics.append(diagnostic)
                if dependency_check is not None:
                    diagnostic['dependencyReasons'] = {name: reasons[name] for name in missing_bins}
                dependency_sections.append("Skill unavailable: " + json.dumps(diagnostic, ensure_ascii=False))
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
            prompt_sections=tuple(skill.body for skill in selected if skill.body) + tuple(dependency_sections),
            diagnostics=tuple(MappingProxyType({key: _freeze_metadata(value) for key, value in item.items()}) for item in diagnostics),
        )


def _parse_skill(path: Path, *, root_path: Path, source: str, trust_level: str, discovery_root: Path) -> SkillDescriptor:
    resolved = path.resolve()
    if resolved.parent != root_path and resolved.parent.parent != root_path:
        raise ValueError("skill path escapes discovery root")
    try:
        if path.stat().st_size > _MAX_SKILL_BYTES:
            raise ValueError("skill exceeds 256 KiB")
    except OSError:
        raise
    front_matter = _read_skill_front_matter(path)
    skill_id, version, name, activation, implicit, ignored = validate_skill_metadata(front_matter)
    description = _required_text(front_matter, "description")
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
        root_path=str(discovery_root),
        supporting_files=supporting_files,
        required_bins=_dependencies(front_matter.get("requires.bins", []), environment=False),
        required_env=_dependencies(front_matter.get("requires.env", []), environment=True),
        name=name, allow_implicit_invocation=implicit, ignored_metadata=ignored,
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
    if not body:
        raise ValueError('skill body is required')
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
    for directory_name in ("references", "assets", "scripts"):
        directory = skill_directory / directory_name
        if not directory.exists():
            continue
        if _is_link(directory):
            raise ValueError("symlinked skill resource directory is not supported")
        for candidate in sorted(directory.rglob("*")):
            if _is_link(candidate) or _is_link(candidate.parent):
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
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    if (not value.strip() or path.is_absolute() or ".." in path.parts or '\\' in value or ':' in value or '\0' in value
            or any(part.rstrip(' .') != part or part.split('.')[0].upper() in reserved for part in path.parts)):
        raise ValueError(f"{label} must stay within the discovery root")


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction() or path.is_file() and path.stat().st_nlink > 1


def _path_has_link(path: Path) -> bool:
    return any(_is_link(part) for part in (path, *path.parents))


def _freeze_metadata(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_metadata(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_metadata(item) for item in value)
    return value


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
    raw = "\n".join(lines)
    # SafeLoader rejects executable tags. Aliases are unnecessary in this small
    # interchange format and can expand a bounded header into unbounded data.
    try:
        if any(isinstance(token, yaml.tokens.AliasToken) for token in yaml.scan(raw)):
            raise ValueError("front matter aliases are not supported")
        class UniqueLoader(yaml.SafeLoader):
            pass
        def unique_mapping(loader, node):
            mapping = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node)
                if not isinstance(key, str) or key in mapping:
                    raise ValueError(f"duplicate front matter field: {key}")
                mapping[key] = loader.construct_object(value_node)
            return mapping
        UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
        parsed = yaml.load(raw, Loader=UniqueLoader)
    except yaml.YAMLError as error:
        raise ValueError("invalid YAML front matter") from error
    if not isinstance(parsed, dict):
        raise ValueError("front matter must be a mapping")
    for key in ("id", "name", "description", "activation"):
        if key in parsed and not isinstance(parsed[key], str):
            raise ValueError(f"{key} must be text")
    # Existing Meido versions were unquoted and could be numeric YAML scalars.
    if "version" in parsed and isinstance(parsed["version"], (int, float)) and not isinstance(parsed["version"], bool):
        parsed["version"] = str(parsed["version"])
    requires = parsed.get("requires")
    if requires is not None:
        if not isinstance(requires, dict) or set(requires) - {"bins", "env"}:
            raise ValueError("requires supports only bins and env")
        for key, value in requires.items():
            if f"requires.{key}" in parsed:
                raise ValueError(f"duplicate dependency field: {key}")
            parsed[f"requires.{key}"] = value
    policy = parsed.get("policy")
    if policy is not None:
        if not isinstance(policy, dict):
            raise ValueError("policy must be a mapping")
        if "allow_implicit_invocation" in policy:
            if "allow_implicit_invocation" in parsed:
                raise ValueError("duplicate invocation policy")
            parsed["allow_implicit_invocation"] = policy["allow_implicit_invocation"]
    return parsed


def validate_skill_metadata(values: dict[str, object]):
    name = str(values.get("name", values.get("id", ""))).strip()
    if not name or len(name) > 120:
        raise ValueError("name or id is required (at most 120 characters)")
    skill_id = values.get("id", name)
    if not isinstance(skill_id, str) or not _SKILL_ID.fullmatch(skill_id):
        raise ValueError("id/name must contain only lowercase letters, digits, '.', '_' or '-'")
    version = values.get("version", "1.0.0")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("version must be non-empty text")
    description = _required_text(values, "description")
    if len(description) > 2000:
        raise ValueError("description exceeds 2000 characters; put steps in the body")
    implicit = values.get("allow_implicit_invocation")
    if implicit is not None and not isinstance(implicit, bool):
        raise ValueError("allow_implicit_invocation must be boolean")
    activation = values.get("activation", "model" if "name" in values or implicit is True else "explicit")
    if activation not in _ACTIVATIONS:
        raise ValueError(f"unsupported activation: {activation}")
    if implicit is True and activation == "explicit" or implicit is False and activation in {"keyword", "role_default"}:
        raise ValueError("activation conflicts with allow_implicit_invocation")
    _tools(values.get("tools", []))
    _dependencies(values.get("requires.bins", []), environment=False)
    _dependencies(values.get("requires.env", []), environment=True)
    supported = {"id", "name", "version", "description", "tools", "activation", "allow_implicit_invocation", "policy", "requires", "requires.bins", "requires.env"}
    ignored = tuple(sorted(set(values) - supported))
    if isinstance(values.get("policy"), dict):
        ignored += tuple(f"policy.{key}" for key in values["policy"] if key != "allow_implicit_invocation")
    return skill_id, version.strip(), name, activation, implicit, ignored


def validate_skill_text(raw: str) -> dict[str, object]:
    if len(raw.encode("utf-8")) > _MAX_SKILL_BYTES:
        raise ValueError("skill exceeds 256 KiB")
    values, body = _split_front_matter(raw)
    skill_id, version, name, activation, implicit, ignored = validate_skill_metadata(values)
    if not body:
        raise ValueError("skill body is required")
    return {"id": skill_id, "version": version, "name": name, "activation": activation,
            "allowImplicitInvocation": implicit, "ignoredMetadata": list(ignored)}


def _check_referenced_resources(body: str, skill: SkillDescriptor) -> None:
    available = {item.path for item in skill.supporting_files}
    base = Path(skill.path).parent
    references = set(re.findall(r"(?<![\w/])((?:references|assets|scripts)/[\w./-]+\.[\w-]+)", body))
    missing = sorted(value for value in references if (base / value).as_posix() not in available)
    if missing:
        raise ValueError("missing supporting resources: " + ", ".join(missing[:16]))


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


def _dependencies(value: object, *, environment: bool) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 32:
        raise ValueError("skill dependencies must be a list of at most 32 names")
    pattern = r"[A-Za-z_][A-Za-z0-9_]{0,127}" if environment else r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,127}"
    if any(not isinstance(item, str) or re.fullmatch(pattern, item) is None for item in value):
        raise ValueError("skill dependencies must contain plain command or environment names")
    if len(set(value)) != len(value):
        raise ValueError("duplicate skill dependency")
    return tuple(value)


def _front_matter_metadata(path: Path) -> tuple[str | None, str | None]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(_FRONT_MATTER_DIAGNOSTIC_BYTES).decode("utf-8", errors="ignore")
    except OSError:
        return None, None
    id_match = re.search(r"(?m)^id:\s*([^\s#]+)", raw) or re.search(r"(?m)^name:\s*([^\s#]+)", raw)
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


def explicit_skill_ids(prompt: str) -> set[str]:
    """Only direct invocation text, never Markdown quotations or code samples."""
    lines = []
    fence = None
    for line in prompt.splitlines(keepends=True):
        marker = re.match(r' {0,3}(`{3,}|~{3,})([^\r\n]*)', line)
        if fence is not None:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                fence = None
            lines.append('\n')
        elif marker and (marker[1][0] != '`' or '`' not in marker[2]):
            fence = marker[1][0], len(marker[1])
            lines.append('\n')
        elif len(line.expandtabs(4)) - len(line.expandtabs(4).lstrip(' ')) >= 4:
            lines.append('\n')
        else:
            lines.append(line)
    direct = ''.join(lines)
    # CommonMark code spans close with a run of exactly the opening length.
    # Precompute matches so long descriptions with many runs remain linear.
    runs = list(re.finditer(r'`+', direct))
    following, latest = {}, {}
    for index in range(len(runs) - 1, -1, -1):
        length = len(runs[index][0])
        following[index] = latest.get(length)
        latest[length] = index
    pieces, cursor, index = [], 0, 0
    while index < len(runs):
        closing = following[index]
        if closing is None:
            index += 1
            continue
        pieces.append(direct[cursor:runs[index].start()])
        pieces.append(' ')
        cursor, index = runs[closing].end(), closing + 1
    pieces.append(direct[cursor:])
    direct = ''.join(pieces)
    direct = re.sub(r'(?m)^\s*>.*$', '', direct)
    direct = re.sub(r'"[^"\n]*"|“[^”\n]*”|「[^」\n]*」|\'[\w\W]*?\'', '', direct)
    return set(re.findall(r'(?<![\w$])\$([a-z0-9][a-z0-9._-]*)', direct))


def _keywords(text: str) -> set[str]:
    tokens: set[str] = set()
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]*|[\u4e00-\u9fff]{2,}", text.casefold()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            tokens.update(token[index : index + 2] for index in range(len(token) - 1))
        elif len(token) >= 2:
            tokens.add(token)
    return tokens
