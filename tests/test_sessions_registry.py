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


def _app_func_sources(*names):
    """Source text of the named top-level functions in the V5 app (static)."""
    import ast

    app = (Path(__file__).resolve().parent.parent
           / "web" / "streamlit_app_v5.py").read_text(encoding="utf-8")
    tree = ast.parse(app)
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            out[node.name] = ast.get_source_segment(app, node) or ""
    return out


def test_apply_project_config_restores_every_registry_param():
    """CI drift guard for the RESTORE side. The gather guard above proves every
    registered param is *saved*; this proves every one is at least *referenced*
    where a loaded project is applied (``_apply_project_config``, plus
    ``_apply_filter_spec_to_state`` for the keys inside ``filter_spec``) — so a
    Param added to the registry can't be saved yet silently never restored.
    Static parse; Streamlit isn't needed."""
    src = _app_func_sources("_apply_project_config",
                            "_apply_filter_spec_to_state")
    assert "_apply_project_config" in src, "could not find _apply_project_config"
    joined = "\n".join(src.values())
    missing = [p for p in param_ids()
               if f'"{p}"' not in joined and f"'{p}'" not in joined]
    assert not missing, (
        f"the restore path never references registry param(s): {missing} — "
        "seed them in _apply_project_config so a saved project restores them")


def test_filter_spec_subkeys_restore_matches_the_transform():
    """The keys inside ``filter_spec`` are not registry params, so the guard
    above misses them. The transform ``_apply_log_filters`` is their source of
    truth (it reads each with ``spec.get("key")``); assert the reverse-map
    ``_apply_filter_spec_to_state`` references every one, so a new pre-mining
    filter (e.g. the cycle-time ``duration_pct``) can't be saved-but-not-
    restored."""
    import re

    src = _app_func_sources("_apply_log_filters",
                            "_apply_filter_spec_to_state")
    transform = src.get("_apply_log_filters", "")
    restore = src.get("_apply_filter_spec_to_state", "")
    assert transform and restore, "could not find the filter functions"
    keys = set(re.findall(r'spec\.get\(\s*["\']([a-z_]+)["\']', transform))
    assert "duration_pct" in keys, "sanity: the transform reads the cycle key"
    missing = sorted(k for k in keys if f'"{k}"' not in restore)
    assert not missing, (
        f"_apply_filter_spec_to_state does not restore filter key(s): "
        f"{missing} — reverse-map them so a saved filter resumes")
