"""Metric-correctness validation suite.

The pre-existing ``test_performance.py`` and ``test_family_stats.py``
fixtures use *uniform* timings (every gap 10 min, every service a fixed
few minutes), which makes ``mean == median == min == max`` for every
activity and edge — so a mean/median or a min/max swap in the
implementation would pass all of them. This module closes that gap.

It validates the activity / edge / process / choice metrics against
*independent* oracles, per ``docs/metrics.md``:

* **hardened distinct-value oracle** — a hand-computed fixture where
  every aggregate (min, median, mean, max, total) is a different
  number, asserted to the exact second (:class:`TestDistinctValueOracle`);
* **invariants** — algebraic identities that must hold on any log
  (:class:`TestInvariants`);
* **metamorphic transforms** — duplicate / shift / scale / permute /
  relabel the log and check the metrics change exactly as they must
  (:class:`TestMetamorphic`);
* **simulation ground truth** — logs generated from a known process
  tree with fixed branch choices and injected noise, so variant
  counts, fitness and per-branch choice counts are known by
  construction (:class:`TestSimulationGroundTruth`);
* **semantics decisions** — behaviours that were deliberately chosen
  (negative waiting on overlapping intervals, timestamp ties,
  single-event cases) pinned so a change is a conscious one
  (:class:`TestSemanticsDecisions`);
* **differential vs pm4py** — the single-timestamp DFG frequencies and
  waiting-time aggregations reconciled against pm4py's own
  ``discover_performance_dfg`` (:class:`TestDifferentialPm4py`, skipped
  when pm4py is absent).
"""
from __future__ import annotations

import statistics as _st

import pytest

pd = pytest.importorskip("pandas")

from pm4py_ucm.algo.discovery.families import discover
from pm4py_ucm.algo.discovery.families.stats import (
    _process_level,
    compute_family_stats,
)
from pm4py_ucm.algo.performance import compute_performance_stats


def _process_duration(df):
    """Case-duration dict from the process level of the family stats."""
    _, duration, _, _ = _process_level(
        df, "case:concept:name", "concept:name",
        "time:timestamp", "start_timestamp")
    return duration


class T:
    """Duck-typed process-tree node (converter protocol)."""

    def __init__(self, operator=None, label=None, children=None):
        self.operator = operator
        self.label = label
        self.children = children or []


# ===========================================================================
# Hardened distinct-value fixture
# ===========================================================================
# Three cases, each running A -> B -> C, with per-occurrence service
# durations and per-case waiting gaps chosen so that for EVERY metric the
# five aggregates are all different numbers. All values in minutes:
#
#          svcA  gapAB  svcB  gapBC  svcC
#   case0:  1     2      4     3      3
#   case1:  2     5      5     4      8
#   case2:  6    14      9    14     10
#
# Derived expectations (seconds) are hand-computed in the assertions.
_SPEC = [
    # (svcA, gapAB, svcB, gapBC, svcC) in minutes
    (1, 2, 4, 3, 3),
    (2, 5, 5, 4, 8),
    (6, 14, 9, 14, 10),
]
_M = 60.0  # minutes -> seconds


def _distinct_log(intervals: bool):
    """Build the distinct-value fixture as a DataFrame.

    ``intervals=True`` adds a ``start_timestamp`` column (interval log);
    otherwise only the completion ``time:timestamp`` is present."""
    rows = []
    base = pd.Timestamp("2026-01-01")
    for i, (svcA, gapAB, svcB, gapBC, svcC) in enumerate(_SPEC):
        t = base + pd.Timedelta(days=i)         # cases far apart
        start_A = t
        comp_A = start_A + pd.Timedelta(minutes=svcA)
        start_B = comp_A + pd.Timedelta(minutes=gapAB)
        comp_B = start_B + pd.Timedelta(minutes=svcB)
        start_C = comp_B + pd.Timedelta(minutes=gapBC)
        comp_C = start_C + pd.Timedelta(minutes=svcC)
        for act, start, comp in (("A", start_A, comp_A),
                                 ("B", start_B, comp_B),
                                 ("C", start_C, comp_C)):
            row = {"case:concept:name": f"c{i}", "concept:name": act,
                   "time:timestamp": comp}
            if intervals:
                row["start_timestamp"] = start
            rows.append(row)
    return pd.DataFrame(rows)


def _agg(entry, prefix=""):
    """Pull the five aggregates for a metric family into a tuple."""
    return (
        entry[f"{prefix}min_time"], entry[f"{prefix}median_time"],
        entry[f"{prefix}mean_time"], entry[f"{prefix}max_time"],
        entry[f"{prefix}total_time"],
    )


