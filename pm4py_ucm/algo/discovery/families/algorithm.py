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


def _prune_cell(tree, cell_df, case_ids, case_id_col: str,
                prune: Dict[str, Any], drop_cases: bool):
    """Prune one cell's tree from that cell's own traversal counts.

    Returns ``(tree, cell_df, case_ids, PruneResult)``; the frame and the
    ids are the cell's own unless ``drop_cases`` removed some. A cell is
    never emptied: if every case walked a removed path, they all stay.
    """
    from ...pruning import prune_infrequent_paths
    from ...traversal import compute_traversal_stats

    # The replay reads the standard case column.
    replay_df = (cell_df if case_id_col == "case:concept:name"
                 else cell_df.rename(columns={case_id_col:
                                              "case:concept:name"}))
    stats = compute_traversal_stats(tree, replay_df)
    result = prune_infrequent_paths(tree, stats, **prune)
    if drop_cases and result.removed:
        gone = set(result.dropped_case_ids(replay_df))
        kept = [c for c in case_ids if str(c) not in gone]
        if kept and len(kept) < len(case_ids):
            cell_df = cell_df[~cell_df[case_id_col].astype(str).isin(gone)]
            case_ids = kept
    return result.tree, cell_df, case_ids, result


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
    progress_callback=None,
    validate: bool = True,
    max_enum_cardinality: Optional[int] = None,
    numeric_coercion_threshold: Optional[float] = None,
    prune: Optional[Dict[str, Any]] = None,
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
    max_enum_cardinality / numeric_coercion_threshold
        Attribute *typing* policy, forwarded to ``partition_log`` when
        given (``None`` keeps its defaults): the most distinct values a
        string attribute may have and still be an enumeration, and the
        share of a string column's values that must parse as numbers for
        it to be typed numeric.
    parameters
        Converter / resource parameters applied per cell (e.g.
        ``resource_attribute``, ``urn_name``). ``map_name`` is ignored:
        each cell's map is named after its value combination. The
        testing hook ``tree_miner`` (a ``callable(cell_df) -> tree``)
        replaces the default pm4py inductive miner.
    progress_callback
        Optional ``callback(stage, done, total)`` fired after each
        mined cell — per-cell mining dominates the family pipeline's
        cost. See :mod:`pm4py_ucm.util.progress`.
    prune
        Remove each cell's infrequent paths
        (:func:`pm4py_ucm.prune_infrequent_paths`), with the frequencies
        of **that cell's own cases**: a dict with any of ``min_cases``,
        ``min_share``, ``prune_loops`` and ``drop_cases``. ``min_cases``
        is therefore an absolute number per cell, and bites harder on a
        small cell. With ``drop_cases`` the cases that walked a removed
        path leave the cell (its ``case_ids`` / ``n_cases``), so every
        statistic and overlay computed from the family describes the
        remaining ones. ``None`` (default), or thresholds of 0, prunes
        nothing.
    validate
        When ``True`` (default), check each cell's model is structurally
        well-formed as it is mined, and raise :class:`ValueError` otherwise.
        The check is a single pass over each cell's nodes and connections,
        run **once, at mine time** — it costs about a thousandth of the
        mining it sits beside, and nothing at all at render time, since the
        grid renderer consumes models that were already checked here.
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
        # Only when given, so partition_log's own defaults stay the one
        # place those numbers are written down.
        **{k: v for k, v in (
            ("max_enum_cardinality", max_enum_cardinality),
            ("numeric_coercion_threshold", numeric_coercion_threshold),
        ) if v is not None},
    )
    if not part.cells:
        raise ValueError(
            "Partitioning produced no cell with at least "
            f"{min_cases} cases (observed combinations: "
            f"{[(tuple(v.label for v in vs), n) for vs, n in part.skipped_cells]}). "
            "Lower min_cases or choose different attributes."
        )

    from ....util.progress import Ticker
    ticker = Ticker(progress_callback, "Mining one model per cell",
                    len(part.cells), report_every=1)

    prune = dict(prune or {})
    drop_cases = bool(prune.pop("drop_cases", False))
    do_prune = bool(prune.get("min_cases") or prune.get("min_share"))

    cells: List[FamilyCell] = []
    pruned_ids: set = set()
    for pcell in part.cells:
        tree = tree_miner(pcell.df)
        cell_df, cell_case_ids = pcell.df, pcell.case_ids
        pruned, pruned_cases = None, 0
        if do_prune:
            tree, cell_df, cell_case_ids, pruned = _prune_cell(
                tree, cell_df, cell_case_ids, case_id_col, prune, drop_cases)
            pruned_cases = len(pcell.case_ids) - len(cell_case_ids)
            if pruned_cases:
                pruned_ids.update(
                    str(c) for c in set(pcell.case_ids) - set(cell_case_ids))
        n_cell_cases = pcell.n_cases - pruned_cases

        conv_params = {
            k: v for k, v in parameters.items() if k not in _RESOURCE_KEYS
        }
        performers, extras = resource_parameters_for(cell_df, parameters)
        if performers is not None:
            conv_params["performers"] = performers
            if extras:
                conv_params["additional_components"] = extras
        conv_params["map_name"] = pcell.label
        if decomposition is not None:
            conv_params["decomposition"] = decomposition
        ucm = _converter.apply(tree, parameters=conv_params)
        if validate:
            # A cell's model is finished the moment it is converted -- unlike
            # the umbrella below, there is no partially-wired state to trip
            # over -- so this is the earliest honest place to check, and the
            # one that names the offending cell while we still know it.
            from ....objects.ucm.validate import check_generated
            check_generated(ucm, f"family cell {pcell.label!r}")

        cells.append(FamilyCell(
            values=pcell.values,
            case_ids=cell_case_ids,
            n_cases=n_cell_cases,
            coverage=n_cell_cases / part.total_cases if part.total_cases else 0.0,
            tree=tree,
            ucm=ucm,
            performers=dict(performers or {}),
            prune=pruned,
            pruned_cases=pruned_cases,
        ))
        ticker.tick()
    ticker.finish()

    if pruned_ids:
        # The family's log is what the assemblies and the statistics read,
        # so the dropped cases leave it too.
        df = df[~df[case_id_col].astype(str).isin(pruned_ids)]

    return ModelFamily(
        attributes=part.attributes,
        cells=cells,
        skipped_cells=part.skipped_cells,
        total_cases=part.total_cases,
        dropped_cases=part.dropped_cases,
        log_df=df,
        cell_parameters=parameters,
        decomposition=decomposition,
        case_id_col=case_id_col,
        pruned_cases=len(pruned_ids),
        pruned_case_ids=sorted(pruned_ids),
    )
