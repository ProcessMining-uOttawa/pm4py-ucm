"""Tests for the discovery/replay cost screening in ``algo.complexity``.

The thresholds themselves are empirical (see ``docs/miner_performance.md``);
what is tested here is the contract around them -- that the profile counts
what it claims, that the screen fires on either trigger independently, and
that the replay probe either finishes the work or extrapolates it, never
silently doing neither.
"""
import pandas as pd
import pytest

from pm4py_ucm.algo import complexity as cx


def _log(sequences, case_offset=0):
    """Build a minimal XES-named DataFrame from ``{sequence: count}``."""
    rows = []
    cid = case_offset
    for seq, count in sequences.items():
        for _ in range(count):
            for i, act in enumerate(seq):
                rows.append({
                    cx.CASE_KEY: str(cid),
                    cx.ACTIVITY_KEY: act,
                    cx.TIMESTAMP_KEY: (pd.Timestamp("2026-01-01")
                                       + pd.Timedelta(minutes=i)),
                })
            cid += 1
    return pd.DataFrame(rows)


# --------------------------------------------------------------------- profile

def test_profile_counts_cases_activities_and_variants():
    log = _log({"abcd": 60, "abd": 20, "acbd": 40})
    p = cx.profile_log(log)
    assert p.cases == 120
    assert p.events == 60 * 4 + 20 * 3 + 40 * 4
    assert p.activities == 4          # a, b, c, d
    assert p.seq_variants == 3
    assert p.trace_len_median == 4.0
    assert p.trace_len_max == 4


def test_variant_ratio_flags_all_unique_logs():
    # Every case its own sequence: the shape that makes mining expensive.
    unique = {"a" * n: 1 for n in range(2, 12)}
    p = cx.profile_log(_log(unique))
    assert p.variant_ratio == 1.0

    p2 = cx.profile_log(_log({"abc": 50}))
    assert p2.variant_ratio == pytest.approx(1 / 50)


def test_profile_accepts_non_xes_column_names():
    log = _log({"abc": 5}).rename(columns={
        cx.CASE_KEY: "cid", cx.ACTIVITY_KEY: "act", cx.TIMESTAMP_KEY: "ts"})
    p = cx.profile_log(log, case_id_key="cid", activity_key="act",
                       timestamp_key="ts")
    assert p.cases == 5 and p.activities == 3


def test_profile_orders_events_by_timestamp():
    """Variants must not depend on row order in the source file."""
    log = _log({"abc": 4})
    p_ordered = cx.profile_log(log)
    p_shuffled = cx.profile_log(log.sample(frac=1.0, random_state=7))
    assert p_ordered.seq_variants == p_shuffled.seq_variants == 1


# ---------------------------------------------------------------------- screen

def test_screen_is_quiet_on_a_small_log():
    risk = cx.screen_mining(cx.profile_log(_log({"abcd": 10})))
    assert risk.high is False
    assert risk.triggers == []


def test_each_threshold_fires_independently():
    p = cx.profile_log(_log({"abcd": 10}))

    by_variants = cx.screen_mining(p, variant_limit=0, activity_limit=999)
    assert by_variants.high and len(by_variants.triggers) == 1
    assert "sequence" in by_variants.triggers[0]

    by_activities = cx.screen_mining(p, variant_limit=999, activity_limit=0)
    assert by_activities.high and len(by_activities.triggers) == 1
    assert "activities" in by_activities.triggers[0]

    both = cx.screen_mining(p, variant_limit=0, activity_limit=0)
    assert both.high and len(both.triggers) == 2


def test_screen_reason_never_promises_a_duration():
    """Mining time is not predictable from these statistics; the reason
    text must not imply otherwise."""
    p = cx.profile_log(_log({"abcd": 10}))
    for risk in (cx.screen_mining(p),
                 cx.screen_mining(p, variant_limit=0, activity_limit=0)):
        assert "second" not in risk.reason.lower()
        assert "minute" not in risk.reason.lower()


def test_screen_calls_out_all_unique_logs():
    unique = {"a" * n: 1 for n in range(2, 30)}
    risk = cx.screen_mining(cx.profile_log(_log(unique)),
                            variant_limit=0, activity_limit=999)
    assert "unique" in risk.reason


# ---------------------------------------------------------------------- replay

@pytest.fixture(scope="module")
def log_and_tree():
    pm4py = pytest.importorskip("pm4py")
    log = _log({"abcd": 60, "abd": 20, "acbd": 40, "axd": 5})
    return log, pm4py.discover_process_tree_inductive(log)


def test_cheap_replay_is_completed_not_estimated(log_and_tree):
    log, tree = log_and_tree
    est = cx.estimate_replay(log, tree, time_budget=60.0)
    assert est.complete is True
    assert est.sampled_cases == est.total_cases
    # A completed probe hands back the real clustering: nothing to redo.
    assert est.clustering is not None
    assert len(est.clustering.variants) > 0


def test_zero_budget_forces_an_extrapolation(log_and_tree):
    log, tree = log_and_tree
    est = cx.estimate_replay(log, tree, time_budget=0.0, probe_cases=10)
    assert est.complete is False
    assert est.sampled_cases == 10
    assert est.total_cases == 125
    assert est.estimated_total_s > 0
    # Nothing was fully computed, so no clustering may be claimed.
    assert est.clustering is None


def test_probe_larger_than_the_log_is_exact(log_and_tree):
    log, tree = log_and_tree
    est = cx.estimate_replay(log, tree, probe_cases=10_000, time_budget=0.0)
    assert est.complete is True
    assert est.sampled_cases == est.total_cases == 125
    assert est.estimated_total_s == est.elapsed_s


def test_empty_log_is_not_an_error():
    empty = pd.DataFrame({cx.CASE_KEY: [], cx.ACTIVITY_KEY: [],
                          cx.TIMESTAMP_KEY: []})
    est = cx.estimate_replay(empty, tree=None)
    assert est.total_cases == 0 and est.complete and est.estimated_total_s == 0
