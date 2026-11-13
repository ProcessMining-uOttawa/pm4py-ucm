"""Tests for dashboard suggestions (``docs/dashboard_suggestions.md`` §2).

A suggestion is a list of ordinary widget specs, so two things are pinned
here: that each *rule* fires on the log property it names and stays quiet
without it, and that everything proposed — on every fixture — is a widget
the engines can actually compute, bind and draw truthfully.
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
    BY_ID,
    build_fact_table,
    catalog_json,
    compile_formula,
    compute_widget,
    dashboard_html,
    scorecard,
    suggest_dashboards,
)
from pm4py_ucm.algo.dashboards.engine import SERIES_METRICS, TIME_AXES

from .test_dashboards import ASSETS, ENGINE_JS, _log, _run_js

HOUR = pd.Timedelta(hours=1)
DAY = pd.Timedelta(days=1)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _claims(n: int = 120, months: int = 6) -> "pd.DataFrame":
    """A log built so that every rule has something to find.

    * ``Register``, then ``Notify`` and ``Log`` in **either order** (a
      parallel pair: half the cases each way);
    * ``channel`` routes the case: *phone* goes to ``Fast Track``, *web* to
      ``Check`` — which takes five days, so it is the bottleneck and the
      attribute both routes behaviour and segments duration;
    * one web case in four reworks: ``Check → Fix → Check``;
    * ``Decide`` a day later, then ``Approve`` (4 in 5) or ``Reject``;
    * cases start across ``months`` calendar months;
    * ``colour`` is unrelated to anything.
    """
    rows = []
    for i in range(n):
        cid = f"c{i:03d}"
        channel = "phone" if i % 3 == 0 else "web"
        t = (pd.Timestamp("2026-01-10", tz="UTC")
             + pd.DateOffset(months=i % months) + (i // months) * HOUR)

        def add(act, step):
            nonlocal t
            t = t + step
            rows.append({
                "case:concept:name": cid, "concept:name": act,
                "time:timestamp": t, "org:resource": f"R{i % 4 + 1}",
                "case:channel": channel,
                "case:colour": ("red", "blue")[(i // 7) % 2],
            })

        add("Register", 0 * HOUR)
        for act in (("Notify", "Log") if i % 2 else ("Log", "Notify")):
            add(act, HOUR)
        if channel == "phone":
            add("Fast Track", HOUR)
        else:
            add("Check", 5 * DAY)
            if i % 4 == 1:
                add("Fix", HOUR)
                add("Check", 5 * DAY)
        add("Decide", DAY)
        add("Approve" if i % 5 else "Reject", HOUR)
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def claims():
    return build_fact_table(_claims(), log_name="claims")


@pytest.fixture(scope="module")
def claims_dash(claims):
    return {d["key"]: d for d in suggest_dashboards(claims)}


def _clustering(df, noise: float = 0.0):
    """The concurrency-aware clustering of ``df`` on its own mined tree."""
    pm4py = pytest.importorskip("pm4py")
    from pm4py_ucm.algo.discovery.variants import clustering
    tree = pm4py.discover_process_tree_inductive(df, noise_threshold=noise)
    return clustering.cluster(df, tree)


@pytest.fixture(scope="module")
def claims_bv():
    """The claims log with its behavioural variants attached."""
    df = _claims()
    return build_fact_table(df, log_name="claims",
                            behaviour=_clustering(df))


@pytest.fixture(scope="module")
def claims_bv_dash(claims_bv):
    return {d["key"]: d for d in suggest_dashboards(claims_bv)}


@pytest.fixture(scope="module")
def tiny():
    """The four-case, one-week log the engine tests use."""
    return build_fact_table(_log(), log_name="t")


def _titles(dash):
    return [s["title"] for s in dash["specs"]]


def _by_title(dash, title):
    return next(s for s in dash["specs"] if s["title"] == title)


def _all_specs(dashboards):
    return [s for d in dashboards for s in d["specs"]]


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

class TestShape:

    def test_overview_first_and_always(self, claims, tiny):
        for t in (claims, tiny):
            assert suggest_dashboards(t)[0]["key"] == "overview"

    def test_dashboard_fields(self, claims):
        for d in suggest_dashboards(claims):
            assert set(d) == {"key", "name", "why", "specs", "filters"}
            assert d["name"] and d["why"] and d["filters"] == []

    def test_every_widget_says_why(self, claims):
        for s in _all_specs(suggest_dashboards(claims)):
            assert s["title"] and len(s["why"]) > 15, s

    def test_ids_are_unique(self, claims):
        ids = [s["id"] for s in _all_specs(suggest_dashboards(claims))]
        assert len(ids) == len(set(ids))

    def test_deterministic(self, claims):
        assert suggest_dashboards(claims) == suggest_dashboards(claims)

    def test_json_serialisable(self, claims):
        """Plain JSON all the way down — no numpy scalar reaches the page
        config, where ``json.dumps`` would refuse it."""
        json.dumps(suggest_dashboards(claims))

    def test_max_widgets_truncates(self, claims):
        for d in suggest_dashboards(claims, max_widgets=3):
            assert len(d["specs"]) <= 3

    def test_thin_dashboards_are_not_proposed(self, tiny):
        """Four cases in one week give the themed dashboards nothing to
        say; only what reaches three widgets is offered."""
        for d in suggest_dashboards(tiny):
            assert d["key"] == "overview" or len(d["specs"]) >= 3


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

class TestOverview:

    def test_headline_kpis(self, claims_dash):
        t = _titles(claims_dash["overview"])
        assert t[:4] == ["median case duration", "P90 case duration",
                         "Rework rate", "avg events per case"]

    def test_the_process_footprint(self, claims, claims_dash):
        """One bar per activity: the share of cases that reach it."""
        s = _by_title(claims_dash["overview"], "Share of cases by activity")
        assert s["metric"] == "actPresence"
        assert s["segment"] == {"rows": "activity"}
        got = {p["label"]: p["value"]
               for p in compute_widget(s, claims)["series"]}
        assert got["Register"] == 100.0
        assert got["Fast Track"] == pytest.approx(100 / 3)

    def test_trend_widgets_need_three_months(self, claims_dash, tiny):
        long_ = _titles(claims_dash["overview"])
        assert "Arrivals per month" in long_
        assert "Work in progress" in long_
        assert "median duration by month" in long_

        short = _titles(suggest_dashboards(tiny)[0])
        assert "Arrivals per month" not in short
        assert not any(" by month" in x for x in short)

    def test_short_log_compares_weekdays_instead(self):
        """Three weeks support no trend, but cases start on five different
        weekdays — the one time axis such a log can be read along."""
        df = _claims(n=30, months=1)
        shift = (df["case:concept:name"].str[1:].astype(int) % 5)
        df["time:timestamp"] = df["time:timestamp"] + pd.to_timedelta(
            shift, unit="D")
        t = build_fact_table(df, log_name="short")
        assert "median duration by weekday" in _titles(
            suggest_dashboards(t)[0])

    def test_no_weekday_chart_of_a_single_weekday(self, tiny):
        """Every case in the four-case log starts on a Monday; one bar is
        not a comparison."""
        assert "median duration by weekday" not in _titles(
            suggest_dashboards(tiny)[0])

    def test_period_coarsens_on_a_long_log(self):
        """Thirty months is too many monthly points, so the trend moves to
        quarters rather than drawing a 30-point line."""
        t = build_fact_table(_claims(n=60, months=30), log_name="long")
        titles = _titles(suggest_dashboards(t)[0])
        assert "median duration by quarter" in titles

    def test_no_histogram_when_durations_are_all_equal(self):
        rows = [{"case:concept:name": f"c{i}", "concept:name": a,
                 "time:timestamp": pd.Timestamp("2026-01-05", tz="UTC")
                 + i * HOUR + k * DAY}
                for i in range(5) for k, a in enumerate("AB")]
        t = build_fact_table(pd.DataFrame(rows), log_name="flat")
        assert "Case duration distribution" not in _titles(
            suggest_dashboards(t)[0])


# ---------------------------------------------------------------------------
# Bottlenecks
# ---------------------------------------------------------------------------

class TestBottlenecks:

    def test_names_the_activity_holding_the_time(self, claims_dash):
        d = claims_dash["bottlenecks"]
        assert d["specs"][0]["title"] == "avg sojourn — Check"
        assert d["specs"][0]["params"] == {"activity": "Check"}
        assert d["why"].startswith("Check holds")

    def test_transitions_do_not_restate_a_listed_activity(self, claims_dash):
        """A step *into* Check is Check's sojourn again."""
        top = {s["params"]["activity"]
               for s in claims_dash["bottlenecks"]["specs"]
               if s["metric"] == "actSojourn" and s["viz"] == "kpi"}
        for s in claims_dash["bottlenecks"]["specs"]:
            if s["metric"] == "edgeTime":
                assert s["params"]["to"] not in top

    def test_trend_and_strong_attribute(self, claims_dash):
        t = _titles(claims_dash["bottlenecks"])
        assert "avg sojourn, activity × month" in t
        # The heatmap already trends every activity, Check included.
        assert "sojourn of Check by month" not in t

    def test_every_activity_side_by_side(self, claims, claims_dash):
        """The KPIs name the top of the ranking; the bar shows the rest."""
        s = _by_title(claims_dash["bottlenecks"], "avg sojourn by activity")
        assert s["viz"] == "bar" and s["segment"] == {"rows": "activity"}
        labels = [p["label"] for p in compute_widget(s, claims)["series"]]
        # Every activity but Register, which is always first in its case.
        assert sorted(labels) == sorted(
            a for a in claims.activities if a != "Register")

    def test_heatmap_spans_the_dashboard(self, claims, claims_dash):
        s = _by_title(claims_dash["bottlenecks"],
                      "avg sojourn, activity × month")
        assert s["viz"] == "table"
        assert s["segment"] == {"rows": "activity", "cols": "month"}
        assert s["size"]["w"] == 4 and 2 <= s["size"]["h"] <= 6
        w = compute_widget(s, claims)
        assert len(w["cols"]) == 6 and "Check" in w["rows"]

    def test_too_many_activities_get_the_bar_and_a_trend_not_a_heatmap(
            self, monkeypatch):
        from pm4py_ucm.algo.dashboards import suggest
        monkeypatch.setattr(suggest, "MAX_HEATMAP_ROWS", 3)
        t = build_fact_table(_claims(), log_name="claims")
        d = {x["key"]: x for x in suggest_dashboards(t)}
        titles = _titles(d["bottlenecks"])
        assert "avg sojourn by activity" in titles
        assert not any(x.startswith("avg sojourn, activity ×")
                       for x in titles)
        assert "sojourn of Check by month" in titles
        assert not any("activity ×" in x for x in _titles(d["segments"]))

    def test_no_chart_of_a_single_segment(self, claims_dash):
        """channel is the strongest attribute, but only web cases reach
        Check — "sojourn of Check by channel" would be one bar."""
        assert "sojourn of Check by channel" not in _titles(
            claims_dash["bottlenecks"])

    def test_interval_widgets_only_on_an_interval_log(self, claims_dash):
        metrics = {s["metric"] for s in claims_dash["bottlenecks"]["specs"]}
        assert not metrics & {"actWaiting", "actService"}

        df = _claims()
        df["start_timestamp"] = df["time:timestamp"] - pd.Timedelta(minutes=5)
        t = build_fact_table(df, log_name="interval")
        d = {x["key"]: x for x in suggest_dashboards(t)}["bottlenecks"]
        assert {"actWaiting", "actService"} <= {s["metric"]
                                                for s in d["specs"]}