class TestDistinctValueOracle:
    """Every aggregate is a distinct hand-computed number, so a
    mean<->median or min<->max swap is caught."""

    def test_activity_service_times_interval(self):
        stats = compute_performance_stats(_distinct_log(intervals=True))
        # A service: [1,2,6] min -> [60,120,360] s
        assert _agg(stats.activity["A"]) == (60, 120, 180, 360, 540)
        # B service: [4,5,9] min
        assert _agg(stats.activity["B"]) == (240, 300, 360, 540, 1080)
        # C service: [3,8,10] min
        assert _agg(stats.activity["C"]) == (180, 480, 420, 600, 1260)

    def test_activity_sojourn_times(self):
        # Sojourn = completion - previous completion (any log).
        stats = compute_performance_stats(_distinct_log(intervals=True))
        # B sojourn = gapAB + svcB = [6,10,23] min
        assert _agg(stats.activity["B"], "sojourn_") == (
            360, 600, 780, 1380, 2340)
        # C sojourn = gapBC + svcC = [6,12,24] min
        assert _agg(stats.activity["C"], "sojourn_") == (
            360, 720, 840, 1440, 2520)
        # A is every case's first event -> no sojourn.
        assert "sojourn_mean_time" not in stats.activity["A"]

    def test_edge_waiting_interval(self):
        # Interval waiting = completion(a) -> start(b) = the gap itself.
        stats = compute_performance_stats(_distinct_log(intervals=True))
        # A->B gap [2,5,14] min
        assert _agg(stats.pairs[("A", "B")]) == (120, 300, 420, 840, 1260)
        # B->C gap [3,4,14] min
        assert _agg(stats.pairs[("B", "C")]) == (180, 240, 420, 840, 1260)

    def test_edge_waiting_single_timestamp(self):
        # Single-ts waiting = completion -> completion = successor
        # sojourn (includes the successor's service time).
        stats = compute_performance_stats(_distinct_log(intervals=False))
        assert _agg(stats.pairs[("A", "B")]) == (360, 600, 780, 1380, 2340)
        assert _agg(stats.pairs[("B", "C")]) == (360, 720, 840, 1440, 2520)
        # Single-timestamp logs carry no activity service times.
        assert "mean_time" not in stats.activity["A"]

    def test_frequencies_and_coverage(self):
        stats = compute_performance_stats(_distinct_log(intervals=False))
        for act in "ABC":
            assert stats.activity[act]["frequency"] == 3
            assert stats.activity[act]["case_coverage"] == 3
        assert stats.pairs[("A", "B")]["frequency"] == 3
        assert stats.pairs[("B", "C")]["frequency"] == 3

    def test_process_case_durations_interval(self):
        # Case duration (interval) = last completion - first start.
        # Per case: svcA+gapAB+svcB+gapBC+svcC = [13,24,53] min.
        dur = _process_duration(_distinct_log(intervals=True))
        assert dur["min"] == 13 * _M
        assert dur["median"] == 24 * _M
        assert dur["mean"] == 30 * _M
        assert dur["max"] == 53 * _M
        assert dur["total"] == 90 * _M

    def test_process_case_durations_single_timestamp(self):
        # Single-ts case duration = last completion - first completion
        #   = interval duration - svcA = [12,22,47] min.
        dur = _process_duration(_distinct_log(intervals=False))
        assert dur["min"] == 12 * _M
        assert dur["median"] == 22 * _M
        assert dur["mean"] == 27 * _M
        assert dur["max"] == 47 * _M
        assert dur["total"] == 81 * _M

    def test_events_per_case_and_counts(self):
        df = _distinct_log(intervals=True)
        eb, _, n_events, n_activities = _process_level(
            df, "case:concept:name", "concept:name",
            "time:timestamp", "start_timestamp")
        assert n_events == 9
        assert n_activities == 3
        # Every case runs exactly A -> B -> C.
        assert eb == {"mean": 3.0, "median": 3.0, "min": 3.0, "max": 3.0}


# ===========================================================================
# Invariants
# ===========================================================================
def _random_interval_log(seed: int, n_cases: int = 30):
    import random
    rng = random.Random(seed)
    acts = ["A", "B", "C", "D", "E"]
    rows = []
    base = pd.Timestamp("2026-03-01")
    cursor = 0
    for cid in range(n_cases):
        for _ in range(rng.randint(2, 6)):
            cursor += rng.randint(1, 40)
            start = cursor
            cursor += rng.randint(1, 30)
            rows.append({
                "case:concept:name": f"c{cid:03d}",
                "concept:name": rng.choice(acts),
                "start_timestamp": base + pd.Timedelta(minutes=start),
                "time:timestamp": base + pd.Timedelta(minutes=cursor),
            })
        cursor += rng.randint(60, 200)
    return pd.DataFrame(rows)


