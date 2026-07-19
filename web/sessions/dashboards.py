"""Versioned envelope for the dashboards payload carried in a project file
(``docs/sessions.md`` §11).

Dashboards are the one setting that does **not** live in ``st.session_state``:
they live in the browser island's ``localStorage``, so they can't ride the
parameter registry like the rest of the config (§5). Instead the app's
*dashboards bridge* reads the island's registry back out and hands it here to
wrap into ``project.dashboards``; on resume the reverse unwraps it to post back
to the island.

Pure data + a version tag — **no Streamlit** — so the shape is testable
headless and the file format can't drift from the app.

The island's registry shape (see ``dash-ui.js`` ``_loadRegistry``) is::

    {"active": "<dashboard id>", "dashboards": [{id, name, specs, filters}, ...]}

and the project stores it wrapped as ``{"version": N, "registry": {...}}`` so a
future breaking change to that shape can be migrated rather than misread.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

#: Bump when the island's registry shape changes in a breaking way (and add a
#: migration in :func:`unwrap_registry`). Independent of the project
#: ``schema_version`` — this versions only the dashboards sub-document.
BRIDGE_VERSION = 1


def _registry_or_none(reg: Any) -> Optional[Dict[str, Any]]:
    """A registry dict with at least one dashboard, else ``None``."""
    if not isinstance(reg, dict):
        return None
    dashboards = reg.get("dashboards")
    if not isinstance(dashboards, list) or not dashboards:
        return None
    return {"active": reg.get("active"), "dashboards": dashboards}


def wrap_registry(
    registry: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """The project ``dashboards`` block for an island registry, or ``None``.

    ``registry`` is what the bridge read from the island's ``localStorage``
    (``{active, dashboards:[...]}``). An empty or missing set → ``None`` so the
    project simply omits dashboards (nothing to restore).
    """
    reg = _registry_or_none(registry)
    if reg is None:
        return None
    return {"version": BRIDGE_VERSION, "registry": reg}


def unwrap_registry(
    payload: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """The island registry to restore from a project ``dashboards`` block.

    Tolerant by design (``docs/sessions.md`` §10): accepts the versioned
    envelope this module writes and, as a fallback, a bare registry dict, so a
    file written by a newer or older app still restores what it can. Returns
    ``None`` when there is nothing usable — resume then leaves the browser's own
    dashboards untouched rather than clearing them.
    """
    if not isinstance(payload, dict):
        return None
    # Envelope (``{"version", "registry"}``) or a bare registry.
    reg = payload.get("registry", payload)
    return _registry_or_none(reg)
