"""The CSV logs bundled in ``web/samples`` must mine straight after
*Load sample*: the column auto-detection has to land on the case, activity
and timestamp columns without the user touching the mapping.

``RefundProcess`` is the cautionary case. Its original export named the
columns ``Order Nr., Status, Employee, Complete Timestamp`` — no name the
auto-detection knows — so the positional fallback mapped the timestamp to
the *employee* column. The bundled copy carries standard headers and ISO
dates instead.
"""
from pathlib import Path

import pandas as pd
import pm4py
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_APP = _ROOT / "web" / "streamlit_app_v6.py"
_SAMPLES = _ROOT / "web" / "samples"

# name -> (cases, events, activities, expected (case, activity, timestamp, role, resource))
_EXPECTED = {
    "Procure-to-Pay.csv": (608, 9119, 24,
                           ("Case ID", "Activity", "Complete Timestamp", "Role", "Resource")),
    "RefundProcess.csv": (1338, 9032, 32,
                          ("Case ID", "Activity", "Timestamp", "(none)", "Resource")),
}


def _slice(start, end, ns):
    src = _APP.read_text(encoding="utf-8")
    exec(compile(src[src.index(start):src.index(end)], "<streamlit_app_v6 slice>", "exec"), ns)
    return ns


def _app():
    import warnings
    ns = {"pd": pd, "pm4py": pm4py, "Dict": dict, "warnings": warnings, "_NONE_OPT": "(none)"}
    _slice("_CSV_AUTOPICK = [", "# -----", ns)
    _slice("def _autopick_column(", "def _seed_csv_selectors(", ns)
    _slice("def _coerce_str_object(", "def _read_log_for_scenarios(", ns)
    return ns


def _autodetect(ns, columns):
    return tuple(
        ns["_autopick_column"](columns, cands, include_none=with_none, fallback_index=i)
        for i, (_key, cands, with_none) in enumerate(ns["_CSV_AUTOPICK"]))


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_bundled_csv_sample_autodetects_and_mines(name):
    cases, events, activities, mapping = _EXPECTED[name]
    ns = _app()
    df = pd.read_csv(_SAMPLES / name, low_memory=False)
    picked = _autodetect(ns, list(df.columns))
    assert picked == mapping
    case_col, act_col, ts_col, role_col, res_col = picked
    out = ns["_format_csv_df"](df, case_col, act_col, ts_col,
                               "" if role_col == "(none)" else role_col,
                               "" if res_col == "(none)" else res_col)
    assert pd.api.types.is_datetime64_any_dtype(out["time:timestamp"])
    assert len(out) == events
    assert out["case:concept:name"].nunique() == cases
    assert out["concept:name"].nunique() == activities
    pm4py.discover_process_tree_inductive(out, noise_threshold=0.2)


def test_every_bundled_csv_is_covered():
    """A new CSV sample must come with its expected mapping here."""
    bundled = {p.name for p in _SAMPLES.glob("*.csv")} - {"devlog.csv"}
    assert bundled == set(_EXPECTED)