# ---------------------------------------------------------------------------
# Rework and flow
# ---------------------------------------------------------------------------

class TestReworkAndFlow:

    def test_repeated_activity_is_a_rate_not_a_rounded_average(
            self, claims, claims_dash):
        """20 of the 80 cases that reach Check run it twice. ``actRepeats``
        would average that to 0.25 and display "0"; the rate reads 25%,
        and the reason gives the share of all 120 cases as well."""
        s = _by_title(claims_dash["rework"], "Rework rate — Check")
        assert s["metric"] == "actRework"
        assert compute_widget(s, claims)["value"] == pytest.approx(25.0)
        assert "25% of the cases that perform it" in s["why"]
        assert "17% of all cases" in s["why"]

    def test_rework_for_every_activity(self, claims, claims_dash):
        s = _by_title(claims_dash["rework"], "Rework rate by activity")
        assert s["segment"] == {"rows": "activity"}
        got = {p["label"]: p["value"]
               for p in compute_widget(s, claims)["series"]}
        # Only Check is ever redone; every other activity reads 0%.
        assert got.pop("Check") == pytest.approx(25.0)
        assert set(got.values()) == {0.0} and len(got) == 8

    def test_rework_heatmap_over_time(self, claims, claims_dash):
        s = _by_title(claims_dash["rework"], "Rework rate, activity × month")
        assert s["viz"] == "table" and s["size"]["w"] == 4
        w = compute_widget(s, claims)
        assert len(w["cols"]) == 6 and len(w["rows"]) == 9
        # It replaces the single overall trend line.
        assert "Rework rate by month" not in _titles(claims_dash["rework"])

    def test_short_log_compares_rework_across_the_strong_attribute(self):
        df = _claims(n=60, months=1)
        t = build_fact_table(df, log_name="short")
        d = {x["key"]: x for x in suggest_dashboards(t)}["rework"]
        assert "Rework rate, activity × channel" in _titles(d)
        assert not any("× month" in x for x in _titles(d))

    def test_no_rework_widgets_without_rework(self):
        df = _claims()
        keep = ~df["concept:name"].eq("Fix")
        # Drop the Fix and the second Check of every reworking case.
        dup = df[keep].duplicated(["case:concept:name", "concept:name"])
        t = build_fact_table(df[keep][~dup], log_name="clean")
        d = {x["key"]: x for x in suggest_dashboards(t)}.get("rework")
        assert d is None or not any(
            s["metric"] in ("rework", "actRework") for s in d["specs"])

    def test_decision_point(self, claims_dash):
        edges = {(s["params"]["from"], s["params"]["to"])
                 for s in claims_dash["rework"]["specs"]
                 if s["metric"] == "edgeShare"}
        assert ("Decide", "Approve") in edges
        assert ("Decide", "Reject") in edges

    def test_parallel_activities_are_not_a_decision(self, claims_dash):
        """Notify and Log run in either order. Directly-follows shows
        Register → Notify and Register → Log at 50% each, and Notify ⇄ Log
        — none of which is a choice anybody makes."""
        for s in claims_dash["rework"]["specs"]:
            if s["metric"] != "edgeShare":
                continue
            pair = {s["params"]["from"], s["params"]["to"]}
            assert pair != {"Notify", "Log"}
            assert s["params"]["from"] != "Register"

    def test_a_loop_is_still_a_decision(self):
        """Check → Fix → Check also runs both ways between two activities,
        but in the *same* cases — a loop, which is a real branch at Check."""
        from pm4py_ucm.algo.dashboards.suggest import (
            _activity_profile,
            _decision_points,
        )

        t = build_fact_table(_claims(), log_name="claims")
        acts = t.activities
        points = {acts[s]: [acts[b] for b, _ in br]
                  for s, br in _decision_points(t, _activity_profile(t))}
        assert set(points["Check"]) == {"Decide", "Fix"}
        assert "Register" not in points

    def test_outcomes(self, claims_dash):
        t = _titles(claims_dash["rework"])
        assert "Cases reaching Approve" in t and "Cases reaching Reject" in t

    def test_single_outcome_gets_no_outcome_widgets(self):
        df = _claims()
        df.loc[df["concept:name"] == "Reject", "concept:name"] = "Approve"
        t = build_fact_table(df, log_name="one-end")
        for d in suggest_dashboards(t):
            assert not any(x.startswith("Cases reaching")
                           for x in _titles(d))


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------

