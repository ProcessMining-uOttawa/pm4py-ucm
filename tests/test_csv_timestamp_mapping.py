"""The CSV import must hand pm4py a real datetime timestamp column.

``pm4py.format_dataframe`` converts text columns with a silent
``try/except: pass`` and never touches numeric ones, so an ISO stamp its
default parser rejects, or an epoch column, reaches the first mine as
strings/ints and dies inside pm4py with "the dataframe should (at least)
contain a column of type date" — an opaque, redacted crash on Streamlit
Cloud (seen on the bundled devlog.csv). The app's ``_format_csv_df`` now
guarantees the datetime dtype itself, and names the offending column when
nothing in it parses.

The Streamlit script cannot be imported (its module body calls Streamlit),
so the two functions are sliced out of the source and executed headlessly,
the same way test_quick_reductions covers the resume path.
"""
from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import pytest

pm4py = pytest.importorskip("pm4py")

_ROOT = Path(__file__).resolve().parents[1]
_APP = _ROOT / "web" / "streamlit_app_v6.py"
_DEVLOG = _ROOT / "web" / "samples" / "devlog.csv"


def _sliced():
    src = _APP.read_text(encoding="utf-8")
    start = src.index("def _coerce_str_object(")
    end = src.index("def _read_log_for_scenarios(")
    import warnings
    ns = {"pd": pd, "pm4py": pm4py, "Dict": dict, "warnings": warnings}
    exec(compile(src[start:end], "<streamlit_app_v6 slice>", "exec"), ns)
    return ns


def _fmt(ns, df, ts="ts"):
    return ns["_format_csv_df"](df, "case", "act", ts, "", "")


def _events(stamps):
    return pd.DataFrame({
        "case": ["c1"] * len(stamps),
        "act": [f"a{i}" for i in range(len(stamps))],
        "ts": stamps,
    })


def test_iso8601_with_t_and_z_is_datetime():
    ns = _sliced()
    out = _fmt(ns, _events(["2026-07-17T20:27:06.425Z",
                            "2026-07-17T20:27:06.937Z"]))
    assert pd.api.types.is_datetime64_any_dtype(out["time:timestamp"])
    # And pm4py's own gate — the one the Cloud crash came from — passes.
    pm4py.discover_process_tree_inductive(out, noise_threshold=0.0)


def test_bundled_devlog_sample_mines():
    ns = _sliced()
    df = pd.read_csv(_DEVLOG, low_memory=False)
    out = ns["_format_csv_df"](
        df, "case:concept:name", "concept:name", "time:timestamp",
        "", "org:resource")
    assert pd.api.types.is_datetime64_any_dtype(out["time:timestamp"])
    assert len(out) == len(df)


def test_coerces_when_pm4py_leaves_strings(monkeypatch):
    """Simulate the deployed pm4py: format_dataframe returns the stamps
    untouched (its parse failed silently). We must still end up datetime."""
    ns = _sliced()
    real = pm4py.format_dataframe

    def leave_strings(df, case_id, activity_key, timestamp_key, **kw):
        out = real(df, case_id=case_id, activity_key=activity_key,
                   timestamp_key=timestamp_key, **kw)
        out["time:timestamp"] = df[timestamp_key].astype(str).values
        return out

    monkeypatch.setattr(ns["pm4py"], "format_dataframe", leave_strings)
    out = _fmt(ns, _events(["2026-07-17T20:27:06.425Z",
                            "2026-07-17 20:27:07"]))
    assert pd.api.types.is_datetime64_any_dtype(out["time:timestamp"])
    assert out["time:timestamp"].notna().all()


@pytest.mark.parametrize("stamps, unit", [
    ([1717372800, 1717406437], "s"),
    ([1717372800000, 1717406437327], "ms"),
])
def test_integer_epoch_column_becomes_datetime(stamps, unit):
    ns = _sliced()
    out = _fmt(ns, _events(stamps))
    ts = out["time:timestamp"]
    assert pd.api.types.is_datetime64_any_dtype(ts)
    assert ts.iloc[0] == pd.Timestamp("2024-06-03", tz="UTC")


def test_unreadable_timestamp_column_names_itself():
    ns = _sliced()
    df = _events(["EAIMEL", "TL32860", "ST32611"])
    with pytest.raises(ValueError, match=r"mapped as timestamp, `ts`"):
        _fmt(ns, df)


def test_mostly_text_column_is_rejected_not_thinned():
    """One stray date in a free-text column is a wrong mapping."""
    ns = _sliced()
    df = _events(["hello", "world", "2024-06-03 09:20:37", "x", "y"])
    with pytest.raises(ValueError):
        _fmt(ns, df)


def test_unparsable_rows_are_dropped_like_format_dataframe():
    ns = _sliced()
    real = pm4py.format_dataframe
    # pm4py drops NaT rows itself when its parse works; make it not work so
    # the fallback is the one dropping.
    import unittest.mock as um
    with um.patch.object(ns["pm4py"], "format_dataframe",
                         side_effect=lambda df, **kw: (
                             lambda o: (o.__setitem__(
                                 "time:timestamp",
                                 df[kw["timestamp_key"]].astype(str).values),
                                 o)[1])(real(df, **kw))):
        out = _fmt(ns, _events(["2026-07-17T20:27:06Z", "not a date",
                                "2026-07-17T20:27:08Z"]))
    assert len(out) == 2
    assert out["time:timestamp"].notna().all()
