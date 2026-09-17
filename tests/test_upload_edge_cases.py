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
