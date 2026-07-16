"""PM4Py-UCM web front-end — V4 (workspace shell + user-defined
dashboards). **Work in progress; V3 is still the deployed app.**

V4 is V3's capability behind the redesigned workspace shell, plus the
new Dashboards view. Where V3 puts its views in ``st.tabs``, V4 puts
them in a left rail — a persistent list of VIEWS beside a persistent
log card — so the log you loaded and the views over it stay visible
together.

Views: **Model** (inductive-mine a UCM, preview in UCM or BPMN
notation, download PNG/.jucm), **Scenarios** (concurrency-aware
variant clustering + scenario synthesis, with every artifact — the
executable .jucm, variants.csv, case_variant_map.csv, and for
data-driven runs condition_mining.csv — as a download), **Family**
(partition the log by 1–2 case attributes and mine a model per
combination: grid rendering, per-cell zip, combined .jucm, a
dynamic-stub umbrella .jucm with per-combination strategies, and the
self-contained interactive HTML statistics report), **Compare** (rank
the family members and compare any two side by side), and
**Dashboards** (build widgets from a metric catalog over the log:
filters, segmentation, targets, scorecard — see
:doc:`docs/dashboards.md </dashboards>`).

The Dashboards view is an HTML/JS island embedded via
``components.html``. It is the *same artifact* the HTML export writes,
built by :func:`pm4py_ucm.algo.dashboards.dashboard_html`, so the app
and the export cannot drift. Its widgets are computed in the browser
and its state lives there — ``components.html`` is one-way, so widget
specs persist to the browser's storage rather than to session state.

Run locally:

    streamlit run web/streamlit_app_v4.py

Deployment layout: ``streamlit_app.py`` (the
https://pm4py-ucm.streamlit.app/ main file) is a shim that runs
``streamlit_app_v3.py``, so the primary deployment serves V3 until V4
is ready to take over. V1 (model-only) was retired at v0.5.1 and lives
in git history. ``streamlit_app_v2.py`` is deliberately NOT a shim: it
is the frozen V2 (model + scenarios) app that
https://pm4py-ucm-scenarios.streamlit.app/ must keep serving while a
paper referencing it is under review — do not fold it into V3 or V4.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json as _json
import os
import re
import tempfile
import traceback
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st
import streamlit.components.v1 as _components

# Make the in-repo package win over any environment-installed copy.
# The main-file shim launches this app with ``sys.path[0]`` set to the
# ``web/`` directory, so a bare ``import pm4py_ucm`` would resolve to a
# site-packages install. On Streamlit Cloud the app *code* is pulled on
# every push but the virtualenv is only rebuilt when requirements
# change, so that install can lag the checkout by several releases
# (missing new APIs / metrics). Prepending the repo root guarantees the
# app always imports the current checkout's ``pm4py_ucm``.
import sys as _sys
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in _sys.path:
    _sys.path.insert(0, _REPO_ROOT)

import pm4py
import pm4py_ucm
from pm4py_ucm.algo.discovery.scenarios import reports as _reports
from pm4py_ucm.algo.discovery.scenarios import synthesis as _scenarios
from pm4py_ucm.algo.discovery.variants import clustering as _clustering_mod
from pm4py_ucm.visualization.ucm import visualizer as _visualizer
from pm4py_ucm.visualization.ucm import stacked as _stacked
from pm4py_ucm.visualization.ucm import svg as _svgmod

# Pillow's default decompression-bomb guard (~178M px) rejects very
# large composites (family grids, decomposed stacks); raise it to a
# still-sane 1B-pixel cap.
from PIL import Image as _PILImage
_PILImage.MAX_IMAGE_PIXELS = 1_000_000_000


def _embed_html(html: str, *, height: int, scrolling: bool = False) -> None:
    """Embed a self-contained HTML document in a sandboxed iframe.

    THE single seam for ``st.components.v1.html`` — every island (the
    Dashboards view, the SVG model viewer, the open-image-in-tab button)
    goes through here. ``st.components.v1.html`` is deprecated in favour
    of ``st.iframe`` (see the tracking issue): ``st.iframe`` takes a
    URL/Path rather than an HTML string, so migrating means serving each
    artifact from a file — a change confined to THIS function instead of
    scattered across the app. ``st.html`` is not an option for these:
    they need the iframe's isolated JS realm (and, for the open-in-tab
    button, its sandboxed popup escape), which inline ``st.html`` does
    not give.
    """
    _components.html(html, height=height, scrolling=scrolling)


_SAMPLES_DIR = Path(__file__).resolve().parent / "samples"
_DISPLAY_WIDTH_PX = 1100
_NONE_OPT = "(none)"

# (session key, candidate names, include_none flag). Used by the CSV
# column auto-detection.
_CSV_AUTOPICK = [
    ("csv_case",      ("case:concept:name", "case_id", "case", "caseid"),            False),
    ("csv_activity",  ("concept:name", "activity", "activityname", "event", "task"), False),
    ("csv_timestamp", ("time:timestamp", "timestamp", "time", "datetime", "date"),   False),
    ("csv_role",      ("org:role", "role"),                                          True),
    ("csv_resource",  ("org:resource", "resource", "user", "performer"),             True),
]


# ---------------------------------------------------------------------------
# Helpers (file IO, sample listing, name sanitisation)
# ---------------------------------------------------------------------------

def _list_samples() -> List[Path]:
    if not _SAMPLES_DIR.is_dir():
        return []
    out: List[Path] = []
    for p in _SAMPLES_DIR.iterdir():
        if not p.is_file():
            continue
        name = p.name.lower()
        if name.endswith((".xes", ".xes.gz", ".gz", ".zip")):
            out.append(p)
    return sorted(out, key=lambda p: p.name.lower())


def _safe_download_name(stem: str, ext: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    if not cleaned:
        cleaned = "model"
    return f"{cleaned}{ext}"


def _extract_xes_from_zip(zip_bytes: bytes) -> bytes:
    """Pick a single .xes / .xes.gz entry, guarding against zip-slip."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if name.startswith("/") or ".." in Path(name).parts:
                continue
            low = name.lower()
            if low.endswith(".xes") or low.endswith(".xes.gz"):
                return zf.read(info)
    raise ValueError(
        "No .xes / .xes.gz entry found in the uploaded zip archive."
    )


def _autopick_column(columns, candidates, *, include_none, fallback_index):
    lower = {c.lower(): c for c in columns}
    for cand in candidates:
        hit = lower.get(cand.lower())
        if hit is not None:
            return hit
    if include_none:
        return _NONE_OPT
    return columns[min(fallback_index, len(columns) - 1)]


def _seed_csv_selectors(columns):
    for i, (key, cands, with_none) in enumerate(_CSV_AUTOPICK):
        st.session_state[key] = _autopick_column(
            columns, cands, include_none=with_none, fallback_index=i,
        )


@st.cache_data(show_spinner="Reading CSV columns...")
def _csv_columns(csv_bytes: bytes, _file_hash: str) -> List[str]:
    try:
        df_head = pd.read_csv(io.BytesIO(csv_bytes), nrows=0, low_memory=False)
        return list(df_head.columns)
    except Exception:
        import csv as _csv
        text = csv_bytes.decode("utf-8", errors="replace").splitlines()
        if not text:
            return []
        reader = _csv.reader(text[:1])
        return next(reader, [])


