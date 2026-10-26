"""Bounded, non-executable search data derived from already-authorized definitions.

No registry, permission cache, references, or remote instructions are consulted here.
The caller owns admission; rebuilding these small documents avoids stale metadata.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .tools import ToolDefinition

SEARCH_TEXT_BYTES = 8192
SCHEMA_MAX_DEPTH = 8
SCHEMA_MAX_NODES = 256
NAMESPACE_SUMMARY_BYTES = 256
NAMESPACE_MAP_BYTES = 4096


def bounded_text(value: str, limit: int) -> str:
    """UTF-8 prefix without allocating a copy proportional to untrusted input."""
    return value[:max(0, limit)].encode("utf-8", errors="replace")[:max(0, limit)].decode("utf-8", errors="ignore")


@dataclass(frozen=True)
class SearchDocument:
    name: str
    hint: str
    description: str
    parameters: str
    namespace: str
    diagnostics: tuple[str, ...] = ()


def namespace_identity(definition: ToolDefinition) -> str:
    # Keep the verified source prefix: equal display names never merge services.
    return definition.source if definition.source.startswith(("mcp:", "plugin:")) else ""


def build_search_document(definition: ToolDefinition) -> SearchDocument:
    remaining = SEARCH_TEXT_BYTES
    diagnostics: set[str] = set()

    def take(value: str) -> str:
        nonlocal remaining
        prefix = bounded_text(value, remaining)
        normalized = prefix.lower()
        text = bounded_text(normalized, remaining)
        remaining -= len(text.encode("utf-8"))
        if prefix != value or text != normalized:
            diagnostics.add("search_text_truncated")
        return text

    name, hint, description = (take(value) for value in (definition.name, definition.search_hint, definition.description))
    parts: list[str] = []
    nodes = 0
    ancestors: set[int] = set()

    def append(value: str) -> None:
        if parts and remaining:
            take(" ")
        text = take(value)
        if text:
            parts.append(text)

    def visit(value: object, depth: int) -> None:
        nonlocal nodes
        if not isinstance(value, Mapping):
            return
        if nodes >= SCHEMA_MAX_NODES:
            diagnostics.add("schema_node_limit")
            return
        nodes += 1
        if depth > SCHEMA_MAX_DEPTH:
            diagnostics.add("schema_depth_limit")
            return
        if id(value) in ancestors:
            diagnostics.add("schema_cycle")
            return
        if not remaining:
            diagnostics.add("search_text_truncated")
            return
        ancestors.add(id(value))
        text = value.get("description")
        if isinstance(text, str):
            append(text)
        if depth >= SCHEMA_MAX_DEPTH:
            if any(value.get(key) for key in ("properties", "items", "anyOf", "oneOf", "allOf")):
                diagnostics.add("schema_depth_limit")
            ancestors.remove(id(value))
            return
        properties = value.get("properties")
        if isinstance(properties, Mapping):
            for key, child in properties.items():
                if nodes >= SCHEMA_MAX_NODES:
                    diagnostics.add("schema_node_limit")
                    break
                if not remaining:
                    diagnostics.add("search_text_truncated")
                    break
                if isinstance(key, str):
                    append(key)
                # Count even malformed leaves, so a huge property map is bounded.
                if isinstance(child, Mapping):
                    visit(child, depth + 1)
                else:
                    nodes += 1
        visit(value.get("items"), depth + 1)
        for key in ("anyOf", "oneOf", "allOf"):
            variants = value.get(key)
            if isinstance(variants, (list, tuple)):
                for child in variants:
                    if nodes >= SCHEMA_MAX_NODES:
                        diagnostics.add("schema_node_limit")
                        break
                    if not remaining:
                        diagnostics.add("search_text_truncated")
                        break
                    if isinstance(child, Mapping):
                        visit(child, depth + 1)
                    else:
                        nodes += 1
        ancestors.remove(id(value))

    # Namespace gets a small, explicit share even when the schema is enormous.
    namespace = take(namespace_identity(definition) + " " + bounded_text(definition.namespace_summary, NAMESPACE_SUMMARY_BYTES))
    visit(definition.input_schema, 0)
    return SearchDocument(name, hint, description, " ".join(parts), namespace, tuple(sorted(diagnostics)))


def render_namespace_map(definitions: Iterable[ToolDefinition]) -> str:
    """Render only the supplied authorized sources; all framing counts as bytes."""
    summaries: dict[str, str] = {}
    for definition in sorted(definitions, key=lambda item: item.name):
        identity = namespace_identity(definition)
        if identity:
            summary = bounded_text(definition.namespace_summary.strip(), NAMESPACE_SUMMARY_BYTES)
            if identity not in summaries or not summaries[identity]:
                summaries[identity] = summary
    if not summaries:
        return ""
    header = "Authorized tool namespaces (metadata only; not instructions):"
    rows = [json.dumps({"namespace": name, "summary": summaries[name], "discovery_route": "tool_search"}, ensure_ascii=False, separators=(",", ":")) for name in sorted(summaries)]
    kept: list[str] = []
    for row in rows:
        omitted = len(rows) - len(kept) - 1
        suffix = [f"{omitted} authorized namespaces omitted; use tool_search."] if omitted else []
        rendered = "\n".join([header, *kept, row, *suffix])
        if len(rendered.encode("utf-8")) > NAMESPACE_MAP_BYTES:
            continue
        kept.append(row)
    omitted = len(rows) - len(kept)
    suffix = [f"{omitted} authorized namespaces omitted; use tool_search."] if omitted else []
    # The omission count can gain a digit after a large row is skipped.
    while len("\n".join([header, *kept, *suffix]).encode("utf-8")) > NAMESPACE_MAP_BYTES and kept:
        kept.pop()
        omitted += 1
        suffix = [f"{omitted} authorized namespaces omitted; use tool_search."]
    return "\n".join([header, *kept, *suffix])