class TestInvariants:

    def test_frequency_sums_to_events(self):
        df = _random_interval_log(1)
        stats = compute_performance_stats(df)
        assert sum(e["frequency"] for e in stats.activity.values()) == len(df)

    def test_coverage_bounded(self):
        df = _random_interval_log(2)
        stats = compute_performance_stats(df)
        n_cases = df["case:concept:name"].nunique()
        for e in stats.activity.values():
            assert e["case_coverage"] <= e["frequency"]
            assert e["case_coverage"] <= n_cases

    def test_pair_frequency_identity(self):
        # Sum of directly-follows pair frequencies = events - cases.
        df = _random_interval_log(3)
        stats = compute_performance_stats(df)
        n_cases = df["case:concept:name"].nunique()
        assert sum(p["frequency"] for p in stats.pairs.values()) == (
            len(df) - n_cases)

    def test_aggregate_ordering(self):
        df = _random_interval_log(4)
        stats = compute_performance_stats(df)
        for e in stats.activity.values():
            if "mean_time" in e:
                assert e["min_time"] <= e["median_time"] <= e["max_time"]
                assert e["min_time"] <= e["mean_time"] <= e["max_time"]
        for p in stats.pairs.values():
            assert p["min_time"] <= p["median_time"] <= p["max_time"]
            assert p["min_time"] <= p["mean_time"] <= p["max_time"]

    def test_total_equals_mean_times_n(self):
        df = _random_interval_log(5)
        stats = compute_performance_stats(df)
        for e in stats.activity.values():
            if "total_time" in e:
                assert e["total_time"] == pytest.approx(
                    e["mean_time"] * e["frequency"])
        for p in stats.pairs.values():
            assert p["total_time"] == pytest.approx(
                p["mean_time"] * p["frequency"])

    def test_sojourn_count_is_frequency_minus_case_starts(self):
        # A sojourn value exists for every event except a case's first,
        # so n_sojourn(act) = frequency(act) - #cases starting with act.
        df = _random_interval_log(6)
        stats = compute_performance_stats(df)
        first = df.sort_values(["case:concept:name", "time:timestamp"]) \
            .groupby("case:concept:name")["concept:name"].first()
        starts = first.value_counts().to_dict()
        for act, e in stats.activity.items():
            n_soj = e["sojourn_total_time"] / e["sojourn_mean_time"] \
                if "sojourn_mean_time" in e else 0
            expected = e["frequency"] - starts.get(act, 0)
            assert round(n_soj) == expected

    def test_covered_cases_equals_sum_of_cell_cases(self):
        df = _make_family_log()
        stats = compute_family_stats(_discover_family(df))
        assert stats.covered_cases == sum(c.n_cases for c in stats.cells)


# ===========================================================================
# Metamorphic transforms
# ===========================================================================
class TestMetamorphic:

    def test_duplicate_cases_scales_counts_not_averages(self):
        df = _random_interval_log(7)
        base = compute_performance_stats(df)
        dup = df.copy()
        dup["case:concept:name"] = dup["case:concept:name"] + "_b"
        doubled = compute_performance_stats(pd.concat([df, dup]))
        for act, e in base.activity.items():
            d = doubled.activity[act]
            assert d["frequency"] == 2 * e["frequency"]
            assert d["case_coverage"] == 2 * e["case_coverage"]
            if "mean_time" in e:
                assert d["mean_time"] == pytest.approx(e["mean_time"])
                assert d["median_time"] == pytest.approx(e["median_time"])
                assert d["total_time"] == pytest.approx(2 * e["total_time"])

    def test_shift_all_timestamps_is_invariant(self):
        df = _random_interval_log(8)
        base = compute_performance_stats(df)
        shifted = df.copy()
        for col in ("start_timestamp", "time:timestamp"):
            shifted[col] = shifted[col] + pd.Timedelta(days=365)
        after = compute_performance_stats(shifted)
        assert after.pairs == base.pairs
        assert after.activity == base.activity

    def test_scale_time_scales_durations(self):
        df = _random_interval_log(9)
        base = compute_performance_stats(df)
        scaled = df.copy()
        origin = pd.Timestamp("2026-03-01")
        for col in ("start_timestamp", "time:timestamp"):
            scaled[col] = origin + (scaled[col] - origin) * 3
        after = compute_performance_stats(scaled)
        for act, e in base.activity.items():
            if "mean_time" in e:
                assert after.activity[act]["mean_time"] == pytest.approx(
                    3 * e["mean_time"])
                assert after.activity[act]["median_time"] == pytest.approx(
                    3 * e["median_time"])
        for pair, p in base.pairs.items():
            assert after.pairs[pair]["total_time"] == pytest.approx(
                3 * p["total_time"])

    def test_permute_row_order_is_invariant(self):
        df = _random_interval_log(10)
        base = compute_performance_stats(df)
        shuffled = df.sample(frac=1.0, random_state=123).reset_index(drop=True)
        after = compute_performance_stats(shuffled)
        # Row order changes float summation order, so std_time may differ
        # by ~1e-13 (floating-point non-associativity) — compare within
        # tolerance rather than bit-for-bit.
        _assert_metric_dicts_close(after.activity, base.activity)
        _assert_metric_dicts_close(after.pairs, base.pairs)


