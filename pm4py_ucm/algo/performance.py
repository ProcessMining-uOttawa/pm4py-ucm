"""Performance overlays: frequency and time metrics on a UCM.

Computes per-activity and per-edge statistics from the event log and
attaches them to the model as ``_perf`` metadata, which the classic
visualizer renders as a small gray annotation under activity names and
on edges, and the exporter writes as jUCMNav ``<metadata>`` entries
(visible in the editor's properties view).

Available metrics
-----------------

Activities (:data:`NODE_METRICS`):

* ``frequency`` — number of executions (events);
* ``case_coverage`` — number of cases containing the activity;
* ``mean_time`` / ``median_time`` / ``total_time`` — activity service
  time. Only derivable from *interval* logs (a ``start_timestamp``
  column alongside the completion timestamp); silently omitted for
  single-timestamp logs. (:class:`PerformanceStats` additionally
  carries min/max and per-activity *sojourn* times for the family
  statistics reports — those extra entries are not overlay metrics.)

Edges (:data:`EDGE_METRICS`):

* ``frequency`` — traversals of the activity-to-activity segment the
  edge belongs to (directly-follows count);
* ``percentage`` — the segment's share of its OR-fork's outgoing
  traversals (only on arcs leaving an OR-fork);
* ``mean_time`` / ``median_time`` / ``total_time`` — waiting time
  between the segment's two activities (completion to completion for
  single-timestamp logs; completion to start for interval logs).

Edge statistics are attributed via *segments*: every arc is walked
backwards and forwards through routing bends, empty points and joins to
the nearest activity on each side; the annotation lands on the
segment's first arc (leaving the activity or the fork). Arcs whose
segment is ambiguous (crosses another fork or a stub) are left
unannotated rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..objects.ucm.obj import UCM


#: Metadata key carrying the rendered overlay text.
PERF_KEY = "_perf"

NODE_METRICS = (
    "frequency", "case_coverage",
    "mean_time", "median_time", "total_time",
)
EDGE_METRICS = (
    "frequency", "percentage",
    "mean_time", "median_time", "total_time",
)


@dataclass
class PerformanceStats:
    """Log statistics keyed by activity name and activity pair."""

    #: ``{activity: {"frequency", "case_coverage", "mean_time",
    #: "median_time", "min_time", "max_time", "total_time",
    #: "sojourn_mean_time", ...}}``. The plain ``*_time`` entries are
    #: service times, only derivable from interval logs; the
    #: ``sojourn_*_time`` entries are the time since the case's
    #: **previous event** (completion to completion ≈ waiting +
    #: service), derivable from any timestamped log. The extra keys
    #: feed the family statistics reports; they are not part of
    #: :data:`NODE_METRICS`, so the overlay/metadata output is
    #: unchanged by their presence.
    activity: Dict[str, Dict[str, float]] = field(default_factory=dict)
    #: ``{(a, b): {"frequency", "mean_time", "median_time",
    #: "min_time", "max_time", "total_time"}}`` for directly-follows
    #: pairs — waiting times (completion → start for interval logs,
    #: completion → completion otherwise).
    pairs: Dict[Tuple[str, str], Dict[str, float]] = field(
        default_factory=dict)
    total_cases: int = 0


def compute_performance_stats(
    log_df,
    *,
    case_id_col: str = "case:concept:name",
    activity_col: str = "concept:name",
    timestamp_col: str = "time:timestamp",
    start_timestamp_col: str = "start_timestamp",
) -> PerformanceStats:
    """Compute :class:`PerformanceStats` from an event-log DataFrame."""
    import pandas as pd

    stats = PerformanceStats(
        total_cases=int(log_df[case_id_col].nunique()),
    )

    activities = log_df[activity_col].astype(str)
    freq = activities.value_counts()
    coverage = log_df.groupby(activities)[case_id_col].nunique()
    for name in freq.index:
        stats.activity[str(name)] = {
            "frequency": int(freq[name]),
            "case_coverage": int(coverage.get(name, 0)),
        }

    # Activity service time — interval logs only.
    has_intervals = (
        start_timestamp_col in log_df.columns
        and timestamp_col in log_df.columns
    )
    if has_intervals:
        duration = (
            pd.to_datetime(log_df[timestamp_col])
            - pd.to_datetime(log_df[start_timestamp_col])
        ).dt.total_seconds()
        grouped = duration.groupby(activities)
        for name, entry in (
                ("mean", grouped.mean()),
                ("median", grouped.median()),
                ("min", grouped.min()),
                ("max", grouped.max()),
                ("total", grouped.sum())):
            for act, value in entry.items():
                if str(act) in stats.activity and value == value:
                    stats.activity[str(act)][f"{name}_time"] = float(value)

    # Directly-follows pairs with waiting time (completion of the
    # first to start — or completion — of the second).
    ordered = log_df.sort_values([case_id_col, timestamp_col])
    case = ordered[case_id_col]
    act = ordered[activity_col].astype(str)
    end_ts = pd.to_datetime(ordered[timestamp_col])
    next_start = pd.to_datetime(
        ordered[start_timestamp_col] if has_intervals
        else ordered[timestamp_col]
    ).shift(-1)
    same_case = case.eq(case.shift(-1))
    pdf = pd.DataFrame({
        "a": act,
        "b": act.shift(-1),
        "delta": (next_start - end_ts).dt.total_seconds(),
    })[same_case.values]
    for (a, b), g in pdf.groupby(["a", "b"], sort=True):
        stats.pairs[(str(a), str(b))] = {
            "frequency": int(len(g)),
            "mean_time": float(g["delta"].mean()),
            "median_time": float(g["delta"].median()),
            "min_time": float(g["delta"].min()),
            "max_time": float(g["delta"].max()),
            "total_time": float(g["delta"].sum()),
        }

    # Per-activity SOJOURN time: completion minus the case's previous
    # completion (≈ waiting + service attributed to the activity).
    # Unlike service times this needs no start_timestamp, so
    # single-timestamp logs get activity-level time statistics too.
    # A case's first event has no predecessor and is excluded.
    sojourn = pd.DataFrame({
        "act": act,
        "delta": (end_ts - end_ts.shift(1)).dt.total_seconds(),
    })[case.eq(case.shift(1)).values]
    for name, g in sojourn.groupby("act", sort=True):
        entry = stats.activity.get(str(name))
        if entry is None:
            continue
        entry["sojourn_mean_time"] = float(g["delta"].mean())
        entry["sojourn_median_time"] = float(g["delta"].median())
        entry["sojourn_min_time"] = float(g["delta"].min())
        entry["sojourn_max_time"] = float(g["delta"].max())
        entry["sojourn_total_time"] = float(g["delta"].sum())
    return stats


# ---------------------------------------------------------------------------
# Display formatting
# ---------------------------------------------------------------------------

def _format_duration(seconds: Optional[float]) -> Optional[str]:
    if seconds is None or seconds != seconds:
        return None
    s = abs(float(seconds))
    sign = "-" if seconds < 0 else ""
    if s < 60:
        return f"{sign}{s:.0f}s"
    if s < 3600:
        return f"{sign}{s / 60:.1f}m"
    if s < 86400:
        return f"{sign}{s / 3600:.1f}h"
    days = s / 86400
    if days >= 500:
        return f"{sign}{days / 365.25:.1f}y"
    return f"{sign}{days:.1f}d"


_TIME_PREFIX = {"mean_time": "avg", "median_time": "med",
                "total_time": "sum"}


def _node_text(entry: Dict[str, float],
               metrics: Sequence[str]) -> Optional[str]:
    parts: List[str] = []
    for m in metrics:
        if m == "frequency":
            parts.append(f"n={entry['frequency']}")
        elif m == "case_coverage":
            parts.append(f"{entry['case_coverage']} cases")
        elif m in _TIME_PREFIX and m in entry:
            text = _format_duration(entry.get(m))
            if text:
                parts.append(f"{_TIME_PREFIX[m]} {text}")
    return " · ".join(parts) or None


def _edge_text(entry: Dict[str, float], metrics: Sequence[str],
               percentage: Optional[float]) -> Optional[str]:
    parts: List[str] = []
    for m in metrics:
        if m == "frequency":
            parts.append(str(int(entry["frequency"])))
        elif m == "percentage" and percentage is not None:
            parts.append(f"{percentage * 100:.0f}%")
        elif m in _TIME_PREFIX and m in entry:
            text = _format_duration(entry.get(m))
            if text:
                parts.append(f"{_TIME_PREFIX[m]} {text}")
    return " · ".join(parts) or None


# ---------------------------------------------------------------------------
# Segment resolution
# ---------------------------------------------------------------------------

_ROUTING_TYPES = (UCM.EmptyPoint, UCM.DirectionArrow)


class _SegmentResolver:
    """Collects the activities reachable on each side of an arc.

    The walks are transparent to routing bends, empty points, joins
    (backward: **all** predecessor branches are explored), forks
    (forward: **all** outgoing branches are explored), and static
    single-binding stubs (the walk follows the plug-in binding into
    and out of the sub-map, so decomposed models get edge statistics
    across stub boundaries). Dynamic / multi-binding stubs stop that
    branch of the walk.

    The result is a *set* of activities per side; the edge's
    statistics are the aggregate of the directly-follows pairs over
    the two sets — this is what lets an OR-fork that sits right after
    a join (the common case) still receive branch statistics."""

    def __init__(self, ucm: UCM) -> None:
        # Plug-in boundary indexes: which binding a plug-in
        # StartPoint / EndPoint belongs to.
        self._start_binding: Dict[int, Any] = {}
        self._end_binding: Dict[int, Any] = {}
        for m in ucm.maps:
            for n in m.nodes:
                if not isinstance(n, UCM.Stub):
                    continue
                for b in n.bindings:
                    for ib in b.in_bindings:
                        if ib.start_point is not None:
                            self._start_binding[id(ib.start_point)] = b
                    for ob in b.out_bindings:
                        if ob.end_point is not None:
                            self._end_binding[id(ob.end_point)] = b

    @staticmethod
    def _static_binding(stub: "UCM.Stub"):
        if stub.dynamic or len(stub.bindings) != 1:
            return None
        return stub.bindings[0]

    def next_activities(self, arc: "UCM.NodeConnection") -> set:
        """Activities reachable forward of ``arc`` before any other
        activity is executed."""
        out: set = set()
        seen: set = set()
        stack = [arc.target]
        while stack:
            node = stack.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            if isinstance(node, UCM.RespRef):
                out.add(node.resp_def.name if node.resp_def else node.name)
                continue
            if isinstance(node, UCM.Stub):
                binding = self._static_binding(node)
                if binding is not None and binding.in_bindings:
                    stack.append(binding.in_bindings[0].start_point)
                continue
            if isinstance(node, UCM.EndPoint):
                binding = self._end_binding.get(id(node))
                if binding is not None and len(binding.out_bindings) == 1:
                    exit_arc = binding.out_bindings[0].stub_exit
                    if exit_arc is not None:
                        stack.append(exit_arc.target)
                continue  # a real (root) end point
            # StartPoints reached forward only via stub-follow;
            # forks fan out; joins pass through.
            stack.extend(s.target for s in node.succ_connections)
        return out

    def prev_activities(self, arc: "UCM.NodeConnection") -> set:
        """Activities reachable backward of ``arc`` (symmetric)."""
        out: set = set()
        seen: set = set()
        stack = [arc.source]
        while stack:
            node = stack.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            if isinstance(node, UCM.RespRef):
                out.add(node.resp_def.name if node.resp_def else node.name)
                continue
            if isinstance(node, UCM.Stub):
                binding = self._static_binding(node)
                if binding is not None and binding.out_bindings:
                    stack.append(binding.out_bindings[0].end_point)
                continue
            if isinstance(node, UCM.StartPoint):
                binding = self._start_binding.get(id(node))
                if binding is not None and len(binding.in_bindings) == 1:
                    entry_arc = binding.in_bindings[0].stub_entry
                    if entry_arc is not None:
                        stack.append(entry_arc.source)
                continue  # a real (root) start point
            # EndPoints reached backward only via stub-follow;
            # joins fan out; forks pass through.
            stack.extend(p.source for p in node.pred_connections)
        return out


def _aggregate_pair_stats(
    stats: PerformanceStats, prevs: set, nexts: set,
) -> Optional[Dict[str, float]]:
    """Combine the directly-follows statistics of every observed
    ``(prev, next)`` pair. Frequencies and totals add; the mean is
    frequency-weighted; the median is only kept when the segment is a
    single unambiguous pair (medians do not combine)."""
    entries = [
        stats.pairs[(a, b)]
        for a in sorted(prevs) for b in sorted(nexts)
        if (a, b) in stats.pairs
    ]
    if not entries:
        return None
    frequency = sum(e["frequency"] for e in entries)
    out: Dict[str, float] = {
        "frequency": frequency,
        "mean_time": sum(
            e["mean_time"] * e["frequency"] for e in entries
        ) / frequency,
        "total_time": sum(e["total_time"] for e in entries),
    }
    if len(entries) == 1:
        out["median_time"] = entries[0]["median_time"]
    return out


# ---------------------------------------------------------------------------
# Annotation
# ---------------------------------------------------------------------------

def _is_perf_metadata(name: str) -> bool:
    return (name == PERF_KEY or name.startswith(PERF_KEY + "_")
            or name.startswith("perf_"))


def _strip_perf(maps) -> None:
    for m in maps:
        for n in m.nodes:
            n._metadata = [md for md in n.metadata
                           if not _is_perf_metadata(md.name)]
        for c in m.connections:
            c._metadata = [md for md in c.metadata
                           if not _is_perf_metadata(md.name)]


def _metric_value(metric: str, entry: Dict[str, float],
                  percentage: Optional[float] = None) -> Optional[str]:
    """One metric as a human-readable metadata value."""
    if metric == "frequency" and "frequency" in entry:
        return str(int(entry["frequency"]))
    if metric == "case_coverage" and "case_coverage" in entry:
        return str(int(entry["case_coverage"]))
    if metric == "percentage":
        return f"{percentage * 100:.0f}%" if percentage is not None else None
    if metric in _TIME_PREFIX and metric in entry:
        return _format_duration(entry.get(metric))
    return None


def _add_metric_metadata(element, metrics: Sequence[str],
                         entry: Dict[str, float],
                         percentage: Optional[float] = None) -> None:
    """Every available metric as its own ``perf_<metric>`` metadata
    entry — one per line in jUCMNav's properties view."""
    for metric in metrics:
        value = _metric_value(metric, entry, percentage)
        if value is not None:
            element.add_metadata(f"perf_{metric}", value)


