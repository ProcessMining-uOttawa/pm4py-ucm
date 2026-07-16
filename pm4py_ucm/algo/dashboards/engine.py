"""The dashboard metrics engine — per-case values, filters, segmentation,
aggregation and target scoring.

This is the Python half of a two-implementation engine: the JS half in
``web/assets/dash-engine.js`` computes the same numbers in the browser
from the same :mod:`~pm4py_ucm.algo.dashboards.contract` payload. The
Python half exists to compute exports and previews server-side, to be
the thing the test suite pins, and to be the reference the JS half is
checked against.

Everything here is vectorised over the fact table's CSR event arrays;
nothing iterates cases in Python. A widget on a 100 k-event log
recomputes in milliseconds, which is what lets the composer preview live
and the reader's filter bar feel instant.

Percentile convention
---------------------
``median`` / ``p90`` use **linear interpolation** — numpy's and pandas'
default, and the convention :doc:`docs/metrics.md </metrics>` pins for
the whole package. Note this deliberately differs from the design
prototype's ``pm-engine.js``, which used nearest-rank
(``v[floor(n*0.9)]``). Matching the package matters more than matching
the prototype: a dashboard median that disagreed with the same log's
model overlay and family report would be a bug report, not a design
choice. The JS engine implements the interpolating definition too.

Missing values
--------------
A per-case value of ``NaN`` means *this case has no value for this
metric* — the ``to`` activity never followed the ``from``, the activity
never occurred, the log has no start timestamps. Missing cases are
excluded from aggregation rather than counted as zero; a case that never
appealed has no appeal-to-decision time, and averaging a zero in would
be a fabricated number. ``share`` is the exception by construction: it
counts non-missing cases and asks what fraction are non-zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .catalog import BY_ID
from .contract import FactTable

#: Seconds in a day — every ``time`` metric is reported in days, the unit
#: the design's KPI values and targets are written in.
DAY = 86400.0

#: Aggregations the engine implements.
AGGS = ("avg", "median", "p90", "sum", "min", "max", "share")


# ---------------------------------------------------------------------------
# Structure helpers
# ---------------------------------------------------------------------------

def _counts(table: FactTable):
    import numpy as np

    off = table.offsets.astype(np.int64)
    return np.diff(off)


def _case_of_event(table: FactTable):
    """``case_of_event[j]`` — which case event *j* belongs to."""
    import numpy as np

    return np.repeat(np.arange(table.n_cases, dtype=np.int64),
                     _counts(table))


def _act_code(table: FactTable, name: str) -> int:
    try:
        return table.activities.index(name)
    except ValueError:
        return -1


def _first_occurrence(table: FactTable, code: int):
    """``[n_cases]`` event index of each case's first occurrence of
    ``code``, or ``-1``. Relies on events being grouped by case and
    ordered by time, which :func:`~.contract.build_fact_table` guarantees.
    """
    import numpy as np

    out = np.full(table.n_cases, -1, dtype=np.int64)
    if code < 0:
        return out
    hits = np.flatnonzero(table.act_codes == code)
    if not hits.size:
        return out
    owner = _case_of_event(table)[hits]
    # `hits` ascends and events are grouped by case, so `owner` ascends;
    # np.unique's first index per value is therefore the first occurrence.
    uniq, first = np.unique(owner, return_index=True)
    out[uniq] = hits[first]
    return out


def _first_following(table: FactTable, after, code: int):
    """``[n_cases]`` event index of the first ``code`` occurring **after**
    event ``after[i]`` in the same case, or ``-1``.

    Ordering is decided by event *index*, never by comparing timestamps.
    The fact table stores whole seconds, so two genuinely ordered events
    can share a timestamp (ClaimsPaymentLog has sub-second transitions);
    a timestamp comparison would then read as "did not happen" and drop
    the case from the metric entirely, rather than reporting the ~0
    duration that actually occurred. Index order is exact because
    :func:`~.contract.build_fact_table` sorts events by time.
    """
    import numpy as np

    out = np.full(table.n_cases, -1, dtype=np.int64)
    if code < 0:
        return out
    hits = np.flatnonzero(table.act_codes == code)
    if not hits.size:
        return out
    owner = _case_of_event(table)[hits]
    has = after >= 0
    if not np.any(has):
        return out
    # `hits` ascends globally and events are grouped by case, so the first
    # hit strictly after `after[i]` is in case i iff it lies before the
    # end of case i's own run of hits.
    pos = np.searchsorted(hits, after[has], side="right")
    end = np.searchsorted(owner, np.arange(table.n_cases), side="right")
    ok = pos < end[has]
    idx = np.flatnonzero(has)[ok]
    out[idx] = hits[pos[ok]]
    return out


def _per_case_mean(table: FactTable, event_mask, values):
    """Mean of ``values`` over the events selected by ``event_mask``,
    per case; ``NaN`` for cases with no selected event.

    This is where the case-weighting documented in
    :data:`~.catalog.WEIGHTING_NOTE` happens: a case with three
    occurrences of an activity collapses to one value here, before any
    cross-case aggregation.
    """
    import numpy as np

    out = np.full(table.n_cases, np.nan, dtype=np.float64)
    if not np.any(event_mask):
        return out
    owner = _case_of_event(table)[event_mask]
    vals = np.asarray(values, dtype=np.float64)[event_mask]
    good = np.isfinite(vals)
    if not np.any(good):
        return out
    owner, vals = owner[good], vals[good]
    total = np.bincount(owner, weights=vals, minlength=table.n_cases)
    n = np.bincount(owner, minlength=table.n_cases)
    hit = n > 0
    out[hit] = total[hit] / n[hit]
    return out


def _prev_dt(table: FactTable):
    """``[n_events]`` completion time of the previous event in the same
    case, ``NaN`` at each case's first event."""
    import numpy as np

    dt = table.dt.astype(np.float64)
    prev = np.full(dt.shape, np.nan)
    prev[1:] = dt[:-1]
    first = table.offsets[:-1].astype(np.int64)
    prev[first] = np.nan
    return prev