def _assert_metric_dicts_close(got, expected):
    """Compare ``{key: {metric: number}}`` maps within float tolerance."""
    assert set(got) == set(expected)
    for key in expected:
        assert set(got[key]) == set(expected[key]), key
        for metric, value in expected[key].items():
            assert got[key][metric] == pytest.approx(value), (key, metric)

    def test_relabel_case_ids_is_invariant(self):
        df = _random_interval_log(11)
        base = compute_performance_stats(df)
        relabelled = df.copy()
        relabelled["case:concept:name"] = (
            "X" + relabelled["case:concept:name"])
        after = compute_performance_stats(relabelled)
        assert after.pairs == base.pairs
        assert after.activity == base.activity
        assert after.total_cases == base.total_cases

    def test_concatenate_disjoint_logs_is_additive(self):
        a = _random_interval_log(12, n_cases=15)
        b = _random_interval_log(13, n_cases=15)
        b = b.copy()
        b["case:concept:name"] = "B_" + b["case:concept:name"]
        sa = compute_performance_stats(a)
        sb = compute_performance_stats(b)
        sab = compute_performance_stats(pd.concat([a, b]))
        assert sab.total_cases == sa.total_cases + sb.total_cases
        for act in set(sa.activity) | set(sb.activity):
            fa = sa.activity.get(act, {}).get("frequency", 0)
            fb = sb.activity.get(act, {}).get("frequency", 0)
            assert sab.activity[act]["frequency"] == fa + fb


# ===========================================================================
# Simulation ground truth (replay metrics)
# ===========================================================================
def _xor_miner(_):
    return T("->", children=[
        T(label="Register"),
        T("X", children=[T(label="Triage"), T(label="Scan")]),
        T(label="Done"),
    ])


def _make_family_log(noise=False):
    """Register -> (Triage|Scan) -> Done, split by cancer_type.
    Breast picks Triage 4x / Scan 2x; Lung Triage 1x / Scan 5x. With
    ``noise=True`` one Breast case skips the middle (non-conforming)."""
    rows = []
    ts = pd.Timestamp("2026-01-01")

    def case(cid, acts, ctype):
        nonlocal ts
        for a in acts:
            rows.append({"case:concept:name": cid, "concept:name": a,
                         "time:timestamp": ts, "case:cancer_type": ctype})
            ts += pd.Timedelta(minutes=10)
        ts += pd.Timedelta(hours=1)

    i = 0
    for _ in range(4):
        case(f"b{i}", ["Register", "Triage", "Done"], "Breast"); i += 1
    for _ in range(2):
        case(f"b{i}", ["Register", "Scan", "Done"], "Breast"); i += 1
    if noise:
        case(f"b{i}", ["Register", "Done"], "Breast"); i += 1  # non-conforming
    for _ in range(1):
        case(f"l{i}", ["Register", "Triage", "Done"], "Lung"); i += 1
    for _ in range(5):
        case(f"l{i}", ["Register", "Scan", "Done"], "Lung"); i += 1
    return pd.DataFrame(rows)


def _discover_family(df, miner=_xor_miner):
    return discover(df, ["cancer_type"], min_cases=1,
                    parameters={"tree_miner": miner,
                                "resource_attribute": False})


