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

from typing import Any, Dict, Optional, Union

from .objects.ucm.obj import UCM
from .objects.ucm.exporter.variants import jucm as _jucm_exporter
from .objects.ucm.importer.variants import jucm as _jucm_importer
from .objects.ucm.conversion import from_process_tree as _tree_converter
from .visualization.ucm import visualizer as _visualizer
from .algo.discovery.ucm import algorithm as _discovery
from .algo.discovery.resources import algorithm as _resources
from .algo.discovery.variants import clustering as _clustering
from .algo.discovery.scenarios import synthesis as _scenarios
from .algo.discovery.scenarios import reports as _scenario_reports


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
        Same shape as in :func:`discover_ucm_inductive`. Pass ``None``
        (default) for a single flat map — the recommended setting for
        scenario synthesis.
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
    clustering = _clustering.cluster(log, tree, coarsen_loops=coarsen_loops)

    # 4. Synthesize scenarios on the UCM.
    _scenarios.synthesize_scenarios(
        ucm, tree, clustering,
        group_name=group_name,
        emit_conditions=emit_conditions,
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
    are composed vertically into a single PNG (root map at the top,
    plug-in maps below in pre-order); each panel carries a title strip
    with the map's name, and adjacent panels are separated by a thin
    horizontal rule."""
    params = dict(parameters or {})
    params.setdefault("style", style)
    if len(ucm.maps) > 1 and map is None:
        from .visualization.ucm import stacked as _stacked
        return _stacked.render(ucm, file_path, parameters=params)
    if map is not None:
        params["map_name"] = map
    gviz = _visualizer.apply(ucm, parameters=params)
    return _visualizer.save(gviz, file_path)
