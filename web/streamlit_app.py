"""pm4py-ucm web front-end.

Run locally:
    streamlit run web/streamlit_app.py

Flow: upload XES -> inductive-mine a UCM -> render PNG in the selected
notation (UCM or BPMN) -> download the PNG and/or the .jucm.

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
import os
import tempfile
from pathlib import Path
from typing import List, Tuple

import streamlit as st

import pm4py
import pm4py_ucm
from pm4py_ucm.visualization.ucm import visualizer as _visualizer
from pm4py_ucm.visualization.ucm import stacked as _stacked

# Pillow ships a ~179M-pixel "decompression bomb" guard. Even at
# graphviz's default DPI, very large multi-map UCMs can exceed it
# (PMM4RPA and similarly broad logs), which would make
# ``stacked._composite`` crash with ``DecompressionBombError``. Disable
# the check — we render the PNGs ourselves so the guard is not doing
# useful work here.
from PIL import Image as _PILImage
_PILImage.MAX_IMAGE_PIXELS = None


# Display width (CSS pixels) for the in-browser preview. The underlying
# PNG keeps its native pixel dimensions — the browser scales the IMG
# element down, which means right-click "Open image in new tab" still
# shows the bitmap at full resolution and the user can scroll-zoom in
# place if they want a closer look.
_DISPLAY_WIDTH_PX = 1100


st.set_page_config(page_title="pm4py-ucm", layout="wide")
st.title("pm4py-ucm")
st.caption("Mine a Use Case Map from an XES event log and export to jUCMNav.")


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


@st.cache_data(show_spinner="Mining UCM...")
def _mine(
    xes_bytes: bytes,
    decomposition_spec,  # str "off" or tuple-of-(key, value) pairs
    resource_attribute: str,
    min_support: float,
    _file_hash: str,
):
    """Read XES and mine a UCM. Returns the .jucm bytes plus metadata.

    Cached per (file, decomposition_spec, resource_attribute, min_support).
    Notation does **not** affect mining, so it is intentionally absent
    from the cache key — toggling UCM <-> BPMN goes through
    :func:`_render_cached` without rerunning the inductive miner.

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
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        xes_path = td / "log.xes"
        xes_path.write_bytes(xes_bytes)

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

        ucm = pm4py_ucm.discover_ucm_inductive(
            log, parameters=params, decomposition=decomp_arg,
        )

        jucm_path = td / "model.jucm"
        pm4py_ucm.write_ucm(ucm, str(jucm_path))

        return {
            "jucm": jucm_path.read_bytes(),
            "n_maps": len(ucm.maps),
            "n_nodes": sum(len(m.nodes) for m in ucm.maps),
        }


@st.cache_data(show_spinner="Rendering diagram...")
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

uploaded = st.file_uploader(
    "Upload an XES event log",
    type=["xes", "gz"],
    help="Standard XES (.xes) or gzip-compressed (.xes.gz).",
)

if uploaded is None:
    st.info("Upload a log to begin.")
    st.stop()

xes_bytes = uploaded.getvalue()
file_hash = hashlib.sha256(xes_bytes).hexdigest()[:16]
style = notation.lower()  # "ucm" / "bpmn"
# Effective min_support: when the slider is disabled, pass 0.0 to keep
# the cache key stable (so dragging the disabled slider — which Streamlit
# still records as a state change — doesn't invalidate the mining cache).
effective_min_support = 0.0 if _min_support_disabled else min_support

# Build the cache-friendly decomposition spec. "off" stays a literal
# string; otherwise we pass a sorted tuple of (key, value) pairs so the
# cache key is hashable and order-independent. The miner reconstructs
# the dict.
if decomposition_preset == "off":
    decomposition_spec: object = "off"
else:
    decomposition_spec = tuple(sorted(decomposition_overrides.items()))

try:
    mined = _mine(
        xes_bytes, decomposition_spec,
        resource_attribute, effective_min_support,
        file_hash,
    )
    png_bytes = _render_cached(mined["jucm"], style)
except Exception as exc:
    st.error(f"Mining failed: {exc}")
    st.stop()

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("File", uploaded.name)
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
    file_name=Path(uploaded.name).stem + ".png",
    mime="image/png",
)
d2.download_button(
    "Download .jucm",
    data=mined["jucm"],
    file_name=Path(uploaded.name).stem + ".jucm",
    mime="application/xml",
)
