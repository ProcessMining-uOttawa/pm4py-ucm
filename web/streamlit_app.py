"""PM4Py-UCM web front-end.

Run locally:
    streamlit run web/streamlit_app.py

Flow: choose a bundled sample OR upload an event log (XES/.xes.gz/.zip
or CSV) -> inductive-mine a UCM -> render PNG in the selected notation
(UCM or BPMN) -> download the PNG and/or the .jucm.

Mining and rendering are cached separately so toggling the notation
(UCM <-> BPMN) only re-renders the PNG; the inductive miner runs only
when the log, decomposition, or performer settings change.

PNG resolution is left at graphviz's default (96 dpi). The browser
display shows the original bitmap inside a fixed-width <img>, so the
user can scroll-zoom the page or open the image in a new tab to view
the file at its native resolution.
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
from typing import List, Optional, Tuple

import pandas as pd
import streamlit as st

import pm4py
import pm4py_ucm
from pm4py_ucm.visualization.ucm import visualizer as _visualizer
from pm4py_ucm.visualization.ucm import stacked as _stacked

# Pillow ships a ~179M-pixel "decompression bomb" guard. Even at
# graphviz's default DPI, very large multi-map UCMs can exceed it
# (PMM4RPA and similarly broad logs), which would make
# ``stacked._composite`` crash with ``DecompressionBombError``. Raise
# the limit to a generous but FINITE cap — leaving it at ``None``
# trusts unbounded image data, which is unsafe for a public deployment
# (a maliciously-crafted log producing a giant rendering could OOM the
# server). 1 billion pixels covers any realistic mined UCM with margin.
from PIL import Image as _PILImage
_PILImage.MAX_IMAGE_PIXELS = 1_000_000_000


# Where bundled sample logs live, relative to this file. Add more
# zipped XES files in this folder and they'll appear in the sample
# selector automatically.
_SAMPLES_DIR = Path(__file__).resolve().parent / "samples"


def _list_samples() -> List[Path]:
    """Return every bundled sample log file, sorted by display name.

    Anything ending in .xes, .xes.gz, .gz, or .zip is offered. Drop
    files into ``web/samples/`` to extend the list — no code changes
    needed.
    """
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
    """Strip characters that would be problematic in a downloaded filename.

    Replaces anything outside ``[A-Za-z0-9._-]`` with an underscore and
    falls back to ``"model"`` when the result is empty. The download
    target is fully under our control, but defence-in-depth costs
    nothing here.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    if not cleaned:
        cleaned = "model"
    return f"{cleaned}{ext}"