def _html_escape_min(text: str) -> str:
    """Escape text bound for the rail's raw-HTML markup.

    Log names and attribute names are user-supplied — an uploaded file
    called ``<img onerror=...>.xes`` would otherwise be injected straight
    into the page by ``unsafe_allow_html``.
    """
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _arg_fingerprint(*args) -> str:
    return hashlib.sha256(repr(args).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Mining (UCM only, no scenarios) — cached on the log + miner
# settings, plus a returned process-tree fingerprint so the scenario
# step can detect whether its inputs changed.
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def _mine(
    log_bytes: bytes,
    log_kind: str,
    csv_columns,
    decomposition_spec,
    resource_attribute: str,
    min_support: float,
    noise_threshold: float,
    overlay_nodes: Tuple[str, ...],
    overlay_edges: Tuple[str, ...],
    _file_hash: str,
    _status=None,
    _progress=None,
) -> Dict[str, Any]:
    """Read the event log and mine a UCM. Returns .jucm bytes + metadata.

    Decomposition is honoured here (the model preview reflects it), but
    the scenarios tab always re-mines internally with decomposition=None
    so OR-fork conditions can land on every XOR (see the limitation
    documented on :func:`pm4py_ucm.discover_scenarios`).
    """
    def _phase(label: str) -> None:
        if _status is not None:
            _status.update(label=label)

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        if log_kind == "csv":
            if not csv_columns:
                raise ValueError("CSV column mapping is required.")
            case_col, activity_col, ts_col, role_col, resource_col = csv_columns
            if not (case_col and activity_col and ts_col):
                raise ValueError(
                    "Case, activity, and timestamp columns are required "
                    "for CSV import."
                )
            _phase("Reading CSV...")
            df = pd.read_csv(io.BytesIO(log_bytes), low_memory=False)
            _phase(f"Formatting {len(df):,} events...")
            df = pm4py.format_dataframe(
                df, case_id=case_col, activity_key=activity_col,
                timestamp_key=ts_col,
            )
            renames: Dict[str, str] = {}
            if role_col and role_col != "org:role":
                renames[role_col] = "org:role"
            if resource_col and resource_col != "org:resource":
                renames[resource_col] = "org:resource"
            if renames:
                df = df.rename(columns=renames)
            log = df
        else:
            is_zip = (
                log_kind == "zip"
                or (len(log_bytes) >= 2 and log_bytes[:2] == b"PK")
            )
            if is_zip:
                _phase("Extracting XES from zip...")
                xes_bytes = _extract_xes_from_zip(log_bytes)
            else:
                xes_bytes = log_bytes
            xes_path = td / "log.xes"
            xes_path.write_bytes(xes_bytes)
            _phase("Reading XES...")
            log = pm4py.read_xes(str(xes_path))

        params: Dict[str, Any] = {}
        attrs = [a.strip() for a in resource_attribute.replace(",", " ").split()
                 if a.strip()]
        if not attrs:
            params["resource_attribute"] = False
        elif len(attrs) == 1:
            params["resource_attribute"] = attrs[0]
        else:
            params["resource_attribute"] = attrs
        if attrs:
            params["resource_parameters"] = {"min_support": float(min_support)}

        if decomposition_spec == "off":
            decomp_arg = "off"
        else:
            decomp_arg = dict(decomposition_spec)

        _phase(
            "Discovering process tree "
            f"(noise threshold {noise_threshold:.2f})..."
        )
        tree = pm4py.discover_process_tree_inductive(
            log, noise_threshold=float(noise_threshold),
        )
        params["process_tree"] = tree

        _phase("Mining performers & converting tree to UCM...")
        ucm = pm4py_ucm.discover_ucm_inductive(
            log, parameters=params, decomposition=decomp_arg,
        )

        if overlay_nodes or overlay_edges:
            _phase("Computing performance overlay...")
            pm4py_ucm.annotate_performance(
                ucm, log,
                node_metrics=list(overlay_nodes),
                edge_metrics=list(overlay_edges),
            )

        _phase("Writing .jucm...")
        jucm_path = td / "model.jucm"
        pm4py_ucm.write_ucm(ucm, str(jucm_path))

        # Case / activity counts for the metrics row. ``log`` here is
        # either a DataFrame (CSV path, or modern pm4py.read_xes) or a
        # pm4py EventLog (older pm4py releases).
        try:
            n_cases = int(log["case:concept:name"].nunique())
            n_activities = int(log["concept:name"].nunique())
            n_events = int(len(log))
        except (KeyError, TypeError, AttributeError):
            n_cases = len(log)
            activities: set = set()
            n_events = 0
            for trace in log:
                for event in trace:
                    activities.add(event.get("concept:name"))
                    n_events += 1
            n_activities = len(activities)

        return {
            "jucm": jucm_path.read_bytes(),
            "n_maps": len(ucm.maps),
            "n_nodes": sum(len(m.nodes) for m in ucm.maps),
            "n_cases": n_cases,
            "n_events": n_events,
            "n_activities": n_activities,
        }


@st.cache_data(show_spinner=False)
def _render_cached(jucm_bytes: bytes, style: str) -> bytes:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        jucm_path = td / "model.jucm"
        jucm_path.write_bytes(jucm_bytes)
        ucm = pm4py_ucm.read_ucm(str(jucm_path))
        png_path = td / "model.png"
        _render_png(ucm, style, str(png_path))
        return png_path.read_bytes()


# Inline-SVG model rendering (single/stacked, navigable stub links, the
# dynamic-stub picker markup) lives in the PACKAGE
# (pm4py_ucm.visualization.ucm.svg), so the app, the family grid, and the
# HTML reports share one implementation and it carries unit tests. This
# app keeps only the Streamlit-specific viewer (_svg_viewer) and the
# cached wrappers below.


@st.cache_data(show_spinner=False)
def _render_svg_cached(jucm_bytes: bytes, style: str) -> str:
    """The model as one inline SVG string (cached per ``jucm`` + notation).

    SVG zooms and pans crisply where a raster does not, and its text is
    selectable. Decomposed models stack their maps and hyperlink each
    stub to its sub-map — see :func:`pm4py_ucm.visualization.ucm.svg`.
    """
    with tempfile.TemporaryDirectory() as td:
        jucm_path = Path(td) / "model.jucm"
        jucm_path.write_bytes(jucm_bytes)
        ucm = pm4py_ucm.read_ucm(str(jucm_path))
        return _svgmod.model_to_svg(ucm, style)


def _render_png(ucm, style: str, out_path: str) -> str:
    params = {"style": style}
    if len(ucm.maps) <= 1:
        gviz = _visualizer.apply(ucm, parameters=params)
        return _visualizer.save(gviz, out_path)
    from pm4py_ucm.visualization.ucm.variants import classic as _classic
    panels: List[Tuple[str, str]] = []
    tmpdir = tempfile.mkdtemp(prefix="pm4py_ucm_v2_")
    for idx, ucm_map in enumerate(ucm.maps):
        per = dict(params)
        per["map_index"] = idx
        per["format"] = "png"
        gviz = _classic.apply(ucm, parameters=per)
        gviz.format = "png"
        base = os.path.join(tmpdir, f"map_{idx:03d}")
        rendered = gviz.render(filename=base, cleanup=True)
        panels.append((ucm_map.name or f"Map{idx}", rendered))
    return _stacked._composite(panels, out_path)


# ---------------------------------------------------------------------------
# Scenario synthesis (cached)
# ---------------------------------------------------------------------------

def _read_log_for_scenarios(log_bytes: bytes, log_kind: str, csv_columns):
    """Materialise the log in the form ``discover_scenarios`` expects.

    Mirrors the log-loading branch of :func:`_mine` but stops at the
    DataFrame / EventLog so the scenarios pipeline can do its own
    process-tree discovery (it pins the same tree to both the UCM
    builder and the clustering pass).
    """
    if log_kind == "csv":
        case_col, activity_col, ts_col, role_col, resource_col = csv_columns
        df = pd.read_csv(io.BytesIO(log_bytes), low_memory=False)
        df = pm4py.format_dataframe(
            df, case_id=case_col, activity_key=activity_col,
            timestamp_key=ts_col,
        )
        renames: Dict[str, str] = {}
        if role_col and role_col != "org:role":
            renames[role_col] = "org:role"
        if resource_col and resource_col != "org:resource":
            renames[resource_col] = "org:resource"
        if renames:
            df = df.rename(columns=renames)
        return df

    is_zip = (
        log_kind == "zip"
        or (len(log_bytes) >= 2 and log_bytes[:2] == b"PK")
    )
    if is_zip:
        xes_bytes = _extract_xes_from_zip(log_bytes)
    else:
        xes_bytes = log_bytes
    with tempfile.TemporaryDirectory() as td:
        xes_path = Path(td) / "log.xes"
        xes_path.write_bytes(xes_bytes)
        return pm4py.read_xes(str(xes_path))


@st.cache_data(show_spinner=False)
def _synthesize(
    log_bytes: bytes,
    log_kind: str,
    csv_columns,
    noise_threshold: float,
    condition_strategy: str,         # "variant" | "data-driven"
    max_loop_iterations: int,
    decision_tree_max_depth: int,
    group_name: str,
    decomposition_spec,              # "off" | tuple of (key, value) pairs
    resource_attribute: str,
    min_support: float,
    _file_hash: str,
    _status=None,
    _progress=None,
) -> Dict[str, Any]:
    """Run the full concurrency-aware variant + scenario pipeline.

    Two strategies, picked one at a time:

    * ``"variant"`` — single ``MinedScenarios`` group, lossless arc
      conditions (``variant_id == v_i``).
    * ``"data-driven"`` — single group, arc conditions mined from
      case-level attributes via a per-OR-fork DecisionTreeClassifier.

    ``decomposition_spec`` follows the sidebar's cache-friendly form
    (``"off"`` or a sorted tuple of ``(key, value)`` pairs) and is
    forwarded to the converter. When decomposition is active, XORs
    that land in plug-in maps will not receive arc conditions — this
    is a known limitation of the synthesizer, surfaced as a UI
    warning before this function is called.
    """
    def _phase(label: str) -> None:
        if _status is not None:
            _status.update(label=label)

    _phase("Loading event log...")
    log = _read_log_for_scenarios(log_bytes, log_kind, csv_columns)

    _phase("Discovering process tree...")
    tree = pm4py.discover_process_tree_inductive(
        log, noise_threshold=float(noise_threshold),
    )

    # Resolve resource params + decomposition argument the same way
    # the model-mining path does, so the UCM the scenarios attach to
    # matches the Model tab when both use the same settings.
    params: Dict[str, Any] = {"process_tree": tree}
    attrs = [a.strip() for a in resource_attribute.replace(",", " ").split()
             if a.strip()]
    if not attrs:
        params["resource_attribute"] = False
    elif len(attrs) == 1:
        params["resource_attribute"] = attrs[0]
    else:
        params["resource_attribute"] = attrs
    if attrs:
        params["resource_parameters"] = {"min_support": float(min_support)}

    if decomposition_spec == "off":
        decomp_arg: Any = "off"
    else:
        decomp_arg = dict(decomposition_spec)

    _phase("Mining performers & converting tree to UCM...")
    ucm = pm4py_ucm.discover_ucm_inductive(
        log, parameters=params, decomposition=decomp_arg,
    )

    _phase("Clustering variants (concurrency-aware replay)...")
    clustering = _clustering_mod.cluster(
        log, tree, progress_callback=_progress,
    )

    _phase(f"Synthesizing {condition_strategy} scenarios...")
    synth_kwargs: Dict[str, Any] = dict(
        group_name=group_name,
        emit_conditions=True,
        max_loop_iterations=int(max_loop_iterations),
        condition_strategy=condition_strategy,
    )
    if condition_strategy == "data-driven":
        synth_kwargs["log"] = log
        synth_kwargs["decision_tree_max_depth"] = int(decision_tree_max_depth)
    group = _scenarios.synthesize_scenarios(ucm, tree, clustering, **synth_kwargs)
    data_group = group if condition_strategy == "data-driven" else None

    _phase("Writing artifacts...")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        jucm_path = td / "model.jucm"
        pm4py_ucm.write_ucm(ucm, str(jucm_path))
        jucm_bytes = jucm_path.read_bytes()

        var_buf = io.StringIO()
        _reports.write_variants_report(clustering, var_buf)
        variants_csv = var_buf.getvalue().encode("utf-8")

        case_buf = io.StringIO()
        _reports.write_case_variant_map(clustering, case_buf)
        case_map_csv = case_buf.getvalue().encode("utf-8")

        condition_csv: Optional[bytes] = None
        if data_group is not None:
            cond_buf = io.StringIO()
            _reports.write_condition_mining_report(data_group, cond_buf)
            condition_csv = cond_buf.getvalue().encode("utf-8")

    variants_df = pd.read_csv(io.BytesIO(variants_csv))
    condition_df: Optional[pd.DataFrame] = None
    if condition_csv is not None:
        condition_df = pd.read_csv(io.BytesIO(condition_csv))

    return {
        "jucm": jucm_bytes,
        "variants_csv": variants_csv,
        "case_map_csv": case_map_csv,
        "condition_csv": condition_csv,
        "variants_df": variants_df,
        "condition_df": condition_df,
        "n_variants": len(clustering.variants),
        "n_sequence_variants": clustering.sequence_variant_count,
        "compression_ratio": clustering.compression_ratio,
        "fitness_percentage": clustering.fitness_percentage,
        "n_noise": len(clustering.noise_case_ids),
        "n_scenarios": sum(len(g.scenarios) for g in ucm.scenario_groups),
        "n_groups": len(ucm.scenario_groups),
        "group_names": [g.name for g in ucm.scenario_groups],
        "n_maps": len(ucm.maps),
    }


# ---------------------------------------------------------------------------
# Model families (cached)
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def _load_log_df(log_bytes: bytes, log_kind: str, csv_columns,
                 _file_hash: str) -> pd.DataFrame:
    """The event log as a DataFrame — the form the family partitioner
    works on. Cached so attribute detection / preview / mining all hit
    the same parse."""
    log = _read_log_for_scenarios(log_bytes, log_kind, csv_columns)
    if isinstance(log, pd.DataFrame):
        return log
    return pm4py.convert_to_dataframe(log)


# ---------------------------------------------------------------------------
# Dashboards (cached)
# ---------------------------------------------------------------------------

#: The dashboard a log opens with, before the user builds their own.
#: Deliberately log-agnostic: every metric here needs no activity
#: parameter, so this works on any log rather than naming activities that
#: exist in one sample and not another. The composer's own defaults pick
#: sensible activities when the user adds a parameterised widget.
_DEFAULT_DASHBOARD_SPECS: List[Dict[str, Any]] = [
    {"id": "d-dur", "title": "Case duration", "metric": "duration",
     "agg": "avg", "viz": "kpi"},
    {"id": "d-med", "title": "median case duration", "metric": "duration",
     "agg": "median", "viz": "kpi"},
    {"id": "d-rework", "title": "Rework rate", "metric": "rework",
     "viz": "kpi"},
    {"id": "d-events", "title": "avg events per case",
     "metric": "eventCount", "agg": "avg", "viz": "kpi"},
    {"id": "d-wip", "title": "Work in progress over time", "metric": "wip",
     "agg": "avg", "viz": "bar"},
    # Weekday, not variant: a real log has hundreds of variants (164 on
    # ClaimsPaymentLog), and a starter widget should be legible on any
    # log rather than render a wall of two-pixel bars.
    {"id": "d-dur-weekday", "title": "avg duration by weekday",
     "metric": "duration", "agg": "avg", "viz": "bar",
     "segment": {"rows": "weekday"}},
]


@st.cache_data(show_spinner="Preparing dashboard data...")
def _fact_table(log_bytes: bytes, log_kind: str, csv_columns,
                log_name: str, _file_hash: str):
    """The per-case fact table the Dashboards view computes over.

    Cached on the log, not on any widget: the browser recomputes every
    widget itself, so this runs once per log rather than once per
    interaction. Sub-second on the logs the app ships (see
    docs/dashboards.md).
    """
    from pm4py_ucm.algo.dashboards import build_fact_table

    df = _load_log_df(log_bytes, log_kind, csv_columns, _file_hash)
    return build_fact_table(df, log_name=log_name)


@st.cache_data(show_spinner=False)
def _dashboard_html_cached(_table, specs_json: str, name: str,
                           renders: Tuple[Tuple[str, str], ...],
                           storage_key: str, read_only: bool,
                           theme: str,
                           model_svg: Tuple[Tuple[str, str], ...],
                           family_report: str = "") -> str:
    """The dashboard artifact.

    ``_table`` is underscore-prefixed so Streamlit does not try to hash a
    FactTable of numpy buffers; ``storage_key`` already identifies the
    log, and the remaining arguments are hashable, so the key is sound.
    """
    from pm4py_ucm.algo.dashboards import dashboard_html
    import json as _json

    return dashboard_html(
        _table,
        specs=_json.loads(specs_json),
        name=name,
        renders=dict(renders),
        storage_key=storage_key,
        read_only=read_only,
        # The island is inside an iframe: it can read the OS preference
        # but not Streamlit's own theme setting, so it has to be told
        # which one is actually on screen around it.
        theme=theme,
        # Both notations as inline SVG, for the session report's model
        # section (which the browser cannot render itself).
        model_svg=dict(model_svg),
        # The mined family's statistics report (empty when none), embedded
        # in the session report as a Family section.
        family_report=family_report or None,
    )


@st.cache_data(show_spinner="Detecting case attributes...")
def _detect_family_attributes(
    log_bytes: bytes, log_kind: str, csv_columns, _file_hash: str,
) -> List[Dict[str, Any]]:
    """Case-constant attributes usable as partition axes, with the
    context a user needs to pick one (type, cardinality, missing %)."""
    from pm4py_ucm.algo.discovery.families import detect_case_attributes

    df = _load_log_df(log_bytes, log_kind, csv_columns, _file_hash)
    specs, per_case_raw = detect_case_attributes(df)
    rows: List[Dict[str, Any]] = []
    for spec in specs.values():
        series = per_case_raw[spec.source_name]
        rows.append({
            "attribute": spec.source_name,
            "type": spec.type,
            "distinct": int(series.nunique(dropna=True)),
            "missing_pct": round(float(series.isna().mean()) * 100, 1),
        })
    rows.sort(key=lambda r: r["attribute"])
    return rows


@st.cache_data(show_spinner="Computing partition coverage...")
def _family_preview(
    log_bytes: bytes, log_kind: str, csv_columns,
    attrs: Tuple[str, ...], min_cases: int, max_values: int, bins: int,
    include_values,  # None or tuple of (attr, (label, ...)) pairs
    _file_hash: str,
) -> Dict[str, Any]:
    """Partition only (no mining) — the coverage heatmap shown before
    the user commits to mining N models. Also returns the value axes
    so the UI can offer per-attribute value filters."""
    from pm4py_ucm.algo.discovery.families import partition_log

    df = _load_log_df(log_bytes, log_kind, csv_columns, _file_hash)
    part = partition_log(
        df, list(attrs),
        min_cases=min_cases,
        max_values_per_attribute=max_values,
        bins=bins,
        include_values=(
            {k: list(v) for k, v in include_values}
            if include_values else None
        ),
    )
    counts = part.grid_counts()
    row_labels = [v.label for v in part.attributes[0].values]
    if len(part.attributes) == 2:
        col_labels = [v.label for v in part.attributes[1].values]
        data = {
            col: [counts.get((r, col), 0) for r in row_labels]
            for col in col_labels
        }
        pivot = pd.DataFrame(
            data, index=pd.Index(
                row_labels, name=part.attributes[0].display_name,
            ),
        )
    else:
        pivot = pd.DataFrame(
            {"cases": [counts.get((r,), 0) for r in row_labels]},
            index=pd.Index(
                row_labels, name=part.attributes[0].display_name,
            ),
        )
    return {
        "pivot": pivot,
        "n_cells": len(part.cells),
        "n_skipped": len(part.skipped_cells),
        "total_cases": part.total_cases,
        "covered_cases": part.covered_cases,
        "dropped_cases": part.dropped_cases,
        "axes": {
            a.display_name: [v.label for v in a.values]
            for a in part.attributes
        },
    }


@st.cache_data(show_spinner=False)
def _mine_family(
    log_bytes: bytes,
    log_kind: str,
    csv_columns,
    attrs: Tuple[str, ...],
    min_cases: int,
    max_values: int,
    bins: int,
    include_values,  # None or tuple of (attr, (label, ...)) pairs
    noise_threshold: float,
    decomposition_spec,
    resource_attribute: str,
    min_support: float,
    dedup: bool,
    overlay_nodes: Tuple[str, ...],
    overlay_edges: Tuple[str, ...],
    _file_hash: str,
    _status=None,
    _progress=None,
) -> Dict[str, Any]:
    """Mine the family and produce the notation-independent
    deliverables in one pass: the per-cell zip, the combined .jucm,
    and the umbrella .jucm. The mined family object is returned too so
    the grid PNG can be (re-)rendered per notation WITHOUT re-mining —
    rendering style must never be part of this function's cache key
    (see :func:`_render_family_grid`)."""
    from pm4py_ucm.objects.ucm.exporter.variants.jucm import (
        serialize_to_string,
    )

    def _phase(label: str) -> None:
        if _status is not None:
            _status.update(label=label)

    _phase("Loading event log...")
    df = _load_log_df(log_bytes, log_kind, csv_columns, _file_hash)

    params: Dict[str, Any] = {}
    res_attrs = [a.strip() for a in resource_attribute.replace(",", " ").split()
                 if a.strip()]
    if not res_attrs:
        params["resource_attribute"] = False
    elif len(res_attrs) == 1:
        params["resource_attribute"] = res_attrs[0]
    else:
        params["resource_attribute"] = res_attrs
    if res_attrs:
        params["resource_parameters"] = {"min_support": float(min_support)}

    decomp_arg: Any = (
        "off" if decomposition_spec == "off" else dict(decomposition_spec)
    )

    _phase(f"Mining one model per cell ({' × '.join(attrs)})...")
    family = pm4py_ucm.discover_ucm_family(
        df, list(attrs),
        decomposition=decomp_arg,
        noise_threshold=float(noise_threshold),
        min_cases=int(min_cases),
        max_values_per_attribute=int(max_values),
        bins=int(bins),
        include_values=(
            {k: list(v) for k, v in include_values}
            if include_values else None
        ),
        parameters=params,
        progress_callback=_progress,
    )

    if overlay_nodes or overlay_edges:
        # Per-cell overlays from each cell's own sub-log — visible in
        # the grid rendering and the per-cell .jucm files. (The
        # combined/umbrella assemblies re-convert the trees and are
        # not annotated.)
        _phase("Computing performance overlays...")
        fam_cases = family.log_df["case:concept:name"].astype(str)
        for cell in family.cells:
            cell_df = family.log_df[
                fam_cases.isin(set(cell.case_ids))
            ]
            pm4py_ucm.annotate_performance(
                cell.ucm, cell_df,
                node_metrics=list(overlay_nodes),
                edge_metrics=list(overlay_edges),
            )

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        _phase("Writing per-cell .jucm files...")
        zip_path = td / "family.zip"
        pm4py_ucm.write_ucm_family(family, str(zip_path))
        zip_bytes = zip_path.read_bytes()

        _phase("Assembling combined model...")
        combined_bytes = serialize_to_string(
            pm4py_ucm.assemble_ucm_family(
                family, mode="combined",
                node_metrics=list(overlay_nodes),
                edge_metrics=list(overlay_edges),
            ),
        ).encode("utf-8")

        _phase("Assembling umbrella model (dynamic stub)...")
        umbrella = pm4py_ucm.assemble_ucm_family(
            family, mode="umbrella", dedup=bool(dedup),
            node_metrics=list(overlay_nodes),
            edge_metrics=list(overlay_edges),
            progress_callback=_progress,
        )
        umbrella_bytes = serialize_to_string(umbrella).encode("utf-8")
        dynamic_stubs = [
            n for n in umbrella.maps[0].nodes
            if isinstance(n, pm4py_ucm.UCM.Stub) and n.dynamic
        ]
        n_variation_points = len(dynamic_stubs)
        n_plugins = sum(len(s.bindings) for s in dynamic_stubs)

    summary_rows = family.summary_rows()
    summary_df = pd.DataFrame(summary_rows[1:], columns=summary_rows[0])

    # Comparative statistics for the Compare tab and the HTML report —
    # MUST be computed here, while family.log_df still exists (the
    # FamilyStats object itself carries no DataFrames and stays small).
    _phase("Computing family statistics...")
    family_stats = pm4py_ucm.compute_family_stats(
        family, progress_callback=_progress,
    )

    # The statistics above were the last consumer of the full log —
    # drop it before this result is pickled into the cache so the
    # cached family stays small (grid rendering only needs the cells).
    family.log_df = None

    return {
        "family": family,
        "stats": family_stats,
        "zip": zip_bytes,
        "combined_jucm": combined_bytes,
        "umbrella_jucm": umbrella_bytes,
        "summary_df": summary_df,
        "n_cells": len(family.cells),
        "n_skipped": len(family.skipped_cells),
        "n_variation_points": n_variation_points,
        "n_plugins": n_plugins,
        "total_cases": family.total_cases,
        "covered_cases": family.covered_cases,
    }


#: Widest inline preview embedded in the page. The full-resolution
#: grid (which can be tens of thousands of pixels wide at export
#: quality) goes to the download button only — base64-embedding it
#: would make the browser tab unusable.
_GRID_PREVIEW_MAX_W = 2200


@st.cache_data(show_spinner=False)
def _render_family_grid(
    mine_fingerprint: str,
    style: str,
    _family,
) -> Tuple[Optional[bytes], Optional[bytes], Optional[str]]:
    """Render the family grid PNG for one notation.

    Cached on ``(mine_fingerprint, style)`` only — ``_family`` (the
    already-mined models) is deliberately excluded from the key via
    the underscore prefix. Switching UCM ↔ BPMN therefore re-renders
    but never re-mines. Returns ``(full_png, preview_png, None)`` or
    ``(None, None, error_text)``; the preview is capped at
    :data:`_GRID_PREVIEW_MAX_W` px wide (may be the same bytes as the
    full render when already narrow)."""
    try:
        with tempfile.TemporaryDirectory() as td:
            png_path = Path(td) / "grid.png"
            pm4py_ucm.save_vis_ucm_family(
                _family, str(png_path), style=style,
            )
            full = png_path.read_bytes()
            with _PILImage.open(io.BytesIO(full)) as im:
                if im.width > _GRID_PREVIEW_MAX_W:
                    f = _GRID_PREVIEW_MAX_W / im.width
                    preview_im = im.convert("RGB").resize(
                        (_GRID_PREVIEW_MAX_W, max(1, int(im.height * f))),
                        _PILImage.LANCZOS,
                    )
                    buf = io.BytesIO()
                    preview_im.save(buf, format="PNG")
                    preview = buf.getvalue()
                else:
                    preview = full
            return full, preview, None
    except Exception as exc:  # pragma: no cover - depends on env
        return None, None, f"{type(exc).__name__}: {exc}"


@st.cache_data(show_spinner=False)
def _build_family_report(
    mine_fingerprint: str,
    style: str,
    _family,
    _stats,
) -> Tuple[Optional[bytes], Optional[str]]:
    """Build the self-contained interactive HTML report (embedded
    per-cell images in the given notation). Cached on
    ``(mine_fingerprint, style)`` only — same convention as
    :func:`_render_family_grid`: switching notation re-renders images
    but never re-mines. Returns ``(html_bytes, None)`` or
    ``(None, error_text)``."""
    from pm4py_ucm.algo.discovery.families.report import family_report_html
    try:
        html = family_report_html(
            _family, stats=_stats, style=style,
        )
        return html.encode("utf-8"), None
    except Exception as exc:  # pragma: no cover - depends on env
        return None, f"{type(exc).__name__}: {exc}"


@st.cache_data(show_spinner=False)
def _render_family_cell(
    mine_fingerprint: str,
    style: str,
    cell_index: int,
    _family,
) -> Optional[bytes]:
    """One cell's model as a PNG for the Compare tab (cached per
    (mining run, notation, cell); ``None`` when rendering is
    unavailable)."""
    from pm4py_ucm.visualization.ucm.family_grid import _render_cell_png
    try:
        with tempfile.TemporaryDirectory() as td:
            path = _render_cell_png(
                _family.cells[cell_index].ucm, td, cell_index,
                {"style": style, "dpi": 192},
            )
            return Path(path).read_bytes()
    except Exception:  # pragma: no cover - depends on env
        return None


@st.cache_data(show_spinner=False)
def _render_family_cell_svg(
    mine_fingerprint: str,
    style: str,
    cell_index: int,
    _family,
) -> Optional[str]:
    """One cell's model as a navigable inline SVG for the Compare tab.

    Cached on ``(mining run, notation, cell)`` — ``_family`` excluded
    from the key (underscore), same convention as
    :func:`_render_family_cell`. A decomposed cell keeps its stub links
    inside itself. ``None`` when rendering is unavailable."""
    try:
        return _svgmod.model_to_svg(_family.cells[cell_index].ucm, style)
    except Exception:  # pragma: no cover - depends on env
        return None


@st.cache_data(show_spinner=False)
def _render_family_grid_svg(
    mine_fingerprint: str,
    style: str,
    _family,
) -> Tuple[Optional[str], Optional[str]]:
    """The whole family as one navigable 2-D vector SVG — the same matrix
    the PNG grid composites (rows × columns, headers, captions), but as
    vectors: it zooms, pans and downloads crisply and its text stays
    selectable.

    Each member is rendered with a per-cell id prefix, so a decomposed
    member's stub links only ever resolve within that member — no
    cross-family jumps. Cached on ``(mine_fingerprint, style)`` like
    :func:`_render_family_grid`. Returns ``(svg_str, None)`` or
    ``(None, error_text)``."""
    try:
        from pm4py_ucm.visualization.ucm.family_grid import render_svg
        if not getattr(_family, "cells", None):
            return None, "no cells to render"
        return render_svg(_family, style), None
    except Exception as exc:  # pragma: no cover - depends on env
        return None, f"{type(exc).__name__}: {exc}"


def _heat_styler(df, formats=None):
    """Column-wise sequential heatmap for ``st.dataframe`` without a
    matplotlib dependency (same blue ramp as the HTML report). Falls
    back to the plain frame when pandas Styler is unavailable.

    Every colored cell sets an explicit text color chosen by the
    background's relative luminance — the app may run in Streamlit's
    dark theme, where the default (white) text is unreadable on the
    light end of the ramp."""
    lo_rgb, hi_rgb = (247, 251, 255), (33, 102, 172)

    def color_column(col):
        vals = pd.to_numeric(col, errors="coerce")
        lo, hi = vals.min(), vals.max()
        out = []
        for v in vals:
            if pd.isna(v) or pd.isna(lo) or pd.isna(hi) or hi <= lo:
                out.append("")
                continue
            t = (v - lo) / (hi - lo)
            rgb = tuple(
                round(a + (b - a) * t) for a, b in zip(lo_rgb, hi_rgb)
            )
            lum = (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2])
            fg = "white" if lum < 140 else "#1a2733"
            out.append(f"background-color: rgb{rgb}; color: {fg};")
        return out

    try:
        styler = df.style.apply(color_column, axis=0)
        if formats:
            styler = styler.format(formats, na_rep="—")
        return styler
    except Exception:  # pragma: no cover - Styler needs jinja2
        return df


