"""Compile only portable role identity; runtime context remains host-owned."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Role


_MACRO = re.compile(r"\{\{([^{}]+)\}\}")
_SUPPORTED_MACROS = frozenset({"char", "user"})


@dataclass(frozen=True)
class RolePromptDiagnostic:
    field: str
    macro: str


@dataclass(frozen=True)
class CompiledRolePrompt:
    text: str
    diagnostics: tuple[RolePromptDiagnostic, ...]


def is_supported_role_macro(value: str) -> bool:
    match = _MACRO.fullmatch(value)
    return match is not None and match[1].strip().lower() in _SUPPORTED_MACROS


def compile_role_prompt(role: Role) -> CompiledRolePrompt:
    """Expand known macros once, without inspecting provenance or runtime data.

    Meido's nickname names the user, unlike Shiori's role-alias field. Values
    inserted by substitution stay literal; this is not a template evaluator.
    """
    profile = role.profile
    values = {"char": role.name.strip(), "user": profile.nickname.strip() or "用户"}
    diagnostics: dict[tuple[str, str], RolePromptDiagnostic] = {}
    blocks = []
    fields = (
        ("name", role.name, "你正在扮演 {}。"),
        ("description", role.description, "简介：{}"),
        ("profile", profile.profile, "角色设定：{}"),
        ("personality", profile.personality, "性格：{}"),
        ("behaviorRules", profile.behaviorRules, "行为规则：{}"),
        ("responseConstraints", profile.responseConstraints, "回复约束：{}"),
        ("nickname", profile.nickname, "称呼用户：{}"),
    )
    for field, raw, label in fields:
        source = raw.strip()
        if not source:
            continue

        def replace(match: re.Match[str]) -> str:
            name = match[1].strip().lower()
            if name in values:
                return values[name]
            macro = match[0]
            diagnostics.setdefault((field, macro), RolePromptDiagnostic(field, macro))
            return macro

        blocks.append(label.format(_MACRO.sub(replace, source)))
    return CompiledRolePrompt("\n\n".join(blocks), tuple(diagnostics.values()))