def annotate_performance(
    ucm: UCM,
    log=None,
    node_metrics: Sequence[str] = ("frequency",),
    edge_metrics: Sequence[str] = ("frequency",),
    stats: Optional[PerformanceStats] = None,
    parameters: Optional[Dict[str, Any]] = None,
    maps: Optional[Sequence["UCM.UCMmap"]] = None,
) -> UCM:
    """Attach performance overlay metadata to ``ucm`` from ``log``.

    ``node_metrics`` / ``edge_metrics`` pick what is shown (up to two
    each keeps the diagram readable — see :data:`NODE_METRICS` /
    :data:`EDGE_METRICS`; pass empty sequences to disable a layer).
    Two metadata layers are attached to RespRefs and connections:

    * ``perf_<metric>`` — **every** available metric, one entry per
      line, independent of the display selection; the ``.jucm``
      exporter writes them so jUCMNav's properties view lists them
      line by line;
    * ``_perf`` — the display string for the *selected* metrics,
      rendered by the classic visualizer as a small gray annotation
      under activity names and on edges.

    ``maps`` restricts annotation (and the replace-on-reannotate
    stripping) to a subset of the model's maps — used by the family
    assemblers, where different maps carry statistics from different
    sub-logs. Re-annotating replaces any previous overlay on the
    touched maps. Returns ``ucm``."""
    for m in node_metrics:
        if m not in NODE_METRICS:
            raise ValueError(
                f"Unknown node metric {m!r}; expected one of {NODE_METRICS}")
    for m in edge_metrics:
        if m not in EDGE_METRICS:
            raise ValueError(
                f"Unknown edge metric {m!r}; expected one of {EDGE_METRICS}")

    if stats is None:
        if log is None:
            raise ValueError("annotate_performance needs a log or stats")
        params = dict(parameters or {})
        stats = compute_performance_stats(
            log,
            case_id_col=params.get("case_id_col", "case:concept:name"),
            activity_col=params.get("activity_col", "concept:name"),
            timestamp_col=params.get("timestamp_col", "time:timestamp"),
            start_timestamp_col=params.get(
                "start_timestamp_col", "start_timestamp"),
        )

    target_maps = list(maps) if maps is not None else list(ucm.maps)
    _strip_perf(target_maps)
    resolver = _SegmentResolver(ucm)

    for m in target_maps:
        for n in m.nodes:
            if not isinstance(n, UCM.RespRef) or n.resp_def is None:
                continue
            entry = stats.activity.get(n.resp_def.name)
            if not entry:
                continue
            # Every available metric as its own metadata line; the
            # display overlay carries only the selected ones.
            _add_metric_metadata(n, NODE_METRICS, entry)
            if node_metrics:
                text = _node_text(entry, node_metrics)
                if text:
                    n.add_metadata(PERF_KEY, text)

        for c in m.connections:
            # One annotation per segment: on its first arc. jUCMNav's
            # metamodel has no metadata on connections, so the edge
            # annotation lives on the arc's SOURCE node under a
            # branch-indexed key (``_perf_branch<i>`` for the i-th
            # outgoing arc) — the visualizer and the .jucm round-trip
            # both read it from there.
            if isinstance(c.source, _ROUTING_TYPES):
                continue
            prevs = resolver.prev_activities(c)
            if not prevs:
                continue
            entry = _aggregate_pair_stats(
                stats, prevs, resolver.next_activities(c),
            )
            if entry is None:
                continue
            percentage = None
            if isinstance(c.source, UCM.OrFork):
                total = 0
                for sibling in c.source.succ_connections:
                    s_entry = _aggregate_pair_stats(
                        stats, prevs, resolver.next_activities(sibling),
                    )
                    if s_entry:
                        total += s_entry["frequency"]
                if total:
                    percentage = entry["frequency"] / total
            try:
                branch = c.source.succ_connections.index(c)
            except ValueError:
                continue
            for metric in EDGE_METRICS:
                value = _metric_value(metric, entry, percentage)
                if value is not None:
                    c.source.add_metadata(
                        f"perf_branch{branch}_{metric}", value,
                    )
            if edge_metrics:
                text = _edge_text(entry, edge_metrics, percentage)
                if text:
                    c.source.add_metadata(
                        f"{PERF_KEY}_branch{branch}", text,
                    )
    return ucm
