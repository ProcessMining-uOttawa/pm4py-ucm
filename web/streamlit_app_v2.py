"""PM4Py-UCM web front-end — V2 (scenarios).

V2 is a superset of V1: same log-source / inductive-miner / performer /
decomposition controls, same UCM/BPMN preview tab, plus a Scenarios
tab that runs the concurrency-aware variant clustering + scenario
synthesis pipeline and exposes every artifact (the executable .jucm,
variants.csv, case_variant_map.csv, and for data-driven runs
condition_mining.csv) as a download.

V1 remains untouched at ``web/streamlit_app.py``. Run either:

    streamlit run web/streamlit_app.py       # V1 (model only)
    streamlit run web/streamlit_app_v2.py    # V2 (model + scenarios)

Helper code is intentionally inlined rather than shared with V1 — keeps
V1 a self-contained one-file app and avoids a refactor that could
regress its existing Streamlit Cloud deployment.
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

# Pillow's default decompression-bomb guard rejects very large composites.
# See V1 for the rationale; same 1B-pixel cap here.
from PIL import Image as _PILImage
_PILImage.MAX_IMAGE_PIXELS = 1_000_000_000


_SAMPLES_DIR = Path(__file__).resolve().parent / "samples"
_DISPLAY_WIDTH_PX = 1100
_NONE_OPT = "(none)"

# (session key, candidate names, include_none flag). Used by the CSV
# column auto-detection — same set as V1.
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
# Mining (UCM only, no scenarios) — same cache surface as V1 plus a
# returned process-tree fingerprint so the scenario step can detect
# whether its inputs changed.
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
    _file_hash: str,
    _status=None,
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

        _phase("Converting process tree to UCM...")
        ucm = pm4py_ucm.discover_ucm_inductive(
            log, parameters=params, decomposition=decomp_arg,
        )

        _phase("Writing .jucm...")
        jucm_path = td / "model.jucm"
        pm4py_ucm.write_ucm(ucm, str(jucm_path))

        return {
            "jucm": jucm_path.read_bytes(),
            "n_maps": len(ucm.maps),
            "n_nodes": sum(len(m.nodes) for m in ucm.maps),
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

    _phase("Converting process tree to UCM...")
    ucm = pm4py_ucm.discover_ucm_inductive(
        log, parameters=params, decomposition=decomp_arg,
    )

    _phase("Clustering variants...")
    clustering = _clustering_mod.cluster(log, tree)

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
# UI
# ---------------------------------------------------------------------------

st.set_page_config(page_title="PM4Py-UCM (V2 · Scenarios)", layout="wide")
st.title("PM4Py-UCM · V2")
st.caption(
    "Mine a Use Case Map model from an XES or CSV event log, and "
    "synthesize executable jUCMNav scenarios with concurrency-aware "
    "variant clustering. V1 (model-only) lives at "
    "`web/streamlit_app.py`."
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
        # V2-specific overrides on the package presets: tighter caps
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
        st.warning("Decomposition has unapplied changes.")
        if st.button("Apply changes", type="primary"):
            st.session_state["applied_decomp"] = candidate_spec
            st.rerun()

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

uploaded = src_tabs[1].file_uploader(
    "Upload an event log",
    type=["xes", "gz", "csv", "zip"],
    key="log_uploader",
)
if uploaded is not None:
    _accept_log_bytes(uploaded.name, uploaded.getvalue())

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
    with st.status("Mining UCM...", expanded=False) as status:
        mined = _mine(
            log_bytes, log_kind, csv_columns,
            decomposition_spec, resource_attribute,
            effective_min_support, noise_threshold,
            file_hash, _status=status,
        )
        status.update(label="Done.", state="complete")
except Exception as exc:
    st.error(f"Mining failed: {type(exc).__name__}: {exc}")
    with st.expander("Show technical details"):
        st.code(traceback.format_exc(), language="text")
    st.stop()

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("File", log_name)
c2.metric("Decomposition", decomposition_preset)
c3.metric("Maps", mined["n_maps"])
c4.metric("Nodes", mined["n_nodes"])
c5.metric("Notation", notation)

# ---- Tabs -----------------------------------------------------------------
model_tab, scenarios_tab = st.tabs(["Model", "Scenarios"])

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
        f'style="max-width:100%; height:auto;" '
        f'alt="Mined {notation} model" />',
        unsafe_allow_html=True,
    )
    st.caption(
        f"Mined model ({notation}, decomposition={decomposition_preset}) — "
        "open in a new tab or zoom in for a closer look."
    )

    d1, d2 = st.columns(2)
    d1.download_button(
        "Download PNG", data=png_bytes,
        file_name=_safe_download_name(Path(log_name).stem, ".png"),
        mime="image/png",
    )
    d2.download_button(
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
    # arg fingerprint and the result dict in session_state.
    _synth_fp = _arg_fingerprint(
        file_hash, log_kind, csv_columns, noise_threshold,
        condition_strategy, max_loop_iterations,
        decision_tree_max_depth, group_name,
        decomposition_spec, resource_attribute,
        effective_min_support,
    )

    if run or st.session_state.get("synth_fp") == _synth_fp:
        try:
            with st.status("Synthesizing scenarios...",
                           expanded=False) as status:
                synth = _synthesize(
                    log_bytes, log_kind, csv_columns,
                    noise_threshold, condition_strategy,
                    max_loop_iterations, decision_tree_max_depth,
                    group_name, decomposition_spec,
                    resource_attribute, effective_min_support,
                    file_hash, _status=status,
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