def _open_image_in_tab_button(png_b64: str, label: str = "Open image "
                              "in new tab ⧉", height: int = 46) -> None:
    """A button-styled link that opens an embedded PNG in its own
    browser tab at full resolution — much easier to zoom and pan
    complex models than the inline preview. A ``data:`` URI cannot be
    a top-level tab, so the embedded JS converts the base64 to a Blob
    URL **at render time** and sets it as a plain anchor ``href``
    with ``target="_blank"`` — ordinary link navigation, so no popup
    blocker is involved (unlike ``window.open``). Runs inside the
    ``components.html`` iframe, whose sandbox allows popups escaping
    to a new tab.

    The component also installs — once per page, in the PARENT page's
    own JS realm so it survives component reloads — a delegated
    double-click listener: any ``<img data-opentab="1">`` on the page
    opens in a new tab the same way. The handler reads the image's
    *current* ``src`` at click time, so re-rendered models never open
    stale; a delegated document-level listener survives Streamlit
    re-rendering the ``st.markdown`` image element on every rerun."""
    _embed_html(
        f"""
<a id="ot" target="_blank" rel="noopener"
  style="font-family: 'Source Sans Pro', sans-serif; font-size: 0.9rem;
  display: block; box-sizing: border-box; text-align: center;
  text-decoration: none; padding: 0.45rem 0.9rem; border-radius: 0.5rem;
  border: 1px solid rgba(49, 51, 63, 0.2); background: white;
  color: rgb(49, 51, 63); cursor: pointer; width: 100%;"
  onmouseover="this.style.borderColor='#ff4b4b';this.style.color='#ff4b4b'"
  onmouseout="this.style.borderColor='rgba(49,51,63,.2)';this.style.color='rgb(49,51,63)'"
>{label}</a>
<script>
(() => {{
  const bin = atob("{png_b64}");
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  document.getElementById("ot").href =
    URL.createObjectURL(new Blob([bytes], {{type: "image/png"}}));

  // Double-click-to-open on marked images of the parent page. The
  // listener is injected as a <script> element so it runs in the
  // parent realm (functions from a reloaded component iframe can die
  // with it); the window flag keeps it single-instance.
  try {{
    const P = window.parent;
    if (P && P.document && !P.__pm4pyImgDblclick) {{
      P.__pm4pyImgDblclick = true;
      const s = P.document.createElement("script");
      s.textContent = "document.addEventListener('dblclick'," +
        "function(e){{var t=e.target;" +
        "if(!(t&&t.tagName==='IMG'&&t.getAttribute('data-opentab')==='1'))return;" +
        "var parts=t.src.split(',');if(parts.length<2)return;" +
        "var bin=atob(parts[1]);var b=new Uint8Array(bin.length);" +
        "for(var i=0;i<bin.length;i++)b[i]=bin.charCodeAt(i);" +
        "window.open(URL.createObjectURL(" +
        "new Blob([b],{{type:'image/png'}})),'_blank');}},true);";
      P.document.body.appendChild(s);
    }}
  }} catch (err) {{ /* cross-origin embedding: button still works */ }}
}})();
</script>""",
        height=height,
    )


