"""Offline bounded search metadata and declaration accounting regressions."""
import asyncio
import json
from dataclasses import replace

from backend.app.agent_runtime import CapabilityRegistry, CancellationToken, ToolContext, ToolDefinition
from backend.app.agent_runtime.discovery import _keyword_score, _match_reasons, _search_terms
from backend.app.agent_runtime.search_metadata import build_search_document, render_namespace_map
from backend.app.agent_runtime.search_budget import declaration_bytes, select_schema_budget


def definition(name="opaque", **kwargs):
    return ToolDefinition(name, "Operation", exposure="deferred", **kwargs)


def test_metadata_whitelist_cjk_namespace_and_diagnostics():
    tool = definition(input_schema={
        "properties": {"invoiceNumber": {"description": "发票编号", "default": "secret-default", "examples": ["secret-example"]}},
        "$ref": "https://invalid.example/secret-reference",
    }, source="mcp:accounts", namespace_summary="财务核算")
    doc = build_search_document(tool)
    assert "发票编号" in doc.parameters and "invoicenumber" in doc.parameters
    assert "财务核算" in doc.namespace and "mcp:accounts" in doc.namespace
    assert "secret" not in repr(doc)
    assert _keyword_score(tool, _search_terms("发票编号")) > 0
    assert any(reason.startswith("参数:") for reason in _match_reasons(tool, _search_terms("发票编号")))
    assert any(reason.startswith("namespace:") for reason in _match_reasons(tool, _search_terms("财务核算")))


def test_metadata_is_bounded_for_cycles_depth_width_and_utf8():
    cyclic = {"description": "根"}
    cyclic["items"] = cyclic
    doc = build_search_document(definition(input_schema=cyclic))
    assert "schema_cycle" in doc.diagnostics
    deep = {"description": "bottom"}
    for _ in range(20):
        deep = {"items": deep}
    assert "schema_depth_limit" in build_search_document(definition(input_schema=deep)).diagnostics
    wide = {"properties": {f"p{i}": {"description": "字段"} for i in range(10000)}}
    wide_doc = build_search_document(definition(input_schema=wide))
    assert "schema_node_limit" in wide_doc.diagnostics
    big = build_search_document(definition(input_schema={"description": "中" * 10000}))
    assert sum(len(getattr(big, field).encode()) for field in ("name", "hint", "description", "parameters", "namespace")) <= 8192
    assert "search_text_truncated" in big.diagnostics
    expanding_case = build_search_document(definition(input_schema={"description": "İ" * 3000}))
    assert "search_text_truncated" in expanding_case.diagnostics
    assert len(expanding_case.parameters.encode()) <= 8192


def test_namespace_map_uses_source_identity_and_complete_render_budget():
    tools = [definition(f"t{i}", source=f"mcp:s{i:03}", namespace_summary="相同别名" + "中" * 300) for i in range(100)]
    rendered = render_namespace_map(tools)
    assert len(rendered.encode()) <= 4096
    assert "omitted" in rendered
    entries = [json.loads(line) for line in rendered.splitlines() if line.startswith("{")]
    assert all(len(item["summary"].encode()) <= 256 for item in entries)
    assert entries[0]["namespace"] == "mcp:s000"
    assert all(item["discovery_route"] == "tool_search" for item in entries)
    assert render_namespace_map(list(reversed(tools))) == rendered
    assert render_namespace_map([]) == ""