class TestSimulationGroundTruth:

    def test_choice_counts_match_construction(self):
        stats = compute_family_stats(_discover_family(_make_family_log()))
        ch = stats.choices[0]
        assert ch.branches == ["Triage", "Scan"]
        # Constructed: Breast 4/2, Lung 1/5 — branch order must be right.
        assert ch.counts == [[4, 2], [1, 5]]

    def test_noise_excluded_from_counts_and_lowers_fitness(self):
        stats = compute_family_stats(
            _discover_family(_make_family_log(noise=True)))
        ch = stats.choices[0]
        # The non-conforming Breast case contributes to NEITHER branch.
        assert ch.counts == [[4, 2], [1, 5]]
        breast, lung = stats.cells
        assert breast.n_cases == 7
        assert breast.variants["fitness"] == pytest.approx(6 / 7)
        assert lung.variants["fitness"] == 1.0

    def test_variant_counts_by_construction(self):
        stats = compute_family_stats(_discover_family(_make_family_log()))
        for cell in stats.cells:
            # Exactly two paths through the single XOR are exercised.
            assert cell.variants["n_variants"] == 2

    def test_inside_loop_choice_counts_exceed_cases(self):
        # A loop whose body is an XOR: each iteration evaluates the
        # choice, so summed branch counts can exceed the case count.
        def miner(_):
            return T("->", children=[
                T(label="Start"),
                T("*", children=[
                    T("X", children=[T(label="Rework"), T(label="Fix")]),
                    T(label="Check"),
                ]),
                T(label="End"),
            ])

        rows = []
        ts = pd.Timestamp("2026-01-01")

        def case(cid, acts, grp):
            nonlocal ts
            for a in acts:
                rows.append({"case:concept:name": cid, "concept:name": a,
                             "time:timestamp": ts, "case:grp": grp})
                ts += pd.Timedelta(minutes=5)
            ts += pd.Timedelta(hours=1)

        # Cell P loops (XOR evaluated twice: Rework then Fix); cell Q
        # runs the body once (Fix). Three evaluations across two cases,
        # so the summed branch counts exceed the two-case count.
        case("p0", ["Start", "Rework", "Check", "Fix", "End"], "P")
        case("q0", ["Start", "Fix", "End"], "Q")
        df = pd.DataFrame(rows)
        stats = compute_family_stats(
            discover(df, ["grp"], min_cases=1,
                     parameters={"tree_miner": miner,
                                 "resource_attribute": False}))
        loop_choices = [c for c in stats.choices if c.inside_loop]
        assert loop_choices, "expected an inside-loop choice"
        ch = loop_choices[0]
        total = sum(sum(c) for c in ch.counts if c)
        # 3 evaluations across 2 cases -> counts exceed the case count.
        assert total == 3


# ===========================================================================
# Semantics decisions (pinned so a change is deliberate)
# ===========================================================================
class TestSemanticsDecisions:

    def test_overlapping_intervals_give_negative_waiting(self):
        # DECISION: when the successor starts before the predecessor
        # completes (overlapping activities on an interval log), the
        # waiting time is reported as negative, not clamped to zero.
        base = pd.Timestamp("2026-01-01")
        df = pd.DataFrame([
            {"case:concept:name": "c0", "concept:name": "A",
             "start_timestamp": base,
             "time:timestamp": base + pd.Timedelta(minutes=30)},
            {"case:concept:name": "c0", "concept:name": "B",
             "start_timestamp": base + pd.Timedelta(minutes=20),
             "time:timestamp": base + pd.Timedelta(minutes=40)},
        ])
        stats = compute_performance_stats(df)
        assert stats.pairs[("A", "B")]["mean_time"] == -600.0

    def test_simultaneous_timestamps_give_zero_waiting(self):
        # DECISION: tied completion timestamps yield zero-duration
        # edges; the pair is still recorded (frequency counts).
        base = pd.Timestamp("2026-01-01")
        df = pd.DataFrame([
            {"case:concept:name": "c0", "concept:name": "A",
             "time:timestamp": base},
            {"case:concept:name": "c0", "concept:name": "B",
             "time:timestamp": base},
        ])
        stats = compute_performance_stats(df)
        assert stats.pairs[("A", "B")]["frequency"] == 1
        assert stats.pairs[("A", "B")]["mean_time"] == 0.0

    def test_single_event_case_has_no_pairs_but_counts(self):
        # DECISION: a one-event case contributes to activity frequency
        # and case coverage but never to any directly-follows pair.
        base = pd.Timestamp("2026-01-01")
        df = pd.DataFrame([
            {"case:concept:name": "c0", "concept:name": "A",
             "time:timestamp": base},
            {"case:concept:name": "c1", "concept:name": "A",
             "time:timestamp": base + pd.Timedelta(minutes=5)},
            {"case:concept:name": "c1", "concept:name": "B",
             "time:timestamp": base + pd.Timedelta(minutes=15)},
        ])
        stats = compute_performance_stats(df)
        assert stats.activity["A"]["frequency"] == 2
        assert stats.activity["A"]["case_coverage"] == 2
        assert stats.pairs[("A", "B")]["frequency"] == 1
        # c0's lone A has no successor -> no (A, ?) pair from it.
        assert stats.total_cases == 2

    def test_timezone_aware_timestamps_supported(self):
        base = pd.Timestamp("2026-01-01 00:00:00", tz="UTC")
        df = pd.DataFrame([
            {"case:concept:name": "c0", "concept:name": "A",
             "time:timestamp": base},
            {"case:concept:name": "c0", "concept:name": "B",
             "time:timestamp": base + pd.Timedelta(minutes=10)},
        ])
        stats = compute_performance_stats(df)
        assert stats.pairs[("A", "B")]["mean_time"] == 600.0


