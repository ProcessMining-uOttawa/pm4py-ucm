"""Tests for telling a truncated replay apart from a trace that cannot fit.

Both outcomes return ``NOFIT``, and before this distinction existed the two
were indistinguishable — so a search that ran out of budget was reported as
a fitness result, i.e. a timeout masquerading as a statement about the
model. These tests pin the separation, not the size of the budget.
"""
import pandas as pd
import pytest

from pm4py_ucm.algo.discovery.variants import choice_signature as _cs
from pm4py_ucm.algo.discovery.variants import clustering as _clustering
from pm4py_ucm.algo.discovery.variants import parses as _parses

CASE, ACT, TS = "case:concept:name", "concept:name", "time:timestamp"


def _log(sequences):
    rows, cid = [], 0
    for seq, count in sequences.items():
        for _ in range(count):
            for i, act in enumerate(seq):
                rows.append({
                    CASE: str(cid), ACT: act,
                    TS: pd.Timestamp("2026-01-01") + pd.Timedelta(minutes=i),
                })
            cid += 1
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def log_and_tree():
    pm4py = pytest.importorskip("pm4py")
    log = _log({"abcd": 40, "abd": 10, "acbd": 20, "axd": 3})
    return log, pm4py.discover_process_tree_inductive(log, noise_threshold=0.4)


# ------------------------------------------------------------------ replay()

def test_stats_is_optional(log_and_tree):
    """The signature must not change for existing callers."""
    _, tree = log_and_tree
    assert _cs.replay(tree, ["a", "b", "c", "d"]) != _cs.NOFIT


def test_fitting_trace_reports_no_exhaustion(log_and_tree):
    _, tree = log_and_tree
    stats = {}
    sig = _cs.replay(tree, ["a", "b", "c", "d"], stats=stats)
    assert sig != _cs.NOFIT
    assert stats["budget_exhausted"] is False


def test_starved_search_reports_exhaustion(log_and_tree):
    _, tree = log_and_tree
    stats = {}
    sig = _cs.replay(tree, ["a", "b", "c", "d"], max_replay_states=1,
                     stats=stats)
    # Still NOFIT — the return contract is unchanged...
    assert sig == _cs.NOFIT
    # ...but the caller can now see it was truncated, not refuted.
    assert stats["budget_exhausted"] is True


def test_unfittable_trace_is_not_blamed_on_the_budget(log_and_tree):
    """A trace the tree cannot produce must not be reported as a timeout,
    which is the misdiagnosis this whole distinction exists to prevent."""
    _, tree = log_and_tree
    stats = {}
    sig = _cs.replay(tree, ["z", "z", "z"], stats=stats)
    assert sig == _cs.NOFIT
    assert stats["budget_exhausted"] is False


# ------------------------------------------------------------- parse table

def test_parse_table_records_the_reason(log_and_tree):
    _, tree = log_and_tree
    seqs = [("a", "b", "c", "d"), ("z", "z", "z")]

    generous = _parses.replay_sequences(tree, seqs)
    assert generous.parses[("z", "z", "z")].budget_exhausted is False

    starved = _parses.replay_sequences(tree, seqs, max_replay_states=1)
    assert starved.parses[("a", "b", "c", "d")].budget_exhausted is True


# --------------------------------------------------------------- clustering

def test_clustering_separates_gave_up_from_does_not_fit(log_and_tree):
    log, tree = log_and_tree

    normal = _clustering.cluster(log, tree)
    # Some cases legitimately do not fit this deliberately noisy tree...
    assert len(normal.noise_case_ids) > 0
    # ...and none of them are budget casualties.
    assert normal.budget_exhausted_case_ids == []

    starved = _clustering.cluster(log, tree, max_replay_states=1)
    assert len(starved.budget_exhausted_case_ids) > 0
    # Gave-up cases are a subset of noise, never a separate population.
    assert (set(starved.budget_exhausted_case_ids)
            <= set(starved.noise_case_ids))


def test_max_replay_states_is_ignored_when_a_table_is_supplied(log_and_tree):
    """The table was built under its own budget; re-declaring it here must
    not silently imply a different one."""
    log, tree = log_and_tree
    table = _parses.replay_sequences(
        tree, [tuple(t) for t in log.groupby(CASE)[ACT].apply(tuple)])
    result = _clustering.cluster(log, tree, parses=table,
                                 max_replay_states=1)
    assert result.budget_exhausted_case_ids == []
    assert result.fitness_percentage > 0
