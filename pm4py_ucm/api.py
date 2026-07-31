"""High-level API for the ``pm4py-ucm`` package.

These functions deliberately mirror the look-and-feel of the helpers in the
top-level ``pm4py`` namespace (``pm4py.read_bpmn``, ``pm4py.write_bpmn``,
``pm4py.discover_bpmn_inductive``, ``pm4py.view_bpmn`` …), so that adopting
Use Case Maps as an additional output of process mining requires only a
one-word change in user code::

    import pm4py
    import pm4py_ucm

    log = pm4py.read_xes("running-example.xes")
    ucm = pm4py_ucm.discover_ucm_inductive(log)
    pm4py_ucm.view_ucm(ucm)
    pm4py_ucm.write_ucm(ucm, "running-example.jucm")
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Union

from .objects.ucm.obj import UCM
from .objects.ucm.exporter.variants import jucm as _jucm_exporter
from .objects.ucm.importer.variants import jucm as _jucm_importer
from .objects.ucm.conversion import from_process_tree as _tree_converter
from .objects.ucm.conversion.decomposition import (  # noqa: F401
    suggest_decomposition,
)
from .visualization.ucm import visualizer as _visualizer
from .algo.discovery.ucm import algorithm as _discovery
from .algo.discovery.resources import algorithm as _resources
from .algo.discovery.variants import clustering as _clustering
from .algo.discovery.scenarios import synthesis as _scenarios
from .algo.discovery.scenarios import reports as _scenario_reports
from .algo.discovery import families as _families
from .algo import performance as _performance


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def read_ucm(file_path: str, parameters: Optional[Dict[str, Any]] = None) -> UCM:
    """Read a Use Case Map from a jUCMNav ``.jucm`` (XMI) file."""
    return _jucm_importer.apply(file_path, parameters=parameters)


def write_ucm(
    ucm: UCM,
    file_path: str,
    parameters: Optional[Dict[str, Any]] = None,
) -> str:
    """Write a Use Case Map as a jUCMNav-compatible ``.jucm`` (XMI) file."""
    _jucm_exporter.apply(ucm, file_path, parameters=parameters)
    return file_path


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_ucm_inductive(
    log,
    parameters: Optional[Dict[str, Any]] = None,
    decomposition=None,
) -> UCM:
    """Discover a UCM from an event log using the inductive miner.

    Pass ``resource_attribute="org:resource"`` (or another attribute name,
    or a fallback list) in ``parameters`` to also mine activity→performer
    bindings and surface them in the resulting UCM.

    The ``decomposition`` argument, if set, splits the result into a
    root map plus plug-in (sub-)maps connected by Stubs. Accepts
    ``"off"`` / ``None`` (single map — current behaviour), ``"auto"``,
    ``"aggressive"``, or a dict — see
    :mod:`pm4py_ucm.objects.ucm.conversion.decomposition` for the
    full parameter shape."""
    params = dict(parameters or {})
    if decomposition is not None:
        params["decomposition"] = decomposition
    return _discovery.apply(
        log,
        variant=_discovery.Variants.INDUCTIVE,
        parameters=params,
    )


def discover_resources(
    log,
    attribute: str = "org:resource",
    strategy: str = "mode",
    min_support: float = 0.0,
    attribute_priority: Optional[list] = None,
    parameters: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Mine the ``{activity: performer}`` mapping from an event log.

    Returns a plain ``dict`` ready to feed into :meth:`UCM.bind_performers`
    or to the converter via ``parameters={"performers": …}``.

    Parameters mirror the underlying
    :mod:`pm4py_ucm.algo.discovery.resources.variants.activity_attribute`
    module — see its docstring for full details. The default
    ``min_support=0.0`` picks the modal performer for every activity that
    has *any* resource information, even when the resource pool is
    highly dispersed; raise it (e.g. ``0.5``) to require a majority
    before binding."""
    params = dict(parameters or {})
    params.setdefault("attribute", attribute)
    params.setdefault("strategy", strategy)
    params.setdefault("min_support", min_support)
    if attribute_priority is not None:
        params["attribute_priority"] = attribute_priority
    return _resources.apply(log, parameters=params)


