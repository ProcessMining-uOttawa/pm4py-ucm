"""Tests for the Python code exporter (``web/sessions/codegen.py``).

Two layers, matching ``docs/code_export.md`` §8:

* **Unit** — the emitter is deterministic and Streamlit-free, so these run
  headless with no pm4py: the emitted script compiles, carries the session's
  config, and only contains the sections the session used.
* **Golden** — the killer property: run the emitted script and assert its
  ``.jucm`` is byte-identical (modulo the exporter's wall-clock timestamp) to a
  direct public-API pipeline. Needs pm4py + a bundled sample, so it is skipped
  when either is unavailable.
"""
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "web"))

from sessions import (  # noqa: E402
    LogRef,
    ProjectDoc,
    generate_notebook,
    generate_script,
)
from sessions.codegen import GENERATOR_VERSION  # noqa: E402
from sessions.registry import defaults, param_ids  # noqa: E402


def _doc(config=None, **log_over):
    log_kw = dict(source="sample", name="Sample.zip", kind="zip", sha256="")
    log_kw.update(log_over)
    return ProjectDoc(log=LogRef(**log_kw), config=config or {})


# ---------------------------------------------------------------------------
# Unit — the emitted script is valid, deterministic, and config-faithful.
# ---------------------------------------------------------------------------

def test_emitted_script_compiles():
    src = generate_script(_doc({"noise_threshold": 0.3, "notation": "bpmn"}))
    compile(src, "generated.py", "exec")  # raises SyntaxError on failure
    assert src.endswith("\n")


def test_deterministic_same_input_same_output():
    doc = _doc({"noise_threshold": 0.25, "overlay_nodes": ["frequency"]})
    assert generate_script(doc) == generate_script(doc)


def test_config_values_are_emitted():
    src = generate_script(_doc({
        "noise_threshold": 0.42,
        "min_support": 0.1,
        "notation": "bpmn",
        "resource_attribute": "org:role",
        "overlay_nodes": ["frequency", "median_time"],
        "overlay_edges": ["percentage"],
    }))
    assert "NOISE_THRESHOLD = 0.42" in src
    assert "MIN_SUPPORT = 0.1" in src
    assert 'NOTATION = \'bpmn\'' in src
    assert "OVERLAY_NODES = ['frequency', 'median_time']" in src
    assert "OVERLAY_EDGES = ['percentage']" in src


def test_decomposition_off_vs_dict():
    off = generate_script(_doc({"decomposition": "off"}))
    assert 'DECOMPOSITION = "off"' in off
    dic = generate_script(_doc({
        "decomposition": [["max_leaves_per_map", 10], ["on_loop", True]]}))
    assert "DECOMPOSITION = {'max_leaves_per_map': 10, 'on_loop': True}" in dic


def test_filter_spec_and_rename_emitted():
    src = generate_script(_doc({"filter_spec": [
        ["activity_ranks", [1, 5]],
        ["rename_map", [["Register", "Intake"], ["Escalate", "Assess"]]],
    ]}))
    assert "'activity_ranks': [1, 5]" in src
    assert "'rename_map': [['Register', 'Intake'], ['Escalate', 'Assess']]" \
        in src


def test_csv_columns_emitted():
    cols = ["case", "act", "ts", "role", "res"]
    src = generate_script(_doc({}, kind="csv", csv_columns=cols))
    assert f"CSV_COLUMNS = {cols!r}" in src
    assert "LOG_KIND = 'csv'" in src


def test_family_auto_detected_from_attrs():
    without = generate_script(_doc({"family_attrs": []}))
    assert "def run_family" not in without
    with_attrs = generate_script(_doc({"family_attrs": ["country", "age"]}))
    assert "def run_family" in with_attrs
    assert "FAMILY_ATTRS = ['country', 'age']" in with_attrs
    assert "run_family(log)" in with_attrs