def _svg_viewer(svg: str, *, height: int = 620, key: str = "svgview") -> None:
    """Show an SVG model inline with wheel-zoom and drag-to-pan.

    SVG is the on-screen default: it stays crisp at any zoom and its text
    is selectable. It is embedded through ``components.html`` (Streamlit's
    markdown sanitiser strips raw ``<svg>``) and carried base64-encoded so
    nothing in the diagram — a stray ``</script>`` in a label, a non-ASCII
    map name — can break the surrounding HTML. The SVG fits the width on
    load; the wheel zooms and dragging pans, so a tall decomposed stack is
    navigable without leaving the page.
    """
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    _embed_html(
        f"""
<style>
  html, body {{ margin: 0; height: 100%; }}
  #stage {{
    position: relative;
    width: 100%; height: {height}px; overflow: hidden;
    border: 1px solid #e2dfd8; border-radius: 8px; background: #fff;
    touch-action: none; cursor: grab;
  }}
  #stage svg {{ width: 100%; height: auto; display: block;
    transform-origin: 0 0; user-select: none; }}
  /* Dynamic-stub picker: a plug-in chooser shown at the click. */
  .pm-menu {{ position: absolute; z-index: 20; min-width: 180px;
    max-width: 320px; background: #fff; border: 1px solid #cfc9bf;
    border-radius: 8px; box-shadow: 0 6px 20px rgba(0,0,0,.18);
    overflow: hidden; font-family: Arial, sans-serif; }}
  .pm-menu-head {{ padding: 6px 10px; font-size: 11px; font-weight: 700;
    color: #6b6459; background: #f4f1ea; border-bottom: 1px solid #e2dfd8;
    text-transform: uppercase; letter-spacing: .03em; }}
  .pm-menu-item {{ padding: 7px 10px; cursor: pointer;
    border-bottom: 1px solid #f0ede6; }}
  .pm-menu-item:last-child {{ border-bottom: none; }}
  .pm-menu-item:hover {{ background: #f7f4ee; }}
  .pm-menu-label {{ font-size: 13px; color: #202020; font-weight: 600; }}
  .pm-menu-cond {{ font-size: 11px; color: #8a8478; margin-top: 2px;
    font-family: ui-monospace, Menlo, Consolas, monospace;
    white-space: pre-wrap; word-break: break-word; }}
</style>
<div id="stage"></div>
<script>
(() => {{
  const stage = document.getElementById("stage");
  // Decode as UTF-8 so accented map names survive.
  const bytes = Uint8Array.from(atob("{b64}"), c => c.charCodeAt(0));
  stage.innerHTML = new TextDecoder("utf-8").decode(bytes);
  const svg = stage.querySelector("svg");
  if (!svg) return;
  let scale = 1, tx = 0, ty = 0, drag = false, px = 0, py = 0, moved = 0;
  let menuEl = null;
  const apply = () => svg.style.transform =
    `translate(${{tx}}px,${{ty}}px) scale(${{scale}})`;
  const closeMenu = () => {{ if (menuEl) {{ menuEl.remove(); menuEl = null; }} }};
  // Pan so the target panel's top meets the viewport.
  const panTo = (href) => {{
    const target = svg.querySelector(href);
    if (!target) return;
    const tr = target.getBoundingClientRect();
    const sr = stage.getBoundingClientRect();
    ty += (sr.top - tr.top) + 10;
    apply();
  }};
  // A dynamic stub links to a hidden <g id="pm-stub-menu-…"> carrying one
  // <g class="pm-binding" data-target/label/cond> per plug-in. Show them
  // as a picker at the click; choosing one pans to that plug-in's panel.
  const showMenu = (menuHref, cx, cy) => {{
    closeMenu();
    const menu = svg.querySelector(menuHref);
    if (!menu) return;
    const rows = menu.querySelectorAll(".pm-binding");
    if (!rows.length) return;
    const el = document.createElement("div");
    el.className = "pm-menu";
    const sr = stage.getBoundingClientRect();
    el.style.left = Math.min(Math.max(4, cx - sr.left), sr.width - 190) + "px";
    el.style.top = Math.max(4, cy - sr.top) + "px";
    const head = document.createElement("div");
    head.className = "pm-menu-head";
    const stub = menu.getAttribute("data-stub") || "";
    head.textContent = "Go to plug-in" + (stub ? ": " + stub : "");
    el.appendChild(head);
    rows.forEach((r) => {{
      const item = document.createElement("div");
      item.className = "pm-menu-item";
      const lab = document.createElement("div");
      lab.className = "pm-menu-label";
      lab.textContent = r.getAttribute("data-label") || "plug-in";
      item.appendChild(lab);
      const cond = r.getAttribute("data-cond");
      if (cond) {{
        const c = document.createElement("div");
        c.className = "pm-menu-cond";
        c.textContent = "[" + cond + "]";
        item.appendChild(c);
      }}
      item.addEventListener("click", (ev) => {{
        ev.stopPropagation();
        const t = r.getAttribute("data-target");
        closeMenu();
        if (t) panTo(t);
      }});
      el.appendChild(item);
    }});
    stage.appendChild(el);
    menuEl = el;
  }};
  stage.addEventListener("wheel", (e) => {{
    e.preventDefault();
    closeMenu();
    const f = e.deltaY < 0 ? 1.1 : 1 / 1.1;
    scale = Math.min(12, Math.max(0.2, scale * f));
    apply();
  }}, {{ passive: false }});
  stage.addEventListener("pointerdown", (e) => {{
    if (e.target.closest && e.target.closest(".pm-menu")) return;
    closeMenu();
    drag = true; moved = 0; px = e.clientX; py = e.clientY;
    stage.setPointerCapture(e.pointerId); stage.style.cursor = "grabbing";
  }});
  stage.addEventListener("pointermove", (e) => {{
    if (!drag) return;
    const dx = e.clientX - px, dy = e.clientY - py;
    moved += Math.abs(dx) + Math.abs(dy);
    tx += dx; ty += dy; px = e.clientX; py = e.clientY;
    apply();
  }});
  const stop = () => {{ drag = false; stage.style.cursor = "grab"; }};
  stage.addEventListener("pointerup", stop);
  stage.addEventListener("pointerleave", stop);
  // Click a stub / sub-process to navigate. graphviz emits an SVG anchor;
  // a static stub links #pm-map-N (pan to that panel), a dynamic stub
  // links #pm-stub-menu-N (open the plug-in picker). Suppressed after a
  // drag so panning never triggers a jump.
  stage.addEventListener("click", (e) => {{
    if (e.target.closest && e.target.closest(".pm-menu")) return;
    if (moved > 6) return;
    const a = e.target.closest && e.target.closest("a");
    if (!a) return;
    const href = a.getAttribute("xlink:href") || a.getAttribute("href") || "";
    if (href.startsWith("#pm-stub-menu-")) {{
      e.preventDefault();
      showMenu(href, e.clientX, e.clientY);
    }} else if (href.startsWith("#pm-map-")) {{
      e.preventDefault();
      panTo(href);
    }}
  }});
}})();
</script>""",
        height=height + 6,
    )


class _ProgressUI:
    """A ``progress_callback(stage, done, total)`` that renders a
    progress bar with a remaining-time estimate inside an
    ``st.status`` container.

    The long pipeline loops (case replay, per-cell family mining,
    umbrella replay, family statistics) accept this callback and fire
    it with known totals, so the bar shows genuine fractions rather
    than a spinner. Repaints are throttled to ~3/second — every
    update is a websocket message, and unthrottled per-item updates
    would slow down the very work being measured. Pass instances into
    the cached miners under a leading-underscore parameter so they
    stay out of the cache keys (same convention as ``_status``)."""

    def __init__(self, container) -> None:
        self._container = container
        self._bar = None
        self._stage: Optional[str] = None
        self._t0 = 0.0
        self._last_paint = 0.0

    def __call__(self, stage: str, done: int, total: int) -> None:
        import time as _time
        now = _time.time()
        if stage != self._stage:
            self._stage, self._t0 = stage, now
            self._last_paint = 0.0
        if done < total and now - self._last_paint < 0.35:
            return
        self._last_paint = now
        frac = (done / total) if total else 1.0
        text = f"{stage} — {done:,}/{total:,}"
        if 0 < done < total:
            elapsed = now - self._t0
            remaining = elapsed * (total - done) / done
            text += f" · about {_fmt_duration_s(remaining)} left"
        if self._bar is None:
            self._bar = self._container.progress(
                min(1.0, frac), text=text,
            )
        else:
            self._bar.progress(min(1.0, frac), text=text)


def _fmt_duration_s(seconds) -> str:
    """Humanized duration for Streamlit tables (mirrors the report)."""
    if seconds is None or pd.isna(seconds):
        return "—"
    s = abs(float(seconds))
    sign = "-" if seconds < 0 else ""
    if s < 60:
        return f"{sign}{s:.0f}s"
    if s < 3600:
        return f"{sign}{s / 60:.1f}m"
    if s < 86400:
        return f"{sign}{s / 3600:.1f}h"
    days = s / 86400
    if days >= 500:
        return f"{sign}{days / 365.25:.1f}y"
    return f"{sign}{days:.0f}d" if days >= 100 else f"{sign}{days:.1f}d"


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

# The rail is this app's primary navigation, not an optional settings
# drawer — it must never open collapsed, whatever the viewport.
st.set_page_config(
    page_title="PM4Py-UCM", layout="wide", initial_sidebar_state="expanded",
)

# Which theme Streamlit is actually rendering. Asking Streamlit beats
# reading the OS preference: the app can be set to dark on a light
# machine (or pinned in config.toml), and then the OS is the wrong
# answer. Guarded because st.context.theme is young API surface — a
# missing attribute must not take the app down, and light is what the
# design specifies.
def _active_theme() -> str:
    try:
        t = getattr(st.context, "theme", None)
        if t is not None and getattr(t, "type", None) in ("light", "dark"):
            return t.type
    except Exception:
        pass
    return "light"


_theme = _active_theme()
_dark = _theme == "dark"

# The workspace shell's paper/garnet surface, applied to Streamlit's own
# chrome. Only tokens and spacing — no structural overrides, which are
# what break on a Streamlit upgrade. The rail is the real sidebar; the
# view list below is the rail's VIEWS section.
#
# The tokens FOLLOW the active theme rather than forcing the light ones.
# Forcing them is not a cosmetic slip: Streamlit keeps its near-white
# text in dark mode, so a hard-coded paper background renders white text
# on white — 1.01:1, invisible. Every surface set here has to move with
# the theme that owns the text on top of it.
_TOKENS_LIGHT = """
        --pm-paper:#faf9f7; --pm-rail:#f1efeb; --pm-border:#e2dfd8;
        --pm-ink:#1c1b1a; --pm-muted:#8a857c; --pm-faint:#a09b91;
        --pm-garnet:#8f001a; --pm-garnet-hover:#6e0014;
        --pm-card:#ffffff; --pm-chip-bg:#f3e6e8; --pm-chip-fg:#8f001a;
        --pm-chip-n-bg:#efece7; --pm-chip-n-fg:#57534b;
        --pm-hover:#e9e6e0;
"""
# Garnet lifts to #ff6b81 on dark for the same reason it does in the
# island: #8f001a on a dark surface is 1.6:1.
_TOKENS_DARK = """
        --pm-paper:#14161b; --pm-rail:#1b1e24; --pm-border:#333842;
        --pm-ink:#f0eee9; --pm-muted:#a5a099; --pm-faint:#837e77;
        --pm-garnet:#ff6b81; --pm-garnet-hover:#ff8a9c;
        --pm-card:#1f232b; --pm-chip-bg:#3a1f26; --pm-chip-fg:#ff8095;
        --pm-chip-n-bg:#2a2f39; --pm-chip-n-fg:#c9c4bb;
        --pm-hover:#2a2f39;
"""

st.markdown(
    """
    <style>
      :root {"""
    + (_TOKENS_DARK if _dark else _TOKENS_LIGHT)
    + """
      }
      .stApp { background: var(--pm-paper); }
      section[data-testid="stSidebar"] {
        background: var(--pm-rail);
        border-right: 1px solid var(--pm-border);
      }
      section[data-testid="stSidebar"] h1,
      section[data-testid="stSidebar"] h2,
      section[data-testid="stSidebar"] h3 {
        font-family: Georgia, "Times New Roman", serif;
        font-size: 14px !important; font-weight: 600;
      }
      .pm-brand {
        font-family: Georgia, "Times New Roman", serif;
        font-weight: 700; font-size: 14px; color: var(--pm-ink);
      }
      .pm-brand span {
        font-family: ui-monospace, Menlo, Consolas, monospace;
        font-size: 10px; color: var(--pm-muted); margin-left: 6px;
      }
      /* Log card — the rail's persistent "what am I looking at". */
      .pm-log {
        background:var(--pm-card); border:1px solid var(--pm-border);
        border-radius:8px; padding:11px 12px; margin:8px 0 14px;
      }
      .pm-log__name { font-weight:600; font-size:12px; color:var(--pm-ink);
        word-break:break-all; }
      .pm-log__meta { font-family: ui-monospace, Menlo, Consolas, monospace;
        font-size:10.5px; color:var(--pm-muted); margin-top:2px; }
      .pm-log__chips { margin-top:6px; display:flex; gap:4px; flex-wrap:wrap; }
      .pm-log__chip {
        font-family: ui-monospace, Menlo, Consolas, monospace;
        font-size:9.5px; border-radius:12px; padding:1px 7px;
        background:var(--pm-chip-bg); color:var(--pm-chip-fg);
      }
      .pm-log__chip--n {
        background:var(--pm-chip-n-bg); color:var(--pm-chip-n-fg);
      }
      .pm-viewhead {
        font-family: Georgia, "Times New Roman", serif;
        font-weight:600; font-size:14px; color:var(--pm-ink);
        border-bottom:1px solid var(--pm-border);
        padding-bottom:8px; margin-bottom:12px;
      }
      /* The rail's view list: radio rendered as the design's rows. */
      section[data-testid="stSidebar"] div[role="radiogroup"] > label {
        border-radius:7px; padding:3px 8px; margin-bottom:1px;
      }
      section[data-testid="stSidebar"] div[role="radiogroup"] > label:hover {
        background:var(--pm-hover);
      }
      section[data-testid="stSidebar"] div[role="radiogroup"] input:checked
        + div { font-weight:600; }
      div[data-testid="stSidebarUserContent"] { padding-top: 10px; }
    </style>
    """,
    unsafe_allow_html=True,
)

# Show the ACTUAL running package version (not the latest GitHub
# release): the deployment imports the checkout's ``pm4py_ucm``, so this
# reflects exactly what is executing — and surfaces immediately if the
# environment ever serves a stale build. Links to that version's release
# notes (the version constant is bumped in the release commit, so the
# ``v<version>`` tag exists for any published version).
_version = getattr(pm4py_ucm, "__version__", "unknown")
_version_text = (
    f"Running [pm4py-ucm {_version}]"
    "(https://github.com/ProcessMining-uOttawa/pm4py-ucm/releases/tag/"
    f"v{_version})"
)


def _accept_log_bytes(name: str, payload: bytes) -> None:
    new_hash = hashlib.sha256(payload).hexdigest()[:16]
    if new_hash == st.session_state.get("log_hash"):
        return
    name_lower = name.lower()
    if name_lower.endswith(".csv"):
        kind = "csv"
    elif name_lower.endswith(".zip"):
        kind = "zip"
    else:
        kind = "xes"
    st.session_state["log_bytes"] = payload
    st.session_state["log_name"] = name
    st.session_state["log_hash"] = new_hash
    st.session_state["log_kind"] = kind
    st.session_state.pop("applied_csv_columns", None)
    st.session_state.pop("csv_seeded_for_hash", None)
    # A different log invalidates the mined family — otherwise the
    # Family/Compare tabs keep showing the PREVIOUS log's results
    # (the in-tab fingerprint check does not run when e.g. attribute
    # detection fails on the new log).
    st.session_state.pop("family_fp", None)
    st.session_state.pop("family_result", None)
    if kind == "csv":
        for k, _, _ in _CSV_AUTOPICK:
            st.session_state.pop(k, None)