def discover_components(
    log,
    attribute: str = "org:resource",
    attribute_priority: Optional[list] = None,
    parameters: Optional[Dict[str, Any]] = None,
) -> list:
    """Return the sorted list of every distinct performer value that
    appears anywhere in ``log``.

    Complements :func:`discover_resources`. ``discover_resources``
    returns one performer per activity (picked by mode / first /
    whatever strategy); ``discover_components`` returns the *vocabulary*
    of every actor seen in the log. Use this when you want every actor
    that ever participated to become a URN
    :class:`UCM.ComponentElement`, regardless of whether any specific
    responsibility happens to be cleanly bound to it.

    The high-level :func:`discover_ucm_inductive` calls this
    automatically when ``resource_attribute`` is set, so most users do
    not need to call it directly."""
    params = dict(parameters or {})
    params.setdefault("attribute", attribute)
    if attribute_priority is not None:
        params["attribute_priority"] = attribute_priority
    return _resources.distinct_components(log, parameters=params)


def bind_performers(ucm: UCM, performers: Dict[str, Any], **kwargs) -> UCM:
    """Attach a ``{activity: performer}`` mapping to an existing UCM.

    Creates one :class:`UCM.ComponentElement` per unique performer name,
    sets :attr:`UCM.Responsibility.performer` on every matching
    responsibility, and adds one :class:`UCM.ComponentRef` per used
    component to each map — binding the visual layer (``cont_ref`` on
    every RespRef) to the semantic one.

    Forwarded keyword arguments (``kind=``, ``kind_for=``) follow
    :meth:`UCM.bind_performers`.

    Returns the same ``ucm`` instance for chaining."""
    ucm.bind_performers(performers, **kwargs)
    return ucm


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def convert_to_ucm(
    obj,
    parameters: Optional[Dict[str, Any]] = None,
    decomposition=None,
) -> UCM:
    """Convert a supported object (currently process trees) to a UCM.

    The ``decomposition`` argument has the same meaning as in
    :func:`discover_ucm_inductive` — see that function for details.
    """
    if isinstance(obj, UCM):
        return obj
    params = dict(parameters or {})
    if decomposition is not None:
        params["decomposition"] = decomposition
    # Duck-type a process tree: it has ``operator`` / ``children`` / ``label``.
    if all(hasattr(obj, a) for a in ("operator", "children", "label")):
        return _tree_converter.apply(obj, parameters=params)
    raise TypeError(
        f"convert_to_ucm: don't know how to convert {type(obj).__name__} "
        "to a Use Case Map."
    )


# ---------------------------------------------------------------------------
# Visualisation
# ---------------------------------------------------------------------------

def view_ucm(
    ucm: UCM,
    style: str = "ucm",
    parameters: Optional[Dict[str, Any]] = None,
    map: Optional[str] = None,
) -> None:
    """Render and display the UCM in the system viewer.

    Parameters
    ----------
    ucm
        The UCM model to render.
    style
        Visual notation: ``"ucm"`` for the Z.151 / jUCMNav look (filled
        circle start, perpendicular-bar end, ``✕`` responsibility refs,
        AND-fork/join as synchronisation bars, OR-fork/join as small
        dots, diamonds reserved for stubs); or ``"bpmn"`` for a
        BPMN-friendly look (activity boxes, gateway diamonds, BPMN
        start/end events).
    parameters
        Forwarded to the underlying visualizer; see
        :func:`pm4py_ucm.visualization.ucm.variants.classic.apply`.
    map
        When set, render only the map with this name (one panel — the
        existing single-map behaviour). When ``None`` (default) and the
        UCM contains more than one map (e.g. produced by hierarchical
        decomposition), all maps are vertically stacked in a single
        image with title strips and separators.
    """
    params = dict(parameters or {})
    params.setdefault("style", style)
    if len(ucm.maps) > 1 and map is None:
        from .visualization.ucm import stacked as _stacked
        import tempfile, os
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
        _stacked.render(ucm, tmp, parameters=params)
        try:
            os.startfile(tmp)  # Windows
        except (AttributeError, OSError):
            # Non-Windows or no handler — fall back to the gviz viewer
            # of the first map so the call doesn't silently no-op.
            pass
        return tmp
    if map is not None:
        params["map_name"] = map
    gviz = _visualizer.apply(ucm, parameters=params)
    return _visualizer.view(gviz)


