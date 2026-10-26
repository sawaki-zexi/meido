"""The checked-in fixture is offline, labeled, split, and independently scored."""
from collections import Counter
from dataclasses import replace

from scripts.evaluate_tool_search import admitted, evaluate, load_fixture, rank
from backend.app.agent_runtime.search_budget import select_schema_budget


def test_quality_fixture_and_untuned_holdout_release_gate():
    data, definitions = load_fixture()
    counts = Counter(item["language"] for item in data["cases"])
    assert len(data["cases"]) >= 60
    assert counts["zh"] >= 20 and counts["mixed"] >= 15 and counts["en"] >= 15 and counts["irrelevant"] >= 10
    holdout = [item for item in data["cases"] if item["split"] == "holdout"]
    assert len(holdout) >= 20
    assert all("relevant" in item and "forbidden" in item for item in data["cases"])
    authorized = admitted(definitions)
    baseline, proposal = [evaluate(holdout, authorized, mode) for mode in ("baseline", "metadata")]
    assert proposal["forbidden_hits"] == proposal["wrong_namespace_or_risk_hits"] == 0
    assert proposal["recall_at_5"] >= baseline["recall_at_5"]
    assert proposal["mrr"] >= baseline["mrr"]
    assert proposal["irrelevant_false_load_rate"] <= baseline["irrelevant_false_load_rate"]


def test_real_browser_parameter_only_intent_and_namespace_ablation():
    data, definitions = load_fixture()
    authorized = admitted(definitions)
    assert rank("keystrokes", authorized, "baseline") == []
    assert rank("keystrokes", authorized, "metadata")[0] == "agent_browser_type"
    assert set(rank("财务核算", authorized, "metadata")[:2]) == {"mcp_accounts__lookup", "mcp_accounts__save"}
    assert rank("财务核算", authorized, "baseline") == []


def test_twenty_broad_searches_plateau_without_unloading_or_repeat_charge():
    _, definitions = load_fixture()
    base = admitted(definitions)[0]
    corpus = [replace(base, name=f"entry_{i}", description="entry", input_schema={"description": "x" * 10000}) for i in range(50)]
    loaded = []
    for _ in range(20):
        names = {item.name for item in loaded}
        selection = select_schema_budget([item for item in corpus if item.name not in names][:5], baseline=[], loaded=loaded)
        loaded.extend(selection.selected)
        assert selection.report["total_bytes"] <= 131072
    assert len(loaded) == 13
    assert selection.skipped and not selection.selected
    repeat = select_schema_budget(loaded[:1], baseline=[], loaded=loaded)
    assert repeat.report["new_bytes"] == 0