# ===========================================================================
# Edge-attribution aggregation (segments spanning multiple DFG pairs)
# ===========================================================================
class TestEdgeAttribution:
    """An OR-fork sitting right after a join has a *set* of predecessor
    activities, so its branch segment aggregates several directly-
    follows pairs. The aggregation must frequency-weight the mean, sum
    the total, and drop the median (medians do not combine)."""

    def _stats_with_pairs(self, pairs):
        from pm4py_ucm.algo.performance import PerformanceStats
        s = PerformanceStats(total_cases=10)
        s.pairs = pairs
        return s

    def test_multi_pair_segment_weighted_mean_median_dropped(self):
        from pm4py_ucm.algo.performance import _aggregate_pair_stats
        stats = self._stats_with_pairs({
            ("B", "E"): {"frequency": 3, "mean_time": 100.0,
                         "median_time": 100.0, "total_time": 300.0,
                         "min_time": 50.0, "max_time": 150.0},
            ("C", "E"): {"frequency": 1, "mean_time": 200.0,
                         "median_time": 200.0, "total_time": 200.0,
                         "min_time": 200.0, "max_time": 200.0},
        })
        agg = _aggregate_pair_stats(stats, {"B", "C"}, {"E"})
        assert agg["frequency"] == 4
        assert agg["total_time"] == 500.0
        # Frequency-weighted mean: (100*3 + 200*1) / 4 = 125, NOT the
        # arithmetic mean 150 of the two pair means.
        assert agg["mean_time"] == 125.0
        # min and max combine across the two pairs.
        assert agg["min_time"] == 50.0
        assert agg["max_time"] == 200.0
        # Two pairs contribute -> median is not defined and is dropped.
        assert "median_time" not in agg

    def test_single_pair_segment_keeps_median(self):
        from pm4py_ucm.algo.performance import _aggregate_pair_stats
        stats = self._stats_with_pairs({
            ("B", "E"): {"frequency": 3, "mean_time": 100.0,
                         "median_time": 90.0, "total_time": 300.0,
                         "min_time": 50.0, "max_time": 150.0,
                         "case_frequency": 2},
        })
        agg = _aggregate_pair_stats(stats, {"B"}, {"E"})
        assert agg["median_time"] == 90.0
        # Single-pair segment carries the pair's case_frequency through.
        assert agg["case_frequency"] == 2


# ===========================================================================
# Additional metrics: rework, relative frequency, start/end,
# edge case-frequency, percentiles + std
# ===========================================================================
class TestReworkMetrics:

    def _rework_log(self):
        base = pd.Timestamp("2026-01-01")
        rows, t = [], base
        for cid, trace in (("c0", ["A", "A", "B"]),   # A repeats
                           ("c1", ["A", "B", "B"])):   # B repeats
            for a in trace:
                rows.append({"case:concept:name": cid, "concept:name": a,
                             "time:timestamp": t})
                t += pd.Timedelta(minutes=5)
        return pd.DataFrame(rows)

    def test_activity_repeat_frequency(self):
        stats = compute_performance_stats(self._rework_log())
        # A: 3 events across 2 cases -> 1 repeat execution.
        assert stats.activity["A"]["frequency"] == 3
        assert stats.activity["A"]["case_coverage"] == 2
        assert stats.activity["A"]["repeat_frequency"] == 1
        assert stats.activity["B"]["repeat_frequency"] == 1

    def test_repeat_frequency_identity_invariant(self):
        stats = compute_performance_stats(_random_interval_log(30))
        for e in stats.activity.values():
            assert e["repeat_frequency"] == e["frequency"] - e["case_coverage"]

    def test_process_rework_stats(self):
        from pm4py_ucm.algo.discovery.families.stats import _rework_stats
        rw = _rework_stats(self._rework_log(),
                           "case:concept:name", "concept:name")
        # Both cases contain a repeat; each has exactly one repeat event.
        assert rw["case_fraction"] == 1.0
        assert rw["mean_repeats_per_case"] == 1.0