# ---------------------------------------------------------------------------
# Scenario synthesis from event logs
# ---------------------------------------------------------------------------

def discover_scenarios(
    log,
    parameters: Optional[Dict[str, Any]] = None,
    decomposition=None,
    coarsen_loops: bool = True,
    emit_conditions: bool = True,
    group_name: str = "MinedScenarios",
    max_loop_iterations: Optional[int] = 2,
    condition_strategy: str = "variant",
    decision_tree_max_depth: int = 3,
    progress_callback=None,
):
    """End-to-end discovery of an executable UCM with scenarios from a log.

    Pipeline:

    1. Discover a process tree using the inductive miner (or reuse a
       pre-built tree passed via ``parameters["process_tree"]``).
    2. Convert it to a :class:`UCM` (decomposition is *not* applied for
       scenario synthesis — the cluster engine wants a single map; if
       the caller requests decomposition it is honoured but condition
       emission may be skipped for plug-in maps).
    3. Cluster the log on the tree via concurrency-aware choice
       signatures; see
       :func:`pm4py_ucm.algo.discovery.variants.clustering.cluster`.
    4. Build one :class:`UCM.ScenarioDef` per variant on the UCM, set
       ``variant_id`` initialisations, attach descriptions carrying
       partial-order expression + case IDs, and (by default) emit
       ``variant_id`` disjunctive conditions on the OR-fork outgoing
       connections that correspond to XOR tree nodes outside loops.

    Parameters
    ----------
    log
        Any input accepted by
        :func:`pm4py.discover_process_tree_inductive` — a path to a
        ``.xes`` file, a ``pandas.DataFrame``, or a pm4py
        ``EventLog``. The same log is used for tree discovery and
        cluster replay.
    parameters
        Forwarded to :func:`discover_ucm_inductive` for the UCM build
        phase (e.g. ``resource_attribute``, ``performers``,
        ``map_name``). Also accepts ``process_tree`` to bypass mining.
    decomposition
        Same shape as in :func:`discover_ucm_inductive`. Decomposed
        multi-map UCMs are fully supported: the synthesizer walks
        every map for OR-fork condition emission and loop-counter
        wiring, so XORs that land in plug-in maps receive the same
        ``variant_id == V`` (or data-driven) conditions they would
        in the flat case, and loops pushed into plug-ins get their
        LoopEntryGuard and decrement responsibility inserted into
        the plug-in map.
    coarsen_loops
        When ``True`` (default), variant clustering collapses loop
        iteration sequences to ``{0, 1, >=2}``. Pass ``False`` to
        differentiate every iteration's inner choices.
    emit_conditions
        When ``True`` (default), set ``variant_id`` disjunctive
        conditions on the OR-fork outgoing connections corresponding
        to non-loop XORs. Pass ``False`` to leave conditions alone.
    group_name
        Name of the synthesized :class:`UCM.ScenarioGroup`.
    max_loop_iterations
        Upper bound on the per-variant loop counter initialisation
        value (default ``2``). Scenarios traverse each loop body at
        most this many times — enough to demonstrate "loop fires
        once vs more than once" behaviour without producing
        iteration chains that take forever to step through in
        jUCMNav. Pass ``None`` to disable capping. Nested loops
        compose multiplicatively, so a default of 2 keeps even
        deep nesting tractable. See
        :func:`pm4py_ucm.algo.discovery.scenarios.synthesis.\
synthesize_scenarios` for details.

    Returns
    -------
    tuple
        ``(ucm, clustering)`` — the UCM (mutated with the scenarios)
        and the
        :class:`pm4py_ucm.algo.discovery.variants.clustering.ClusteringResult`
        with per-variant case IDs and the fitness percentage.
    """
    try:
        import pm4py
    except ImportError as exc:  # pragma: no cover - depends on env
        raise ImportError(
            "discover_scenarios requires pm4py to be installed; "
            "install it with `pip install pm4py`."
        ) from exc

    params = dict(parameters or {})
    if isinstance(log, str):
        log = pm4py.read_xes(log)

    # 1+2. Mine tree (or reuse), then build UCM. We force tree
    # discovery up here so the SAME tree object reaches both the UCM
    # builder and the clustering pass. The inductive miner is
    # deterministic on a given log, but going via PM4Py twice and
    # comparing identity would be brittle, so we pin the tree.
    tree = params.pop("process_tree", None)
    if tree is None:
        tree = pm4py.discover_process_tree_inductive(log)

    ucm = discover_ucm_inductive(
        log,
        parameters={**params, "process_tree": tree},
        decomposition=decomposition,
    )

    # 3. Cluster the log on the tree.
    clustering = _clustering.cluster(
        log, tree, coarsen_loops=coarsen_loops,
        progress_callback=progress_callback,
    )

    # 4. Synthesize scenarios on the UCM.
    _scenarios.synthesize_scenarios(
        ucm, tree, clustering,
        group_name=group_name,
        emit_conditions=emit_conditions,
        max_loop_iterations=max_loop_iterations,
        condition_strategy=condition_strategy,
        log=log if condition_strategy == "data-driven" else None,
        decision_tree_max_depth=decision_tree_max_depth,
    )

    return ucm, clustering