class TestSegments:

    def test_the_routing_attribute_leads(self, claims_dash):
        d = claims_dash["segments"]
        assert d["specs"][0]["title"] == "median duration by channel"
        assert d["specs"][0]["segment"] == {"rows": "attr:case:channel"}
        assert "channel" in d["why"]

    def test_an_irrelevant_attribute_is_offered_but_says_so(
            self, claims_dash):
        s = _by_title(claims_dash["segments"], "median duration by colour")
        assert "barely separates" in s["why"]

    def test_only_a_strong_attribute_is_used_beyond_its_own_bar(
            self, claims_dash):
        for d in claims_dash.values():
            for s in d["specs"]:
                axes = set((s.get("segment") or {}).values())
                if "attr:case:colour" in axes:
                    assert s["title"] == "median duration by colour"

    def test_how_the_path_differs_across_the_strong_attribute(
            self, claims, claims_dash):
        s = _by_title(claims_dash["segments"],
                      "Share of cases, activity × channel")
        assert s["segment"] == {"rows": "activity",
                                "cols": "attr:case:channel"}
        w = compute_widget(s, claims)
        cell = dict(zip(w["cols"], w["cells"][w["rows"].index("Check")]))
        # phone cases never reach Check; every web case does.
        assert cell == {
            "phone": {"value": 0.0, "text": "0.0%", "state": None,
                      "nCases": 40},
            "web": {"value": 100.0, "text": "100.0%", "state": None,
                    "nCases": 80}}

    def test_table_and_resource_and_variant(self, claims_dash):
        t = _titles(claims_dash["segments"])
        assert "avg duration, channel × month" in t
        assert "median duration by resource" in t
        assert "median duration by variant" in t

    def test_case_constant_resource_is_not_charted_twice(self, claims):
        """A resource that never changes within a case is detected as a
        case attribute too; the ``resource`` axis already covers it."""
        assert any(a.name == "org:resource" for a in claims.attributes)
        for s in _all_specs(suggest_dashboards(claims)):
            assert "attr:org:resource" not in (
                s.get("segment") or {}).values()

    def test_identifier_like_attribute_is_not_charted(self):
        """One value per case is an identifier, not a grouping."""
        df = _claims()
        df["case:ticket"] = df["case:concept:name"].str.upper()
        t = build_fact_table(df, log_name="ids")
        for s in _all_specs(suggest_dashboards(t)):
            assert "attr:case:ticket" not in (s.get("segment") or {}).values()


