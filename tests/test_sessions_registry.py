"""Tests for the Session Parameter Registry (``web/sessions/registry.py``)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "web"))

_WEB = Path(__file__).resolve().parent.parent / "web"
#: The app the drift guards parse — the one ``streamlit_app.py`` shims, i.e.
#: the one actually deployed. V5 is superseded and only has to keep *saving*
#: (see ``test_v5_gather_still_satisfies_the_registry``).
_APP = _WEB / "streamlit_app_v6.py"
_APP_V5 = _WEB / "streamlit_app_v5.py"

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
    "overlay_replay": False,
    "overlay_heatmap": True,
    "overlay_heatmap_scope": "family",
    "filter_spec": [["exclude_activities", ["Fix Bug"]],
                    ["rename_map", [["A", "B"]]]],
    "csv_columns": ["case", "act", "ts", "role", "res"],
    "scenario_strategy": "data-driven",
    "scenario_group_name": "MyScenarios",
    "scenario_max_loop_iterations": 3,
    "scenario_decision_tree_max_depth": 4,
    "simulation_mode": "compare",
    "simulation_scenarios": ["v1_Quick"],
    "simulation_a": "v1_Quick",
    "simulation_b": "v2_Analyze",
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


def test_decomposition_sizes_round_trip():
    """A decomposition spec's hand-set sizes + toggles — and the "auto"
    shape-fit sentinel — survive collect + save/load unchanged, so a resumed
    project mines with exactly the decomposition it was saved with."""
    from pm4py_ucm.objects.ucm.conversion.decomposition import AUTO_DIM

    cases = [
        ([["balance_ratio", 0.35], ["max_leaves_per_map", 12],
          ["min_leaves_to_decompose", 5], ["on_loop", False],
          ["on_root_sequence", True]],
         {"max_leaves_per_map": 12, "min_leaves_to_decompose": 5,
          "balance_ratio": 0.35, "on_loop": False}),
        ([["max_leaves_per_map", AUTO_DIM],
          ["min_leaves_to_decompose", AUTO_DIM], ["balance_ratio", 0.2]],
         {"max_leaves_per_map": AUTO_DIM,
          "min_leaves_to_decompose": AUTO_DIM}),
    ]
    for dec, expect in cases:
        cfg = collect({**FULL_VALUES, "decomposition": dec})
        back = loads(dumps(ProjectDoc(
            log=LogRef("upload", "l.xes", "xes", "h"), config=cfg)))
        got = {k: v for k, v in (tuple(p)
                                 for p in back.config["decomposition"])}
        for k, v in expect.items():
            assert got[k] == v, (k, got.get(k), v)


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

    app = _APP.read_text(encoding="utf-8")
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
    """Source text of the named top-level functions in the app (static)."""
    import ast

    app = _APP.read_text(encoding="utf-8")
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


def _load_sticky_helpers():
    """Exec the app's four sticky helpers against a fake ``st`` whose
    ``session_state`` is a plain dict — they depend on nothing else, so this
    exercises the real logic without a Streamlit runtime."""
    import types

    src = _app_func_sources("_sticky", "_sticky_save",
                            "_sticky_get", "_sticky_seed")
    st = types.SimpleNamespace(session_state={})
    ns = {"st": st}
    exec("\n\n".join(src[n] for n in                      # noqa: S102
         ("_sticky", "_sticky_save", "_sticky_get", "_sticky_seed")), ns)
    return ns, st


def test_family_min_cases_survives_saving_from_another_view():
    """Regression (Issue 3, SAVE side): a Family-view number_input is a
    main-area widget, so Streamlit drops its ``session_state`` key when another
    view is active. Saving must read the durable ``_keep::`` mirror via
    ``_sticky_get``, not the (garbage-collected) widget key — else the saved
    project captures the default, not the user's value."""
    ns, st = _load_sticky_helpers()
    ss = st.session_state
    # User is on the Family view and sets Min cases to 50; the widget mirrors it.
    ss["cfg_family_min_cases"] = 50
    ns["_sticky_save"]("cfg_family_min_cases")
    # Navigate away → Streamlit garbage-collects the un-rendered widget key.
    del ss["cfg_family_min_cases"]
    # The raw key is gone, but the save gather still sees 50, not the default.
    assert ns["_sticky_get"]("cfg_family_min_cases", 10) == 50
    # And the gather is actually wired to _sticky_get for the family sizes.
    app = _APP.read_text(encoding="utf-8")
    for key in ("cfg_family_min_cases", "cfg_family_max_values",
                "cfg_family_bins"):
        assert f'_sticky_get("{key}"' in app, key