def write_variants_report(
    clustering,
    file_path: str,
    max_case_ids: int = 5,
) -> str:
    """Write the variant summary CSV — one row per variant plus a
    noise row and a totals row. See
    :func:`pm4py_ucm.algo.discovery.scenarios.reports.write_variants_report`
    for column semantics."""
    _scenario_reports.write_variants_report(
        clustering, file_path, max_case_ids=max_case_ids,
    )
    return file_path


def write_case_variant_map(
    clustering,
    file_path: str,
) -> str:
    """Write the per-case → variant mapping as CSV — joinable on
    ``case_id`` against the user's own log."""
    _scenario_reports.write_case_variant_map(clustering, file_path)
    return file_path


def write_condition_mining_report(
    scenario_group,
    file_path: str,
) -> str:
    """Write the per-OR-fork decision-mining report as CSV.

    Only meaningful when the UCM was produced via
    ``discover_scenarios(..., condition_strategy="data-driven")``:
    the synthesizer stashes per-fork
    :class:`pm4py_ucm.algo.discovery.scenarios.decision_mining.\
OrForkMiningResult` records on the
    :class:`UCM.ScenarioGroup` that this report writer reads back.
    See :func:`pm4py_ucm.algo.discovery.scenarios.reports.\
write_condition_mining_report` for the column semantics."""
    _scenario_reports.write_condition_mining_report(
        scenario_group, file_path,
    )
    return file_path


# ---------------------------------------------------------------------------
# Performance overlays
# ---------------------------------------------------------------------------

