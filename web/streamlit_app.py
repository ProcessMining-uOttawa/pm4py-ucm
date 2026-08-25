"""The primary deployment path now serves the full **V6** app — the code
lives in ``streamlit_app_v6.py`` (V5's workspace shell, Dashboards, log
filtering and activity renaming, plus the pre-mining **cost screen** that
sizes a log before mining it and gates the replay-based metrics behind a
measured estimate).

This shim keeps the primary Streamlit Cloud deployment
(https://pm4py-ucm.streamlit.app/, whose *Main file path* setting says
``web/streamlit_app.py``) on the latest code without touching its
configuration. V5/V4/V3 remain in git history and are strict subsets of V6.
Note: ``streamlit_app_v2.py`` is NOT a shim — it is the frozen V2 scenarios
app that https://pm4py-ucm-scenarios.streamlit.app/ keeps serving for a
paper under review.
"""
from pathlib import Path
import runpy

runpy.run_path(
    str(Path(__file__).with_name("streamlit_app_v6.py")),
    run_name="__main__",
)