# Sidebar — miner config (decomposition affects the Model tab only; the
# Scenarios tab always runs flat).
with st.sidebar:
    # Brand at the top of the rail — the app's name and the version that
    # is actually running (see ``_version`` above). The design carries
    # the identity here rather than a main-area title.
    st.markdown(
        f'<div class="pm-brand">PM4Py-UCM<span>{_html_escape_min(_version)}'
        f'</span></div>',
        unsafe_allow_html=True,
    )
    st.header("Inductive miner")
    noise_threshold = st.slider(
        "Noise threshold", min_value=0.0, max_value=1.0,
        value=0.2, step=0.05,
        help=(
            "IMf threshold. 0.0 = classic Inductive Miner. "
            "0.2 is a common practical default."
        ),
    )

    st.subheader("Decomposition (Model tab only)")
    decomposition_preset = st.selectbox(
        "Decomposition",
        options=["off", "auto", "aggressive"],
        index=0,
        help=(
            "Splits the Model tab's UCM into a root map + plug-ins. "
            "The Scenarios tab always runs flat (decomposition=None) "
            "so every OR-fork can receive a variant_id condition."
        ),
    )
    decomposition_overrides: Dict[str, Any] = {}
    if decomposition_preset != "off":
        from pm4py_ucm.objects.ucm.conversion.decomposition import (
            AUTO_DEFAULTS, AGGRESSIVE_DEFAULTS,
        )
        # V3-specific overrides on the package presets: tighter caps
        # than the API defaults so the multi-map UCMs the web preview
        # renders stay readable on screen. The package's own defaults
        # remain unchanged.
        if decomposition_preset == "auto":
            _preset_defaults = dict(
                AUTO_DEFAULTS,
                max_leaves_per_map=8,
                min_leaves_to_decompose=4,
            )
        else:
            _preset_defaults = dict(
                AGGRESSIVE_DEFAULTS,
                max_leaves_per_map=6,
                min_leaves_to_decompose=3,
            )
        with st.expander("Advanced", expanded=False):
            kp = f"decomp_{decomposition_preset}_"
            for key, label, help_txt in [
                ("on_root_sequence", "on_root_sequence",
                 "Each child of a top-level sequence becomes a plug-in."),
                ("on_parallel", "on_parallel",
                 "Each + branch becomes a plug-in."),
                ("on_alternative", "on_alternative",
                 "Each XOR branch becomes a plug-in."),
                ("on_loop", "on_loop",
                 "Each loop expansion becomes a plug-in."),
            ]:
                decomposition_overrides[key] = st.checkbox(
                    label, value=bool(_preset_defaults[key]),
                    key=kp + key, help=help_txt,
                )
            decomposition_overrides["max_leaves_per_map"] = st.number_input(
                "max_leaves_per_map",
                min_value=1, max_value=500,
                value=int(_preset_defaults["max_leaves_per_map"]),
                step=1, key=kp + "mx",
            )
            decomposition_overrides["min_leaves_to_decompose"] = st.number_input(
                "min_leaves_to_decompose",
                min_value=1, max_value=100,
                value=int(_preset_defaults["min_leaves_to_decompose"]),
                step=1, key=kp + "mn",
            )
            decomposition_overrides["balance_ratio"] = st.slider(
                "balance_ratio",
                min_value=0.0, max_value=1.0,
                value=float(_preset_defaults["balance_ratio"]),
                step=0.05, key=kp + "br",
            )

    if decomposition_preset == "off":
        candidate_spec: object = "off"
    else:
        candidate_spec = tuple(sorted(decomposition_overrides.items()))
    if "applied_decomp" not in st.session_state:
        st.session_state["applied_decomp"] = candidate_spec
    if candidate_spec != st.session_state["applied_decomp"]:
        # Applying must NOT st.rerun(): a rerun aborts the script
        # before every widget below this point (the rest of the
        # sidebar — resource attribute, overlays, the Notation radio —
        # and the whole main body) is instantiated, and Streamlit
        # DROPS the state of widgets skipped in a run. That is what
        # silently flipped the Notation radio back to UCM whenever a
        # decomposition change was applied. Updating the session value
        # and falling through lets THIS run continue with the new
        # spec; the button disappears by itself on the next run.
        if st.button("Apply changes", type="primary"):
            st.session_state["applied_decomp"] = candidate_spec
        else:
            st.warning("Decomposition has unapplied changes.")

    st.subheader("Performers")
    _RES_BUILTIN = ["org:role", "org:resource"]
    _RES_OTHER = "Other..."
    resource_choice = st.selectbox(
        "Resource attribute",
        options=_RES_BUILTIN + [_RES_OTHER, "(none)"],
        index=0,
    )
    if resource_choice == _RES_OTHER:
        resource_attribute = st.text_input(
            "Custom attribute(s)",
            value="org:role, org:resource, org:group",
        )
    elif resource_choice == "(none)":
        resource_attribute = ""
    else:
        resource_attribute = resource_choice
    _min_support_disabled = not resource_attribute.strip()
    min_support = st.slider(
        "Min support", min_value=0.0, max_value=1.0,
        value=0.0, step=0.05,
        disabled=_min_support_disabled,
    )

    st.subheader("Performance overlay")
    from pm4py_ucm.algo.performance import (
        EDGE_METRICS as _EDGE_METRICS,
        NODE_METRICS as _NODE_METRICS,
    )
    overlay_nodes = tuple(st.multiselect(
        "On activities (max 2)",
        options=list(_NODE_METRICS), default=[],
        help=(
            "frequency = executions; case_coverage = cases containing "
            "the activity; relative_frequency = share of all events; "
            "repeat_frequency = repeat executions (rework); the "
            "mean/median/min/max/std/p90/p95/total_time metrics are "
            "activity service times and need an interval log "
            "(start_timestamp column); the sojourn_* metrics are the "
            "time since the case's previous event (≈ waiting + service) "
            "and work on any timestamped log. Every available metric is "
            "written to the .jucm as jUCMNav metadata regardless of the "
            "≤2 shown on the diagram. See docs/metrics.md."
        ),
    )[:2])
    overlay_edges = tuple(st.multiselect(
        "On edges (max 2)",
        options=list(_EDGE_METRICS), default=[],
        help=(
            "frequency = directly-follows traversals; case_frequency = "
            "distinct cases traversing the handover; relative_frequency "
            "= share of all traversals; percentage = an OR-fork branch's "
            "share of the fork; the time metrics are waiting times "
            "between the edge's activities. case_frequency, "
            "relative_frequency and the shape aggregates (median/std/"
            "p90/p95) apply to single-pair segments; min/max and mean/"
            "total also aggregate across fork-after-join segments."
        ),
    )[:2])

    st.divider()
    notation = st.radio(
        "Notation (Model tab)",
        options=["UCM", "BPMN"], index=0,
    )


# ---- Log source ------------------------------------------------------------
samples = _list_samples()
src_tabs = (
    st.tabs(["Sample log", "Upload your own"])
    if samples else (None, st.container())
)

if samples:
    with src_tabs[0]:
        def _label(p: Path) -> str:
            stem = p.name
            for suffix in (".xes.gz", ".xes", ".zip", ".gz"):
                if stem.lower().endswith(suffix):
                    stem = stem[: -len(suffix)]
                    break
            return stem.replace("_", " ").strip() or p.name

        label_to_path = {_label(p): p for p in samples}
        labels = list(label_to_path.keys())
        st.selectbox("Choose a bundled log", options=labels, key="sample_choice")
        if st.button("Load sample", type="primary", key="load_sample"):
            chosen = label_to_path[st.session_state["sample_choice"]]
            _accept_log_bytes(chosen.name, chosen.read_bytes())
            st.rerun()

# Upload-size policy — see .streamlit/config.toml. The server-side
# cap is 1 GB; on Streamlit Community Cloud we additionally enforce a
# 75 MB app-level limit to keep a single upload from DoS'ing a public
# demo. Local (and self-hosted) runs get the full 1 GB.
_CLOUD_MAX_UPLOAD_BYTES = 75 * 1024 * 1024


def _running_on_streamlit_cloud() -> bool:
    """Best-effort detection of Streamlit Community Cloud.

    Community Cloud sets ``HOSTNAME`` to something like
    ``streamlit-abc123`` and runs apps from ``/mount/src/…``. Either
    fingerprint alone is enough to trigger the tighter cap; both being
    absent means we're on someone's laptop / private VM and the 1 GB
    server-side cap applies unchanged."""
    if os.environ.get("HOSTNAME", "").startswith("streamlit"):
        return True
    return os.path.isdir("/mount/src")


uploaded = src_tabs[1].file_uploader(
    "Upload an event log",
    type=["xes", "gz", "csv", "zip"],
    key="log_uploader",
    help=(
        "Upload cap: 1 GB locally, 75 MB on the public Community "
        "Cloud deployment."
    ),
)
# Streamlit's file_uploader remembers the last upload across reruns,
# so once a file has been dropped in, ``uploaded`` keeps returning
# that same UploadedFile on every subsequent rerun -- including ones
# triggered by clicking "Load sample". Without the guard below, the
# sample we just loaded gets immediately overwritten by the
# (still-attached) previous upload: every condition_strategy click,
# slider tweak, or sample-tab switch resurrects the old log. We
# fingerprint the uploaded payload and only re-apply it when its
# content actually changes (i.e. a genuinely new upload).
if uploaded is not None:
    _uploaded_bytes = uploaded.getvalue()
    _uploaded_hash = hashlib.sha256(_uploaded_bytes).hexdigest()[:16]
    if st.session_state.get("last_uploader_hash") != _uploaded_hash:
        if _running_on_streamlit_cloud() and len(_uploaded_bytes) > _CLOUD_MAX_UPLOAD_BYTES:
            st.error(
                f"This upload is {len(_uploaded_bytes) / (1024 * 1024):.1f} MB, "
                f"which exceeds the 75 MB cap enforced on the public "
                "Community Cloud deployment. Run pm4py-ucm locally "
                "(`streamlit run web/streamlit_app_v3.py`) to process "
                "files up to 1 GB."
            )
            st.stop()
        _accept_log_bytes(uploaded.name, _uploaded_bytes)
        st.session_state["last_uploader_hash"] = _uploaded_hash

if "log_bytes" not in st.session_state:
    st.info("Upload a log to begin.")
    st.stop()

log_bytes = st.session_state["log_bytes"]
log_name = st.session_state["log_name"]
log_kind = st.session_state["log_kind"]
file_hash = st.session_state["log_hash"]
style = notation.lower()

# ---- CSV column mapping ----------------------------------------------------
csv_columns: Optional[Tuple[str, str, str, str, str]] = None
if log_kind == "csv":
    columns = _csv_columns(log_bytes, file_hash)
    if not columns:
        st.error("Could not read columns from the uploaded CSV.")
        st.stop()
    st.subheader("CSV columns")
    if st.session_state.get("csv_seeded_for_hash") != file_hash:
        _seed_csv_selectors(columns)
        st.session_state["csv_seeded_for_hash"] = file_hash
    cc1, cc2, cc3 = st.columns(3)
    case_col = cc1.selectbox("Case id column", options=columns, key="csv_case")
    activity_col = cc2.selectbox("Activity column", options=columns, key="csv_activity")
    ts_col = cc3.selectbox("Timestamp column", options=columns, key="csv_timestamp")
    cc4, cc5 = st.columns(2)
    role_col = cc4.selectbox(
        "Role column (optional)",
        options=[_NONE_OPT] + columns, key="csv_role",
    )
    resource_col = cc5.selectbox(
        "Resource column (optional)",
        options=[_NONE_OPT] + columns, key="csv_resource",
    )
    candidate_csv_columns = (
        case_col, activity_col, ts_col,
        "" if role_col == _NONE_OPT else role_col,
        "" if resource_col == _NONE_OPT else resource_col,
    )
    applied_csv_columns = st.session_state.get("applied_csv_columns")
    if applied_csv_columns is None:
        st.info("Review the column mapping above, then click "
                "**Apply column mapping** to start mining.")
        if st.button("Apply column mapping", type="primary",
                     key="apply_csv_initial"):
            st.session_state["applied_csv_columns"] = candidate_csv_columns
            st.rerun()
        st.stop()
    elif applied_csv_columns != candidate_csv_columns:
        st.warning("Column mapping has unapplied changes.")
        if st.button("Apply column mapping", type="primary",
                     key="apply_csv_update"):
            st.session_state["applied_csv_columns"] = candidate_csv_columns
            st.rerun()
    csv_columns = st.session_state["applied_csv_columns"]

effective_min_support = 0.0 if _min_support_disabled else min_support
decomposition_spec = st.session_state["applied_decomp"]

# ---- Mine UCM (for Model tab) ----------------------------------------------
try:
    with st.status("Mining UCM...", expanded=True) as status:
        mined = _mine(
            log_bytes, log_kind, csv_columns,
            decomposition_spec, resource_attribute,
            effective_min_support, noise_threshold,
            overlay_nodes, overlay_edges,
            file_hash, _status=status,
            _progress=_ProgressUI(status),
        )
        status.update(label="Done.", state="complete")
except Exception as exc:
    st.error(f"Mining failed: {type(exc).__name__}: {exc}")
    with st.expander("Show technical details"):
        st.code(traceback.format_exc(), language="text")
    st.stop()

# ---- Family staleness, hoisted out of the Family view ----------------------
# V3 rendered every tab body on every rerun, so the Family tab's
# staleness check ran even while you were looking at Compare. V4 renders
# one view at a time, so the half of that check which depends on
# always-rendered sidebar settings has to run out here — otherwise
# changing the noise threshold while on Compare would leave it showing a
# family mined with the old one.
#
# Only that half: the family-specific settings (attributes, min cases,
# bins) live inside the Family view and cannot change while you are
# elsewhere, so the Family view keeps its own full-fingerprint check.
# ``family_fp`` is deliberately left in place — the Family view compares
# against it to tell "settings changed, re-mine" apart from "nothing
# mined yet".
_family_base_fp = _arg_fingerprint(
    file_hash, log_kind, csv_columns, noise_threshold, decomposition_spec,
    resource_attribute, effective_min_support, overlay_nodes, overlay_edges,
)
if st.session_state.get("family_base_fp") not in (None, _family_base_fp):
    st.session_state.pop("family_result", None)
st.session_state["family_base_fp"] = _family_base_fp

# ---- Rail: the log card and the VIEWS list ---------------------------------
_VIEWS = ["Model", "Scenarios", "Family", "Compare", "Dashboards"]

with st.sidebar:
    st.markdown("---")
    _n_attrs = 0
    try:
        _n_attrs = len(_detect_family_attributes(
            log_bytes, log_kind, csv_columns, file_hash))
    except Exception:
        # Attribute detection is best-effort context for the log card;
        # a log without usable case attributes is normal, and a failure
        # here must never stop the app from rendering.
        pass
    _chips = ""
    if resource_attribute:
        _chips += (f'<span class="pm-log__chip">{_html_escape_min(resource_attribute)}'
                   f' ✓</span>')
    _chips += (f'<span class="pm-log__chip pm-log__chip--n">{_n_attrs} case '
               f'attr{"s" if _n_attrs != 1 else ""}</span>')
    st.markdown(
        f'<div class="pm-log">'
        f'<div class="pm-log__name">{_html_escape_min(log_name)}</div>'
        f'<div class="pm-log__meta">{mined["n_cases"]:,} cases &nbsp;·&nbsp; '
        f'{mined["n_events"]:,} events</div>'
        f'<div class="pm-log__chips">{_chips}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div style="font-weight:700;font-size:10px;letter-spacing:.08em;'
        'color:var(--pm-faint);text-transform:uppercase;margin-bottom:2px">'
        'Views</div>',
        unsafe_allow_html=True,
    )
    # A view switch requested from elsewhere in the page (Pin to
    # dashboard) arrives as `goto_view` and is consumed HERE, before the
    # radio exists. Streamlit forbids writing a widget's own key once the
    # widget has been instantiated, and every such request comes from a
    # control rendered *below* this one — so the request cannot be the
    # key itself, and it cannot be applied any later than this.
    if "goto_view" in st.session_state:
        _requested = st.session_state.pop("goto_view")
        if _requested in _VIEWS:
            st.session_state["view"] = _requested
    _view = st.radio(
        "Views", _VIEWS, label_visibility="collapsed", key="view",
    )

# ---- Views -----------------------------------------------------------------
st.markdown(
    f'<div class="pm-viewhead">{_view}</div>', unsafe_allow_html=True,
)

