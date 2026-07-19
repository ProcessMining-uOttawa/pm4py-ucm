"""Tests for the project save/share/resume core (``web/sessions``).

The package is Streamlit-free, so these run headless. ``web/`` is not on the
default path (it holds app scripts), so we add it here to import ``sessions``.
"""
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "web"))

from sessions import (  # noqa: E402
    FORMAT,
    SCHEMA_VERSION,
    LogRef,
    ProjectDoc,
    ProjectError,
    dumps,
    load,
    loads,
    save_bundle,
    save_settings,
)
from sessions.bundle import read_bundle, write_bundle  # noqa: E402


def _doc(**over):
    base = dict(
        log=LogRef(source="upload", name="claims.xes", kind="xes",
                   sha256="abc123", csv_columns=None),
        config={"noise_threshold": 0.2, "notation": "ucm",
                "filter_spec": [["exclude_activities", ["Fix Bug"]]]},
        dashboards={"version": 1, "specs": [{"id": "d1"}]},
        app_version="0.7.0", created_utc="2026-07-18T00:00:00Z",
    )
    base.update(over)
    return ProjectDoc(**base)


def test_settings_round_trip_is_stable():
    doc = _doc()
    again = loads(dumps(doc))
    assert again.to_dict() == doc.to_dict()
    # And a second round is a fixed point.
    assert loads(dumps(again)).to_dict() == doc.to_dict()


def test_unknown_keys_are_preserved():
    raw = json.loads(dumps(_doc()))
    raw["future_top_level"] = {"x": 1}
    raw["config"]["future_setting"] = 42
    doc = ProjectDoc.from_dict(raw)
    out = doc.to_dict()
    assert out["future_top_level"] == {"x": 1}       # top-level preserved
    assert out["config"]["future_setting"] == 42     # unknown config preserved


def test_rejects_non_project_and_bad_json():
    with pytest.raises(ProjectError):
        loads(json.dumps({"hello": "world"}))          # no format marker
    with pytest.raises(ProjectError):
        loads("{not json")                              # malformed


def test_format_and_version_are_stamped():
    d = json.loads(dumps(_doc()))
    assert d["format"] == FORMAT
    assert d["schema_version"] == SCHEMA_VERSION


def test_bundle_round_trip():
    doc = _doc(log=LogRef("upload", "claims.xes", "xes", "h", None))
    payload = save_bundle(doc, "claims.xes", b"<xes>data</xes>")
    assert payload[:2] == b"PK"                        # a zip
    got_doc, log = load(payload)
    assert log is not None
    name, data = log
    assert name == "claims.xes"
    assert data == b"<xes>data</xes>"
    assert got_doc.to_dict() == doc.to_dict()


def test_load_autodetects_settings_vs_bundle():
    doc = _doc()
    got, log = load(save_settings(doc))
    assert log is None                                  # settings-only
    assert got.to_dict() == doc.to_dict()


def test_bundle_rejects_non_zip_and_missing_project():
    with pytest.raises(ProjectError):
        read_bundle(b"not a zip")
    # A zip without project.json.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("log/x.xes", b"data")
    with pytest.raises(ProjectError):
        read_bundle(buf.getvalue())


def test_bundle_rejects_zip_slip():
    good = save_bundle(_doc(), "x.xes", b"d")
    # Tamper: add a traversal member under log/.
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(good)) as src, \
            zipfile.ZipFile(buf, "w") as dst:
        for info in src.infolist():
            if info.filename == "log/x.xes":
                dst.writestr("log/../../evil.xes", src.read(info))
            else:
                dst.writestr(info, src.read(info))
    with pytest.raises(ProjectError):
        read_bundle(buf.getvalue())


def test_future_schema_version_still_loads():
    raw = json.loads(dumps(_doc()))
    raw["schema_version"] = SCHEMA_VERSION + 5          # from a newer app
    raw["config"]["brand_new"] = "keep me"
    doc = ProjectDoc.from_dict(raw)                     # must not raise
    assert doc.to_dict()["config"]["brand_new"] == "keep me"