def _sdt(table: FactTable):
    """``[n_events]`` start-time delta, ``NaN`` where absent/invalid."""
    import numpy as np

    if "sdt" not in table.buffers:
        return None
    raw = table.buffers["sdt"]
    out = raw.astype(np.float64)
    out[raw == np.iinfo(raw.dtype).max] = np.nan
    return out


def _df_steps(table: FactTable, src: int, tgt: int):
    """Boolean ``[n_events]`` mask of events that are the *source* of a
    directly-follows ``src → tgt`` step inside one case."""
    import numpy as np

    mask = np.zeros(table.n_events, dtype=bool)
    if src < 0 or tgt < 0 or table.n_events < 2:
        return mask
    act = table.act_codes
    owner = _case_of_event(table)
    same_case = np.zeros(table.n_events, dtype=bool)
    same_case[:-1] = owner[:-1] == owner[1:]
    mask[:-1] = (act[:-1] == src) & (act[1:] == tgt) & same_case[:-1]
    return mask


# ---------------------------------------------------------------------------
# Per-case values
# ---------------------------------------------------------------------------

def per_case_values(table: FactTable, metric: str,
                    params: Optional[Dict[str, Any]] = None):
    """``[n_cases]`` float array of the metric's per-case value.

    ``NaN`` marks a case with no value; see the module docstring.
    ``time`` metrics are in days, ``percent`` metrics are 0/1 indicators
    that :func:`aggregate` turns into a percentage via ``share``.
    """
    import numpy as np

    p = params or {}
    n = table.n_cases
    off = table.offsets.astype(np.int64)
    dt = table.dt.astype(np.float64)

    if metric == "custom":
        return custom_values(table, p.get("formula", ""))

    if metric == "duration":
        # Events are time-ordered, so the case's last dt is its span.
        last = off[1:] - 1
        return dt[last] / DAY

    if metric == "eventCount":
        return _counts(table).astype(np.float64)

    if metric == "timeBetween":
        a = _first_occurrence(table, _act_code(table, p.get("from", "")))
        b = _first_following(table, a, _act_code(table, p.get("to", "")))
        out = np.full(n, np.nan)
        ok = (a >= 0) & (b >= 0)
        if np.any(ok):
            # b follows a in event order, so the gap cannot be negative.
            out[ok] = (dt[b[ok]] - dt[a[ok]]) / DAY
        return out

    if metric == "rework":
        return _rework(table)

    if metric in ("actFreq", "actPresence", "actRepeats"):
        code = _act_code(table, p.get("activity", ""))
        if code < 0:
            return np.zeros(n) if metric != "actRepeats" else np.zeros(n)
        owner = _case_of_event(table)[table.act_codes == code]
        freq = np.bincount(owner, minlength=n).astype(np.float64)
        if metric == "actFreq":
            return freq
        if metric == "actPresence":
            return (freq > 0).astype(np.float64)
        return np.maximum(freq - 1.0, 0.0)

    if metric == "actSojourn":
        code = _act_code(table, p.get("activity", ""))
        mask = (table.act_codes == code) & np.isfinite(_prev_dt(table))
        return _per_case_mean(table, mask, (dt - _prev_dt(table)) / DAY)

    if metric == "actService":
        sdt = _sdt(table)
        if sdt is None:
            return np.full(n, np.nan)
        code = _act_code(table, p.get("activity", ""))
        mask = (table.act_codes == code) & np.isfinite(sdt)
        return _per_case_mean(table, mask, (dt - sdt) / DAY)

    if metric == "actWaiting":
        sdt = _sdt(table)
        if sdt is None:
            return np.full(n, np.nan)
        code = _act_code(table, p.get("activity", ""))
        prev = _prev_dt(table)
        mask = (table.act_codes == code) & np.isfinite(sdt) & np.isfinite(prev)
        # Negative waiting means the activity started before the previous
        # event finished — real concurrency, kept rather than clamped.
        return _per_case_mean(table, mask, (sdt - prev) / DAY)

    if metric in ("edgeFreq", "edgeTime", "edgeShare"):
        src = _act_code(table, p.get("from", ""))
        tgt = _act_code(table, p.get("to", ""))
        steps = _df_steps(table, src, tgt)
        if metric == "edgeFreq":
            owner = _case_of_event(table)[steps]
            return np.bincount(owner, minlength=n).astype(np.float64)
        if metric == "edgeTime":
            nxt = np.full(table.n_events, np.nan)
            nxt[:-1] = dt[1:]
            return _per_case_mean(table, steps, (nxt - dt) / DAY)
        # edgeShare — among cases that reach 'from', did they take this
        # branch? Cases that never reach 'from' are not in the
        # denominator, so they are missing rather than 0.
        reached = _first_occurrence(table, src) >= 0
        owner = _case_of_event(table)[steps]
        took = np.bincount(owner, minlength=n) > 0
        out = np.full(n, np.nan)
        out[reached] = took[reached].astype(np.float64)
        return out

    if metric in ("wip", "arrivalRate", "completionRate"):
        raise ValueError(
            f"{metric!r} is a series metric — call series_values(), not "
            "per_case_values()."
        )

    raise ValueError(f"Unknown metric {metric!r}")


