"""Tests for user-defined dashboards: the client contract, the metric
engine, and the parity of the Python engine with its JS counterpart.

The log fixtures are small and hand-computable on purpose — every
expected value below is one a reader can verify by counting, so a failure
names the broken semantic rather than just a changed number.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")

from pm4py_ucm.algo.dashboards import (
    CONTRACT_VERSION,
    aggregate,
    apply_filters,
    build_fact_table,
    bundle_script,
    catalog_json,
    compute_widget,
    dashboard_html,
    fmt,
    write_dashboard,
    per_case_values,
    scorecard,
    segment_axes,
    segment_keys,
    series_values,
    target_state,
    worst_state,
)
from pm4py_ucm.algo.dashboards.engine import (
    percentile,
    round_half_away,
    target_goal_value,
)
from pm4py_ucm.algo.dashboards.view import _script_body

DAY = 86400.0
REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "pm4py_ucm" / "algo" / "dashboards" / "assets"
ENGINE_JS = ASSETS / "dash-engine.js"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _log():
    """Four cases with deliberately distinct shapes:

    * ``c0`` A→B→C over 3 days, resource R1, country AUS
    * ``c1`` A→B→C over 6 days, resource R2, country NZL
    * ``c2`` A→C — skips B entirely, so B-scoped metrics have no value
    * ``c3`` A→B→A→C — rework on A, and B→A→C, so A repeats
    """
    rows = []

    def add(cid, act, day, res, country, hour=0):
        rows.append({
            "case:concept:name": cid,
            "concept:name": act,
            "time:timestamp": pd.Timestamp("2026-01-05", tz="UTC")
                              + pd.Timedelta(days=day, hours=hour),
            "org:resource": res,
            "case:country": country,
        })

    # 2026-01-05 is a Monday.
    add("c0", "A", 0, "R1", "AUS"); add("c0", "B", 1, "R1", "AUS")
    add("c0", "C", 3, "R1", "AUS")
    add("c1", "A", 0, "R2", "NZL"); add("c1", "B", 2, "R2", "NZL")
    add("c1", "C", 6, "R2", "NZL")
    add("c2", "A", 0, "R1", "AUS"); add("c2", "C", 4, "R1", "AUS")
    add("c3", "A", 0, "R2", "NZL"); add("c3", "B", 1, "R2", "NZL")
    add("c3", "A", 2, "R2", "NZL"); add("c3", "C", 8, "R2", "NZL")
    return pd.DataFrame(rows)


@pytest.fixture()
def table():
    return build_fact_table(_log(), log_name="t")


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

class TestContract:

    def test_shape_and_dictionaries(self, table):
        assert table.n_cases == 4
        assert table.n_events == 12
        assert table.activities == ["A", "B", "C"]
        assert table.resources == ["R1", "R2"]
        assert table.interval_log is False
        assert table.dropped_events == 0

    def test_csr_offsets_address_each_case(self, table):
        assert list(table.offsets) == [0, 3, 6, 8, 12]
        assert table.offsets[-1] == table.n_events

    def test_case_attribute_detected(self, table):
        attr = table.attribute("case:country")
        assert attr is not None
        assert attr.type == "enumeration"
        assert attr.values == ["AUS", "NZL"]

    def test_payload_round_trips_through_base64(self, table):
        payload = table.to_payload()
        assert payload["version"] == CONTRACT_VERSION
        import base64
        for key, buf in payload["buffers"].items():
            raw = base64.b64decode(buf["b64"])
            got = np.frombuffer(raw, dtype=buf["dtype"])
            assert np.array_equal(got, table.buffers[key]), key

    def test_unparseable_timestamps_are_dropped_not_absorbed(self):
        """A NaT must not become a case's start; it sentinels to int64
        min and would silently corrupt every duration in that case."""
        df = _log()
        df.loc[1, "time:timestamp"] = pd.NaT
        t = build_fact_table(df)
        assert t.dropped_events == 1
        assert t.n_events == 11
        # c0 is now A→C; its start is still the A event, not a sentinel.
        assert per_case_values(t, "duration")[0] == pytest.approx(3.0)

    def test_sampling_records_the_true_population(self):
        t = build_fact_table(_log(), max_payload_bytes=1, sample_cases=2)
        assert t.sampled is True
        assert t.sampled_from == 4
        assert t.n_cases == 2

    def test_sampling_is_deterministic(self):
        a = build_fact_table(_log(), max_payload_bytes=1, sample_cases=2)
        b = build_fact_table(_log(), max_payload_bytes=1, sample_cases=2)
        assert np.array_equal(a.starts, b.starts)


# ---------------------------------------------------------------------------
# Per-case metrics
# ---------------------------------------------------------------------------

class TestPerCaseValues:

    def test_duration(self, table):
        assert list(per_case_values(table, "duration")) == [3.0, 6.0, 4.0, 8.0]

    def test_event_count(self, table):
        assert list(per_case_values(table, "eventCount")) == [3, 3, 2, 4]

    def test_time_between(self, table):
        v = per_case_values(table, "timeBetween", {"from": "A", "to": "B"})
        assert v[0] == 1.0 and v[1] == 2.0 and v[3] == 1.0
        # c2 never runs B: no value, not a zero.
        assert np.isnan(v[2])

    def test_time_between_uses_the_to_that_follows_the_from(self, table):
        """c3 is A→B→A→C. B→A must find the *second* A, at day 2."""
        v = per_case_values(table, "timeBetween", {"from": "B", "to": "A"})
        assert v[3] == pytest.approx(1.0)
        # Cases without B→A contribute no value.
        assert all(np.isnan(v[i]) for i in (0, 1, 2))

    def test_time_between_missing_is_excluded_from_the_average(self, table):
        v = per_case_values(table, "timeBetween", {"from": "A", "to": "B"})
        # (1 + 2 + 1) / 3, not / 4 — c2 leaves the denominator.
        assert aggregate(v, "avg") == pytest.approx(4 / 3)

    def test_zero_length_transition_is_a_value_not_a_gap(self):
        """Two ordered events in the same second must read as 0 days, not
        as 'never happened' — the sub-second-transition bug."""
        df = pd.DataFrame([
            {"case:concept:name": "x", "concept:name": "A",
             "time:timestamp": pd.Timestamp("2026-01-01 00:00:00.100",
                                            tz="UTC")},
            {"case:concept:name": "x", "concept:name": "B",
             "time:timestamp": pd.Timestamp("2026-01-01 00:00:00.400",
                                            tz="UTC")},
        ])
        t = build_fact_table(df)
        v = per_case_values(t, "timeBetween", {"from": "A", "to": "B"})
        assert v[0] == 0.0
        assert not np.isnan(v[0])

    def test_rework(self, table):
        # Only c3 repeats an activity (A twice).
        assert list(per_case_values(table, "rework")) == [0, 0, 0, 1]
        assert aggregate(per_case_values(table, "rework"), "share") == 25.0

    def test_act_freq_and_presence_and_repeats(self, table):
        assert list(per_case_values(table, "actFreq", {"activity": "A"})) \
            == [1, 1, 1, 2]
        assert list(per_case_values(table, "actPresence", {"activity": "B"})) \
            == [1, 1, 0, 1]
        assert list(per_case_values(table, "actRepeats", {"activity": "A"})) \
            == [0, 0, 0, 1]

    def test_act_presence_counts_absence_as_zero_not_missing(self, table):
        """Presence is a property of every case, so c2 is a 0 and stays in
        the denominator — unlike a duration, which it has no value for."""
        v = per_case_values(table, "actPresence", {"activity": "B"})
        assert aggregate(v, "share") == 75.0

    def test_act_sojourn_is_case_weighted(self, table):
        """c3 runs A at day 0 (first event, no sojourn) and day 2 (2 days
        after B). Its per-case value is the mean over its own occurrences
        — one value, not two."""
        v = per_case_values(table, "actSojourn", {"activity": "A"})
        assert np.isnan(v[0])  # c0's only A is its first event
        assert v[3] == pytest.approx(1.0)  # day 2 minus B at day 1

    def test_edge_freq(self, table):
        v = per_case_values(table, "edgeFreq", {"from": "A", "to": "B"})
        assert list(v) == [1, 1, 0, 1]

    def test_edge_time(self, table):
        v = per_case_values(table, "edgeTime", {"from": "B", "to": "C"})
        assert v[0] == pytest.approx(2.0)
        assert v[1] == pytest.approx(4.0)
        assert np.isnan(v[2])

    def test_edge_share_excludes_cases_that_never_reach_the_fork(self, table):
        """A→B: c0, c1, c3 reach A and take it; c2 reaches A and does not.
        Every case reaches A, so the share is 3/4."""
        v = per_case_values(table, "edgeShare", {"from": "A", "to": "B"})
        assert list(v) == [1, 1, 0, 1]
        assert aggregate(v, "share") == 75.0

    def test_edge_share_denominator_is_cases_reaching_the_source(self, table):
        """B→C: c2 never runs B, so it is not in the denominator at all.
        c0 and c1 take B→C; c3 runs B→A, so it does not."""
        v = per_case_values(table, "edgeShare", {"from": "B", "to": "C"})
        assert np.isnan(v[2])
        assert aggregate(v, "share") == pytest.approx(200 / 3)

    def test_unknown_metric_raises(self, table):
        with pytest.raises(ValueError, match="Unknown metric"):
            per_case_values(table, "nope")

    def test_series_metric_rejected_by_per_case(self, table):
        with pytest.raises(ValueError, match="series metric"):
            per_case_values(table, "wip")


class TestServiceTimes:

    def test_service_time_needs_an_interval_log(self, table):
        v = per_case_values(table, "actService", {"activity": "A"})
        assert np.all(np.isnan(v))

    def test_service_and_waiting_on_an_interval_log(self):
        rows = []
        for act, day, dur in (("A", 0, 1), ("B", 4, 2)):
            rows.append({
                "case:concept:name": "c",
                "concept:name": act,
                "time:timestamp": pd.Timestamp("2026-01-01", tz="UTC")
                                  + pd.Timedelta(days=day),
                "start_timestamp": pd.Timestamp("2026-01-01", tz="UTC")
                                   + pd.Timedelta(days=day - dur),
            })
        t = build_fact_table(pd.DataFrame(rows))
        assert t.interval_log is True
        # B completes on day 4 having started on day 2: 2 days of service.
        assert per_case_values(t, "actService", {"activity": "B"})[0] \
            == pytest.approx(2.0)
        # B started day 2; the previous event (A) completed day 0.
        assert per_case_values(t, "actWaiting", {"activity": "B"})[0] \
            == pytest.approx(2.0)

    def test_start_after_completion_is_missing_not_clamped(self):
        """A corrupt interval must not become a fake zero-length service
        time that quietly drags the mean down."""
        rows = [{
            "case:concept:name": "c", "concept:name": "A",
            "time:timestamp": pd.Timestamp("2026-01-01", tz="UTC"),
            "start_timestamp": pd.Timestamp("2026-01-09", tz="UTC"),
        }]
        t = build_fact_table(pd.DataFrame(rows))
        assert np.isnan(per_case_values(t, "actService", {"activity": "A"})[0])


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

class TestAggregate:

    def test_basic_aggregations(self):
        v = [1.0, 2.0, 3.0, 4.0]
        assert aggregate(v, "avg") == 2.5
        assert aggregate(v, "sum") == 10.0
        assert aggregate(v, "min") == 1.0
        assert aggregate(v, "max") == 4.0

    def test_median_interpolates(self):
        """The package convention (docs/metrics.md) is numpy's linear
        interpolation, NOT the design prototype's nearest-rank."""
        assert aggregate([1.0, 2.0, 3.0, 4.0], "median") == 2.5

    def test_percentile_matches_numpy(self):
        rng = np.random.default_rng(3)
        for n in (1, 2, 5, 17, 100):
            v = rng.normal(size=n)
            for q in (0.5, 0.9, 0.95):
                assert percentile(v, q) == pytest.approx(
                    float(np.percentile(v, q * 100)))

    def test_missing_values_leave_the_denominator(self):
        assert aggregate([1.0, np.nan, 3.0], "avg") == 2.0

    def test_empty_is_none_not_zero(self):
        assert aggregate([], "avg") is None
        assert aggregate([np.nan], "avg") is None

    def test_share_is_percentage_of_nonzero(self):
        assert aggregate([1.0, 0.0, 1.0, 0.0], "share") == 50.0

    def test_unknown_aggregation_raises(self):
        with pytest.raises(ValueError, match="Unknown aggregation"):
            aggregate([1.0], "nope")


