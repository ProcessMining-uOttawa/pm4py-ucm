"""Serialise / parse project files — the settings JSON and the bundle zip
(``docs/sessions.md`` §9). No Streamlit; pure bytes/text.
"""
from __future__ import annotations

import json
from typing import Optional, Tuple

from .bundle import read_bundle, write_bundle
from .schema import ProjectDoc, ProjectError


def dumps(doc: ProjectDoc) -> str:
    """The project document as pretty JSON text."""
    return json.dumps(doc.to_dict(), indent=2, ensure_ascii=False)


def loads(text: str) -> ProjectDoc:
    """Parse settings JSON text into a :class:`ProjectDoc`."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProjectError(f"Project file is not valid JSON: {exc}") from exc
    return ProjectDoc.from_dict(raw)


def save_settings(doc: ProjectDoc) -> bytes:
    """A settings-only project file (no log) as UTF-8 bytes."""
    return dumps(doc).encode("utf-8")


def save_bundle(doc: ProjectDoc, log_name: str, log_bytes: bytes) -> bytes:
    """A self-contained project bundle (settings + log) as zip bytes."""
    return write_bundle(dumps(doc), log_name, log_bytes)


def load(data: bytes) -> Tuple[ProjectDoc, Optional[Tuple[str, bytes]]]:
    """Load a project from raw file bytes, auto-detecting settings vs bundle.

    Returns ``(doc, log)`` where ``log`` is ``(name, bytes)`` for a bundle or
    ``None`` for a settings-only file (the caller then re-supplies the log).
    """
    if data[:2] == b"PK":  # zip magic → a bundle
        project_json, log_name, log_bytes = read_bundle(data)
        return loads(project_json), (log_name, log_bytes)
    # Otherwise a settings JSON.
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectError(
            "File is neither a project bundle (zip) nor UTF-8 JSON.") from exc
    return loads(text), None
