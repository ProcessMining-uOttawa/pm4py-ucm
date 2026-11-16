"""Upload edge cases the readers used to crash or silently misread.

Found by probing the loaders after two file-loading crashes in a row (the
silent CSV timestamp, the un-inflated .xes.gz). Each case here is one the
audit turned up:

* ';'- or tab-delimited CSV read as a single column (European Excel);
* cp1252 / UTF-16 CSV raised UnicodeDecodeError from the cost screen;
* a ``.csv.gz`` was routed to the XES parser by its name;
* a Finder zip's ``__MACOSX/._log.xes`` twin was picked instead of the log;
* ``03/06/2024`` read month-first with no warning.

The Streamlit script cannot be imported (its module body calls Streamlit),
so the helpers are sliced out of the source and run against stubs, as the
other app-slice tests do.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import types
import warnings
import zipfile
from pathlib import Path
from typing import Dict, List

import pandas as pd
import pytest

pm4py = pytest.importorskip("pm4py")

_ROOT = Path(__file__).resolve().parents[1]
_APP = _ROOT / "web" / "streamlit_app_v6.py"

_BASE = ("case,activity,timestamp\n"
         "1,A,2024-06-03 10:00:00\n"
         "1,B,2024-06-03 11:00:00\n"
         "2,A,2024-06-04 10:00:00\n")
_XES = (b'<?xml version="1.0"?><log xes.version="1.0"><trace>'
        b'<string key="concept:name" value="c"/><event>'
        b'<string key="concept:name" value="a"/>'
        b'<date key="time:timestamp" value="2024-01-01T00:00:00+00:00"/>'
        b'</event></trace></log>')


def _passthrough_cache(*_a, **_k):
    def deco(fn):
        return fn
    if _a and callable(_a[0]) and not _k:
        return _a[0]
    return deco


def _ns():
    """Slice the reader helpers + the log-accept function out of the app."""
    src = _APP.read_text(encoding="utf-8")

    def cut(start, end):
        return src[src.index(start):src.index(end)]

    code = "\n\n".join([
        cut("_CSV_AUTOPICK = [", "# -----"),
        cut("def _extract_xes_from_zip(", "def _html_escape_min("),
        cut("def _coerce_str_object(", "def _read_log_for_scenarios("),
        cut("def _accept_log_bytes(", "# ---- Resume a saved project"),
    ])
    st = types.SimpleNamespace(session_state={}, cache_data=_passthrough_cache)
    ns = {"st": st, "pd": pd, "pm4py": pm4py, "io": io, "zipfile": zipfile,
          "Path": Path, "hashlib": hashlib, "warnings": warnings,
          "Dict": Dict, "List": List, "_NONE_OPT": "(none)"}
    exec(compile(code, "<streamlit_app_v6 slice>", "exec"), ns)
    return ns


def _zip_with(*entries) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in entries:
            zf.writestr(name, payload)
    return buf.getvalue()


# ---- delimiter + encoding ---------------------------------------------------

@pytest.mark.parametrize("label, payload", [
    ("semicolon", _BASE.replace(",", ";").encode()),
    ("tab", _BASE.replace(",", "\t").encode()),
    ("pipe", _BASE.replace(",", "|").encode()),
    ("utf8-bom", ("﻿" + _BASE).encode("utf-8")),
    ("utf16", _BASE.encode("utf-16")),
    ("utf16 tabs", _BASE.replace(",", "\t").encode("utf-16")),
    ("crlf", _BASE.replace("\n", "\r\n").encode()),
])
def test_delimiter_and_encoding_are_sniffed(label, payload):
    ns = _ns()
    assert ns["_csv_columns"](payload, "fh") == ["case", "activity", "timestamp"]
    df = ns["_read_csv_bytes"](payload)
    out = ns["_format_csv_df"](df, "case", "activity", "timestamp", "", "")
    assert len(out) == 3
    assert list(out["concept:name"]) == ["A", "B", "A"]


def test_cp1252_accents_survive():
    ns = _ns()
    payload = ("case;activité;timestamp\n1;Réception;2024-06-03 10:00:00\n"
               "1;Décision;2024-06-03 11:00:00\n").encode("cp1252")
    assert ns["_csv_columns"](payload, "fh") == ["case", "activité", "timestamp"]
    df = ns["_read_csv_bytes"](payload)
    out = ns["_format_csv_df"](df, "case", "activité", "timestamp", "", "")
    assert list(out["concept:name"]) == ["Réception", "Décision"]


def test_header_only_and_single_column_do_not_raise():
    ns = _ns()
    assert ns["_csv_columns"](b"case,activity,timestamp\n", "fh") == [
        "case", "activity", "timestamp"]
    assert ns["_csv_columns"](b"onlyone\n1\n2\n", "fh") == ["onlyone"]
    assert ns["_csv_columns"](b"", "fh") == []


# ---- zip layouts ----------------------------------------------------------

def test_finder_zip_skips_macosx_twin():
    ns = _ns()
    z = _zip_with(("__MACOSX/._log.xes", b"\x00\x05\x16junk"), ("log.xes", _XES))
    assert ns["_plain_xes_bytes"](z, "zip") == _XES


def test_dot_underscore_twin_beside_the_log_is_skipped():
    ns = _ns()
    z = _zip_with(("._log.xes", b"junk"), ("log.xes", _XES))
    assert ns["_plain_xes_bytes"](z, "zip") == _XES


def test_zip_with_gz_inside_and_nested_dir():
    ns = _ns()
    z = _zip_with(("exports/2026/log.xes.gz", gzip.compress(_XES)))
    assert ns["_plain_xes_bytes"](z, "zip") == _XES


# ---- kind detection on upload ---------------------------------------------

def _accept(ns, name, payload):
    ns["st"].session_state.clear()
    ns["_accept_log_bytes"](name, payload)
    ss = ns["st"].session_state
    return ss["log_kind"], ss["log_name"], ss["log_bytes"]


def test_csv_gz_is_inflated_and_treated_as_csv():
    ns = _ns()
    kind, name, payload = _accept(ns, "events.csv.gz", gzip.compress(_BASE.encode()))
    assert kind == "csv"
    assert name == "events.csv"
    assert payload == _BASE.encode()


def test_bare_gz_of_csv_is_judged_by_content():
    ns = _ns()
    kind, _, payload = _accept(ns, "mystery.gz", gzip.compress(_BASE.encode()))
    assert kind == "csv"
    assert payload == _BASE.encode()


def test_xes_gz_stays_on_the_xes_path_compressed():
    ns = _ns()
    gz = gzip.compress(_XES)
    kind, name, payload = _accept(ns, "log.xes.gz", gz)
    assert (kind, name, payload) == ("xes", "log.xes.gz", gz)
    kind, _, payload = _accept(ns, "renamed.gz", gz)   # XML inside → XES
    assert kind == "xes" and payload == gz


@pytest.mark.parametrize("name, kind", [
    ("a.csv", "csv"), ("a.tsv", "csv"), ("a.zip", "zip"),
    ("a.xes", "xes"), ("A.XES.GZ", "xes"),
])
def test_plain_names_keep_their_kind(name, kind):
    ns = _ns()
    assert _accept(ns, name, b"x")[0] == kind


# ---- day-first dates ------------------------------------------------------

_AMBIG = ("case,activity,timestamp\n"
          "1,A,03/06/2024 10:00\n"
          "1,B,04/06/2024 10:00\n"
          "1,C,05/06/2024 10:00\n").encode()


def test_ambiguous_dates_are_flagged_and_iso_is_not():
    ns = _ns()
    assert ns["_csv_dayfirst_ambiguous"](_AMBIG, "timestamp", "fh") is True
    assert ns["_csv_dayfirst_ambiguous"](_BASE.encode(), "timestamp", "fh") is False


def test_dayfirst_flag_changes_the_reading():
    ns = _ns()
    df = ns["_read_csv_bytes"](_AMBIG)
    mf = ns["_format_csv_df"](df.copy(), "case", "activity", "timestamp", "", "")
    dfst = ns["_format_csv_df"](df.copy(), "case", "activity", "timestamp", "", "",
                                True)
    assert mf["time:timestamp"].iloc[0] == pd.Timestamp("2024-03-06 10:00", tz="UTC")
    assert dfst["time:timestamp"].iloc[0] == pd.Timestamp("2024-06-03 10:00", tz="UTC")
    # Day-first keeps the three events in the order the log meant.
    assert dfst["time:timestamp"].is_monotonic_increasing


def test_six_element_mapping_flows_through_the_loader():
    """The optional sixth element rides the existing csv_columns plumbing."""
    ns = _ns()
    src = _APP.read_text(encoding="utf-8")
    code = src[src.index("def _read_log_for_scenarios("):
               src.index("def _load_log_df(")]
    code = code[:code.rindex("@st.cache_data")]   # drop the next def's decorator
    ns2 = dict(ns)
    import tempfile
    ns2["tempfile"] = tempfile
    exec(compile(code, "<slice>", "exec"), ns2)
    out = ns2["_read_log_for_scenarios"](
        _AMBIG, "csv", ("case", "activity", "timestamp", "", "", True))
    assert out["time:timestamp"].iloc[0] == pd.Timestamp("2024-06-03 10:00", tz="UTC")
    out5 = ns2["_read_log_for_scenarios"](
        _AMBIG, "csv", ("case", "activity", "timestamp", "", ""))
    assert out5["time:timestamp"].iloc[0] == pd.Timestamp("2024-03-06 10:00", tz="UTC")


# -- Start timestamp: a CSV as an interval log (#153) -----------------------

_INTERVAL = (b"case,activity,start,end,who\n"
             b"c1,a,2024-01-01 09:00,2024-01-01 09:30,ann\n"
             b"c1,b,2024-01-01 10:00,2024-01-01 11:00,bob\n"
             b"c2,a,2024-01-02 09:00,2024-01-02 09:10,ann\n")


def _fmt(ns, payload, *mapping):
    return ns["_format_csv_df"](ns["_read_csv_bytes"](payload), *mapping)


def test_start_and_end_columns_are_autodetected():
    ns = _ns()
    cols = ns["_csv_columns"](_INTERVAL, "fh")
    picked = [ns["_autopick_column"](cols, cands, include_none=none,
                                     fallback_index=i)
              for i, (_k, cands, none) in enumerate(ns["_CSV_AUTOPICK"])]
    assert picked == ["case", "activity", "end", "(none)", "(none)", "start"]


def test_a_start_that_is_also_the_positional_timestamp_is_not_seeded():
    """With no name the timestamp knows, it falls back to column 2, which
    here is the start column itself. Seeding must not map both to it."""
    ns = _ns()
    ns["_seed_csv_selectors"](["case", "activity", "start_time", "finish"])
    state = ns["st"].session_state
    assert state["csv_timestamp"] == "start_time"
    assert state["csv_start"] == "(none)"


def test_mapped_start_makes_an_interval_log():
    ns = _ns()
    out = _fmt(ns, _INTERVAL, "case", "activity", "end", "", "who", False, "start")
    assert "start" not in out.columns
    service = (out["time:timestamp"] - out["start_timestamp"]).dt.total_seconds()
    assert sorted(service) == [600.0, 1800.0, 3600.0]


def test_unmapped_start_gives_a_single_timestamp_log():
    """The mapping decides, not the header: a column literally called
    start_timestamp is dropped when no start is mapped."""
    ns = _ns()
    literal = _INTERVAL.replace(b"start,end", b"start_timestamp,end")
    out = _fmt(ns, literal, "case", "activity", "end", "", "")
    assert "start_timestamp" not in out.columns
    out = _fmt(ns, literal, "case", "activity", "end", "", "", False,
               "start_timestamp")
    assert out["start_timestamp"].notna().all()


def test_start_follows_the_day_first_switch():
    ns = _ns()
    payload = (b"case,activity,start,end\n"
               b"c1,a,03/06/2024 09:00,03/06/2024 10:00\n"
               b"c1,b,04/06/2024 09:00,04/06/2024 10:00\n")
    out = _fmt(ns, payload, "case", "activity", "end", "", "", True, "start")
    assert out["start_timestamp"].iloc[0] == pd.Timestamp("2024-06-03 09:00", tz="UTC")
    assert out["time:timestamp"].iloc[0] == pd.Timestamp("2024-06-03 10:00", tz="UTC")


def test_epoch_starts_are_read():
    ns = _ns()
    payload = (b"case,activity,start,end\n"
               b"c1,a,1704099600,1704101400\n"      # 09:00 -> 09:30 UTC
               b"c1,b,1704103200,1704106800\n")
    out = _fmt(ns, payload, "case", "activity", "end", "", "", False, "start")
    service = (out["time:timestamp"] - out["start_timestamp"]).dt.total_seconds()
    assert list(service) == [1800.0, 3600.0]


def test_unreadable_start_keeps_the_row():
    """One bad start is a missing start, not a missing event."""
    ns = _ns()
    payload = _INTERVAL.replace(b"2024-01-01 10:00,", b"soon,")
    out = _fmt(ns, payload, "case", "activity", "end", "", "", False, "start")
    assert len(out) == 3
    assert int(out["start_timestamp"].isna().sum()) == 1


def test_start_column_that_is_not_a_date_names_the_column():
    ns = _ns()
    with pytest.raises(ValueError, match="start timestamp, `who`"):
        _fmt(ns, _INTERVAL, "case", "activity", "end", "", "", False, "who")


def test_start_equal_to_the_timestamp_is_refused():
    ns = _ns()
    with pytest.raises(ValueError, match="both mapped to `end`"):
        _fmt(ns, _INTERVAL, "case", "activity", "end", "", "", False, "end")


def test_swapped_start_and_completion_are_refused():
    ns = _ns()
    with pytest.raises(ValueError, match="look swapped"):
        _fmt(ns, _INTERVAL, "case", "activity", "start", "", "", False, "end")
    assert "look swapped" in ns["_csv_mapping_problem"](
        _INTERVAL, ("case", "activity", "start", "", "", False, "end"), "fh")


def test_a_few_late_starts_are_kept_and_counted():
    ns = _ns()
    payload = _INTERVAL.replace(b"2024-01-02 09:00,2024-01-02 09:10",
                                b"2024-01-02 09:20,2024-01-02 09:10")
    mapping = ("case", "activity", "end", "", "", False, "start")
    assert ns["_csv_mapping_problem"](payload, mapping, "fh") is None
    assert ns["_csv_late_starts"](payload, mapping, "fh") == (1, 3)
    assert ns["_csv_late_starts"](_INTERVAL, mapping, "fh") == (0, 3)
    assert ns["_csv_late_starts"](
        _INTERVAL, ("case", "activity", "end", "", ""), "fh") == (0, 0)


def test_mapping_tuple_keeps_older_projects_equal():
    """Five names, then an optional tail: nothing changes for a mapping
    without the newer options, so a saved project still compares equal."""
    ns = _ns()
    t = ns["_csv_mapping_tuple"]
    assert t("c", "a", "t", "", "") == ("c", "a", "t", "", "")
    assert t("c", "a", "t", "r", "", True) == ("c", "a", "t", "r", "", True)
    assert t("c", "a", "t", "", "", False, "s") == ("c", "a", "t", "", "", False, "s")
    assert t("c", "a", "t", "", "", True, "s") == ("c", "a", "t", "", "", True, "s")
    for cols, start in ((("c", "a", "t", "", ""), ""),
                        (("c", "a", "t", "", "", True), ""),
                        (["c", "a", "t", "", "", False, "s"], "s"),
                        (None, "")):
        assert ns["_csv_start_col"](cols) == start


def test_csv_is_an_interval_log_exactly_when_a_start_is_mapped():
    ns = _ns()
    src = _APP.read_text(encoding="utf-8")
    code = src[src.index("def _log_is_interval("):]
    code = code[:code.index("\n# -----")]
    exec(compile(code, "<slice>", "exec"), ns)
    is_interval = ns["_log_is_interval"]
    assert is_interval(_INTERVAL, "csv", ("case", "activity", "end", "", "",
                                          False, "start"), "fh") is True
    assert is_interval(_INTERVAL, "csv", ("case", "activity", "end", "", ""),
                       "fh") is False
    assert is_interval(_INTERVAL, "csv", None, "fh") is False
    assert is_interval(_XES, "xes", None, "fh") is False
    assert is_interval(_XES.replace(b"<event>", b'<event><date key='
                                    b'"start_timestamp" value="2024-01-01T00:00:00+00:00"/>'),
                       "xes", None, "fh") is True


def test_seven_element_mapping_flows_through_the_loader():
    ns = _ns()
    src = _APP.read_text(encoding="utf-8")
    code = src[src.index("def _read_log_for_scenarios("):
               src.index("def _load_log_df(")]
    code = code[:code.rindex("@st.cache_data")]
    import tempfile
    ns["tempfile"] = tempfile
    exec(compile(code, "<slice>", "exec"), ns)
    out = ns["_read_log_for_scenarios"](
        _INTERVAL, "csv", ("case", "activity", "end", "", "", False, "start"))
    assert out["start_timestamp"].notna().all()


def test_interval_csv_reaches_the_engines():
    """What the mapping is for: service time from a CSV."""
    from pm4py_ucm.algo.dashboards.contract import build_fact_table
    from pm4py_ucm.util.timing import case_durations
    ns = _ns()
    out = _fmt(ns, _INTERVAL, "case", "activity", "end", "", "", False, "start")
    assert build_fact_table(out).interval_log is True
    single = _fmt(ns, _INTERVAL, "case", "activity", "end", "", "")
    assert build_fact_table(single).interval_log is False
    # c1 runs 09:00 -> 11:00 from its earliest START, 09:30 -> 11:00 without.
    assert case_durations(out).loc["c1"] == 7200.0
    assert case_durations(single).loc["c1"] == 5400.0
