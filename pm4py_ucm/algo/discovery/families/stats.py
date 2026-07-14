"""Family-wide comparative statistics (the ``FamilyStats`` layer).

Computes, for every cell of a :class:`~.family.ModelFamily`, three
levels of statistics designed for *comparison across cells* — the data
behind the interactive HTML report (:mod:`.report`) and the web app's
Compare tab:

* **process level** — case counts, event counts, events per case,
  case duration (min / mean / median / max and the **total** across
  cases), distinct activities, and the concurrency-aware behavioural
  variant counts + replay fitness;
* **activity level** — execution frequency, case coverage, *sojourn*
  time (since the case's previous event — derivable from any
  timestamped log), and (for interval logs carrying a
  ``start_timestamp`` column) service-time
  min / mean / median / max / **total** per activity, per cell;
* **edge level** — directly-follows activity pairs with traversal
  frequency and waiting-time min / mean / median / max / total
  (completion → start on interval logs, completion → completion
  otherwise);
* **choice level** — OR-fork branch counts *aligned across cells*:
  the per-cell trees are anti-unified into the family skeleton (the
  same merge that builds the umbrella, control-flow only), each cell's
  sub-log is replayed on the cell's configured tree, and branch
  traversal counts land on skeleton-identified forks — so "the choice
  after *Close Assessment*" is one row even though every cell mined
  its own tree.

Everything is computed from ``family.log_df`` **once**, up front
(:func:`compute_family_stats`), and the result carries no DataFrames —
callers that drop the log after mining (the web app's cache does) keep
a small, picklable object. Time statistics are only derivable from the
log's timestamp columns; when a column is missing the corresponding
entries are honestly absent rather than fabricated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ....objects.ucm.conversion.decomposition import (
    _first_label,
    _label_clean,
    _last_label,
    _truncate_words,
)
from ...performance import compute_performance_stats
from ..variants import choice_signature as _cs
from ..variants import clustering as _clustering
from .family import FamilyCell, ModelFamily


_LOOP_OP = "*"
_CHOICE_OPS = ("X", "O")


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------

@dataclass
class CellStats:
    """Statistics of one family cell (no DataFrames — picklable)."""

    labels: Tuple[str, ...]
    label: str
    file_stem: str
    n_cases: int
    coverage: float          # share of the log's total cases (0..1)
    n_events: int
    n_activities: int
    #: events per case: ``{"mean", "median", "min", "max"}``.
    events_per_case: Dict[str, float] = field(default_factory=dict)
    #: case duration in seconds: ``{"mean", "median", "min", "max",
    #: "total", "std", "p90", "p95"}`` — absent (empty) when the log has
    #: no timestamps.
    duration: Dict[str, float] = field(default_factory=dict)
    #: rework / repetition: ``{"case_fraction"``: share of cases with at
    #: least one repeated activity, ``"mean_repeats_per_case"``: mean
    #: over cases of Σ(count−1) repeat executions ``}``.
    rework: Dict[str, float] = field(default_factory=dict)
    #: ``{activity: n_cases}`` — cases whose first event is that
    #: activity (sum = :attr:`n_cases`).
    start_activities: Dict[str, int] = field(default_factory=dict)
    #: ``{activity: n_cases}`` — cases whose last event is that activity.
    end_activities: Dict[str, int] = field(default_factory=dict)
    #: behavioural variants (concurrency-aware replay on the cell's
    #: configured tree): ``{"n_variants", "n_sequence_variants",
    #: "fitness"}`` — fitness is the share of cases in [0, 1] that
    #: replayed cleanly.
    variants: Dict[str, float] = field(default_factory=dict)
    #: per-activity statistics: ``{activity: {"frequency",
    #: "case_coverage", "mean_time", ..., "sojourn_mean_time", ...}}``.
    #: Plain ``*_time`` entries are service times (interval logs
    #: only); ``sojourn_*_time`` entries are the time since the case's
    #: previous event (any timestamped log).
    activity: Dict[str, Dict[str, float]] = field(default_factory=dict)
    #: per-edge (directly-follows activity pair) statistics, keyed by
    #: the display label ``"A → B"``: ``{"frequency", "mean_time",
    #: "median_time", "min_time", "max_time", "total_time"}`` —
    #: waiting times (completion → start on interval logs;
    #: completion → completion otherwise, which includes the
    #: successor's service time). Empty for logs without timestamps.
    edges: Dict[str, Dict[str, float]] = field(default_factory=dict)


@dataclass
class FamilyChoice:
    """One OR-fork choice, aligned across the family's cells.

    ``counts[i]`` is the branch-traversal count list for the family's
    i-th cell (parallel to :attr:`FamilyStats.cells`) — ``None`` when
    that cell never reaches this choice (e.g. the choice lives in a
    variation-point variant the cell does not use)."""

    choice_id: str
    #: Human context: ``"after Close Assessment"`` / ``"at start"``.
    name: str
    #: Branch display labels (first activity of each branch).
    branches: List[str]
    #: The choice sits inside a loop — counts are evaluations summed
    #: across iterations, so they may exceed the cell's case count.
    inside_loop: bool
    #: ``True``: the fork is part of the shared family skeleton (every
    #: cell can reach it); ``False``: it sits inside one variation
    #: point variant (only the covering cells have counts).
    shared: bool
    counts: List[Optional[List[int]]] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        """``"after Close Assessment: Amend vs Request"``."""
        vs = " vs ".join(self.branches[:2])
        if len(self.branches) > 2:
            vs += " vs ..."
        return f"{self.name}: {vs}"


@dataclass
class FamilyStats:
    """Comparative statistics for every cell of a model family.

    Produced by :func:`compute_family_stats`; consumed by
    :func:`~.report.write_family_report` and the web app's Compare
    tab. Carries only plain Python data (picklable, JSON-friendly via
    :meth:`to_dict`)."""

    attribute_names: List[str]
    cells: List[CellStats]
    choices: List[FamilyChoice]
    activity_names: List[str]
    #: The log carries ``start_timestamp`` — activity service times
    #: are available. Case durations only need the completion
    #: timestamp (see :attr:`has_timestamps`).
    has_intervals: bool
    has_timestamps: bool
    total_cases: int
    covered_cases: int
    dropped_cases: int
    #: Observed value combinations excluded for having fewer than
    #: ``min_cases`` cases: ``[(labels, n_cases), ...]``.
    skipped: List[Tuple[Tuple[str, ...], int]] = field(default_factory=list)
    #: Union of the cells' directly-follows edges (``"A → B"``
    #: labels), ordered by descending family-wide frequency so the
    #: busiest handovers list first. Empty without timestamps.
    edge_names: List[str] = field(default_factory=list)

    @property
    def cell_labels(self) -> List[str]:
        return [c.label for c in self.cells]

    def cell(self, label: str) -> Optional[CellStats]:
        for c in self.cells:
            if c.label == label:
                return c
        return None

    # ------------------------------------------------------------------
    # pandas views (for the Streamlit Compare tab and notebooks)
    # ------------------------------------------------------------------

    def process_frame(self):
        """One row per cell: the process-level metrics side by side.
        Durations are in seconds; ``coverage`` and ``fitness`` in
        percent."""
        import pandas as pd

        rows = []
        for c in self.cells:
            row: Dict[str, Any] = {
                "cell": c.label,
                "cases": c.n_cases,
                "coverage_pct": round(c.coverage * 100, 1),
                "events": c.n_events,
                "activities": c.n_activities,
            }
            for k in ("mean", "median", "min", "max"):
                row[f"events_per_case_{k}"] = c.events_per_case.get(k)
            for k in ("mean", "median", "min", "max", "total",
                      "std", "p90", "p95"):
                row[f"duration_{k}_s"] = c.duration.get(k)
            row["rework_case_fraction"] = c.rework.get("case_fraction")
            row["rework_mean_repeats"] = c.rework.get("mean_repeats_per_case")
            row["variants"] = c.variants.get("n_variants")
            row["sequence_variants"] = c.variants.get("n_sequence_variants")
            fitness = c.variants.get("fitness")
            row["fitness_pct"] = (
                round(fitness * 100, 1) if fitness is not None else None
            )
            rows.append(row)
        return pd.DataFrame(rows).set_index("cell")

    def activity_frame(self, metric: str = "frequency"):
        """Activities × cells matrix for one activity metric."""
        import pandas as pd

        data = {
            c.label: {
                act: entry.get(metric)
                for act, entry in c.activity.items()
                if entry.get(metric) is not None
            }
            for c in self.cells
        }
        frame = pd.DataFrame(data)
        return frame.reindex(sorted(frame.index))

    def edge_frame(self, metric: str = "frequency"):
        """Directly-follows edges × cells matrix for one edge metric
        (``frequency`` or waiting-time ``mean_time`` / ``median_time``
        / ``min_time`` / ``max_time`` / ``total_time``)."""
        import pandas as pd

        data = {
            c.label: {
                edge: entry.get(metric)
                for edge, entry in c.edges.items()
                if entry.get(metric) is not None
            }
            for c in self.cells
        }
        return pd.DataFrame(data).reindex(self.edge_names)

    def choice_count_frame(self, choice: "FamilyChoice"):
        """Cells × branches matrix of traversal counts (NaN = the cell
        never reaches the choice)."""
        import pandas as pd

        rows = {}
        for cell, counts in zip(self.cells, choice.counts):
            rows[cell.label] = (
                dict(zip(choice.branches, counts))
                if counts is not None else {}
            )
        return pd.DataFrame.from_dict(
            rows, orient="index", columns=choice.branches,
        )

    def choice_share_frame(self, choice: "FamilyChoice"):
        """Cells × branches matrix of branch shares in percent."""
        counts = self.choice_count_frame(choice)
        totals = counts.sum(axis=1)
        return counts.div(totals.where(totals > 0), axis=0) * 100

    # ------------------------------------------------------------------
    # JSON view (for the self-contained HTML report)
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attributes": list(self.attribute_names),
            "hasIntervals": self.has_intervals,
            "hasTimestamps": self.has_timestamps,
            "totalCases": self.total_cases,
            "coveredCases": self.covered_cases,
            "droppedCases": self.dropped_cases,
            "skipped": [
                {"labels": list(labels), "cases": n}
                for labels, n in self.skipped
            ],
            "activityNames": list(self.activity_names),
            "edgeNames": list(self.edge_names),
            "cells": [
                {
                    "labels": list(c.labels),
                    "label": c.label,
                    "fileStem": c.file_stem,
                    "cases": c.n_cases,
                    "coverage": c.coverage,
                    "events": c.n_events,
                    "activities": c.n_activities,
                    "eventsPerCase": dict(c.events_per_case),
                    "duration": dict(c.duration),
                    "rework": dict(c.rework),
                    "startActivities": dict(c.start_activities),
                    "endActivities": dict(c.end_activities),
                    "variants": dict(c.variants),
                    "activity": {
                        act: dict(entry) for act, entry in c.activity.items()
                    },
                    "edges": {
                        edge: dict(entry) for edge, entry in c.edges.items()
                    },
                }
                for c in self.cells
            ],
            "choices": [
                {
                    "id": ch.choice_id,
                    "name": ch.name,
                    "fullName": ch.full_name,
                    "branches": list(ch.branches),
                    "insideLoop": ch.inside_loop,
                    "shared": ch.shared,
                    "counts": [
                        list(c) if c is not None else None
                        for c in ch.counts
                    ],
                }
                for ch in self.choices
            ],
        }


# ---------------------------------------------------------------------------
# Process + activity level
# ---------------------------------------------------------------------------

def _series_stats(series) -> Dict[str, float]:
    return {
        "mean": float(series.mean()),
        "median": float(series.median()),
        "min": float(series.min()),
        "max": float(series.max()),
    }


def _duration_stats(span) -> Dict[str, float]:
    """Case-duration aggregates including total, sample std (ddof=1) and
    the P90 / P95 percentiles (linear interpolation)."""
    out = _series_stats(span)
    out["total"] = float(span.sum())
    out["p90"] = float(span.quantile(0.9))
    out["p95"] = float(span.quantile(0.95))
    if len(span) > 1:
        out["std"] = float(span.std())
    return out


def _rework_stats(cell_df, case_id_col: str, activity_col: str
                  ) -> Dict[str, float]:
    """``{"case_fraction", "mean_repeats_per_case"}`` — how much a cell
    repeats activities within a case (a rework indicator)."""
    case = cell_df[case_id_col].astype(str)
    act = cell_df[activity_col].astype(str)
    # Per (case, activity) occurrence counts.
    counts = act.groupby([case, act]).size()
    repeats_per_case = (counts - 1).clip(lower=0).groupby(level=0).sum()
    n_cases = int(case.nunique())
    if n_cases == 0:
        return {}
    return {
        "case_fraction": float((repeats_per_case > 0).sum()) / n_cases,
        "mean_repeats_per_case": float(repeats_per_case.sum()) / n_cases,
    }


def _process_level(
    cell_df,
    case_id_col: str,
    activity_col: str,
    timestamp_col: str,
    start_timestamp_col: str,
) -> Tuple[Dict[str, float], Dict[str, float], int, int]:
    """``(events_per_case, duration, n_events, n_activities)`` for one
    cell's sub-log. ``duration`` includes the **total** (summed) case
    duration plus std / P90 / P95; empty when the log has no completion
    timestamps."""
    import pandas as pd

    n_events = int(len(cell_df))
    n_activities = int(cell_df[activity_col].astype(str).nunique())
    per_case = cell_df.groupby(cell_df[case_id_col].astype(str))
    events_per_case = _series_stats(per_case.size())

    duration: Dict[str, float] = {}
    if timestamp_col in cell_df.columns:
        end = pd.to_datetime(cell_df[timestamp_col])
        start = (
            pd.to_datetime(cell_df[start_timestamp_col])
            if start_timestamp_col in cell_df.columns else end
        )
        case = cell_df[case_id_col].astype(str)
        span = (
            end.groupby(case).max() - start.groupby(case).min()
        ).dt.total_seconds()
        duration = _duration_stats(span)
    return events_per_case, duration, n_events, n_activities


def _start_end_activities(
    cell_df, case_id_col: str, activity_col: str, timestamp_col: str,
) -> Tuple[Dict[str, int], Dict[str, int]]:
    """``(start_activities, end_activities)`` — cases beginning / ending
    with each activity. Ordered by timestamp when present, else by row
    order within the case."""
    df = cell_df
    if timestamp_col in df.columns:
        df = df.sort_values([case_id_col, timestamp_col])
    grp = df.groupby(df[case_id_col].astype(str), sort=False)[activity_col]
    start = {str(a): int(n)
             for a, n in grp.first().astype(str).value_counts().items()}
    end = {str(a): int(n)
           for a, n in grp.last().astype(str).value_counts().items()}
    return start, end


# ---------------------------------------------------------------------------
# Choice level — skeleton-aligned fork statistics
# ---------------------------------------------------------------------------

def _walk_structure(node, visit, parents: Dict[int, Tuple[Any, int]]):
    """DFS over the merged skeleton, descending into every variation
    point's group subtrees. ``visit(node, covering_cells_or_None)`` is
    called on every node; ``parents[id(child)] = (parent, child_index)``
    is filled for the context-naming walk (a group subtree's parent is
    the variation point itself)."""
    from .assembly import _VariationPoint, _children_of

    def _walk(n, cover):
        visit(n, cover)
        if isinstance(n, _VariationPoint):
            for cells, subtree in n.groups:
                parents[id(subtree)] = (n, 0)
                _walk(subtree, cells)
            return
        for i, c in enumerate(_children_of(n)):
            parents[id(c)] = (n, i)
            _walk(c, cover)

    _walk(node, None)


def _sibling_last_label(sibling, cover) -> str:
    """Last activity of a preceding sibling. A variation-point sibling
    has no label of its own — take the last activity of each variant
    subtree instead, restricted to the groups covering ``cover``'s
    cells (a choice that only exists for some cells should be named by
    *their* preceding activity, not every family member's)."""
    if not _is_vp(sibling):
        return _last_label(sibling)
    labels: List[str] = []
    for group_cells, subtree in sibling.groups:
        if cover is not None and not any(
                c is gc for c in cover for gc in group_cells):
            continue
        lab = _last_label(subtree)
        if lab and lab not in labels:
            labels.append(lab)
    if len(labels) > 2:
        return " | ".join(labels[:2]) + " | ..."
    return " | ".join(labels)


def _preceding_label(
    node, parents: Dict[int, Tuple[Any, int]], cover,
) -> str:
    """The activity executed directly before ``node`` — walking up
    through sequences (previous siblings' last activity), loop redo
    branches (the do part), and variation-point boundaries."""
    from .assembly import _children_of, _op_of

    current = node
    while id(current) in parents:
        parent, index = parents[id(current)]
        op = _op_of(parent) if not _is_vp(parent) else None
        if op == "->":
            siblings = _children_of(parent)
            for j in range(index - 1, -1, -1):
                lab = _sibling_last_label(siblings[j], cover)
                if lab:
                    return lab
        elif op == _LOOP_OP and index == 1:
            lab = _last_label(_children_of(parent)[0])
            if lab:
                return lab
        current = parent
    return ""


def _is_vp(node) -> bool:
    from .assembly import _VariationPoint
    return isinstance(node, _VariationPoint)


def _inside_loop(node, parents: Dict[int, Tuple[Any, int]]) -> bool:
    from .assembly import _op_of

    current = node
    while id(current) in parents:
        parent, _ = parents[id(current)]
        if not _is_vp(parent) and _op_of(parent) == _LOOP_OP:
            return True
        current = parent
    return False


def _branch_labels(children: Sequence[Any]) -> List[str]:
    """Display label per branch: the branch's first activity, with
    duplicates disambiguated and empty (tau) branches as ``(skip)``."""
    labels: List[str] = []
    seen: Dict[str, int] = {}
    for child in children:
        lab = _truncate_words(_label_clean(_first_label(child))) or "(skip)"
        seen[lab] = seen.get(lab, 0) + 1
        labels.append(lab if seen[lab] == 1 else f"{lab} ({seen[lab]})")
    return labels


def _collect_choices(
    merged,
    family: ModelFamily,
) -> Tuple[List[FamilyChoice], Dict[int, FamilyChoice]]:
    """Walk the merged skeleton (and every variation point's variants)
    and create one :class:`FamilyChoice` per XOR/OR node, in
    deterministic DFS order. Returns the ordered list plus the
    ``{id(tree_node): choice}`` index the replay counts land on."""
    from .assembly import _children_of, _op_of

    parents: Dict[int, Tuple[Any, int]] = {}
    found: List[Tuple[Any, Optional[List[FamilyCell]]]] = []

    def visit(node, cover):
        if not _is_vp(node) and _op_of(node) in _CHOICE_OPS:
            children = _children_of(node)
            if len(children) >= 2:
                found.append((node, cover))

    _walk_structure(merged, visit, parents)

    n_cells = len(family.cells)
    choices: List[FamilyChoice] = []
    by_node: Dict[int, FamilyChoice] = {}
    for i, (node, cover) in enumerate(found):
        prev = _preceding_label(node, parents, cover)
        choice = FamilyChoice(
            choice_id=f"choice_{i + 1}",
            name=(
                f"after {_truncate_words(_label_clean(prev))}"
                if prev else "at start"
            ),
            branches=_branch_labels(_children_of(node)),
            inside_loop=_inside_loop(node, parents),
            shared=cover is None,
            counts=[None] * n_cells,
        )
        choices.append(choice)
        by_node[id(node)] = choice
    return choices, by_node


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def compute_family_stats(
    family: ModelFamily,
    *,
    case_id_col: str = "case:concept:name",
    activity_col: str = "concept:name",
    timestamp_col: str = "time:timestamp",
    start_timestamp_col: str = "start_timestamp",
    coarsen_loops: bool = True,
    progress_callback=None,
) -> FamilyStats:
    """Compute :class:`FamilyStats` for a mined family.

    ``progress_callback(stage, done, total)`` (optional) fires per
    processed cell — the per-cell replay dominates this pass. See
    :mod:`pm4py_ucm.util.progress`.

    Needs ``family.log_df`` — call this right after
    :func:`pm4py_ucm.discover_ucm_family`, *before* dropping the log
    (the result itself carries no DataFrames and stays small).

    The choice-level alignment reuses the umbrella's skeleton merge
    (control-flow only — resource variation does not split forks) and
    the concurrency-aware replay: every cell's sub-log is replayed on
    the cell's configured tree, so branch counts of the same skeleton
    fork are directly comparable across cells, and each cell's
    behavioural variant count / replay fitness comes out of the same
    pass."""
    if family.log_df is None:
        raise ValueError(
            "compute_family_stats needs family.log_df — compute the "
            "statistics right after mining, before dropping the log."
        )
    from .assembly import _VariationPoint, _merge_variants
    from .scenarios import _configured_tree

    df = family.log_df
    has_timestamps = timestamp_col in df.columns
    has_intervals = has_timestamps and start_timestamp_col in df.columns
    case_series = df[case_id_col].astype(str)

    # Skeleton merge for choice alignment: control-flow only —
    # performer differences must not split forks that compare
    # behaviourally (unlike the umbrella, which shows resource
    # variation as variation).
    merged = _merge_variants(
        [([cell], cell.tree) for cell in family.cells],
        dedup=True, resource_variation=False,
    )
    choices, choice_by_node = _collect_choices(merged, family)

    from ....util.progress import Ticker
    ticker = Ticker(progress_callback, "Computing family statistics",
                    len(family.cells), report_every=1)

    cells: List[CellStats] = []
    all_activities: set = set()
    for cell_index, cell in enumerate(family.cells):
        cell_df = df[case_series.isin(set(cell.case_ids))]

        events_per_case, duration, n_events, n_activities = _process_level(
            cell_df, case_id_col, activity_col,
            timestamp_col, start_timestamp_col,
        )

        edge_stats: Dict[str, Dict[str, float]] = {}
        if has_timestamps:
            perf = compute_performance_stats(
                cell_df,
                case_id_col=case_id_col,
                activity_col=activity_col,
                timestamp_col=timestamp_col,
                start_timestamp_col=start_timestamp_col,
            )
            activity_stats = {
                act: dict(entry) for act, entry in perf.activity.items()
            }
            # Directly-follows edges — the between-activities view.
            # Frequencies and waiting times need only completion
            # timestamps, so single-timestamp logs are covered.
            edge_stats = {
                f"{a} → {b}": dict(entry)
                for (a, b), entry in perf.pairs.items()
            }
        else:
            # No timestamps at all — frequencies and coverage only.
            acts = cell_df[activity_col].astype(str)
            freq = acts.value_counts()
            cov = cell_df.groupby(acts)[case_id_col].nunique()
            n_ev = int(len(cell_df))
            activity_stats = {
                str(a): {
                    "frequency": int(freq[a]),
                    "case_coverage": int(cov.get(a, 0)),
                    "relative_frequency": (
                        int(freq[a]) / n_ev if n_ev else 0.0),
                    "repeat_frequency": int(freq[a]) - int(cov.get(a, 0)),
                }
                for a in freq.index
            }
        all_activities.update(activity_stats)

        rework = _rework_stats(cell_df, case_id_col, activity_col)
        start_acts, end_acts = _start_end_activities(
            cell_df, case_id_col, activity_col, timestamp_col)

        # Replay on the configured tree (skeleton with this cell's
        # variants substituted, reusing the original node objects) —
        # yields the variant counts AND the aligned choice counts.
        def chooser(vp: "_VariationPoint", _cell=cell):
            for group_cells, subtree in vp.groups:
                if any(c is _cell for c in group_cells):
                    return subtree
            return vp.groups[0][1]

        alias: Dict[int, Any] = {}
        ctree = _configured_tree(merged, chooser, alias)
        ctree_ids = _cs.assign_node_ids(ctree)
        inverse_ids = {v: k for k, v in ctree_ids.items()}
        clustering = _clustering.cluster(
            cell_df, ctree, coarsen_loops=coarsen_loops,
        )

        variants = {
            "n_variants": len(clustering.variants),
            "n_sequence_variants": clustering.sequence_variant_count,
            "fitness": clustering.fitness_percentage,
        }

        for variant in clustering.variants:
            for tree_id, branch_counts in (
                    variant.xor_branch_totals or {}).items():
                py_id = inverse_ids.get(tree_id)
                if py_id is None:
                    continue
                original = alias.get(py_id)
                key = id(original) if original is not None else py_id
                choice = choice_by_node.get(key)
                if choice is None:
                    continue
                counts = choice.counts[cell_index]
                if counts is None:
                    counts = [0] * len(choice.branches)
                    choice.counts[cell_index] = counts
                for branch, count in branch_counts.items():
                    if 0 <= branch < len(counts):
                        counts[branch] += int(count)

        cells.append(CellStats(
            labels=cell.labels,
            label=cell.label,
            file_stem=family.file_stem(cell),
            n_cases=cell.n_cases,
            coverage=cell.coverage,
            n_events=n_events,
            n_activities=n_activities,
            events_per_case=events_per_case,
            duration=duration,
            rework=rework,
            start_activities=start_acts,
            end_activities=end_acts,
            variants=variants,
            activity=activity_stats,
            edges=edge_stats,
        ))
        ticker.tick()
    ticker.finish()

    # Edge union, busiest handovers first (family-wide frequency
    # desc, label asc) — the natural reading order for a ranking.
    edge_totals: Dict[str, float] = {}
    for c in cells:
        for edge, entry in c.edges.items():
            edge_totals[edge] = (
                edge_totals.get(edge, 0.0) + entry.get("frequency", 0.0)
            )
    edge_names = sorted(edge_totals, key=lambda e: (-edge_totals[e], e))

    # A shared choice a cell never traversed (all its cases replayed
    # as noise, or the fork sits in a skipped region) keeps None —
    # rendered as "not reached", distinct from zero.
    return FamilyStats(
        attribute_names=[a.display_name for a in family.attributes],
        cells=cells,
        choices=choices,
        activity_names=sorted(all_activities),
        edge_names=edge_names,
        has_intervals=has_intervals,
        has_timestamps=has_timestamps,
        total_cases=family.total_cases,
        covered_cases=family.covered_cases,
        dropped_cases=family.dropped_cases,
        skipped=[
            (tuple(v.label for v in values), n)
            for values, n in family.skipped_cells
        ],
    )
