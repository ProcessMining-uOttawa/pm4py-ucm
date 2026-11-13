"""Dashboard suggestions — propose dashboards from the nature of the log.

A dashboard is a list of widget specs, so proposing one means *emitting
specs*: nothing here adds a metric, a visualisation or a second
implementation of anything. :func:`suggest_dashboards` profiles a
:class:`~.contract.FactTable` and returns a few themed dashboards whose
widgets name the activities, transitions and attributes that actually
matter in this log, each with the sentence (and the number) that
justifies it.

Deterministic, and LLM-free: every rule is a property of the log, never a
guess about its domain. The rules and their thresholds are specified in
``docs/dashboard_suggestions.md`` §2 and pinned by
``tests/test_dashboard_suggestions.py``.

Only the fact table is read — the same snapshot the browser computes on —
so a suggestion cannot name something a widget cannot bind, and on a
sampled table the reasons quote the same estimates the widgets show.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .contract import BEHAVIOUR_ATTRIBUTE, FactTable
from .engine import (
    ACTIVITY_AXIS,
    _case_of_event,
    _prev_dt,
    _variant_codes,
    aggregate,
    compute_widget,
    fmt,
    per_case_values,
    segment_keys,
)

#: An activity must hold at least this share of all elapsed time to be
#: called a bottleneck.
MIN_TIME_SHARE = 0.05
#: Share of cases a transition, a repeat or an end activity must involve
#: before a widget is spent on it.
MIN_CASE_SHARE = 0.05
#: A decision point is reached by this share of cases and has at least two
#: branches each taken by this share of the cases reaching it.
MIN_BRANCH_SHARE = 0.10
#: Fewest / most segments an axis may have to be charted.
MIN_SEGMENTS, MAX_SEGMENTS = 2, 24
#: A trend needs at least this many points.
MIN_PERIODS = 3
#: Most resources the by-resource bar is offered for (it draws the top 24
#: and says so; past this the axis is an identifier, not a grouping).
MAX_RESOURCES = 200
#: Lowest partition-advisor score at which an attribute earns a widget.
MIN_ATTRIBUTE_SCORE = 0.05
#: Variants beyond this (by frequency) share one bucket when scoring
#: attributes — the partition advisor's own bound.
_MAX_VARIANTS = 30
#: Most activities an activity × something heatmap is proposed for; past
#: this the table is a scroll, not an overview, and the bar (which keeps
#: the largest and says so) is offered alone.
MAX_HEATMAP_ROWS = 40
#: Behavioural variants a heatmap's variant axis is narrowed to — the most
#: frequent ones, by a widget filter the user can see and remove.
TOP_VARIANTS = 8
#: A suggested target's warning threshold, relative to its baseline.
_WARN_FACTOR = 1.2
#: A dashboard with fewer widgets than this is not worth proposing.
_MIN_WIDGETS = 3


# ---------------------------------------------------------------------------
# Spec helpers
# ---------------------------------------------------------------------------

def _spec(title: str, metric: str, why: str, *, agg: Optional[str] = None,
          viz: str = "kpi", params: Optional[Dict[str, Any]] = None,
          rows: Optional[str] = None, cols: Optional[str] = None,
          target: Optional[Dict[str, Any]] = None,
          filter: Optional[List[Dict[str, Any]]] = None,
          size: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """One widget spec, in the shape the composer itself writes."""
    segment: Dict[str, str] = {}
    if rows:
        segment["rows"] = rows
    if cols:
        segment["cols"] = cols
    out: Dict[str, Any] = {
        "title": title, "metric": metric, "params": dict(params or {}),
        "filter": list(filter or []), "segment": segment, "viz": viz,
        "why": why,
    }
    if size:
        out["size"] = size
    if agg:
        out["agg"] = agg
    if target:
        out["target"] = target
    return out


def _sig(value: float, digits: int = 3) -> float:
    """``value`` to ``digits`` significant figures — a baseline a person
    would type, not fifteen decimals."""
    import math

    if not value or not math.isfinite(value):
        return 0.0
    return round(value, digits - 1 - int(math.floor(math.log10(abs(value)))))


def _baseline_target(value: Optional[float]) -> Optional[Dict[str, Any]]:
    """A switched-off "no worse than today" target, or ``None``.

    Off on purpose: a target is intent, and the data cannot supply intent.
    This only saves the typing — see ``docs/dashboard_suggestions.md`` §2.3.
    """
    if value is None or value <= 0:
        return None
    return {"on": False, "dir": "<=", "value": _sig(value),
            "warn": _sig(value * _WARN_FACTOR), "mode": "aggregate"}


def _pct(share: float) -> str:
    return f"{share * 100:.0f}%"


def _heatmap_size(n_rows: int) -> Dict[str, int]:
    """Grid span for a heatmap of ``n_rows`` rows: full width, and tall
    enough to show the rows without scrolling, up to six grid rows."""
    return {"w": 4, "h": max(2, min(6, -(-(n_rows * 24 + 90) // 118)))}


def _clip(text: str, limit: int = 110) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

def _period_axis(table: FactTable) -> Tuple[Optional[str], int]:
    """``(axis, n_months)`` — the finest of month / quarter / year with a
    chartable number of segments, or ``None`` on a log too short to trend.
    """
    n_months = len(segment_keys(table, "month")[1]) if table.n_cases else 0
    for axis in ("month", "quarter", "year"):
        n = len(segment_keys(table, axis)[1]) if table.n_cases else 0
        if MIN_PERIODS <= n <= MAX_SEGMENTS:
            return axis, n_months
    return None, n_months


def _activity_profile(table: FactTable) -> Dict[str, Any]:
    """Per-activity and per-transition statistics, in one pass each."""
    import numpy as np

    n_act = len(table.activities)
    n = table.n_cases
    act = table.act_codes.astype(np.int64)
    owner = _case_of_event(table)
    dt = table.dt.astype(np.float64)
    prev = _prev_dt(table)

    # Where the log's time goes: time since the previous event, summed per
    # activity over every execution (event-weighted, on purpose).
    gap = dt - prev
    has_gap = np.isfinite(gap)
    held = np.bincount(act[has_gap], weights=gap[has_gap], minlength=n_act)
    total = float(held.sum())
    time_share = held / total if total > 0 else np.zeros(n_act)

    # Cases reaching / repeating each activity.
    pair, per_pair = np.unique(owner * n_act + act, return_counts=True)
    pair_act = pair % n_act
    reach = np.bincount(pair_act, minlength=n_act)
    repeat = np.bincount(pair_act[per_pair > 1], minlength=n_act)

    # Directly-follows steps inside one case.
    same = owner[:-1] == owner[1:] if act.size > 1 else np.zeros(0, bool)
    src, tgt = act[:-1][same], act[1:][same]
    step_owner = owner[:-1][same]
    step_time = (dt[1:] - dt[:-1])[same]
    edge = src * n_act + tgt
    edge_ids, inv = np.unique(edge, return_inverse=True)
    edge_time = (np.bincount(inv, weights=step_time, minlength=edge_ids.size)
                 / np.maximum(np.bincount(inv, minlength=edge_ids.size), 1))
    # Cases taking each edge (once per case, however often it recurs).
    case_edge = np.unique(step_owner * edge_ids.size + inv)
    edge_cases = np.bincount(case_edge % max(edge_ids.size, 1),
                             minlength=edge_ids.size)

    last = table.offsets.astype(np.int64)[1:] - 1
    ends = np.bincount(act[last], minlength=n_act) if n else np.zeros(n_act)

    return {
        "time_share": time_share, "reach": reach, "repeat": repeat,
        "edge_src": edge_ids // n_act, "edge_tgt": edge_ids % n_act,
        "edge_time": edge_time, "edge_cases": edge_cases, "ends": ends,
        # Sorted (case, edge) keys, one per case taking the edge.
        "case_edge": case_edge, "n_edges": int(edge_ids.size),
    }


def _interleaved(prof: Dict[str, Any], k: int, back: int) -> bool:
    """True when edges ``k`` (a → b) and ``back`` (b → a) look like two
    orders of the same parallel pair rather than a loop.

    A directly-follows relation cannot tell ``a ∥ b`` from ``a ⇄ b`` by
    itself — both show a → b and b → a. The cases can: in a loop the case
    that returns b → a is one that went a → b first, so the two edges share
    their cases; under concurrency each case takes one order *or* the
    other, so they barely overlap.
    """
    import numpy as np

    ce, n_edges = prof["case_edge"], prof["n_edges"]
    took = ce[ce % n_edges == k] // n_edges
    returned = ce[ce % n_edges == back] // n_edges
    if not returned.size:
        return False
    both = np.intersect1d(took, returned, assume_unique=True).size
    return both < 0.5 * returned.size


def _decision_points(table: FactTable, prof: Dict[str, Any]
                     ) -> List[Tuple[int, List[Tuple[int, float]]]]:
    """``[(activity, [(successor, share), …])]`` — activities whose flow
    genuinely splits, most-reached first, branches largest first.

    Read from the directly-follows relation because that is exactly what
    ``edgeShare`` measures. A self-loop is rework, not a decision, and a
    successor that merely interleaves with the activity (see
    :func:`_interleaved`) is concurrency, not a branch.
    """
    n = table.n_cases
    index = {(int(s), int(t)): k for k, (s, t) in
             enumerate(zip(prof["edge_src"], prof["edge_tgt"]))}
    by_src: Dict[int, List[Tuple[int, float]]] = {}
    for (s, t), k in index.items():
        reach = int(prof["reach"][s])
        if s == t or reach < MIN_BRANCH_SHARE * n:
            continue
        share = float(prof["edge_cases"][k]) / reach
        if share < MIN_BRANCH_SHARE:
            continue
        back = index.get((t, s))
        if back is not None and _interleaved(prof, k, back):
            continue  # two orders of a parallel pair, not a choice
        by_src.setdefault(s, []).append((t, share))

    def parallel(t: int, u: int) -> bool:
        fwd, back = index.get((t, u)), index.get((u, t))
        return (fwd is not None and back is not None
                and _interleaved(prof, fwd, back))

    points = []
    for s, branches in by_src.items():
        # Two successors that interleave with *each other* are the two
        # halves of a parallel split after ``s``, not alternatives.
        targets = [t for t, _ in branches]
        kept = [(t, share) for t, share in branches
                if not any(parallel(t, u) for u in targets if u != t)]
        if len(kept) >= 2:
            points.append((s, sorted(kept, key=lambda x: (-x[1], x[0]))))
    points.sort(key=lambda p: (-int(prof["reach"][p[0]]), p[0]))
    return points


def _score_attributes(table: FactTable, duration,
                      variant_codes) -> List[Dict[str, Any]]:
    """Case attributes a bar can be drawn over, best first.

    ``strong`` marks those that genuinely split the cases (score at or
    above :data:`MIN_ATTRIBUTE_SCORE`); the rest are still offered once, on
    the Segments dashboard, with a reason that says they show little — an
    attribute that does not matter is a finding too.

    Scored as the partition advisor scores them — control-flow divergence
    and duration effect size — but on the fact table's own segment keys, so
    what is scored is exactly the axis a widget would draw.
    """
    import numpy as np
    import pandas as pd

    from ..discovery.families.advisor import (
        _eta_squared,
        _uncertainty_coefficient,
    )

    if not table.attributes or table.n_cases < 2:
        return []
    variant = np.minimum(variant_codes, _MAX_VARIANTS)

    out: List[Dict[str, Any]] = []
    for a in table.attributes:
        if a.name.startswith("@@"):  # pm4py bookkeeping, not an attribute
            continue
        if a.name == BEHAVIOUR_ATTRIBUTE:
            # Derived from the trace itself, so it "explains" the variant
            # perfectly; it has a dashboard of its own (``_variants``).
            continue
        if a.name == "org:resource":
            # Case-constant in this log, so it was detected as an attribute
            # — but the ``resource`` axis already charts it.
            continue
        codes, labels = segment_keys(table, f"attr:{a.name}")
        known = codes >= 0
        if not known.any():
            continue
        counts = np.bincount(codes[known], minlength=len(labels))
        n_segments = int((counts > 0).sum())
        coverage = float(known.mean())
        balance = 1.0 - float(counts.max()) / float(counts.sum())
        if not (MIN_SEGMENTS <= n_segments <= MAX_SEGMENTS):
            continue
        if coverage < 0.5 or balance < 0.05:
            continue
        bucket = pd.Series(codes, dtype="float64").where(known)
        divergence = _uncertainty_coefficient(bucket, variant)
        effect = _eta_squared(duration, bucket)
        score = 0.6 * divergence + 0.4 * effect
        if score < MIN_ATTRIBUTE_SCORE:
            reason = (f"barely separates behaviour or duration across its "
                      f"{n_segments} values in this log — worth confirming "
                      "rather than assuming")
        elif divergence >= 0.15 and effect >= 0.1:
            reason = (f"routes behaviour (explains {_pct(divergence)} of the "
                      f"variant variation) and segments duration")
        elif divergence >= effect:
            reason = (f"routes behaviour — it explains {_pct(divergence)} of "
                      "the variant variation")
        else:
            reason = (f"segments duration — {_pct(effect)} of the duration "
                      "variance lies between its values")
        out.append({"axis": f"attr:{a.name}", "label": a.label,
                    "score": score, "reason": reason,
                    "strong": score >= MIN_ATTRIBUTE_SCORE,
                    "n_segments": n_segments})
    out.sort(key=lambda r: (-r["score"], r["label"]))
    return out


# ---------------------------------------------------------------------------
# The dashboards
# ---------------------------------------------------------------------------

def _overview(table, ctx) -> Tuple[str, List[Dict[str, Any]]]:
    import numpy as np

    dur = ctx["duration"]
    med, p90 = aggregate(dur, "median"), aggregate(dur, "p90")
    rework = ctx["rework_share"]
    period, n_months = ctx["period"], ctx["n_months"]

    specs = [
        _spec("median case duration", "duration", agg="median",
              target=_baseline_target(med),
              why=f"Half of the cases finish within {fmt(med, 'd')}."),
        _spec("P90 case duration", "duration", agg="p90",
              target=_baseline_target(p90),
              why=f"One case in ten takes longer than {fmt(p90, 'd')} — the "
                  "tail the median hides."),
        _spec("Rework rate", "rework",
              target=_baseline_target(rework * 100),
              why=f"{_pct(rework)} of cases repeat at least one activity."),
        _spec("avg events per case", "eventCount", agg="avg",
              why=f"{table.n_events:,} events over {table.n_cases:,} cases."),
    ]
    finite = dur[np.isfinite(dur)]
    if finite.size and float(finite.max()) > float(finite.min()):
        specs.append(_spec(
            "Case duration distribution", "duration", agg="median",
            viz="hist",
            why="Shows whether the duration has one typical value or "
                "several groups of cases."))
    if n_months >= MIN_PERIODS:
        specs.append(_spec(
            "Arrivals per month", "arrivalRate", agg="avg", viz="line",
            why=f"Cases start in {n_months} different months, enough to "
                "show a trend in demand."))
        specs.append(_spec(
            "Work in progress", "wip", agg="avg", viz="line",
            why="Open cases at each month boundary: a rising line means "
                "work arrives faster than it completes."))
    if period:
        specs.append(_spec(
            f"median duration by {period}", "duration", agg="median",
            viz="line", rows=period,
            why=f"Whether cases are getting faster or slower by {period} "
                "of case start."))
    else:
        specs.append(_spec(
            "median duration by weekday", "duration", agg="median",
            viz="bar", rows="weekday",
            why="The log is too short for a trend, so duration is compared "
                "by the weekday a case starts on."))
    specs.append(_spec(
        "Share of cases by activity", "actPresence", viz="bar",
        rows=ACTIVITY_AXIS,
        why=f"The whole process at a glance: how many cases reach each of "
            f"its {len(table.activities)} activities."))
    why = (f"{table.n_cases:,} cases, median {fmt(med, 'd')}, "
           f"{_pct(rework)} with rework.")
    return why, specs


def _bottlenecks(table, ctx) -> Tuple[str, List[Dict[str, Any]]]:
    import numpy as np

    prof, acts, n = ctx["prof"], table.activities, table.n_cases
    share = prof["time_share"]
    order = [int(i) for i in np.argsort(-share, kind="stable")
             if share[i] >= MIN_TIME_SHARE][:3]
    if not order:
        return "", []

    specs: List[Dict[str, Any]] = []
    for i in order:
        value = aggregate(
            per_case_values(table, "actSojourn", {"activity": acts[i]}),
            "avg")
        specs.append(_spec(
            f"avg sojourn — {acts[i]}", "actSojourn", agg="avg",
            params={"activity": acts[i]}, target=_baseline_target(value),
            why=f"{acts[i]} holds {_pct(share[i])} of all elapsed time "
                f"(summed over every execution); a case spends "
                f"{fmt(value, 'd')} reaching it on average."))

    top = acts[order[0]]
    if table.interval_log:
        for metric, word in (("actWaiting", "waiting"),
                             ("actService", "service")):
            value = aggregate(
                per_case_values(table, metric, {"activity": top}), "avg")
            if value is None:
                continue
            specs.append(_spec(
                f"avg {word} — {top}", metric, agg="avg",
                params={"activity": top},
                why=f"Splits {top}'s sojourn into queueing and work: "
                    f"{fmt(value, 'd')} of {word} on average."))

    # The whole picture, not only the top of the ranking: every activity
    # side by side, then every activity over time.
    listed = sum(share[i] for i in order)
    specs.append(_spec(
        "avg sojourn by activity", "actSojourn", agg="avg", viz="bar",
        rows=ACTIVITY_AXIS,
        why=f"Every activity side by side. The {len(order)} above hold "
            f"{_pct(listed)} of all elapsed time; this shows what the rest "
            "look like."))
    heatmap = bool(ctx["period"]) and len(acts) <= MAX_HEATMAP_ROWS
    if heatmap:
        specs.append(_spec(
            f"avg sojourn, activity × {ctx['period']}", "actSojourn",
            agg="avg", viz="table", rows=ACTIVITY_AXIS, cols=ctx["period"],
            size=_heatmap_size(len(acts)),
            why=f"Every activity by {ctx['period']}: a bottleneck that is "
                "easing or worsening shows as a row changing colour, and a "
                "new one as a row lighting up."))

    # Slowest transitions that enough cases take to matter.
    # A step into an activity already listed above would restate its
    # sojourn, so those are skipped.
    edges = [k for k in np.argsort(-prof["edge_time"], kind="stable")
             if prof["edge_cases"][k] >= MIN_CASE_SHARE * n
             and prof["edge_time"][k] > 0
             and int(prof["edge_tgt"][k]) not in order][:2]
    for k in edges:
        a, b = acts[int(prof["edge_src"][k])], acts[int(prof["edge_tgt"][k])]
        value = aggregate(
            per_case_values(table, "edgeTime", {"from": a, "to": b}), "avg")
        specs.append(_spec(
            f"avg time {a} → {b}", "edgeTime", agg="avg",
            params={"from": a, "to": b}, target=_baseline_target(value),
            why=f"Among the steps at least {_pct(MIN_CASE_SHARE)} of cases "
                f"take, one of the slowest: {fmt(value, 'd')} on average, "
                f"taken by {_pct(prof['edge_cases'][k] / n)} of cases."))

    if ctx["period"] and not heatmap:
        # Too many activities for the heatmap: trend the main one instead.
        specs.append(_spec(
            f"sojourn of {top} by {ctx['period']}", "actSojourn", agg="avg",
            viz="line", params={"activity": top}, rows=ctx["period"],
            why=f"Whether the main bottleneck is easing or worsening by "
                f"{ctx['period']}."))
    strong = [a for a in ctx["attributes"] if a["strong"]]
    if strong:
        best = strong[0]
        specs.append(_spec(
            f"sojourn of {top} by {best['label']}", "actSojourn", agg="avg",
            viz="bar", params={"activity": top}, rows=best["axis"],
            why=f"{best['label']} {best['reason']}, so the bottleneck may "
                "not hit every group alike."))
    why = f"{top} holds {_pct(share[order[0]])} of all elapsed time."
    return why, specs


def _rework_and_flow(table, ctx) -> Tuple[str, List[Dict[str, Any]]]:
    import numpy as np

    prof, acts, n = ctx["prof"], table.activities, table.n_cases
    rework = ctx["rework_share"]
    specs: List[Dict[str, Any]] = []
    reasons: List[str] = []

    if rework >= MIN_CASE_SHARE:
        reasons.append(f"{_pct(rework)} of cases repeat an activity")
        specs.append(_spec(
            "Rework rate", "rework", target=_baseline_target(rework * 100),
            why=f"{_pct(rework)} of cases repeat at least one activity."))
        repeated = [int(i) for i in np.argsort(-prof["repeat"], kind="stable")
                    if prof["repeat"][i] >= MIN_CASE_SHARE * n][:2]
        for i in repeated:
            # ``actRework``, not ``actRepeats``: an average of 0.3 repeats
            # displays as "0", while a rate reads. Its denominator is the
            # cases performing the activity, so the reason gives both.
            redone = prof["repeat"][i] / max(int(prof["reach"][i]), 1)
            specs.append(_spec(
                f"Rework rate — {acts[i]}", "actRework",
                params={"activity": acts[i]},
                target=_baseline_target(redone * 100),
                why=f"{acts[i]} is redone in {_pct(redone)} of the cases "
                    f"that perform it — {_pct(prof['repeat'][i] / n)} of "
                    "all cases."))

        # Then every activity, not only the two above: where rework
        # happens, and whether that is moving.
        specs.append(_spec(
            "Rework rate by activity", "actRework", viz="bar",
            rows=ACTIVITY_AXIS,
            why="Every activity side by side: the share of the cases "
                "performing it that perform it more than once."))
        heatmap = len(acts) <= MAX_HEATMAP_ROWS and (
            ctx["period"] or [a for a in ctx["attributes"] if a["strong"]])
        if heatmap and ctx["period"]:
            specs.append(_spec(
                f"Rework rate, activity × {ctx['period']}", "actRework",
                viz="table", rows=ACTIVITY_AXIS, cols=ctx["period"],
                size=_heatmap_size(len(acts)),
                why=f"Every activity by {ctx['period']}: rework that is "
                    "spreading or receding shows as a row changing "
                    "colour."))
        elif heatmap:
            best = [a for a in ctx["attributes"] if a["strong"]][0]
            specs.append(_spec(
                f"Rework rate, activity × {best['label']}", "actRework",
                viz="table", rows=ACTIVITY_AXIS, cols=best["axis"],
                size=_heatmap_size(len(acts)),
                why=f"The log is too short for a trend, so rework is "
                    f"compared across {best['label']}, which "
                    f"{best['reason']}."))

    # How cases end comes before how they branch: with the widget budget
    # spent in this order, the outcomes are never the part cut off.
    ends = [int(i) for i in np.argsort(-prof["ends"], kind="stable")
            if prof["ends"][i] >= MIN_CASE_SHARE * n]
    if len(ends) >= 2:
        reasons.append(f"cases end in {len(ends)} different activities")
        for i in ends[:3]:
            specs.append(_spec(
                f"Cases reaching {acts[i]}", "actPresence",
                params={"activity": acts[i]},
                why=f"{_pct(prof['ends'][i] / n)} of cases end with "
                    f"{acts[i]}, one of {len(ends)} common outcomes."))

    points = ctx["decision_points"]
    if points:
        reasons.append(f"the flow splits after {acts[points[0][0]]}")
    budget = 4
    for src, branches in points[:2]:
        for tgt, share in branches[:2]:
            if budget == 0:
                break
            budget -= 1
            specs.append(_spec(
                f"{acts[src]} → {acts[tgt]}", "edgeShare",
                params={"from": acts[src], "to": acts[tgt]},
                why=f"{acts[src]} is a decision point "
                    f"({len(branches)} branches): {_pct(share)} of the "
                    f"cases reaching it go straight to {acts[tgt]}."))

    if rework >= MIN_CASE_SHARE and ctx["period"] \
            and len(acts) > MAX_HEATMAP_ROWS:
        # Too many activities for the heatmap: trend the overall rate.
        specs.append(_spec(
            f"Rework rate by {ctx['period']}", "rework", viz="line",
            rows=ctx["period"],
            why=f"Whether rework is rising or falling by {ctx['period']}."))

    why = (reasons[0][0].upper() + reasons[0][1:] + "." if reasons else "")
    return why, specs


def _segments(table, ctx) -> Tuple[str, List[Dict[str, Any]]]:
    import numpy as np

    specs: List[Dict[str, Any]] = []
    attrs = ctx["attributes"]
    for a in attrs[:2]:
        specs.append(_spec(
            f"median duration by {a['label']}", "duration", agg="median",
            viz="bar", rows=a["axis"],
            why=f"{a['label']} {a['reason']}."))
    attrs = [a for a in attrs if a["strong"]]
    if attrs and ctx["rework_share"] >= MIN_CASE_SHARE:
        specs.append(_spec(
            f"Rework rate by {attrs[0]['label']}", "rework", viz="bar",
            rows=attrs[0]["axis"],
            why=f"Whether the {_pct(ctx['rework_share'])} of cases with "
                f"rework concentrate in some values of "
                f"{attrs[0]['label']}."))
    if attrs and len(table.activities) <= MAX_HEATMAP_ROWS:
        specs.append(_spec(
            f"Share of cases, activity × {attrs[0]['label']}", "actPresence",
            viz="table", rows=ACTIVITY_AXIS, cols=attrs[0]["axis"],
            size=_heatmap_size(len(table.activities)),
            why=f"How the path itself differs across {attrs[0]['label']}: "
                "an activity one group performs and another skips is a row "
                "that changes colour."))
    if attrs and ctx["period"]:
        specs.append(_spec(
            f"avg duration, {attrs[0]['label']} × {ctx['period']}",
            "duration", agg="avg", viz="table", rows=attrs[0]["axis"],
            cols=ctx["period"],
            why=f"Whether the differences across {attrs[0]['label']} are "
                "stable over time."))

    res_codes, _ = segment_keys(table, "resource")
    n_res = int(np.unique(res_codes[res_codes >= 0]).size)
    if MIN_SEGMENTS <= n_res <= MAX_RESOURCES:
        specs.append(_spec(
            "median duration by resource", "duration", agg="median",
            viz="bar", rows="resource",
            why=f"{n_res} resources open cases; a case is attributed to "
                "the resource of its first event."))
    n_var = ctx["n_variants"]
    # With behavioural variants available the Variants dashboard covers
    # this better; the raw sequence variants are the fallback.
    if n_var >= 2 and table.attribute(BEHAVIOUR_ATTRIBUTE) is None:
        specs.append(_spec(
            "median duration by variant", "duration", agg="median",
            viz="bar", rows="variant",
            why=f"The log has {n_var:,} sequence variants; v1 is the most "
                "common path."))

    if attrs:
        why = f"{attrs[0]['label']} {attrs[0]['reason']}."
    elif ctx["attributes"]:
        why = ("No case attribute clearly separates the cases; the closest "
               f"is {ctx['attributes'][0]['label']}.")
    elif MIN_SEGMENTS <= n_res <= MAX_RESOURCES:
        why = f"{n_res} resources open cases."
    else:
        why = f"The log has {n_var:,} variants."
    return why, specs


def _variants(table, ctx) -> Tuple[str, List[Dict[str, Any]]]:
    """Dashboards over the **behavioural** variants — the concurrency-aware
    clustering, in which every interleaving of parallel activities is one
    variant. Proposed only when the fact table carries them (see
    ``build_fact_table(behaviour=…)``)."""
    import numpy as np

    attr = table.attribute(BEHAVIOUR_ATTRIBUTE)
    if attr is None:
        return "", []
    axis = f"attr:{BEHAVIOUR_ATTRIBUTE}"
    codes, labels = segment_keys(table, axis)
    fit = codes >= 0
    counts = np.bincount(codes[fit], minlength=len(labels))
    order = [int(k) for k in np.argsort(-counts, kind="stable")
             if counts[k] > 0]
    if len(order) < MIN_SEGMENTS:
        return "", []
    n, n_fit, n_seq = table.n_cases, int(fit.sum()), ctx["n_variants"]
    first = labels[order[0]]
    top3 = sum(int(counts[k]) for k in order[:3])

    # A heatmap with a column per variant is narrowed to the most frequent
    # ones — by a filter on the widget, where the user can see and lift it.
    top = [labels[k] for k in order[:TOP_VARIANTS]]
    narrowed = len(order) > TOP_VARIANTS
    only_top = ([{"field": axis, "op": "in", "value": top}]
                if narrowed else [])
    top_share = sum(int(counts[k]) for k in order[:TOP_VARIANTS]) / n
    scope = (f"the {TOP_VARIANTS} most frequent variants "
             f"({_pct(top_share)} of cases)" if narrowed
             else f"all {len(order)} variants")

    expr = attr.descriptions.get(first, "")
    specs = [
        _spec("Cases by behavioural variant", "custom", agg="sum",
              viz="pie", params={"formula": "1"}, rows=axis,
              why=f"{first} covers {_pct(counts[order[0]] / n)} of cases and "
                  f"the top three {_pct(top3 / n)}."
                  + (f" {first} is {_clip(expr)}." if expr else "")),
        _spec("median duration by behavioural variant", "duration",
              agg="median", viz="bar", rows=axis,
              why="Which ways of running the process are slow — compared "
                  "by behaviour, so two orders of the same parallel work "
                  "are not two bars."),
    ]
    if ctx["rework_share"] >= MIN_CASE_SHARE:
        specs.append(_spec(
            "Rework rate by behavioural variant", "rework", viz="bar",
            rows=axis,
            why=f"Which variants the {_pct(ctx['rework_share'])} of cases "
                "with rework belong to."))
    if len(table.activities) <= MAX_HEATMAP_ROWS:
        specs.append(_spec(
            "Share of cases, activity × behavioural variant", "actPresence",
            viz="table", rows=ACTIVITY_AXIS, cols=axis, filter=only_top,
            size=_heatmap_size(len(table.activities)),
            why=f"What each variant consists of: which activities occur in "
                f"{scope}. Hover a column for its expression."))
    if ctx["period"]:
        specs.append(_spec(
            f"Cases, behavioural variant × {ctx['period']}", "custom",
            agg="sum", viz="table", params={"formula": "1"}, rows=axis,
            cols=ctx["period"], filter=only_top,
            why=f"Whether the mix of behaviours is shifting by "
                f"{ctx['period']}, over {scope}."))
    strong = [a for a in ctx["attributes"] if a["strong"]]
    if strong:
        specs.append(_spec(
            f"Cases, behavioural variant × {strong[0]['label']}", "custom",
            agg="sum", viz="table", params={"formula": "1"}, rows=axis,
            cols=strong[0]["axis"], filter=only_top,
            why=f"{strong[0]['label']} {strong[0]['reason']}: this shows "
                f"which variants each of its values follows, over {scope}."))

    why = (f"{n_seq:,} activity sequences fold into {len(order):,} "
           f"behavioural variants once interleavings of parallel activities "
           f"count as one"
           + ("." if n_fit == n else
              f"; {_pct(n_fit / n)} of cases fit the model and have one."))
    return why, specs


def _draws(table: FactTable, spec: Dict[str, Any]) -> bool:
    """Whether a segmented widget has enough segments to say anything.

    The rules above pick an axis from the *log's* shape; a widget's own
    metric can still leave it nearly empty — the sojourn of an activity
    only one channel performs has one bar "by channel". So every chart is
    computed once and kept only if it draws: two bars, three points on a
    line, a table at least 2 × 2.
    """
    if spec["viz"] not in ("bar", "line", "table", "pie"):
        return True
    try:
        w = compute_widget(spec, table)
    except Exception:
        return False
    if spec["viz"] == "table":
        return len(w.get("rows") or []) >= 2 and len(w.get("cols") or []) >= 2
    need = MIN_PERIODS if spec["viz"] == "line" else MIN_SEGMENTS
    return len(w.get("series") or []) >= need


_DASHBOARDS = (
    ("overview", "Overview", _overview),
    ("bottlenecks", "Bottlenecks", _bottlenecks),
    ("rework", "Rework and flow", _rework_and_flow),
    ("segments", "Segments", _segments),
    ("variants", "Behavioural variants", _variants),
)


def suggest_dashboards(table: FactTable, *, max_widgets: int = 10
                       ) -> List[Dict[str, Any]]:
    """Dashboards worth opening on this log, most general first.

    Each is ``{"key", "name", "why", "specs", "filters"}`` — ``specs`` are
    ordinary widget specs (``docs/dashboards.md`` §8) carrying one extra
    field, ``why``: the sentence that justifies the widget. Hand ``specs``
    to :func:`~.view.dashboard_html` to open on one, or the whole list as
    its ``suggestions`` to offer them in the page.

    The Overview is always proposed (on a non-empty log); the others only
    when the log gives them at least three widgets, and *Behavioural
    variants* only when the table was built with a variant clustering
    (``build_fact_table(..., behaviour=…)``). ``max_widgets``
    truncates each dashboard, most important widget first.

    Deterministic: the same table always yields the same list.
    """
    import numpy as np

    if table.n_cases == 0:
        return []

    duration = per_case_values(table, "duration")
    variant_codes = _variant_codes(table)[0]
    period, n_months = _period_axis(table)
    prof = _activity_profile(table)
    ctx = {
        "duration": duration,
        "rework_share": (aggregate(per_case_values(table, "rework"),
                                   "share") or 0.0) / 100.0,
        "period": period, "n_months": n_months,
        "prof": prof,
        "decision_points": _decision_points(table, prof),
        "attributes": _score_attributes(table, duration, variant_codes),
        "n_variants": int(np.unique(variant_codes).size),
    }

    out: List[Dict[str, Any]] = []
    for key, name, build in _DASHBOARDS:
        why, specs = build(table, ctx)
        specs = [s for s in specs if _draws(table, s)][:max(0, max_widgets)]
        if key != "overview" and len(specs) < _MIN_WIDGETS:
            continue
        for i, spec in enumerate(specs):
            spec["id"] = f"s-{key}-{i}"
        out.append({"key": key, "name": name, "why": why,
                    "specs": specs, "filters": []})
    return out
