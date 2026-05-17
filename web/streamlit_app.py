"""pm4py-ucm web front-end (MVP).

Run locally:
    streamlit run web/streamlit_app.py

Flow: upload XES -> inductive-mine a UCM -> render PNG -> download .jucm.
Later iterations add notation toggle, decomposition, performer config, CSV.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import streamlit as st

import pm4py
import pm4py_ucm


st.set_page_config(page_title="pm4py-ucm", layout="wide")
st.title("pm4py-ucm")
st.caption("Mine a Use Case Map from an XES event log and export to jUCMNav.")


@st.cache_data(show_spinner="Mining UCM...")
def _mine(xes_bytes: bytes, _file_hash: str):
    """Read XES, mine UCM, render PNG, serialize .jucm. Cached per file hash."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        xes_path = td / "log.xes"
        xes_path.write_bytes(xes_bytes)

        log = pm4py.read_xes(str(xes_path))
        ucm = pm4py_ucm.discover_ucm_inductive(log)

        png_path = td / "model.png"
        pm4py_ucm.save_vis_ucm(ucm, str(png_path))
        jucm_path = td / "model.jucm"
        pm4py_ucm.write_ucm(ucm, str(jucm_path))

        return {
            "png": png_path.read_bytes(),
            "jucm": jucm_path.read_bytes(),
            "n_maps": len(ucm.maps),
            "n_nodes": sum(len(m.nodes) for m in ucm.maps),
        }


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

try:
    result = _mine(xes_bytes, file_hash)
except Exception as exc:
    st.error(f"Mining failed: {exc}")
    st.stop()

c1, c2, c3 = st.columns(3)
c1.metric("File", uploaded.name)
c2.metric("Maps", result["n_maps"])
c3.metric("Nodes", result["n_nodes"])

st.image(result["png"], caption="Mined UCM", use_column_width=True)

st.download_button(
    "Download .jucm",
    data=result["jucm"],
    file_name=Path(uploaded.name).stem + ".jucm",
    mime="application/xml",
)
