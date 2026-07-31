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
  assembly;
* :mod:`.stats` — family-wide comparative statistics (``FamilyStats``);
* :mod:`.report` — the self-contained interactive HTML report.
"""

from .partition import (  # noqa: F401
    PartitionAttribute,
    PartitionCell,
    PartitionValue,
    Partition,
    detect_case_attributes,
    partition_log,
)
from .advisor import (  # noqa: F401
    AttributeScore,
    rank_partition_attributes,
)
from .family import FamilyCell, ModelFamily, write_family  # noqa: F401
from .algorithm import discover  # noqa: F401
from .assembly import assemble_combined, assemble_umbrella  # noqa: F401
from .stats import (  # noqa: F401
    CellStats,
    FamilyChoice,
    FamilyStats,
    compute_family_stats,
)
from .report import family_report_html, write_family_report  # noqa: F401
