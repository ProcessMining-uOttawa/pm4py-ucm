"""The metric catalog — what a dashboard widget can measure.

Each entry declares its parameters, its result type and the aggregations
that make sense for it. The catalog is data, not code: it is serialised
into the client payload so the widget composer's metric list, the
parameter controls it shows, and the aggregation pills it enables are all
driven from one definition rather than from parallel Python and JS lists
that drift.

The computation model
---------------------
Every metric here is **per case, then aggregated**. A widget computes one
value per case, filters cases, groups them by the segmentation axes, and
aggregates within each group. That model is what makes reader-side
filtering possible at all — a filter is a case mask, and every number on
the dashboard falls out of re-aggregating the surviving cases.

It also has a consequence worth being explicit about. The activity time
metrics here are *case-weighted*: a case that ran ``Send Fine`` three
times contributes the mean of its three service times, once. The
performance **overlays** on the model
(:mod:`pm4py_ucm.algo.performance`) are *event-weighted*: that same case
contributes three times. The two agree whenever an activity occurs at
most once per case, and diverge on rework. Neither is wrong; they answer
different questions ("how long does this activity take *for a case*" vs
"how long does an execution of this activity take"). See
:data:`WEIGHTING_NOTE`.

Result types
------------
``time``
    Duration in days. Aggregations: avg / median / p90 / min / max.
``count``
    A cardinality. Aggregations: avg / median / p90 / sum / min / max.
``percent``
    A per-case 0/1 indicator aggregated with ``share`` into a
    percentage. No other aggregation applies.
``rate``
    Events per unit time; produced as a series, not a per-case value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

#: Surfaced in the composer next to activity time metrics, and in the
#: docs, so the difference from the model overlays is never a surprise.
WEIGHTING_NOTE = (
    "Case-weighted: each case contributes one value (the mean over its "
    "own occurrences). The model overlay is event-weighted, so the two "
    "differ for activities that repeat within a case."
)

#: Aggregations by result type — drives which pills the composer enables.
#:
#: ``sum`` is offered for times as well as counts: a total duration is a
#: real quantity ("28 case-years spent in Assess"), and it is what a pie
#: needs — slices only add to a whole when the aggregation is additive. It
#: stays off ``rate``, where a sum of rates means nothing.
AGGS_BY_TYPE: Dict[str, Tuple[str, ...]] = {
    "time": ("avg", "median", "p90", "sum", "min", "max"),
    "count": ("avg", "median", "p90", "sum", "min", "max"),
    "percent": ("share",),
    "rate": ("avg", "median", "p90", "max"),
}

# Which visualisation a widget takes is not a property of the metric: it
# depends on the segmentation arity, the aggregation, the kind of axis and
# whether a target is set. So the guards live in the composer (``syncViz``
# in ``assets/dash-ui.js``), specified in ``docs/dashboards.md`` §6 — there
# is deliberately no per-result-type viz list here.


@dataclass
class Param:
    """One parameter of a metric.

    ``kind`` tells the composer which control to render:
    ``"activity"`` — a picker over the log's activity dictionary;
    ``"attribute"`` — a picker over the log's case attributes.
    """

    name: str
    kind: str
    label: str
    optional: bool = False

    def to_json(self) -> Dict[str, object]:
        return {"name": self.name, "kind": self.kind,
                "label": self.label, "optional": self.optional}


@dataclass
class MetricSpec:
    """A catalog entry."""

    #: Stable id used in a widget spec's ``metric`` field.
    id: str
    #: ``"process"``, ``"activity"`` or ``"edge"`` — the composer's tabs.
    level: str
    label: str
    #: ``"time"``, ``"count"``, ``"percent"`` or ``"rate"``.
    result_type: str
    params: List[Param] = field(default_factory=list)
    #: Aggregation offered by default when a widget picks this metric.
    default_agg: str = "avg"
    #: One-line explanation shown in the composer.
    help: str = ""
    #: True when the metric is only derivable from an interval log (a
    #: ``start_timestamp`` column). The composer greys these out with a
    #: reason rather than silently returning nothing.
    needs_interval: bool = False
    #: True when the per-case value is case-weighted in a way that
    #: diverges from the event-weighted model overlay.
    case_weighted: bool = False

    @property
    def aggs(self) -> Tuple[str, ...]:
        return AGGS_BY_TYPE[self.result_type]

    def to_json(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "id": self.id,
            "level": self.level,
            "label": self.label,
            "resultType": self.result_type,
            "params": [p.to_json() for p in self.params],
            "aggs": list(self.aggs),
            "defaultAgg": self.default_agg,
            "help": self.help,
            "needsInterval": self.needs_interval,
        }
        if self.case_weighted:
            out["weightingNote"] = WEIGHTING_NOTE
        return out


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------

CATALOG: Tuple[MetricSpec, ...] = (
    # -- process ---------------------------------------------------------
    MetricSpec(
        id="duration", level="process", label="Case duration",
        result_type="time", default_agg="avg",
        help="First to last event of the case.",
    ),
    MetricSpec(
        id="timeBetween", level="process",
        label="Time between two activities", result_type="time",
        params=[Param("from", "activity", "from"),
                Param("to", "activity", "to")],
        default_agg="avg",
        help="From the first 'from' to the first 'to' that follows it. "
             "Cases where 'to' never follows 'from' contribute no value "
             "rather than a zero, so they leave the denominator instead "
             "of dragging the average down.",
    ),
    MetricSpec(
        id="wip", level="process", label="Work in progress (WIP)",
        result_type="rate", default_agg="avg",
        help="Cases open at each month boundary.",
    ),
    MetricSpec(
        id="arrivalRate", level="process", label="Arrival rate",
        result_type="rate", default_agg="avg",
        help="Cases starting per month.",
    ),
    MetricSpec(
        id="completionRate", level="process", label="Completion rate",
        result_type="rate", default_agg="avg",
        help="Cases ending per month.",
    ),
    MetricSpec(
        id="eventCount", level="process", label="Events per case",
        result_type="count", default_agg="avg",
    ),
    MetricSpec(
        id="rework", level="process", label="Rework rate",
        result_type="percent", default_agg="share",
        help="Share of cases in which any activity occurs more than once.",
    ),
    # -- activity --------------------------------------------------------
    MetricSpec(
        id="actFreq", level="activity", label="Activity frequency",
        result_type="count",
        params=[Param("activity", "activity", "activity")],
        default_agg="avg",
        help="Occurrences of the activity within a case.",
    ),
    MetricSpec(
        id="actPresence", level="activity", label="Activity presence",
        result_type="percent",
        params=[Param("activity", "activity", "activity")],
        default_agg="share",
        help="Share of cases containing the activity at least once.",
    ),
    MetricSpec(
        id="actRepeats", level="activity", label="Repeats per case",
        result_type="count",
        params=[Param("activity", "activity", "activity")],
        default_agg="avg",
        help="Occurrences beyond the first — 0 when the activity runs "
             "once, and for cases that skip it entirely.",
    ),
    MetricSpec(
        id="actSojourn", level="activity", label="Sojourn time",
        result_type="time",
        params=[Param("activity", "activity", "activity")],
        default_agg="avg", case_weighted=True,
        help="Time since the case's previous event (waiting + service).",
    ),
    MetricSpec(
        id="actService", level="activity", label="Service time",
        result_type="time",
        params=[Param("activity", "activity", "activity")],
        default_agg="avg", needs_interval=True, case_weighted=True,
        help="Start to completion of the activity.",
    ),
    MetricSpec(
        id="actWaiting", level="activity", label="Waiting time",
        result_type="time",
        params=[Param("activity", "activity", "activity")],
        default_agg="avg", needs_interval=True, case_weighted=True,
        help="Previous event's completion to this activity's start.",
    ),
    # -- edge ------------------------------------------------------------
    MetricSpec(
        id="edgeFreq", level="edge", label="Transition frequency",
        result_type="count",
        params=[Param("from", "activity", "from"),
                Param("to", "activity", "to")],
        default_agg="avg",
        help="Directly-follows traversals of from → to within a case.",
    ),
    MetricSpec(
        id="edgeTime", level="edge", label="Transition time",
        result_type="time",
        params=[Param("from", "activity", "from"),
                Param("to", "activity", "to")],
        default_agg="avg", case_weighted=True,
        help="Elapsed time across the directly-follows step from → to.",
    ),
    MetricSpec(
        id="edgeShare", level="edge", label="Branch probability",
        result_type="percent",
        params=[Param("from", "activity", "from"),
                Param("to", "activity", "to")],
        default_agg="share",
        help="Share of cases that take from → to among those reaching "
             "'from' at all — the branch probability at that fork.",
    ),
)

BY_ID: Dict[str, MetricSpec] = {m.id: m for m in CATALOG}


def catalog_json(*, interval_log: bool) -> List[Dict[str, object]]:
    """The catalog as the client payload sees it.

    ``interval_log`` does not filter the list — entries stay visible so
    the composer can explain *why* a metric is unavailable instead of
    leaving a hole where a user expects service time to be.
    """
    return [
        {**m.to_json(),
         "available": bool(interval_log or not m.needs_interval),
         "unavailableReason": (
             "" if interval_log or not m.needs_interval
             else "Needs an interval log (a start_timestamp column "
                  "alongside the completion timestamp)."
         )}
        for m in CATALOG
    ]