# ---------------------------------------------------------------------------
# Behavioural variants
# ---------------------------------------------------------------------------

class TestBehaviouralVariants:

    def test_only_proposed_when_the_table_carries_them(
            self, claims_dash, claims_bv_dash):
        assert "variants" not in claims_dash
        assert claims_bv_dash["variants"]["name"] == "Behavioural variants"

    def test_says_how_far_the_sequences_fold(self, claims_bv_dash):
        """Ten activity sequences, six behaviours: Notify and Log in either
        order are one, and so is every number of Check repetitions past
        the first."""
        why = claims_bv_dash["variants"]["why"]
        assert why.startswith(
            "10 activity sequences fold into 6 behavioural variants")
        # Every case replays on a tree mined without noise.
        assert "fit the model" not in why

    def test_says_how_many_cases_fit_when_not_all_do(self):
        df = _claims()
        cl = _clustering(df)
        cl.variants = cl.variants[:-1]   # as if the rarest did not replay
        t = build_fact_table(df, log_name="claims", behaviour=cl)
        why = {d["key"]: d for d in suggest_dashboards(t)}["variants"]["why"]
        assert "97% of cases fit the model" in why

    def test_case_counts_by_variant(self, claims_bv, claims_bv_dash):
        """No catalog metric counts cases; the ƒ formula ``1`` summed
        does, and a sum is what a pie needs."""
        s = _by_title(claims_bv_dash["variants"],
                      "Cases by behavioural variant")
        assert s["viz"] == "pie" and s["agg"] == "sum"
        assert s["params"] == {"formula": "1"}
        series = compute_widget(s, claims_bv)["series"]
        assert [p["value"] for p in series] == [48, 32, 16, 12, 8, 4]
        assert "v1 covers 40% of cases and the top three 80%" in s["why"]
        assert "(Log || Notify)" in s["why"]

    def test_what_each_variant_consists_of(self, claims_bv, claims_bv_dash):
        s = _by_title(claims_bv_dash["variants"],
                      "Share of cases, activity × behavioural variant")
        w = compute_widget(s, claims_bv)
        assert w["cols"] == ["v1", "v2", "v3", "v4", "v5", "v6"]
        fast = [c["value"] for c in w["cells"][w["rows"].index("Fast Track")]]
        # A variant either contains an activity or does not.
        assert set(fast) == {0.0, 100.0}
        # Six variants need no narrowing.
        assert s["filter"] == []

    def test_many_variants_are_narrowed_by_a_visible_filter(self):
        from types import SimpleNamespace
        df = _claims()
        ids = sorted(df["case:concept:name"].unique())
        # Twelve variants, strictly decreasing in size, over all 120 cases.
        sizes = [16, 15, 14, 13, 12, 11, 10, 9, 8, 6, 4, 2]
        groups, at = [], 0
        for k, size in enumerate(sizes):
            groups.append(SimpleNamespace(
                variant_id=f"v{k + 1}", partial_order_expression=f"e{k}",
                case_ids=ids[at:at + size]))
            at += size
        t = build_fact_table(df, log_name="claims",
                             behaviour=SimpleNamespace(variants=groups))
        d = {x["key"]: x for x in suggest_dashboards(t)}["variants"]
        s = _by_title(d, "Share of cases, activity × behavioural variant")
        assert s["filter"] == [{
            "field": "attr:behavioural variant", "op": "in",
            "value": [f"v{k}" for k in range(1, 9)]}]
        assert len(compute_widget(s, t)["cols"]) == 8
        assert "the 8 most frequent variants" in s["why"]
        # The bars are not narrowed: the bar cap announces its own tail.
        assert _by_title(
            d, "median duration by behavioural variant")["filter"] == []

    def test_replaces_the_sequence_variant_bar(
            self, claims_dash, claims_bv_dash):
        assert "median duration by variant" in _titles(
            claims_dash["segments"])
        assert "median duration by variant" not in _titles(
            claims_bv_dash["segments"])

    def test_it_is_not_scored_as_a_case_attribute(self, claims_bv_dash):
        """It is derived from the trace, so it would "explain" the variant
        perfectly and crowd out every real attribute."""
        for key in ("bottlenecks", "rework", "segments"):
            for s in claims_bv_dash[key]["specs"]:
                assert "attr:behavioural variant" not in (
                    s.get("segment") or {}).values()
        assert claims_bv_dash["segments"]["specs"][0]["title"] \
            == "median duration by channel"


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

