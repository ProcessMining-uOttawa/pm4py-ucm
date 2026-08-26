"""Coverage over a set of scenarios, and the A/B comparison.

The numbers here are what the 0.8.0 Scenarios-view section reports, and the
colour/tooltip maps are what it renders. See `docs/scenario_simulation.md`.

Two properties are worth stating because they are easy to get wrong and
neither is visible from a percentage: coverage is a **set** (a loop that
enters an element nine times covers it once), and the denominator is the
**whole model** including every plug-in map.
"""
from __future__ import annotations

import re

import pytest

from pm4py_ucm.objects.ucm.obj import UCM
from pm4py_ucm.algo import scenario_traversal as st
from pm4py_ucm.algo import scenario_coverage as sc


def _fork_model():
    """`start -> fork -> (A | B) -> end`, with a scenario down each branch."""
    u = UCM(name="fork")
    m = u.add_map(name="m")
    sp = m.add_node(UCM.StartPoint(name="start"))
    fork = m.add_node(UCM.OrFork(name="Choice"))
    ra = m.add_node(UCM.RespRef(
        name="A", resp_def=u.get_or_add_responsibility("A")))
    rb = m.add_node(UCM.RespRef(
        name="B", resp_def=u.get_or_add_responsibility("B")))
    ea = m.add_node(UCM.EndPoint(name="endA"))
    eb = m.add_node(UCM.EndPoint(name="endB"))
    var = u.get_or_add_variable("x", type="integer")
    m.add_connection(sp, fork)
    m.add_connection(fork, ra, condition=UCM.Condition(expression="x == 1"))
    m.add_connection(fork, rb, condition=UCM.Condition(expression="x == 2"))
    m.add_connection(ra, ea)
    m.add_connection(rb, eb)

    group = u.add_scenario_group(name="G")
    for name, value, end in (("a", "1", ea), ("b", "2", eb)):
        s = UCM.ScenarioDef(name=name)
        s._owner = u
        s.add_initialization(var, value)
        s.add_start_point(sp)
        s.add_end_point(end)
        group.scenarios.append(s)
    return u, group.scenarios[0], group.scenarios[1]


@pytest.fixture
def forked():
    u, sa, sb = _fork_model()
    return u, st.traverse_scenario(u, sa), st.traverse_scenario(u, sb)


class TestCoverage:

    def test_the_denominator_is_every_node_and_connection(self, forked):
        u, ra, _rb = forked
        cov = sc.coverage(u, [ra])
        expected = sum(len(m.nodes) + len(m.connections) for m in u.maps)
        assert len(cov.total) == expected

    def test_one_branch_does_not_cover_the_other(self, forked):
        u, ra, _rb = forked
        cov = sc.coverage(u, [ra])
        assert 0 < cov.fraction < 1
        assert cov.uncovered
        assert cov.covered | cov.uncovered == cov.total

    def test_both_scenarios_together_cover_more(self, forked):
        u, ra, rb = forked
        one = sc.coverage(u, [ra]).fraction
        both = sc.coverage(u, [ra, rb]).fraction
        assert both > one

    def test_coverage_is_a_set_not_a_sum(self):
        """A loop entering an element repeatedly still covers it once."""
        u = _looping_model(iterations=3)
        result = st.traverse_all(u)[0]
        cov = sc.coverage(u, [result])
        assert max(cov.hits.values()) > 1        # it really did loop
        assert len(cov.covered) <= len(cov.total)

    def test_by_kind_splits_the_single_percentage(self, forked):
        u, ra, _rb = forked
        by_kind = sc.coverage(u, [ra]).by_kind()
        assert by_kind["RespRef"] == (1, 2)      # one of the two branches
        covered, total = by_kind["NodeConnection"]
        assert 0 < covered < total

    def test_results_from_another_model_are_rejected(self, forked):
        """Silently reporting coverage against the wrong model would be
        worse than failing: the percentage would look plausible."""
        u, ra, _rb = forked
        other, _sa, _sb = _fork_model()
        with pytest.raises(ValueError, match="do not match"):
            sc.coverage(other, [ra])


