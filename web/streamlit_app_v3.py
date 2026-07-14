"""PM4Py-UCM web front-end — V3 (model + scenarios + families +
reports). THE app: every deployment path serves this file.

Tabs: **Model** (inductive-mine a UCM, preview in UCM or BPMN
notation, download PNG/.jucm), **Scenarios** (concurrency-aware
variant clustering + scenario synthesis, with every artifact — the
executable .jucm, variants.csv, case_variant_map.csv, and for
data-driven runs condition_mining.csv — as a download), **Family**
(partition the log by 1–2 case attributes and mine a model per
combination: grid rendering, per-cell zip, combined .jucm, a
dynamic-stub umbrella .jucm with per-combination strategies, and the
self-contained interactive HTML statistics report), and **Compare**
(rank the family members and compare any two side by side).

Run locally:

    streamlit run web/streamlit_app_v3.py

Deployment layout: ``streamlit_app.py`` (the
https://pm4py-ucm.streamlit.app/ main file) is a shim that runs THIS
file, so the primary deployment always serves the latest app. V1
(model-only) was retired at v0.5.1 and lives in git history.
``streamlit_app_v2.py`` is deliberately NOT a shim: it is the frozen
V2 (model + scenarios) app that
https://pm4py-ucm-scenarios.streamlit.app/ must keep serving while a
paper referencing it is under review — do not fold it into V3.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import tempfile
import traceback
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

import pm4py
import pm4py_ucm
from pm4py_ucm.algo.discovery.scenarios import reports as _reports
from pm4py_ucm.algo.discovery.scenarios import synthesis as _scenarios
from pm4py_ucm.algo.discovery.variants import clustering as _clustering_mod
from pm4py_ucm.visualization.ucm import visualizer as _visualizer
from pm4py_ucm.visualization.ucm import stacked as _stacked

# Pillow's default decompression-bomb guard (~178M px) rejects very
# large composites (family grids, decomposed stacks); raise it to a
# still-sane 1B-pixel cap.
from PIL import Image as _PILImage
_PILImage.MAX_IMAGE_PIXELS = 1_000_000_000


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
    import streamlit.components.v1 as _components
    _components.html(
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

st.set_page_config(page_title="PM4Py-UCM (V3 · Families)", layout="wide")
st.title("PM4Py-UCM")


@st.cache_data(ttl=3600, show_spinner=False)
def _latest_release() -> Optional[Dict[str, str]]:
    """The repository's latest published GitHub release —
    ``{"tag", "date", "url"}`` — or ``None`` when the API is
    unreachable (offline, rate-limited). Cached for an hour. The
    deployed code may be ahead of or behind the latest release, so
    the header quotes the RELEASE, not the package version — the
    version constant may never have been published under that tag."""
    import json
    import urllib.request
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/ProcessMining-uOttawa/"
            "pm4py-ucm/releases/latest",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "pm4py-ucm-streamlit"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.load(resp)
        if not data.get("tag_name"):
            return None
        return {
            "tag": str(data["tag_name"]),
            "date": str(data.get("published_at") or "")[:10],
            "url": str(
                data.get("html_url")
                or "https://github.com/ProcessMining-uOttawa/pm4py-ucm/"
                   "releases/latest"
            ),
        }
    except Exception:
        return None


_release = _latest_release()
if _release is not None:
    _release_text = (
        f"Latest release: [pm4py-ucm {_release['tag']}]({_release['url']})"
        + (f", {_release['date']}" if _release["date"] else "")
    )
else:  # offline / rate-limited: a link that always resolves
    _release_text = (
        "Releases: [github.com/ProcessMining-uOttawa/pm4py-ucm]"
        "(https://github.com/ProcessMining-uOttawa/pm4py-ucm/"
        "releases/latest)"
    )
st.caption(
    "Mine a Use Case Map model from an XES or CSV event log, "
    "synthesize executable jUCMNav scenarios with concurrency-aware "
    "variant clustering, and mine attribute-partitioned model "
    "families with comparative statistics reports. "
    f"{_release_text} — by [Daniel Amyot](https://damyot.github.io/), "
    "University of Ottawa, Canada."
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
            "the activity; mean/median/total_time are activity service "
            "times and need an interval log (start_timestamp column); "
            "the sojourn_* metrics are the time since the case's "
            "previous event (≈ waiting + service) and work on any "
            "timestamped log."
        ),
    )[:2])
    overlay_edges = tuple(st.multiselect(
        "On edges (max 2)",
        options=list(_EDGE_METRICS), default=[],
        help=(
            "frequency = directly-follows traversals; percentage = an "
            "OR-fork branch's share of the fork; the time metrics are "
            "waiting times between the edge's activities."
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

c1, c2, c3, c4, c5, c6, c7, c8 = st.columns(8)
c1.metric("File", log_name)
c2.metric("Cases", f"{mined['n_cases']:,}")
c3.metric("Events", f"{mined['n_events']:,}")
c4.metric("Activities", f"{mined['n_activities']:,}")
c5.metric("Notation", notation)
c6.metric("Decomposition", decomposition_preset)
c7.metric("Maps", mined["n_maps"])
c8.metric("Nodes", mined["n_nodes"])

# ---- Tabs -----------------------------------------------------------------
model_tab, scenarios_tab, family_tab, compare_tab = st.tabs(
    ["Model", "Scenarios", "Family", "Compare"]
)

# ===== Model tab ===========================================================
with model_tab:
    try:
        with st.spinner(f"Rendering {notation} diagram..."):
            png_bytes = _render_cached(mined["jucm"], style)
    except Exception as exc:
        st.error(f"Render failed: {type(exc).__name__}: {exc}")
        with st.expander("Show technical details"):
            st.code(traceback.format_exc(), language="text")
        st.stop()

    _b64 = base64.b64encode(png_bytes).decode("ascii")
    st.markdown(
        f'<img src="data:image/png;base64,{_b64}" '
        f'width="{_DISPLAY_WIDTH_PX}" '
        f'style="max-width:100%; height:auto; cursor: zoom-in;" '
        f'data-opentab="1" '
        f'title="Double-click to open in a new browser tab" '
        f'alt="Mined {notation} model" />',
        unsafe_allow_html=True,
    )
    st.caption(
        f"Mined model ({notation}, decomposition={decomposition_preset}) — "
        "double-click the image (or use the button below) to open it "
        "in its own browser tab and zoom complex models more easily."
    )

    d1, d2, d3 = st.columns(3)
    with d1:
        _open_image_in_tab_button(_b64)
    d2.download_button(
        "Download PNG", data=png_bytes,
        file_name=_safe_download_name(Path(log_name).stem, ".png"),
        mime="image/png",
    )
    d3.download_button(
        "Download .jucm (no scenarios)", data=mined["jucm"],
        file_name=_safe_download_name(Path(log_name).stem, ".jucm"),
        mime="application/xml",
    )

# ===== Scenarios tab =======================================================
with scenarios_tab:
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

# ===== Family tab ==========================================================
with family_tab:
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
                "this many quantile ranges."
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

                # Grid rendering is per-notation and cached
                # independently of mining — switching UCM ↔ BPMN only
                # re-renders the already-mined models.
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
                        f'style="max-width:100%; height:auto;" '
                        f'alt="Model family grid" />',
                        unsafe_allow_html=True,
                    )
                    st.caption(
                        "One panel per combination — captions show "
                        "each cell's case count and share of the log. "
                        "This inline view is a downscaled preview; "
                        "the **Grid PNG** download below is full "
                        "resolution (text-readable)."
                    )
                elif grid_error:
                    st.warning(
                        f"Grid rendering unavailable: {grid_error}"
                    )

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
                if grid_png is not None:
                    fd4.download_button(
                        "Grid PNG", data=grid_png,
                        file_name=_safe_download_name(
                            f"{stem}_family_grid_{style}", ".png",
                        ),
                        mime="image/png",
                    )
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

# ===== Compare tab =========================================================
with compare_tab:
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
        _imc = st.columns(2)
        for _col, _tag, _idx, _cell in (
                (_imc[0], "A", _ia, _A), (_imc[1], "B", _ib, _B)):
            with _col:
                _png = _render_family_cell(
                    _cmp_fp, style, _idx, _cmp_fam["family"],
                )
                _cap = (
                    f"{_tag} — {_cell.label} · n={_cell.n_cases} "
                    f"({_cell.coverage * 100:.1f}% of the log)"
                )
                if _png is not None:
                    st.image(_png, caption=_cap,
                             use_container_width=True)
                else:
                    st.caption(_cap + " — rendering unavailable")

        # ---- activity / edge comparison ---------------------------------
        def _delta_frame(names, entry_of, key, per_case, is_time,
                         row_name):
            """Rows: A value, B value, Δ, ratio for every named entry
            present in either cell; returns (frame, formats)."""
            rows = []
            for name in names:
                ea = entry_of(_A, name) or {}
                eb = entry_of(_B, name) or {}
                va, vb = ea.get(key), eb.get(key)
                if va is None and vb is None:
                    continue
                if per_case and not is_time:
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
            ("frequency", "frequency (executions)", False),
            ("case_coverage", "case coverage (cases)", False),
        ]
        if _stats.has_intervals:
            _metric_opts += [
                ("mean_time", "mean service time", True),
                ("median_time", "median service time", True),
                ("min_time", "min service time", True),
                ("max_time", "max service time", True),
                ("total_time", "total service time", True),
            ]
        if _stats.has_timestamps:
            # Sojourn = time since the case's previous event — the
            # activity-level time statistic that works WITHOUT a
            # start_timestamp column.
            _metric_opts += [
                ("sojourn_mean_time",
                 "mean sojourn time (since previous event)", True),
                ("sojourn_median_time", "median sojourn time", True),
                ("sojourn_min_time", "min sojourn time", True),
                ("sojourn_max_time", "max sojourn time", True),
                ("sojourn_total_time", "total sojourn time", True),
            ]
        ac1, ac2 = st.columns([2, 1])
        _m_label = ac1.selectbox(
            "Metric", [m[1] for m in _metric_opts], key="cmp_metric",
        )
        _m_key, _, _m_is_time = next(
            m for m in _metric_opts if m[1] == _m_label
        )
        _per_case = ac2.checkbox(
            "Per case", value=False, key="cmp_percase",
            disabled=_m_is_time,
            help="Divide by the cell's case count — cells differ "
                 "hugely in size, so absolute counts mislead.",
        )
        _cmp_df, _cmp_fmts = _delta_frame(
            _stats.activity_names,
            lambda c, n: c.activity.get(n), _m_key,
            _per_case, _m_is_time, "activity",
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
                ("frequency", "frequency (traversals)", False),
                ("mean_time", f"mean {_wait_label}", True),
                ("median_time", f"median {_wait_label}", True),
                ("min_time", f"min {_wait_label}", True),
                ("max_time", f"max {_wait_label}", True),
                ("total_time", f"total {_wait_label}", True),
            ]
            ec1, ec2 = st.columns([2, 1])
            _e_label = ec1.selectbox(
                "Edge metric", [m[1] for m in _e_opts],
                key="cmp_emetric",
            )
            _e_key, _, _e_is_time = next(
                m for m in _e_opts if m[1] == _e_label
            )
            _e_per_case = ec2.checkbox(
                "Per case", value=False, key="cmp_epercase",
                disabled=_e_is_time,
            )
            _edge_df, _edge_fmts = _delta_frame(
                _stats.edge_names,
                lambda c, n: c.edges.get(n), _e_key,
                _e_per_case, _e_is_time, "edge",
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
