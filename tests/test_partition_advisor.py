"""Tests for the deterministic partition advisor (docs/ai_insights.md §4.1b)."""
from __future__ import annotations

import pytest

pd = pytest.importorskip("pandas")

from pm4py_ucm.algo.discovery.families.advisor import (  # noqa: E402
    _case_durations,
    _eta_squared,
    _uncertainty_coefficient,
    rank_partition_attributes,
)


def _log():
    """120 cases. ``route`` fully determines the path (A: 3 acts, B: 4) — a
    perfect control-flow determinant; ``noise`` is random and irrelevant;
    ``const`` is constant; ``cid`` is a per-case identifier."""
    base = pd.Timestamp("2026-01-01", tz="UTC")
    rows = []
    for i in range(120):
        cid = f"c{i:03d}"
        route = "A" if i % 2 == 0 else "B"
        acts = (["Start", "Approve", "End"] if route == "A"
                else ["Start", "Reject", "Notify", "End"])
        for j, a in enumerate(acts):
            rows.append({
                "case:concept:name": cid, "concept:name": a,
                "time:timestamp": base + pd.Timedelta(hours=i * 24 + j),
                "case:route": route, "case:noise": ["x", "y", "z"][i % 3],
                "case:const": "K", "case:cid": cid})
    return pd.DataFrame(rows)


def _by_label(scores):
    return {s.label: s for s in scores}


def test_true_determinant_ranks_first():
    scores = rank_partition_attributes(_log())
    assert scores, "expected at least one ranked attribute"
    assert scores[0].label == "route"
    top = _by_label(scores)["route"]
    assert top.divergence > 0.95          # explains ~all variant variation
    assert top.score > 0.9
    assert not top.flags


def test_irrelevant_attribute_scores_low():
    by = _by_label(rank_partition_attributes(_log()))
    assert "noise" in by
    assert by["noise"].divergence < 0.05
    assert by["noise"].score < by["route"].score


def test_constant_and_identifier_are_excluded_or_flagged():
    by = _by_label(rank_partition_attributes(_log()))
    # A single-valued attribute is not a usable partition axis; a per-case
    # identifier is filtered upstream (cardinality) — neither should rank as a
    # real suggestion. If either survives detection it must be flagged, not #1.
    assert "const" not in by
    if "cid" in by:
        assert by["cid"].flags and by["cid"].score < 0.3


def test_ranking_is_sorted_descending():
    scores = rank_partition_attributes(_log())
    assert [s.score for s in scores] == sorted(
        (s.score for s in scores), reverse=True)


def test_no_case_attributes_returns_empty():
    base = pd.Timestamp("2026-01-01", tz="UTC")
    df = pd.DataFrame([
        {"case:concept:name": "c0", "concept:name": "A",
         "time:timestamp": base},
        {"case:concept:name": "c0", "concept:name": "B",
         "time:timestamp": base + pd.Timedelta(hours=1)},
    ])
    assert rank_partition_attributes(df) == []


def test_to_row_is_flat_and_serialisable():
    row = rank_partition_attributes(_log())[0].to_row()
    assert row["attribute"] == "route"
    assert set(row) >= {"attribute", "type", "values", "coverage_%",
                        "control_flow", "duration_effect", "score", "note"}


# --- the statistics helpers ------------------------------------------------

def test_uncertainty_coefficient_bounds():
    # Perfect dependence -> 1; independence -> ~0.
    x = [0, 0, 1, 1, 2, 2]
    assert _uncertainty_coefficient(x, x) == pytest.approx(1.0)
    indep_attr = [0, 1] * 50
    indep_var = [0, 0, 1, 1] * 25
    assert _uncertainty_coefficient(indep_attr, indep_var) < 0.05


def test_eta_squared_bounds():
    # Group fully explains the value -> 1; no separation -> 0.
    perfect = _eta_squared([1, 1, 9, 9], ["a", "a", "b", "b"])
    assert perfect == pytest.approx(1.0)
    none = _eta_squared([5, 5, 5, 5], ["a", "b", "a", "b"])
    assert none == 0.0


# -- case duration: earliest start to latest completion --------------------

def _interval_rows(lead_by_kind):
    """Eight cases, two events each, completing at hour 2 and hour 10 —
    so every case lasts 8 h between completions. The first event's own
    duration (its lead-in) depends on ``case:kind``."""
    base = pd.Timestamp("2026-01-01", tz="UTC")
    rows = []
    for i in range(8):
        kind = "slow" if i < 4 else "fast"
        day = base + pd.Timedelta(days=i)
        for act, end, dur in (("A", 2, lead_by_kind[kind]), ("B", 10, 1)):
            rows.append({
                "case:concept:name": f"c{i}", "concept:name": act,
                "time:timestamp": day + pd.Timedelta(hours=end),
                "start_timestamp": day + pd.Timedelta(hours=end - dur),
                "case:kind": kind, "case:parity": "even" if i % 2 else "odd"})
    return pd.DataFrame(rows)


def test_case_duration_on_an_interval_log_starts_at_the_earliest_start():
    """By hand: completions at hours 2 and 10; A starts 20 h (slow) or 1 h
    (fast) before it completes. Earliest start to latest completion is
    8 + 20 = 28 h and 8 + 1 = 9 h; first-to-last completion would be 8 h
    for every case."""
    df = _interval_rows({"slow": 20, "fast": 1})
    dur = _case_durations(df, "case:concept:name", "time:timestamp")
    assert list(dur / 3600) == [28.0] * 4 + [9.0] * 4


def test_case_duration_without_start_timestamps_is_first_to_last_event():
    df = _interval_rows({"slow": 20, "fast": 1}).drop(
        columns="start_timestamp")
    dur = _case_durations(df, "case:concept:name", "time:timestamp")
    assert list(dur / 3600) == [8.0] * 8


def test_duration_effect_sees_the_first_activitys_lead_in():
    """``kind`` changes nothing but how long the first activity runs. With
    durations measured between completions every case lasted 8 h and the
    effect size was 0; it is now 1 (all the variance lies between the two
    kinds)."""
    by = _by_label(rank_partition_attributes(
        _interval_rows({"slow": 20, "fast": 1})))
    assert by["kind"].effect == pytest.approx(1.0)
    assert by["parity"].effect == pytest.approx(0.0, abs=1e-9)