class TestComparison:

    def test_the_three_way_partition_is_non_empty(self, forked):
        u, ra, rb = forked
        cmp = sc.compare(u, ra, rb)
        assert cmp.a_only and cmp.b_only and cmp.both

    def test_the_partition_is_disjoint_and_covers_the_union(self, forked):
        u, ra, rb = forked
        cmp = sc.compare(u, ra, rb)
        assert not (cmp.a_only & cmp.b_only)
        assert not (cmp.a_only & cmp.both)
        assert not (cmp.b_only & cmp.both)
        assert cmp.union == cmp.a_only | cmp.b_only | cmp.both
        assert cmp.union | cmp.neither == cmp.total

    def test_a_scenario_compared_with_itself_is_total_agreement(self, forked):
        u, ra, _rb = forked
        cmp = sc.compare(u, ra, ra)
        assert cmp.agreement == 1.0
        assert not cmp.a_only and not cmp.b_only

    def test_agreement_is_below_one_for_different_branches(self, forked):
        u, ra, rb = forked
        assert 0 < sc.compare(u, ra, rb).agreement < 1


class TestRenderBindings:

    def test_each_partition_gets_its_own_colour(self, forked):
        u, ra, rb = forked
        cmp = sc.compare(u, ra, rb)
        render = sc.comparison_render(u, cmp)
        colours = set(render["colors"].values())
        assert colours == {sc.COLOR_A, sc.COLOR_B, sc.COLOR_BOTH}

    def test_colours_are_keyed_by_object_identity_for_the_renderer(self, forked):
        u, ra, _rb = forked
        cov = sc.coverage(u, [ra])
        render = sc.coverage_render(u, cov)
        live = {id(el) for m in u.maps
                for coll in (m.nodes, m.connections) for el in coll}
        assert set(render["colors"]) <= live

    def test_uncovered_elements_still_get_hover_text(self, forked):
        """Otherwise they keep graphviz's default title, which is an
        internal name embedding a memory address."""
        u, ra, _rb = forked
        cov = sc.coverage(u, [ra])
        render = sc.coverage_render(u, cov)
        index = sc.model_elements(u)
        for k in cov.uncovered:
            assert "not covered" in render["tooltips"][id(index[k])]

    def test_every_element_is_hoverable(self, forked):
        u, ra, rb = forked
        render = sc.comparison_render(u, sc.compare(u, ra, rb))
        total = sum(len(m.nodes) + len(m.connections) for m in u.maps)
        assert len(render["tooltips"]) == total


class TestSvgOutput:
    """The colours and hover text must survive into the rendered SVG."""

    def test_the_partition_colours_reach_the_svg(self, forked):
        pytest.importorskip("graphviz")
        from pm4py_ucm.visualization.ucm import svg as ucm_svg
        u, ra, rb = forked
        out = ucm_svg.model_to_svg(
            u, "bpmn", coverage=sc.comparison_render(u, sc.compare(u, ra, rb)))
        for colour in (sc.COLOR_A, sc.COLOR_B, sc.COLOR_BOTH):
            assert colour.lower() in out.lower()

    def test_hover_text_replaces_the_internal_object_name(self, forked):
        """graphviz writes `<title>n<address></title>` on every element.

        It ignores its own `tooltip=` unless the element also has a URL, so
        the text is injected by rewriting those titles — which also stops
        the diagram exposing memory addresses on hover.
        """
        pytest.importorskip("graphviz")
        from pm4py_ucm.visualization.ucm import svg as ucm_svg
        u, ra, _rb = forked
        # One scenario, so part of the model is definitely uncovered — the
        # two branches together happen to cover this fixture entirely.
        cov = sc.coverage(u, [ra])
        assert cov.uncovered, "fixture must leave something uncovered"
        out = ucm_svg.model_to_svg(
            u, "bpmn", coverage=sc.coverage_render(u, cov))
        titles = re.findall(r"<title>(.*?)</title>", out, re.S)
        assert titles
        assert not [t for t in titles if re.fullmatch(r"n\d{6,}", t or "")], (
            "no element still shows graphviz's internal name")
        assert any("covered —" in t for t in titles), "covered elements say so"
        assert any("not covered" in t for t in titles), (
            "uncovered elements say so rather than showing an address")

    def test_coverage_replaces_the_heatmap_rather_than_stacking(self, forked):
        """They compete for the same colour channel, so only one can show."""
        pytest.importorskip("graphviz")
        from pm4py_ucm.visualization.ucm import svg as ucm_svg
        u, ra, rb = forked
        heat_only = ucm_svg.model_to_svg(
            u, "bpmn", heatmap=True, node_metric="frequency",
            edge_metric="frequency")
        with_coverage = ucm_svg.model_to_svg(
            u, "bpmn", heatmap=True, node_metric="frequency",
            edge_metric="frequency",
            coverage=sc.comparison_render(u, sc.compare(u, ra, rb)))
        assert heat_only != with_coverage
        assert sc.COLOR_BOTH.lower() in with_coverage.lower()


