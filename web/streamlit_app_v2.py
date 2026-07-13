"""Compatibility shim — the V2 app became **V3** when the model-family
statistics reports landed; the code now lives in
``streamlit_app_v3.py``.

This path keeps existing deployments working (the Streamlit Community
Cloud app's *Main file path* setting still says
``web/streamlit_app_v2.py``). Point new deployments — and local runs —
at ``web/streamlit_app_v3.py`` instead.
"""
from pathlib import Path
import runpy

runpy.run_path(
    str(Path(__file__).with_name("streamlit_app_v3.py")),
    run_name="__main__",
)
