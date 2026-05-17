"""pm4py-ucm web front-end.

Run locally:
    streamlit run web/streamlit_app.py

Flow: upload XES -> inductive-mine a UCM -> render high-DPI PNG in the
selected notation (UCM or BPMN) -> download .jucm.

Resolution note: pm4py-ucm's visualizer builds a :class:`graphviz.Digraph`
with hardcoded ``graph_attr`` and no DPI knob. To get a crisp PNG without
touching the package, we call the visualizer to obtain the Digraph,
inject ``dpi`` into its ``graph_attr``, then render. For multi-map UCMs
we render each map individually and composite with the package's own
``stacked._composite`` helper so we get the same titled, separated layout
the CLI produces.
"""
from __future__ import annotations

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


# DPI for the rendered PNG. 220 roughly doubles graphviz's default of 96
# (graphviz scales fonts and line widths with DPI, so the result is a
# straight up-res rather than just a bigger bitmap of the same image).
_PNG_DPI = 220


st.set_page_config(page_title="pm4py-ucm", layout="wide")
st.title("pm4py-ucm")
st.caption("Mine a Use Case Map from an XES event log and export to jUCMNav.")


def _render_high_dpi_png(ucm, style: str, out_path: str) -> str:
    """Render ``ucm`` to ``out_path`` at :data:`_PNG_DPI`.

    Handles the single-map case directly and uses the package's
    composite helper for multi-map UCMs.
    """
    params = {"style": style}

    if len(ucm.maps) <= 1:
        gviz = _visualizer.apply(ucm, parameters=params)
        gviz.graph_attr["dpi"] = str(_PNG_DPI)
        return _visualizer.save(gviz, out_path)

    # Multi-map: replicate stacked._render_each but with DPI injection,
    # then reuse the package's compositor for the title strips and
    # separators.
    from pm4py_ucm.visualization.ucm.variants import classic as _classic

    panels: List[Tuple[str, str]] = []
    tmpdir = tempfile.mkdtemp(prefix="pm4py_ucm_web_")
    for idx, ucm_map in enumerate(ucm.maps):
        per = dict(params)
        per["map_index"] = idx
        per["format"] = "png"
        gviz = _classic.apply(ucm, parameters=per)
        gviz.graph_attr["dpi"] = str(_PNG_DPI)
        gviz.format = "png"
        base = os.path.join(tmpdir, f"map_{idx:03d}")
        rendered = gviz.render(filename=base, cleanup=True)
        panels.append((ucm_map.name or f"Map{idx}", rendered))
    return _stacked._composite(panels, out_path)


@st.cache_data(show_spinner="Mining UCM...")
def _mine(xes_bytes: bytes, style: str, decomposition: str, _file_hash: str):
    """Read XES, mine UCM, render high-DPI PNG, serialize .jucm.

    Cached per (file hash, style, decomposition) so toggling notation or
    the decomposition mode re-renders without redoing the steps that
    haven't changed (mining is rerun only when the decomposition mode
    changes — it affects the mined model itself).
    """
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        xes_path = td / "log.xes"
        xes_path.write_bytes(xes_bytes)

        log = pm4py.read_xes(str(xes_path))
        ucm = pm4py_ucm.discover_ucm_inductive(
            log, decomposition=decomposition,
        )

        png_path = td / "model.png"
        _render_high_dpi_png(ucm, style, str(png_path))
        jucm_path = td / "model.jucm"
        pm4py_ucm.write_ucm(ucm, str(jucm_path))

        return {
            "png": png_path.read_bytes(),
            "jucm": jucm_path.read_bytes(),
            "n_maps": len(ucm.maps),
            "n_nodes": sum(len(m.nodes) for m in ucm.maps),
        }


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

try:
    result = _mine(xes_bytes, style, decomposition, file_hash)
except Exception as exc:
    st.error(f"Mining failed: {exc}")
    st.stop()

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("File", uploaded.name)
c2.metric("Notation", notation)
c3.metric("Decomposition", decomposition)
c4.metric("Maps", result["n_maps"])
c5.metric("Nodes", result["n_nodes"])

# Streamlit 1.36+ deprecated ``use_column_width``; ``width="stretch"``
# is the supported replacement (image fills its container width).
st.image(
    result["png"],
    caption=f"Mined model ({notation}, decomposition={decomposition})",
    width="stretch",
)

st.download_button(
    "Download .jucm",
    data=result["jucm"],
    file_name=Path(uploaded.name).stem + ".jucm",
    mime="application/xml",
)