class TestRelativeFrequencyAndStartEnd:

    def test_activity_relative_frequency_sums_to_one(self):
        stats = compute_performance_stats(_random_interval_log(31))
        total = sum(e["relative_frequency"] for e in stats.activity.values())
        assert total == pytest.approx(1.0)

    def test_edge_relative_frequency_sums_to_one(self):
        stats = compute_performance_stats(_random_interval_log(32))
        total = sum(p["relative_frequency"] for p in stats.pairs.values())
        assert total == pytest.approx(1.0)

    def test_start_end_sum_to_case_count(self):
        df = _random_interval_log(33)
        stats = compute_performance_stats(df)
        n_cases = df["case:concept:name"].nunique()
        assert sum(stats.start_activities.values()) == n_cases
        assert sum(stats.end_activities.values()) == n_cases

    def test_start_end_match_pm4py(self):
        pm4py = pytest.importorskip("pm4py")
        df = _random_interval_log(34, n_cases=20)
        cdf = df[["case:concept:name", "concept:name",
                  "time:timestamp"]].copy()
        stats = compute_performance_stats(cdf)
        log = pm4py.format_dataframe(
            cdf, case_id="case:concept:name",
            activity_key="concept:name", timestamp_key="time:timestamp")
        assert stats.start_activities == {
            str(k): int(v) for k, v in pm4py.get_start_activities(log).items()}
        assert stats.end_activities == {
            str(k): int(v) for k, v in pm4py.get_end_activities(log).items()}


class TestEdgeCaseFrequency:

    def test_case_frequency_counts_distinct_cases(self):
        base = pd.Timestamp("2026-01-01")
        rows, t = [], base
        # c0 traverses A->B twice; c1 once. frequency 3, case_frequency 2.
        for cid, trace in (("c0", ["A", "B", "A", "B"]), ("c1", ["A", "B"])):
            for a in trace:
                rows.append({"case:concept:name": cid, "concept:name": a,
                             "time:timestamp": t})
                t += pd.Timedelta(minutes=5)
        stats = compute_performance_stats(pd.DataFrame(rows))
        assert stats.pairs[("A", "B")]["frequency"] == 3
        assert stats.pairs[("A", "B")]["case_frequency"] == 2

    def test_case_frequency_bounded_by_frequency(self):
        stats = compute_performance_stats(_random_interval_log(35))
        for p in stats.pairs.values():
            assert p["case_frequency"] <= p["frequency"]


class TestPercentilesAndStd:

    def test_hand_computed_percentiles_and_std(self):
        # A service seconds [60, 120, 360] (linear interpolation, ddof=1).
        stats = compute_performance_stats(_distinct_log(intervals=True))
        a = stats.activity["A"]
        assert a["p90_time"] == pytest.approx(312.0)   # 120 + .8*(360-120)
        assert a["p95_time"] == pytest.approx(336.0)   # 120 + .9*(360-120)
        assert a["std_time"] == pytest.approx(158.7450787, abs=1e-5)

    def test_percentile_ordering_invariant(self):
        stats = compute_performance_stats(_random_interval_log(36))
        for e in stats.activity.values():
            if "p90_time" in e:
                assert e["min_time"] <= e["p90_time"] <= e["p95_time"] \
                    <= e["max_time"]
            if "std_time" in e:
                assert e["std_time"] >= 0
        for p in stats.pairs.values():
            if "p90_time" in p:
                assert p["min_time"] <= p["p90_time"] <= p["p95_time"] \
                    <= p["max_time"]

    def test_scale_time_scales_percentiles_and_std(self):
        df = _random_interval_log(37)
        base = compute_performance_stats(df)
        scaled = df.copy()
        origin = pd.Timestamp("2026-03-01")
        for col in ("start_timestamp", "time:timestamp"):
            scaled[col] = origin + (scaled[col] - origin) * 4
        after = compute_performance_stats(scaled)
        for act, e in base.activity.items():
            if "p90_time" in e:
                assert after.activity[act]["p90_time"] == pytest.approx(
                    4 * e["p90_time"])
                if "std_time" in e:
                    assert after.activity[act]["std_time"] == pytest.approx(
                        4 * e["std_time"])

    def test_case_duration_percentiles_match_numpy_on_pm4py(self):
        pm4py = pytest.importorskip("pm4py")
        import numpy as np
        df = _random_interval_log(38, n_cases=25)
        cdf = df[["case:concept:name", "concept:name",
                  "time:timestamp"]].copy()
        dur = _process_duration(cdf)
        log = pm4py.format_dataframe(
            cdf, case_id="case:concept:name",
            activity_key="concept:name", timestamp_key="time:timestamp")
        durs = pm4py.get_all_case_durations(log)
        assert dur["p90"] == pytest.approx(float(np.percentile(durs, 90)))
        assert dur["p95"] == pytest.approx(float(np.percentile(durs, 95)))
        assert dur["std"] == pytest.approx(float(np.std(durs, ddof=1)))


