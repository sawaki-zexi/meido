"""Pure complete-declaration accounting, shared by discovery and restore."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .tools import ToolDefinition

NEW_SCHEMA_BYTES = 32 * 1024
VISIBLE_SCHEMA_BYTES = 128 * 1024


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def declaration_bytes(definition: ToolDefinition) -> int:
    """UTF-8 bytes of full stable neutral declaration, not tokens or result text."""
    return len(json.dumps({"name": definition.name, "description": definition.description,
        "parameters": _json_value(definition.input_schema)}, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"))


@dataclass(frozen=True)
class BudgetSelection:
    selected: tuple[ToolDefinition, ...]
    skipped: tuple[ToolDefinition, ...]
    report: dict[str, int | bool | None]


def select_schema_budget(
    candidates: Sequence[ToolDefinition], *, baseline: Sequence[ToolDefinition],
    loaded: Sequence[ToolDefinition] = (), pending: Sequence[ToolDefinition] = (),
    new_limit: int | None = NEW_SCHEMA_BYTES, total_limit: int = VISIBLE_SCHEMA_BYTES,
) -> BudgetSelection:
    """Greedy whole-tool selection in caller order. Existing/pending names cost once.

    Restore passes new_limit=None: it is not a new search, but must obey the same
    current total bound. Required baseline is never silently trimmed.
    """
    baseline_by_name = {item.name: item for item in baseline}
    loaded_by_name = {item.name: item for item in loaded if item.name not in baseline_by_name}
    existing = {**baseline_by_name, **loaded_by_name}
    pending_by_name = {item.name: item for item in pending if item.name not in existing}
    baseline_bytes = sum(declaration_bytes(item) for item in baseline_by_name.values())
    loaded_bytes = sum(declaration_bytes(item) for item in loaded_by_name.values())
    pending_bytes = sum(declaration_bytes(item) for item in pending_by_name.values())
    total = baseline_bytes + loaded_bytes + pending_bytes
    cap = max(baseline_bytes, total_limit)
    seen = set(existing) | set(pending_by_name)
    selected: list[ToolDefinition] = []
    selected_names: set[str] = set()
    skipped: list[ToolDefinition] = []
    new_bytes = 0
    for item in candidates:
        if item.name in selected_names:
            continue
        if item.name in seen:
            selected.append(item)
            selected_names.add(item.name)
            continue
        size = declaration_bytes(item)
        if baseline_bytes >= total_limit or total + size > cap or (new_limit is not None and new_bytes + size > new_limit):
            skipped.append(item)
            continue
        selected.append(item)
        selected_names.add(item.name)
        seen.add(item.name)
        total += size
        new_bytes += size
    return BudgetSelection(tuple(selected), tuple(skipped), {
        "baseline_bytes": baseline_bytes, "loaded_bytes": loaded_bytes,
        "pending_bytes": pending_bytes, "new_bytes": new_bytes, "total_bytes": total,
        "new_limit_bytes": new_limit, "total_limit_bytes": total_limit,
        "effective_limit_bytes": cap, "remaining_bytes": max(0, cap - total),
        "remaining_new_bytes": max(0, new_limit - new_bytes) if new_limit is not None else None,
        "baseline_over_budget": baseline_bytes >= total_limit, "rejected_count": len(skipped),
    })
