"""Per-cell discovery: partition the log, mine one UCM per cell.

Entry point :func:`discover` — the engine behind
:func:`pm4py_ucm.discover_ucm_family`. Each cell's sub-log goes through
the same pipeline as :func:`pm4py_ucm.discover_ucm_inductive`: inductive
process-tree mining, optional per-cell resource mining, conversion to a
standalone :class:`UCM` (flat or decomposed — the ``decomposition``
parameter is simply applied per cell).

The mined trees are kept on the :class:`FamilyCell` objects because the
assemblers in :mod:`.assembly` re-convert them into a *shared* container
(one ID counter, shared responsibility/component definitions) rather
than grafting the standalone per-cell models together.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..resources import algorithm as _resources
from ....objects.ucm.conversion import from_process_tree as _converter
from .partition import partition_log
from .family import FamilyCell, ModelFamily


def _normalise_log(log):
    """Coerce ``log`` (DataFrame / EventLog / ``.xes`` path) to a
    pandas DataFrame. Only needs pm4py for the non-DataFrame forms."""
    import pandas as pd

    if isinstance(log, pd.DataFrame):
        return log
    try:
        import pm4py
    except ImportError as exc:  # pragma: no cover - depends on env
        raise ImportError(
            "discover_ucm_family requires pm4py to read non-DataFrame "
            "logs; install it with `pip install pm4py`."
        ) from exc
    if isinstance(log, str):
        log = pm4py.read_xes(log)
    if isinstance(log, pd.DataFrame):
        return log
    return pm4py.convert_to_dataframe(log)


def _default_tree_miner(noise_threshold: float):
    def mine(cell_df):
        try:
            import pm4py
        except ImportError as exc:  # pragma: no cover - depends on env
            raise ImportError(
                "discover_ucm_family requires pm4py for process-tree "
                "mining; install it with `pip install pm4py`."
            ) from exc
        return pm4py.discover_process_tree_inductive(
            cell_df, noise_threshold=noise_threshold,
        )
    return mine


def resource_parameters_for(
    log_df,
    parameters: Optional[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, str]], List[str]]:
    """Mine ``(performers, additional_components)`` from ``log_df``,
    mirroring the resource logic of the inductive UCM variant (default
    attribute fallback list, caller overrides win, unbound resources
    surfaced as extra components). Returns ``(None, [])`` when the
    caller disabled resource mining with ``resource_attribute=False``.
    """
    params = dict(parameters or {})
    res_attr = params.get(
        "resource_attribute",
        ("org:resource", "org:role", "org:group"),
    )
    if not res_attr:
        return None, []
    res_params = dict(params.get("resource_parameters", None) or {})
    if isinstance(res_attr, (list, tuple)):
        res_params.setdefault("attribute_priority", list(res_attr))
    elif isinstance(res_attr, str):
        res_params.setdefault("attribute", res_attr)

    performers = _resources.apply(log_df, parameters=res_params)
    performers.update(params.get("performers") or {})

    all_resources = _resources.distinct_components(
        log_df, parameters=res_params,
    )
    already_bound = set(performers.values())
    extras: List[str] = []
    seen: set = set()
    for c in list(params.get("additional_components") or []) + [
        r for r in all_resources if r and r not in already_bound
    ]:
        if c and c not in seen:
            seen.add(c)
            extras.append(c)
    return performers, extras


#: Parameter keys consumed by the resource layer — stripped before the
#: converter sees the parameters.
_RESOURCE_KEYS = (
    "resource_attribute", "resource_parameters",
    "performers", "additional_components",
)


def discover(
    log,
    attributes: Sequence[str],
    *,
    decomposition=None,
    noise_threshold: float = 0.0,
    min_cases: int = 10,
    max_values_per_attribute: int = 12,
    bins: int = 4,
    bin_edges: Optional[Dict[str, Sequence[float]]] = None,
    other_bucket: bool = True,
    unknown_bucket: bool = True,
    include_values: Optional[Dict[str, Sequence[str]]] = None,
    ignore_value_case: bool = True,
    case_id_col: str = "case:concept:name",
    parameters: Optional[Dict[str, Any]] = None,
) -> ModelFamily:
    """Mine a :class:`ModelFamily` from ``log`` partitioned by 1–2
    case-level attributes.

    Parameters
    ----------
    log
        DataFrame, pm4py EventLog, or path to a ``.xes`` file.
    attributes
        One or two case-attribute names (source column names or their
        sanitised jUCMNav forms).
    decomposition
        Applied to every cell — same shape as in
        :func:`pm4py_ucm.discover_ucm_inductive`.
    noise_threshold
        Forwarded to the inductive miner per cell.
    min_cases / max_values_per_attribute / bins / bin_edges /
    other_bucket / unknown_bucket / include_values / ignore_value_case
        Partitioning policy — see
        :func:`pm4py_ucm.algo.discovery.families.partition.partition_log`.
        ``include_values`` restricts each attribute to the listed
        values (``{attribute: [labels]}``); other cases are dropped.
        ``ignore_value_case`` (default ``True``) merges enumeration
        values differing only in letter case (``F`` / ``f``).
    parameters
        Converter / resource parameters applied per cell (e.g.
        ``resource_attribute``, ``urn_name``). ``map_name`` is ignored:
        each cell's map is named after its value combination. The
        testing hook ``tree_miner`` (a ``callable(cell_df) -> tree``)
        replaces the default pm4py inductive miner.
    """
    parameters = dict(parameters or {})
    tree_miner = parameters.pop("tree_miner", None)
    if tree_miner is None:
        tree_miner = _default_tree_miner(noise_threshold)

    df = _normalise_log(log)
    part = partition_log(
        df, attributes,
        case_id_col=case_id_col,
        min_cases=min_cases,
        max_values_per_attribute=max_values_per_attribute,
        bins=bins,
        bin_edges=bin_edges,
        other_bucket=other_bucket,
        unknown_bucket=unknown_bucket,
        include_values=include_values,
        ignore_value_case=ignore_value_case,
    )
    if not part.cells:
        raise ValueError(
            "Partitioning produced no cell with at least "
            f"{min_cases} cases (observed combinations: "
            f"{[(tuple(v.label for v in vs), n) for vs, n in part.skipped_cells]}). "
            "Lower min_cases or choose different attributes."
        )

    cells: List[FamilyCell] = []
    for pcell in part.cells:
        tree = tree_miner(pcell.df)

        conv_params = {
            k: v for k, v in parameters.items() if k not in _RESOURCE_KEYS
        }
        performers, extras = resource_parameters_for(pcell.df, parameters)
        if performers is not None:
            conv_params["performers"] = performers
            if extras:
                conv_params["additional_components"] = extras
        conv_params["map_name"] = pcell.label
        if decomposition is not None:
            conv_params["decomposition"] = decomposition
        ucm = _converter.apply(tree, parameters=conv_params)

        cells.append(FamilyCell(
            values=pcell.values,
            case_ids=pcell.case_ids,
            n_cases=pcell.n_cases,
            coverage=pcell.n_cases / part.total_cases if part.total_cases else 0.0,
            tree=tree,
            ucm=ucm,
            performers=dict(performers or {}),
        ))

    return ModelFamily(
        attributes=part.attributes,
        cells=cells,
        skipped_cells=part.skipped_cells,
        total_cases=part.total_cases,
        dropped_cases=part.dropped_cases,
        log_df=df,
        cell_parameters=parameters,
        decomposition=decomposition,
    )
