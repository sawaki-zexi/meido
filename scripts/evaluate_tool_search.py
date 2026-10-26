"""Reproducible offline tool search quality + bounded growth measurement.

No service calls, credentials, private user logs, model tokens, or dependencies.
  python scripts/evaluate_tool_search.py --split dev
  python scripts/evaluate_tool_search.py --split all --benchmark --output report.json
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import random
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.agent_runtime import CapabilityRegistry, CancellationToken, ToolContext, ToolDefinition
from backend.app.agent_runtime.discovery import _keyword_score, _search_terms
from backend.app.agent_runtime.search_budget import declaration_bytes, select_schema_budget
from backend.app.agent_runtime.search_metadata import build_search_document

FIXTURE = ROOT / "tests/fixtures/tool_search/quality.json"
MODES = ("baseline", "metadata", "no_parameters", "no_namespace", "no_hint", "no_description")
RISKS = {"read-only": "read_only", "write": "mutating", "external-side-effect": "external"}


class CatalogTool:
    def __init__(self, definition):
        self.definition = definition

    async def execute(self, arguments, context, on_update=None):
        raise AssertionError("metadata-only fixture must never execute")


def load_fixture(path=FIXTURE):
    data = json.loads(path.read_text(encoding="utf-8"))
    definitions = [ToolDefinition(**item["definition"]) for item in data["definitions"]]
    return data, definitions


def admitted(definitions):
    """Use the real host admission and live-authorization seam, before ranking."""
    resolution = CapabilityRegistry([CatalogTool(item) for item in definitions]).resolve(
        role_id="evaluation", session_key="role:evaluation", run_id="offline",
        discovery_enabled=True, authorize_tool=lambda item, _: item.name != "forbidden_private")
    context = ToolContext("evaluation", "role:evaluation", "offline", CancellationToken())
    return [item for item in resolution.snapshot.candidates if item.name != "tool_search"
            and resolution.authorization_denial(item, context) is None]


def legacy_score(definition, terms):
    """Exact frozen 8a2d27d scorer; do not tune this reference with the proposal."""
    name = definition.name.lower()
    parts = [part for part in name.split("_") if part]
    hint, description = definition.search_hint.lower(), definition.description.lower()
    mcp = definition.source.startswith("mcp:")
    score = 0
    for term in terms:
        if term in parts:
            score += 12 if mcp else 10
        elif any(term in part or part in term for part in parts):
            score += 6 if mcp else 5
        elif term in name:
            score += 3
        if hint and term in hint:
            score += 4
        if term in description:
            score += 2
    return score


def rank(query, definitions, mode="metadata", documents=None):
    query = query.strip()
    if query in {item.name for item in definitions}:
        return [query]
    terms = _search_terms(query)
    ranked = []
    for item in definitions:
        if mode == "baseline":
            score = legacy_score(item, terms)
        else:
            doc = documents[item.name] if documents is not None else build_search_document(item)
            field = {"no_parameters": "parameters", "no_namespace": "namespace", "no_hint": "hint", "no_description": "description"}.get(mode)
            if field:
                doc = replace(doc, **{field: ""})
            score = _keyword_score(item, terms, doc)
        if score > 0:
            ranked.append((score, item.name))
    return [name for _, name in sorted(ranked, key=lambda row: (-row[0], row[1]))]


def evaluate(cases, definitions, mode):
    positive, empty = [], []
    forbidden_hits = wrong_namespace_hits = 0
    by_name = {item.name: item for item in definitions}
    for case in cases:
        risks = {RISKS[value] for value in case.get("allowed_risk", [])}
        eligible = [item for item in definitions if not risks or item.risk in risks]
        ranked = rank(case["query"], eligible, mode)
        result = ranked[:5]
        forbidden_hits += len(set(result) & set(case["forbidden"]))
        wrong_namespace_hits += sum(by_name[name].source in case["forbidden_namespaces"] for name in result)
        relevant = set(case["relevant"])
        if relevant:
            positive.append({"id": case["id"],
                "recall3": len(set(result[:3]) & relevant) / len(relevant),
                "recall5": len(set(result[:5]) & relevant) / len(relevant),
                "rr": next((1 / (index + 1) for index, name in enumerate(ranked) if name in relevant), 0),
                "rr5": next((1 / (index + 1) for index, name in enumerate(result) if name in relevant), 0)})
        else:
            empty.append(bool(result))
    return {"queries": len(cases), "positive_queries": len(positive),
        "recall_at_3": statistics.mean(item["recall3"] for item in positive) if positive else 0,
        "recall_at_5": statistics.mean(item["recall5"] for item in positive) if positive else 0,
        "mrr": statistics.mean(item["rr"] for item in positive) if positive else 0,
        "mrr_at_5": statistics.mean(item["rr5"] for item in positive) if positive else 0,
        "irrelevant_false_load_rate": statistics.mean(empty) if empty else 0,
        "forbidden_hits": forbidden_hits, "wrong_namespace_or_risk_hits": wrong_namespace_hits,
        "miss_ids": [item["id"] for item in positive if item["recall5"] < 1]}


def percentile(values, quantile):
    return sorted(values)[max(0, int(len(values) * quantile + .999) - 1)]


def timings(values):
    return {"p50_ms": round(statistics.median(values) * 1000, 3), "p95_ms": round(percentile(values, .95) * 1000, 3)}


def measure(definitions, seed):
    rng = random.Random(seed)
    matrix = []
    for size in (50, 500, 2000):
        corpus = [replace(rng.choice(definitions), name=f"fixture_{index:04d}") for index in range(size)]
        builds = []
        for _ in range(7):
            start = time.perf_counter()
            documents = {item.name: build_search_document(item) for item in corpus}
            builds.append(time.perf_counter() - start)
        searches, complete_searches = [], []
        for query in ("群笔记", "keystrokes", "财务invoice", "browser", "qzxwv987") * 4:
            start = time.perf_counter()
            rank(query, corpus, documents=documents)
            searches.append(time.perf_counter() - start)
            start = time.perf_counter()
            rank(query, corpus)
            complete_searches.append(time.perf_counter() - start)
        matrix.append({"definitions": size, "rebuild": timings(builds), "rank_existing_documents": timings(searches), "rebuild_and_rank": timings(complete_searches),
            "derived_document_bytes": sum(sum(len(getattr(doc, field).encode()) for field in ("name", "hint", "description", "parameters", "namespace")) for doc in documents.values())})
    # Depletion/growth is deliberately not part of fixed-corpus relevance metrics.
    # Each declaration is about 10KiB: broad repeated loads must reach the cap.
    corpus = [ToolDefinition(f"archive_{index:02d}", "archive", {"properties": {"note": {"description": "archive", "type": "string", "default": "x" * 10000}}}, exposure="deferred") for index in range(50)]
    loaded = []
    steps = []
    for index in range(20):
        visible = {item.name for item in loaded}
        names = rank("archive", [item for item in corpus if item.name not in visible])[:5]
        selection = select_schema_budget([next(item for item in corpus if item.name == name) for name in names], baseline=[], loaded=loaded)
        loaded.extend(selection.selected)
        steps.append({"search": index + 1, **selection.report, "active_names": [item.name for item in loaded],
                      "skipped_names": [item.name for item in selection.skipped]})
    repeated = select_schema_budget([loaded[0]], baseline=[], loaded=loaded)
    assert repeated.report["new_bytes"] == 0
    huge = ToolDefinition("huge", "schema", {"description": "x" * 1000000}, exposure="deferred")
    start = time.perf_counter()
    huge_doc = build_search_document(huge)
    huge_ms = (time.perf_counter() - start) * 1000
    huge_budget = select_schema_budget([huge], baseline=[])
    sizes = sorted(declaration_bytes(item) for item in definitions)
    return {"matrix": matrix, "growth": steps, "repeat_exact_new_bytes": repeated.report["new_bytes"],
        "all_loaded_keyword_results": rank("archive", []),
        "huge_schema": {"declaration_bytes": declaration_bytes(huge), "build_ms": round(huge_ms, 3),
            "diagnostics": huge_doc.diagnostics, "skipped_count": len(huge_budget.skipped)},
        "fixture_schema_sizes": {"min": min(sizes), "p50": statistics.median(sizes), "p95": percentile(sizes, .95), "max": max(sizes),
            "total": sum(sizes), "over_32k": sum(size > 32768 for size in sizes)}}


def run(split="all", benchmark=False):
    data, definitions = load_fixture()
    eligible = admitted(definitions)
    splits = ("dev", "holdout") if split == "all" else (split,)
    report = {"seed": data["seed"], "provenance": data["provenance"],
        "definitions": len(definitions), "authorized_definitions": len(eligible),
        "languages": dict(Counter(case["language"] for case in data["cases"])), "quality": {}}
    for group in splits:
        cases = [case for case in data["cases"] if case["split"] == group]
        results = {mode: evaluate(cases, eligible, mode) for mode in MODES}
        baseline, current = results["baseline"], results["metadata"]
        results["release_gate_passed"] = (current["forbidden_hits"] == 0
            and current["wrong_namespace_or_risk_hits"] == 0
            and current["recall_at_5"] >= baseline["recall_at_5"]
            and current["mrr"] >= baseline["mrr"]
            and current["irrelevant_false_load_rate"] <= baseline["irrelevant_false_load_rate"])
        report["quality"][group] = results
    if benchmark:
        report["performance_and_growth"] = measure(eligible, data["seed"])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "holdout", "all"), default="all")
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.split, args.benchmark)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if all(item["release_gate_passed"] for item in report["quality"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
