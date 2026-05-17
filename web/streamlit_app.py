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
    decomposition: str,
    resource_attribute: str,
    min_support: float,
    _file_hash: str,
):
    """Read XES and mine a UCM. Returns the .jucm bytes plus metadata.

    Cached per (file, decomposition, resource_attribute, min_support).
    Notation does **not** affect mining, so it is intentionally absent
    from the cache key — toggling UCM <-> BPMN goes through
    :func:`_render_cached` without rerunning the inductive miner.

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

        ucm = pm4py_ucm.discover_ucm_inductive(
            log, parameters=params, decomposition=decomposition,
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
    decomposition = st.selectbox(
        "Decomposition",
        options=["off", "auto", "aggressive"],
        index=0,
        help=(
            "off: single flat map. "
            "auto: split into a root map plus plug-in maps when the "
            "model is large enough to benefit. "
            "aggressive: same boundary rules with a tighter cap, "
            "producing more / smaller plug-ins."
        ),
    )

    st.subheader("Performers")
    resource_attribute = st.text_input(
        "Resource attribute",
        value="org:resource",
        help=(
            "Event attribute holding the performer name. "
            "Pass a fallback list like "
            "`org:role, org:resource, org:group` to use the first one "
            "that's set on each event. Leave empty to disable performer "
            "mining."
        ),
    )
    # Min support is meaningful only when both:
    #   * a resource attribute is configured (otherwise no performer
    #     mining happens at all), and
    #   * decomposition is on (the support filter is most useful when
    #     deciding which performer "owns" a sub-map; with decomposition
    #     off the slider has no visible effect on the rendered diagram).
    _min_support_disabled = (
        not resource_attribute.strip() or decomposition == "off"
    )
    min_support = st.slider(
        "Min support",
        min_value=0.0, max_value=1.0, value=0.0, step=0.05,
        help=(
            "Minimum fraction of events for an activity that must agree "
            "on the same performer before the binding is kept. 0.0 "
            "(default) accepts the modal performer even when the resource "
            "pool is highly dispersed; raise (e.g. 0.5) to require a "
            "clear majority. Disabled when performer mining is off, or "
            "when decomposition is off."
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

try:
    mined = _mine(
        xes_bytes, decomposition,
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
c3.metric("Decomposition", decomposition)
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
    f"Mined model ({notation}, decomposition={decomposition}) — "
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
