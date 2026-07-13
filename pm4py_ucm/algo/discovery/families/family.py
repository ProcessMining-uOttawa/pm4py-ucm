"""The :class:`ModelFamily` container and its per-cell file export.

A family is the output of :func:`pm4py_ucm.algo.discovery.families.
algorithm.discover`: one mined :class:`UCM` per partition cell, plus
the partition axes and coverage bookkeeping. The family is the single
object every downstream consumer works from — the grid visualizer, the
zip/dir exporter here, and the combined / umbrella assemblers in
:mod:`.assembly`.
"""

from __future__ import annotations

import csv
import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ....objects.ucm.obj import UCM
from .partition import PartitionAttribute, PartitionValue


_FILE_STEM_RE = re.compile(r"[^A-Za-z0-9_.=-]+")


def _file_token(text: str) -> str:
    """Filesystem-safe token: keep word chars, ``.``, ``=``, ``-``."""
    return _FILE_STEM_RE.sub("_", text).strip("_") or "value"


@dataclass
class FamilyCell:
    """One mined cell of the family."""

    values: Tuple[PartitionValue, ...]
    case_ids: List[str]
    n_cases: int
    coverage: float  # share of the log's total cases (0..1)
    tree: Any        # the process tree mined from this cell's sub-log
    ucm: UCM         # standalone per-cell model (own container)
    #: ``{activity: performer}`` mined from THIS cell's sub-log. Kept
    #: separately from the standalone model so the assemblers can
    #: detect *resource variation*: the same activity performed by
    #: different actors in different cells is a genuine difference
    #: between the cell processes even when control flow is identical.
    performers: Dict[str, str] = field(default_factory=dict)

    @property
    def labels(self) -> Tuple[str, ...]:
        return tuple(v.label for v in self.values)

    @property
    def label(self) -> str:
        return " / ".join(self.labels)

    @property
    def caption(self) -> str:
        """Display caption: ``"n=123 (14.2%)"``."""
        return f"n={self.n_cases} ({self.coverage * 100:.1f}%)"


@dataclass
class ModelFamily:
    """A family of UCM models mined from one log, partitioned by 1–2
    case attributes.

    ``attributes`` are the partition axes (1 or 2). ``cells`` hold one
    standalone :class:`UCM` each, in deterministic axis order.
    ``skipped_cells`` records observed value combinations excluded for
    having fewer than ``min_cases`` cases. ``log_df`` keeps the full
    normalized log so the assemblers can mine *whole-log* performer
    bindings (definitions are shared in combined output, so the
    performer of a responsibility must be decided globally, not per
    cell). ``cell_parameters`` records the converter parameters used
    per cell (including ``decomposition``) so assembly re-converts each
    cell's tree identically into a shared container."""

    attributes: List[PartitionAttribute]
    cells: List[FamilyCell]
    skipped_cells: List[Tuple[Tuple[PartitionValue, ...], int]]
    total_cases: int
    dropped_cases: int
    log_df: Any
    cell_parameters: Dict[str, Any] = field(default_factory=dict)
    decomposition: Any = None

    # ------------------------------------------------------------------
    # Lookup / grid helpers
    # ------------------------------------------------------------------

    @property
    def covered_cases(self) -> int:
        return sum(c.n_cases for c in self.cells)

    def cell(self, *labels: str) -> Optional[FamilyCell]:
        """Cell by display labels, e.g. ``family.cell("Breast", "40-59")``."""
        for c in self.cells:
            if c.labels == tuple(labels):
                return c
        return None

    @property
    def row_values(self) -> List[PartitionValue]:
        """Values of the first attribute — the grid rows."""
        return self.attributes[0].values

    @property
    def col_values(self) -> List[PartitionValue]:
        """Values of the second attribute (grid columns); empty for a
        single-attribute family (rendered as one column)."""
        if len(self.attributes) < 2:
            return []
        return self.attributes[1].values

    def grid(self) -> Dict[Tuple[str, ...], "FamilyCell"]:
        """``{labels: cell}`` for direct grid lookup."""
        return {c.labels: c for c in self.cells}

    def file_stem(self, cell: FamilyCell) -> str:
        """Deterministic per-cell file stem:
        ``cancer_type=Breast_age=40-59``."""
        parts = [
            f"{_file_token(a.display_name)}={_file_token(v.label)}"
            for a, v in zip(self.attributes, cell.values)
        ]
        return "_".join(parts)

    def summary_rows(self) -> List[List[str]]:
        """Rows of the family summary CSV (header included)."""
        header = [a.display_name for a in self.attributes]
        rows = [header + ["n_cases", "coverage_pct", "file"]]
        for c in self.cells:
            rows.append(
                list(c.labels)
                + [str(c.n_cases), f"{c.coverage * 100:.1f}",
                   self.file_stem(c) + ".jucm"]
            )
        for values, n in self.skipped_cells:
            rows.append(
                [v.label for v in values]
                + [str(n), "", "(skipped: below min_cases)"]
            )
        return rows


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def write_family(family: ModelFamily, path: str) -> str:
    """Write one ``.jucm`` file per cell plus a ``family_summary.csv``.

    ``path`` ending in ``.zip`` produces a single zip archive;
    otherwise ``path`` is treated as a directory (created if needed).
    Returns ``path``."""
    from ....objects.ucm.exporter.variants.jucm import serialize_to_string

    entries: List[Tuple[str, str]] = []
    for cell in family.cells:
        entries.append(
            (family.file_stem(cell) + ".jucm",
             serialize_to_string(cell.ucm))
        )

    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(family.summary_rows())
    entries.append(("family_summary.csv", buf.getvalue()))

    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, content in entries:
                zf.writestr(name, content)
    else:
        os.makedirs(path, exist_ok=True)
        for name, content in entries:
            with open(os.path.join(path, name), "w",
                      encoding="utf-8", newline="") as fh:
                fh.write(content)
    return path
