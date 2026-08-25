"""``pm4py-ucm`` — Use Case Map (URN/UCM) extension for PM4Py.

This package adds first-class support for the Use Case Map modelling
notation — part of the ITU-T Z.151 *User Requirements Notation* (URN)
standard and supported by the open-source jUCMNav tool — to PM4Py-style
process-mining workflows.

The high-level API mirrors PM4Py's BPMN helpers; see :mod:`pm4py_ucm.api`
for the details. The most common entry points are re-exported here::

    import pm4py
    import pm4py_ucm

    log = pm4py.read_xes("log.xes")
    ucm = pm4py_ucm.discover_ucm_inductive(log)
    pm4py_ucm.write_ucm(ucm, "log.jucm")
"""

from .api import (
    read_ucm,
    write_ucm,
    discover_ucm_inductive,
    discover_resources,
    discover_components,
    bind_performers,
    convert_to_ucm,
    view_ucm,
    save_vis_ucm,
    discover_scenarios,
    write_variants_report,
    write_case_variant_map,
    write_condition_mining_report,
    discover_ucm_family,
    rank_partition_attributes,
    write_ucm_family,
    assemble_ucm_family,
    save_vis_ucm_family,
    view_ucm_family,
    compute_family_stats,
    write_family_report,
    annotate_performance,
    compute_traversal_stats,
    suggest_decomposition,
    suggest_loop_iterations,
)

# Scenario simulation. Note the neighbouring ``compute_traversal_stats``
# above: despite the shared word, the two are unrelated. That one replays
# the *event log* over a model to produce the performance overlay's
# traversal counts; these run a model's *scenarios* the way jUCMNav does,
# and know nothing about a log.
from .algo.scenario_traversal import (  # noqa: E402
    ScenarioTraversalResult,
    TraversalProblem,
    NoScenariosError,
    traverse_scenario,
    traverse_all,
    check_traversal,
    required_max_hit_count,
)
from .algo.scenario_coverage import (  # noqa: E402
    Coverage,
    Comparison,
    coverage,
    compare,
)
from .objects.ucm.obj import UCM

__all__ = [
    "UCM",
    "read_ucm",
    "write_ucm",
    "discover_ucm_inductive",
    "discover_resources",
    "discover_components",
    "bind_performers",
    "convert_to_ucm",
    "view_ucm",
    "save_vis_ucm",
    "discover_scenarios",
    "write_variants_report",
    "write_case_variant_map",
    "write_condition_mining_report",
    "discover_ucm_family",
    "write_ucm_family",
    "assemble_ucm_family",
    "save_vis_ucm_family",
    "view_ucm_family",
    "compute_family_stats",
    "write_family_report",
    "annotate_performance",
    "compute_traversal_stats",
    # Scenario simulation — unrelated to compute_traversal_stats above.
    "ScenarioTraversalResult",
    "TraversalProblem",
    "NoScenariosError",
    "traverse_scenario",
    "traverse_all",
    "check_traversal",
    "required_max_hit_count",
    "Coverage",
    "Comparison",
    "coverage",
    "compare",
    "suggest_decomposition",
    "suggest_loop_iterations",
    "rank_partition_attributes",
]

__version__ = "0.7.12"
