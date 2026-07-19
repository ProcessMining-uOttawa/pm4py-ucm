"""Schema migrations (see ``docs/sessions.md`` §10).

Each migration is a tiny pure function ``dict -> dict`` that upgrades a
document from version ``n`` to ``n + 1``. ``migrate`` chains them up to
:data:`~web.sessions.schema.SCHEMA_VERSION`. There are none yet (v1 is the
first shipped version); the machinery exists so the *first* breaking change is
a one-line addition rather than a redesign.
"""
from __future__ import annotations

from typing import Any, Callable, Dict

#: ``{from_version: fn}`` — ``fn`` returns the doc at ``from_version + 1`` and
#: must set ``doc["schema_version"]`` accordingly.
_MIGRATIONS: Dict[int, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}


def migrate(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Upgrade ``doc`` in place to the current schema version."""
    from .schema import SCHEMA_VERSION, ProjectError

    v = int(doc.get("schema_version", SCHEMA_VERSION))
    if v > SCHEMA_VERSION:
        # A newer file: we keep going (unknown keys are preserved on load), but
        # flag it so the caller can warn the user their app may be behind.
        doc.setdefault("_newer_schema", v)
        return doc
    while v < SCHEMA_VERSION:
        fn = _MIGRATIONS.get(v)
        if fn is None:
            raise ProjectError(
                f"No migration from schema v{v} to v{v + 1}.")
        doc = fn(doc)
        v = int(doc.get("schema_version", v + 1))
    return doc