class TestTargets:

    def test_suggested_targets_are_off(self, claims):
        specs = _all_specs(suggest_dashboards(claims))
        assert any(s.get("target") for s in specs)
        assert not any(s["target"]["on"] for s in specs if s.get("target"))
        # ...so nothing suggested reaches the scorecard until the analyst
        # turns a target on.
        assert scorecard(specs, claims) == []

    def test_baseline_is_todays_value(self, claims, claims_dash):
        s = _by_title(claims_dash["overview"], "median case duration")
        value = compute_widget(s, claims)["value"]
        assert s["target"]["dir"] == "<="
        assert s["target"]["value"] == pytest.approx(value, rel=5e-3)
        assert s["target"]["warn"] == pytest.approx(value * 1.2, rel=5e-3)

    def test_only_unsegmented_kpis_carry_one(self, claims):
        for s in _all_specs(suggest_dashboards(claims)):
            if s.get("target"):
                assert s["viz"] == "kpi" and not s["segment"]


# ---------------------------------------------------------------------------
# Everything proposed is a real, truthful widget
# ---------------------------------------------------------------------------

def _guard_ok(spec) -> bool:
    """The visualisation guards of ``docs/dashboards.md`` §6."""
    seg = spec.get("segment") or {}
    axes = [a for a in (seg.get("rows"), seg.get("cols")) if a]
    viz, series = spec["viz"], spec["metric"] in SERIES_METRICS
    if len(axes) == 2:
        return viz == "table"
    if series:
        return viz in ("bar", "line")
    if not axes:
        if viz == "gauge":
            return bool((spec.get("target") or {}).get("on"))
        return viz in ("kpi", "hist", "box")
    if viz == "line":
        return axes[0] in TIME_AXES
    if viz == "pie":
        return spec.get("agg") == "sum"
    return viz == "bar"