# ===========================================================================
# New metrics reach the .jucm metadata + the overlay menus
# ===========================================================================
class TestJucmExportOfNewMetrics:

    def test_new_metrics_written_as_jucm_metadata(self):
        from pm4py_ucm import convert_to_ucm
        from pm4py_ucm.algo.performance import annotate_performance
        from pm4py_ucm.objects.ucm.exporter.variants.jucm import (
            serialize_to_string,
        )
        tree = T("->", children=[T(label="A"), T(label="B"), T(label="C")])
        ucm = convert_to_ucm(tree)
        # Only frequency is *shown*, but every metric is exported.
        annotate_performance(ucm, _distinct_log(intervals=True),
                             node_metrics=["frequency"],
                             edge_metrics=["frequency"])
        text = serialize_to_string(ucm)
        for key in ("perf_relative_frequency", "perf_repeat_frequency",
                    "perf_min_time", "perf_max_time", "perf_std_time",
                    "perf_p90_time", "perf_p95_time",
                    "perf_sojourn_p90_time"):
            assert f'name="{key}"' in text, key
        # Edge (single-pair) segments carry the new edge metrics too.
        for key in ("perf_branch0_case_frequency",
                    "perf_branch0_relative_frequency",
                    "perf_branch0_min_time", "perf_branch0_p90_time"):
            assert f'name="{key}"' in text, key
        # A appears once per case -> repeat_frequency 0, relative 3/9.
        assert '<metadata name="perf_repeat_frequency" value="0"/>' in text
        assert ('<metadata name="perf_relative_frequency" '
                'value="33.3%"/>' in text)

    def test_new_metrics_selectable_in_overlay_menus(self):
        from pm4py_ucm.algo.performance import NODE_METRICS, EDGE_METRICS
        for m in ("relative_frequency", "repeat_frequency", "min_time",
                  "max_time", "std_time", "p90_time", "p95_time",
                  "sojourn_std_time"):
            assert m in NODE_METRICS
        for m in ("case_frequency", "relative_frequency", "min_time",
                  "std_time", "p90_time", "p95_time"):
            assert m in EDGE_METRICS


# ===========================================================================
# Differential vs pm4py (single-timestamp DFG)
# ===========================================================================
class TestDifferentialPm4py:

    def test_dfg_frequency_and_waiting_match_pm4py(self):
        pm4py = pytest.importorskip("pm4py")
        df = _random_interval_log(21, n_cases=25)
        # Single-timestamp view: pm4py's performance DFG then measures
        # completion -> completion, matching our single-ts semantics.
        cdf = df[["case:concept:name", "concept:name",
                  "time:timestamp"]].copy()
        stats = compute_performance_stats(cdf)

        log = pm4py.convert_to_event_log(pm4py.format_dataframe(
            cdf, case_id="case:concept:name",
            activity_key="concept:name", timestamp_key="time:timestamp"))
        freq_dfg = pm4py.discover_dfg(log)[0]
        perf_dfg, _, _ = pm4py.discovery.discover_performance_dfg(log)

        for edge, f in freq_dfg.items():
            assert stats.pairs[edge]["frequency"] == f
        agg = [("mean", "mean_time"), ("median", "median_time"),
               ("min", "min_time"), ("max", "max_time"),
               ("sum", "total_time")]
        for edge, v in perf_dfg.items():
            for pk, mk in agg:
                assert stats.pairs[edge][mk] == pytest.approx(
                    float(v[pk]), abs=1e-3)
            # Sample std (ddof=1) reconciles with pm4py's 'stdev' when
            # the edge has more than one occurrence.
            if "std_time" in stats.pairs[edge] and "stdev" in v:
                assert stats.pairs[edge]["std_time"] == pytest.approx(
                    float(v["stdev"]), rel=1e-6)