def _formula_base(table: FactTable) -> Dict[str, Any]:
    """The per-case primitives the ƒ grammar's functions resolve to.

    Each returns an ``[n_cases]`` array. They are the *same* computations
    the catalog metrics use, routed through :func:`per_case_values`, so a
    formula and a catalog widget can never disagree about what
    ``duration()`` or ``count("X")`` means.
    """
    import numpy as np

    def timestamp(act: str):
        first = _first_occurrence(table, _act_code(table, act))
        out = np.full(table.n_cases, np.nan)
        ok = first >= 0
        # Absolute epoch seconds of the first occurrence.
        starts = table.starts.astype(np.float64)
        owner = _case_of_event(table)
        out[ok] = starts[ok] + table.dt.astype(np.float64)[first[ok]]
        return out

    def attr(name: str):
        values, spec = case_attribute_values(table, name)
        if values is None or spec is None or spec.type != "integer":
            # Only numeric attributes are values; a categorical one is
            # filtered with the widget's Filter row, not read here.
            return np.full(table.n_cases, np.nan)
        return np.asarray(values, dtype=np.float64)

    return {
        "duration": lambda: per_case_values(table, "duration"),
        "contains": lambda a: per_case_values(table, "actPresence",
                                              {"activity": a}),
        "count": lambda a: per_case_values(table, "actFreq", {"activity": a}),
        "time_between": lambda a, b: per_case_values(
            table, "timeBetween", {"from": a, "to": b}),
        "timestamp": timestamp,
        "attr": attr,
    }


def custom_values(table: FactTable, formula: str):
    """``[n_cases]`` value array for a ƒ custom-metric formula.

    Parse errors surface as an all-``NaN`` result rather than an
    exception, so a widget carrying a broken formula renders as empty
    ("—") instead of taking the dashboard down — the composer is where a
    formula is validated before it is ever saved.
    """
    import numpy as np

    from .formula import FormulaError, evaluate, parse

    try:
        ast = parse(formula)
        return evaluate(ast, _formula_base(table), table.n_cases)
    except (FormulaError, KeyError, ValueError):
        return np.full(table.n_cases, np.nan)