def test_family_include_override_flag():
    # Auto-detect can be overridden either way.
    forced_off = generate_script(
        _doc({"family_attrs": ["country"]}), include_family=False)
    assert "def run_family" not in forced_off
    forced_on = generate_script(_doc({"family_attrs": []}), include_family=True)
    assert "def run_family" in forced_on


def test_scenarios_opt_in():
    off = generate_script(_doc({}))
    assert "def run_scenarios" not in off
    on = generate_script(_doc({"scenario_strategy": "data-driven"}),
                         include_scenarios=True)
    assert "def run_scenarios" in on
    assert "SCENARIO_STRATEGY = 'data-driven'" in on
    assert "run_scenarios(log)" in on


def test_all_sections_compile_together():
    src = generate_script(
        _doc({"family_attrs": ["country"],
              "scenario_strategy": "variant"}),
        include_scenarios=True, include_family=True)
    compile(src, "full.py", "exec")
    for fn in ("def run_model", "def run_scenarios", "def run_family",
               "def run(", "def read_log", "def apply_log_filters"):
        assert fn in src
    # The model is also exported as a standalone SVG (used by pinned-model
    # dashboard widgets and offered as a download in the app).
    assert 'model.svg' in src and "write_model_svg" in src


def test_notebook_is_a_tutorial_not_a_single_run_call():
    nb = generate_notebook(_doc({"family_attrs": ["country"]}),
                           include_scenarios=True)
    data = json.loads(nb)
    assert data["nbformat"] == 4
    code_cells = [c for c in data["cells"] if c["cell_type"] == "code"]
    assert code_cells
    joined = "\n".join("".join(c["source"]) for c in code_cells)
    compile(joined, "notebook.py", "exec")  # the whole notebook is valid Python
    # It is a tutorial: each stage is invoked where it's defined, not bundled
    # into a single `run()` at the end (the .py keeps that; the notebook spreads
    # it out for interactivity).
    assert not joined.strip().endswith("run()")
    assert "def run(" not in joined and "__main__" not in joined
    for call in ("ucm = run_model(log)", "run_scenarios(log)",
                 "run_family(log)"):
        assert call in joined, call
    # Intermediate results are shown inline, not just written to disk.
    for shown in ("log.head()", "Image(str(OUT_DIR",
                  'pd.read_csv(OUT_DIR / "variants.csv")'):
        assert shown in joined, shown
    # Interleaving: the load call appears before the family definition.
    assert joined.index("log = read_log(") < joined.index("def run_family")


def test_dashboards_emitted_when_the_project_carries_them():
    from sessions.dashboards import wrap_registry
    reg = {"active": "d1", "dashboards": [
        {"id": "d1", "name": "Ops", "filters": [],
         "specs": [{"id": "w1", "metric": "duration", "agg": "avg",
                    "viz": "kpi"}]}]}
    doc = ProjectDoc(log=LogRef("sample", "L.zip", "zip", ""),
                     config={}, dashboards=wrap_registry(reg))
    # Auto-detected from the presence of dashboards.
    src = generate_script(doc)
    assert "def run_dashboards" in src
    assert "run_dashboards(log, ucm)" in src
    assert "DASHBOARDS = " in src
    assert "'name': 'Ops'" in src and "build_fact_table" in src
    # All dashboards go into ONE file with a switcher (the registry is passed,
    # not one dashboard's specs), rendered read-only.
    assert 'OUT_DIR / "dashboards.html"' in src
    assert "dashboards=DASHBOARDS" in src
    assert "dashboard_{i" not in src  # not one file per dashboard
    # The mined UCM's SVG is embedded only when a dashboard pins the model.
    assert 'w.get("viz") == "model"' in src and "needs_model" in src
    assert "model_svg=model_svg" in src and "renders=renders" in src
    compile(src, "dash.py", "exec")
    # And absent when the project has none, or when explicitly excluded.
    assert "def run_dashboards" not in generate_script(_doc({}))
    assert "def run_dashboards" not in generate_script(
        doc, include_dashboards=False)


