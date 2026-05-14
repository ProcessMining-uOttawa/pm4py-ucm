"""Inductive Use-Case-Map miner.

Pipeline:

1. Discover a process tree using ``pm4py.discover_process_tree_inductive``.
2. Optionally mine the activity→performer mapping via
   :mod:`pm4py_ucm.algo.discovery.resources`.
3. Convert the tree into a Use Case Map via
   :mod:`pm4py_ucm.objects.ucm.conversion.from_process_tree`, binding
   performers if available.

The resulting UCM has its responsibilities populated from the activity
labels of the tree's leaves; if a resource attribute is configured, every
responsibility also has its :attr:`UCM.Responsibility.performer` set, and
each :class:`UCM.RespRef` is drawn inside the right component rectangle.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .....objects.ucm.obj import UCM
from .....objects.ucm.conversion import from_process_tree as _converter
from ...resources import algorithm as _resources


def apply(log, parameters: Optional[Dict[str, Any]] = None) -> UCM:
    """Mine a UCM from an event log via the inductive miner.

    Parameters
    ----------
    log
        Anything :func:`pm4py.discover_process_tree_inductive` accepts:
        a ``pandas.DataFrame``, a ``pm4py.objects.log.obj.EventLog``, or
        path to a ``.xes`` file.
    parameters
        Optional dictionary forwarded to the converter (see
        :func:`pm4py_ucm.objects.ucm.conversion.from_process_tree.apply`).
        Additional keys understood here:

        ``resource_attribute``
            Name (or list of names) of the event attribute to mine for
            performer information. Common values are ``"org:resource"``
            (default if just ``True``), ``"org:role"``, ``"org:group"``.
            When unset, no resource mining is performed.
        ``resource_parameters``
            Forwarded to
            :func:`pm4py_ucm.algo.discovery.resources.algorithm.apply`.

    Returns
    -------
    UCM
        The discovered Use Case Map.

    Raises
    ------
    ImportError
        If ``pm4py`` is not installed in the current environment.
    """
    try:
        import pm4py
    except ImportError as exc:  # pragma: no cover - depends on env
        raise ImportError(
            "discover_ucm_inductive requires pm4py to be installed; "
            "install it with `pip install pm4py`."
        ) from exc

    parameters = dict(parameters or {})
    # Resource mining is enabled by default: when the log carries any of
    # the standard XES "who" attributes (``org:resource`` / ``org:role``
    # / ``org:group``) the miner produces a non-empty
    # ``{activity: performer}`` mapping; otherwise it returns ``{}`` and
    # nothing changes. The historical opt-in behaviour is preserved for
    # callers that pass ``resource_attribute=False`` explicitly, which
    # disables resource mining entirely.
    res_attr = parameters.pop(
        "resource_attribute",
        ("org:resource", "org:role", "org:group"),
    )
    res_params = dict(parameters.pop("resource_parameters", None) or {})

    # When the caller passes a path to an XES file, materialise it once
    # up front. PM4Py's inductive miner and our resource miner both
    # expect a DataFrame / EventLog, not a string path — the latter
    # was previously accepted only by the docstring.
    if isinstance(log, str):
        log = pm4py.read_xes(log)

    # Allow the caller to either pass a pre-built tree or a log.
    tree = parameters.pop("process_tree", None)
    if tree is None:
        tree = pm4py.discover_process_tree_inductive(log)

    # Mine performers when enabled (default: yes — with a fallback
    # attribute list — unless the caller passed ``resource_attribute=False``).
    if res_attr:
        if isinstance(res_attr, (list, tuple)):
            res_params.setdefault("attribute_priority", list(res_attr))
        elif isinstance(res_attr, str):
            res_params.setdefault("attribute", res_attr)
        # else: assume `True` / truthy default
        performers = _resources.apply(log, parameters=res_params)
        # Caller-supplied performers override mined ones (for tests / overrides)
        existing = parameters.get("performers") or {}
        performers.update(existing)
        parameters["performers"] = performers

        # Also surface every distinct resource value in the log as a URN
        # component, separate from the activity binding. This matters for
        # logs (e.g. Road Traffic Fines) where many activities don't have
        # a clean modal performer and would otherwise drop out of the
        # component vocabulary entirely.
        existing_extras = parameters.get("additional_components") or []
        all_resources = _resources.distinct_components(log, parameters=res_params)
        already_bound = set(performers.values())
        extras = [r for r in all_resources if r and r not in already_bound]
        if extras or existing_extras:
            seen = set()
            merged = []
            for c in list(existing_extras) + extras:
                if c and c not in seen:
                    seen.add(c)
                    merged.append(c)
            parameters["additional_components"] = merged

    return _converter.apply(tree, parameters=parameters)

