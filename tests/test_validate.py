"""Structural well-formedness of a UCM.

A model can serialise, render and even traverse while being structurally
impossible — a responsibility sitting on two incoming path segments is not
a UCM, but nothing downstream complained. These tests pin the arity rules
and the bug that motivated writing them down.
"""
from __future__ import annotations

import pytest

from pm4py_ucm.objects.ucm.obj import UCM
from pm4py_ucm.objects.ucm.validate import (
    StructuralProblem, check_ucm, validate_ucm,
)


def _linear():
    """`start -> A -> end`, the smallest well-formed map."""
    u = UCM(name="ok")
    m = u.add_map(name="m")
    sp = m.add_node(UCM.StartPoint(name="start"))
    a = m.add_node(UCM.RespRef(
        name="A", resp_def=u.get_or_add_responsibility("A")))
    ep = m.add_node(UCM.EndPoint(name="end"))
    m.add_connection(sp, a)
    m.add_connection(a, ep)
    return u, m, sp, a, ep


class TestWellFormed:

    def test_a_linear_map_has_no_problems(self):
        u, *_ = _linear()
        assert validate_ucm(u) == []

    def test_check_does_not_raise_on_a_good_model(self):
        u, *_ = _linear()
        check_ucm(u)                      # must not raise

    def test_a_fork_and_join_pair_is_well_formed(self):
        u = UCM(name="fork")
        m = u.add_map(name="m")
        sp = m.add_node(UCM.StartPoint(name="s"))
        fork = m.add_node(UCM.OrFork(name="f"))
        join = m.add_node(UCM.OrJoin(name="j"))
        a = m.add_node(UCM.RespRef(
            name="A", resp_def=u.get_or_add_responsibility("A")))
        b = m.add_node(UCM.RespRef(
            name="B", resp_def=u.get_or_add_responsibility("B")))
        ep = m.add_node(UCM.EndPoint(name="e"))
        m.add_connection(sp, fork)
        m.add_connection(fork, a)
        m.add_connection(fork, b)
        m.add_connection(a, join)
        m.add_connection(b, join)
        m.add_connection(join, ep)
        assert validate_ucm(u) == []


class TestArityViolations:

    def test_a_responsibility_with_two_incoming_is_reported(self):
        """The bug this module was written for."""
        u, m, sp, a, ep = _linear()
        second = m.add_node(UCM.StartPoint(name="other"))
        m.add_connection(second, a)
        problems = validate_ucm(u)
        assert [p.node_type for p in problems] == ["RespRef"]
        assert "2 incoming (expected 1)" in problems[0].detail

    def test_a_fork_with_one_outgoing_is_reported(self):
        """A fork that forks nothing is a fork in name only."""
        u = UCM(name="f")
        m = u.add_map(name="m")
        sp = m.add_node(UCM.StartPoint(name="s"))
        fork = m.add_node(UCM.OrFork(name="f"))
        ep = m.add_node(UCM.EndPoint(name="e"))
        m.add_connection(sp, fork)
        m.add_connection(fork, ep)
        kinds = {(p.node_type, p.detail) for p in validate_ucm(u)}
        assert any(k[0] == "OrFork" and "outgoing" in k[1] for k in kinds)

    def test_a_join_with_one_incoming_is_reported(self):
        u = UCM(name="j")
        m = u.add_map(name="m")
        sp = m.add_node(UCM.StartPoint(name="s"))
        join = m.add_node(UCM.OrJoin(name="j"))
        ep = m.add_node(UCM.EndPoint(name="e"))
        m.add_connection(sp, join)
        m.add_connection(join, ep)
        assert any(p.node_type == "OrJoin" and "incoming" in p.detail
                   for p in validate_ucm(u))

    def test_a_start_point_with_an_incoming_arc_is_reported(self):
        u, m, sp, a, ep = _linear()
        m.add_connection(a, sp)
        assert any(p.node_type == "StartPoint" for p in validate_ucm(u))

    def test_check_raises_and_names_every_problem(self):
        u, m, sp, a, ep = _linear()
        other = m.add_node(UCM.StartPoint(name="other"))
        m.add_connection(other, a)
        with pytest.raises(ValueError) as exc:
            check_ucm(u)
        assert "RespRef" in str(exc.value)
        assert "2 incoming" in str(exc.value)

    def test_the_message_locates_the_node(self):
        u, m, sp, a, ep = _linear()
        m.add_connection(m.add_node(UCM.StartPoint(name="o")), a)
        text = str(validate_ucm(u)[0])
        assert "m" in text and "RespRef" in text and "'A'" in text