def _with_behaviour(df):
    return build_fact_table(df, log_name="bv", behaviour=_clustering(df))


_FIXTURES = {
    "claims": lambda: _claims(),
    "claims-bv": lambda: _with_behaviour(_claims()),
    "tiny": lambda: _log(),
    "long": lambda: _claims(n=60, months=30),
    "two-cases": lambda: _log()[lambda d: d["case:concept:name"].isin(
        ["c0", "c2"])],
}


@pytest.fixture(scope="module", params=sorted(_FIXTURES))
def any_table(request):
    made = _FIXTURES[request.param]()
    if not isinstance(made, pd.DataFrame):
        return made  # already a fact table
    return build_fact_table(made, log_name=request.param)


class TestEverySuggestionIsSound:

    def test_computes_in_python(self, any_table):
        for s in _all_specs(suggest_dashboards(any_table)):
            w = compute_widget(s, any_table)
            assert w["nCases"] == any_table.n_cases, s["title"]

    def test_metric_and_aggregation_are_offered(self, any_table):
        for s in _all_specs(suggest_dashboards(any_table)):
            if s["metric"] == "custom":
                compile_formula(s["params"]["formula"])
                continue
            metric = BY_ID[s["metric"]]
            assert not metric.needs_interval or any_table.interval_log
            assert s.get("agg", metric.default_agg) in metric.aggs, s["title"]
            want = {p.name for p in metric.params}
            if "activity" in (s.get("segment") or {}).values():
                # The Activity axis supplies the activity itself.
                assert "activity" in want, s["title"]
                want.discard("activity")
            assert set(s["params"]) == want, s["title"]

    def test_visualisation_guards_hold(self, any_table):
        for s in _all_specs(suggest_dashboards(any_table)):
            assert _guard_ok(s), (s["title"], s["viz"], s["segment"])

    def test_segment_axes_have_something_to_draw(self, any_table):
        """No chart of one bar, no line of two points, no 1 × n table."""
        for s in _all_specs(suggest_dashboards(any_table)):
            w = compute_widget(s, any_table)
            if s["viz"] in ("bar", "pie"):
                assert len(w["series"]) >= 2, s["title"]
            elif s["viz"] == "line":
                assert len(w["series"]) >= 3, s["title"]
            elif s["viz"] == "table":
                assert len(w["rows"]) >= 2 and len(w["cols"]) >= 2, s["title"]

    def test_binds_and_computes_in_the_browser_engine(
            self, any_table, tmp_path):
        """The page renders these, so the JS engine has the last word:
        nothing unbound, no error, and the same headline value as Python.

        (Value, not text: a duration of exactly one hour formats as "1.0 h"
        or "60.0 m" depending on the last bit of a float mean, which the two
        engines sum in different orders. That is the engines' business, not
        the proposer's.)"""
        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not available")
        specs = _all_specs(suggest_dashboards(any_table))
        js = _run_js(any_table.to_payload(), specs, [[]],
                     catalog_json(interval_log=any_table.interval_log),
                     tmp_path)[:-1]
        for spec, w in zip(specs, js):
            assert "error" not in w, (spec["title"], w.get("error"))
            py = compute_widget(spec, any_table).get("value")
            if py is None:
                assert w.get("value") is None, spec["title"]
            else:
                assert w["value"] == pytest.approx(py), spec["title"]

        runner = tmp_path / "unbound.mjs"
        runner.write_text(
            "import * as E from %s;\n"
            "import { readFileSync } from 'node:fs';\n"
            "const inp = JSON.parse(readFileSync(process.argv[2], 'utf8'));\n"
            "const t = E.decodePayload(inp.payload);\n"
            "process.stdout.write(JSON.stringify(\n"
            "  inp.specs.map((s) => E.unboundRefs(s, t))));\n"
            % json.dumps(ENGINE_JS.resolve().as_uri()), encoding="utf8")
        proc = subprocess.run(
            [node, str(runner), str(tmp_path / "input.json")],
            capture_output=True, text=True, encoding="utf8", timeout=120)
        assert proc.returncode == 0, proc.stderr
        for spec, refs in zip(specs, json.loads(proc.stdout)):
            assert refs == {"activities": [], "attributes": []}, spec["title"]


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def _page_config(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="pm-data">(.*?)'
                  r'</script>', html, re.S)
    return json.loads(m.group(1))


