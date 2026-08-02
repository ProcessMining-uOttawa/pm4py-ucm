"""Offline simulation of jUCMNav's scenario traversal.

Exporting a valid ``.jucm`` is not the same as exporting a model whose
scenarios *run*, and the static branch-guard check cannot tell the
difference: it asks whether exactly one branch holds at each fork under
the initial values, which says nothing about whether the resulting token
flow reaches the end point, and cannot be asked at all of forks inside a
loop. These tests cover the simulator that closes that gap.

Two behaviours here are worth more than the rest.

*Never report success on something not run.* A model with no scenarios
raises rather than returning "no problems": a caller asking whether a
model executes cleanly must not be told *yes* on the strength of having
executed nothing.

*The hit-count ceiling is a preference, not a property of the model.*
jUCMNav declares an infinite loop once a single element is entered
``maximum hit count`` times. Set that below what a model legitimately
needs and jUCMNav reports deadlocks that do not exist — the offending
visit is abandoned, its AND-joins starve, and the scenario never
finishes. A real model of ours needed 10 against a preference set to 10,
which produced eight errors in a model that is in fact correct.
"""
from __future__ import annotations

import pytest

from pm4py_ucm.objects.ucm.obj import UCM
from pm4py_ucm.algo import scenario_traversal as st


def _kinds(problems):
    return sorted(p.kind for p in problems)


def _scenario(ucm, name="s", inits=(), starts=(), ends=()):
    group = (ucm.scenario_groups[0] if ucm.scenario_groups
             else ucm.add_scenario_group(name="G"))
    sc = UCM.ScenarioDef(name=name)
    sc._owner = ucm
    for var, value in inits:
        sc.add_initialization(var, value)
    for sp in starts:
        sc.add_start_point(sp)
    for ep in ends:
        sc.add_end_point(ep)
    group.add_scenario(sc)
    return sc


# ---------------------------------------------------------------------------
# A model that simply works
# ---------------------------------------------------------------------------

def _linear_model():
    u = UCM(name="linear")
    m = u.add_map(name="m")
    sp = m.add_node(UCM.StartPoint(name="start"))
    a = m.add_node(UCM.RespRef(
        name="A", resp_def=u.get_or_add_responsibility("A")))
    ep = m.add_node(UCM.EndPoint(name="end"))
    m.add_connection(sp, a)
    m.add_connection(a, ep)
    _scenario(u, starts=[sp], ends=[ep])
    return u


class TestCleanTraversal:

    def test_a_linear_model_runs_without_problems(self):
        assert st.check_traversal(_linear_model()) == []

    def test_the_responsibility_actually_executed(self):
        result = st.traverse_all(_linear_model())[0]
        assert result.responsibilities == ["A"]
        assert result.reached_end_points == ["end"]


# ---------------------------------------------------------------------------
# Refusing to certify what was never run
# ---------------------------------------------------------------------------

class TestNothingToRun:

    def test_a_model_with_no_scenarios_raises_rather_than_passing(self):
        u = _linear_model()
        u.scenario_groups.clear()
        # The dangerous answer here is "[]" — no problems found.
        with pytest.raises(st.NoScenariosError):
            st.check_traversal(u)

    def test_a_scenario_enabling_no_start_point_is_reported(self):
        u = _linear_model()
        sc = u.scenario_groups[0].scenarios[0]
        sc.start_points[0].enabled = False
        assert "no_start_point" in _kinds(st.check_traversal(u))


# ---------------------------------------------------------------------------
# Deadlock: the failure the static guard check cannot see
# ---------------------------------------------------------------------------

def _deadlocking_model():
    """An AND-join one of whose arms is guarded away, so it never fires."""
    u = UCM(name="deadlock")
    m = u.add_map(name="m")
    sp = m.add_node(UCM.StartPoint(name="start"))
    ep = m.add_node(UCM.EndPoint(name="end"))
    fork = m.add_node(UCM.AndFork(name="AndFork"))
    join = m.add_node(UCM.AndJoin(name="AndJoin"))
    choice = m.add_node(UCM.OrFork(name="Choice"))
    away = m.add_node(UCM.EndPoint(name="elsewhere"))
    a = m.add_node(UCM.RespRef(
        name="A", resp_def=u.get_or_add_responsibility("A")))
    b = m.add_node(UCM.RespRef(
        name="B", resp_def=u.get_or_add_responsibility("B")))
    flag = u.get_or_add_variable("flag", type="boolean")
    m.add_connection(sp, fork)
    m.add_connection(fork, a)
    m.add_connection(fork, choice)
    m.add_connection(a, join)
    m.add_connection(choice, b,
                     condition=UCM.Condition(expression="flag == true"))
    m.add_connection(choice, away,
                     condition=UCM.Condition(expression="flag == false"))
    m.add_connection(b, join)
    m.add_connection(join, ep)
    _scenario(u, inits=[(flag, "false")], starts=[sp], ends=[ep])
    return u