# ===== Model view ==========================================================
if _view == "Model":
    st.subheader("Mine a Use Case Map model")
    st.caption(
        "Inductive-mine a Use Case Map (UCM) from the loaded event log, "
        "preview it in UCM or BPMN notation (click a stub to navigate its "
        "sub-maps), and download the vector SVG, a raster PNG, or the "
        "jUCMNav `.jucm`. Synthesize executable scenarios and mine "
        "attribute-partitioned model families in the other views. "
        f"{_version_text} — by [Daniel Amyot](https://damyot.github.io/), "
        "University of Ottawa, Canada."
    )
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Activities", f"{mined['n_activities']:,}")
    m2.metric("Notation", notation)
    m3.metric("Decomposition", decomposition_preset)
    m4.metric("Maps", mined["n_maps"])
    m5.metric("Nodes", mined["n_nodes"])

    # SVG is the default on-screen render: vector, crisp at any zoom, with
    # selectable text, and cheap (0.1–0.4 s even for a decomposed stack).
    # PNG is no longer rendered proactively — it is a download prepared on
    # demand below.
    try:
        with st.spinner(f"Rendering {notation} diagram..."):
            _svg = _render_svg_cached(mined["jucm"], style)
        _svg_err = None
    except Exception as _exc:
        _svg = None
        _svg_err = f"{type(_exc).__name__}: {_exc}"

    if _svg is not None:
        _svg_viewer(_svg, height=620)
        st.caption(
            f"Mined model ({notation}, "
            f"decomposition={decomposition_preset}) — vector SVG; scroll "
            "to zoom, drag to pan. Download SVG or a raster PNG below."
        )
    else:
        # SVG failed for some reason — fall back to a PNG so the model is
        # still visible, and say why.
        try:
            _png_fallback = _render_cached(mined["jucm"], style)
            st.markdown(
                f'<img src="data:image/png;base64,'
                f'{base64.b64encode(_png_fallback).decode("ascii")}" '
                f'width="{_DISPLAY_WIDTH_PX}" '
                f'style="max-width:100%; height:auto; cursor: zoom-in;" '
                f'data-opentab="1" alt="Mined {notation} model" />',
                unsafe_allow_html=True,
            )
            st.caption(f"SVG render failed ({_svg_err}); showing PNG.")
        except Exception as exc:
            st.error(f"Render failed: {type(exc).__name__}: {exc}")
            with st.expander("Show technical details"):
                st.code(traceback.format_exc(), language="text")
            st.stop()

    _svg_ok = _svg is not None
    d1, d2, d3, d4 = st.columns(4)

    # Download SVG.
    d1.download_button(
        "Download SVG",
        data=_svg.encode("utf-8") if _svg_ok else b"",
        file_name=_safe_download_name(Path(log_name).stem, ".svg"),
        mime="image/svg+xml",
        disabled=not _svg_ok,
        key="model_svg_download",
        help="Vector — crisp at any zoom, with selectable text.",
    )

    # Download PNG — rendered ONLY when asked, since SVG is the default
    # display. First press renders (with a spinner) and stashes it; the
    # button then becomes the actual download. Keyed by model + notation
    # so switching either re-arms it.
    _png_key = f"model_png::{file_hash}::{style}"
    with d2:
        if _png_key in st.session_state:
            st.download_button(
                "Download PNG", data=st.session_state[_png_key],
                file_name=_safe_download_name(Path(log_name).stem, ".png"),
                mime="image/png", key="model_png_download",
            )
        elif st.button("Prepare PNG…", key="model_png_prepare",
                       help="Render a raster PNG to download (SVG is the "
                            "default; PNG is generated only when you ask)."):
            with st.spinner(f"Rendering {notation} PNG..."):
                try:
                    st.session_state[_png_key] = _render_cached(
                        mined["jucm"], style)
                except Exception as exc:
                    st.warning(f"PNG render failed: {exc}")
            st.rerun()

    d3.download_button(
        "Download .jucm (no scenarios)", data=mined["jucm"],
        file_name=_safe_download_name(Path(log_name).stem, ".jucm"),
        mime="application/xml",
    )
    with d4.popover("Pin to dashboard ▦", use_container_width=True):
        st.caption(
            "Adds the model to the Dashboards view as a widget. The pin "
            "is live: it renders whatever the model currently is, so it "
            "follows a re-mine rather than freezing today's picture."
        )
        _pin_title = st.text_input(
            "Widget title", value=f"Mined model — {Path(log_name).stem}",
            key="pin_title",
        )
        if st.button("Pin ▦", type="primary", key="pin_go"):
            # The island cannot be reached directly — components.html is
            # one-way — so the request travels in its config on the next
            # rerun. The id must be fresh per click: the config is re-sent
            # on every rerun, and the island skips ids it has applied.
            st.session_state["pending_pin"] = {
                "id": uuid.uuid4().hex,
                "spec": {
                    "id": f"pin-{uuid.uuid4().hex[:8]}",
                    "title": _pin_title or "Mined model",
                    # A model widget carries no measurement of its own;
                    # the metric is required by the spec shape, and the
                    # renderer short-circuits before computing it.
                    "metric": "duration",
                    "viz": "model",
                },
            }
            st.session_state["goto_view"] = "Dashboards"
            st.rerun()

# ===== Scenarios view =====================================================
if _view == "Scenarios":
    st.subheader("Synthesize executable scenarios")
    st.caption(
        "Concurrency-aware variant clustering on the discovered "
        "process tree → one ScenarioDef per variant on the URN model, "
        "attached to the UCM built with the sidebar's decomposition "
        "setting."
    )

    # Detect sklearn so we can grey out data-driven cleanly.
    try:
        import sklearn  # noqa: F401
        _has_sklearn = True
    except ImportError:
        _has_sklearn = False

    cfg_left, cfg_right = st.columns([2, 1])
    with cfg_left:
        if _has_sklearn:
            strategy_opts = ["variant", "data-driven"]
            strategy_help = (
                "**variant**: lossless — each scenario replays its "
                "variant exactly. "
                "**data-driven**: mine a DecisionTreeClassifier per "
                "outside-loop XOR over case attributes; arc "
                "conditions become business-readable rules."
            )
        else:
            strategy_opts = ["variant"]
            strategy_help = (
                "**variant**: lossless. **data-driven** requires "
                "`scikit-learn` (not installed in this environment)."
            )
        condition_strategy = st.radio(
            "Condition strategy",
            options=strategy_opts,
            index=0,
            help=strategy_help,
            horizontal=True,
            key="cond_strategy",
        )
        group_name = st.text_input(
            "Scenario group name", value="MinedScenarios",
            help="Becomes the <scenarioGroups name=…> attribute in the .jucm.",
        )
    with cfg_right:
        max_loop_iterations = st.slider(
            "max_loop_iterations", min_value=1, max_value=10,
            value=2, step=1,
            help=(
                "Per-variant cap on the loop counter initialisation "
                "value. Default 2 keeps scenarios short to step through "
                "in jUCMNav even when the underlying trace ran the "
                "loop dozens of times."
            ),
        )
        decision_tree_max_depth = st.slider(
            "decision_tree_max_depth", min_value=1, max_value=6,
            value=3, step=1,
            disabled=(condition_strategy != "data-driven"),
            help=(
                "Per-OR-fork DecisionTreeClassifier max depth. Higher "
                "captures more nuance at the cost of less readable "
                "expressions. Used only in data-driven mode."
            ),
        )

    run = st.button(
        "Synthesize scenarios", type="primary", key="run_synth",
    )

    # Re-show cached results across reruns by stashing the synthesis
    # arg fingerprint and the result dict in session_state. CRITICAL:
    # the displayed result and the download bytes must always reflect
    # the CURRENT widget state. If the user changes the strategy
    # radio (or any other input) after a run, we must invalidate the
    # stashed result rather than show last run's bytes under a
    # download button labelled with the new strategy. (Previous bug:
    # data-driven download was serving the cached variant-driven
    # bytes because the result was kept and the filename was rebuilt
    # from the new radio value.)
    _synth_fp = _arg_fingerprint(
        file_hash, log_kind, csv_columns, noise_threshold,
        condition_strategy, max_loop_iterations,
        decision_tree_max_depth, group_name,
        decomposition_spec, resource_attribute,
        effective_min_support,
    )

    stashed_fp = st.session_state.get("synth_fp")
    params_changed = stashed_fp is not None and stashed_fp != _synth_fp
    if params_changed and not run:
        # Drop the stale result so the rest of the tab falls back to
        # the "configure + click" state.
        st.session_state.pop("synth_fp", None)
        st.session_state.pop("synth_result", None)

    if run:
        try:
            with st.status("Synthesizing scenarios...",
                           expanded=True) as status:
                synth = _synthesize(
                    log_bytes, log_kind, csv_columns,
                    noise_threshold, condition_strategy,
                    max_loop_iterations, decision_tree_max_depth,
                    group_name, decomposition_spec,
                    resource_attribute, effective_min_support,
                    file_hash, _status=status,
                    _progress=_ProgressUI(status),
                )
                status.update(label="Done.", state="complete")
            st.session_state["synth_fp"] = _synth_fp
            st.session_state["synth_result"] = synth
        except Exception as exc:
            st.error(
                f"Scenario synthesis failed: "
                f"{type(exc).__name__}: {exc}"
            )
            with st.expander("Show technical details"):
                st.code(traceback.format_exc(), language="text")
            st.stop()

    synth = st.session_state.get("synth_result")
    if synth is None:
        if params_changed:
            st.info(
                "Settings changed since the last run — click "
                "**Synthesize scenarios** to regenerate."
            )
        else:
            st.info(
                "Configure the run above and click **Synthesize scenarios**."
            )
    else:
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Variants", synth["n_variants"])
        m2.metric("Sequence variants", synth["n_sequence_variants"])
        m3.metric(
            "Compression",
            f"{synth['compression_ratio']:.3f}"
            if synth["n_sequence_variants"] else "—",
        )
        m4.metric("Fitness", f"{synth['fitness_percentage'] * 100:.1f}%")
        m5.metric("Scenarios", synth["n_scenarios"])

        if synth["n_noise"]:
            st.warning(
                f"{synth['n_noise']} non-conforming case(s) bucketed "
                f"as noise (excluded from variants)."
            )

        st.subheader("Variants")
        st.dataframe(synth["variants_df"], use_container_width=True)

        if synth["condition_df"] is not None:
            st.subheader("Condition mining (per OR-fork)")
            st.caption(
                "Per-XOR DecisionTreeClassifier accuracy and the "
                "post-minimisation expression emitted on each branch. "
                "`skipped_reason=inside_loop` marks forks the "
                "data-driven path cannot disambiguate from case-level "
                "attributes."
            )
            st.dataframe(synth["condition_df"], use_container_width=True)

        st.subheader("Downloads")
        stem = Path(log_name).stem
        d1, d2, d3, d4 = st.columns(4)
        # Filename suffix encodes strategy AND decomposition so a user
        # comparing several runs in a single folder can tell them apart.
        _suffix_bits = [condition_strategy]
        if decomposition_preset != "off":
            _suffix_bits.append(f"decomp_{decomposition_preset}")
        _jucm_suffix = "_".join(_suffix_bits)
        d1.download_button(
            "Download .jucm (scenarios)", data=synth["jucm"],
            file_name=_safe_download_name(
                f"{stem}_{_jucm_suffix}", ".jucm"
            ),
            mime="application/xml",
        )
        d2.download_button(
            "variants.csv", data=synth["variants_csv"],
            file_name=_safe_download_name(f"{stem}_variants", ".csv"),
            mime="text/csv",
        )
        d3.download_button(
            "case_variant_map.csv", data=synth["case_map_csv"],
            file_name=_safe_download_name(f"{stem}_case_variant_map", ".csv"),
            mime="text/csv",
        )
        if synth["condition_csv"] is not None:
            d4.download_button(
                "condition_mining.csv",
                data=synth["condition_csv"],
                file_name=_safe_download_name(
                    f"{stem}_condition_mining", ".csv"
                ),
                mime="text/csv",
            )
        else:
            d4.caption("_condition_mining.csv is only emitted in data-driven mode._")