class TestKindsWithLooserRules:

    def test_a_stub_needs_only_one_arc_each_way(self):
        u = UCM(name="s")
        m = u.add_map(name="m")
        sp = m.add_node(UCM.StartPoint(name="s"))
        stub = m.add_node(UCM.Stub(name="S"))
        ep = m.add_node(UCM.EndPoint(name="e"))
        m.add_connection(sp, stub)
        m.add_connection(stub, ep)
        assert validate_ucm(u) == []

    def test_an_isolated_stub_is_still_reported(self):
        u = UCM(name="s")
        m = u.add_map(name="m")
        m.add_node(UCM.Stub(name="S"))
        assert any(p.node_type == "Stub" for p in validate_ucm(u))

    def test_a_timer_is_checked_as_itself_not_as_its_base_class(self):
        """`Timer` subclasses `WaitingPlace`; an isinstance-based lookup
        would silently apply the wrong rule to one of them."""
        u = UCM(name="t")
        m = u.add_map(name="m")
        sp = m.add_node(UCM.StartPoint(name="s"))
        timer = m.add_node(UCM.Timer(name="T"))
        ep = m.add_node(UCM.EndPoint(name="e"))
        m.add_connection(sp, timer)
        m.add_connection(timer, ep)
        # Timer has no rule of its own, so it is simply not constrained —
        # what must NOT happen is WaitingPlace's rule being applied to it
        # by accident, or a crash.
        assert [p.node_type for p in validate_ucm(u)] == []


class TestMinedModels:
    """The generators must produce structurally valid models."""

    @pytest.fixture(scope="class")
    def claims(self):
        pm4py = pytest.importorskip("pm4py")
        import zipfile
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        xes = root / "demo" / "ClaimsPaymentLog.xes"
        if not xes.exists():
            zf = root / "demo" / "ClaimsPaymentLog.zip"
            if not zf.exists():
                pytest.skip("bundled log unavailable")
            with zipfile.ZipFile(zf) as z:
                z.extractall(root / "demo")
        return pm4py.read_xes(str(xes))

    def test_a_mined_model_is_well_formed(self, claims):
        import pm4py_ucm
        assert validate_ucm(pm4py_ucm.discover_ucm_inductive(claims)) == []

    def test_a_decomposed_model_is_well_formed(self, claims):
        import pm4py_ucm
        ucm = pm4py_ucm.discover_ucm_inductive(claims, decomposition="auto")
        assert validate_ucm(ucm) == []

    def test_a_synthesized_model_is_well_formed(self, claims):
        """The regression.

        Condition emission splices a LoopEntryGuard whose bypass arc has
        to land somewhere. It used to land directly on the loop's exit
        node, giving a responsibility — or an AND-fork — a second incoming
        path segment. The model still exported, rendered and traversed,
        which is exactly why it went unnoticed.
        """
        import pm4py, pm4py_ucm
        tree = pm4py.discover_process_tree_inductive(claims,
                                                     noise_threshold=0.0)
        ucm, _ = pm4py_ucm.discover_scenarios(
            claims, parameters={"process_tree": tree})
        assert validate_ucm(ucm) == []

    def test_the_bypass_merges_through_a_join(self, claims):
        """Not just "no problems" — the bypass must reach its target
        through an OrJoin, which is the construct for alternatives."""
        import pm4py, pm4py_ucm
        tree = pm4py.discover_process_tree_inductive(claims,
                                                     noise_threshold=0.0)
        ucm, _ = pm4py_ucm.discover_scenarios(
            claims, parameters={"process_tree": tree})
        guards = [n for m in ucm.maps for n in m.nodes
                  if isinstance(n, UCM.OrFork) and n.name == "LoopEntryGuard"]
        assert guards, "the fixture must exercise loop entry guards"
        for g in guards:
            bypass = [a for a in g.succ_connections
                      if a.condition and "<= 0" in a.condition.expression]
            assert bypass
            for arc in bypass:
                assert isinstance(arc.target, (UCM.OrJoin, UCM.Stub)), (
                    f"bypass landed on {type(arc.target).__name__}, which "
                    "admits only one incoming segment")