class TestDeadlockDetection:

    def test_a_starved_and_join_is_reported(self):
        kinds = _kinds(st.check_traversal(_deadlocking_model()))
        assert "blocked_and_join" in kinds
        assert "end_point_not_reached" in kinds

    def test_the_report_names_the_join_and_the_missing_arms(self):
        problem = next(p for p in st.check_traversal(_deadlocking_model())
                       if p.kind == "blocked_and_join")
        assert problem.node_type == "AndJoin"
        assert problem.node_name == "AndJoin"
        assert "retried without progress" in problem.detail

    def test_the_same_model_passes_when_the_arm_is_taken(self):
        u = _deadlocking_model()
        u.scenario_groups[0].scenarios[0].initializations[0].value = "true"
        assert st.check_traversal(u) == []


# ---------------------------------------------------------------------------
# Loops, counters, and the hit-count ceiling
# ---------------------------------------------------------------------------

def _counted_loop(iterations: int = 3):
    """``start -> join -> body -> fork ..(redo).. -> end``, counter-guarded."""
    u = UCM(name="loop")
    m = u.add_map(name="m")
    sp = m.add_node(UCM.StartPoint(name="start"))
    ep = m.add_node(UCM.EndPoint(name="end"))
    loop_join = m.add_node(UCM.OrJoin(name="LoopJoin"))
    loop_fork = m.add_node(UCM.OrFork(name="LoopFork"))
    counter = u.get_or_add_variable("Loop_body", type="integer")
    decrement = u.get_or_add_responsibility("decrement")
    decrement.expression = "Loop_body = Loop_body - 1;"
    body = m.add_node(UCM.RespRef(name="body", resp_def=decrement))
    m.add_connection(sp, loop_join)
    m.add_connection(loop_join, body)
    m.add_connection(body, loop_fork)
    m.add_connection(loop_fork, loop_join,
                     condition=UCM.Condition(label="redo",
                                             expression="Loop_body > 0"))
    m.add_connection(loop_fork, ep,
                     condition=UCM.Condition(label="exit",
                                             expression="Loop_body <= 0"))
    _scenario(u, inits=[(counter, str(iterations))], starts=[sp], ends=[ep])
    return u


class TestLoops:

    def test_a_counted_loop_terminates_and_runs_its_body_each_time(self):
        result = st.traverse_all(_counted_loop(3))[0]
        assert result.problems == []
        assert result.responsibilities.count("decrement") == 3

    def test_the_counter_actually_drives_the_iteration_count(self):
        assert st.traverse_all(_counted_loop(5))[0] \
            .responsibilities.count("decrement") == 5


class TestHitCountCeiling:
    """The ceiling is a jUCMNav *preference*, not a property of the model.

    Setting it below what a model legitimately needs turns a correct
    model into a Problems view full of deadlocks. This is not
    hypothetical: it is what produced eight errors on a real model whose
    busiest element is entered nine times, against a preference of ten.
    """

    def test_required_ceiling_reflects_the_loop_depth(self):
        need, scenario, element = st.required_max_hit_count(_counted_loop(5))
        assert need > 5                      # each iteration re-enters the join
        assert scenario == "s"
        assert "LoopJoin" in element or "OrJoin" in element

    def test_a_ceiling_below_that_manufactures_a_failure(self):
        model = _counted_loop(5)
        need, _, _ = st.required_max_hit_count(model)
        problems = st.check_traversal(model, max_hit_count=need - 1)
        assert "infinite_loop" in _kinds(problems)

    def test_and_the_same_model_is_clean_at_the_required_ceiling(self):
        model = _counted_loop(5)
        need, _, _ = st.required_max_hit_count(model)
        assert st.check_traversal(model, max_hit_count=need) == []

    def test_peak_hit_count_is_reported_per_scenario(self):
        result = st.traverse_all(_counted_loop(4))[0]
        assert result.peak_hit_count >= 4
        assert result.peak_hit_element


