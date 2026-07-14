"""Tests for the progress-reporting layer and the vectorized
DataFrame resource miner.

Covers: the throttled Ticker contract (start/finish emissions, cap on
update count, no-op with a None callback), progress callbacks firing
from variant clustering / family discovery / family statistics with
monotonic counts, DataFrame-vs-EventLog equivalence of the resource
miner across every aggregation strategy (the DataFrame path was
rewritten vectorized after per-row iteration took minutes on a
617k-event log), and the O(1) short-circuit when no performer
attribute exists."""
from __future__ import annotations

import pytest

pd = pytest.importorskip("pandas")

from pm4py_ucm.algo.discovery.resources.variants import activity_attribute
from pm4py_ucm.algo.discovery.variants import clustering as _clustering
from pm4py_ucm.util.progress import Ticker


class _Event(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


def _as_eventlog(rows):
    """rows -> list of single-event traces (order preserved)."""
    return [[_Event(r)] for r in rows]


def _as_dataframe(rows):
    df = pd.DataFrame(rows)
    if "case:concept:name" not in df.columns:
        df["case:concept:name"] = [f"c{i}" for i in range(len(df))]
    return df


# ---------------------------------------------------------------------------
# Ticker
# ---------------------------------------------------------------------------

class TestTicker:

    def test_emits_start_progress_and_finish(self):
        seen = []
        t = Ticker(lambda s, d, tot: seen.append((s, d, tot)),
                   "work", 4, report_every=1)
        for _ in range(4):
            t.tick()
        t.finish()
        assert seen[0] == ("work", 0, 4)
        assert seen[-1] == ("work", 4, 4)
        assert [d for _, d, _ in seen] == [0, 1, 2, 3, 4]

    def test_throttles_large_totals(self):
        seen = []
        t = Ticker(lambda s, d, tot: seen.append(d), "work", 100_000)
        for _ in range(100_000):
            t.tick()
        t.finish()
        assert len(seen) <= 205          # ~200 updates + start/final
        assert seen[-1] == 100_000
        assert seen == sorted(seen)      # monotonic

    def test_none_callback_is_noop(self):
        t = Ticker(None, "work", 10)
        for _ in range(10):
            t.tick()
        t.finish()  # nothing raised, nothing emitted

    def test_finish_idempotent_and_final(self):
        seen = []
        t = Ticker(lambda s, d, tot: seen.append(d), "work", 5,
                   report_every=100)
        t.tick(2)
        t.finish()
        t.finish()
        assert seen == [0, 5]


# ---------------------------------------------------------------------------
# Callbacks from the pipelines
# ---------------------------------------------------------------------------

class T:
    def __init__(self, operator=None, label=None, children=None):
        self.operator = operator
        self.label = label
        self.children = children or []


class TestPipelineCallbacks:

    def test_clustering_reports_per_case(self):
        tree = T("->", children=[T(label="A"), T(label="B")])
        log = [("c%d" % i, ["A", "B"]) for i in range(10)]
        seen = []
        _clustering.cluster(
            log, tree,
            progress_callback=lambda s, d, t: seen.append((s, d, t)),
        )
        assert seen[0] == ("Replaying cases", 0, 10)
        assert seen[-1] == ("Replaying cases", 10, 10)

    def test_family_discovery_reports_per_cell(self):
        from pm4py_ucm.algo.discovery.families import discover

        rows = []
        for i, (ctype, act) in enumerate(
                [("X", "A")] * 3 + [("Y", "B")] * 3):
            rows.append({
                "case:concept:name": f"c{i}",
                "concept:name": act,
                "time:timestamp": pd.Timestamp("2026-01-01"),
                "case:t": ctype,
            })
        df = pd.DataFrame(rows)

        def miner(cell_df):
            return T("->", children=[
                T(label=a) for a in sorted(set(cell_df["concept:name"]))
            ])

        seen = []
        family = discover(
            df, ["t"], min_cases=1,
            parameters={"tree_miner": miner, "resource_attribute": False},
            progress_callback=lambda s, d, t: seen.append((s, d, t)),
        )
        assert ("Mining one model per cell", 2, 2) in seen

        from pm4py_ucm.algo.discovery.families.stats import (
            compute_family_stats,
        )
        seen.clear()
        compute_family_stats(
            family,
            progress_callback=lambda s, d, t: seen.append((s, d, t)),
        )
        assert ("Computing family statistics", 2, 2) in seen


# ---------------------------------------------------------------------------
# Vectorized resource mining — equivalence with the per-event path
# ---------------------------------------------------------------------------

_ROWS = [
    {"concept:name": "Login",  "org:resource": "Alice"},
    {"concept:name": "Login",  "org:resource": "Alice"},
    {"concept:name": "Login",  "org:resource": "Bob"},
    {"concept:name": "Pay",    "org:resource": "Carol"},
    {"concept:name": "Pay",    "org:resource": "Dave"},
    {"concept:name": "Ship",   "org:resource": None},
    {"concept:name": "Ship",   "org:resource": ""},
    {"concept:name": "Audit"},                          # attr missing
    {"concept:name": "Review", "org:role": "Clerk"},    # priority fallback
    {"concept:name": "Review", "org:resource": "Erin"},
]


class TestVectorizedResources:

    @pytest.mark.parametrize("strategy", ["mode", "first", "unbound", "all"])
    def test_dataframe_matches_eventlog(self, strategy):
        params = {
            "attribute_priority": ["org:role", "org:resource"],
            "strategy": strategy,
        }
        from_events = activity_attribute.apply(_as_eventlog(_ROWS), params)
        from_frame = activity_attribute.apply(_as_dataframe(_ROWS), params)
        assert from_frame == from_events
        # Bucket/first-occurrence ordering preserved (component-creation
        # order drives exported IDs downstream).
        assert list(from_frame) == list(from_events)

    def test_distinct_components_match(self):
        params = {"attribute_priority": ["org:role", "org:resource"]}
        assert (
            activity_attribute.distinct_components(
                _as_dataframe(_ROWS), params)
            == activity_attribute.distinct_components(
                _as_eventlog(_ROWS), params)
        )

    def test_min_support_matches(self):
        params = {"attribute": "org:resource", "min_support": 0.6}
        assert (
            activity_attribute.apply(_as_dataframe(_ROWS), params)
            == activity_attribute.apply(_as_eventlog(_ROWS), params)
        )

    def test_numeric_values_stringified(self):
        rows = [
            {"concept:name": "A", "org:resource": 7},
            {"concept:name": "A", "org:resource": 7},
            {"concept:name": "B", "org:resource": 3.5},
        ]
        params = {"attribute": "org:resource"}
        # EventLog path sees the Python ints as-is; the DataFrame path
        # sees what pandas stores — a mixed int/float column coerces to
        # float64 (this was already true of the per-row iteration, it
        # is a property of the frame, not of the miner).
        assert activity_attribute.apply(_as_eventlog(rows), params) == \
            {"A": "7", "B": "3.5"}
        assert activity_attribute.apply(_as_dataframe(rows), params) == \
            {"A": "7.0", "B": "3.5"}

    def test_no_attribute_column_short_circuits_empty(self):
        df = _as_dataframe([{"concept:name": "A"}] * 5)
        params = {"attribute_priority": ["org:resource", "org:role"]}
        assert activity_attribute.apply(df, params) == {}
        assert activity_attribute.distinct_components(df, params) == []

    def test_nan_attribute_values_are_unset(self):
        rows = [
            {"concept:name": "A", "org:resource": float("nan")},
            {"concept:name": "A", "org:resource": "Zoe"},
        ]
        params = {"attribute": "org:resource"}
        assert (
            activity_attribute.apply(_as_dataframe(rows), params)
            == activity_attribute.apply(_as_eventlog(rows), params)
            == {"A": "Zoe"}
        )
