"""Save / share / resume of PM4Py-UCM sessions (see ``docs/sessions.md``).

Streamlit-free: the whole package operates on plain data and a passed *state
mapping*, so it is unit-testable headless and keeps the published library
clean. The Streamlit app wires ``st.session_state`` and ``file_hash`` into it.

Public surface:

* schema — :class:`ProjectDoc`, :class:`LogRef`, :class:`ProjectError`,
  :data:`SCHEMA_VERSION`, :data:`FORMAT`.
* io — :func:`dumps`, :func:`loads`, :func:`save_settings`,
  :func:`save_bundle`, :func:`load`.
* registry — the persisted-parameter registry and :func:`collect_config` /
  :func:`apply_config` (added in P0's registry step).
"""
from __future__ import annotations

from .io import dumps, load, loads, save_bundle, save_settings
from .registry import REGISTRY, Param, collect, param_ids
from .schema import (
    FORMAT,
    SCHEMA_VERSION,
    LogRef,
    ProjectDoc,
    ProjectError,
)

__all__ = [
    "FORMAT",
    "SCHEMA_VERSION",
    "LogRef",
    "ProjectDoc",
    "ProjectError",
    "REGISTRY",
    "Param",
    "collect",
    "param_ids",
    "dumps",
    "loads",
    "load",
    "save_settings",
    "save_bundle",
]
