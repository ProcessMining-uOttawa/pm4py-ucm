"""
Mine the *performer* of each activity from an event log.

Performers are the actors / teams / roles that execute each activity. In
URN they correspond to :class:`pm4py_ucm.UCM.ComponentElement` definitions,
and the result of mining is naturally a ``{activity: performer}`` dict
that :meth:`pm4py_ucm.UCM.bind_performers` can attach to a UCM.

Two variants ship with the package:

* ``"activity_attribute"`` — the default. Reads one or several event-level
  attributes (typically ``org:resource``, ``org:role``, ``org:group``)
  and uses a configurable aggregation strategy (mode, first-seen, …) to
  pick one performer per activity.
* ``"role_then_resource"`` — convenience preset over the same engine that
  prioritises ``org:role`` over ``org:resource`` over ``org:group``.

See :func:`pm4py_ucm.algo.discovery.resources.variants.activity_attribute.apply`
for the full parameter list.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, List, Optional

from .variants import activity_attribute


class Variants(Enum):
    ACTIVITY_ATTRIBUTE = activity_attribute
    ROLE_THEN_RESOURCE = activity_attribute  # presets via parameters


DEFAULT_VARIANT = Variants.ACTIVITY_ATTRIBUTE


def apply(
    log: Any,
    variant: Optional[Variants] = None,
    parameters: Optional[dict] = None,
) -> dict:
    """Mine the activity→performer mapping from ``log``. Returns a plain
    ``dict[str, str]`` suitable for :meth:`UCM.bind_performers`.

    Parameters
    ----------
    log
        A PM4Py EventLog or a pandas DataFrame in PM4Py's XES-like format.
    variant
        Discovery variant; see :class:`Variants`.
    parameters
        Variant-specific parameters. See the variant module's docstring.
    """
    variant = variant or DEFAULT_VARIANT
    params = dict(parameters or {})
    if variant is Variants.ROLE_THEN_RESOURCE:
        params.setdefault(
            "attribute_priority", ["org:role", "org:resource", "org:group"])
    return variant.value.apply(log, params)


def distinct_components(
    log: Any,
    variant: Optional[Variants] = None,
    parameters: Optional[dict] = None,
) -> List[str]:
    """Return every distinct performer-like value that appears anywhere
    in ``log`` — sorted for stable output.

    This is the natural complement to :func:`apply`. ``apply`` returns a
    ``{activity: performer}`` mapping (one performer per activity, picked
    by mode/first/whatever strategy); ``distinct_components`` returns the
    full *vocabulary* of values that occur, including:

    * resources that perform several different activities,
    * resources that perform only an activity with no clear majority
      (which ``apply`` drops via ``min_support``), and
    * resources that perform activities not in the discovered process
      model at all.

    Used by :func:`pm4py_ucm.discover_ucm_inductive` to register every
    real-world actor as a URN component, separate from whether any
    specific responsibility happens to be bound to it.
    """
    variant = variant or DEFAULT_VARIANT
    params = dict(parameters or {})
    if variant is Variants.ROLE_THEN_RESOURCE:
        params.setdefault(
            "attribute_priority", ["org:role", "org:resource", "org:group"])
    return variant.value.distinct_components(log, params)
