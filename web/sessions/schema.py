"""Project-file schema for save/share/resume (see ``docs/sessions.md``).

Pure data + validation — **no Streamlit import** — so it is testable headless
and can be reused by any front-end. A project stores *inputs* (log reference +
config), never derived artifacts; opening it recomputes the outputs.

Compatibility rules (``docs/sessions.md`` §10):

* ``schema_version`` gates migrations; ``app_version`` is informational and is
  never used to decide whether a file loads.
* Unknown top-level keys and unknown ``config`` keys are **preserved** on the
  round-trip, so a file written by a newer app survives an older one.
* Missing keys fall back to the caller's registry defaults (handled outside
  this module, when the config is applied).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: Bump only on a breaking change to the on-disk shape; add a migration.
SCHEMA_VERSION = 1

#: Marker so we can recognise (and refuse) unrelated files.
FORMAT = "pm4py-ucm-project"

#: Top-level keys this version knows about; everything else is carried in
#: :attr:`ProjectDoc.extra` and written back untouched.
_KNOWN_TOP_KEYS = {
    "format", "schema_version", "app_version", "created_utc",
    "log", "config", "dashboards",
}


class ProjectError(ValueError):
    """A project file could not be read (wrong format, corrupt, unsafe)."""


@dataclass
class LogRef:
    """How a project points at its event log."""

    source: str          # "sample" | "upload"
    name: str            # file / sample name, for display and re-matching
    kind: str            # "xes" | "csv" | "zip"
    sha256: str          # matches the app's ``file_hash``; "" if unknown
    #: CSV column mapping (case, activity, timestamp, role, resource), or None.
    csv_columns: Optional[List[str]] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "source": self.source, "name": self.name,
            "kind": self.kind, "sha256": self.sha256,
        }
        if self.csv_columns is not None:
            d["csv_columns"] = list(self.csv_columns)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "LogRef":
        cols = d.get("csv_columns")
        return cls(
            source=str(d.get("source", "upload")),
            name=str(d.get("name", "")),
            kind=str(d.get("kind", "xes")),
            sha256=str(d.get("sha256", "")),
            csv_columns=list(cols) if cols is not None else None,
        )


@dataclass
class ProjectDoc:
    """A whole project: a log reference, the config, and (optionally) the
    dashboards payload. ``extra`` preserves any keys a newer app wrote."""

    log: LogRef
    config: Dict[str, Any] = field(default_factory=dict)
    dashboards: Optional[Dict[str, Any]] = None
    app_version: str = ""
    created_utc: str = ""
    schema_version: int = SCHEMA_VERSION
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "format": FORMAT,
            "schema_version": SCHEMA_VERSION,
            "app_version": self.app_version,
            "created_utc": self.created_utc,
            "log": self.log.to_dict(),
            "config": dict(self.config),
        }
        if self.dashboards is not None:
            d["dashboards"] = self.dashboards
        # Round-trip anything a newer app added that we don't model yet.
        for k, v in self.extra.items():
            d.setdefault(k, v)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ProjectDoc":
        if not isinstance(d, dict):
            raise ProjectError("Project file is not a JSON object.")
        if d.get("format") != FORMAT:
            raise ProjectError(
                f"Not a PM4Py-UCM project file (missing format={FORMAT!r}).")
        from .migrate import migrate  # local import: avoid a cycle
        d = migrate(dict(d))
        log = d.get("log")
        if not isinstance(log, dict):
            raise ProjectError("Project file has no 'log' section.")
        extra = {k: v for k, v in d.items() if k not in _KNOWN_TOP_KEYS}
        return cls(
            log=LogRef.from_dict(log),
            config=dict(d.get("config") or {}),
            dashboards=d.get("dashboards"),
            app_version=str(d.get("app_version", "")),
            created_utc=str(d.get("created_utc", "")),
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
            extra=extra,
        )
