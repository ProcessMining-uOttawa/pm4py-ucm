"""Generate a runnable Python pipeline from a saved project session.

Streamlit-free and **deterministic** (see ``docs/code_export.md``): walk the
session parameter registry + :class:`~web.sessions.schema.LogRef` and emit plain
Python over the public ``pm4py_ucm`` API. A GUI analysis becomes an automatable,
version-controllable, reproducible script.

Because a project stores only *inputs* (the registry is config-only and every
derived artifact recomputes), the emitted script is a **faithful replay** —
running it reproduces the same ``.jucm`` / reports the app produced. No LLM is
involved: this is a template emitter, not a generator.

Public entry points:

* :func:`generate_script` — a ``.py`` script (string).
* :func:`generate_notebook` — the same pipeline as a Jupyter ``.ipynb`` (JSON
  string); doubles as a personalised tutorial.

Both accept a :class:`~web.sessions.schema.ProjectDoc`. The pre-mining transform
(rename + log filters) and the log loader are **inlined** into the emitted
script so it is self-contained and portable — it needs only ``pandas``,
``pm4py`` and ``pm4py_ucm`` on the path, nothing from this repo.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .dashboards import unwrap_registry
from .registry import defaults
from .schema import ProjectDoc

# The generator's own version marker, surfaced in the script header. Kept in
# lock-step with the package version by a test.
GENERATOR_VERSION = "0.7.3"


# ---------------------------------------------------------------------------
# Inlined runtime helpers (log loading + pre-mining transform).
#
# These mirror the web app's `_log_and_tree` loader, `_apply_rename`,
# `_apply_log_filters` and `_coerce_str_object` so the generated script
# reproduces the app's mining input exactly. Kept as a plain string constant
# (NOT an f-string — it contains braces and %-formats) that references the
# config constants emitted just above it in the script.
# ---------------------------------------------------------------------------
_HELPERS = '''\
def _coerce_str_object(df):
    """Coerce pandas ``string`` (arrow-backed) columns to plain ``object`` so a
    CSV import behaves like an XES one everywhere (variant filters, mining)."""
    if isinstance(df, pd.DataFrame):
        cols = list(df.select_dtypes(include=["string"]).columns)
        if cols:
            df = df.astype({c: object for c in cols})
    return df


def read_log(log_path, kind=LOG_KIND, csv_columns=CSV_COLUMNS):
    """Read the event log as a pm4py DataFrame (mirrors the web app loader)."""
    log_path = str(log_path)
    if kind == "csv":
        if not csv_columns:
            raise ValueError("CSV_COLUMNS mapping is required for a CSV log.")
        case_col, activity_col, ts_col, role_col, resource_col = csv_columns
        df = pd.read_csv(log_path, low_memory=False)
        df = pm4py.format_dataframe(
            df, case_id=case_col, activity_key=activity_col,
            timestamp_key=ts_col)
        renames = {}
        if role_col and role_col != "org:role":
            renames[role_col] = "org:role"
        if resource_col and resource_col != "org:resource":
            renames[resource_col] = "org:resource"
        if renames:
            df = df.rename(columns=renames)
        return _coerce_str_object(df)
    data = Path(log_path).read_bytes()
    if kind == "zip" or (len(data) >= 2 and data[:2] == b"PK"):
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".xes")]
            if not names:
                raise ValueError("No .xes member found inside the zip log.")
            with tempfile.TemporaryDirectory() as td:
                target = Path(td) / "log.xes"
                target.write_bytes(zf.read(names[0]))
                return pm4py.read_xes(str(target))
    return pm4py.read_xes(log_path)


def apply_rename(df, rename_map):
    """Return ``df`` with ``concept:name`` relabelled (unmapped names kept)."""
    if not rename_map:
        return df
    rename_map = dict(rename_map)
    return df.assign(**{
        "concept:name": df["concept:name"].map(
            lambda v: rename_map.get(v, v))})


def apply_log_filters(log, filter_spec):
    """Apply the pre-mining rename + log filters (mirrors the web app)."""
    if not filter_spec:
        return log
    spec = dict(filter_spec)
    df = log if isinstance(log, pd.DataFrame) else pm4py.convert_to_dataframe(log)
    rename = spec.get("rename_map")
    if rename:
        df = apply_rename(df, dict(rename))
    ranks = spec.get("activity_ranks")
    exclude = spec.get("exclude_activities")
    if ranks or exclude:
        counts = df["concept:name"].value_counts()  # descending frequency
        keep = set(counts.index)
        if ranks:
            lo, hi = ranks
            keep = set(counts.index[lo - 1:hi])
        keep -= set(exclude or ())
        df = pm4py.filter_event_attribute_values(
            df, "concept:name", keep, level="event", retain=True)
    dpct = spec.get("duration_pct")
    if dpct:
        lo_pct, hi_pct = dpct       # percentile band; 0 = fastest, 100 = slowest
        ts = pd.to_datetime(df["time:timestamp"], utc=True, errors="coerce")
        dur = ts.groupby(df["case:concept:name"]).agg(lambda s: s.max() - s.min())
        dur = dur.sort_values()     # ascending: fastest case first
        n = len(dur)
        if n:
            lo_i = int(lo_pct / 100.0 * n)          # floor
            hi_i = int(hi_pct / 100.0 * n)
            if hi_pct / 100.0 * n > hi_i:           # ceil, so 100 keeps the last
                hi_i += 1
            keep_cases = set(dur.index[lo_i:hi_i])
            df = df[df["case:concept:name"].isin(keep_cases)]
    vranks = spec.get("variant_ranks")
    if vranks:
        lo, hi = vranks
        ranked = sorted(pm4py.get_variants(df).items(),
                        key=lambda kv: kv[1], reverse=True)
        selected = [k for k, _ in ranked[lo - 1:hi]]
        if selected:
            df = pm4py.filter_variants(df, selected, retain=True)
    t_from, t_to = spec.get("time_from"), spec.get("time_to")
    if t_from or t_to:
        mode = spec.get("time_mode", "traces_intersecting")
        ts = pd.to_datetime(df["time:timestamp"], utc=True, errors="coerce")
        lo = t_from or ts.min().strftime("%Y-%m-%d %H:%M:%S")
        hi = t_to or ts.max().strftime("%Y-%m-%d %H:%M:%S")
        df = pm4py.filter_time_range(df, lo, hi, mode=mode)
    return df


def resource_params():
    """Build the ``parameters`` resource keys the way the app does."""
    params = {}
    attrs = [a.strip() for a in RESOURCE_ATTRIBUTE.replace(",", " ").split()
             if a.strip()]
    if not attrs:
        params["resource_attribute"] = False
    elif len(attrs) == 1:
        params["resource_attribute"] = attrs[0]
    else:
        params["resource_attribute"] = attrs
    if attrs:
        params["resource_parameters"] = {"min_support": float(MIN_SUPPORT)}
    return params


def _heat_metrics():
    nm = OVERLAY_NODES[0] if OVERLAY_NODES else None
    em = OVERLAY_EDGES[0] if OVERLAY_EDGES else None
    return nm, em


def _family_span(family):
    """Shared (node_span, edge_span) across all cells for the 'family' scale."""
    if not (OVERLAY_HEATMAP and OVERLAY_HEATMAP_SCOPE == "family"):
        return None, None
    from pm4py_ucm.visualization.ucm.variants import classic as _classic
    nm, em = _heat_metrics()
    return _classic.heat_span([c.ucm for c in family.cells], nm, em)


def model_heat_params(family=None):
    """classic.apply heat parameters (colour/thickness) for the rendered images,
    matching the web app's overlay + scale. Empty when the heat-map is off."""
    if not OVERLAY_HEATMAP:
        return {}
    nm, em = _heat_metrics()
    ns, es = _family_span(family) if family is not None else (None, None)
    return {
        "heatmap_node": (nm, nm.endswith("_time")) if nm else None,
        "heatmap_edge": (em, em.endswith("_time")) if em else None,
        "heatmap_global": OVERLAY_HEATMAP_SCOPE in ("global", "family"),
        "node_span": ns, "edge_span": es,
    }


