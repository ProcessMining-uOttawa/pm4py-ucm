"""Tests for the family statistics layer and the HTML report.

Covers: process-level statistics (case counts, events per case, case
durations including the TOTAL), activity-level statistics (frequency,
coverage, service-time min/mean/median/max/total on interval logs),
skeleton-aligned choice statistics (shared forks vs variation-point
forks, per-cell branch counts, context naming), the pandas helper
frames, JSON serialisation, and the self-contained HTML report
(structure, embedded data, determinism, image handling).

All tests inject deterministic toy tree miners so they run without
pm4py; report-image tests skip without graphviz."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile

import pytest

pd = pytest.importorskip("pandas")

import pm4py_ucm
from pm4py_ucm.algo.discovery.families import discover
from pm4py_ucm.algo.discovery.families.report import (
    family_report_html,
    write_family_report,
)
from pm4py_ucm.algo.discovery.families.stats import compute_family_stats
from pm4py_ucm.algo.performance import compute_performance_stats


_GRAPHVIZ = shutil.which("dot") is not None


class T:
    """Duck-typed process-tree node (converter protocol)."""

    def __init__(self, operator=None, label=None, children=None):
        self.operator = operator
        self.label = label
        self.children = children or []


def _xor_miner(df):
    """Same shape for every cell: Register -> (Triage | Scan) -> Done."""
    return T("->", children=[
        T(label="Register"),
        T("X", children=[T(label="Triage"), T(label="Scan")]),
        T(label="Done"),
    ])


def _variant_miner(df):
    """Different shapes per cell: Breast keeps an XOR after Register;
    Lung has a loop followed by its own XOR — exercises variation-point
    choices and their context naming."""
    acts = set(df["concept:name"])
    if "Chemo" in acts:
        return T("->", children=[
            T(label="Register"),
            T("*", children=[T(label="Chemo"), T(label="Review")]),
            T("X", children=[T(label="Scan"), T(label="Home")]),
            T(label="Done"),
        ])
    return T("->", children=[
        T(label="Register"),
        T("X", children=[T(label="Triage"), T(label="Scan")]),
        T(label="Done"),
    ])


def _make_log(intervals=True, timestamps=True):
    """Two cancer types; Breast picks Triage 4× / Scan 2×, Lung picks
    Triage 1× / Scan 5×. Every activity takes exactly 2 minutes
    (interval logs); consecutive events are 10 minutes apart."""
    rows = []
    ts = pd.Timestamp("2026-01-01")

    def add_case(cid, mid, ctype):
        nonlocal ts
        for act in ("Register", mid, "Done"):
            row = {
                "case:concept:name": cid,
                "concept:name": act,
                "case:cancer_type": ctype,
            }
            if timestamps:
                row["time:timestamp"] = ts
                if intervals:
                    row["start_timestamp"] = ts - pd.Timedelta(minutes=2)
            rows.append(row)
            ts += pd.Timedelta(minutes=10)
        ts += pd.Timedelta(hours=1)

    i = 0
    for mid in ["Triage"] * 4 + ["Scan"] * 2:
        add_case(f"b{i:02d}", mid, "Breast"); i += 1
    for mid in ["Triage"] * 1 + ["Scan"] * 5:
        add_case(f"l{i:02d}", mid, "Lung"); i += 1
    return pd.DataFrame(rows)


def _make_variant_log():
    """Log matching :func:`_variant_miner`: Breast cases follow
    Register-(Triage|Scan)-Done; Lung cases run Chemo (with optional
    Review-Chemo rework) then choose Scan or Home before Done."""
    rows = []
    ts = pd.Timestamp("2026-01-01")

    def add_case(cid, acts, ctype):
        nonlocal ts
        for act in acts:
            rows.append({
                "case:concept:name": cid,
                "concept:name": act,
                "time:timestamp": ts,
                "case:cancer_type": ctype,
            })
            ts += pd.Timedelta(minutes=7)
        ts += pd.Timedelta(hours=2)

    i = 0
    for _ in range(3):
        add_case(f"b{i}", ["Register", "Triage", "Done"], "Breast"); i += 1
    add_case(f"b{i}", ["Register", "Scan", "Done"], "Breast"); i += 1
    add_case(f"l{i}", ["Register", "Chemo", "Scan", "Done"], "Lung"); i += 1
    for _ in range(2):
        add_case(f"l{i}",
                 ["Register", "Chemo", "Review", "Chemo", "Home", "Done"],
                 "Lung"); i += 1
    add_case(f"l{i}", ["Register", "Chemo", "Home", "Done"], "Lung"); i += 1
    return pd.DataFrame(rows)


def _discover(df, miner=_xor_miner, attributes=("cancer_type",), **kw):
    kw.setdefault("min_cases", 1)
    return discover(
        df, list(attributes),
        parameters={"tree_miner": miner, "resource_attribute": False},
        **kw,
    )


# ---------------------------------------------------------------------------
# compute_performance_stats gains min/max (report prerequisite)
# ---------------------------------------------------------------------------

class TestPerformanceMinMax:

    def test_min_max_service_times(self):
        df = _make_log(intervals=True)
        stats = compute_performance_stats(df)
        entry = stats.activity["Register"]
        assert entry["min_time"] == 120.0
        assert entry["max_time"] == 120.0
        assert entry["total_time"] == 12 * 120.0  # 12 cases x 2 min

    def test_min_max_absent_without_intervals(self):
        df = _make_log(intervals=False)
        stats = compute_performance_stats(df)
        assert "min_time" not in stats.activity["Register"]
        assert "max_time" not in stats.activity["Register"]


# ---------------------------------------------------------------------------
# Process level
# ---------------------------------------------------------------------------

class TestProcessStats:

    def test_counts_and_events_per_case(self):
        stats = compute_family_stats(_discover(_make_log()))
        assert stats.cell_labels == ["Breast", "Lung"]
        breast = stats.cells[0]
        assert breast.n_cases == 6
        assert breast.n_events == 18
        assert breast.n_activities == 4
        assert breast.events_per_case == {
            "mean": 3.0, "median": 3.0, "min": 3.0, "max": 3.0,
        }

    def test_case_durations_include_total(self):
        stats = compute_family_stats(_discover(_make_log()))
        breast = stats.cells[0]
        # First activity starts 2 min before its completion; the case
        # spans 2 x 10 min between completions + that lead-in = 22 min.
        assert breast.duration["mean"] == 22 * 60.0
        assert breast.duration["min"] == 22 * 60.0
        assert breast.duration["max"] == 22 * 60.0
        assert breast.duration["total"] == 6 * 22 * 60.0

    def test_variant_counts_and_fitness(self):
        stats = compute_family_stats(_discover(_make_log()))
        for cell in stats.cells:
            # Two paths through the XOR = two behavioural variants.
            assert cell.variants["n_variants"] == 2
            assert cell.variants["fitness"] == 1.0

    def test_no_timestamp_log_has_no_durations(self):
        stats = compute_family_stats(
            _discover(_make_log(timestamps=False)))
        assert stats.has_timestamps is False
        assert stats.has_intervals is False
        assert stats.cells[0].duration == {}
        # Frequencies still present.
        assert stats.cells[0].activity["Register"]["frequency"] == 6

    def test_requires_log_df(self):
        family = _discover(_make_log())
        family.log_df = None
        with pytest.raises(ValueError, match="log_df"):
            compute_family_stats(family)


# ---------------------------------------------------------------------------
# Activity level
# ---------------------------------------------------------------------------

class TestActivityStats:

    def test_frequencies_and_coverage(self):
        stats = compute_family_stats(_discover(_make_log()))
        breast, lung = stats.cells
        assert breast.activity["Triage"]["frequency"] == 4
        assert breast.activity["Scan"]["frequency"] == 2
        assert lung.activity["Triage"]["frequency"] == 1
        assert lung.activity["Scan"]["frequency"] == 5
        assert breast.activity["Register"]["case_coverage"] == 6

    def test_service_times_min_mean_max_total(self):
        stats = compute_family_stats(_discover(_make_log()))
        assert stats.has_intervals is True
        entry = stats.cells[0].activity["Triage"]
        assert entry["min_time"] == 120.0
        assert entry["mean_time"] == 120.0
        assert entry["max_time"] == 120.0
        assert entry["total_time"] == 4 * 120.0

    def test_activity_names_union_sorted(self):
        stats = compute_family_stats(
            _discover(_make_variant_log(), miner=_variant_miner))
        assert stats.activity_names == sorted(stats.activity_names)
        assert "Chemo" in stats.activity_names      # lung-only
        assert "Triage" in stats.activity_names     # breast-only


# ---------------------------------------------------------------------------
# Choice level
# ---------------------------------------------------------------------------

class TestChoiceStats:

    def test_shared_fork_aligned_across_cells(self):
        stats = compute_family_stats(_discover(_make_log()))
        assert len(stats.choices) == 1
        ch = stats.choices[0]
        assert ch.shared is True
        assert ch.inside_loop is False
        assert ch.name == "after Register"
        assert ch.branches == ["Triage", "Scan"]
        assert ch.counts == [[4, 2], [1, 5]]

    def test_variation_point_forks(self):
        stats = compute_family_stats(
            _discover(_make_variant_log(), miner=_variant_miner))
        by_name = {ch.full_name: ch for ch in stats.choices}
        breast_ch = by_name["after Register: Triage vs Scan"]
        lung_ch = by_name["after Review: Scan vs Home"]
        assert breast_ch.shared is False and lung_ch.shared is False
        # Breast reaches only its own fork; Lung only its own.
        assert breast_ch.counts == [[3, 1], None]
        assert lung_ch.counts == [None, [1, 3]]

    def test_full_name_and_ids_deterministic(self):
        s1 = compute_family_stats(_discover(_make_log()))
        s2 = compute_family_stats(_discover(_make_log()))
        assert [c.choice_id for c in s1.choices] == \
               [c.choice_id for c in s2.choices]
        assert [c.full_name for c in s1.choices] == \
               [c.full_name for c in s2.choices]


# ---------------------------------------------------------------------------
# Frames + serialisation
# ---------------------------------------------------------------------------

class TestFramesAndSerialisation:

    def test_process_frame(self):
        stats = compute_family_stats(_discover(_make_log()))
        frame = stats.process_frame()
        assert list(frame.index) == ["Breast", "Lung"]
        assert frame.loc["Breast", "cases"] == 6
        assert frame.loc["Breast", "duration_total_s"] == 6 * 22 * 60.0

    def test_activity_frame(self):
        stats = compute_family_stats(_discover(_make_log()))
        frame = stats.activity_frame("frequency")
        assert frame.loc["Scan", "Lung"] == 5
        assert list(frame.index) == sorted(frame.index)

    def test_choice_share_frame(self):
        stats = compute_family_stats(_discover(_make_log()))
        share = stats.choice_share_frame(stats.choices[0])
        assert share.loc["Breast", "Triage"] == pytest.approx(400 / 6)
        assert share.loc["Lung", "Scan"] == pytest.approx(500 / 6)

    def test_to_dict_json_round_trip(self):
        stats = compute_family_stats(_discover(_make_log()))
        payload = json.loads(json.dumps(stats.to_dict()))
        assert payload["totalCases"] == 12
        assert payload["cells"][0]["label"] == "Breast"
        assert payload["choices"][0]["counts"] == [[4, 2], [1, 5]]

    def test_stats_picklable_without_log(self):
        import pickle
        stats = compute_family_stats(_discover(_make_log()))
        clone = pickle.loads(pickle.dumps(stats))
        assert clone.cell_labels == stats.cell_labels


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

def _report_data(html: str) -> dict:
    match = re.search(r"const DATA = (\{.*?\});\n", html, re.DOTALL)
    assert match, "embedded DATA object not found"
    return json.loads(match.group(1).replace("<\\/", "</"))


class TestReport:

    def test_report_structure_and_embedded_data(self):
        family = _discover(_make_log())
        html = family_report_html(family, images=False)
        assert html.startswith("<!DOCTYPE html>")
        for needle in ("Overview", "Compare", "Activities",
                       "Choices", "Models"):
            assert needle in html
        data = _report_data(html)
        assert [c["label"] for c in data["cells"]] == ["Breast", "Lung"]
        assert data["choices"][0]["counts"] == [[4, 2], [1, 5]]
        assert data["cells"][0]["duration"]["total"] == 6 * 22 * 60.0
        assert "image" not in data["cells"][0]

    def test_write_report_and_determinism(self):
        family = _discover(_make_log())
        stats = compute_family_stats(family)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "report.html")
            assert write_family_report(
                family, path, stats=stats, images=False,
            ) == path
            with open(path, encoding="utf-8") as fh:
                first = fh.read()
        second = family_report_html(family, stats=stats, images=False)
        assert first == second  # no timestamps, fully deterministic

    def test_custom_title_escaped(self):
        family = _discover(_make_log())
        html = family_report_html(
            family, images=False, title='A <b>"title"</b> & more',
        )
        assert "A &lt;b&gt;&quot;title&quot;&lt;/b&gt; &amp; more" in html

    def test_report_via_api(self):
        family = _discover(_make_log())
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "r.html")
            pm4py_ucm.write_family_report(family, path, images=False)
            assert os.path.getsize(path) > 10_000

    @pytest.mark.skipif(not _GRAPHVIZ, reason="graphviz not on PATH")
    def test_images_embedded(self):
        family = _discover(_make_log())
        html = family_report_html(family, images=True)
        data = _report_data(html)
        assert all(
            c["image"].startswith("data:image/png;base64,")
            for c in data["cells"]
        )

    def test_stats_survive_log_drop(self):
        """The web-app flow: stats computed at mine time, log dropped,
        report generated later from the stashed stats."""
        family = _discover(_make_log())
        stats = compute_family_stats(family)
        family.log_df = None
        html = family_report_html(family, stats=stats, images=False)
        assert _report_data(html)["totalCases"] == 12
