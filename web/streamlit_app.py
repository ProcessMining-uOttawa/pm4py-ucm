"""The primary deployment path now serves the full **V4** app — the code
lives in ``streamlit_app_v4.py`` (the workspace shell plus the Dashboards
view; a strict superset of V3).

This shim keeps the primary Streamlit Cloud deployment
(https://pm4py-ucm.streamlit.app/, whose *Main file path* setting says
``web/streamlit_app.py``) on the latest code without touching its
configuration. V3 (``streamlit_app_v3.py``) and V1 remain in git history;
V3 is a strict subset of V4. Note: ``streamlit_app_v2.py`` is NOT a shim —
it is the frozen V2 scenarios app that
https://pm4py-ucm-scenarios.streamlit.app/ keeps serving for a paper
under review.
"""
from pathlib import Path
import runpy

runpy.run_path(
    str(Path(__file__).with_name("streamlit_app_v4.py")),
    run_name="__main__",
)
