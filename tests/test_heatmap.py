"""Tests for the performance heat-map (render-time emphasis) in
:mod:`pm4py_ucm.visualization.ucm.variants.classic`.

The pure helpers (value parsing, ramp, per-diagram normalisation, time
classification) run anywhere; the end-to-end render (colours + thickened
strokes actually reach the SVG, scoped per map) is skipped without graphviz.
"""
from __future__ import annotations

import re
import shutil

import pytest

pd = pytest.importorskip("pandas")

from pm4py_ucm.visualization.ucm.variants import classic as _classic


# -- pure helpers -----------------------------------------------------------

def test_metric_is_time():
    for m in ("mean_time", "median_time", "sojourn_p90_time", "total_time"):
        assert _classic._metric_is_time(m)
    for m in ("frequency", "case_coverage", "relative_frequency",
              "percentage", "repeat_frequency", None, ""):
        assert not _classic._metric_is_time(m)


def test_parse_perf_value():
    assert _classic._parse_perf_value("42") == 42.0
    assert _classic._parse_perf_value("50.0%") == 50.0
    assert _classic._parse_perf_value("30s") == 30.0
    assert _classic._parse_perf_value("2.5m") == 150.0
    assert _classic._parse_perf_value("1.5h") == 5400.0
    assert _classic._parse_perf_value("2d") == 172800.0
    assert _classic._parse_perf_value("1y") == pytest.approx(365.25 * 86400)
    for junk in (None, "", "   ", "n/a", "??"):
        assert _classic._parse_perf_value(junk) is None


def test_heat_normalize_scales_to_unit_interval():
    ts = _classic._heat_normalize({1: 10.0, 2: 20.0, 3: 30.0})
    assert ts[1] == 0.0 and ts[3] == 1.0 and ts[2] == pytest.approx(0.5)


def test_heat_normalize_degenerate_is_full_emphasis():
    # A single value, or all-equal, has no gradient -> full emphasis.
    assert _classic._heat_normalize({1: 7.0}) == {1: 1.0}
    assert _classic._heat_normalize({1: 5.0, 2: 5.0}) == {1: 1.0, 2: 1.0}
    assert _classic._heat_normalize({}) == {}


def test_heat_color_endpoints_and_ramp():
    # Time -> red ramp, else blue; t=0 light, t=1 dark.
    assert _classic._heat_color(0.0, True) == "#faaaaa"     # light pink
    assert _classic._heat_color(1.0, True) == "#7f1414"     # dark red
    assert _classic._heat_color(0.0, False) == "#96c3fa"    # light blue
    assert _classic._heat_color(1.0, False) == "#14378c"    # dark blue
    # Midpoint is between the endpoints on every channel.
    mid = _classic._heat_color(0.5, False)
    assert mid not in ("#96c3fa", "#14378c") and mid.startswith("#")


# -- end-to-end render ------------------------------------------------------

_GRAPHVIZ = shutil.which("dot") is not None


def _widths(svg):
    return {round(float(x), 3) for x in re.findall(r'stroke-width="([\d.]+)"', svg)}


def _strokes(svg):
    return set(re.findall(r'stroke="#([0-9a-f]{6})"', svg))


@pytest.mark.skipif(not _GRAPHVIZ, reason="graphviz 'dot' absent")
def test_heatmap_render_colours_and_thickens():
    import pm4py
    from pm4py_ucm import discover_ucm_inductive
    from pm4py_ucm.algo.performance import annotate_performance
    from pm4py_ucm.visualization.ucm import svg as _svgmod

    # A small synthetic log with clearly different activity frequencies, so
    # the per-diagram normalisation has a real min and max to span.
    rows = []
    for c in range(20):
        rows.append((f"c{c}", "A", c))          # A in every case
        if c < 10:
            rows.append((f"c{c}", "B", c + 1))  # B in half
        if c < 3:
            rows.append((f"c{c}", "C", c + 2))  # C rare
    df = pd.DataFrame(rows, columns=["case:concept:name", "concept:name",
                                     "time:timestamp"])
    df["time:timestamp"] = pd.to_datetime(df["time:timestamp"], unit="D")
    df = pm4py.format_dataframe(df)

    ucm = discover_ucm_inductive(df, decomposition="off")
    annotate_performance(ucm, log=df, node_metrics=["frequency"],
                         edge_metrics=["frequency"])

    off = _svgmod.model_to_svg(ucm, "bpmn", heatmap=False)
    on = _svgmod.model_to_svg(ucm, "bpmn", heatmap=True,
                              node_metric="frequency", edge_metric="frequency")

    # The heat-map adds distinct thicknesses and blue ramp colours the plain
    # render does not have.
    assert len(_widths(on)) > len(_widths(off))
    blues_on = {h for h in _strokes(on)
                if int(h[4:6], 16) > int(h[0:2], 16) + 20}
    assert blues_on, "expected blue ramp strokes with the heat-map on"
    # Frequency is not time-based, so no dark-red endpoint appears.
    assert "7f1414" not in _strokes(on)


@pytest.mark.skipif(not _GRAPHVIZ, reason="graphviz 'dot' absent")
def test_heatmap_off_leaves_render_unchanged():
    import pm4py
    from pm4py_ucm import discover_ucm_inductive
    from pm4py_ucm.visualization.ucm import svg as _svgmod

    df = pd.DataFrame(
        [(f"c{c}", a, i) for c in range(8)
         for i, a in enumerate(["A", "B", "C"])],
        columns=["case:concept:name", "concept:name", "time:timestamp"])
    df["time:timestamp"] = pd.to_datetime(df["time:timestamp"], unit="D")
    df = pm4py.format_dataframe(df)
    ucm = discover_ucm_inductive(df, decomposition="off")

    # No overlay metric selected -> heatmap flag has nothing to drive.
    a = _svgmod.model_to_svg(ucm, "bpmn", heatmap=True)
    b = _svgmod.model_to_svg(ucm, "bpmn", heatmap=False)
    assert a == b
