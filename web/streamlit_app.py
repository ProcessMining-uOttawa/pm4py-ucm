"""The primary deployment path now serves the full **V5** app — the code
lives in ``streamlit_app_v5.py`` (the workspace shell plus the Dashboards
view, log filtering and activity renaming; a strict superset of V4/V3).

This shim keeps the primary Streamlit Cloud deployment
(https://pm4py-ucm.streamlit.app/, whose *Main file path* setting says
``web/streamlit_app.py``) on the latest code without touching its
configuration. V3 (``streamlit_app_v3.py``) and V1 remain in git history;
V4/V3 are strict subsets of V5. Note: ``streamlit_app_v2.py`` is NOT a shim —
it is the frozen V2 scenarios app that
https://pm4py-ucm-scenarios.streamlit.app/ keeps serving for a paper
under review.
"""
from pathlib import Path
import runpy

runpy.run_path(
    str(Path(__file__).with_name("streamlit_app_v5.py")),
    run_name="__main__",
)
