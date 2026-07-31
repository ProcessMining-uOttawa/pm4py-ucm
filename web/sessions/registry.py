"""The Session Parameter Registry — the single source of truth for *what* a
project stores (``docs/sessions.md`` §4–§6).

Streamlit-free and value-oriented: the app gathers the current settings into a
``{id: value}`` mapping and calls :func:`collect`; a later step (Load/resume)
will call :func:`apply` to seed them back. Keeping the *list of ids* here — not
inlined in the app — is what stops persistence rotting as the app grows:
adding a setting is one :data:`REGISTRY` entry, and :func:`collect` refuses a
mapping that doesn't match the registry (an unknown id, or a missing one), so
the app's gather and this list can't silently drift apart.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Tuple


@dataclass(frozen=True)
class Param:
    """One persisted setting."""

    id: str
    category: str          # "miner" | "transform" | "performers" | "overlay"
    #                        | "csv" | "scenarios" | "family" | "compare" | "view"
    default: Any = None


#: The persisted parameters. Order is for humans; ids are the contract.
#: **To add a setting:** add a `Param` here and gather it in the app (a test
#: enforces the two stay in lock-step — see tests/test_sessions_registry.py).
REGISTRY: Tuple[Param, ...] = (
    # Inductive miner + rendering.
    Param("noise_threshold", "miner", 0.2),
    Param("min_support", "miner", 0.0),
    Param("notation", "miner", "bpmn"),         # "ucm" | "bpmn"
    Param("decomposition", "miner", "off"),     # "off" | list of [k, v] pairs
    # Performers.
    Param("resource_attribute", "performers", "org:role"),
    # Performance overlay.
    Param("overlay_nodes", "overlay", []),
    Param("overlay_edges", "overlay", []),
    Param("overlay_heatmap", "overlay", False),
    # Heat-map scale: "local" (per map) | "global" (whole model) | "family"
    # (one shared scale across all Family/Compare cells). Superseded the earlier
    # boolean overlay_heatmap_global; a project written by an older app is
    # migrated on load (see _apply_project_config).
    Param("overlay_heatmap_scope", "overlay", "local"),
    # Pre-mining transforms (rename lives inside the filter spec).
    Param("filter_spec", "transform", []),      # list of [key, value] pairs
    # CSV column mapping (case, activity, timestamp, role, resource).
    Param("csv_columns", "csv", None),
    # Scenario synthesis.
    Param("scenario_strategy", "scenarios", "variant"),
    Param("scenario_group_name", "scenarios", "MinedScenarios"),
    Param("scenario_max_loop_iterations", "scenarios", 2),
    Param("scenario_decision_tree_max_depth", "scenarios", 3),
    # Model families.
    Param("family_attrs", "family", []),
    Param("family_min_cases", "family", 10),
    Param("family_max_values", "family", 8),
    Param("family_bins", "family", 4),
    Param("family_include_values", "family", None),
    Param("family_dedup", "family", False),
    # Compare.
    Param("compare_a", "compare", None),
    Param("compare_b", "compare", None),
    # Which view is open.
    Param("active_view", "view", "Model"),
)

_BY_ID: Dict[str, Param] = {p.id: p for p in REGISTRY}


def param_ids() -> List[str]:
    """All registered ids (stable order)."""
    return [p.id for p in REGISTRY]


def defaults() -> Dict[str, Any]:
    """The registry's default value for every id (stable order).

    Used by consumers (e.g. the Python code exporter) that need a full
    ``{id: value}`` mapping when a project only stores the non-default keys.
    """
    return {p.id: p.default for p in REGISTRY}


def _jsonify(v: Any) -> Any:
    """Normalise a value to JSON-safe form (tuples/sets → lists, recursively)."""
    if isinstance(v, (list, tuple)):
        return [_jsonify(x) for x in v]
    if isinstance(v, set):
        return sorted(_jsonify(x) for x in v)
    if isinstance(v, dict):
        return {str(k): _jsonify(x) for k, x in v.items()}
    return v


def collect(values: Mapping[str, Any]) -> Dict[str, Any]:
    """Serialise a ``{id: value}`` gather into the project ``config`` block.

    Enforces the registry contract so the app can't drift:

    * an id in ``values`` that isn't registered → :class:`KeyError`;
    * a registered id missing from ``values`` → :class:`KeyError`.

    (Pass a registered id's value as its default explicitly if it doesn't
    apply right now — that keeps "what the app knows" and "what we persist"
    provably aligned.)
    """
    ids = set(_BY_ID)
    given = set(values)
    unknown = given - ids
    if unknown:
        raise KeyError(
            f"Not registered as persistable session params: "
            f"{sorted(unknown)}. Add a Param in web/sessions/registry.py "
            f"or drop it from the app's gather.")
    missing = ids - given
    if missing:
        raise KeyError(
            f"App gather is missing registered session params: "
            f"{sorted(missing)}. Gather them, or remove their Param.")
    return {pid: _jsonify(values[pid]) for pid in param_ids()}