class TestFormatting:

    @pytest.mark.parametrize("value,unit,want", [
        (None, "d", "—"),
        (1.25, "d", "1.3 d"),      # half away from zero, not Python's 1.2
        (250.4, "d", "250 d"),     # no decimal past 100 days
        (1500.0, "d", "1,500 d"),
        (78.23, "%", "78.2%"),
        (1234.6, "n", "1,235"),
        (1234.5, "n", "1,235"),    # half away from zero, not Python's 1,234
        (-1.25, "d", "-1.3 d"),
    ])
    def test_fmt(self, value, unit, want):
        assert fmt(value, unit) == want

    def test_rounding_is_half_away_from_zero_not_bankers(self):
        """Both engines must print the same string for the same value, so
        the rule is explicit rather than each language's default."""
        assert round_half_away(5.25, 1) == pytest.approx(5.3)
        assert round_half_away(5.35, 1) == pytest.approx(5.4)
        assert round_half_away(1234.5) == 1235
        assert round_half_away(-0.5) == -1


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

class TestTargets:

    @pytest.mark.parametrize("value,want", [
        (10.0, "met"), (14.0, "met"), (16.0, "risk"),
        (18.0, "risk"), (20.0, "missed"),
    ])
    def test_at_most_target(self, value, want):
        t = {"on": True, "dir": "<=", "value": 14, "warn": 18}
        assert target_state(value, t) == want

    @pytest.mark.parametrize("value,want", [
        (95.0, "met"), (90.0, "met"), (85.0, "risk"), (70.0, "missed"),
    ])
    def test_at_least_target(self, value, want):
        t = {"on": True, "dir": ">=", "value": 90, "warn": 80}
        assert target_state(value, t) == want

    def test_no_target_no_state(self):
        assert target_state(5.0, None) is None
        assert target_state(5.0, {"on": False, "dir": "<=", "value": 1}) is None

    def test_no_value_no_state(self):
        assert target_state(None, {"on": True, "dir": "<=", "value": 1}) is None

    def test_worst_state_wins(self):
        assert worst_state(["met", "risk", "missed"]) == "missed"
        assert worst_state(["met", "risk"]) == "risk"
        assert worst_state(["met", None]) == "met"
        assert worst_state([None]) is None


# ---------------------------------------------------------------------------
# Filters and segmentation
# ---------------------------------------------------------------------------

class TestFilters:

    def test_contains(self, table):
        m = apply_filters(table, [{"field": "contains", "op": "is",
                                   "value": "B"}])
        assert list(m) == [True, True, False, True]

    def test_contains_negated(self, table):
        m = apply_filters(table, [{"field": "contains", "op": "not",
                                   "value": "B"}])
        assert list(m) == [False, False, True, False]

    def test_attribute_enum(self, table):
        m = apply_filters(table, [{"field": "attr:case:country",
                                   "op": "is", "value": "AUS"}])
        assert list(m) == [True, False, True, False]

    def test_resource(self, table):
        m = apply_filters(table, [{"field": "resource", "op": "is",
                                   "value": "R2"}])
        assert list(m) == [False, True, False, True]

    def test_date_range_includes_the_whole_final_period(self, table):
        m = apply_filters(table, [{"field": "date", "op": "between",
                                   "value": ["2026-01", "2026-01"]}])
        assert list(m) == [True, True, True, True]

    def test_filters_and_together(self, table):
        m = apply_filters(table, [
            {"field": "attr:case:country", "op": "is", "value": "NZL"},
            {"field": "contains", "op": "is", "value": "B"},
        ])
        assert list(m) == [False, True, False, True]

    def test_segment_drilldown_filter(self, table):
        m = apply_filters(table, [{"field": "segment", "op": "is",
                                   "value": ["resource", "R1"]}])
        assert list(m) == [True, False, True, False]

    def test_unknown_field_raises(self, table):
        with pytest.raises(ValueError, match="Unknown filter field"):
            apply_filters(table, [{"field": "bogus", "op": "is", "value": 1}])