def annotate_performance(
    ucm: UCM,
    log,
    node_metrics=("frequency",),
    edge_metrics=("frequency",),
    parameters: Optional[Dict[str, Any]] = None,
    traversal=None,
    tree=None,
) -> UCM:
    """Overlay performance information from ``log`` on ``ucm``.

    Activity metrics (up to two keeps the diagram readable):
    ``frequency`` (executions), ``case_coverage`` (cases containing
    the activity), ``sojourn_mean_time`` / ``sojourn_median_time`` /
    ``sojourn_total_time`` (time since the case's previous event —
    works on any timestamped log), and — for interval logs carrying a
    ``start_timestamp`` column — ``mean_time`` / ``median_time`` /
    ``total_time`` service times. Edge metrics: directly-follows
    ``frequency``, ``percentage`` (an OR-fork branch's share of the
    fork's traversals), and ``mean_time`` / ``median_time`` /
    ``total_time`` waiting times between the edge's two activities.

    Passing ``traversal`` (from :func:`compute_traversal_stats`) together
    with the ``tree`` the model was discovered from adds the
    ``traversal_frequency`` / ``traversal_percentage`` metrics, which
    count how often the log **walks the model** instead of counting
    events and directly-follows pairs. Prefer them on any model with
    concurrency or silent skips: they conserve across the diagram, and
    they are the only ones that can measure a branch that skips
    silently.

    The overlay is stored as ``_perf`` metadata: rendered by the
    visualizer as a small gray annotation under activity names and on
    edges, and exported to ``.jucm`` as node ``<metadata>`` entries
    (jUCMNav shows them in the properties view). Re-annotating
    replaces the previous overlay; pass empty metric sequences to
    remove a layer. Returns the same ``ucm``. See
    :mod:`pm4py_ucm.algo.performance` for details, including how edge
    statistics are attributed to activity-to-activity segments."""
    import pandas as pd
    if not isinstance(log, pd.DataFrame):
        import pm4py
        log = pm4py.convert_to_dataframe(log)
    return _performance.annotate_performance(
        ucm, log,
        node_metrics=node_metrics,
        edge_metrics=edge_metrics,
        parameters=parameters,
        traversal=traversal,
        tree=tree,
    )


def compute_traversal_stats(tree, log, **kwargs):
    """Count how often ``log`` walks each part of the model built from
    ``tree`` — the input to the ``traversal_*`` overlay metrics.

    Replays the log on the process tree, so the counts conserve across
    the model (an activity's own count equals the count on its incoming
    and outgoing edges) and a silently skipped branch is measurable.
    Pass the result, with the same ``tree``, to
    :func:`annotate_performance`.

    By default non-fitting cases are aligned to their nearest model path
    so the counts cover the whole log; pass ``repair=False`` to count
    only cases that fit exactly. Either way the returned
    :class:`~pm4py_ucm.algo.traversal.TraversalStats` reports coverage —
    ``fitting_ratio`` is what to show a reader, since a model mined with
    a noise threshold explains only part of its log. See
    :mod:`pm4py_ucm.algo.traversal`.
    """
    from .algo.traversal import compute_traversal_stats as _cts
    return _cts(tree, log, **kwargs)


# ---------------------------------------------------------------------------
# Model families (attribute-partitioned discovery)
# ---------------------------------------------------------------------------

def rank_partition_attributes(log, **kwargs):
    """Rank a log's case attributes by **discriminative power** — how much the
    process changes across each attribute's values — to advise *which* attribute
    to build a :func:`discover_ucm_family` on.

    Deterministic (no LLM). Returns a list of
    :class:`~pm4py_ucm.algo.discovery.families.advisor.AttributeScore`, best
    first. See that module for the scoring (control-flow divergence + duration
    effect size + cardinality/coverage sanity). ``log`` is a pandas DataFrame;
    ``kwargs`` forward the ``*_col`` names.
    """
    try:
        import pm4py
    except ImportError:  # pragma: no cover
        pm4py = None
    if pm4py is not None and not hasattr(log, "columns"):
        log = pm4py.convert_to_dataframe(log)
    return _families.rank_partition_attributes(log, **kwargs)


