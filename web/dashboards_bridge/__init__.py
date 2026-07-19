"""Streamlit ⇆ dashboard-island bridge (``docs/sessions.md`` §11).

The Dashboards view is an ``st.iframe`` island that keeps its widgets in the
browser's ``localStorage``; ``st.iframe`` is one-way, so the host cannot read
them back to fold into a saved project. This tiny **bidirectional** component
closes that loop:

* it is a *declared* Streamlit component, served from the app's own origin, so
  it shares the island's ``localStorage`` (verified: a same-origin component
  reads the exact keys the srcdoc island writes);
* it speaks the postMessage component protocol by hand from one static HTML
  file, so there is **no build step and no new dependency**.

On **save** it reads the island's registry (``pm4py-ucm:dash:{key}:set``) back
to Python. On **resume** it writes a restored registry into that same key,
idempotently (once per token), so the island picks it up on its next render.

The component renders invisibly (zero height); it is a transport, not UI.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import streamlit.components.v1 as _components

_FRONTEND = Path(__file__).resolve().parent / "frontend"

#: One declared component instance for the app. The name must be unique within
#: the app; ``path`` makes Streamlit serve the static frontend same-origin.
_component = _components.declare_component(
    "ucm_dashboards_bridge", path=str(_FRONTEND))


def sync_dashboards(
    storage_key: str,
    *,
    write: Optional[Dict[str, Any]] = None,
    write_token: str = "",
    nonce: int = 0,
    key: str = "dashboards_bridge",
) -> Optional[Dict[str, Any]]:
    """Render the invisible bridge and exchange dashboard state with the island.

    Parameters
    ----------
    storage_key
        The log's ``file_hash`` — namespaces the island's registry exactly as
        the Dashboards view does (``pm4py-ucm:dash:{storage_key}:set``).
    write
        A registry (``{active, dashboards}``) to write into the island's
        ``localStorage`` on resume, or ``None`` to only read.
    write_token
        Idempotency token: the bridge applies a given ``write`` **once per
        token** (it records applied tokens in ``localStorage``), so re-sending
        the same restore across reruns can't clobber edits the user just made.
        Keep it stable across reruns for one restore.
    nonce
        Bump to force a fresh read — the returned value is otherwise one rerun
        behind (a Streamlit component reports its value on the *next* run).

    Returns
    -------
    dict or None
        ``{"registry": {...} | None, "applied_token": str}`` — the island's
        current registry as read from ``localStorage``, plus the write token it
        last applied (so the host can stop re-sending). ``None`` before the
        component's first round-trip.
    """
    return _component(
        storage_key=str(storage_key or ""),
        write=write,
        write_token=str(write_token or ""),
        nonce=int(nonce),
        key=key,
        default=None,
    )