class TestSegmentation:

    def test_axes_offered(self, table):
        ids = [a["id"] for a in segment_axes(table)]
        assert "resource" in ids and "variant" in ids
        assert "attr:case:country" in ids
        assert ids[:4] == ["year", "quarter", "month", "weekday"]

    def test_weekday_is_monday_first(self, table):
        """2026-01-05 is a Monday. A Sunday-first offset would label every
        case Tuesday."""
        codes, labels = segment_keys(table, "weekday")
        assert labels[0] == "Mon"
        assert [labels[c] for c in codes] == ["Mon"] * 4

    def test_month_and_quarter_labels(self, table):
        _, m = segment_keys(table, "month")
        assert m == ["2026-01"]
        _, q = segment_keys(table, "quarter")
        assert q == ["2026-Q1"]

    def test_resource(self, table):
        codes, labels = segment_keys(table, "resource")
        assert labels == ["R1", "R2"]
        assert [labels[c] for c in codes] == ["R1", "R2", "R1", "R2"]

    def test_attribute(self, table):
        codes, labels = segment_keys(table, "attr:case:country")
        assert labels == ["AUS", "NZL"]
        assert [labels[c] for c in codes] == ["AUS", "NZL", "AUS", "NZL"]

    def test_variants_ranked_by_frequency(self, table):
        codes, labels = segment_keys(table, "variant")
        # c0 and c1 share A→B→C; it must be v1.
        assert codes[0] == codes[1] == 0
        assert labels[0] == "v1"
        assert len(set(codes)) == 3

    def test_unknown_axis_raises(self, table):
        with pytest.raises(ValueError, match="Unknown segmentation axis"):
            segment_keys(table, "bogus")


# ---------------------------------------------------------------------------
# Discrete-integer (single-value) bins
# ---------------------------------------------------------------------------

def _priority_log(levels):
    """Log with an integer ``case:priority`` attribute; ``levels`` is
    ``[(value, n_cases), ...]``. One A→B→C case per id."""
    rows = []
    i = 0
    base = pd.Timestamp("2026-01-05", tz="UTC")
    for val, n in levels:
        for _ in range(n):
            cid = f"c{i:04d}"
            for k, act in enumerate(("A", "B", "C")):
                rows.append({
                    "case:concept:name": cid,
                    "concept:name": act,
                    "time:timestamp": base + pd.Timedelta(days=k),
                    "org:resource": "R1",
                    "case:priority": val,
                })
            i += 1
    return pd.DataFrame(rows)


class TestDiscreteIntegerBins:
    """A small set of whole-number values (e.g. priority levels) gets one
    bin per value, not quantile ranges — mirrors the family partitioner's
    _discrete_integer_values."""

    def test_bins_are_single_values(self):
        t = build_fact_table(
            _priority_log([(1, 2), (2, 3), (3, 2), (4, 1), (5, 2)]), bins=5)
        attr = t.attribute("case:priority")
        assert attr.type == "integer"
        assert [b.label for b in attr.bins] == ["1", "2", "3", "4", "5"]
        assert [(b.lo, b.hi) for b in attr.bins] == [
            (1.0, 1.0), (2.0, 2.0), (3.0, 3.0), (4.0, 4.0), (5.0, 5.0)]

    def test_segment_assigns_each_case_to_its_value(self):
        t = build_fact_table(_priority_log([(1, 2), (3, 1), (5, 2)]), bins=5)
        codes, labels = segment_keys(t, "attr:case:priority")
        assert labels == ["1", "3", "5"]
        assert [labels[c] for c in codes] == ["1", "1", "3", "5", "5"]

    def test_fewer_bins_than_values_keeps_ranges(self):
        t = build_fact_table(
            _priority_log([(1, 2), (2, 2), (3, 2), (4, 2), (5, 2)]), bins=2)
        attr = t.attribute("case:priority")
        assert len(attr.bins) <= 2
        assert any("–" in b.label for b in attr.bins)

    def test_js_parity_on_single_value_bins(self, tmp_path):
        t = build_fact_table(_priority_log([(1, 2), (3, 1), (5, 2)]), bins=5)
        spec = {"id": "prio", "metric": "eventCount", "viz": "bar",
                "agg": "sum", "segment": {"rows": "attr:case:priority"}}
        py = compute_widget(spec, t)
        js = _run_js(t.to_payload(), [spec], [[]],
                     catalog_json(interval_log=t.interval_log), tmp_path)[0]
        assert "error" not in js, js.get("error")
        assert [s["label"] for s in py["series"]] == ["1", "3", "5"]
        assert json.dumps(_norm(js["series"])) == \
            json.dumps(_norm(py["series"]))


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class TestWidgets:

    def test_kpi(self, table):
        w = compute_widget({"id": "w", "metric": "duration", "viz": "kpi",
                            "agg": "avg"}, table)
        assert w["value"] == pytest.approx(5.25)
        assert w["text"] == "5.3 d"
        assert w["nCases"] == 4

    def test_kpi_with_target(self, table):
        w = compute_widget({"id": "w", "metric": "duration", "viz": "kpi",
                            "agg": "avg",
                            "target": {"on": True, "dir": "<=", "value": 4,
                                       "warn": 6}}, table)
        assert w["state"] == "risk"
        assert w["sub"] == "target ≤ 4 d"

    def test_kpi_per_case_target_mode(self, table):
        """Durations are 3, 6, 4, 8. Against '<= 4 warn 6': two met, one
        risk, one missed."""
        w = compute_widget({"id": "w", "metric": "duration", "viz": "kpi",
                            "target": {"on": True, "dir": "<=", "value": 4,
                                       "warn": 6, "mode": "per_case",
                                       "shareGoal": 90}}, table)
        assert w["value"] == pytest.approx(50.0)
        assert w["unit"] == "%"
        assert w["distribution"] == {"met": 50.0, "risk": 25.0,
                                     "missed": 25.0}
        assert w["state"] == "missed"
        assert w["sub"] == "goal ≥ 90%"

    def test_dashboard_filters_stack_on_widget_filters(self, table):
        spec = {"id": "w", "metric": "duration", "viz": "kpi",
                "filter": [{"field": "contains", "op": "is", "value": "B"}]}
        w = compute_widget(spec, table, dashboard_filters=[
            {"field": "attr:case:country", "op": "is", "value": "NZL"}])
        # NZL and contains B => c1 and c3 only.
        assert w["nCases"] == 2
        assert w["value"] == pytest.approx(7.0)

    def test_bar_by_one_axis(self, table):
        w = compute_widget({"id": "w", "metric": "duration", "viz": "bar",
                            "agg": "avg",
                            "segment": {"rows": "resource"}}, table)
        assert [s["label"] for s in w["series"]] == ["R1", "R2"]
        assert w["series"][0]["value"] == pytest.approx(3.5)
        assert w["series"][1]["value"] == pytest.approx(7.0)

    def test_table_by_two_axes(self, table):
        w = compute_widget({"id": "w", "metric": "duration", "viz": "table",
                            "agg": "avg",
                            "segment": {"rows": "attr:case:country",
                                        "cols": "resource"}}, table)
        assert w["rows"] == ["AUS", "NZL"]
        assert w["cols"] == ["R1", "R2"]
        # AUS is all R1, NZL all R2 — the off-diagonal has no cases.
        assert w["cells"][0][0]["value"] == pytest.approx(3.5)
        assert w["cells"][0][1]["value"] is None
        assert w["cells"][0][1]["text"] == "—"

    def test_empty_segments_are_omitted_not_zeroed(self, table):
        w = compute_widget({"id": "w", "metric": "timeBetween",
                            "params": {"from": "A", "to": "B"},
                            "viz": "bar", "segment": {"rows": "weekday"}},
                           table)
        # Only Monday has cases; the other six weekdays must not appear.
        assert [s["label"] for s in w["series"]] == ["Mon"]

    def test_segmented_widget_rolls_up_to_worst_segment(self, table):
        w = compute_widget({"id": "w", "metric": "duration", "viz": "bar",
                            "agg": "avg", "segment": {"rows": "resource"},
                            "target": {"on": True, "dir": "<=", "value": 4,
                                       "warn": 6}}, table)
        # R1 avg 3.5 (met), R2 avg 7.0 (missed) -> the widget is missed.
        assert w["state"] == "missed"

    def test_model_widget_computes_nothing(self, table):
        w = compute_widget({"id": "w", "metric": "duration",
                            "viz": "model"}, table)
        assert "value" not in w

    def test_percent_metric_forces_share(self, table):
        w = compute_widget({"id": "w", "metric": "rework", "viz": "kpi",
                            "agg": "avg"}, table)
        assert w["agg"] == "share"
        assert w["value"] == 25.0

    def test_sampled_flag_propagates_to_the_widget(self):
        t = build_fact_table(_log(), max_payload_bytes=1, sample_cases=2)
        w = compute_widget({"id": "w", "metric": "duration", "viz": "kpi"}, t)
        assert w["sampled"] is True

    def test_unknown_metric_raises(self, table):
        with pytest.raises(ValueError, match="Unknown metric"):
            compute_widget({"id": "w", "metric": "nope"}, table)


