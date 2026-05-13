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

def discover_ucm_inductive(log, parameters: Optional[Dict[str, Any]] = None) -> UCM:
    """Discover a UCM from an event log using the inductive miner.

    Pass ``resource_attribute="org:resource"`` (or another attribute name,
    or a fallback list) in ``parameters`` to also mine activity→performer
    bindings and surface them in the resulting UCM."""
    return _discovery.apply(
        log,
        variant=_discovery.Variants.INDUCTIVE,
        parameters=parameters,
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

def convert_to_ucm(obj, parameters: Optional[Dict[str, Any]] = None) -> UCM:
    """Convert a supported object (currently process trees) to a UCM."""
    if isinstance(obj, UCM):
        return obj
    # Duck-type a process tree: it has ``operator`` / ``children`` / ``label``.
    if all(hasattr(obj, a) for a in ("operator", "children", "label")):
        return _tree_converter.apply(obj, parameters=parameters)
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
    """
    params = dict(parameters or {})
    params.setdefault("style", style)
    gviz = _visualizer.apply(ucm, parameters=params)
    return _visualizer.view(gviz)


def save_vis_ucm(
    ucm: UCM,
    file_path: str,
    style: str = "ucm",
    parameters: Optional[Dict[str, Any]] = None,
) -> str:
    """Render the UCM and save the resulting image to ``file_path``.

    See :func:`view_ucm` for the ``style`` parameter."""
    params = dict(parameters or {})
    params.setdefault("style", style)
    gviz = _visualizer.apply(ucm, parameters=params)
    return _visualizer.save(gviz, file_path)
