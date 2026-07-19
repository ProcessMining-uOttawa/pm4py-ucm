"""Project *bundle* — a zip carrying ``project.json`` plus the event log, so a
whole analysis travels in one self-contained file (``docs/sessions.md`` §9).

Pure bytes in / bytes out — no Streamlit. Reading is defensive: the zip is
guarded against path traversal (zip-slip) and against decompression bombs via
explicit size caps, and non-project archives are rejected. Project files are
**data, not code** (§12) — nothing here executes anything.
"""
from __future__ import annotations

import io
import posixpath
import zipfile
from typing import Tuple

from .schema import ProjectError

_PROJECT_ENTRY = "project.json"
_LOG_DIR = "log/"

#: Refuse a bundle whose stored log would inflate past this (guards against a
#: decompression bomb). 2 GiB is well above any real event log.
_MAX_LOG_BYTES = 2 * 1024 * 1024 * 1024
#: The project.json itself is tiny; cap it hard.
_MAX_JSON_BYTES = 32 * 1024 * 1024


def write_bundle(project_json: str, log_name: str, log_bytes: bytes) -> bytes:
    """Serialise a bundle to zip bytes: ``project.json`` + ``log/<name>``.

    The log is stored under its (sanitised) basename; the zip's own DEFLATE
    keeps an uncompressed XES small, so no extra gzip step is needed.
    """
    safe = _safe_basename(log_name) or "log.xes"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(_PROJECT_ENTRY, project_json)
        zf.writestr(_LOG_DIR + safe, log_bytes)
    return buf.getvalue()


def read_bundle(zip_bytes: bytes) -> Tuple[str, str, bytes]:
    """Read a bundle → ``(project_json_text, log_name, log_bytes)``.

    Raises :class:`~web.sessions.schema.ProjectError` if the archive is not a
    valid, safe project bundle.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes), "r")
    except zipfile.BadZipFile as exc:
        raise ProjectError(f"Not a valid zip archive: {exc}") from exc
    with zf:
        names = set(zf.namelist())
        if _PROJECT_ENTRY not in names:
            raise ProjectError(
                "Bundle is missing 'project.json' — is this a PM4Py-UCM "
                "project bundle?")
        json_info = zf.getinfo(_PROJECT_ENTRY)
        if json_info.file_size > _MAX_JSON_BYTES:
            raise ProjectError("project.json is implausibly large.")
        project_json = zf.read(_PROJECT_ENTRY).decode("utf-8")

        log_entry = None
        for info in zf.infolist():
            name = info.filename
            if info.is_dir() or not name.startswith(_LOG_DIR):
                continue
            if not _is_safe_member(name):
                raise ProjectError(f"Unsafe path in bundle: {name!r}")
            if info.file_size > _MAX_LOG_BYTES:
                raise ProjectError("Embedded log is implausibly large.")
            log_entry = info
            break
        if log_entry is None:
            raise ProjectError("Bundle contains no log under 'log/'.")
        log_name = posixpath.basename(log_entry.filename)
        return project_json, log_name, zf.read(log_entry)


def _safe_basename(name: str) -> str:
    """The basename, stripped of any directory or traversal component."""
    return posixpath.basename((name or "").replace("\\", "/")).lstrip(".") \
        or posixpath.basename((name or "").replace("\\", "/"))


def _is_safe_member(name: str) -> bool:
    """True if a zip member path stays inside the archive (no zip-slip)."""
    norm = posixpath.normpath(name)
    return not (norm.startswith("/") or norm.startswith("../")
                or "/../" in norm or norm == "..")