class TestSeriesMetrics:

    def test_arrival_rate(self, table):
        pts = series_values(table, "arrivalRate")
        assert [p["label"] for p in pts] == ["2026-01"]
        assert pts[0]["value"] == 4

    def test_wip_counts_open_cases_at_each_boundary(self):
        """One case spanning Jan→Mar: open at the Feb and Mar boundaries."""
        rows = [
            {"case:concept:name": "c", "concept:name": "A",
             "time:timestamp": pd.Timestamp("2026-01-15", tz="UTC")},
            {"case:concept:name": "c", "concept:name": "B",
             "time:timestamp": pd.Timestamp("2026-03-15", tz="UTC")},
        ]
        t = build_fact_table(pd.DataFrame(rows))
        pts = series_values(t, "wip")
        assert [(p["label"], p["value"]) for p in pts] == [
            ("2026-01", 0), ("2026-02", 1), ("2026-03", 1)]

    def test_series_respects_the_mask(self, table):
        m = apply_filters(table, [{"field": "resource", "op": "is",
                                   "value": "R1"}])
        assert series_values(table, "arrivalRate", m)[0]["value"] == 2


class TestScorecard:

    def test_only_targeted_widgets_appear(self, table):
        specs = [
            {"id": "a", "title": "no target", "metric": "duration",
             "viz": "kpi"},
            {"id": "b", "title": "with target", "metric": "duration",
             "viz": "kpi", "agg": "avg",
             "target": {"on": True, "dir": "<=", "value": 4, "warn": 6}},
        ]
        rows = scorecard(specs, table)
        assert [r["id"] for r in rows] == ["b"]
        assert rows[0]["goal"] == "≤ 4 d"
        assert rows[0]["actual"] == "5.3 d"
        assert rows[0]["state"] == "risk"

    def test_per_case_goal_is_the_share_goal_not_the_threshold(self, table):
        """In per_case mode the widget's value is a share, so its goal is
        the share goal. Printing the per-case threshold with the share's
        unit would read "≤ 4%" for a target that means "≥ 90% of cases
        within 4 days" — a different claim, not a formatting slip."""
        spec = {
            "id": "b", "title": "% within 4 d", "metric": "duration",
            "viz": "kpi",
            "target": {"on": True, "dir": "<=", "value": 4, "warn": 6,
                       "mode": "per_case", "shareGoal": 90},
        }
        row = scorecard([spec], table)[0]
        assert row["goal"] == "≥ 90% of cases"
        assert row["actual"] == "50.0%"
        assert target_goal_value(spec) == 90

    def test_per_case_without_a_share_goal_describes_the_threshold(self, table):
        spec = {
            "id": "b", "title": "t", "metric": "duration", "viz": "kpi",
            "target": {"on": True, "dir": "<=", "value": 4,
                       "mode": "per_case"},
        }
        assert scorecard([spec], table)[0]["goal"] == "per case ≤ 4 d"

    def test_aggregate_goal_measures_against_the_target_value(self, table):
        spec = {"id": "b", "metric": "duration", "viz": "kpi",
                "target": {"on": True, "dir": "<=", "value": 4}}
        assert target_goal_value(spec) == 4


class TestCatalog:

    def test_interval_metrics_are_listed_but_marked_unavailable(self):
        entries = {e["id"]: e for e in catalog_json(interval_log=False)}
        assert entries["actService"]["available"] is False
        assert "start_timestamp" in entries["actService"]["unavailableReason"]
        # Still present, so the composer can explain rather than hide.
        assert entries["duration"]["available"] is True

    def test_interval_log_enables_service_metrics(self):
        entries = {e["id"]: e for e in catalog_json(interval_log=True)}
        assert entries["actService"]["available"] is True

    def test_percent_metrics_only_offer_share(self):
        entries = {e["id"]: e for e in catalog_json(interval_log=True)}
        assert entries["rework"]["aggs"] == ["share"]


# ---------------------------------------------------------------------------
# ƒ custom-metric grammar
# ---------------------------------------------------------------------------

from pm4py_ucm.algo.dashboards import (  # noqa: E402
    FormulaError, compile_formula, parse_formula, result_type,
)
from pm4py_ucm.algo.dashboards.engine import custom_values  # noqa: E402


class TestFormulaParsing:

    def test_empty_is_an_error(self):
        assert compile_formula("")["ok"] is False
        assert compile_formula("   ")["error"] == "The formula is empty."

    def test_unknown_function(self):
        c = compile_formula('frequency("A")')
        assert not c["ok"] and "Unknown function" in c["error"]

    def test_wrong_arity(self):
        assert "takes 1 argument" in compile_formula("contains()")["error"]
        assert "takes 0 arguments" in compile_formula("duration(\"A\")")["error"]

    def test_single_equals_is_caught(self):
        assert "Use ==" in compile_formula("duration() = 5")["error"]

    def test_bare_string_rejected(self):
        assert not compile_formula('"Payment"')["ok"]

    def test_non_string_argument_rejected(self):
        assert not compile_formula("count(5)")["ok"]

    def test_unbalanced_parens(self):
        assert not compile_formula("duration( >")["ok"]
        assert not compile_formula("(duration()")["ok"]

    @pytest.mark.parametrize("formula,want", [
        ("duration()", "time"),
        ('time_between("A", "B")', "time"),
        ('timestamp("A")', "time"),
        ('count("A")', "count"),
        ('count("A") + 1', "count"),
        ("duration() > 5", "percent"),
        ('contains("A")', "percent"),
        ('contains("A") and contains("B")', "percent"),
        ('not contains("A")', "percent"),
        ('duration() where contains("A")', "time"),
        ('contains("A") where duration() > 5', "percent"),
    ])
    def test_result_type(self, formula, want):
        assert compile_formula(formula)["resultType"] == want

    def test_unknown_names_are_warned_not_errors(self):
        c = compile_formula('contains("Nope") + attr("ghost")',
                            activities=["A"], attributes=["amount"])
        assert c["ok"] is True
        assert "Nope" in c["unknown"]
        assert "attr:ghost" in c["unknown"]


