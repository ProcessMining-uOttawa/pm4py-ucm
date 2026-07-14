"""The original (V1, model-only) deployment path now serves the full
**V3** app — the code lives in ``streamlit_app_v3.py``.

This shim keeps the primary Streamlit Cloud deployment
(https://pm4py-ucm.streamlit.app/, whose *Main file path* setting says
``web/streamlit_app.py``) on the latest code without touching its
configuration. The V1 model-only app was retired when V3 became a
strict superset — its last version remains in git history (up to
v0.5.1). Note: ``streamlit_app_v2.py`` is NOT a shim — it is the
frozen V2 scenarios app that
https://pm4py-ucm-scenarios.streamlit.app/ keeps serving for a paper
under review.
"""
from pathlib import Path
import runpy

runpy.run_path(
    str(Path(__file__).with_name("streamlit_app_v3.py")),
    run_name="__main__",
)