def discover_ucm_family(
    log,
    attributes,
    decomposition=None,
    noise_threshold: float = 0.0,
    min_cases: int = 10,
    max_values_per_attribute: int = 12,
    bins: int = 4,
    bin_edges: Optional[Dict[str, Any]] = None,
    other_bucket: bool = True,
    unknown_bucket: bool = True,
    include_values: Optional[Dict[str, Any]] = None,
    ignore_value_case: bool = True,
    case_id_col: str = "case:concept:name",
    parameters: Optional[Dict[str, Any]] = None,
    progress_callback=None,
):
    """Mine a *family* of UCM models: partition ``log`` by the values
    of 1–2 case-level attributes (e.g. cancer type × age group) and
    discover one model per partition cell.

    Each cell's sub-log runs through the same pipeline as
    :func:`discover_ucm_inductive` — the ``decomposition`` argument is
    simply applied per cell, so the family can be flat or decomposed.
    Enumeration attributes partition by value **case-insensitively**
    by default — ``F`` and ``f`` are one value, displayed as the
    log's most frequent spelling (disable with
    ``ignore_value_case=False`` for genuinely case-significant
    codes); low-count values merge into ``Other`` past
    ``max_values_per_attribute``. Boolean attributes partition by
    ``true``/``false`` (any letter case); numeric attributes are
    binned into ranges (``bins`` quantiles, or explicit
    ``bin_edges={attribute: [edges]}``); missing values go to an
    ``Unknown`` bucket. ``include_values={attribute: [labels]}``
    restricts an attribute to the listed values — cases carrying other
    values are dropped from the family entirely. Observed combinations
    with fewer than ``min_cases`` cases are skipped (recorded on the
    family, shown grayed in the grid rendering).

    Returns a
    :class:`~pm4py_ucm.algo.discovery.families.family.ModelFamily`;
    feed it to :func:`write_ucm_family`, :func:`save_vis_ucm_family`,
    or :func:`assemble_ucm_family`.
    """
    return _families.discover(
        log, attributes,
        decomposition=decomposition,
        noise_threshold=noise_threshold,
        min_cases=min_cases,
        max_values_per_attribute=max_values_per_attribute,
        bins=bins,
        bin_edges=bin_edges,
        other_bucket=other_bucket,
        unknown_bucket=unknown_bucket,
        include_values=include_values,
        ignore_value_case=ignore_value_case,
        case_id_col=case_id_col,
        parameters=parameters,
        progress_callback=progress_callback,
    )


def write_ucm_family(family, path: str) -> str:
    """Write one ``.jucm`` file per family cell plus a
    ``family_summary.csv`` (cell labels, case counts, coverage, file
    names). ``path`` ending in ``.zip`` produces a single archive;
    otherwise it is treated as a directory."""
    return _families.write_family(family, path)


def assemble_ucm_family(
    family,
    mode: str = "umbrella",
    **kwargs,
) -> UCM:
    """Assemble a mined family into a single :class:`UCM`.

    ``mode="combined"`` puts every cell model in one URN spec as
    independent root maps (shared responsibility/component
    definitions, one ID counter — the same activity is one definition
    referenced from many maps).

    ``mode="umbrella"`` builds one overarching model: the root map is
    the **shared skeleton** of the cell processes, with a **dynamic
    stub at every point where behaviour diverges**. Each stub's
    plug-ins are the distinct variant sub-maps, guarded by
    preconditions over the partition attributes
    (``cancer_type == Breast && age_group == _40_59``); cells that
    skip a variation point get a pass-through ``skip`` plug-in.
    Behaviourally identical variants share a single plug-in with a
    domain-factored OR'd precondition, and one scenario strategy per
    cell initialises the attribute variables so jUCMNav's traversal
    selects the matching plug-in at every stub. **Resource variation
    counts as variation**: the same activity performed by different
    actors in different cells becomes a variation point too, each
    variant binding the activity to its cells' actor (disable with
    ``resource_variation=False``). Pass ``skeleton=False`` for the
    plain single-stub umbrella (whole cell models as plug-ins).
    By default the strategies are **path scenarios**: each cell's
    sub-log is replayed to emit one executable scenario per
    (combination × behavioural variant), with ``family_variant``
    branch conditions on outside-loop OR-forks and loop-counter
    scaffolding — so different strategies traverse the different
    paths of each combination (``path_scenarios=False`` restores the
    plain one-strategy-per-combination form). Keyword arguments are
    forwarded to
    :func:`~pm4py_ucm.algo.discovery.families.assembly.assemble_umbrella`
    (``root_map_name``, ``stub_name``, ``dedup``, ``strategies``,
    ``group_name``, ``skeleton``, ``resource_variation``,
    ``path_scenarios``, ``max_variants_per_cell``,
    ``max_loop_iterations``) or
    :func:`~pm4py_ucm.algo.discovery.families.assembly.assemble_combined`
    (``urn_name``)."""
    if mode == "combined":
        return _families.assemble_combined(family, **kwargs)
    if mode == "umbrella":
        return _families.assemble_umbrella(family, **kwargs)
    raise ValueError(
        f"Unknown assembly mode {mode!r}; expected 'combined' or 'umbrella'."
    )