class TestFormulaEvaluation:
    """Evaluated against the hand-computable four-case fixture."""

    def test_duration(self, table):
        v = custom_values(table, "duration()")
        assert list(v) == [3.0, 6.0, 4.0, 8.0]

    def test_count(self, table):
        assert list(custom_values(table, 'count("A")')) == [1, 1, 1, 2]

    def test_contains_is_zero_one(self, table):
        assert list(custom_values(table, 'contains("B")')) == [1, 1, 0, 1]

    def test_comparison(self, table):
        # durations 3,6,4,8 > 4  ->  0,1,0,1
        assert list(custom_values(table, "duration() > 4")) == [0, 1, 0, 1]

    def test_arithmetic(self, table):
        # count(A): 1,1,1,2 ; count(C): 1,1,1,1 -> A + C*2
        assert list(custom_values(table, 'count("A") + count("C") * 2')) \
            == [3, 3, 3, 4]

    def test_where_excludes_via_null(self, table):
        v = custom_values(table, 'duration() where contains("B")')
        # c2 has no B -> null, not zero
        assert np.isnan(v[2])
        assert aggregate(v, "avg") == pytest.approx((3 + 6 + 8) / 3)

    def test_logic_and(self, table):
        # contains(B): 1,1,0,1 ; duration()>5: 0,1,0,1  -> and
        assert list(custom_values(table, 'contains("B") and duration() > 5')) \
            == [0, 1, 0, 1]

    def test_not(self, table):
        assert list(custom_values(table, 'not contains("B")')) == [0, 0, 1, 0]

    def test_null_propagates_through_arithmetic(self, table):
        # time_between A->B is null for c2 (no B); +1 stays null
        v = custom_values(table, 'time_between("A", "B") + 1')
        assert np.isnan(v[2])
        assert not np.isnan(v[0])

    def test_divide_by_zero_is_null_not_inf(self, table):
        v = custom_values(table, '1 / (count("B") - 1)')  # c0,c1,c3 have 1 B
        # count(B)-1 == 0 for those -> null, not inf
        assert np.isnan(v[0])

    def test_attr_numeric(self):
        df = pd.DataFrame([
            {"case:concept:name": "c1", "concept:name": "A",
             "time:timestamp": pd.Timestamp("2026-01-01", tz="UTC"),
             "case:amount": 100},
            {"case:concept:name": "c2", "concept:name": "A",
             "time:timestamp": pd.Timestamp("2026-01-02", tz="UTC"),
             "case:amount": 900},
        ])
        t = build_fact_table(df)
        v = custom_values(t, 'attr("case:amount") > 500')
        assert list(v) == [0, 1]

    def test_broken_formula_is_all_null_not_a_crash(self, table):
        v = custom_values(table, "duration( >")
        assert np.all(np.isnan(v))


class TestFormulaWidget:

    def test_custom_metric_in_a_widget(self, table):
        w = compute_widget({
            "id": "c", "metric": "custom", "viz": "kpi",
            "params": {"formula": "duration() > 4"},
        }, table)
        assert w["resultType"] == "percent"
        assert w["agg"] == "share"
        assert w["value"] == pytest.approx(50.0)   # 2 of 4 over 4 days

    def test_custom_time_metric_unit(self, table):
        w = compute_widget({
            "id": "c", "metric": "custom", "viz": "kpi", "agg": "avg",
            "params": {"formula": "duration()"},
        }, table)
        assert w["unit"] == "d"
        assert w["value"] == pytest.approx(5.25)

    def test_broken_custom_widget_renders_empty(self, table):
        w = compute_widget({"id": "c", "metric": "custom", "viz": "kpi",
                            "params": {"formula": "count("}}, table)
        assert w["text"] == "—"


class TestFormulaEditor:
    """The composer's ƒ editor, checked at the source level (the browser
    behaviour is verified live, not in CI)."""

    def test_composer_offers_a_custom_option(self):
        ui = (ASSETS / "dash-ui.js").read_text(encoding="utf8")
        assert 'value: "custom"' in ui
        assert "ƒ Custom formula" in ui

    def test_editor_validates_with_the_same_engine_it_computes_with(self):
        """The chip and result type come from E.compileFormula, so what
        the editor says is valid is exactly what will run."""
        ui = (ASSETS / "dash-ui.js").read_text(encoding="utf8")
        assert "E.compileFormula(fxArea.value" in ui

    def test_editor_rebuilds_aggregations_from_the_result_type(self):
        """The result type changes with the formula, so the aggregations
        on offer must follow it."""
        ui = (ASSETS / "dash-ui.js").read_text(encoding="utf8")
        assert "AGGS_BY_TYPE" in ui and "rebuildAggs(spec.agg)" in ui

    def test_js_agg_table_matches_the_python_catalog(self):
        """AGGS_BY_TYPE is duplicated into JS because a custom metric has
        no catalog entry; it must still agree with the Python source."""
        from pm4py_ucm.algo.dashboards.catalog import AGGS_BY_TYPE

        ui = (ASSETS / "dash-ui.js").read_text(encoding="utf8")
        for rtype, aggs in AGGS_BY_TYPE.items():
            # Each type's list appears verbatim in the JS table.
            js_list = ", ".join(f'"{a}"' for a in aggs)
            assert js_list in ui, f"{rtype}: {js_list}"


# ---------------------------------------------------------------------------
# The self-contained artifact
# ---------------------------------------------------------------------------

