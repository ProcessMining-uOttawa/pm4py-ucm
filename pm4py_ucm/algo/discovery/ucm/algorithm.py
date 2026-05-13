"""Variant-dispatching UCM discovery algorithm.

Mirrors ``pm4py.algo.discovery.bpmn.algorithm`` so users moving between
notations have a familiar API::

    from pm4py_ucm.algo.discovery.ucm import algorithm as ucm_discovery
    ucm = ucm_discovery.apply(log, variant=ucm_discovery.Variants.INDUCTIVE)
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional

from .variants import inductive as _inductive
from ....objects.ucm.obj import UCM


class Variants(Enum):
    """Available discovery back-ends."""
    INDUCTIVE = _inductive


DEFAULT_VARIANT = Variants.INDUCTIVE


def apply(
    log,
    variant: Variants = DEFAULT_VARIANT,
    parameters: Optional[Dict[str, Any]] = None,
) -> UCM:
    """Discover a Use Case Map from ``log`` using the chosen variant."""
    return variant.value.apply(log, parameters=parameters)