class TestView:

    def test_suggestions_travel_in_the_config(self, tiny):
        sugg = suggest_dashboards(tiny)
        cfg = _page_config(dashboard_html(tiny, suggestions=sugg))
        assert cfg["suggestions"] == sugg

    def test_default_is_none(self, tiny):
        assert _page_config(dashboard_html(tiny))["suggestions"] == []

    def test_a_read_only_page_carries_none(self, tiny):
        """A reader cannot add a dashboard, so the list would be dead
        weight in an export."""
        cfg = _page_config(dashboard_html(
            tiny, suggestions=suggest_dashboards(tiny), read_only=True))
        assert cfg["suggestions"] == []

    def test_gallery_is_wired(self):
        ui = (ASSETS / "dash-ui.js").read_text(encoding="utf8")
        assert "_openSuggestions()" in ui and "_addSuggested(s)" in ui
        assert "✦ Suggest" in ui
        # Offered only where a dashboard can be restructured.
        assert "this.suggestions = this._multi" in ui

    def test_client_side_exports_strip_the_list(self):
        """The page exports itself by cloning its own config, which would
        otherwise carry the suggestions into every read-only file."""
        ui = (ASSETS / "dash-ui.js").read_text(encoding="utf8")
        for fn in ("exportHtml()", "exportAllHtml()"):
            body = ui.split(f"  {fn} {{", 1)[1].split("\n  }\n", 1)[0]
            assert "cfg.suggestions = []" in body, fn
        report = (ASSETS / "dash-report.js").read_text(encoding="utf8")
        assert "cfg.suggestions = []" in report

    def test_adding_never_overwrites_a_built_dashboard(self):
        """Add fills the current dashboard only when it is empty; otherwise
        it creates a new one."""
        ui = (ASSETS / "dash-ui.js").read_text(encoding="utf8")
        body = ui.split("  _addSuggested(s) {", 1)[1].split("\n  }\n", 1)[0]
        assert "if (!this.specs.length)" in body
        assert "this._reg.dashboards.push(" in body