class TestView:
    """The app's Dashboards view and the exported report are one
    artifact, built here. These pin the properties that make it work
    offline and make it safe to embed."""

    def test_is_a_complete_document(self, table):
        html = dashboard_html(table, name="Ops")
        assert html.startswith("<!DOCTYPE html>")
        assert "<div id=\"pm-root\"></div>" in html
        assert "new Dashboard(" in html

    def test_is_self_contained(self, table):
        """No network of any kind: the export has to open from a USB
        stick with no server and no CDN."""
        html = dashboard_html(table)
        for bad in ("<script src=", "<link rel=\"stylesheet\"",
                    "http://", "https://", "import(", "fetch("):
            assert bad not in html, bad

    def test_engine_and_ui_are_both_inlined(self, table):
        html = dashboard_html(table)
        assert "function computeWidget" in html   # the engine
        assert "class Dashboard" in html          # the UI
        # The UI's import of the engine cannot survive: nothing can
        # resolve a relative specifier in a srcdoc/file:// page.
        assert 'from "./dash-engine.js"' not in html

    def test_no_bare_export_survives_inlining(self, table):
        """`export` outside a module is a syntax error — one left behind
        takes the whole view down."""
        assert "\nexport " not in dashboard_html(table)

    def test_engine_exports_reach_the_ui_namespace(self):
        """The UI calls E.<name>; the bundle builds E from the engine's
        exports, so those names must actually be found."""
        import re

        script = bundle_script()
        # The namespace is the IIFE's own return — the last one before it
        # closes, not the first `return {` in the engine's body.
        m = re.search(r"return \{([^{}]*)\};\n\}\)\(\);", script)
        assert m, "the engine IIFE's return statement is not where the " \
                  "bundler put it"
        ns = {n.strip() for n in m.group(1).split(",")}
        for name in ("decodePayload", "computeWidget", "applyFilters",
                     "scorecard", "segmentAxes", "segmentKeys", "fmt",
                     "heat", "commonEndpoints", "targetGoalValue"):
            assert name in ns, name

    def test_every_engine_call_the_ui_makes_is_exported(self):
        """Catches the real failure mode: the UI reaching for E.<name>
        that the engine does not export, which is a runtime TypeError in
        the browser and invisible to the Python suite."""
        import re

        engine = (ASSETS / "dash-engine.js").read_text(encoding="utf8")
        ui = (ASSETS / "dash-ui.js").read_text(encoding="utf8")
        exported = set(re.findall(
            r"^export\s+(?:async\s+)?(?:function|const|let|var|class)\s+"
            r"([A-Za-z_$][\w$]*)", engine, re.M))
        used = set(re.findall(r"\bE\.([A-Za-z_$][\w$]*)", ui))
        assert used, "no E.<name> calls found — did the UI stop using the " \
                     "engine namespace?"
        assert used <= exported, f"not exported by the engine: {used - exported}"

    def test_payload_cannot_break_out_of_the_script_element(self):
        """An activity named `</script>` must not end the element — every
        literal `<` in the config is escaped to \\u003c."""
        df = _log()
        df.loc[0, "concept:name"] = "</script><script>alert(1)</script>"
        html = dashboard_html(build_fact_table(df))
        assert "<script>alert(1)</script>" not in html
        assert "\\u003cscript>alert(1)" in html

    def test_the_config_stays_valid_json_with_html_in_it(self):
        """Escaping `<!--` to `<\\!--` would break JSON.parse (\\! is not a
        legal escape); a graphviz SVG's `<!-- Generated -->` comment hit
        exactly that. \\u003c keeps it parseable."""
        svg = {"ucm": "<svg><!-- Generated by graphviz --><g/></svg>"}
        html = dashboard_html(build_fact_table(_log()), model_svg=svg)
        blob = html.split('id="pm-data">', 1)[1].split("</script>", 1)[0]
        cfg = json.loads(blob)   # must not raise
        assert cfg["modelSvg"]["ucm"].startswith("<svg")
        assert "<!--" not in blob   # the raw comment never appears literally

    def test_the_bundle_cannot_break_out_of_the_script_element(self, table):
        """The HTML parser ends a <script> at the first `</script` in the
        raw text — inside a string, a regex, or a *comment*. A comment in
        dash-ui.js explaining this escaping once truncated the bundle at
        exactly that word, dumping the engine into the page body.
        """
        assert "</script" not in bundle_script().lower()

    def test_document_has_exactly_the_scripts_it_should(self, table):
        """The real invariant: the document must not be truncated. The
        *closing* tag count is what matters — a break-out mints an extra
        `</script>` — while a literal `<script` in a comment is harmless
        to the parser, so only closings are counted."""
        html = dashboard_html(table)
        # The JSON data block + the module script, each closed once.
        assert html.count("</script>") == 2
        # And the document actually reaches its end.
        assert html.rstrip().endswith("</html>")

    def test_script_escaping_preserves_javascript_meaning(self):
        r"""`<\/script` is identical to `</script` for JS, so escaping is
        safe inside strings and regexes — the transform must not mangle
        anything else."""
        src = 'const a = "</script>"; const b = /<\\/script>/; // </script>'
        out = _script_body(src)
        assert "</script" not in out
        assert out.count("<\\/script") == 3

    def test_specs_and_flags_travel_into_the_page(self, table):
        specs = [{"id": "w1", "metric": "duration", "viz": "kpi"}]
        html = dashboard_html(table, specs=specs, read_only=True, name="R")
        cfg = json.loads(html.split('id="pm-data">', 1)[1]
                         .split("</script>", 1)[0].replace("\\u003c", "<"))
        assert cfg["readOnly"] is True
        assert cfg["name"] == "R"
        assert cfg["specs"] == specs
        assert cfg["payload"]["version"] == CONTRACT_VERSION
        assert len(cfg["catalog"]) == len(catalog_json(interval_log=False))

    def test_storage_key_defaults_to_the_log(self, table):
        html = dashboard_html(table)
        assert '"storageKey":"t"' in html

    def test_pending_pin_travels_into_the_page(self, table):
        pin = {"id": "abc", "spec": {"id": "w9", "title": "Model",
                                     "metric": "duration", "viz": "model"}}
        html = dashboard_html(table, pending_pin=pin)
        cfg = json.loads(html.split('id="pm-data">', 1)[1]
                         .split("</script>", 1)[0].replace("\\u003c", "<"))
        assert cfg["pendingPin"] == pin

    def test_absent_pin_is_a_splicable_null(self, table):
        """The app splices a pin into the *cached* document rather than
        keying the cache on it — a pin id is unique per click and would
        miss the cache every time, rebuilding a megabyte for nothing.
        That splice targets this exact literal."""
        assert '"pendingPin":null' in dashboard_html(table)

    def test_pin_is_applied_once(self):
        """The config is re-sent on every rerun, so an un-guarded pin
        would breed a new widget on each one."""
        ui = (ASSETS / "dash-ui.js").read_text(encoding="utf8")
        assert "_applyPendingPin" in ui
        assert "applied.includes(pin.id)" in ui

    def test_composer_is_one_method_for_add_and_edit(self):
        """Add and edit must share the composer, or the two drift in what
        they can express. Edit replaces at the index; add appends."""
        ui = (ASSETS / "dash-ui.js").read_text(encoding="utf8")
        assert "_openComposer(editSpec, editIndex" in ui
        assert "this.specs[editIndex] = clean" in ui   # edit path
        assert "this.specs.push(clean)" in ui           # add path
        # Edit opens on a deep copy, so a cancelled edit leaves the
        # original untouched.
        assert "JSON.parse(JSON.stringify(editSpec))" in ui

    def test_model_widgets_are_not_editable_by_the_composer(self):
        """A pinned model has no metric/segment/target for the
        metric-based composer to edit."""
        ui = (ASSETS / "dash-ui.js").read_text(encoding="utf8")
        assert 'if (w.viz !== "model")' in ui

    def test_reorder_moves_by_one_position(self):
        ui = (ASSETS / "dash-ui.js").read_text(encoding="utf8")
        assert "_move(i, delta)" in ui
        assert "this.specs.splice(i, 1)" in ui and "this.specs.splice(j, 0" in ui

    def test_filters_travel_into_the_page(self, table):
        f = [{"field": "contains", "op": "is", "value": "B"}]
        html = dashboard_html(table, filters=f)
        cfg = json.loads(html.split('id="pm-data">', 1)[1]
                         .split("</script>", 1)[0].replace("\\u003c", "<"))
        assert cfg["filters"] == f

    def test_the_page_can_export_itself(self, table):
        """The page serialises itself rather than asking the server for an
        export — the only way the export can carry the widgets the user
        actually built, since components.html is one-way."""
        ui = (ASSETS / "dash-ui.js").read_text(encoding="utf8")
        assert "exportHtml()" in ui
        # It must re-read the config it emits, or the export ships the
        # seed widgets instead of the user's.
        assert "cfg.specs = this.specs" in ui
        assert "cfg.readOnly = true" in ui
        # And it must apply the same script-escaping the Python side does.
        assert "scriptJson(cfg)" in ui

    def test_write_dashboard(self, table, tmp_path):
        p = tmp_path / "d.html"
        write_dashboard(p, table, name="Ops")
        assert p.read_text(encoding="utf8").startswith("<!DOCTYPE html>")