def _extract_xes_from_zip(zip_bytes: bytes) -> bytes:
    """Pick a single .xes / .xes.gz entry out of a zip archive.

    Guards against zip-slip-style path traversal in archive entries by
    only honouring entries whose normalised name has no parent
    components. Raises ``ValueError`` if no usable entry is found.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        # Pick the first .xes or .xes.gz that lives at the archive root
        # or in a single subdirectory with a safe name. Reject any
        # entry whose path tries to escape (`..`) or starts with `/`.
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


# Display width (CSS pixels) for the in-browser preview. The underlying
# PNG keeps its native pixel dimensions — the browser scales the IMG
# element down, which means right-click "Open image in new tab" still
# shows the bitmap at full resolution and the user can scroll-zoom in
# place if they want a closer look.
_DISPLAY_WIDTH_PX = 1100


st.set_page_config(page_title="PM4Py-UCM", layout="wide")
st.title("PM4Py-UCM")
st.caption(
    "Mine a Use Case Map model from an XES event log and export it to "
    "jUCMNav, or to PNG files with BPMN or UCM views."
)


def _render_png(ucm, style: str, out_path: str) -> str:
    """Render ``ucm`` to ``out_path`` at graphviz's default resolution.

    Handles the single-map case directly and uses the package's
    composite helper for multi-map UCMs.
    """
    params = {"style": style}

    if len(ucm.maps) <= 1:
        gviz = _visualizer.apply(ucm, parameters=params)
        return _visualizer.save(gviz, out_path)

    # Multi-map: render each panel individually and reuse the package's
    # compositor for the title strips and separators.
    from pm4py_ucm.visualization.ucm.variants import classic as _classic

    panels: List[Tuple[str, str]] = []
    tmpdir = tempfile.mkdtemp(prefix="pm4py_ucm_web_")
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


# ---- CSV column autodetect --------------------------------------------------
# Common XES / process-mining column names. The mapping value is the
# session_state key the autopick result is written to so the column
# selectors below read it as their initial value. ``include_none`` means
# the option list starts with a "(none)" sentinel — used for optional
# role / resource columns where "no column" is a valid choice.
_CSV_AUTOPICK = [
    # (session key,        candidate names,                                                include_none)
    ("csv_case",       ("case:concept:name", "case_id", "case", "caseid"),                  False),
    ("csv_activity",   ("concept:name", "activity", "activityname", "event", "task"),       False),
    ("csv_timestamp",  ("time:timestamp", "timestamp", "time", "datetime", "date"),         False),
    ("csv_role",       ("org:role", "role"),                                                True),
    ("csv_resource",   ("org:resource", "resource", "user", "performer"),                   True),
]
_NONE_OPT = "(none)"


def _autopick_column(columns, candidates, *, include_none, fallback_index):
    """Pick the best column from ``columns`` matching any of ``candidates``.

    Case-insensitive match. When ``include_none`` is True the result is
    one of ``columns`` or :data:`_NONE_OPT` (no match -> "(none)").
    When False the result is always one of ``columns`` (no match ->
    columns[fallback_index]).
    """
    lower = {c.lower(): c for c in columns}
    for cand in candidates:
        hit = lower.get(cand.lower())
        if hit is not None:
            return hit
    if include_none:
        return _NONE_OPT
    return columns[min(fallback_index, len(columns) - 1)]


def _seed_csv_selectors(columns, *, only_invalid: bool = False):
    """Pre-populate the column selectors with autodetected values.

    Writing to ``st.session_state`` before the selectboxes render means
    the new CSV's auto-detected mapping is the deterministic initial
    display — the user sees the proposed columns immediately and can
    confirm or modify them. Without this step Streamlit would fall back
    to the selectbox's ``index=`` parameter, which is only consulted
    when the key has no session_state value: across reruns that can
    produce confusing transient states.

    When ``only_invalid=True``, existing session_state values that are
    valid options for the current ``columns`` are LEFT ALONE — only
    keys that are missing or hold a value no longer in ``columns`` are
    re-seeded. This is the safe form used by the per-rerun defensive
    check so the user's previously-applied role / resource choices are
    not silently overwritten when something else triggers a rerun.
    """
    valid_required = set(columns)
    valid_optional = valid_required | {_NONE_OPT}
    for i, (key, cands, with_none) in enumerate(_CSV_AUTOPICK):
        if only_invalid:
            current = st.session_state.get(key)
            valid = valid_optional if with_none else valid_required
            if current in valid:
                continue
        st.session_state[key] = _autopick_column(
            columns, cands,
            include_none=with_none,
            fallback_index=i,
        )


@st.cache_data(show_spinner="Reading CSV columns...")
def _csv_columns(csv_bytes: bytes, _file_hash: str) -> List[str]:
    """Read just the header row of a CSV and return its column names.

    Cheap to compute and shared across reruns, so the column selectors
    in the main pane stay snappy even on large CSVs. Falls back to
    Python's csv module when pandas can't sniff the dialect.
    """
    try:
        # nrows=0 reads the header only.
        df_head = pd.read_csv(io.BytesIO(csv_bytes), nrows=0, low_memory=False)
        return list(df_head.columns)
    except Exception:
        # Last-resort fallback so the user still gets a list to pick from.
        import csv as _csv
        text = csv_bytes.decode("utf-8", errors="replace").splitlines()
        if not text:
            return []
        reader = _csv.reader(text[:1])
        return next(reader, [])


# show_spinner=False so cache hits leave no UI trace at all. The
# orchestrating code below wraps the call in ``st.spinner`` (transient,
# only visible during actual computation) so the user sees feedback on
# a cache miss but nothing on a cache hit. Per-phase status updates are
# pushed through the ``_status`` parameter when one is supplied —
# Streamlit passes the live ``st.status`` handle so the cached function
# can call ``.update()`` mid-run. The ``_status`` arg is prefixed with
# underscore to keep it out of the cache key (different status handles
# across reruns must not invalidate cached results).
@st.cache_data(show_spinner=False)
def _mine(
    log_bytes: bytes,
    log_kind: str,                  # "xes", "csv", or "zip"
    csv_columns,                    # tuple (case, activity, timestamp, role, resource) or None
    decomposition_spec,             # str "off" or tuple-of-(key, value) pairs
    resource_attribute: str,
    min_support: float,
    noise_threshold: float,
    _file_hash: str,
    _status=None,
):
    """Read the event log and mine a UCM. Returns .jucm bytes + metadata.

    Cached per (file, kind, csv columns, decomposition_spec,
    resource_attribute, min_support). Notation does **not** affect
    mining, so it is intentionally absent from the cache key — toggling
    UCM <-> BPMN goes through :func:`_render_cached` without rerunning
    the inductive miner.

    For ``log_kind="csv"``, ``csv_columns`` is a 5-tuple
    ``(case_col, activity_col, timestamp_col, role_col, resource_col)``
    (role/resource may be empty strings). The CSV is converted to a
    PM4Py-formatted DataFrame via ``pm4py.format_dataframe`` and the
    role/resource columns are renamed to the standard XES attribute
    names so the performer miner picks them up.

    ``decomposition_spec`` is either the literal string ``"off"`` or a
    sorted tuple of ``(key, value)`` pairs (so the cache key is hashable
    and order-independent); the dict is reconstructed before the call.

    ``resource_attribute`` may be:

    * an empty string — disables performer mining (passes
      ``resource_attribute=False`` to the discoverer);
    * a single attribute name like ``"org:resource"``;
    * a comma- or whitespace-separated fallback list like
      ``"org:role, org:resource, org:group"`` — the first attribute
      that is set on each event wins.
    """
    # Local helper so we either push to a real ``st.status`` panel (when
    # the orchestrator provided one) or stay silent (programmatic call,
    # e.g. cache-warming).
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
            # ``low_memory=False`` reads the whole CSV in one pass and
            # picks one dtype per column from the full data, instead of
            # pandas' default chunked read that can guess differently
            # for different chunks and trigger DtypeWarning + downstream
            # type confusion. Cost is higher peak RAM; benefit is robust
            # column types for the miner.
            _phase("Reading CSV...")
            df = pd.read_csv(io.BytesIO(log_bytes), low_memory=False)
            # format_dataframe renames the three required columns to the
            # PM4Py standard (case:concept:name, concept:name, time:timestamp).
            _phase(f"Formatting {len(df):,} events...")
            df = pm4py.format_dataframe(
                df,
                case_id=case_col,
                activity_key=activity_col,
                timestamp_key=ts_col,
            )
            # Rename role/resource columns to the standard XES attribute
            # names so the performer miner finds them via "org:role" /
            # "org:resource".
            renames = {}
            if role_col and role_col != "org:role":
                renames[role_col] = "org:role"
            if resource_col and resource_col != "org:resource":
                renames[resource_col] = "org:resource"
            if renames:
                df = df.rename(columns=renames)
            log = df
        else:
            # Accept .xes, .xes.gz, AND .zip (zipped XES is the form
            # bundled samples ship in). Detect by extension first;
            # fall back to magic-bytes sniffing so a user can rename
            # files without the app guessing wrong.
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

        # Build the parameters dict the inductive variant understands.
        # An empty resource_attribute means "disable performer mining"
        # — pass False explicitly so the discoverer doesn't fall back
        # to its default attribute list.
        params: dict = {}
        attrs = [a.strip() for a in resource_attribute.replace(",", " ").split()
                 if a.strip()]
        if not attrs:
            params["resource_attribute"] = False
        elif len(attrs) == 1:
            params["resource_attribute"] = attrs[0]
        else:
            params["resource_attribute"] = attrs
        # min_support only applies when resource mining is on.
        if attrs:
            params["resource_parameters"] = {"min_support": float(min_support)}

        # Reconstruct the decomposition argument from the cache-friendly
        # spec. "off" stays a string; a tuple of pairs becomes a dict
        # that the discoverer's parse_decomposition merges onto AUTO_DEFAULTS.
        if decomposition_spec == "off":
            decomp_arg = "off"
        else:
            decomp_arg = dict(decomposition_spec)

        # Pre-mine the process tree with the user-configured threshold,
        # then hand it to discover_ucm_inductive via parameters
        # ["process_tree"]. The package pops that key from parameters
        # and uses the pre-built tree instead of remining with default
        # settings (see pm4py_ucm/algo/discovery/ucm/variants/inductive.py).
        # This lets us expose noise_threshold without touching the
        # package's public API.
        _phase(
            "Discovering process tree "
            f"(noise threshold {noise_threshold:.2f})... "
            "this can take a few minutes on large logs."
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


# show_spinner=False so cache hits (e.g. flipping back to a notation
# already rendered) do not display any spinner at all. The orchestrator
# wraps the call in ``st.spinner`` so the user sees feedback only when
# the renderer actually has work to do.
@st.cache_data(show_spinner=False)
def _render_cached(jucm_bytes: bytes, style: str) -> bytes:
    """Render the UCM (round-tripped from .jucm bytes) to PNG bytes.

    Cached per (jucm hash, style). The first argument is hashable, so
    flipping the notation toggle hits the cache after one render per
    style.
    """
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        jucm_path = td / "model.jucm"
        jucm_path.write_bytes(jucm_bytes)
        ucm = pm4py_ucm.read_ucm(str(jucm_path))
        png_path = td / "model.png"
        _render_png(ucm, style, str(png_path))
        return png_path.read_bytes()


with st.sidebar:
    st.header("Options")
    notation = st.radio(
        "Notation",
        options=["UCM", "BPMN"],
        index=0,
        help=(
            "UCM: Z.151 / jUCMNav-style Use Case Map. "
            "BPMN: BPMN-friendly look (activity boxes, gateway diamonds)."
        ),
    )
    st.subheader("Inductive miner")
    noise_threshold = st.slider(
        "Noise threshold",
        min_value=0.0, max_value=1.0, value=0.2, step=0.05,
        help=(
            "Infrequent inductive miner (IMf) threshold. "
            "0.0 keeps every observed behaviour (classic Inductive "
            "Miner, perfect fitness, often noisy). Higher values "
            "filter out increasingly rare arcs / activities, producing "
            "smaller and more abstract models. 0.2 is a common "
            "practical default; useful range is roughly 0.1-0.4."
        ),
    )

    decomposition_preset = st.selectbox(
        "Decomposition",
        options=["off", "auto", "aggressive"],
        index=0,
        help=(
            "off: single flat map. "
            "auto: split into a root map plus plug-in maps when the "
            "model is large enough to benefit. "
            "aggressive: same boundary rules with a tighter cap, "
            "producing more / smaller plug-ins. "
            "Use the Advanced section to override individual keys."
        ),
    )

    # Decomposition advanced keys. Visible only when decomposition is on.
    # The widget defaults are seeded from the chosen preset so the
    # "auto vs aggressive" choice still has an immediate effect even
    # without expanding the section. When the user changes the preset,
    # the widget identity (the ``key=`` argument) is preset-specific so
    # Streamlit re-runs them from their new defaults.
    #
    # Widget edits do NOT trigger an immediate remine. The user makes as
    # many changes as they want, then clicks "Apply changes" to commit
    # them. The committed state is held in ``st.session_state`` under
    # ``applied_decomp``; the mining cache key reads from that, not from
    # the live widget values.
    decomposition_overrides: dict = {}
    if decomposition_preset != "off":
        from pm4py_ucm.objects.ucm.conversion.decomposition import (
            AUTO_DEFAULTS, AGGRESSIVE_DEFAULTS,
        )
        _preset_defaults = (
            AUTO_DEFAULTS if decomposition_preset == "auto"
            else AGGRESSIVE_DEFAULTS
        )
        with st.expander("Decomposition - advanced", expanded=False):
            kp = f"decomp_{decomposition_preset}_"
            decomposition_overrides["on_root_sequence"] = st.checkbox(
                "on_root_sequence",
                value=bool(_preset_defaults["on_root_sequence"]),
                key=kp + "rs",
                help="Each child of a top-level sequence becomes a plug-in map.",
            )
            decomposition_overrides["on_parallel"] = st.checkbox(
                "on_parallel",
                value=bool(_preset_defaults["on_parallel"]),
                key=kp + "par",
                help="Each branch of a parallel (+) becomes a plug-in map.",
            )
            decomposition_overrides["on_loop"] = st.checkbox(
                "on_loop",
                value=bool(_preset_defaults["on_loop"]),
                key=kp + "lp",
                help="Each loop (*) expansion becomes a plug-in map.",
            )
            decomposition_overrides["max_leaves_per_map"] = st.number_input(
                "max_leaves_per_map",
                min_value=1, max_value=500,
                value=int(_preset_defaults["max_leaves_per_map"]),
                step=1, key=kp + "mx",
                help="Hard cap on the activity-leaf count of any single map.",
            )
            decomposition_overrides["min_leaves_to_decompose"] = st.number_input(
                "min_leaves_to_decompose",
                min_value=1, max_value=100,
                value=int(_preset_defaults["min_leaves_to_decompose"]),
                step=1, key=kp + "mn",
                help="Subtrees smaller than this are never cut.",
            )
            decomposition_overrides["balance_ratio"] = st.slider(
                "balance_ratio",
                min_value=0.0, max_value=1.0,
                value=float(_preset_defaults["balance_ratio"]),
                step=0.05, key=kp + "br",
                help=(
                    "Siblings under a + or top-level -> are only extracted "
                    "when their share of the parent's leaves is at least "
                    "this fraction."
                ),
            )

    # ---- Buffered apply ----------------------------------------------------
    # Convert the live widget state to the cache-friendly spec form.
    if decomposition_preset == "off":
        candidate_spec: object = "off"
    else:
        candidate_spec = tuple(sorted(decomposition_overrides.items()))

    # First render: seed applied state from candidate so the initial
    # mine actually runs without waiting for an Apply click.
    if "applied_decomp" not in st.session_state:
        st.session_state["applied_decomp"] = candidate_spec

    if candidate_spec != st.session_state["applied_decomp"]:
        st.warning(
            "Decomposition settings have unapplied changes. "
            "Click **Apply changes** to remine."
        )
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
        help=(
            "Event attribute holding the performer name. "
            "Choose org:role / org:resource, pick 'Other...' to type a "
            "custom attribute name (or a fallback list like "
            "`org:role, org:resource, org:group`), or '(none)' to "
            "disable performer mining."
        ),
    )
    if resource_choice == _RES_OTHER:
        resource_attribute = st.text_input(
            "Custom attribute(s)",
            value="org:role, org:resource, org:group",
            help=(
                "Single attribute name, or a comma/whitespace-separated "
                "fallback list (first one set on each event wins)."
            ),
        )
    elif resource_choice == "(none)":
        resource_attribute = ""
    else:
        resource_attribute = resource_choice

    # Min support is meaningful only when a resource attribute is configured
    # (otherwise no performer mining happens at all).
    _min_support_disabled = not resource_attribute.strip()
    min_support = st.slider(
        "Min support",
        min_value=0.0, max_value=1.0, value=0.0, step=0.05,
        help=(
            "Minimum fraction of events for an activity that must agree "
            "on the same performer before the binding is kept. 0.0 "
            "(default) accepts the modal performer even when the resource "
            "pool is highly dispersed; raise (e.g. 0.5) to require a "
            "clear majority. Disabled when performer mining is off."
        ),
        disabled=_min_support_disabled,
    )

def _accept_log_bytes(name: str, payload: bytes) -> None:
    """Common entry point for both uploads and sample loads.

    Hashes the bytes, decides log_kind from the extension, resets any
    prior CSV column mapping, and seeds the CSV selectors with
    autodetected defaults when relevant.
    """
    new_hash = hashlib.sha256(payload).hexdigest()[:16]
    if new_hash == st.session_state.get("log_hash"):
        return  # nothing changed — keep current state intact
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
    # Reset the committed CSV column mapping so mining waits for
    # explicit confirmation on the new file. Also clear the seeding
    # gate so the CSV section's one-time per-file seed fires fresh
    # against this file's columns.
    st.session_state.pop("applied_csv_columns", None)
    st.session_state.pop("csv_seeded_for_hash", None)
    if kind == "csv":
        # Clear any previous selector state so the seeding gate
        # downstream doesn't see leftover values that happened to be
        # valid for the new columns (which would skip the autopick).
        for k, _, _ in _CSV_AUTOPICK:
            st.session_state.pop(k, None)


# ---- Log source ------------------------------------------------------------
samples = _list_samples()
src_tabs = (
    st.tabs(["Sample log", "Upload your own"])
    if samples else (None, st.container())
)

if samples:
    with src_tabs[0]:
        # Pretty display names: drop the extension(s) and replace
        # underscores with spaces. The Path object stays the source of
        # truth for the actual file load.
        def _label(p: Path) -> str:
            stem = p.name
            for suffix in (".xes.gz", ".xes", ".zip", ".gz"):
                if stem.lower().endswith(suffix):
                    stem = stem[: -len(suffix)]
                    break
            return stem.replace("_", " ").strip() or p.name

        label_to_path = {_label(p): p for p in samples}
        labels = list(label_to_path.keys())
        st.selectbox(
            "Choose a bundled log",
            options=labels,
            key="sample_choice",
            help=(
                "Pre-bundled XES event logs to make it easy to try the "
                "tool without hunting for an event log. "
                "Add more files to `web/samples/` to extend this list."
            ),
        )
        if st.button("Load sample", type="primary", key="load_sample"):
            chosen = label_to_path[st.session_state["sample_choice"]]
            _accept_log_bytes(chosen.name, chosen.read_bytes())
            st.rerun()

uploaded = src_tabs[1].file_uploader(
    "Upload an event log",
    type=["xes", "gz", "csv", "zip"],
    help=(
        "XES (.xes / .xes.gz) is mined directly. "
        ".zip archives are searched for the first .xes inside. "
        "CSV requires picking the case / activity / timestamp columns "
        "(and optionally role / resource) after upload."
    ),
    key="log_uploader",
)

# Persist the uploaded bytes in session_state so reruns triggered by
# the sidebar (notation toggle, "Apply changes", etc.) don't lose the
# log — st.file_uploader can return None on a programmatic st.rerun()
# in some Streamlit / browser combinations, which would otherwise drop
# the user back at the upload prompt.
if uploaded is not None:
    _accept_log_bytes(uploaded.name, uploaded.getvalue())

if "log_bytes" not in st.session_state:
    st.info("Upload a log to begin.")
    st.stop()

log_bytes = st.session_state["log_bytes"]
log_name = st.session_state["log_name"]
log_kind = st.session_state["log_kind"]
file_hash = st.session_state["log_hash"]
style = notation.lower()  # "ucm" / "bpmn"

# ---- CSV column mapping -----------------------------------------------------
# For CSV uploads, the user picks which columns hold the case id,
# activity name, timestamp, and optionally role / resource. Mining is
# blocked until the three required columns are chosen.
csv_columns: Optional[Tuple[str, str, str, str, str]] = None
if log_kind == "csv":
    columns = _csv_columns(log_bytes, file_hash)
    if not columns:
        st.error("Could not read columns from the uploaded CSV.")
        st.stop()

    st.subheader("CSV columns")
    # Seeding is gated strictly by file_hash. _accept_log_bytes() seeded
    # the selectors when this CSV was loaded; we do NOT touch them
    # again here on subsequent reruns. This guarantees the user's
    # role / resource picks cannot be silently overwritten by any
    # defensive re-seed pass — they only change when the user
    # interacts with the selectbox or uploads a different file.
    # The recovery path below covers the (rare) edge case where a
    # stored value is somehow no longer a valid option.
    _seeded_for = st.session_state.get("csv_seeded_for_hash")
    if _seeded_for != file_hash:
        _seed_csv_selectors(columns)
        st.session_state["csv_seeded_for_hash"] = file_hash
    else:
        # Safety net: if any stored value is no longer a valid option
        # (corrupted state, manual edit, etc.), reseed JUST that key so
        # Streamlit's selectbox doesn't crash. Valid values are
        # untouched.
        _valid_req = set(columns)
        _valid_opt = _valid_req | {_NONE_OPT}
        for _i, (_k, _cands, _with_none) in enumerate(_CSV_AUTOPICK):
            _valid = _valid_opt if _with_none else _valid_req
            if st.session_state.get(_k) not in _valid:
                st.session_state[_k] = _autopick_column(
                    columns, _cands,
                    include_none=_with_none,
                    fallback_index=_i,
                )

    cc1, cc2, cc3 = st.columns(3)
    case_col = cc1.selectbox(
        "Case id column", options=columns, key="csv_case",
    )
    activity_col = cc2.selectbox(
        "Activity column", options=columns, key="csv_activity",
    )
    ts_col = cc3.selectbox(
        "Timestamp column", options=columns, key="csv_timestamp",
    )

    cc4, cc5 = st.columns(2)
    _role_opts = [_NONE_OPT] + columns
    _resource_opts = [_NONE_OPT] + columns
    role_col = cc4.selectbox(
        "Role column (optional)", options=_role_opts, key="csv_role",
    )
    resource_col = cc5.selectbox(
        "Resource column (optional)", options=_resource_opts, key="csv_resource",
    )

    candidate_csv_columns = (
        case_col,
        activity_col,
        ts_col,
        "" if role_col == _NONE_OPT else role_col,
        "" if resource_col == _NONE_OPT else resource_col,
    )

    # Buffered apply: mining uses the *committed* mapping
    # (``applied_csv_columns``). On a fresh upload (no committed
    # mapping yet) mining is blocked until the user clicks Apply.
    # Once a mapping is committed, subsequent edits to the selectors
    # show a warning + remine button but do NOT block mining — this
    # way changes to other settings (notation, decomposition, etc.)
    # do not require re-confirming a column mapping that the user
    # happened to fiddle with.
    applied_csv_columns = st.session_state.get("applied_csv_columns")
    if applied_csv_columns is None:
        st.info(
            "Review the column mapping above, then click "
            "**Apply column mapping** to start mining."
        )
        if st.button("Apply column mapping", type="primary",
                     key="apply_csv_initial"):
            st.session_state["applied_csv_columns"] = candidate_csv_columns
            st.rerun()
        st.stop()
    elif applied_csv_columns != candidate_csv_columns:
        st.warning(
            "Column mapping has unapplied changes. "
            "Click **Apply column mapping** to remine."
        )
        if st.button("Apply column mapping", type="primary",
                     key="apply_csv_update"):
            st.session_state["applied_csv_columns"] = candidate_csv_columns
            st.rerun()
        # Do NOT st.stop() — mining continues with the previously
        # applied mapping so toggles elsewhere (notation, decomposition)
        # remain responsive.

    csv_columns = st.session_state["applied_csv_columns"]
# Effective min_support: when the slider is disabled, pass 0.0 to keep
# the cache key stable (so dragging the disabled slider — which Streamlit
# still records as a state change — doesn't invalidate the mining cache).
effective_min_support = 0.0 if _min_support_disabled else min_support

# Use the *applied* decomposition spec rather than the live widget
# values: the user can tweak the advanced controls freely; only
# clicking "Apply changes" (handled in the sidebar above) promotes the
# widget state into ``applied_decomp`` and triggers a remine on the
# next rerun.
decomposition_spec = st.session_state["applied_decomp"]

# Cache-hit short circuit: detect whether the call would hit the cache
# by comparing the arg fingerprint to the one we stored on the prior
# run. On a cache hit we skip the st.status / st.spinner wrappers
# entirely so flipping the notation toggle (which doesn't affect
# mining) leaves no UI trace. On a cache miss we wrap the call in
# st.status (mining) / st.spinner (rendering) for live feedback.
def _arg_fingerprint(*args) -> str:
    """Stable hash over a tuple of args, used to detect cache-key
    changes across reruns."""
    return hashlib.sha256(repr(args).encode("utf-8")).hexdigest()

_mining_fp = _arg_fingerprint(
    file_hash, log_kind, csv_columns, decomposition_spec,
    resource_attribute, effective_min_support, noise_threshold,
)
_mining_cache_hit = (
    st.session_state.get("last_mining_fp") == _mining_fp
)

try:
    if _mining_cache_hit:
        # Same inputs as last run -> guaranteed cache hit. Skip the
        # status panel so notation toggles, etc. look silent.
        mined = _mine(
            log_bytes, log_kind, csv_columns,
            decomposition_spec,
            resource_attribute, effective_min_support,
            noise_threshold,
            file_hash,
        )
    else:
        with st.status("Mining UCM...", expanded=False) as status:
            mined = _mine(
                log_bytes, log_kind, csv_columns,
                decomposition_spec,
                resource_attribute, effective_min_support,
                noise_threshold,
                file_hash,
                _status=status,
            )
            status.update(label="Done.", state="complete")
    st.session_state["last_mining_fp"] = _mining_fp

    # Same trick for rendering, keyed on (jucm hash, style). Switching
    # back to a notation we've already rendered hits the cache and
    # shows no spinner; first render of a new style shows the spinner.
    _render_fp = _arg_fingerprint(
        hashlib.sha256(mined["jucm"]).hexdigest(), style,
    )
    _render_cache_hit = (
        st.session_state.get("last_render_fp") == _render_fp
    )
    if _render_cache_hit:
        png_bytes = _render_cached(mined["jucm"], style)
    else:
        with st.spinner(f"Rendering {notation} diagram..."):
            png_bytes = _render_cached(mined["jucm"], style)
    st.session_state["last_render_fp"] = _render_fp
except Exception as exc:
    # Surface the short message inline; tuck the full traceback into
    # an expander so power users can debug without exposing a Python
    # stack to casual visitors.
    st.error(f"Mining failed: {type(exc).__name__}: {exc}")
    with st.expander("Show technical details"):
        st.code(traceback.format_exc(), language="text")
    st.stop()

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("File", log_name)
c2.metric("Notation", notation)
c3.metric("Decomposition", decomposition_preset)
c4.metric("Maps", mined["n_maps"])
c5.metric("Nodes", mined["n_nodes"])

# Embed the PNG via a raw <img> tag so the original bitmap is sent to
# the browser unchanged. ``width=`` is a CSS pixel size, NOT a resample:
# the browser only scales the displayed element. Right-click → "Open
# image in new tab" yields the file at its native resolution, and
# scroll-zooming the page magnifies the rendered bitmap directly.
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
    f"open in a new tab or zoom in for a closer look."
)

d1, d2 = st.columns(2)
d1.download_button(
    "Download PNG",
    data=png_bytes,
    file_name=_safe_download_name(Path(log_name).stem, ".png"),
    mime="image/png",
)
d2.download_button(
    "Download .jucm",
    data=mined["jucm"],
    file_name=_safe_download_name(Path(log_name).stem, ".jucm"),
    mime="application/xml",
)