def save_vis_ucm_family(
    family,
    file_path: str,
    style: str = "ucm",
    parameters: Optional[Dict[str, Any]] = None,
) -> str:
    """Render the family as a single grid PNG — a vertical stack for
    one partition attribute, a matrix (rows × columns) for two. Every
    panel carries the cell's ``n=… (…%)`` caption; skipped
    combinations render as grayed placeholders.

    Resolution adapts to the family size: rendering aims for
    ``parameters["target_dpi"]`` (default 192 — twice graphviz's
    default, so text stays readable in the export) and backs off
    toward 96 dpi only when the projected composite would exceed
    ``parameters["max_total_pixels"]`` (default 150M). Pass
    ``parameters={"dpi": …}`` to pin an exact resolution instead.

    An **``.svg``** target (by extension or ``parameters["format"]``)
    writes the same rows × columns matrix as a single vector document
    instead — crisp at any zoom, its text selectable, and needing none of
    the DPI machinery. See
    :func:`pm4py_ucm.visualization.ucm.family_grid.render`."""
    from .visualization.ucm import family_grid as _grid
    params = dict(parameters or {})
    params.setdefault("style", style)
    fmt = (params.get("format")
           or Path(file_path).suffix.lstrip(".")).lower()
    if fmt == "svg":
        # Forward any heat-map settings passed the ``classic.apply`` way
        # (``heatmap_node`` / ``heatmap_edge`` tuples + span) to the vector
        # renderer, so an .svg export carries the same heat-map as the .png.
        hn = params.get("heatmap_node")
        he = params.get("heatmap_edge")
        Path(file_path).write_text(
            _grid.render_svg(
                family, style,
                heatmap=bool(hn or he),
                node_metric=hn[0] if hn else None,
                edge_metric=he[0] if he else None,
                heatmap_global=bool(params.get("heatmap_global")),
                node_span=params.get("node_span"),
                edge_span=params.get("edge_span")),
            encoding="utf-8")
        return file_path
    return _grid.render(family, file_path, parameters=params)


def view_ucm_family(
    family,
    style: str = "ucm",
    parameters: Optional[Dict[str, Any]] = None,
):
    """Render the family grid to a temporary PNG and open it in the
    system viewer (Windows); returns the image path."""
    import os
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
    save_vis_ucm_family(family, tmp, style=style, parameters=parameters)
    try:
        os.startfile(tmp)  # Windows
    except (AttributeError, OSError):
        pass
    return tmp


def compute_family_stats(family, parameters: Optional[Dict[str, Any]] = None,
                         progress_callback=None):
    """Comparative statistics for every cell of a mined family — the
    data layer behind :func:`write_family_report` and the web app's
    Compare tab.

    Three levels, all designed for ranking and cross-cell comparison:
    **process** (cases, events, events per case, case-duration
    min/mean/median/max and **total**, behavioural variant counts,
    replay fitness), **activity** (frequency, case coverage, and — for
    interval logs with a ``start_timestamp`` column — service-time
    min/mean/median/max/total per activity), and **choice** (OR-fork
    branch counts *aligned across cells* through the family's shared
    skeleton, so the same decision point is one comparable row for
    every combination).

    Needs ``family.log_df`` — call it right after
    :func:`discover_ucm_family` (the result carries no DataFrames and
    stays small). ``parameters`` may override the column names
    (``case_id_col``, ``activity_col``, ``timestamp_col``,
    ``start_timestamp_col``). Returns a
    :class:`~pm4py_ucm.algo.discovery.families.stats.FamilyStats`
    with pandas helpers (``process_frame``, ``activity_frame``,
    ``choice_share_frame``) and a JSON-ready ``to_dict``."""
    params = dict(parameters or {})
    return _families.compute_family_stats(
        family,
        case_id_col=params.get("case_id_col", "case:concept:name"),
        activity_col=params.get("activity_col", "concept:name"),
        timestamp_col=params.get("timestamp_col", "time:timestamp"),
        start_timestamp_col=params.get(
            "start_timestamp_col", "start_timestamp"),
        progress_callback=progress_callback,
    )