# ---------------------------------------------------------------------------
# Fork semantics
# ---------------------------------------------------------------------------

def _fork_model(expr_a: str, expr_b: str, patient_default=True):
    u = UCM(name="fork")
    m = u.add_map(name="m")
    sp = m.add_node(UCM.StartPoint(name="start"))
    ep = m.add_node(UCM.EndPoint(name="end"))
    other = m.add_node(UCM.EndPoint(name="other"))
    fork = m.add_node(UCM.OrFork(name="Choice"))
    var = u.get_or_add_variable("x", type="integer")
    m.add_connection(sp, fork)
    m.add_connection(fork, ep, condition=UCM.Condition(expression=expr_a))
    m.add_connection(fork, other, condition=UCM.Condition(expression=expr_b))
    _scenario(u, inits=[(var, "1")], starts=[sp], ends=[ep])
    return u


class TestForks:

    def test_exactly_one_guard_is_the_normal_case(self):
        assert st.check_traversal(_fork_model("x > 0", "x <= 0")) == []

    def test_overlapping_guards_are_reported(self):
        kinds = _kinds(st.check_traversal(_fork_model("x > 0", "x < 5")))
        assert "multiple_branches_enabled" in kinds

    def test_no_true_guard_parks_the_token_by_default(self):
        # jUCMNav's DEFAULT_ISPATIENTONPRECONDITIONS is true: a fork with
        # no true branch waits, and surfaces later as a blocked
        # traversal rather than as an error at the fork itself.
        kinds = _kinds(st.check_traversal(_fork_model("x > 9", "x > 8")))
        assert "traversal_blocked" in kinds
        assert "end_point_not_reached" in kinds

    def test_impatient_mode_reports_the_fork_directly(self):
        u = _fork_model("x > 9", "x > 8")
        problems = []
        for g in u.scenario_groups:
            for sc in g.scenarios:
                problems += st.traverse_scenario(
                    u, sc, patient_on_preconditions=False).problems
        assert "no_branch_enabled" in _kinds(problems)


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------

class TestExpressions:

    def test_bare_identifiers_are_enumeration_literals(self):
        u = UCM(name="enum")
        m = u.add_map(name="m")
        sp = m.add_node(UCM.StartPoint(name="start"))
        ep = m.add_node(UCM.EndPoint(name="end"))
        away = m.add_node(UCM.EndPoint(name="away"))
        fork = m.add_node(UCM.OrFork(name="Choice"))
        et = u.get_or_add_enumeration_type("VariantId", values=["v1", "v2"])
        var = u.get_or_add_variable(
            "variant_id", type="enumeration", enumeration_type=et)
        m.add_connection(sp, fork)
        m.add_connection(fork, ep, condition=UCM.Condition(
            expression="variant_id == v1"))
        m.add_connection(fork, away, condition=UCM.Condition(
            expression="variant_id == v2"))
        _scenario(u, inits=[(var, "v1")], starts=[sp], ends=[ep])
        assert st.check_traversal(u) == []

    def test_an_uninitialised_variable_is_reported_not_guessed(self):
        u = _fork_model("y > 0", "y <= 0")
        u.get_or_add_variable("y", type="integer")   # declared, never set
        assert "expression_error" in _kinds(st.check_traversal(u))


# ---------------------------------------------------------------------------
# Constructs the simulator does not implement
# ---------------------------------------------------------------------------

class TestUnsupportedConstructs:

    def test_a_stub_is_reported_rather_than_silently_skipped(self):
        u = UCM(name="stub")
        m = u.add_map(name="m")
        sp = m.add_node(UCM.StartPoint(name="start"))
        stub = m.add_node(UCM.Stub(name="Stub"))
        ep = m.add_node(UCM.EndPoint(name="end"))
        m.add_connection(sp, stub)
        m.add_connection(stub, ep)
        _scenario(u, starts=[sp], ends=[ep])
        # Skipping it quietly would report a clean run of a model half of
        # which was never executed.
        assert "unsupported_node" in _kinds(st.check_traversal(u))