def test_generator_version_matches_package():
    text = (_ROOT / "pm4py_ucm" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    assert m, "could not find __version__ in pm4py_ucm/__init__.py"
    assert GENERATOR_VERSION == m.group(1), (
        "codegen.GENERATOR_VERSION is out of sync with the package version")


def test_every_registry_id_is_handled_or_intentionally_ignored():
    """Drift guard: a new registry param must be wired into codegen (or added
    to the ignore set with a reason), so the exporter can't silently omit it."""
    # UI-state params that do not shape the emitted pipeline. (The heat-map
    # settings — overlay_heatmap / overlay_heatmap_scope — ARE emitted now, so
    # the exported render calls reproduce the heat-map; they are not ignored.)
    ignored = {
        "compare_a", "compare_b",  # Compare-view selection, no pipeline effect
        "active_view",             # which tab was open
        "csv_columns",             # carried on the LogRef, emitted from there
    }
    src = generate_script(
        _doc({"family_attrs": ["country"]}), include_scenarios=True)
    for pid in param_ids():
        if pid in ignored:
            continue
        token = pid.upper()
        assert token in src, (
            f"registry param {pid!r} is not handled by codegen; wire it in "
            f"or add it to the ignore set with a reason")


def test_defaults_covers_all_ids():
    assert set(defaults()) == set(param_ids())


# ---------------------------------------------------------------------------
# Golden — the emitted script reproduces the public-API pipeline byte-for-byte.
# ---------------------------------------------------------------------------

_SAMPLE = _ROOT / "web" / "samples" / "IssueTrackerSyntheticLog.zip"


def _strip_timestamps(jucm: bytes) -> str:
    """Neutralise the exporter's wall-clock ``created`` / ``modified`` stamps —
    the only intentionally nondeterministic fields in a fresh export."""
    s = jucm.decode("utf-8")
    s = re.sub(r'created="[^"]*"', 'created="X"', s)
    s = re.sub(r'modified="[^"]*"', 'modified="X"', s)
    return s


@pytest.mark.skipif(not _SAMPLE.exists(), reason="bundled sample not present")
def test_generated_script_reproduces_reference_jucm(tmp_path):
    pm4py = pytest.importorskip("pm4py")
    import pm4py_ucm

    config = {
        "noise_threshold": 0.2, "notation": "ucm",
        "resource_attribute": "org:resource",
        "overlay_nodes": ["frequency"], "overlay_edges": ["frequency"],
    }
    doc = _doc(config, name="IssueTrackerSyntheticLog.zip")
    out = tmp_path / "gen_out"
    script = tmp_path / "pipeline.py"
    script.write_text(generate_script(doc, out_dir=str(out)), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(script), str(_SAMPLE)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"generated script failed:\n{proc.stderr}"
    gen_jucm = (out / "model.jucm").read_bytes()

    # Reference: the same public-API pipeline the script emits.
    with zipfile.ZipFile(_SAMPLE) as zf:
        member = [n for n in zf.namelist() if n.lower().endswith(".xes")][0]
        xes = tmp_path / "log.xes"
        xes.write_bytes(zf.read(member))
        log = pm4py.read_xes(str(xes))
    tree = pm4py.discover_process_tree_inductive(log, noise_threshold=0.2)
    params = {"process_tree": tree, "resource_attribute": "org:resource",
              "resource_parameters": {"min_support": 0.0}}
    ucm = pm4py_ucm.discover_ucm_inductive(
        log, parameters=params, decomposition="off")
    pm4py_ucm.annotate_performance(
        ucm, log, node_metrics=["frequency"], edge_metrics=["frequency"])
    ref = tmp_path / "ref.jucm"
    pm4py_ucm.write_ucm(ucm, str(ref))

    assert _strip_timestamps(gen_jucm) == _strip_timestamps(ref.read_bytes())


def test_emitted_transform_covers_all_app_filter_keys():
    """The emitted ``apply_log_filters`` must handle every ``filter_spec`` key
    the app's ``_apply_log_filters`` reads, so a new app filter can't be silently
    dropped from generated scripts (as ``duration_pct`` — the cycle-time filter —
    nearly was). This is the §6a drift guard: the transform is inlined, so it has
    to track the app by test rather than by import."""
    app = (_ROOT / "web" / "streamlit_app_v5.py").read_text(encoding="utf-8")
    m = re.search(r"\ndef _apply_log_filters\(.*?(?=\ndef )", app, re.S)
    assert m, "could not locate _apply_log_filters in the app"
    app_keys = set(re.findall(r'spec\.get\(\s*["\']([a-z_]+)["\']', m.group(0)))
    assert "duration_pct" in app_keys, "sanity: the app reads the cycle-time key"
    gen = generate_script(_doc({}))
    missing = sorted(k for k in app_keys if k not in gen)
    assert not missing, (
        f"emitted apply_log_filters is missing app filter key(s): {missing} — "
        "mirror the app's _apply_log_filters in web/sessions/codegen.py")


def test_heatmap_config_emitted_and_wired_into_render_calls():
    src = generate_script(
        _doc({"overlay_heatmap": True, "overlay_heatmap_scope": "family",
              "overlay_nodes": ["frequency"], "family_attrs": ["c"]}),
        include_family=True)
    assert "OVERLAY_HEATMAP = True" in src
    assert "OVERLAY_HEATMAP_SCOPE = 'family'" in src
    # The heat-map reaches the rendered artifacts: the model PNG, the family
    # grid PNG, and the exported HTML report's embedded images.
    assert "parameters=model_heat_params())" in src        # model PNG
    assert "parameters=model_heat_params(family))" in src  # family grid PNG
    assert "heat=report_heat(family))" in src              # family report HTML


def test_emitted_heat_helpers_reflect_overlay_state():
    pytest.importorskip("pandas")
    pytest.importorskip("pm4py")  # the emitted script imports it at module load

    off = {"__name__": "t"}
    exec(compile(generate_script(_doc({})), "g.py", "exec"), off)
    assert off["model_heat_params"]() == {}     # heat-map off → no render params
    assert off["report_heat"](None) is None

    on = {"__name__": "t"}
    exec(compile(generate_script(_doc({
        "overlay_heatmap": True, "overlay_heatmap_scope": "global",
        "overlay_nodes": ["median_time"], "overlay_edges": ["percentage"],
    })), "g.py", "exec"), on)
    hp = on["model_heat_params"]()             # global scope needs no family
    assert hp["heatmap_node"] == ("median_time", True)   # a *_time metric
    assert hp["heatmap_edge"] == ("percentage", False)
    assert hp["heatmap_global"] is True
    assert hp["node_span"] is None and hp["edge_span"] is None  # not "family"


def test_emitted_apply_log_filters_applies_duration_pct():
    """Exec the emitted helpers and confirm ``apply_log_filters`` really applies
    the cycle-time band (keeps the fastest cases by end-to-end duration)."""
    pd = pytest.importorskip("pandas")
    pytest.importorskip("pm4py")  # the emitted script imports it at module load
    ns = {"__name__": "codegen_test"}  # not "__main__" → the CLI block is skipped
    exec(compile(generate_script(_doc({})), "gen.py", "exec"), ns)

    base = pd.Timestamp("2026-01-01", tz="UTC")
    rows = []
    for i in range(1, 11):  # 10 cases, durations 1..10 days
        cid = f"c{i:02d}"
        rows.append((cid, "A", base))
        rows.append((cid, "B", base + pd.Timedelta(days=i)))
    df = pd.DataFrame(
        rows, columns=["case:concept:name", "concept:name", "time:timestamp"])

    fastest30 = ns["apply_log_filters"](df, {"duration_pct": (0, 30)})
    assert sorted(fastest30["case:concept:name"].unique()) == \
        ["c01", "c02", "c03"]
    slowest10 = ns["apply_log_filters"](df, {"duration_pct": (90, 100)})
    assert sorted(slowest10["case:concept:name"].unique()) == ["c10"]