def write_family_report(
    family,
    path: str,
    stats=None,
    title: Optional[str] = None,
    images: bool = True,
    style: str = "ucm",
    heat: Optional[Dict[str, Any]] = None,
) -> str:
    """Write a **self-contained interactive HTML report** comparing the
    family's processes — sortable heat-mapped statistics tables, a
    pair-comparison view (any two combinations side by side, with
    their model images and per-activity deltas), aligned OR-fork
    branch-share bars, and a model gallery. One file, no external
    dependencies: it opens offline in any browser and can be archived
    as supplementary material for a paper.

    ``stats`` defaults to :func:`compute_family_stats` on the family
    (which then needs ``family.log_df``); pass a precomputed
    ``FamilyStats`` when the log has been dropped. ``images=False``
    (or a machine without the graphviz binary) omits the embedded
    per-cell model images; ``style`` picks their notation (``"ucm"``
    or ``"bpmn"``). ``heat`` (optional) forwards performance heat-map
    kwargs — ``heatmap`` / ``node_metric`` / ``edge_metric`` /
    ``heatmap_global`` and an explicit ``node_span`` / ``edge_span`` — to the
    embedded per-cell images, so the report can carry the same heat-map as the
    web app's views. Returns ``path``."""
    return _families.write_family_report(
        family, path, stats=stats, title=title,
        images=images, style=style, heat=heat,
    )


def save_vis_ucm(
    ucm: UCM,
    file_path: str,
    style: str = "ucm",
    parameters: Optional[Dict[str, Any]] = None,
    map: Optional[str] = None,
) -> str:
    """Render the UCM and save the resulting image to ``file_path``.

    See :func:`view_ucm` for the ``style`` and ``map`` parameters. When
    the UCM has multiple maps and ``map`` is left as ``None``, all maps
    are composed vertically into a single image (root map at the top,
    plug-in maps below in pre-order); each panel carries a title strip
    with the map's name, and adjacent panels are separated by a thin
    horizontal rule.

    The output format is taken from ``file_path``'s extension (or an
    explicit ``parameters["format"]``). An **``.svg``** target produces a
    single self-contained vector document — crisp at any zoom, its text
    selectable — in which each stub is hyperlinked to its plug-in map's
    panel (a single plug-in links directly; a dynamic multi-binding stub
    carries a small picker), so a decomposed model is navigable when the
    SVG is opened in a browser or the web viewer. Any other extension
    (``.png``, ``.pdf`` …) goes through graphviz as before."""
    params = dict(parameters or {})
    params.setdefault("style", style)
    fmt = (params.get("format")
           or Path(file_path).suffix.lstrip(".")).lower()
    if fmt == "svg" and map is None:
        # One navigable vector document for the whole model (single map or
        # a stacked, stub-linked decomposition) — the same renderer the
        # web app and the HTML reports use.
        from .visualization.ucm import svg as _svg
        Path(file_path).write_text(
            _svg.model_to_svg(ucm, style), encoding="utf-8")
        return file_path
    if len(ucm.maps) > 1 and map is None:
        from .visualization.ucm import stacked as _stacked
        return _stacked.render(ucm, file_path, parameters=params)
    if map is not None:
        params["map_name"] = map
    gviz = _visualizer.apply(ucm, parameters=params)
    return _visualizer.save(gviz, file_path)