def report_heat(family):
    """model_to_svg heat kwargs for the report's embedded per-cell images."""
    if not OVERLAY_HEATMAP:
        return None
    nm, em = _heat_metrics()
    ns, es = _family_span(family)
    return {"heatmap": True, "node_metric": nm, "edge_metric": em,
            "heatmap_global": OVERLAY_HEATMAP_SCOPE in ("global", "family"),
            "node_span": ns, "edge_span": es}


def _slug(text):
    """A short filesystem-safe slug for an output filename."""
    s = "".join(c if c.isalnum() else "_" for c in str(text)).strip("_")
    return (s or "dashboard")[:40]


def _save_image(fn, *args, **kwargs):
    """Best-effort image render — a missing graphviz binary must not kill the
    pipeline (the ``.jucm`` and reports still get written)."""
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - user-facing, keep going
        print(f"[warn] image render skipped ({type(exc).__name__}: {exc})")
'''


# ---------------------------------------------------------------------------
# Value helpers.
# ---------------------------------------------------------------------------

def _as_dict(pairs: Any) -> Dict[str, Any]:
    """Normalise a ``[[k, v], ...]`` (JSON round-tripped tuple list) or a dict
    into a plain dict."""
    if not pairs:
        return {}
    if isinstance(pairs, dict):
        return dict(pairs)
    return {k: v for k, v in pairs}


def _resolved_config(doc: ProjectDoc) -> Dict[str, Any]:
    """Registry defaults overlaid with the project's stored config (known ids
    only; unknown/extra keys are ignored for codegen)."""
    cfg = defaults()
    for k, v in (doc.config or {}).items():
        if k in cfg:
            cfg[k] = v
    return cfg


def _decomposition_literal(value: Any) -> str:
    """Emit ``"off"`` or a dict literal for the decomposition argument."""
    if value in (None, "off", "", [], {}):
        return '"off"'
    return repr(_as_dict(value))


# ---------------------------------------------------------------------------
# Section builders — each returns a self-contained code block string.
# ---------------------------------------------------------------------------

def _doc_safe(s: str) -> str:
    """Make a value safe to embed inside the generated module docstring:
    neutralise backslashes (a Windows path would otherwise read as an escape)
    and any embedded triple double-quote."""
    return str(s).replace("\\", "/").replace('"' * 3, "'" * 3)


def _header(doc: ProjectDoc) -> str:
    log = doc.log
    name = _doc_safe(log.name)
    created = (f"\nCreated (UTC): {_doc_safe(doc.created_utc)}"
               if doc.created_utc else "")
    app = (f"\nApp version:   {_doc_safe(doc.app_version)}"
           if doc.app_version else "")
    return (
        '"""Reproducible pm4py-ucm analysis pipeline.\n\n'
        f"Generated by pm4py-ucm {GENERATOR_VERSION} from a saved project "
        "session.\n"
        "This is a faithful, deterministic replay of the GUI session's "
        "configuration:\n"
        "re-running it reproduces the same .jucm / reports. Edit the "
        "CONFIG block\n"
        "below (or pass a log path on the command line) to adapt it.\n\n"
        f"Log:           {name} ({log.kind})"
        f"{created}{app}\n"
        '"""'
    )


def _imports() -> str:
    return (
        "from __future__ import annotations\n\n"
        "import io\n"
        "import tempfile\n"
        "from pathlib import Path\n\n"
        "import pandas as pd\n"
        "import pm4py\n"
        "import pm4py_ucm"
    )


def _config(
    doc: ProjectDoc,
    cfg: Dict[str, Any],
    out_dir: str,
    include_scenarios: bool,
    include_family: bool,
    include_dashboards: bool = False,
    dashboards: Optional[List[Dict[str, Any]]] = None,
) -> str:
    log = doc.log
    lines: List[str] = [
        "# --- CONFIG (from the saved session) "
        + "-" * 37,
        f"DEFAULT_LOG = {log.name!r}",
        f"LOG_KIND = {log.kind!r}",
        f"CSV_COLUMNS = {list(log.csv_columns)!r}"
        if log.csv_columns else "CSV_COLUMNS = None",
        f"OUT_DIR = Path({out_dir!r})",
        "",
        f"NOISE_THRESHOLD = {float(cfg['noise_threshold'])!r}",
        f"MIN_SUPPORT = {float(cfg['min_support'])!r}",
        f"NOTATION = {cfg['notation']!r}",
        f"DECOMPOSITION = {_decomposition_literal(cfg['decomposition'])}",
        f"RESOURCE_ATTRIBUTE = {cfg['resource_attribute']!r}",
        f"OVERLAY_NODES = {list(cfg['overlay_nodes'])!r}",
        f"OVERLAY_EDGES = {list(cfg['overlay_edges'])!r}",
        f"OVERLAY_HEATMAP = {bool(cfg['overlay_heatmap'])!r}",
        f"OVERLAY_HEATMAP_SCOPE = {cfg['overlay_heatmap_scope']!r}",
        f"FILTER_SPEC = {_as_dict(cfg['filter_spec'])!r}",
    ]
    if include_scenarios:
        lines += [
            "",
            f"SCENARIO_STRATEGY = {cfg['scenario_strategy']!r}",
            f"SCENARIO_GROUP_NAME = {cfg['scenario_group_name']!r}",
            "SCENARIO_MAX_LOOP_ITERATIONS = "
            f"{int(cfg['scenario_max_loop_iterations'])!r}",
            "SCENARIO_DECISION_TREE_MAX_DEPTH = "
            f"{int(cfg['scenario_decision_tree_max_depth'])!r}",
        ]
    if include_family:
        inc = cfg["family_include_values"]
        inc_literal = repr(_as_dict(inc)) if inc else "None"
        lines += [
            "",
            f"FAMILY_ATTRS = {list(cfg['family_attrs'])!r}",
            f"FAMILY_MIN_CASES = {int(cfg['family_min_cases'])!r}",
            f"FAMILY_MAX_VALUES = {int(cfg['family_max_values'])!r}",
            f"FAMILY_BINS = {int(cfg['family_bins'])!r}",
            f"FAMILY_INCLUDE_VALUES = {inc_literal}",
            f"FAMILY_DEDUP = {bool(cfg['family_dedup'])!r}",
        ]
    if include_dashboards:
        # The saved dashboards (widget specs + dashboard-level filters), as
        # data. Each is rendered to a self-contained interactive HTML file.
        lines += [
            "",
            "# Saved dashboards: [{id, name, specs, filters}, ...].",
            f"DASHBOARDS = {dashboards!r}",
        ]
    return "\n".join(lines)


def _model_fn() -> str:
    return (
        "def run_model(log):\n"
        '    """Mine the UCM, overlay performance, and export it."""\n'
        "    tree = pm4py.discover_process_tree_inductive(\n"
        "        log, noise_threshold=NOISE_THRESHOLD)\n"
        '    params = {"process_tree": tree}\n'
        "    params.update(resource_params())\n"
        "    ucm = pm4py_ucm.discover_ucm_inductive(\n"
        "        log, parameters=params, decomposition=DECOMPOSITION)\n"
        "    if OVERLAY_NODES or OVERLAY_EDGES:\n"
        "        pm4py_ucm.annotate_performance(\n"
        "            ucm, log,\n"
        "            node_metrics=OVERLAY_NODES, edge_metrics=OVERLAY_EDGES)\n"
        '    pm4py_ucm.write_ucm(ucm, str(OUT_DIR / "model.jucm"))\n'
        "    _save_image(pm4py_ucm.save_vis_ucm, ucm,\n"
        '                str(OUT_DIR / "model.png"), style=NOTATION,\n'
        "                parameters=model_heat_params())\n"
        '    print(f"[model] wrote {OUT_DIR / \'model.jucm\'} "\n'
        '          f"({len(ucm.maps)} map(s), "\n'
        '          f"{sum(len(m.nodes) for m in ucm.maps)} nodes)")\n'
        "    return ucm"
    )


def _scenarios_fn() -> str:
    return (
        "def run_scenarios(log):\n"
        '    """Synthesize an executable UCM: one ScenarioDef per variant."""\n'
        "    ucm_s, clustering = pm4py_ucm.discover_scenarios(\n"
        "        log,\n"
        "        parameters=resource_params(),\n"
        "        decomposition=DECOMPOSITION,\n"
        "        condition_strategy=SCENARIO_STRATEGY,\n"
        "        group_name=SCENARIO_GROUP_NAME,\n"
        "        max_loop_iterations=SCENARIO_MAX_LOOP_ITERATIONS,\n"
        "        decision_tree_max_depth=SCENARIO_DECISION_TREE_MAX_DEPTH,\n"
        "    )\n"
        '    pm4py_ucm.write_ucm(ucm_s, str(OUT_DIR / "scenarios.jucm"))\n'
        "    pm4py_ucm.write_variants_report(\n"
        '        clustering, str(OUT_DIR / "variants.csv"))\n'
        "    pm4py_ucm.write_case_variant_map(\n"
        '        clustering, str(OUT_DIR / "case_variant_map.csv"))\n'
        '    if SCENARIO_STRATEGY == "data-driven" and ucm_s.scenario_groups:\n'
        "        pm4py_ucm.write_condition_mining_report(\n"
        "            ucm_s.scenario_groups[0],\n"
        '            str(OUT_DIR / "condition_mining.csv"))\n'
        '    print(f"[scenarios] wrote {OUT_DIR / \'scenarios.jucm\'} "\n'
        '          f"({len(clustering.variants)} variant(s))")\n'
        "    return ucm_s"
    )


def _family_fn() -> str:
    return (
        "def run_family(log):\n"
        '    """Mine one model per attribute cell + the umbrella + report."""\n'
        "    family = pm4py_ucm.discover_ucm_family(\n"
        "        log, FAMILY_ATTRS,\n"
        "        decomposition=DECOMPOSITION,\n"
        "        noise_threshold=NOISE_THRESHOLD,\n"
        "        min_cases=FAMILY_MIN_CASES,\n"
        "        max_values_per_attribute=FAMILY_MAX_VALUES,\n"
        "        bins=FAMILY_BINS,\n"
        "        include_values=FAMILY_INCLUDE_VALUES,\n"
        "        parameters=resource_params(),\n"
        "    )\n"
        "    if OVERLAY_NODES or OVERLAY_EDGES:\n"
        '        fam_cases = family.log_df["case:concept:name"].astype(str)\n'
        "        for cell in family.cells:\n"
        "            cell_df = family.log_df[\n"
        "                fam_cases.isin(set(cell.case_ids))]\n"
        "            pm4py_ucm.annotate_performance(\n"
        "                cell.ucm, cell_df,\n"
        "                node_metrics=OVERLAY_NODES,\n"
        "                edge_metrics=OVERLAY_EDGES)\n"
        '    pm4py_ucm.write_ucm_family(family, str(OUT_DIR / "family.zip"))\n'
        "    _save_image(pm4py_ucm.save_vis_ucm_family, family,\n"
        '                str(OUT_DIR / "family_grid.png"), style=NOTATION,\n'
        "                parameters=model_heat_params(family))\n"
        "    umbrella = pm4py_ucm.assemble_ucm_family(\n"
        '        family, mode="umbrella", dedup=FAMILY_DEDUP,\n'
        "        node_metrics=OVERLAY_NODES, edge_metrics=OVERLAY_EDGES)\n"
        "    pm4py_ucm.write_ucm(\n"
        '        umbrella, str(OUT_DIR / "family_umbrella.jucm"))\n'
        "    stats = pm4py_ucm.compute_family_stats(family)\n"
        "    pm4py_ucm.write_family_report(\n"
        '        family, str(OUT_DIR / "family_report.html"),\n'
        "        stats=stats, style=NOTATION, heat=report_heat(family))\n"
        '    print(f"[family] wrote {OUT_DIR / \'family.zip\'} "\n'
        '          f"({len(family.cells)} cell(s))")\n'
        "    return family"
    )


def _dashboards_fn() -> str:
    return (
        "def run_dashboards(log):\n"
        '    """Render each saved dashboard to a self-contained interactive '
        'HTML file\n'
        '    over the same (filtered) log the model was mined from."""\n'
        "    from pm4py_ucm.algo.dashboards import (\n"
        "        build_fact_table, write_dashboard)\n"
        "    df = log\n"
        "    if not isinstance(df, pd.DataFrame):\n"
        "        df = pm4py.convert_to_dataframe(df)\n"
        "    table = build_fact_table(df, log_name=Path(DEFAULT_LOG).stem)\n"
        "    for i, dash in enumerate(DASHBOARDS, start=1):\n"
        '        name = dash.get("name") or f"Dashboard {i}"\n'
        '        out = OUT_DIR / f"dashboard_{i:02d}_{_slug(name)}.html"\n'
        "        write_dashboard(\n"
        '            str(out), table, specs=dash.get("specs") or [],\n'
        '            filters=dash.get("filters") or [], name=name, title=name,\n'
        "            read_only=True)\n"
        '        print(f"[dashboards] wrote {out} "\n'
        "              f\"({len(dash.get('specs') or [])} widget(s))\")\n"
        "    return table"
    )


def _run_fn(include_scenarios: bool, include_family: bool,
            include_dashboards: bool = False) -> str:
    body = [
        "def run(log_path=DEFAULT_LOG):",
        '    """Load the log, apply the pre-mining transform, and reproduce '
        'every configured artifact into OUT_DIR."""',
        "    OUT_DIR.mkdir(parents=True, exist_ok=True)",
        "    log = read_log(log_path)",
        "    log = apply_log_filters(log, FILTER_SPEC)",
        "    run_model(log)",
    ]
    if include_scenarios:
        body.append("    run_scenarios(log)")
    if include_family:
        body.append(
            "    if FAMILY_ATTRS:\n"
            "        run_family(log)\n"
            "    else:\n"
            '        print("[family] skipped: FAMILY_ATTRS is empty")'
        )
    if include_dashboards:
        body.append("    run_dashboards(log)")
    body.append('    print(f"Done. Outputs in {OUT_DIR.resolve()}")')
    return "\n".join(body)


def _main() -> str:
    return (
        'if __name__ == "__main__":\n'
        "    import argparse\n"
        "    ap = argparse.ArgumentParser(\n"
        '        description="Reproduce this pm4py-ucm analysis.")\n'
        '    ap.add_argument("log", nargs="?", default=DEFAULT_LOG,\n'
        '                    help="path to the event log '
        '(default: %(default)s)")\n'
        '    ap.add_argument("--out", default=str(OUT_DIR),\n'
        '                    help="output directory (default: %(default)s)")\n'
        "    args = ap.parse_args()\n"
        "    OUT_DIR = Path(args.out)\n"
        "    run(args.log)"
    )


# ---------------------------------------------------------------------------
# Assembly.
# ---------------------------------------------------------------------------

def _blocks(
    doc: ProjectDoc,
    out_dir: str,
    include_scenarios: bool,
    include_family: bool,
    include_dashboards: bool = False,
) -> List[Tuple[str, str]]:
    """Ordered ``(label, code)`` blocks for the pipeline. ``label`` drives the
    notebook's per-cell markdown; the script just concatenates the code."""
    cfg = _resolved_config(doc)
    dashboards = _project_dashboards(doc) if include_dashboards else []
    blocks: List[Tuple[str, str]] = [
        ("header", _header(doc)),
        ("imports", _imports()),
        ("config", _config(doc, cfg, out_dir, include_scenarios,
                           include_family, include_dashboards, dashboards)),
        ("helpers", _HELPERS.rstrip("\n")),
        ("model", _model_fn()),
    ]
    if include_scenarios:
        blocks.append(("scenarios", _scenarios_fn()))
    if include_family:
        blocks.append(("family", _family_fn()))
    if include_dashboards:
        blocks.append(("dashboards", _dashboards_fn()))
    blocks.append(("run", _run_fn(include_scenarios, include_family,
                                  include_dashboards)))
    blocks.append(("main", _main()))
    return blocks


def _wants_family(doc: ProjectDoc, include_family: Optional[bool]) -> bool:
    if include_family is not None:
        return include_family
    return bool(_resolved_config(doc).get("family_attrs"))


def _project_dashboards(doc: ProjectDoc) -> List[Dict[str, Any]]:
    """The list of ``{id, name, specs, filters}`` dashboards a project carries
    (unwrapped from the versioned envelope), or ``[]``."""
    reg = unwrap_registry(getattr(doc, "dashboards", None))
    return list((reg or {}).get("dashboards") or [])


def _wants_dashboards(doc: ProjectDoc, include_dashboards: Optional[bool]) -> bool:
    if include_dashboards is not None:
        return include_dashboards
    return bool(_project_dashboards(doc))


def generate_script(
    doc: ProjectDoc,
    *,
    out_dir: str = "pm4py_ucm_output",
    include_scenarios: bool = False,
    include_family: Optional[bool] = None,
    include_dashboards: Optional[bool] = None,
) -> str:
    """Emit a runnable ``.py`` pipeline for ``doc``.

    ``include_family`` / ``include_dashboards`` default to auto — on when the
    session picked family attributes / carries saved dashboards. ``include_
    scenarios`` is opt-in (a scenario re-mine is costly and a session carries
    scenario settings even when the user never used the view). The result always
    ends with a trailing newline.
    """
    inc_family = _wants_family(doc, include_family)
    inc_dash = _wants_dashboards(doc, include_dashboards)
    blocks = _blocks(doc, out_dir, include_scenarios, inc_family, inc_dash)
    return "\n\n\n".join(code for _, code in blocks) + "\n"


def _nb_md(text: str) -> Dict[str, Any]:
    return {"cell_type": "markdown", "metadata": {},
            "source": text.splitlines(keepends=True) or [""]}


def _nb_code(text: str) -> Dict[str, Any]:
    return {"cell_type": "code", "metadata": {}, "execution_count": None,
            "outputs": [], "source": text.splitlines(keepends=True) or [""]}


def generate_notebook(
    doc: ProjectDoc,
    *,
    out_dir: str = "pm4py_ucm_output",
    include_scenarios: bool = False,
    include_family: Optional[bool] = None,
    include_dashboards: Optional[bool] = None,
) -> str:
    """Emit the pipeline as a Jupyter ``.ipynb`` (JSON string), laid out as a
    **tutorial**: each stage's function is defined and then immediately invoked,
    with the intermediate result shown inline (the loaded log, the case counts,
    the mined model image, the variants, the family grid), not just written to
    the output folder. Built as nbformat-4 JSON by hand (no ``nbformat`` dep).
    """
    cfg = _resolved_config(doc)
    inc_family = _wants_family(doc, include_family)
    inc_dash = _wants_dashboards(doc, include_dashboards)
    dashboards = _project_dashboards(doc) if inc_dash else []
    log = doc.log

    cells: List[Dict[str, Any]] = [
        _nb_md(
            f"# Reproducible pm4py-ucm pipeline\n\n"
            f"Generated from a saved project session — a faithful, "
            f"deterministic replay of the GUI configuration on "
            f"**{_doc_safe(log.name)}**. Run the cells top to bottom: each step "
            f"defines its function, runs it, and shows the result inline while "
            f"also writing the artifact to the output folder."),
        _nb_md("## Imports"),
        _nb_code(_imports()
                 + "\nfrom IPython.display import Image  # inline previews"),
        _nb_md("## Configuration\n\nEverything the session captured — edit any "
               "of these to adapt the pipeline."),
        _nb_code(_config(doc, cfg, out_dir, include_scenarios, inc_family,
                         inc_dash, dashboards)),
        _nb_md("## Helpers\n\nLog loading, the pre-mining transform (rename + "
               "filters), the resource parameters and the heat-map render "
               "settings — defined once, used by the steps below."),
        _nb_code(_HELPERS.rstrip("\n")),
        _nb_md("## 1 · Load the event log"),
        _nb_code(
            "OUT_DIR.mkdir(parents=True, exist_ok=True)\n"
            "log = read_log(DEFAULT_LOG)\n"
            "print(f\"Loaded {log['case:concept:name'].nunique():,} cases, \"\n"
            "      f\"{len(log):,} events.\")\n"
            "log.head()"),
        _nb_md("## 2 · Apply the pre-mining transform\n\nActivity renaming and "
               "the log filters, exactly as configured (a no-op when the "
               "session had none)."),
        _nb_code(
            "log = apply_log_filters(log, FILTER_SPEC)\n"
            "print(f\"After the transform: "
            "{log['case:concept:name'].nunique():,} cases, \"\n"
            "      f\"{len(log):,} events.\")"),
        _nb_md("## 3 · Mine and export the UCM\n\nInductive miner → process "
               "tree → UCM, with performers and the performance overlay. Writes "
               "`model.jucm` and `model.png`."),
        _nb_code(_model_fn() + "\n\nucm = run_model(log)"),
        _nb_md("The mined model, previewed inline:"),
        _nb_code('Image(str(OUT_DIR / "model.png")) '
                 'if (OUT_DIR / "model.png").exists() else ucm'),
    ]
    if include_scenarios:
        cells += [
            _nb_md("## 4 · Scenario synthesis\n\nOne executable `ScenarioDef` "
                   "per behavioural variant. Writes `scenarios.jucm` and the "
                   "variant CSVs."),
            _nb_code(_scenarios_fn() + "\n\nrun_scenarios(log)"),
            _nb_md("The behavioural variants that drive the scenarios:"),
            _nb_code('pd.read_csv(OUT_DIR / "variants.csv").head(10)'),
        ]
    if inc_family:
        cells += [
            _nb_md("## 5 · Model family\n\nOne model per attribute combination, "
                   "the dynamic-stub umbrella, and the statistics report. "
                   "Writes `family.zip`, `family_umbrella.jucm`, "
                   "`family_grid.png` and `family_report.html`."),
            _nb_code(_family_fn()
                     + "\n\nfamily = run_family(log) if FAMILY_ATTRS else None"),
            _nb_md("The family grid — one mined model per combination:"),
            _nb_code('Image(str(OUT_DIR / "family_grid.png")) '
                     'if (OUT_DIR / "family_grid.png").exists() else None'),
        ]
    if inc_dash:
        cells += [
            _nb_md("## 6 · Dashboards\n\nEach saved dashboard rendered to a "
                   "self-contained interactive HTML file over the same filtered "
                   "log — open one in a browser for the live view."),
            _nb_code(_dashboards_fn() + "\n\ntable = run_dashboards(log)"),
            _nb_md("The dashboard files written:"),
            _nb_code("sorted(p.name for p in OUT_DIR.glob('dashboard_*.html'))"),
        ]
    cells.append(_nb_md(f"---\nDone — every artifact is under `{out_dir}/`."))

    nb = {
        "cells": cells,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3", "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    return json.dumps(nb, indent=1, ensure_ascii=False) + "\n"