def test_declaration_bytes_measure_full_neutral_schema_not_search_text():
    tool = definition(input_schema={"properties": {"内容": {"type": "string"}}})
    actual_schema = tool.as_provider_schema()['function']
    assert 'description' in actual_schema['parameters']['properties']
    expected = json.dumps(actual_schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert declaration_bytes(tool) == len(expected)
    raw_bytes = len(json.dumps({'name': tool.name, 'description': tool.description, 'parameters': tool.input_schema},
                              ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
    assert not select_schema_budget([tool], baseline=[], new_limit=raw_bytes).selected


def test_budget_counts_pending_deduplicates_and_keeps_whole_schemas():
    base = replace(definition("required"), exposure="direct")
    a = definition("a", input_schema={"description": "a" * 80})
    b = definition("b", input_schema={"description": "b" * 80})
    limit = declaration_bytes(base) + declaration_bytes(a) + declaration_bytes(b) - 1
    first = select_schema_budget([a, b], baseline=[base], total_limit=limit)
    assert [item.name for item in first.selected] == ["a"]
    assert [item.name for item in first.skipped] == ["b"]
    assert first.report["new_bytes"] == declaration_bytes(a)
    second = select_schema_budget([a, b], baseline=[base], pending=[a], total_limit=limit)
    assert [item.name for item in second.selected] == ["a"]
    assert second.report["new_bytes"] == 0
    assert second.report["pending_bytes"] == declaration_bytes(a)
    assert a.input_schema["description"] == "a" * 80


def test_baseline_exception_and_per_search_limit_never_allow_extra():
    huge = replace(definition("required", input_schema={"description": "x" * 131072}), exposure="direct")
    small = definition("small")
    result = select_schema_budget([small], baseline=[huge])
    assert not result.selected and result.report["baseline_over_budget"]
    assert result.report["total_bytes"] == declaration_bytes(huge)
    assert result.report["remaining_bytes"] == 0
    result = select_schema_budget([definition("huge", input_schema={"description": "x" * 32768}), small], baseline=[])
    assert [item.name for item in result.selected] == ["small"]


class Tool:
    def __init__(self, item):
        self.definition = item

    async def execute(self, arguments, context, on_update=None):
        raise AssertionError("metadata-only fixture must never execute")


def resolution(items, **kwargs):
    return CapabilityRegistry([Tool(item) for item in items]).resolve(role_id="r", session_key="role:r", run_id="run", discovery_enabled=True, **kwargs)


def search(cap, query, call="search"):
    return asyncio.run(cap.get("tool_search").execute({"query": query}, ToolContext("r", "role:r", "run", CancellationToken(), call)))


def test_search_filters_before_metadata_explanations_or_budget_skips():
    allowed = definition("approved", input_schema={"description": "发票编号"}, source="mcp:public", namespace_summary="财务")
    hidden = replace(allowed, name="hidden", exposure="hidden", source="mcp:private")
    denied = replace(allowed, name="denied", approval="deny")
    revoked = replace(allowed, name="revoked")
    cap = resolution([allowed, hidden, denied, revoked], authorize_tool=lambda d, _: d.name != "revoked")
    result = search(cap, "发票编号")
    assert json.loads(result.content)["unlocked"] == ["approved"]
    assert all(word not in result.content for word in ("hidden", "denied", "revoked", "private"))


def test_search_pending_reservation_output_and_committed_bytes_agree():
    items = [definition(f"tool{i}", input_schema={"description": "z" * 30000}) for i in range(6)]
    cap = resolution(items)
    first = json.loads(search(cap, "select:tool0,tool1", "a").content)
    assert first["unlocked"] == ["tool0"]
    assert first["skipped_due_to_budget"] == ["tool1"]
    assert first["budget"]["new_bytes"] == declaration_bytes(items[0])
    for index in range(1, 4):
        result = json.loads(search(cap, f"select:tool{index}", str(index)).content)
        assert result["unlocked"] == [f"tool{index}"]
    rejected = json.loads(search(cap, "select:tool4", "fifth").content)
    assert rejected["unlocked"] == []
    assert rejected["skipped_due_to_budget"] == ["tool4"]
    cap._discovery.pending.pop("a")  # host cancellation releases only this reservation
    retried = json.loads(search(cap, "select:tool4", "retry").content)
    assert retried["unlocked"] == ["tool4"]
    assert retried["budget"]["total_bytes"] <= 131072


def test_keyword_limit_counts_loadable_matches_after_skipping_large_schemas():
    large = definition("archive_big", input_schema={"description": "z" * 32768})
    small = definition("archive_small")
    cap = resolution([large, small, definition("archive_tiny")])
    result = asyncio.run(cap.get("tool_search").execute(
        {"query": "archive", "top_k": 1},
        ToolContext("r", "role:r", "run", CancellationToken(), "search"),
    ))
    payload = json.loads(result.content)
    assert payload["unlocked"] == ["archive_small"]
    assert payload["skipped_due_to_budget"] == ["archive_big"]
    assert payload["budget"]["new_bytes"] == declaration_bytes(small)
    assert payload["budget"]["rejected_count"] == 1


def test_budget_diagnostics_do_not_crowd_out_a_loadable_keyword_match():
    oversized = [definition(f"archive_{index:03}_" + "x" * 45,
        input_schema={"description": "z" * 32768}) for index in range(300)]
    small = definition("archive_zzz_small")
    cap = resolution([*oversized, small])
    result = asyncio.run(cap.get("tool_search").execute(
        {"query": "archive", "top_k": 1},
        ToolContext("r", "role:r", "run", CancellationToken(), "search"),
    ))
    payload = json.loads(result.content)
    assert payload["unlocked"] == [small.name]
    assert payload["matched"][0]["name"] == small.name
    assert payload["budget"]["rejected_count"] == 300
    assert payload["budget"]["new_bytes"] == declaration_bytes(small)
    assert payload["skipped_names_truncated"] is True
    assert len(result.content) <= 16000


def test_request_bound_search_does_not_reintroduce_a_removed_current_candidate():
    from backend.app.agent_runtime.discovery import ServiceGrant

    item = definition("removed", source="mcp:service")
    cap = resolution([item], service_grants=(ServiceGrant("grant", "service", "r", frozenset({"read_only"})),))
    issued = cap.snapshot.snapshot_id
    context = ToolContext("r", "role:r", "run", CancellationToken(), "remove", issued)
    cap.stage_service_removal(context, grant_id="grant", parent_snapshot_id=issued)
    cap.publish_revision(cap.pending_revision("remove"))
    # The old request binding remains valid for its already-issued calls, not for
    # publishing a definition that is absent from the current registry.
    binding = {"capability_binding": cap} if "capability_binding" in ToolContext.__dataclass_fields__ else {}
    result = asyncio.run(cap.get("tool_search").execute({"query": "select:removed"},
        ToolContext("r", "role:r", "run", CancellationToken(), "find", issued, **binding)))
    assert json.loads(result.content)["unlocked"] == []
    assert cap._discovery.pending["find"] == ()