def _looping_model(iterations: int = 3):
    """`start -> R -> fork -(back)-> R ... -> end`, counted by a variable."""
    u = UCM(name="loop")
    m = u.add_map(name="m")
    var = u.get_or_add_variable("i", type="integer")
    sp = m.add_node(UCM.StartPoint(name="start"))
    r = m.add_node(UCM.RespRef(
        name="R", resp_def=u.get_or_add_responsibility("R")))
    r.resp_def.expression = "i = i + 1"
    fork = m.add_node(UCM.OrFork(name="again?"))
    join = m.add_node(UCM.OrJoin(name="join"))
    ep = m.add_node(UCM.EndPoint(name="end"))
    m.add_connection(sp, join)
    m.add_connection(join, r)
    m.add_connection(r, fork)
    m.add_connection(fork, join,
                     condition=UCM.Condition(expression=f"i < {iterations}"))
    m.add_connection(fork, ep,
                     condition=UCM.Condition(expression=f"i >= {iterations}"))
    group = u.add_scenario_group(name="G")
    s = UCM.ScenarioDef(name="loop")
    s._owner = u
    s.add_initialization(var, "0")
    s.add_start_point(sp)
    s.add_end_point(ep)
    group.scenarios.append(s)
    return u


class TestElementsAndPathsSplit:
    """One percentage cannot say whether the gap is behaviour or wiring.

    A run can walk every node and still miss segments — an alternative
    that carries no responsibility is a segment and nothing else — so the
    two are reported apart.
    """

    def test_the_split_is_exhaustive_and_disjoint(self, forked):
        u, ra, _rb = forked
        cov = sc.coverage(u, [ra])
        ec, et = cov.elements
        pc, pt = cov.paths
        assert ec + pc == len(cov.covered)
        assert et + pt == len(cov.total)

    def test_elements_are_nodes_and_paths_are_connections(self, forked):
        u, ra, _rb = forked
        cov = sc.coverage(u, [ra])
        _ec, et = cov.elements
        _pc, pt = cov.paths
        assert et == sum(len(m.nodes) for m in u.maps)
        assert pt == sum(len(m.connections) for m in u.maps)

    def test_fractions_match_their_counts(self, forked):
        u, ra, _rb = forked
        cov = sc.coverage(u, [ra])
        ec, et = cov.elements
        pc, pt = cov.paths
        assert cov.element_fraction == pytest.approx(ec / et)
        assert cov.path_fraction == pytest.approx(pc / pt)

    def test_full_coverage_reports_both_at_one(self, forked):
        u, ra, rb = forked
        cov = sc.coverage(u, [ra, rb])
        # Both branches together walk this whole fixture.
        assert cov.element_fraction == 1.0
        assert cov.path_fraction == 1.0

    def test_an_empty_model_does_not_divide_by_zero(self):
        u = UCM(name="empty")
        u.add_map(name="m")
        cov = sc.coverage(u, [])
        assert cov.element_fraction == 0.0
        assert cov.path_fraction == 0.0


class TestComparisonColours:

    def test_the_three_colours_are_distinct(self):
        assert len({sc.COLOR_A, sc.COLOR_B, sc.COLOR_BOTH}) == 3

    def test_a_and_b_differ_in_lightness_not_only_hue(self):
        """Hue alone fails on a greyscale printout and for some colour
        vision deficiencies; the pair must separate by lightness too."""
        def luma(hex_color):
            h = hex_color.lstrip("#")
            r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
            return 0.2126 * r + 0.7152 * g + 0.0722 * b
        assert abs(luma(sc.COLOR_A) - luma(sc.COLOR_B)) > 30
