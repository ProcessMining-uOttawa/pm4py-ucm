"""When a case starts, and how long it lasts — defined once.

**Case duration is earliest start to latest completion.** On an interval
log (one carrying a start-timestamp column) a case starts at the earliest
moment any of its events started, so the first activity's own duration is
part of the case. On a single-timestamp log an event starts when it
completes, and the definition reduces to first event to last event.

An event whose start timestamp is missing, does not parse, or lies after
its own completion counts from its completion: a broken start must neither
stretch a case nor drop it.

Three callers share this definition and must keep agreeing:

* the family statistics (:mod:`pm4py_ucm.algo.discovery.families.stats`)
  and the partition advisor
  (:mod:`pm4py_ucm.algo.discovery.families.advisor`), through
  :func:`case_durations`;
* the dashboards' fact table
  (:func:`pm4py_ucm.algo.dashboards.contract.build_fact_table`), which
  applies the same rule to the integer epoch seconds it stores, because the
  case start is also the origin of its delta encoding. Its ``duration``
  metric can therefore differ from :func:`case_durations` by the rounding of
  two timestamps to the second, and by nothing else.

``tests/test_metric_validation.py`` holds the three to one another.
"""
from __future__ import annotations

from typing import Optional

__all__ = ["case_durations"]


def case_durations(
    log_df,
    case_id_col: str = "case:concept:name",
    timestamp_col: str = "time:timestamp",
    start_timestamp_col: Optional[str] = "start_timestamp",
    *,
    case=None,
):
    """Per-case duration in **seconds**: earliest start to latest
    completion (see the module docstring).

    Returns a ``pandas.Series`` indexed by case id, in order of first
    appearance. A case none of whose completion timestamps parse has
    ``NaN``.

    ``start_timestamp_col`` may be ``None`` or absent from ``log_df``: the
    log is then a single-timestamp one. ``case`` overrides the grouping key
    (a Series aligned with ``log_df``), for a caller that normalises case
    ids.
    """
    import pandas as pd

    end = pd.to_datetime(log_df[timestamp_col], utc=True, errors="coerce")
    begin = end
    if start_timestamp_col and start_timestamp_col in log_df.columns:
        start = pd.to_datetime(
            log_df[start_timestamp_col], utc=True, errors="coerce")
        # NaT fails the comparison, so a missing start falls back as well.
        begin = start.where(start <= end, end)
    key = log_df[case_id_col] if case is None else case
    span = (end.groupby(key, sort=False).max()
            - begin.groupby(key, sort=False).min())
    return span.dt.total_seconds()