class TestSampleLogs:
    """The logs the app ships: every suggestion computes on real data."""

    def test_default_budget_fits_the_overview_widgets(self, claims):
        """Ten widgets a dashboard: enough for the KPIs *and* the wider
        charts that follow them."""
        for d in suggest_dashboards(claims):
            assert len(d["specs"]) <= 10

    @pytest.mark.parametrize("name", ["devlog.csv"])
    def test_sample(self, name):
        path = Path(__file__).resolve().parents[1] / "web" / "samples" / name
        if not path.exists():
            pytest.skip(f"{name} not present")
        df = pd.read_csv(path)
        df["time:timestamp"] = pd.to_datetime(df["time:timestamp"], utc=True,
                                              format="mixed")
        t = build_fact_table(df, log_name=name)
        dashboards = suggest_dashboards(t)
        assert dashboards[0]["key"] == "overview"
        for s in _all_specs(dashboards):
            assert _guard_ok(s), s["title"]
            compute_widget(s, t)


class TestEditingASuggestedWidget:
    """A suggestion is a starting point, so its widgets get edited. Two
    composer defects made that destructive; both are pinned at the source
    because the composer has no DOM harness."""

    @staticmethod
    def _init_block() -> str:
        ui = (ASSETS / "dash-ui.js").read_text(encoding="utf8")
        return ui.split("metricSel.value = spec.metric;", 1)[1].split(
            "drawChips();", 1)[0]

    def test_params_are_read_before_the_formula_validator_rewrites_them(self):
        """``validateFormula()`` sets ``spec.params = {formula}`` for every
        metric. Reading the edited widget's params after it restored
        nothing, so the activity pickers fell back to the log's defaults."""
        body = self._init_block()
        assert body.index("const savedParams") < body.index(
            "validateFormula();") < body.index("rebuildParams(savedParams)")
        assert "rebuildParams(isEdit ? spec.params" not in body

    def test_the_saved_chart_shape_is_restored(self):
        """Only hist/box used to be restored; a line (or pie, or gauge)
        reopened as the first shape offered and saved back as bars."""
        body = self._init_block()
        assert 'chartPick.select(isEdit ? spec.viz : "kpi")' in body