def test_family_attr_restores_before_its_view_is_opened():
    """Regression (Issue 3, RESTORE side): a project loads with a non-Family
    active view, so seeding the raw ``family_attr1`` widget key would be
    garbage-collected before the Family view is ever opened. Seeding the
    durable ``_keep::`` mirror via ``_sticky_seed`` makes the first render of
    the selectbox pick up the restored value."""
    ns, st = _load_sticky_helpers()
    ss = st.session_state
    # Restore seeds the mirror (the raw key is deliberately NOT set).
    ns["_sticky_seed"]("family_attr1", "Channel")
    assert "family_attr1" not in ss
    # First render of the selectbox: factory default is the first attribute.
    opts = ["Broker", "Channel", "Online"]
    assert ns["_sticky"]("family_attr1", lambda: opts[0], options=opts) \
        == "Channel"
    # And the restore path is actually wired to _sticky_seed for both attrs.
    restore = _app_func_sources("_apply_project_config")["_apply_project_config"]
    assert '_sticky_seed("family_attr1"' in restore
    assert '_sticky_seed("family_attr2"' in restore


def test_v5_gather_still_satisfies_the_registry():
    """The superseded V5 app must keep *saving*. ``collect`` refuses a gather
    that is missing a registered id, so a Param added for V6 without a
    corresponding (default-valued) entry in V5 would make V5's Save button
    raise at runtime. V5 need not restore the newer params — only round-trip
    them — so this is a gather-only guard."""
    import ast

    tree = ast.parse(_APP_V5.read_text(encoding="utf-8"))
    keys = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_proj_values"
                        for t in node.targets)
                and isinstance(node.value, ast.Dict)):
            keys = {k.value for k in node.value.keys
                    if isinstance(k, ast.Constant)}
            break
    assert keys is not None, "could not find V5's _proj_values gather dict"
    assert keys == set(param_ids())


def test_simulation_mode_is_persisted_as_an_identifier_not_a_label():
    """The rule this codebase has learned twice: a persisted option value is an
    identifier, never the words on screen. The Highlight radio's options must
    be ``_SIM_MODES``' KEYS, with the labels applied through ``format_func`` —
    otherwise a saved project stores "Compare A vs B" and breaks the moment the
    wording changes."""
    import ast
    import re

    src = _APP.read_text(encoding="utf-8")
    modes = None
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_SIM_MODES"
                        for t in node.targets)):
            modes = ast.literal_eval(node.value)
            break
    assert modes == {"coverage": "Coverage", "compare": "Compare A vs B"}
    # The registry default is one of the identifiers, not one of the labels.
    from sessions.registry import defaults
    assert defaults()["simulation_mode"] in modes

    radio = re.search(r'st\.radio\(\s*"Highlight".*?\n\s*\)',
                      src, re.S)
    assert radio, "could not find the Highlight radio"
    radio = radio.group(0)
    assert "options=list(_SIM_MODES)" in radio, radio
    assert "format_func=" in radio, radio
    # No label may appear as a *value* anywhere the mode is compared or stored.
    for label in modes.values():
        assert f'== "{label}"' not in src, label


def test_simulation_selection_round_trips_through_a_project():
    """Save → load → save keeps the highlight the app showed."""
    cfg = collect({**FULL_VALUES, "simulation_mode": "compare",
                   "simulation_scenarios": ["v1_Quick", "v3_Escalate"],
                   "simulation_a": "v1_Quick", "simulation_b": "v2_Analyze"})
    back = loads(dumps(ProjectDoc(
        log=LogRef("upload", "l.xes", "xes", "h"), config=cfg)))
    assert back.config["simulation_mode"] == "compare"
    assert back.config["simulation_scenarios"] == ["v1_Quick", "v3_Escalate"]
    assert back.config["simulation_a"] == "v1_Quick"
    assert back.config["simulation_b"] == "v2_Analyze"


def test_simulation_restores_before_the_scenarios_view_is_opened():
    """The Simulation section sits at the bottom of the Scenarios view, which is
    usually not the active view when a project loads — so its widget keys would
    be garbage collected before ever rendering. The restore must go through the
    durable ``_keep::`` mirror (``_sticky_seed``), and the save through
    ``_sticky_get``, exactly like the Family attributes."""
    ns, st = _load_sticky_helpers()

    ns["_sticky_seed"]("sim_mode", "compare")
    ns["_sticky_seed"]("sim_a", "v2_Analyze")
    assert "sim_mode" not in st.session_state
    # First render of the Highlight radio picks up the restored identifier
    # rather than the "coverage" default.
    assert ns["_sticky"]("sim_mode", lambda: "coverage",
                         options=("coverage", "compare")) == "compare"
    assert ns["_sticky"]("sim_a", lambda: "v1_Quick",
                         options=["v1_Quick", "v2_Analyze"]) == "v2_Analyze"
    # And the wiring is really there on both sides.
    restore = _app_func_sources(
        "_apply_project_config")["_apply_project_config"]
    for key in ("sim_mode", "sim_cov_picks", "sim_a", "sim_b"):
        assert f'_sticky_seed("{key}"' in restore, key
        assert f'_sticky_get("{key}"' in _APP.read_text(encoding="utf-8"), key