class TestSessionReport:
    """The multi-section report is a second shape of the same artifact,
    chosen by a mode switch. These pin the composition and the seam."""

    def test_bundle_carries_all_three_modules(self, table):
        """The report references Dashboard / h / E as bundle-scope names,
        so the engine, UI and report must be in one script."""
        s = bundle_script()
        assert "function computeWidget" in s   # engine
        assert "class Dashboard" in s          # UI
        assert "class Report" in s             # report
        assert "function buildSessionReport" in s
        # No import survived — they would be dead references in the bundle.
        assert 'from "./dash-engine.js"' not in s
        assert 'from "./dash-ui.js"' not in s

    def test_the_concatenated_bundle_parses(self):
        """Each module parses alone, but the bundle concatenates the UI
        and report at one scope — a name declared top-level in both is a
        SyntaxError that fails the whole script and renders nothing. Only
        checking the assembled bundle catches it (a duplicate STATE_LABEL
        did exactly this)."""
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if node is None:
            pytest.skip("node not available")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bundle.mjs"
            p.write_text(bundle_script(), encoding="utf8")
            r = subprocess.run([node, "--check", str(p)],
                               capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, f"bundle does not parse:\n{r.stderr}"

    def test_bootstrap_switches_on_mode(self, table):
        html = dashboard_html(table)
        assert 'cfg.mode === "report"' in html
        assert "new Report(el, cfg)" in html
        assert "new Dashboard(el, cfg)" in html

    def test_model_svg_travels_into_the_page(self, table):
        svg = {"ucm": "<svg id='u'></svg>", "bpmn": "<svg id='b'></svg>"}
        html = dashboard_html(table, model_svg=svg)
        cfg = json.loads(html.split('id="pm-data">', 1)[1]
                         .split("</script>", 1)[0].replace("\\u003c", "<"))
        assert cfg["modelSvg"] == svg

    def test_family_report_travels_into_the_page(self, table):
        # The family statistics report rides in the config for the session
        # report's Family section; its <script>/</script> must survive the
        # \\u003c escaping without breaking the embedding document.
        report = "<!DOCTYPE html><body><script>var x = 1;</script><p>hi</p></body>"
        html = dashboard_html(table, family_report=report)
        # The document is not truncated by the embedded </script>.
        assert html.count("</script>") == html.count("<script")
        cfg = json.loads(html.split('id="pm-data">', 1)[1]
                         .split("</script>", 1)[0].replace("\\u003c", "<"))
        assert cfg["familyReport"] == report

    def test_family_report_absent_by_default(self, table):
        cfg = json.loads(dashboard_html(table).split('id="pm-data">', 1)[1]
                         .split("</script>", 1)[0].replace("\\u003c", "<"))
        assert cfg["familyReport"] is None

    def test_default_page_is_dashboard_mode(self, table):
        cfg = json.loads(dashboard_html(table).split('id="pm-data">', 1)[1]
                         .split("</script>", 1)[0].replace("\\u003c", "<"))
        assert cfg.get("mode") in (None, "dashboard")

    def test_report_is_built_client_side_from_live_state(self):
        """buildSessionReport clones the page and swaps in the user's live
        specs — a server build would only know the seed dashboard."""
        report = (ASSETS / "dash-report.js").read_text(encoding="utf8")
        assert "specs: dash.specs" in report
        assert 'cfg.mode = "report"' in report
        # Same script-escape the rest of the export path uses.
        assert "scriptJson(cfg)" in report

    def test_report_reuses_the_dashboard_for_its_sections(self):
        """A widget must look identical in the app and the report, so the
        report renders sections with the same Dashboard, headless."""
        report = (ASSETS / "dash-report.js").read_text(encoding="utf8")
        assert "new Dashboard(mount, {" in report
        assert "headless: true" in report

    def test_headless_dashboard_has_no_header(self):
        ui = (ASSETS / "dash-ui.js").read_text(encoding="utf8")
        assert "this.headless = !!opts.headless" in ui
        assert 'this.headless ? document.createComment("headless")' in ui


class TestTheme:
    """Dark mode.

    The bug this pins was white text on a white background: the shell
    hard-coded the light paper surface while the host kept its near-white
    text, for a contrast ratio of 1.01:1. Every colour therefore has to
    be a token that moves with the theme.
    """

    def test_theme_reaches_the_page_and_the_root(self, table):
        html = dashboard_html(table, theme="dark")
        assert '<html lang="en" data-theme="dark">' in html
        cfg = json.loads(html.split('id="pm-data">', 1)[1]
                         .split("</script>", 1)[0].replace("\\u003c", "<"))
        assert cfg["theme"] == "dark"

    def test_theme_is_on_html_so_the_page_does_not_flash(self, table):
        """The browser paints the page background before any script runs,
        so a pinned theme must be in the markup, not applied later."""
        html = dashboard_html(table, theme="dark")
        head = html.split("<head>", 1)[0]
        assert 'data-theme="dark"' in head

    def test_auto_theme_leaves_it_to_the_viewer(self, table):
        html = dashboard_html(table)
        assert '<html lang="en">' in html
        cfg = json.loads(html.split('id="pm-data">', 1)[1]
                         .split("</script>", 1)[0].replace("\\u003c", "<"))
        assert cfg["theme"] is None

    def test_bad_theme_rejected(self, table):
        with pytest.raises(ValueError, match="theme must be"):
            dashboard_html(table, theme="garnet")

    def test_stylesheet_defines_both_palettes(self):
        css = (ASSETS / "dash-styles.css").read_text(encoding="utf8")
        assert "@media (prefers-color-scheme: dark)" in css  # the export
        assert '.pm-dash[data-theme="dark"]' in css          # a host tells us
        # An explicit light request must beat a dark OS.
        assert '.pm-dash:not([data-theme="light"])' in css

    def test_every_dark_token_has_a_light_counterpart(self):
        """A token defined in only one palette falls back to the other
        theme's value — which is exactly how white-on-white happens."""
        css = (ASSETS / "dash-styles.css").read_text(encoding="utf8")
        blocks = re.findall(r"\{([^{}]*)\}", css)
        names = lambda b: set(re.findall(r"(--[\w-]+)\s*:", b))
        light = names(blocks[0])                       # the .pm-dash block
        dark = names([b for b in blocks
                      if "--paper: #14161b" in b][0])
        colourish = {n for n in light
                     if not n.startswith(("--f-", "--gap", "--row-h"))}
        assert colourish - dark == set(), \
            f"defined in light but not dark: {sorted(colourish - dark)}"

    def test_no_colour_is_hardcoded_outside_the_palettes(self):
        """Every colour must be a token; one stray hex is one surface that
        will not follow the theme."""
        css = (ASSETS / "dash-styles.css").read_text(encoding="utf8")
        body = css.split(".pm-dash *, .pm-dash *::before", 1)[1]
        hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", body)
        # #fff on a garnet fill, and the two rgba() scrim/shadow blacks,
        # are theme-independent by construction.
        assert [h for h in hexes if h.lower() not in ("#fff", "#ffffff")] \
            == [], f"hard-coded colours outside the palettes: {hexes}"

    def test_heat_ramp_has_a_dark_variant(self):
        """The ramp is a function of the value, so CSS tokens cannot
        reach it — the two themes are spelled out in JS."""
        js = (ASSETS / "dash-engine.js").read_text(encoding="utf8")
        assert "export function heat(u, dark)" in js
        ui = (ASSETS / "dash-ui.js").read_text(encoding="utf8")
        # Every call site must pass the resolved theme, or a chart keeps
        # the light ramp on a dark page.
        assert "E.heat(" in ui
        assert not re.search(r"E\.heat\([^)]*\)(?<!this\.dark\))", ui), \
            "an E.heat() call is not passing this.dark"

    def test_surfaces_outside_the_root_are_told_the_theme(self):
        """The toast and the modal scrim mount on <body>, outside the
        dashboard root, so they inherit no data-theme."""
        ui = (ASSETS / "dash-ui.js").read_text(encoding="utf8")
        assert '"data-theme": theme' in ui       # the scrim
        assert 'el.setAttribute("data-theme", theme)' in ui  # the toast
        assert ui.count("theme: this._theme()") >= 3

    def test_embedded_island_reads_the_live_host_theme(self):
        """The partial-refresh fix: rather than trust the theme Python
        baked in (which can lag a rerun), the embedded island reads the
        host's own color-scheme and re-themes on change."""
        ui = (ASSETS / "dash-ui.js").read_text(encoding="utf8")
        assert "_hostThemeSource" in ui
        assert "window.parent.document" in ui
        assert 'cs.colorScheme === "dark"' in ui
        assert "MutationObserver" in ui
        # The theme must reach both the root (token block) and <html>
        # (the page background), or a dark board keeps a light gutter.
        assert 'document.documentElement.setAttribute("data-theme"' in ui

    def test_theme_read_falls_back_when_there_is_no_host(self):
        """A standalone export is top-level, so window.parent === window;
        it must then use the pinned/OS theme, not crash reaching for a
        host."""
        ui = (ASSETS / "dash-ui.js").read_text(encoding="utf8")
        assert "if (window.parent === window) return null" in ui


# ---------------------------------------------------------------------------
# Python <-> JS parity
# ---------------------------------------------------------------------------

_PARITY_SPECS = [
    {"id": "kpi-duration", "metric": "duration", "viz": "kpi", "agg": "avg"},
    {"id": "kpi-median", "metric": "duration", "viz": "kpi", "agg": "median"},
    {"id": "kpi-p90", "metric": "duration", "viz": "kpi", "agg": "p90"},
    {"id": "kpi-events", "metric": "eventCount", "viz": "kpi", "agg": "sum"},
    {"id": "kpi-rework", "metric": "rework", "viz": "kpi"},
    {"id": "kpi-tb", "metric": "timeBetween", "viz": "kpi",
     "params": {"from": "A", "to": "B"}, "agg": "avg"},
    {"id": "kpi-tb-loop", "metric": "timeBetween", "viz": "kpi",
     "params": {"from": "B", "to": "A"}, "agg": "avg"},
    {"id": "kpi-sojourn", "metric": "actSojourn", "viz": "kpi",
     "params": {"activity": "A"}, "agg": "avg"},
    {"id": "kpi-edgeshare", "metric": "edgeShare", "viz": "kpi",
     "params": {"from": "B", "to": "C"}},
    {"id": "kpi-edgetime", "metric": "edgeTime", "viz": "kpi",
     "params": {"from": "A", "to": "B"}, "agg": "avg"},
    {"id": "kpi-repeats", "metric": "actRepeats", "viz": "kpi",
     "params": {"activity": "A"}, "agg": "max"},
    {"id": "bar-resource", "metric": "duration", "viz": "bar",
     "agg": "avg", "segment": {"rows": "resource"}},
    {"id": "bar-weekday", "metric": "duration", "viz": "bar",
     "agg": "avg", "segment": {"rows": "weekday"}},
    {"id": "bar-variant", "metric": "duration", "viz": "bar",
     "agg": "avg", "segment": {"rows": "variant"}},
    {"id": "table-country-resource", "metric": "duration", "viz": "table",
     "agg": "avg", "segment": {"rows": "attr:case:country",
                               "cols": "resource"}},
    {"id": "table-month-country", "metric": "eventCount", "viz": "table",
     "agg": "sum", "segment": {"rows": "month",
                               "cols": "attr:case:country"}},
    {"id": "kpi-target", "metric": "duration", "viz": "kpi", "agg": "avg",
     "target": {"on": True, "dir": "<=", "value": 4, "warn": 6}},
    {"id": "kpi-target-ge", "metric": "duration", "viz": "kpi", "agg": "avg",
     "target": {"on": True, "dir": ">=", "value": 6, "warn": 5}},
    {"id": "kpi-percase", "metric": "duration", "viz": "kpi",
     "target": {"on": True, "dir": "<=", "value": 4, "warn": 6,
                "mode": "per_case", "shareGoal": 90, "shareWarn": 40}},
    {"id": "table-target", "metric": "duration", "viz": "table", "agg": "avg",
     "segment": {"rows": "attr:case:country", "cols": "resource"},
     "target": {"on": True, "dir": "<=", "value": 4, "warn": 6}},
    {"id": "filtered", "metric": "duration", "viz": "kpi", "agg": "avg",
     "filter": [{"field": "contains", "op": "is", "value": "B"}]},
    {"id": "filtered-attr", "metric": "duration", "viz": "kpi", "agg": "avg",
     "filter": [{"field": "attr:case:country", "op": "is", "value": "NZL"}]},
    {"id": "filtered-segment", "metric": "duration", "viz": "kpi",
     "agg": "avg",
     "filter": [{"field": "segment", "op": "is",
                 "value": ["resource", "R1"]}]},
    {"id": "series-wip", "metric": "wip", "viz": "bar", "agg": "avg"},
    {"id": "series-arrival", "metric": "arrivalRate", "viz": "bar",
     "agg": "sum"},
    {"id": "series-completion", "metric": "completionRate", "viz": "bar",
     "agg": "sum"},
    # ƒ custom formulas — the parity-risky part is the evaluator's
    # arithmetic / comparison / logic / where / null propagation, all of
    # which these exercise on the fixture's A/B/C activities.
    {"id": "cust-count", "metric": "custom", "viz": "kpi", "agg": "avg",
     "params": {"formula": 'count("A")'}},
    {"id": "cust-contains", "metric": "custom", "viz": "kpi",
     "params": {"formula": 'contains("B")'}},
    {"id": "cust-cmp", "metric": "custom", "viz": "kpi",
     "params": {"formula": "duration() > 4"}},
    {"id": "cust-arith", "metric": "custom", "viz": "kpi", "agg": "avg",
     "params": {"formula": 'count("A") + count("C") * 2'}},
    {"id": "cust-where", "metric": "custom", "viz": "kpi", "agg": "avg",
     "params": {"formula": 'duration() where contains("B")'}},
    {"id": "cust-logic", "metric": "custom", "viz": "kpi",
     "params": {"formula": 'contains("B") and duration() > 5'}},
    {"id": "cust-not", "metric": "custom", "viz": "kpi",
     "params": {"formula": 'not contains("B")'}},
    {"id": "cust-tb", "metric": "custom", "viz": "kpi", "agg": "median",
     "params": {"formula": 'time_between("A", "C")'}},
    {"id": "cust-seg", "metric": "custom", "viz": "table", "agg": "avg",
     "params": {"formula": 'count("A")'},
     "segment": {"rows": "attr:case:country", "cols": "resource"}},
    {"id": "cust-broken", "metric": "custom", "viz": "kpi",
     "params": {"formula": "duration( >"}},
]

_PARITY_DASHBOARD_FILTERS = [
    [],
    [{"field": "attr:case:country", "op": "is", "value": "AUS"}],
    [{"field": "contains", "op": "not", "value": "B"}],
]

_RUNNER = r"""
import * as E from %(engine)s;
import { readFileSync } from "node:fs";

const inp = JSON.parse(readFileSync(process.argv[2], "utf8"));
const t = E.decodePayload(inp.payload);
const catalog = Object.fromEntries(inp.catalog.map((m) => [m.id, m]));

const out = [];
for (const df of inp.dashboardFilters) {
  for (const spec of inp.specs) {
    let r;
    try {
      r = E.computeWidget(spec, t, catalog, df);
    } catch (e) {
      r = { error: String(e && e.message || e) };
    }
    out.push(r);
  }
}
out.push({ scorecard: E.scorecard(inp.specs, t, catalog, []) });
process.stdout.write(JSON.stringify(out));
"""


def _run_js(payload, specs, dashboard_filters, catalog, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available; cannot check JS engine parity")

    inp = tmp_path / "input.json"
    inp.write_text(json.dumps({
        "payload": payload, "specs": specs,
        "dashboardFilters": dashboard_filters, "catalog": catalog,
    }), encoding="utf8")

    runner = tmp_path / "runner.mjs"
    runner.write_text(
        _RUNNER % {"engine": json.dumps(ENGINE_JS.resolve().as_uri())},
        encoding="utf8")

    proc = subprocess.run(
        [node, str(runner), str(inp)],
        capture_output=True, text=True, encoding="utf8", timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"node exited {proc.returncode}\nstderr:\n{proc.stderr}")
    return json.loads(proc.stdout)


class TestJsParity:
    """The JS engine must return what the Python engine returns.

    Two implementations of one set of semantics will drift unless
    something holds them together; this is that something. It compares
    the full widget structure — values, states, labels, counts and the
    formatted text — across every metric, every visualisation, targets in
    both modes, and stacked filters.
    """

    @pytest.fixture(scope="class")
    def js_results(self, tmp_path_factory):
        t = build_fact_table(_log(), log_name="t")
        return _run_js(
            t.to_payload(), _PARITY_SPECS, _PARITY_DASHBOARD_FILTERS,
            catalog_json(interval_log=t.interval_log),
            tmp_path_factory.mktemp("parity"),
        )

    @pytest.fixture(scope="class")
    def py_results(self):
        t = build_fact_table(_log(), log_name="t")
        out = []
        for df in _PARITY_DASHBOARD_FILTERS:
            for spec in _PARITY_SPECS:
                out.append(compute_widget(spec, t, dashboard_filters=df))
        return out

    def test_runner_produced_a_result_per_spec(self, js_results):
        expected = len(_PARITY_SPECS) * len(_PARITY_DASHBOARD_FILTERS)
        assert len(js_results) == expected + 1  # + the scorecard entry

    @pytest.mark.parametrize("i", range(
        len(_PARITY_SPECS) * len(_PARITY_DASHBOARD_FILTERS)))
    def test_widget_matches(self, i, py_results, js_results):
        py, js = py_results[i], js_results[i]
        assert "error" not in js, f"JS raised: {js.get('error')}"

        label = f"{py['id']} (filter set {i // len(_PARITY_SPECS)})"
        for key in ("id", "viz", "unit", "resultType", "nCases", "agg",
                    "state", "text", "sub", "rows", "cols", "axis"):
            if key in py or key in js:
                assert js.get(key) == py.get(key), f"{label}: {key}"

        if py.get("value") is None:
            assert js.get("value") is None, f"{label}: value"
        else:
            assert js["value"] == pytest.approx(py["value"]), \
                f"{label}: value"

        if "distribution" in py:
            for k, v in py["distribution"].items():
                assert js["distribution"][k] == pytest.approx(v), \
                    f"{label}: distribution[{k}]"

        for k in ("series", "cells"):
            if k not in py:
                continue
            assert json.dumps(_norm(js[k])) == json.dumps(_norm(py[k])), \
                f"{label}: {k}"

    def test_scorecard_matches(self, js_results):
        t = build_fact_table(_log(), log_name="t")
        py = scorecard(_PARITY_SPECS, t)
        js = js_results[-1]["scorecard"]
        assert json.dumps(_norm(js)) == json.dumps(_norm(py))


def _norm(obj):
    """Canonicalise for comparison: round away the last bits of IEEE
    noise, and coerce every number to float.

    JSON does not distinguish 7 from 7.0 but Python and JS disagree about
    which one to emit, so comparing the serialised forms without this
    would fail on a difference that does not exist.
    """
    if isinstance(obj, dict):
        return {k: _norm(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_norm(v) for v in obj]
    if isinstance(obj, bool):  # bool is an int subclass — keep it a bool
        return obj
    if isinstance(obj, (np.floating, np.integer)):
        return _norm(obj.item())
    if isinstance(obj, (int, float)):
        return round(float(obj), 9)
    return obj
