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
* dashboards — :func:`wrap_registry` / :func:`unwrap_registry`, the versioned
  envelope for the browser-island dashboards a project carries (§11); the
  Streamlit-side transport lives in ``web/dashboards_bridge``.
* codegen — :func:`generate_script` / :func:`generate_notebook`, the
  deterministic Python code exporter (see ``docs/code_export.md``).
"""
from __future__ import annotations

from .codegen import generate_notebook, generate_script
from .dashboards import BRIDGE_VERSION, unwrap_registry, wrap_registry
from .io import dumps, load, loads, save_bundle, save_settings
from .registry import REGISTRY, Param, collect, defaults, param_ids
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
    "defaults",
    "param_ids",
    "generate_script",
    "generate_notebook",
    "dumps",
    "loads",
    "load",
    "save_settings",
    "save_bundle",
    "BRIDGE_VERSION",
    "wrap_registry",
    "unwrap_registry",
]