def test_restored_scenario_that_no_longer_exists_is_dropped():
    """A resumed project can name scenarios a re-mine no longer produces.
    A/B clamp to the default through ``_sticky``'s ``options``; the coverage
    multiselect must NOT — an empty selection there is a deliberate choice, so
    the app filters unknown names by hand instead."""
    ns, st = _load_sticky_helpers()
    names = ["v1_Quick", "v2_Analyze"]

    # A/B: an unknown name falls back to the factory default.
    ns["_sticky_seed"]("sim_a", "v9_Gone")
    assert ns["_sticky"]("sim_a", lambda: names[0], options=names) == "v1_Quick"

    # Coverage picks: unknown names are dropped, known ones kept.
    ns["_sticky_seed"]("sim_cov_picks", ["v9_Gone", "v2_Analyze"])
    ns["_sticky"]("sim_cov_picks", lambda: names[:1])
    st.session_state["sim_cov_picks"] = [
        n for n in st.session_state["sim_cov_picks"] if n in names]
    assert st.session_state["sim_cov_picks"] == ["v2_Analyze"]

    # And an emptied selection stays empty — it must not spring back to the
    # default on the next rerun (which `options=` would have done).
    st.session_state["sim_cov_picks"] = []
    ns["_sticky_save"]("sim_cov_picks")
    del st.session_state["sim_cov_picks"]
    ns["_sticky"]("sim_cov_picks", lambda: names[:1])
    assert st.session_state["sim_cov_picks"] == []


# The pre-V6 deprecation checks that used to live here moved to
# tests/test_release_consistency.py, where they derive the current app
# from the shim instead of hard-coding V6 -- so they follow the next
# major app version rather than guarding a file nobody runs.


def test_progress_callbacks_into_cached_functions_only_update_labels():
    """A callback handed to an ``@st.cache_data`` function may only mutate its
    status container's label — never create a child element on it.

    Streamlit records every element a cached function touches so it can replay
    them on a cache hit. A child element created on a block that was made
    *outside* the function replays into a block that no longer exists, and the
    hit raises ``CacheReplayClosureError``. Mutating the container's own label
    replays safely.

    This is written down in ``_ProgressUI``'s docstring, and was then broken
    anyway by a later per-cell progress bar on the family grid: it worked on
    the first render and failed on every cache hit afterwards. Hence a test.
    """
    import ast

    src = _APP.read_text(encoding="utf-8")
    tree = ast.parse(src)

    # Names of the @st.cache_data functions that accept a progress handle.
    cached = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and any("cache_data" in ast.unparse(d)
                        for d in node.decorator_list)):
            params = [a.arg for a in node.args.args + node.args.kwonlyargs]
            if any(p.lstrip("_") in ("progress", "status", "bar")
                   for p in params):
                cached.add(node.name)
    assert cached, "expected some cached functions to take a progress handle"

    # Callback names passed to those functions as _progress=/_status=.
    passed = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in cached):
            continue
        for kw in node.keywords:
            if kw.arg in ("_progress", "_status") and isinstance(kw.value, ast.Name):
                passed.add(kw.value.id)

    # Every locally-defined callback among them may only call `.update(...)`.
    ALLOWED = {"update"}
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name in passed):
            continue
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)):
                continue
            if call.func.attr in ALLOWED:
                continue
            # Ignore plain builtins on locals (e.g. "".join, dict.get).
            if call.func.attr in ("join", "get", "format", "append", "keys"):
                continue
            offenders.append(
                f"line {call.lineno}: {node.name}() calls "
                f".{call.func.attr}(...) on a container")
    assert not offenders, (
        "a progress callback passed into a cached function touches something "
        "other than its status label:\n  " + "\n  ".join(offenders)
        + "\nOnly `.update(label=...)` replays safely on a cache hit; see "
          "_ProgressUI's docstring.")


def test_cache_audit_finds_nothing_in_the_deployed_app():
    """Run ``tools/cache_audit.py`` over V6 and require a clean report.

    The audit encodes the two Streamlit cache faults this project has actually
    shipped — a cached function drawing into a block created outside it, and a
    call site letting a cache-key parameter fall back to its default. Both are
    invisible to the rest of the suite: the first only fails on the *second*
    call, and the second never fails at all, it just returns the wrong data.

    Running it here means the audit cannot rot, and a new cached function that
    reintroduces either fault fails CI rather than a user's session.
    """
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, str(root / "tools" / "cache_audit.py"), str(_APP)],
        capture_output=True, text=True, cwd=str(root))
    assert proc.returncode == 0, (
        "tools/cache_audit.py reported a cache hazard:\n" + proc.stdout
        + proc.stderr)