# ===== Family view ========================================================
if _view == "Family":
    st.subheader("Model family by case attributes")
    st.caption(
        "Partition the log by 1–2 case-level attributes (e.g. cancer "
        "type × age group) and mine one model per combination. The "
        "sidebar's decomposition setting applies to every cell (flat "
        "vs decomposed). Outputs: a grid rendering, one .jucm per "
        "cell (zip), a combined multi-map .jucm, and an overarching "
        "model whose **dynamic stub** selects the applicable plug-in "
        "via attribute conditions, with one strategy per combination."
    )

    try:
        attr_rows = _detect_family_attributes(
            log_bytes, log_kind, csv_columns, file_hash,
        )
    except Exception as exc:
        st.error(
            f"Attribute detection failed: {type(exc).__name__}: {exc}"
        )
        with st.expander("Show technical details"):
            st.code(traceback.format_exc(), language="text")
        attr_rows = []

    if not attr_rows:
        st.info(
            "No case-constant attributes detected in this log — "
            "nothing to partition on. (Attributes must be constant "
            "within each case: patient-level fields like cancer type, "
            "age, or admission channel.)"
        )
    else:
        with st.expander("Detected case attributes", expanded=False):
            st.dataframe(
                pd.DataFrame(attr_rows), use_container_width=True,
                hide_index=True,
            )

        attr_names = [r["attribute"] for r in attr_rows]
        attr_type = {r["attribute"]: r["type"] for r in attr_rows}

        pc1, pc2 = st.columns(2)
        attr1 = pc1.selectbox(
            "First attribute", options=attr_names, key="family_attr1",
        )
        attr2 = pc2.selectbox(
            "Second attribute (optional)",
            options=[_NONE_OPT] + [a for a in attr_names if a != attr1],
            key="family_attr2",
        )
        selected_attrs: Tuple[str, ...] = (
            (attr1,) if attr2 == _NONE_OPT else (attr1, attr2)
        )

        pc3, pc4, pc5 = st.columns(3)
        family_min_cases = pc3.number_input(
            "Min cases per cell", min_value=1, max_value=100_000,
            value=10, step=1,
            help=(
                "Combinations with fewer cases are skipped (shown "
                "grayed in the grid). Models mined from a handful of "
                "traces overfit badly."
            ),
        )
        family_max_values = pc4.number_input(
            "Max values per attribute", min_value=2, max_value=20,
            value=8, step=1,
            help=(
                "Cardinality cap per axis; the least frequent values "
                "merge into an 'Other' bucket."
            ),
        )
        _any_numeric = any(
            attr_type.get(a) == "integer" for a in selected_attrs
        )
        family_bins = pc5.number_input(
            "Bins (numeric attributes)", min_value=2, max_value=10,
            value=4, step=1, disabled=not _any_numeric,
            help=(
                "Numeric attributes (e.g. age) are partitioned into "
                "this many quantile ranges. A column with at most this "
                "many distinct whole-number values (e.g. priority levels "
                "1-5) gets one bin per value instead of ranges."
            ),
        )

        # Coverage heatmap BEFORE mining — see the cell sizes before
        # committing to mining N models. A first (unfiltered) pass
        # supplies the value axes for the per-attribute value filters;
        # the displayed preview then honours the filters.
        preview = None
        family_include_values = None
        try:
            base_preview = _family_preview(
                log_bytes, log_kind, csv_columns,
                selected_attrs, int(family_min_cases),
                int(family_max_values), int(family_bins),
                None, file_hash,
            )
            filter_cols = st.columns(len(selected_attrs))
            selections: Dict[str, Tuple[str, ...]] = {}
            for i, attr in enumerate(selected_attrs):
                display = (attr[len("case:"):]
                           if attr.startswith("case:") else attr)
                options = base_preview["axes"].get(display, [])
                picked = filter_cols[i].multiselect(
                    f"Values of {display}",
                    options=options, default=options,
                    key=f"family_values_{attr}",
                    help=(
                        "Deselect values to exclude them from the "
                        "family — their cases are dropped entirely."
                    ),
                )
                if picked and len(picked) < len(options):
                    selections[attr] = tuple(picked)
            family_include_values = (
                tuple(sorted(selections.items())) if selections else None
            )
            preview = (
                base_preview if family_include_values is None
                else _family_preview(
                    log_bytes, log_kind, csv_columns,
                    selected_attrs, int(family_min_cases),
                    int(family_max_values), int(family_bins),
                    family_include_values, file_hash,
                )
            )
        except Exception as exc:
            st.error(
                f"Partitioning failed: {type(exc).__name__}: {exc}"
            )
            with st.expander("Show technical details"):
                st.code(traceback.format_exc(), language="text")

        if preview is not None:
            st.markdown("**Case coverage per combination** "
                        "(mined cells need ≥ min cases)")
            st.dataframe(preview["pivot"], use_container_width=True)
            pm1, pm2, pm3, pm4 = st.columns(4)
            pm1.metric("Cells to mine", preview["n_cells"])
            pm2.metric("Skipped (small)", preview["n_skipped"])
            cov = (
                preview["covered_cases"] / preview["total_cases"] * 100
                if preview["total_cases"] else 0.0
            )
            pm3.metric("Case coverage", f"{cov:.1f}%")
            pm4.metric("Dropped cases", preview["dropped_cases"])

            family_dedup = st.checkbox(
                "Merge behaviourally identical plug-ins (umbrella)",
                value=True,
                help=(
                    "Combinations whose mined process trees are "
                    "identical share one plug-in map; its selection "
                    "condition becomes the simplified OR of the "
                    "member conditions. The shared plug-ins show "
                    "which sub-populations follow the same process."
                ),
            )

            run_family = st.button(
                "Mine model family", type="primary", key="run_family",
                disabled=preview["n_cells"] == 0,
            )
            if preview["n_cells"] == 0:
                st.warning(
                    "No combination reaches the minimum case count — "
                    "lower 'Min cases per cell' or pick different "
                    "attributes."
                )

            # NOTE: the notation style is deliberately NOT part of
            # this fingerprint — it only affects grid rendering,
            # which has its own cache (_render_family_grid). Toggling
            # UCM ↔ BPMN must never invalidate the mined family.
            _family_fp = _arg_fingerprint(
                file_hash, log_kind, csv_columns, selected_attrs,
                int(family_min_cases), int(family_max_values),
                int(family_bins), family_include_values,
                noise_threshold, decomposition_spec,
                resource_attribute, effective_min_support,
                family_dedup, overlay_nodes, overlay_edges,
            )
            stashed_family_fp = st.session_state.get("family_fp")
            family_params_changed = (
                stashed_family_fp is not None
                and stashed_family_fp != _family_fp
            )
            if family_params_changed and not run_family:
                st.session_state.pop("family_fp", None)
                st.session_state.pop("family_result", None)

            if run_family:
                try:
                    with st.status("Mining model family...",
                                   expanded=True) as status:
                        fam = _mine_family(
                            log_bytes, log_kind, csv_columns,
                            selected_attrs, int(family_min_cases),
                            int(family_max_values), int(family_bins),
                            family_include_values,
                            noise_threshold, decomposition_spec,
                            resource_attribute, effective_min_support,
                            family_dedup, overlay_nodes, overlay_edges,
                            file_hash, _status=status,
                            _progress=_ProgressUI(status),
                        )
                        status.update(label="Done.", state="complete")
                    st.session_state["family_fp"] = _family_fp
                    st.session_state["family_result"] = fam
                except Exception as exc:
                    st.error(
                        f"Family mining failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    with st.expander("Show technical details"):
                        st.code(traceback.format_exc(), language="text")
                    st.stop()

            fam = st.session_state.get("family_result")
            if fam is None:
                if family_params_changed:
                    st.info(
                        "Settings changed since the last run — click "
                        "**Mine model family** to regenerate."
                    )
                else:
                    st.info(
                        "Review the coverage above, then click "
                        "**Mine model family**."
                    )
            else:
                fm1, fm2, fm3, fm4, fm5 = st.columns(5)
                fm1.metric("Models mined", fam["n_cells"])
                fm2.metric("Skipped cells", fam["n_skipped"])
                fm3.metric(
                    "Variation points",
                    fam["n_variation_points"],
                    help=(
                        "Dynamic stubs on the umbrella's root map — "
                        "the places where the cell processes actually "
                        "diverge. Structure outside the stubs is "
                        "shared by every combination."
                    ),
                )
                fm4.metric(
                    "Variant plug-ins", fam["n_plugins"],
                    help=(
                        "Total conditioned plug-in maps across the "
                        "variation points, after merging behaviourally "
                        "identical variants (when enabled)."
                    ),
                )
                fcov = (
                    fam["covered_cases"] / fam["total_cases"] * 100
                    if fam["total_cases"] else 0.0
                )
                fm5.metric("Case coverage", f"{fcov:.1f}%")

                # SVG is the default grid view: one 2-D vector matrix,
                # crisp at any zoom with selectable text, and cheap to
                # build (per-cell graphviz SVG, no rasterising). The PNG is
                # a download prepared on demand below. Cached per notation,
                # independent of mining — switching UCM ↔ BPMN re-renders
                # but never re-mines.
                grid_png = None  # rendered lazily (fallback / on-demand)
                with st.spinner(f"Rendering family grid ({notation})..."):
                    grid_svg, grid_svg_err = _render_family_grid_svg(
                        st.session_state["family_fp"], style, fam["family"],
                    )
                if grid_svg is not None:
                    _svg_viewer(grid_svg, height=640, key="familysvg")
                    st.caption(
                        "One panel per combination (rows × columns) — "
                        "captions show each cell's case count and share of "
                        "the log. Vector SVG: scroll to zoom, drag to pan; "
                        "for a decomposed member, click a stub to jump to "
                        "its sub-map. Download SVG or a raster PNG below."
                    )
                else:
                    # SVG failed — fall back to the raster grid so the
                    # family stays visible, and say why.
                    with st.spinner(f"Rendering family grid ({notation})..."):
                        grid_png, grid_preview, grid_error = (
                            _render_family_grid(
                                st.session_state["family_fp"], style,
                                fam["family"],
                            )
                        )
                    if grid_preview is not None:
                        _fb64 = base64.b64encode(grid_preview).decode("ascii")
                        st.markdown(
                            f'<img src="data:image/png;base64,{_fb64}" '
                            f'width="{_DISPLAY_WIDTH_PX}" '
                            f'style="max-width:100%; height:auto; '
                            f'cursor: zoom-in;" data-opentab="1" '
                            f'title="Double-click to open in a new browser tab" '
                            f'alt="Model family grid" />',
                            unsafe_allow_html=True,
                        )
                        st.caption(
                            f"SVG render failed ({grid_svg_err}); showing "
                            "the raster grid. Double-click to open it in a "
                            "new browser tab."
                        )
                        _open_image_in_tab_button(
                            _fb64, label="Open grid in new tab ⧉")
                    elif grid_error:
                        st.warning(f"Grid rendering unavailable: {grid_error}")

                st.subheader("Cells")
                st.dataframe(
                    fam["summary_df"], use_container_width=True,
                    hide_index=True,
                )

                st.subheader("Downloads")
                stem = Path(log_name).stem
                fd1, fd2, fd3, fd4, fd5 = st.columns(5)
                fd1.download_button(
                    "Per-cell models (.zip)", data=fam["zip"],
                    file_name=_safe_download_name(f"{stem}_family", ".zip"),
                    mime="application/zip",
                )
                fd2.download_button(
                    "Combined .jucm", data=fam["combined_jucm"],
                    file_name=_safe_download_name(
                        f"{stem}_family_combined", ".jucm",
                    ),
                    mime="application/xml",
                    help="Every cell model as an independent root map "
                         "in one file (shared definitions).",
                )
                fd3.download_button(
                    "Umbrella .jucm (dynamic stub)",
                    data=fam["umbrella_jucm"],
                    file_name=_safe_download_name(
                        f"{stem}_family_umbrella", ".jucm",
                    ),
                    mime="application/xml",
                    help="Overarching model: dynamic stub + one "
                         "conditioned plug-in per (merged) cell + one "
                         "strategy per combination.",
                )
                if grid_svg is not None:
                    fd4.download_button(
                        "Grid SVG", data=grid_svg.encode("utf-8"),
                        file_name=_safe_download_name(
                            f"{stem}_family_grid_{style}", ".svg",
                        ),
                        mime="image/svg+xml",
                        help="Vector grid — crisp at any zoom, text "
                             "selectable.",
                    )
                elif grid_svg_err:
                    fd4.caption(f"SVG unavailable: {grid_svg_err}")

                # Grid PNG — a raster download, rendered ONLY when asked
                # (SVG is the default view). Keyed by family + notation.
                _grid_png_key = (
                    f"family_grid_png::{st.session_state['family_fp']}"
                    f"::{style}"
                )
                if grid_png is None and _grid_png_key in st.session_state:
                    grid_png = st.session_state[_grid_png_key]
                if grid_png is not None:
                    fd4.download_button(
                        "Grid PNG", data=grid_png,
                        file_name=_safe_download_name(
                            f"{stem}_family_grid_{style}", ".png",
                        ),
                        mime="image/png", key="family_grid_png_download",
                    )
                elif fd4.button(
                    "Prepare Grid PNG…", key="family_grid_png_prepare",
                    help="Render a raster PNG to download (SVG is the "
                         "default; PNG is generated only when you ask).",
                ):
                    with st.spinner(f"Rendering family grid PNG "
                                    f"({notation})..."):
                        _gp, _, _ge = _render_family_grid(
                            st.session_state["family_fp"], style,
                            fam["family"],
                        )
                    if _gp is not None:
                        st.session_state[_grid_png_key] = _gp
                        st.rerun()
                    else:
                        fd4.caption(f"PNG unavailable: {_ge}")
                report_bytes, report_error = _build_family_report(
                    st.session_state["family_fp"], style,
                    fam["family"], fam["stats"],
                )
                if report_bytes is not None:
                    fd5.download_button(
                        "Interactive report (.html)", data=report_bytes,
                        file_name=_safe_download_name(
                            f"{stem}_family_report", ".html",
                        ),
                        mime="text/html",
                        help="Self-contained statistics report — "
                             "sortable tables, heatmaps, pairwise "
                             "process comparison with model images. "
                             "Opens offline in any browser; see the "
                             "Compare tab for the interactive version.",
                    )
                elif report_error:
                    fd5.caption(f"Report unavailable: {report_error}")

# ===== Compare view =======================================================
if _view == "Compare":
    st.subheader("Compare the family's processes")
    _cmp_fam = st.session_state.get("family_result")
    if _cmp_fam is None or "stats" not in _cmp_fam:
        st.info(
            "Mine a model family in the **Family** tab first — this tab "
            "ranks its combinations and compares any two of them side "
            "by side (statistics, models, activities, and choices)."
        )
    else:
        _stats = _cmp_fam["stats"]
        _cmp_fp = st.session_state.get("family_fp", "")
        _labels = _stats.cell_labels

        # ---- family-wide ranking table --------------------------------
        st.markdown(
            "**Family overview** — one row per combination; click a "
            "column header to rank. Durations are per case; "
            "`duration_total_s` is the **total** across all of the "
            "cell's cases."
        )
        _proc = _stats.process_frame()
        # Explicit format per column — raw floats would print with
        # full precision (e.g. 13.929231); never show more than two
        # decimals.
        _proc_fmts = {}
        for _c in _proc.columns:
            if _c.startswith("duration_"):
                _proc_fmts[_c] = _fmt_duration_s
            elif _c.startswith("events_per_case"):
                _proc_fmts[_c] = "{:.2f}"
            elif _c == "rework_case_fraction":
                _proc_fmts[_c] = "{:.1%}"
            elif _c == "rework_mean_repeats":
                _proc_fmts[_c] = "{:.2f}"
            elif _c.endswith("_pct"):
                _proc_fmts[_c] = "{:.1f}%"
            else:
                _proc_fmts[_c] = "{:,.0f}"
        st.dataframe(
            _heat_styler(_proc, _proc_fmts), use_container_width=True,
        )
        if not _stats.has_intervals:
            st.caption(
                "No `start_timestamp` column in this log — activity "
                "service times are not derivable (case durations and "
                "frequencies are unaffected)."
            )

        st.divider()

        # ---- pair selection --------------------------------------------
        sc1, sc2 = st.columns(2)
        _a_label = sc1.selectbox("Process A", _labels, index=0,
                                 key="cmp_cell_a")
        _b_label = sc2.selectbox(
            "Process B", _labels,
            index=min(1, len(_labels) - 1), key="cmp_cell_b",
        )
        _ia, _ib = _labels.index(_a_label), _labels.index(_b_label)
        _A, _B = _stats.cells[_ia], _stats.cells[_ib]

        _card_defs = [
            ("Cases", lambda c: c.n_cases, lambda v: f"{v:,.0f}",
             "normal"),
            ("Events/case (mean)",
             lambda c: c.events_per_case.get("mean"),
             lambda v: f"{v:.1f}", "normal"),
            ("Variants", lambda c: c.variants.get("n_variants"),
             lambda v: f"{v:,.0f}", "normal"),
            ("Rework rate (cases w/ a repeat)",
             lambda c: c.rework.get("case_fraction"),
             lambda v: f"{v * 100:.0f}%", "inverse"),
        ]
        if _stats.has_timestamps:
            _card_defs[2:2] = [
                ("Mean duration", lambda c: c.duration.get("mean"),
                 _fmt_duration_s, "inverse"),
                ("Median duration", lambda c: c.duration.get("median"),
                 _fmt_duration_s, "inverse"),
                ("TOTAL duration", lambda c: c.duration.get("total"),
                 _fmt_duration_s, "inverse"),
            ]
        # Rows of three — six side-by-side columns truncate the
        # "A -> B" values on ordinary screens.
        _cards = []
        for _row_start in range(0, len(_card_defs), 3):
            _n = min(3, len(_card_defs) - _row_start)
            _cards.extend(st.columns(3)[:_n])
        for _col, (_t, _get, _fmt, _dcolor) in zip(_cards, _card_defs):
            _va, _vb = _get(_A), _get(_B)
            if _va is None or _vb is None:
                _col.metric(_t, "—")
                continue
            _d = _vb - _va
            _delta = ("+" if _d > 0 else "") + _fmt(_d) + (
                f" ({_d / abs(_va) * 100:+.0f}%)" if _va else ""
            )
            _col.metric(
                _t, f"{_fmt(_va)} → {_fmt(_vb)}",
                delta=_delta if _d else None,
                delta_color=_dcolor if _d else "off",
                help="A → B; the delta is B minus A.",
            )

        # ---- models side by side ----------------------------------------
        # SVG is the on-screen default (crisp zoom/pan, selectable text);
        # a decomposed member's stubs are clickable and stay inside that
        # member. PNG stays available as a download.
        st.caption("Zoom and drag to pan each model. For a decomposed "
                   "member, click a stub to jump to its sub-map; a "
                   "dynamic stub opens a plug-in picker.")
        _cmp_stem = Path(log_name).stem
        _imc = st.columns(2)
        for _col, _tag, _idx, _cell in (
                (_imc[0], "A", _ia, _A), (_imc[1], "B", _ib, _B)):
            with _col:
                _cap = (
                    f"{_tag} — {_cell.label} · n={_cell.n_cases} "
                    f"({_cell.coverage * 100:.1f}% of the log)"
                )
                _csvg = _render_family_cell_svg(
                    _cmp_fp, style, _idx, _cmp_fam["family"],
                )
                if _csvg is not None:
                    _svg_viewer(_csvg, height=460, key=f"cmpsvg{_tag}")
                    st.caption(_cap)
                    _dlc = st.columns(2)
                    _dlc[0].download_button(
                        "SVG", data=_csvg.encode("utf-8"),
                        file_name=_safe_download_name(
                            f"{_cmp_stem}_compare_{_tag}_{style}", ".svg"),
                        mime="image/svg+xml", key=f"cmpsvgdl{_tag}",
                    )
                    _png = _render_family_cell(
                        _cmp_fp, style, _idx, _cmp_fam["family"],
                    )
                    if _png is not None:
                        _dlc[1].download_button(
                            "PNG", data=_png,
                            file_name=_safe_download_name(
                                f"{_cmp_stem}_compare_{_tag}_{style}", ".png"),
                            mime="image/png", key=f"cmppngdl{_tag}",
                        )
                    continue
                # SVG unavailable — fall back to the PNG image (with the
                # shared double-click-to-open-in-a-new-tab behaviour).
                _png = _render_family_cell(
                    _cmp_fp, style, _idx, _cmp_fam["family"],
                )
                if _png is not None:
                    _cb64 = base64.b64encode(_png).decode("ascii")
                    st.markdown(
                        f'<img src="data:image/png;base64,{_cb64}" '
                        f'style="max-width:100%; height:auto; '
                        f'cursor: zoom-in;" '
                        f'data-opentab="1" '
                        f'title="Double-click to open in a new browser tab" '
                        f'alt="Compare {_tag} model" />',
                        unsafe_allow_html=True,
                    )
                    st.caption(_cap)
                else:
                    st.caption(_cap + " — rendering unavailable")

        # ---- activity / edge comparison ---------------------------------
        def _delta_frame(names, entry_of, key, per_case, kind,
                         row_name):
            """Rows: A value, B value, Δ, ratio for every named entry
            present in either cell; returns (frame, formats). ``kind``
            is ``"count"`` | ``"time"`` | ``"ratio"``."""
            is_time = kind == "time"
            rows = []
            for name in names:
                ea = entry_of(_A, name) or {}
                eb = entry_of(_B, name) or {}
                va, vb = ea.get(key), eb.get(key)
                if va is None and vb is None:
                    continue
                if per_case and kind == "count":
                    va = None if va is None else va / _A.n_cases
                    vb = None if vb is None else vb / _B.n_cases
                rows.append({
                    row_name: name,
                    f"A — {_A.label}": va,
                    f"B — {_B.label}": vb,
                    "Δ (B−A)": (
                        vb - va if va is not None and vb is not None
                        else None
                    ),
                    "ratio B/A": (
                        vb / va
                        if va not in (None, 0) and vb is not None
                        else None
                    ),
                })
            frame = pd.DataFrame(rows).set_index(row_name)
            if is_time:
                fmts = {c: _fmt_duration_s for c in frame.columns
                        if c != "ratio B/A"}
            elif kind == "ratio":
                fmts = {c: "{:.1%}" for c in frame.columns
                        if c != "ratio B/A"}
            elif per_case:
                fmts = {c: "{:.2f}" for c in frame.columns
                        if c != "ratio B/A"}
            else:
                fmts = {c: "{:,.0f}" for c in frame.columns
                        if c != "ratio B/A"}
            fmts["ratio B/A"] = "{:.2f}×"
            return frame, fmts

        st.markdown("**Activity comparison** (Δ and ratio are B vs A)")
        _metric_opts = [
            ("frequency", "frequency (executions)", "count"),
            ("relative_frequency", "relative frequency (share of events)",
             "ratio"),
            ("case_coverage", "case coverage (cases)", "count"),
            ("repeat_frequency", "repeat frequency (rework executions)",
             "count"),
        ]
        if _stats.has_intervals:
            _metric_opts += [
                ("mean_time", "mean service time", "time"),
                ("median_time", "median service time", "time"),
                ("min_time", "min service time", "time"),
                ("max_time", "max service time", "time"),
                ("std_time", "std service time", "time"),
                ("p90_time", "P90 service time", "time"),
                ("p95_time", "P95 service time", "time"),
                ("total_time", "total service time", "time"),
            ]
        if _stats.has_timestamps:
            # Sojourn = time since the case's previous event — the
            # activity-level time statistic that works WITHOUT a
            # start_timestamp column.
            _metric_opts += [
                ("sojourn_mean_time",
                 "mean sojourn time (since previous event)", "time"),
                ("sojourn_median_time", "median sojourn time", "time"),
                ("sojourn_min_time", "min sojourn time", "time"),
                ("sojourn_max_time", "max sojourn time", "time"),
                ("sojourn_std_time", "std sojourn time", "time"),
                ("sojourn_p90_time", "P90 sojourn time", "time"),
                ("sojourn_p95_time", "P95 sojourn time", "time"),
                ("sojourn_total_time", "total sojourn time", "time"),
            ]
        ac1, ac2 = st.columns([2, 1])
        _m_label = ac1.selectbox(
            "Metric", [m[1] for m in _metric_opts], key="cmp_metric",
        )
        _m_key, _, _m_kind = next(
            m for m in _metric_opts if m[1] == _m_label
        )
        _per_case = ac2.checkbox(
            "Per case", value=False, key="cmp_percase",
            disabled=_m_kind != "count",
            help="Divide by the cell's case count — cells differ "
                 "hugely in size, so absolute counts mislead.",
        )
        _cmp_df, _cmp_fmts = _delta_frame(
            _stats.activity_names,
            lambda c, n: c.activity.get(n), _m_key,
            _per_case, _m_kind, "activity",
        )
        st.dataframe(
            _heat_styler(_cmp_df, _cmp_fmts), use_container_width=True,
        )

        if _stats.edge_names:
            _wait_label = (
                "waiting time" if _stats.has_intervals
                else "waiting time (completion-to-completion)"
            )
            st.markdown(
                "**Edge comparison** — directly-follows handovers "
                "between two activities (Δ and ratio are B vs A)"
            )
            _e_opts = [
                ("frequency", "frequency (traversals)", "count"),
                ("case_frequency", "case frequency (distinct cases)",
                 "count"),
                ("relative_frequency",
                 "relative frequency (share of traversals)", "ratio"),
                ("mean_time", f"mean {_wait_label}", "time"),
                ("median_time", f"median {_wait_label}", "time"),
                ("min_time", f"min {_wait_label}", "time"),
                ("max_time", f"max {_wait_label}", "time"),
                ("std_time", f"std {_wait_label}", "time"),
                ("p90_time", f"P90 {_wait_label}", "time"),
                ("p95_time", f"P95 {_wait_label}", "time"),
                ("total_time", f"total {_wait_label}", "time"),
            ]
            ec1, ec2 = st.columns([2, 1])
            _e_label = ec1.selectbox(
                "Edge metric", [m[1] for m in _e_opts],
                key="cmp_emetric",
            )
            _e_key, _, _e_kind = next(
                m for m in _e_opts if m[1] == _e_label
            )
            _e_per_case = ec2.checkbox(
                "Per case", value=False, key="cmp_epercase",
                disabled=_e_kind != "count",
            )
            _edge_df, _edge_fmts = _delta_frame(
                _stats.edge_names,
                lambda c, n: c.edges.get(n), _e_key,
                _e_per_case, _e_kind, "edge",
            )
            st.dataframe(
                _heat_styler(_edge_df, _edge_fmts),
                use_container_width=True,
            )
            if not _stats.has_intervals:
                st.caption(
                    "Single-timestamp log: waiting times are measured "
                    "completion-to-completion, so they include the "
                    "successor activity's own duration."
                )

        # ---- choices ------------------------------------------------------
        st.markdown("**Choices** — OR-fork branch shares, aligned "
                    "across the family")
        if not _stats.choices:
            st.caption("The family's processes contain no OR-fork "
                       "choices.")
        for _ch in _stats.choices:
            _meta = ("shared skeleton fork" if _ch.shared
                     else "variation-point fork (not in every process)")
            if _ch.inside_loop:
                _meta += (" · inside a loop — counts are evaluations "
                          "across iterations")
            with st.expander(_ch.full_name,
                             expanded=len(_stats.choices) <= 6):
                st.caption(_meta)
                _ch_rows = {}
                for _tag, _idx2, _cell2 in (
                        ("A — " + _A.label, _ia, _A),
                        ("B — " + _B.label, _ib, _B)):
                    _counts = _ch.counts[_idx2]
                    if _counts is None:
                        _ch_rows[_tag] = {
                            b: None for b in _ch.branches
                        }
                        continue
                    _total = sum(_counts) or 1
                    _ch_rows[_tag] = {
                        b: c / _total * 100
                        for b, c in zip(_ch.branches, _counts)
                    }
                    _ch_rows[_tag]["n"] = sum(_counts)
                _ch_df = pd.DataFrame.from_dict(_ch_rows, orient="index")
                _ch_fmts = {b: "{:.1f}%" for b in _ch.branches}
                _ch_fmts["n"] = "{:,.0f}"
                st.dataframe(
                    _heat_styler(_ch_df, _ch_fmts),
                    use_container_width=True,
                )

        # ---- report download ----------------------------------------------
        st.divider()
        _rep_bytes, _rep_err = _build_family_report(
            _cmp_fp, style, _cmp_fam["family"], _stats,
        )
        if _rep_bytes is not None:
            st.download_button(
                "Download the interactive report (.html)",
                data=_rep_bytes,
                file_name=_safe_download_name(
                    f"{Path(log_name).stem}_family_report", ".html",
                ),
                mime="text/html",
                help="Everything on this tab (and more) as ONE "
                     "self-contained offline HTML file — sortable "
                     "heat-mapped tables, pair comparison with the "
                     "model images embedded, choice bars, and a model "
                     "gallery. Open it several times to compare "
                     "several pairs side by side.",
                key="cmp_report_dl",
            )
        elif _rep_err:
            st.caption(f"Report unavailable: {_rep_err}")

# ===== Dashboards view =====================================================
if _view == "Dashboards":
    st.caption(
        "Build widgets from a metric catalog over this log — filter, "
        "segment by resource / time / case attribute, set targets, and "
        "drill into any cell. Everything below is computed **in your "
        "browser** from a compact snapshot of the log, which is why the "
        "exported HTML stays interactive offline: the export is this "
        "same view."
    )

    try:
        _ft = _fact_table(log_bytes, log_kind, csv_columns, log_name,
                          file_hash)
    except Exception as exc:
        st.error(
            f"Could not prepare dashboard data: {type(exc).__name__}: {exc}"
        )
        with st.expander("Show technical details"):
            st.code(traceback.format_exc(), language="text")
        st.stop()

    if _ft.sampled:
        st.warning(
            f"This log is too large to send to the browser whole, so the "
            f"dashboard is computed from a random sample of "
            f"{_ft.n_cases:,} of {_ft.sampled_from:,} cases. Every value "
            f"below is an estimate."
        )
    if _ft.dropped_events:
        st.warning(
            f"{_ft.dropped_events:,} events were excluded because their "
            f"timestamp could not be parsed."
        )

    # Model widgets and the report's model section both render from SVG
    # now — no PNG is produced here. One SVG per notation serves both: a
    # data URI for a model widget's <img>, and the raw SVG for the
    # report's zoom/pan viewer. The render is cached per style, so both
    # notations cost one render each rather than one per rerun.
    _renders: Dict[str, str] = {}
    _model_svgs: Dict[str, str] = {}
    for _style, _label in (("ucm", "ucm"), ("bpmn", "bpmn")):
        try:
            _svg = _render_svg_cached(mined["jucm"], _style)
            _model_svgs[_label] = _svg
            _renders[_label] = (
                "data:image/svg+xml;base64,"
                + base64.b64encode(_svg.encode("utf-8")).decode("ascii")
            )
        except Exception:
            # A notation that will not render must not take the whole
            # dashboard down; the widget falls back to a placeholder.
            pass

    # Fold the mined family's statistics report into the session report as
    # a Family section (only when a family has been mined for this log).
    # The report is a self-contained HTML doc (families/report.py); the
    # client-built session report embeds it whole in an <iframe>. Cached
    # per (family, style) via _build_family_report.
    _family_report_html = ""
    _fam = st.session_state.get("family_result")
    if _fam and _fam.get("stats") is not None:
        _fr_bytes, _fr_err = _build_family_report(
            st.session_state.get("family_fp", ""), style,
            _fam["family"], _fam["stats"],
        )
        if _fr_bytes is not None:
            _family_report_html = _fr_bytes.decode("utf-8")

    _specs_json = _json.dumps(_DEFAULT_DASHBOARD_SPECS)
    _html = _dashboard_html_cached(
        _ft, _specs_json, "Ops overview",
        tuple(sorted(_renders.items())), file_hash, False, _theme,
        tuple(sorted(_model_svgs.items())),
        _family_report_html,
    )

    # A pin carries a fresh id per click, so it must not reach the cache
    # key — it would miss every time and rebuild a 1 MB document for
    # nothing. Splice it into the cached config instead. The island skips
    # ids it has already applied, so re-sending the same one across
    # reruns is harmless; it stays until the log or the theme changes.
    _pin = st.session_state.get("pending_pin")
    if _pin:
        _html = _html.replace(
            '"pendingPin":null',
            '"pendingPin":' + _json.dumps(_pin, separators=(",", ":"))
                              .replace("</", "<\\/"),
            1,
        )

    # The island owns its own scrolling; a fixed height keeps the page
    # from growing an outer scrollbar around an inner one.
    _embed_html(_html, height=760, scrolling=True)

    with st.expander("About these numbers"):
        st.markdown(
            f"""
**Data.** {_ft.n_cases:,} cases · {_ft.n_events:,} events ·
{len(_ft.activities)} activities ·
{"interval log (service and waiting times available)"
 if _ft.interval_log else
 "single-timestamp log (service/waiting times need a `start_timestamp` "
 "column; sojourn times work on any log)"}.

**Where the widgets live.** Widgets you add are saved **in this
browser**, not on the server — the view is an embedded page and cannot
write back to Streamlit. They survive a reload and a re-mine, and are
kept per log. Clearing site data clears them.

**Semantics.** Missing values leave the denominator rather than counting
as zero, percentiles interpolate linearly (matching the model overlays
and family reports), and activity time metrics are case-weighted — see
[docs/metrics.md](https://github.com/ProcessMining-uOttawa/pm4py-ucm/blob/main/docs/metrics.md)
and
[docs/dashboards.md](https://github.com/ProcessMining-uOttawa/pm4py-ucm/blob/main/docs/dashboards.md).
            """
        )