def _rework(table: FactTable):
    """1.0 where any activity occurs more than once in the case, else 0.

    Sorting ``case*K + activity`` puts a case's repeated activities
    adjacent, so a zero first difference marks a repeat — one sort
    instead of a per-case set.
    """
    import numpy as np

    n = table.n_cases
    if table.n_events == 0:
        return np.zeros(n)
    k = max(1, len(table.activities))
    key = _case_of_event(table) * k + table.act_codes.astype(np.int64)
    key = np.sort(key)
    dup = np.flatnonzero(np.diff(key) == 0)
    out = np.zeros(n, dtype=np.float64)
    if dup.size:
        out[np.unique(key[dup + 1] // k)] = 1.0
    return out


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def percentile(values, q: float) -> Optional[float]:
    """Linear-interpolation percentile — see the module docstring.

    Written out rather than delegated to ``np.percentile`` so the JS
    engine can mirror it line for line.
    """
    import numpy as np

    v = np.sort(np.asarray(values, dtype=np.float64))
    n = v.size
    if n == 0:
        return None
    if n == 1:
        return float(v[0])
    pos = q * (n - 1)
    lo = int(np.floor(pos))
    hi = int(np.ceil(pos))
    if lo == hi:
        return float(v[lo])
    return float(v[lo] + (v[hi] - v[lo]) * (pos - lo))


def aggregate(values, kind: str) -> Optional[float]:
    """Aggregate per-case values, ignoring missing ones.

    Returns ``None`` — not 0 — when nothing survives, so an empty segment
    renders as an em-dash instead of a confident zero.
    """
    import numpy as np

    v = np.asarray(values, dtype=np.float64)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return None
    if kind == "share":
        return float(100.0 * np.count_nonzero(v) / v.size)
    if kind == "sum":
        return float(v.sum())
    if kind == "min":
        return float(v.min())
    if kind == "max":
        return float(v.max())
    if kind == "median":
        return percentile(v, 0.5)
    if kind == "p90":
        return percentile(v, 0.9)
    if kind == "avg":
        return float(v.mean())
    raise ValueError(f"Unknown aggregation {kind!r}; expected one of {AGGS}")


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

def target_state(value: Optional[float],
                 target: Optional[Dict[str, Any]]) -> Optional[str]:
    """``"met"`` / ``"risk"`` / ``"missed"``, or ``None`` when there is no
    target or no value.

    ``warn`` is the threshold between *at risk* and *missed*; it sits on
    the far side of ``value`` (a ``<=`` target with value 14 and warn 18
    reads "meet 14, tolerate 18, miss beyond").
    """
    if value is None or not target or not target.get("on"):
        return None
    goal = float(target["value"])
    warn = float(target.get("warn", goal))
    if target.get("dir") == ">=":
        if value >= goal:
            return "met"
        return "risk" if value >= warn else "missed"
    if value <= goal:
        return "met"
    return "risk" if value <= warn else "missed"


#: Label and colours per target state — the single source the Streamlit
#: view, the export and the scorecard all read, so a state can never be
#: garnet in one place and red in another.
STATE_UI: Dict[str, Dict[str, str]] = {
    "met": {"label": "MET", "bg": "#e6f4ec", "fg": "#166b42"},
    "risk": {"label": "AT RISK", "bg": "#fdf3d7", "fg": "#8a6d00"},
    "missed": {"label": "MISSED", "bg": "#fbeaec", "fg": "#8f001a"},
}

#: Worst-first, for rolling a segmented widget up to one state.
_STATE_ORDER = ("missed", "risk", "met")


def worst_state(states: Sequence[Optional[str]]) -> Optional[str]:
    """The worst state present — how a segmented widget rolls up.

    A heatmap of 40 segments with one breach is a breach; reporting its
    average state would hide exactly the cell the reader needs.
    """
    present = [s for s in states if s]
    for s in _STATE_ORDER:
        if s in present:
            return s
    return None


# ---------------------------------------------------------------------------
# Case-level derived columns
# ---------------------------------------------------------------------------

def case_resource(table: FactTable):
    """``[n_cases]`` resource code of each case's **first** event, or the
    missing sentinel.

    A case has many resources; a segmentation axis needs one. The first
    event's resource — whoever opened the case — is the deterministic,
    explicable choice, and it is what the ``resource`` axis is labelled
    after. Attributing a case to the resource of the *metric's* activity
    would be more faithful for activity-scoped metrics but would make the
    axis mean different things in different widgets.
    """
    import numpy as np

    if "res" not in table.buffers:
        return None
    res = table.buffers["res"]
    return res[table.offsets[:-1].astype(np.int64)]


def case_attribute_values(table: FactTable, name: str):
    """``(values, attribute)`` for a case attribute, or ``(None, None)``."""
    attr = table.attribute(name)
    if attr is None or attr.buffer not in table.buffers:
        return None, None
    return table.buffers[attr.buffer], attr


def _case_end(table: FactTable):
    import numpy as np

    off = table.offsets.astype(np.int64)
    return table.starts.astype(np.int64) + table.dt.astype(np.int64)[off[1:] - 1]


def _dt64(seconds):
    import numpy as np

    return np.asarray(seconds, dtype="int64").astype("datetime64[s]")


def _variant_codes(table: FactTable):
    """``(codes, labels, sequences)`` — one code per case, labels ranked
    by case count so ``v1`` is always the most common path."""
    import numpy as np

    off = table.offsets.astype(np.int64)
    act = table.act_codes
    keys = [act[off[i]:off[i + 1]].tobytes() for i in range(table.n_cases)]
    order: Dict[bytes, int] = {}
    counts: Dict[bytes, int] = {}
    for k in keys:
        counts[k] = counts.get(k, 0) + 1
    ranked = sorted(counts, key=lambda k: (-counts[k], k))
    order = {k: i for i, k in enumerate(ranked)}
    codes = np.fromiter((order[k] for k in keys), dtype=np.int64,
                        count=len(keys))
    labels = [f"v{i + 1}" for i in range(len(ranked))]
    seqs = [
        " → ".join(table.activities[c] for c in
                   np.frombuffer(k, dtype=act.dtype))
        for k in ranked
    ]
    return codes, labels, seqs


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

#: Time axes derivable from a case's start, with no log support needed.
TIME_AXES = ("year", "quarter", "month", "weekday")

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def segment_axes(table: FactTable) -> List[Dict[str, str]]:
    """Segmentation axes this log actually supports, for the composer."""
    axes = [{"id": a, "label": a.capitalize(), "group": "time"}
            for a in TIME_AXES]
    if "res" in table.buffers:
        axes.append({"id": "resource", "label": "Resource (first event)",
                     "group": "log"})
    axes.append({"id": "variant", "label": "Variant", "group": "log"})
    for a in table.attributes:
        axes.append({"id": f"attr:{a.name}", "label": a.label,
                     "group": "attribute"})
    return axes


def segment_keys(table: FactTable, axis: str):
    """``(codes, labels)`` — ``codes[i]`` indexes ``labels`` for case *i*,
    or ``-1`` when the case has no value on this axis.

    Label order is the order segments appear on screen: chronological for
    time axes, the attribute's own bin/enum order for attributes,
    frequency-ranked for variants.
    """
    import numpy as np

    if not axis or axis == "none":
        return np.zeros(table.n_cases, dtype=np.int64), ["all"]

    start = _dt64(table.starts)

    if axis == "year":
        years = start.astype("datetime64[Y]").astype(int) + 1970
        labels = [str(y) for y in np.unique(years)]
        lut = {int(l): i for i, l in enumerate(labels)}
        return np.array([lut[int(y)] for y in years], dtype=np.int64), labels

    if axis == "month":
        months = start.astype("datetime64[M]")
        uniq = np.unique(months)
        labels = [str(m) for m in uniq]  # numpy renders as "2011-01"
        codes = np.searchsorted(uniq, months)
        return codes.astype(np.int64), labels

    if axis == "quarter":
        m = start.astype("datetime64[M]").astype(int)
        y, mo = 1970 + m // 12, m % 12
        q = y * 4 + mo // 3
        uniq = np.unique(q)
        labels = [f"{int(v) // 4}-Q{int(v) % 4 + 1}" for v in uniq]
        return np.searchsorted(uniq, q).astype(np.int64), labels

    if axis == "weekday":
        # Epoch day 0 (1970-01-01) was a Thursday, which is index 3 in a
        # Monday-first week — hence +3. (The familiar +4 is for
        # Sunday-first weeks; using it here would label every case with
        # the following day.)
        days = start.astype("datetime64[D]").astype(np.int64)
        return ((days + 3) % 7).astype(np.int64), list(_WEEKDAYS)

    if axis == "resource":
        res = case_resource(table)
        if res is None:
            return np.full(table.n_cases, -1, dtype=np.int64), []
        codes = res.astype(np.int64)
        codes[res == np.iinfo(res.dtype).max] = -1
        return codes, list(table.resources)

    if axis == "variant":
        codes, labels, _ = _variant_codes(table)
        return codes, labels

    if axis.startswith("attr:"):
        values, attr = case_attribute_values(table, axis[len("attr:"):])
        if values is None:
            return np.full(table.n_cases, -1, dtype=np.int64), []
        if attr.type == "integer":
            if not attr.bins:
                return np.full(table.n_cases, -1, dtype=np.int64), []
            # Bins are [lo, hi) with the last closed on both ends — the
            # family partitioner's convention, so a dashboard segmented
            # by an attribute matches a family grid partitioned on it.
            edges = np.array([b.lo for b in attr.bins] +
                             [attr.bins[-1].hi], dtype=np.float64)
            codes = np.digitize(values, edges[1:-1], right=False)
            codes = np.where(np.isfinite(values), codes, -1).astype(np.int64)
            out_of_range = np.isfinite(values) & (
                (values < edges[0]) | (values > edges[-1]))
            codes[out_of_range] = -1
            return codes, [b.label for b in attr.bins]
        codes = values.astype(np.int64)
        codes[values == np.iinfo(values.dtype).max] = -1
        return codes, list(attr.values)

    raise ValueError(f"Unknown segmentation axis {axis!r}")


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def apply_filters(table: FactTable,
                  filters: Optional[Sequence[Dict[str, Any]]]):
    """``[n_cases]`` boolean mask of the cases surviving ``filters``.

    Filters AND together. Each is ``{"field", "op", "value"}``:

    ``field="contains"``
        ``op="is"`` / ``"not"``, ``value`` an activity name — does the
        case contain it.
    ``field="date"``
        ``op="between"``, ``value`` a ``[from, to]`` pair of ISO dates
        (either may be null) matched against the case **start**.
    ``field="resource"``
        ``op="is"`` / ``"in"`` / ``"not"`` over the first-event resource.
    ``field="attr:<name>"``
        ``op`` in ``is / not / in / > / >= / < / <= / between``.
    ``field="segment"``
        ``op="is"``, ``value`` ``[axis, label]`` — the drill-down filter a
        table cell click produces.
    """
    import numpy as np

    mask = np.ones(table.n_cases, dtype=bool)
    for f in filters or []:
        mask &= _one_filter(table, f)
    return mask


def _one_filter(table: FactTable, f: Dict[str, Any]):
    import numpy as np

    field = f.get("field", "")
    op = f.get("op", "is")
    value = f.get("value")
    n = table.n_cases

    if field == "contains":
        code = _act_code(table, value)
        has = _first_occurrence(table, code) >= 0
        return ~has if op == "not" else has

    if field == "date":
        lo, hi = (value or [None, None])[:2]
        start = _dt64(table.starts)
        m = np.ones(n, dtype=bool)
        if lo:
            m &= start >= np.datetime64(str(lo)).astype("datetime64[s]")
        if hi:
            # Inclusive of the whole final period: "2012-12" means through
            # the end of December, not midnight on the 1st.
            end = (np.datetime64(str(hi)) + 1).astype("datetime64[s]")
            m &= start < end
        return m

    if field == "resource":
        res = case_resource(table)
        if res is None:
            return np.ones(n, dtype=bool)
        wanted = value if isinstance(value, (list, tuple)) else [value]
        codes = [table.resources.index(v) for v in wanted
                 if v in table.resources]
        m = np.isin(res, codes)
        return ~m if op == "not" else m

    if field == "segment":
        axis, label = value[0], value[1]
        codes, labels = segment_keys(table, axis)
        if label not in labels:
            return np.zeros(n, dtype=bool)
        m = codes == labels.index(label)
        return ~m if op == "not" else m

    if field.startswith("attr:"):
        values, attr = case_attribute_values(table, field[len("attr:"):])
        if values is None:
            return np.ones(n, dtype=bool)
        if attr.type == "integer":
            v = values.astype(np.float64)
            known = np.isfinite(v)
            if op == "between":
                lo, hi = float(value[0]), float(value[1])
                return known & (v >= lo) & (v <= hi)
            x = float(value)
            m = {">": v > x, ">=": v >= x, "<": v < x, "<=": v <= x,
                 "is": v == x, "not": v != x}.get(op)
            if m is None:
                raise ValueError(f"Bad op {op!r} for numeric attribute")
            return known & m
        wanted = value if isinstance(value, (list, tuple)) else [value]
        codes = [attr.values.index(str(v)) for v in wanted
                 if str(v) in attr.values]
        m = np.isin(values, codes)
        return ~m if op == "not" else m

    raise ValueError(f"Unknown filter field {field!r}")


# ---------------------------------------------------------------------------
# Series metrics
# ---------------------------------------------------------------------------

#: Metrics that are a time series over the log, not a per-case value.
SERIES_METRICS = ("wip", "arrivalRate", "completionRate")


def series_values(table: FactTable, metric: str,
                  mask=None) -> List[Dict[str, Any]]:
    """``[{"label": "2011-01", "value": n}]`` at monthly resolution.

    WIP counts cases *open at* each month boundary — started, not yet
    ended — so it is a stock, while the arrival and completion rates are
    flows counted *within* each month. A case with no end (its last event
    is not a terminal activity) is still open as far as the log knows;
    the log cannot distinguish "still running" from "ended silently", and
    this counts it open through its last event only.
    """
    import numpy as np

    if metric not in SERIES_METRICS:
        raise ValueError(f"{metric!r} is not a series metric")

    start = table.starts.astype(np.int64)
    end = _case_end(table)
    if mask is not None:
        start, end = start[mask], end[mask]
    if start.size == 0:
        return []

    lo = _dt64([start.min()])[0].astype("datetime64[M]")
    hi = _dt64([end.max()])[0].astype("datetime64[M]")
    months = np.arange(lo, hi + 1, dtype="datetime64[M]")
    bounds = months.astype("datetime64[s]").astype(np.int64)

    if metric == "wip":
        # Cases straddling each boundary: started at or before it, ended
        # after it. Both edges via searchsorted on sorted copies.
        s_sorted = np.sort(start)
        e_sorted = np.sort(end)
        started = np.searchsorted(s_sorted, bounds, side="right")
        ended = np.searchsorted(e_sorted, bounds, side="right")
        values = started - ended
    else:
        which = start if metric == "arrivalRate" else end
        edges = np.append(bounds,
                          (months[-1] + 1).astype("datetime64[s]"
                                                  ).astype(np.int64))
        values = np.histogram(which, bins=edges)[0]

    return [{"label": str(m), "value": int(v)}
            for m, v in zip(months, values)]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

#: Display unit per result type.
UNIT_BY_TYPE = {"time": "d", "percent": "%", "count": "n", "rate": "n"}


def round_half_away(value: float, digits: int = 0) -> float:
    """Round half away from zero — 5.25 → 5.3, 1234.5 → 1235.

    Neither language's default will do here. Python's ``round`` and
    ``%.1f`` round half to *even* (5.25 → 5.2); JS's ``toFixed`` rounds
    half *up* (5.25 → 5.3). Left to their defaults the two engines print
    different numbers for the same value, and a KPI reading 5.2 in the app
    and 5.3 in the export is indistinguishable from a bug.

    So both implement this rule explicitly instead. Because IEEE 754
    doubles behave identically in Python and JS, ``floor(abs(v)*m + 0.5)``
    agrees bit for bit across the two — including on the values where
    binary representation makes "half" not actually half (1.005 rounds
    *down* at 2 digits in both, because it is really 1.00499…).
    """
    import math

    if value is None or not math.isfinite(value):
        return value
    m = 10.0 ** digits
    return math.copysign(math.floor(abs(value) * m + 0.5) / m, value)


def fmt_days(value: float) -> str:
    """A duration given in DAYS, shown in the largest unit that keeps it
    legible: days when ≥ 1 d, else hours, else minutes, else seconds — so
    a short duration reads "2.4 h" or "43 m", not "0.0 d". Days lose their
    decimal past 100 (nobody reads "142.3 d"); seconds are whole (the
    contract stores whole seconds). Mirrored by ``fmtDays`` in
    ``dash-engine.js`` — see :func:`round_half_away`."""
    a = abs(value)
    if a >= 1.0:
        if a >= 100:
            return f"{int(round_half_away(value)):,} d"
        return f"{round_half_away(value, 1):.1f} d"
    if a * 24.0 >= 1.0:
        return f"{round_half_away(value * 24.0, 1):.1f} h"
    if a * 1440.0 >= 1.0:
        return f"{round_half_away(value * 1440.0, 1):.1f} m"
    return f"{int(round_half_away(value * 86400.0)):,} s"


def fmt(value: Optional[float], unit: str) -> str:
    """Format a value the way the design's KPI and table cells read.

    Durations adapt their unit (see :func:`fmt_days`), percentages keep
    one decimal, counts get thousands separators. Mirrored by ``fmt`` in
    ``dash-engine.js`` — see :func:`round_half_away`.
    """
    if value is None:
        return "—"
    if unit == "%":
        return f"{round_half_away(value, 1):.1f}%"
    if unit == "d":
        return fmt_days(value)
    return f"{int(round_half_away(value)):,}"


# ---------------------------------------------------------------------------
# Widget computation
# ---------------------------------------------------------------------------

#: Default aggregation for a ƒ custom metric, by inferred result type.
_CUSTOM_DEFAULT_AGG = {"percent": "share", "time": "avg", "count": "avg"}


def _resolve_metric(spec: Dict[str, Any]):
    """The metric a widget measures — a catalog entry, or a synthetic one
    for a ƒ custom formula whose result type is inferred from the AST.

    ``compute_widget`` only reads ``result_type``, ``default_agg`` and
    ``label``, so the synthetic object carries just those.
    """
    from types import SimpleNamespace

    metric_id = spec.get("metric", "")
    if metric_id == "custom":
        from .formula import compile_formula

        formula = (spec.get("params") or {}).get("formula", "")
        rtype = compile_formula(formula)["resultType"] or "count"
        return SimpleNamespace(
            result_type=rtype,
            default_agg=_CUSTOM_DEFAULT_AGG[rtype],
            label=spec.get("title") or spec.get("customName")
            or "custom metric",
        )
    metric = BY_ID.get(metric_id)
    if metric is None:
        raise ValueError(f"Unknown metric {metric_id!r}")
    return metric


def compute_widget(spec: Dict[str, Any], table: FactTable, *,
                   dashboard_filters: Optional[Sequence[Dict[str, Any]]] = None
                   ) -> Dict[str, Any]:
    """Compute one widget.

    Returns *data*, not styling: values, states, labels and counts. The
    Streamlit view and the exported HTML both render from this same
    structure, and the JS engine returns the identical shape — so a
    widget looks the same in the app and in the export because they read
    one computation, not two.

    ``dashboard_filters`` stack **on top of** the widget's own filter, so
    a reader-side filter narrows every widget without editing any of them.
    """
    import numpy as np

    metric_id = spec.get("metric", "")
    metric = _resolve_metric(spec)

    filters = list(dashboard_filters or []) + list(spec.get("filter") or [])
    mask = apply_filters(table, filters)
    n_cases = int(mask.sum())

    unit = UNIT_BY_TYPE[metric.result_type]
    agg_kind = spec.get("agg") or metric.default_agg
    if metric.result_type == "percent":
        agg_kind = "share"  # the only aggregation that means anything

    out: Dict[str, Any] = {
        "id": spec.get("id"),
        "title": spec.get("title", metric.label),
        "viz": spec.get("viz", "kpi"),
        "unit": unit,
        "resultType": metric.result_type,
        "nCases": n_cases,
        "agg": agg_kind,
        "sampled": table.sampled,
    }

    if out["viz"] == "model":
        return out  # the diagram render carries the value; nothing to compute

    if metric_id in SERIES_METRICS:
        pts = series_values(table, metric_id, mask)
        out["series"] = pts
        value = aggregate([p["value"] for p in pts], agg_kind)
        out["value"] = value
        out["text"] = fmt(value, unit)
        out["state"] = target_state(value, spec.get("target"))
        return out

    values = per_case_values(table, metric_id, spec.get("params"))
    values = np.where(mask, values, np.nan)

    rows_ax = (spec.get("segment") or {}).get("rows") or "none"
    cols_ax = (spec.get("segment") or {}).get("cols") or "none"
    target = spec.get("target")

    if rows_ax == "none" and cols_ax == "none":
        return {**out, **_kpi(values, agg_kind, unit, target, n_cases)}

    if cols_ax == "none" or rows_ax == "none":
        axis = rows_ax if rows_ax != "none" else cols_ax
        return {**out, **_series_by(table, values, axis, agg_kind, unit,
                                    target)}

    return {**out, **_grid(table, values, rows_ax, cols_ax, agg_kind, unit,
                           target)}


def _score(values, agg_kind: str, target) -> Tuple[Optional[float],
                                                   Optional[str],
                                                   Optional[Dict[str, float]]]:
    """``(value, state, distribution)`` for one group of cases.

    Two target modes, per the design:

    ``aggregate`` (default)
        Score the group's aggregated value — "is our average within 14
        days".
    ``per_case``
        Score every case, then report the **share that met** as the
        group's value — "are 90% of cases within 14 days". The
        distribution across met/risk/missed feeds the tri-colour bar; the
        share is scored against ``shareGoal`` / ``shareWarn`` when set,
        since a share needs its own threshold to have a state at all.
    """
    import numpy as np

    if not target or not target.get("on") or target.get("mode") != "per_case":
        value = aggregate(values, agg_kind)
        return value, target_state(value, target), None

    v = np.asarray(values, dtype=np.float64)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return None, None, None
    states = [target_state(float(x), target) for x in v]
    dist = {
        s: 100.0 * sum(1 for x in states if x == s) / len(states)
        for s in ("met", "risk", "missed")
    }
    share = dist["met"]
    state = None
    if target.get("shareGoal") is not None:
        state = target_state(share, {
            "on": True, "dir": ">=",
            "value": target["shareGoal"],
            "warn": target.get("shareWarn", target["shareGoal"]),
        })
    return share, state, dist


def _kpi(values, agg_kind, unit, target, n_cases) -> Dict[str, Any]:
    per_case = bool(target and target.get("mode") == "per_case")
    value, state, dist = _score(values, agg_kind, target)
    unit = "%" if per_case else unit
    out: Dict[str, Any] = {
        "value": value, "text": fmt(value, unit), "state": state,
        "unit": unit,
    }
    if dist:
        out["distribution"] = dist
    if target and target.get("on"):
        arrow = "≥" if target.get("dir") == ">=" else "≤"
        if per_case:
            goal = target.get("shareGoal")
            out["sub"] = (f"goal ≥ {goal:g}%" if goal is not None
                          else f"per case {arrow} {target['value']:g}")
        else:
            out["sub"] = f"target {arrow} {target['value']:g}" + (
                " d" if unit == "d" else unit if unit == "%" else "")
    else:
        out["sub"] = f"{agg_kind} over {n_cases:,} cases"
    return out


def _series_by(table, values, axis, agg_kind, unit, target) -> Dict[str, Any]:
    import numpy as np

    codes, labels = segment_keys(table, axis)
    series, states = [], []
    for i, label in enumerate(labels):
        sel = np.where(codes == i, values, np.nan)
        if not np.any(np.isfinite(sel)):
            continue  # a segment with no surviving case is not a zero bar
        value, state, _ = _score(sel, agg_kind, target)
        series.append({"label": label, "value": value,
                       "text": fmt(value, unit), "state": state,
                       "nCases": int(np.count_nonzero(np.isfinite(sel)))})
        states.append(state)
    return {"axis": axis, "series": series,
            "state": worst_state(states),
            "value": aggregate(values, agg_kind),
            "text": fmt(aggregate(values, agg_kind), unit)}


def _grid(table, values, rows_ax, cols_ax, agg_kind, unit,
          target) -> Dict[str, Any]:
    import numpy as np

    r_codes, r_labels = segment_keys(table, rows_ax)
    c_codes, c_labels = segment_keys(table, cols_ax)

    # Only emit rows/columns that hold a case, so an attribute with 58
    # values does not render 50 empty columns.
    live = np.isfinite(values)
    r_used = [i for i in range(len(r_labels))
              if np.any(live & (r_codes == i))]
    c_used = [i for i in range(len(c_labels))
              if np.any(live & (c_codes == i))]

    cells, states = [], []
    for ri in r_used:
        row = []
        for ci in c_used:
            sel = np.where((r_codes == ri) & (c_codes == ci), values, np.nan)
            n = int(np.count_nonzero(np.isfinite(sel)))
            if n == 0:
                row.append({"value": None, "text": "—", "state": None,
                            "nCases": 0})
                continue
            value, state, _ = _score(sel, agg_kind, target)
            row.append({"value": value, "text": fmt(value, unit),
                        "state": state, "nCases": n})
            states.append(state)
        cells.append(row)

    return {
        "rowsAxis": rows_ax, "colsAxis": cols_ax,
        "rows": [r_labels[i] for i in r_used],
        "cols": [c_labels[i] for i in c_used],
        "cells": cells,
        "state": worst_state(states),
        "value": aggregate(values, agg_kind),
        "text": fmt(aggregate(values, agg_kind), unit),
    }


def scorecard(specs: Sequence[Dict[str, Any]], table: FactTable, *,
              dashboard_filters=None) -> List[Dict[str, Any]]:
    """One row per targeted widget — the export's landing section.

    Segmented widgets roll up to their worst segment (see
    :func:`worst_state`), so a scorecard row reads "this target is missed"
    the moment any segment misses it.
    """
    rows = []
    for spec in specs:
        target = spec.get("target")
        if not target or not target.get("on"):
            continue
        w = compute_widget(spec, table, dashboard_filters=dashboard_filters)
        rows.append({
            "id": spec.get("id"),
            "title": w["title"],
            "goal": target_goal_text(spec),
            "actual": w.get("text", "—"),
            "value": w.get("value"),
            "state": w.get("state"),
            "nCases": w.get("nCases", 0),
        })
    return rows


def target_goal_text(spec: Dict[str, Any]) -> str:
    """The goal a widget is scored against, in words.

    In ``per_case`` mode the widget's value is a *share*, so its goal is
    the share goal — not the per-case threshold. Reading the threshold
    with the share's unit would print "≤ 14%" for a target that actually
    reads "≥ 90% of cases within 14 days", which is not a rounding
    difference but a different claim.
    """
    target = spec.get("target") or {}
    try:
        metric = _resolve_metric(spec)
    except ValueError:
        metric = None
    unit = UNIT_BY_TYPE[metric.result_type] if metric else ""
    suffix = " d" if unit == "d" else ("%" if unit == "%" else "")
    arrow = "≥" if target.get("dir") == ">=" else "≤"

    if target.get("mode") == "per_case":
        share = target.get("shareGoal")
        if share is not None:
            return f"≥ {share:g}% of cases"
        return f"per case {arrow} {target['value']:g}{suffix}"
    return f"{arrow} {target['value']:g}{suffix}"


def target_goal_value(spec: Dict[str, Any]) -> Optional[float]:
    """The number :func:`target_goal_text` describes — what an
    achievement bar should measure against."""
    target = spec.get("target") or {}
    if target.get("mode") == "per_case":
        return target.get("shareGoal")
    return target.get("value")
