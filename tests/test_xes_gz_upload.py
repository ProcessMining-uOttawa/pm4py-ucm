"""An uploaded ``.xes.gz`` (bare, or inside a zip) must reach pm4py inflated.

The app writes the upload to a temp file called ``log.xes`` and pm4py picks
its reader by that extension, so gzip bytes behind a ``.xes`` name went to
the XML parser and died with a bare ``NoTopLevelLog``. ``_plain_xes_bytes``
detects the wrappers by magic bytes and unwraps them all.
"""
from __future__ import annotations

import gzip
import io
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_APP = _ROOT / "web" / "streamlit_app_v6.py"

_XES = b"""<?xml version="1.0" encoding="UTF-8"?>
<log xes.version="1.0"><trace><string key="concept:name" value="c1"/>
<event><string key="concept:name" value="a"/>
<date key="time:timestamp" value="2026-01-01T00:00:00.000+00:00"/></event>
</trace></log>
"""


def _helper():
    src = _APP.read_text(encoding="utf-8")
    sl = src[src.index("def _extract_xes_from_zip("):
             src.index("def _autopick_column(")]
    ns = {"zipfile": zipfile, "io": io, "Path": Path}
    exec(compile(sl, "<streamlit_app_v6 slice>", "exec"), ns)
    return ns["_plain_xes_bytes"]


def _zip_with(name: str, payload: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, payload)
    return buf.getvalue()


@pytest.mark.parametrize("kind, payload", [
    ("xes", _XES),
    ("xes", gzip.compress(_XES)),                       # bare .xes.gz
    ("zip", _zip_with("log.xes", _XES)),
    ("zip", _zip_with("log.xes.gz", gzip.compress(_XES))),  # gz inside zip
    ("xes", _zip_with("log.xes", _XES)),                # zip by magic only
])
def test_every_wrapper_yields_plain_xml(kind, payload):
    assert _helper()(payload, kind) == _XES


def test_pm4py_reads_the_inflated_bytes(tmp_path):
    pm4py = pytest.importorskip("pm4py")
    plain = _helper()(gzip.compress(_XES), "xes")
    p = tmp_path / "log.xes"
    p.write_bytes(plain)
    log = pm4py.read_xes(str(p))
    assert len(log) == 1
