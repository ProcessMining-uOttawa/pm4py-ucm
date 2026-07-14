"""
Activity-attribute performer miner.

For each activity name in the log, look at every event labelled with that
activity, read a configured event attribute (or fall through a priority
list of them), aggregate the values across the log, and return the
activity→performer mapping.

Aggregation strategies
----------------------

``"mode"`` (default)
    Pick the most-common attribute value. Ties broken
    lexicographically. Use when each activity is "owned" by a single
    actor most of the time and occasional anomalies should be smoothed
    out.
``"first"``
    Pick the attribute value of the first event for the activity in the
    log. Use when the order of events carries meaning (e.g. the first
    handler is the one that "matters").
``"unbound"``
    Skip — return no mapping for activities whose attribute is ambiguous
    (multiple distinct values).
``"all"``
    Return a composite string like ``"Alice+Bob+Carol"`` when more than
    one value appears. Use as a debugging aid; binds RespRefs to a
    single (composite) Component rather than to none of them.

Parameters
----------

``attribute`` (str, default ``"org:resource"``)
    Primary event attribute to read. Ignored when ``attribute_priority``
    is supplied.
``attribute_priority`` (list of str, optional)
    Fall-through list of attribute names. For each event, the first
    attribute that is set wins (e.g. ``["org:role", "org:resource"]``
    prefers role over resource).
``strategy`` (str, default ``"mode"``)
    One of ``"mode"``, ``"first"``, ``"unbound"``, ``"all"``.
``min_support`` (float, default ``0.0``)
    For ``"mode"``: minimum fraction of events for an activity that must
    agree on the performer for the binding to be returned. Default
    ``0.0`` admits the modal performer even when the activity is highly
    dispersed (e.g. one of 100+ people who can perform it), since this
    is more informative than dropping the binding entirely. Set higher
    (e.g. ``0.5``) to require a clear majority before binding.
``activity_key`` (str, default ``"concept:name"``)
    Event-level attribute holding the activity name.
``case_id_key`` (str, default ``"case:concept:name"``)
    Case-level attribute (only used to detect a DataFrame).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple


def apply(log: Any, parameters: Optional[dict] = None) -> Dict[str, str]:
    """Return ``{activity_name: performer_name}``."""
    p = dict(parameters or {})
    primary_attr: str = p.get("attribute", "org:resource")
    attr_priority: List[str] = p.get("attribute_priority", [primary_attr])
    strategy: str = p.get("strategy", "mode")
    min_support: float = float(p.get("min_support", 0.0))
    activity_key: str = p.get("activity_key", "concept:name")
    case_id_key: str = p.get("case_id_key", "case:concept:name")

    # Bucket performer values per activity. Bucket order is first
    # occurrence in the log so the "first" strategy is meaningful,
    # ties break deterministically, and downstream component-creation
    # order (hence exported IDs) stays stable.
    if _is_dataframe(log, case_id_key):
        buckets = _dataframe_buckets(log, attr_priority, activity_key)
    else:
        buckets = defaultdict(list)
        for activity, value in _iter_events(log, attr_priority,
                                            activity_key, case_id_key):
            if value is None:
                continue
            buckets[activity].append(value)

    # Aggregate
    out: Dict[str, str] = {}
    for activity, values in buckets.items():
        chosen = _aggregate(values, strategy, min_support)
        if chosen:
            out[activity] = chosen
    return out


def distinct_components(
    log: Any, parameters: Optional[dict] = None,
) -> List[str]:
    """Return every distinct non-empty performer-like value that appears
    anywhere in ``log``, in sorted order.

    Used for component discovery — the vocabulary of every actor that
    shows up in the log, regardless of whether they have a clear binding
    to a specific activity (which is what :func:`apply` returns).
    """
    p = dict(parameters or {})
    primary_attr: str = p.get("attribute", "org:resource")
    attr_priority: List[str] = p.get("attribute_priority", [primary_attr])
    activity_key: str = p.get("activity_key", "concept:name")
    case_id_key: str = p.get("case_id_key", "case:concept:name")

    if _is_dataframe(log, case_id_key):
        _, resolved = _dataframe_values(log, attr_priority, activity_key)
        if resolved is None:
            return []
        return sorted(set(resolved))

    seen: set = set()
    for _, value in _iter_events(log, attr_priority, activity_key, case_id_key):
        if value is None:
            continue
        seen.add(value)
    return sorted(seen)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _aggregate(values: List[str], strategy: str, min_support: float) -> Optional[str]:
    if not values:
        return None
    if strategy == "first":
        return values[0]
    if strategy == "all":
        unique = []
        seen = set()
        for v in values:
            if v not in seen:
                seen.add(v); unique.append(v)
        return "+".join(unique) if len(unique) > 1 else unique[0]
    if strategy == "unbound":
        unique = set(values)
        return next(iter(unique)) if len(unique) == 1 else None
    # default: "mode"
    counts = Counter(values)
    # Sort by (-count, name) for deterministic tie-breaking.
    top, top_count = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    if top_count / len(values) >= min_support:
        return top
    return None


# ---------------------------------------------------------------------------
# Event iteration — PM4Py EventLog path
# ---------------------------------------------------------------------------

def _iter_events(
    log: Any,
    attr_priority: Iterable[str],
    activity_key: str,
    case_id_key: str,
) -> Iterable[Tuple[str, Optional[str]]]:
    """Yield ``(activity, performer)`` pairs over every event of ``log``
    — the EventLog (iterable-of-traces) path. DataFrames go through the
    vectorized helpers below instead: per-row iteration
    (``iterrows``) took *minutes* on logs with a few hundred thousand
    events, even when no performer attribute existed at all.
    """
    attrs = list(attr_priority)
    for trace in log:
        for ev in trace:
            activity = ev.get(activity_key) if hasattr(ev, "get") else None
            if activity is None:
                continue
            yield str(activity), _first_set(ev, attrs)


# ---------------------------------------------------------------------------
# Vectorized DataFrame path
# ---------------------------------------------------------------------------

def _is_dataframe(log: Any, case_id_key: str) -> bool:
    """Same duck-typed detection the per-event iterator used."""
    return (hasattr(log, "iterrows")
            and case_id_key in getattr(log, "columns", []))


def _dataframe_values(log, attr_priority: Iterable[str], activity_key: str):
    """Vectorized equivalent of the per-event walk: ``(activities,
    performers)`` as two aligned pandas Series, restricted to events
    whose performer is set. ``performers`` is ``None`` when no priority
    attribute exists as a column (O(1) — no scan), mirroring the
    per-event yield of ``None`` for every row.

    Semantics mirror :func:`_first_set` exactly: ``None``/NaN and
    empty-string values do not count as "set"; the first *set*
    attribute in priority order wins; values are stringified. Events
    with a missing activity are dropped.
    """
    import pandas as pd

    if activity_key not in log.columns:
        return None, None
    present = [a for a in attr_priority if a in log.columns]
    if not present:
        return None, None

    # Priority fall-through: first set (non-null, non-empty) value.
    resolved = pd.Series(None, index=log.index, dtype=object)
    unset = pd.Series(True, index=log.index)
    for col in present:
        s = log[col]
        is_set = unset & s.notna() & (s.astype(str) != "")
        resolved[is_set] = s[is_set]
        unset &= ~is_set

    keep = resolved.notna() & log[activity_key].notna()
    activities = log.loc[keep, activity_key].astype(str)
    performers = resolved[keep].map(str)
    return activities, performers


def _dataframe_buckets(
    log, attr_priority: Iterable[str], activity_key: str,
) -> Dict[str, List[str]]:
    """``{activity: [performer, ...]}`` in first-occurrence order —
    identical to the per-event bucket construction, built vectorized."""
    activities, performers = _dataframe_values(
        log, attr_priority, activity_key,
    )
    if performers is None:
        return {}
    import pandas as pd

    frame = pd.DataFrame({"a": activities, "v": performers})
    # groupby(sort=False) preserves the order in which activities first
    # appear — the same order the defaultdict insertion produced.
    return {a: g["v"].tolist() for a, g in frame.groupby("a", sort=False)}


def _first_set(ev: Any, attrs: Iterable[str]) -> Optional[str]:
    """Return the first attribute in ``attrs`` set on ``ev``, or None."""
    for attr in attrs:
        try:
            v = ev.get(attr) if hasattr(ev, "get") else None
        except Exception:
            v = None
        if v is not None and v != "" and (isinstance(v, str) or not _is_nan(v)):
            return str(v)
    return None


def _is_nan(v: Any) -> bool:
    try:
        return v != v   # NaN is the only value that is not equal to itself
    except Exception:
        return False
