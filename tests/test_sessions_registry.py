"""Tests for the Session Parameter Registry (``web/sessions/registry.py``)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "web"))

from sessions import (  # noqa: E402
    LogRef,
    ProjectDoc,
    collect,
    dumps,
    loads,
    param_ids,
)

# A full, representative gather — one value per registered id. Deliberately
# spelled out (not derived from the registry) so a change to the persisted set
# is a conscious edit here too — the registry change-guard below.
FULL_VALUES = {
    "noise_threshold": 0.3,
    "min_support": 0.1,
    "notation": "bpmn",
    "decomposition": [["on_root_sequence", True]],
    "resource_attribute": "org:resource",
    "overlay_nodes": ["frequency", "sojourn_median_time"],
    "overlay_edges": ["percentage", "frequency"],
    "overlay_heatmap": True,
    "overlay_heatmap_scope": "family",
    "filter_spec": [["exclude_activities", ["Fix Bug"]],
                    ["rename_map", [["A", "B"]]]],
    "csv_columns": ["case", "act", "ts", "role", "res"],
    "scenario_strategy": "data-driven",
    "scenario_group_name": "MyScenarios",
    "scenario_max_loop_iterations": 3,
    "scenario_decision_tree_max_depth": 4,
    "family_attrs": ["case:region"],
    "family_min_cases": 20,
    "family_max_values": 6,
    "family_bins": 5,
    "family_include_values": [["case:region", ["North", "South"]]],
    "family_dedup": True,
    "compare_a": "North",
    "compare_b": "South",
    "active_view": "Compare",
}


def test_full_values_match_registry_exactly():
    # If this fails, the registry changed: update FULL_VALUES (and the app's
    # gather) so persistence stays in lock-step. This is the change-guard.
    assert set(FULL_VALUES) == set(param_ids())


def test_collect_round_trips_through_a_project_file():
    cfg = collect(FULL_VALUES)
    doc = ProjectDoc(
        log=LogRef("upload", "log.xes", "xes", "h"), config=cfg)
    back = loads(dumps(doc))
    assert back.config == cfg
    # Tuples were normalised to lists on the way in; the round-trip is stable.
    assert back.config["filter_spec"] == [
        ["exclude_activities", ["Fix Bug"]], ["rename_map", [["A", "B"]]]]


def test_collect_rejects_unknown_id():
    with pytest.raises(KeyError):
        collect({**FULL_VALUES, "surprise_setting": 1})


def test_collect_rejects_missing_id():
    partial = dict(FULL_VALUES)
    partial.pop("noise_threshold")
    with pytest.raises(KeyError):
        collect(partial)


def test_app_gather_matches_registry():
    """CI drift guard: the app's ``_proj_values`` gather dict must have exactly
    the registered ids — so adding a Param without gathering it (or vice
    versa) fails here rather than silently dropping a setting on save. Parsed
    statically (no execution), so Streamlit isn't needed."""
    import ast

    app = (Path(__file__).resolve().parent.parent
           / "web" / "streamlit_app_v5.py").read_text(encoding="utf-8")
    tree = ast.parse(app)
    keys = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_proj_values"
                        for t in node.targets)
                and isinstance(node.value, ast.Dict)):
            keys = {k.value for k in node.value.keys
                    if isinstance(k, ast.Constant)}
            break
    assert keys is not None, "could not find the _proj_values gather dict"
    assert keys == set(param_ids())
