"""Attribute-based model families.

Partition an event log by the values of one or two case-level
attributes (e.g. *cancer type* × *age group*), mine one UCM per
partition cell, and assemble the results — as separate models, as one
combined multi-map model, or as a single *overarching* model whose
dynamic stub selects the applicable plug-in map via attribute
conditions.

Modules:

* :mod:`.partition` — case-attribute detection and log partitioning;
* :mod:`.family` — the :class:`ModelFamily` container and file export;
* :mod:`.algorithm` — per-cell discovery (``discover``);
* :mod:`.assembly` — combined single-URN and dynamic-stub umbrella
  assembly.
"""

from .partition import (  # noqa: F401
    PartitionAttribute,
    PartitionCell,
    PartitionValue,
    Partition,
    detect_case_attributes,
    partition_log,
)
from .family import FamilyCell, ModelFamily, write_family  # noqa: F401
from .algorithm import discover  # noqa: F401
from .assembly import assemble_combined, assemble_umbrella  # noqa: F401
