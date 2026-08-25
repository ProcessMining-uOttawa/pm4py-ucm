"""End-to-end smoke tests for variant clustering + scenario synthesis.

Each test builds a synthetic process tree, fabricates a tiny event log
covering selected variants, runs the full pipeline (convert → cluster
→ synthesize), and asserts the shape of the resulting UCM and
ClusteringResult."""
from __future__ import annotations

import io
import re

import pm4py_ucm

#: The synthesizer names each per-loop counter contextually
#: (``Loop_AmendDoc``) and falls back to the historical
#: ``loop_counter_<tree_id>`` form only when the loop body has no
#: usable activity label. Tests that probe "is this a loop counter?"
#: match either form.
_LOOP_COUNTER_RE = re.compile(r"(?:Loop_|loop_counter_)\w+")


def _is_loop_counter_name(name: str) -> bool:
    """True iff ``name`` is one of the synthesizer's per-loop counter
    variable names. Anchored to the start so substrings inside other
    names don't match."""
    return bool(_LOOP_COUNTER_RE.match(name))
from pm4py_ucm.algo.discovery.variants import clustering as _clustering
from pm4py_ucm.algo.discovery.scenarios import synthesis as _scenarios
from pm4py_ucm.algo.discovery.scenarios import reports as _reports
from pm4py_ucm.objects.ucm.exporter.variants import jucm as _jucm_exporter


class T:
    def __init__(self, operator=None, label=None, children=None):
        self.operator = operator
        self.label = label
        self.children = children or []


def _seq(*c): return T(operator="->", children=list(c))
def _xor(*c): return T(operator="X", children=list(c))
def _par(*c): return T(operator="+", children=list(c))
def _loop(do, redo): return T(operator="*", children=[do, redo])
def _leaf(l): return T(label=l)
def _tau(): return T()


def _build_tree_and_log():
    """X → (Y ∥ Z) → (A × B) → W. Three observed variants:

    * 60 cases take A
    * 30 cases take B
    * 10 cases interleave Y/Z differently than the modal variant.

    Concurrency-equivalent: X-Y-Z-A-W ≡ X-Z-Y-A-W. They share a variant.
    Sequence-distinct: X-Y-Z-A-W ≢ X-Z-Y-A-W if you cluster by string
    equality; the choice-signature clustering must collapse them.
    """
    tree = _seq(
        _leaf("X"),
        _par(_leaf("Y"), _leaf("Z")),
        _xor(_leaf("A"), _leaf("B")),
        _leaf("W"),
    )
    log = []
    # 60 cases: A branch, Y-then-Z order
    for i in range(60):
        log.append((f"caseA_{i}", ["X", "Y", "Z", "A", "W"]))
    # 30 cases: B branch
    for i in range(30):
        log.append((f"caseB_{i}", ["X", "Y", "Z", "B", "W"]))
    # 10 cases: A branch, Z-then-Y order (concurrency-equivalent to caseA)
    for i in range(10):
        log.append((f"caseA_alt_{i}", ["X", "Z", "Y", "A", "W"]))
    return tree, log


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def test_cluster_collapses_parallel_interleavings_into_one_variant():
    tree, log = _build_tree_and_log()
    result = _clustering.cluster(log, tree)
    # 70 A-branch cases (60 + 10) into one variant; 30 B-branch cases
    # into another. Two variants total — not three (the parallel
    # reordering does not split them).
    assert len(result.variants) == 2
    assert result.variants[0].variant_id == "v1"
    assert result.variants[0].frequency == 70  # the modal A-cluster
    assert result.variants[1].variant_id == "v2"
    assert result.variants[1].frequency == 30
    assert result.total_cases == 100
    assert len(result.noise_case_ids) == 0
    assert result.fitness_percentage == 1.0


def _loop_body_decrement_sites(ucm):
    """``[(responsibility_name, expression)]`` for every responsibility
    carrying a loop-counter decrement."""
    return [(r.name, r.expression) for r in ucm.responsibilities
            if r.expression and "- 1" in r.expression]


def test_loop_decrement_is_not_placed_inside_a_choice():
    """The loop counter must be decremented on EVERY iteration.

    When the loop body starts with a choice, the first responsibility
    reachable from the LoopJoin sits inside one branch only. Attaching
    the decrement there means an iteration taking any other branch
    leaves the counter untouched, the redo condition stays true, and
    the scenario spins forever — jUCMNav reports an infinite loop, a
    blocked AND-join, or a scenario that never reaches its end point.
    """
    # *( X( A, B ), tau ) — a body that is a choice, so neither A nor B
    # is guaranteed to run on a given iteration.
    tree = _seq(_loop(_xor(_leaf("A"), _leaf("B")), _tau()), _leaf("C"))
    log = [("c1", ["A", "C"]), ("c2", ["B", "C"]), ("c3", ["A", "B", "C"])]
    ucm = pm4py_ucm.discover_ucm_inductive(
        log, parameters={"process_tree": tree})
    clustering = _clustering.cluster(log, tree)
    _scenarios.synthesize_scenarios(ucm, tree, clustering)

    sites = _loop_body_decrement_sites(ucm)
    assert sites, "the loop must decrement its counter somewhere"
    names = {n for n, _ in sites}
    assert not (names & {"A", "B"}), (
        f"decrement landed on a conditional activity: {sites}")
    assert any(n.startswith("decrement_") for n in names), (
        f"expected a dedicated always-executed decrement node, got {sites}")


def test_suggest_loop_iterations_sums_choices_and_maxes_sequences():
    """The structural minimum: a choice needs the sum of its branches
    (one iteration goes down one branch), a sequence needs the max (its
    children share an iteration), and a nested loop needs 1 because its
    own counter covers what is inside it."""
    # *( ->( A, X(B, C, D) ), tau ) — the 3-way choice sets the floor.
    three_way = _loop(_seq(_leaf("A"), _xor(_leaf("B"), _leaf("C"),
                                            _leaf("D"))), _tau())
    got = _scenarios.suggest_loop_iterations(three_way)
    assert list(got.values()) == [3], got

    # Two choices in sequence share iterations: max(2, 2) = 2.
    shared = _loop(_seq(_xor(_leaf("B"), _leaf("C")),
                        _xor(_leaf("D"), _leaf("E"))), _tau())
    assert list(_scenarios.suggest_loop_iterations(shared).values()) == [2]

    # Nested choices multiply out: X(X(B,C), X(D,E)) needs 2+2.
    nested = _loop(_xor(_xor(_leaf("B"), _leaf("C")),
                        _xor(_leaf("D"), _leaf("E"))), _tau())
    assert list(_scenarios.suggest_loop_iterations(nested).values()) == [4]

    # A choice on the REDO path runs once fewer than the body, so it
    # needs one iteration more than the same choice in the body.
    redo = _loop(_leaf("A"), _xor(_leaf("P"), _leaf("Q")))
    assert list(_scenarios.suggest_loop_iterations(redo).values()) == [3]


def test_loop_counter_is_raised_so_every_body_branch_can_fire():
    """A 3-way choice in the body cannot be demonstrated in 2
    iterations, whatever the conditions say. The counter is therefore
    raised above the ``max_loop_iterations`` cap when coverage needs it —
    the cap trims a loop that merely ran often, not one that has to run
    to stay honest."""
    tree = _seq(_loop(_xor(_leaf("A"), _leaf("B"), _leaf("C")), _tau()),
                _leaf("Z"))
    log = [("c1", ["A", "B", "C", "Z"]), ("c2", ["A", "B", "C", "Z"]),
           ("c3", ["A", "Z"])]
    ucm = pm4py_ucm.discover_ucm_inductive(
        log, parameters={"process_tree": tree})
    group = _scenarios.synthesize_scenarios(
        ucm, tree, _clustering.cluster(log, tree), max_loop_iterations=2)

    counters = {}
    for sc in group.scenarios:
        for init in sc.initializations:
            if _is_loop_counter_name(init.variable.name):
                counters[sc.name] = int(init.value)
    assert counters, "expected a loop counter initialisation"
    assert max(counters.values()) >= 3, (
        "the 3-way choice needs 3 iterations to be demonstrable; the cap "
        f"of 2 must not win here. got {counters}")


def test_every_branch_a_variant_takes_gets_a_satisfiable_condition():
    """The coverage guarantee, at the level this module controls: if a
    variant takes a branch, that branch's condition is satisfiable for
    that variant — never ``false``, and never a counter range outside
    the iterations the variant actually runs."""
    from pm4py_ucm.objects.ucm.obj import UCM

    tree = _seq(_loop(_xor(_leaf("A"), _leaf("B"), _leaf("C")), _tau()),
                _leaf("Z"))
    log = [("c1", ["A", "B", "C", "Z"]), ("c2", ["B", "C", "Z"]),
           ("c3", ["A", "Z"])]
    clustering = _clustering.cluster(log, tree)
    ucm = pm4py_ucm.discover_ucm_inductive(
        log, parameters={"process_tree": tree})
    _scenarios.synthesize_scenarios(ucm, tree, clustering)

    forks = [n for m in ucm.maps for n in m.nodes
             if isinstance(n, UCM.OrFork) and n.name == "OrFork"]
    assert forks, "expected the body choice to become an OR-fork"
    for fork in forks:
        exprs = [(a.condition.expression if a.condition else "true")
                 for a in fork.succ_connections]
        # Every branch some variant takes must be reachable, so at most
        # the branches nobody takes may read "false".
        assert sum(1 for e in exprs if e.strip() == "false") < len(exprs), (
            f"every branch of {fork.name} is unsatisfiable: {exprs}")
        assert any("variant_id" in e for e in exprs), exprs


def test_activities_of_fitting_cases_are_all_demonstrable():
    """The coverage contract, stated as the guarantee it is.

    Every activity that appears in a case the variants cover must have a
    satisfiable route through the conditions. Activities appearing ONLY
    in non-fitting cases have no variant to demonstrate them, and that
    is the honest boundary of the guarantee — the premise is "if the
    variants cover the cases".
    """
    from pm4py_ucm.objects.ucm.obj import UCM

    # A four-way choice inside a loop, plus one activity that only ever
    # occurs in a trace the tree cannot parse.
    tree = _seq(_leaf("S"),
                _loop(_xor(_leaf("A"), _leaf("B"), _leaf("C"), _leaf("D")),
                      _tau()),
                _leaf("Z"))
    log = [("c1", ["S", "A", "B", "Z"]),
           ("c2", ["S", "C", "D", "Z"]),
           ("c3", ["S", "A", "Z"]),
           ("c4", ["S", "A", "NEVER_FITS", "Z"])]
    clustering = _clustering.cluster(log, tree)
    covered = {a for cid, acts in log if cid not in clustering.noise_case_ids
               for a in acts}
    assert covered >= {"A", "B", "C", "D"}, covered

    ucm = pm4py_ucm.discover_ucm_inductive(
        log, parameters={"process_tree": tree})
    _scenarios.synthesize_scenarios(ucm, tree, clustering)

    # Each of the four branches must be satisfiable for some variant.
    fork = next(n for m in ucm.maps for n in m.nodes
                if isinstance(n, UCM.OrFork) and n.name == "OrFork"
                and len(n.succ_connections) == 4)
    exprs = [(a.condition.expression if a.condition else "true")
             for a in fork.succ_connections]
    unsatisfiable = [e for e in exprs if e.strip() == "false"]
    assert not unsatisfiable, (
        "every branch of the 4-way choice is taken by some variant, so "
        f"none may be unsatisfiable; got {exprs}")

    # And the loop must be sized to make four branches demonstrable.
    assert max(_scenarios.suggest_loop_iterations(tree).values()) >= 4


def test_loop_decrement_runs_after_the_bodys_choices():
    """The decrement must sit at the END of the body, immediately before
    the LoopFork.

    The inside-loop XOR conditions partition the counter as
    ``(lower, upper]`` with ``upper`` starting at the counter's initial
    value M_V, so they assume the counter still reads M_V the first time
    a body XOR is evaluated. Decrementing at the TOP of the body shifts
    every evaluation down by one and makes the topmost range
    unreachable — the branch that should fire on the first iteration
    never fires at all, and its activity appears in no scenario. (Seen
    on a real log: "First symptoms (if known)" was never executed by any
    of the four generated scenarios.)"""
    from pm4py_ucm.objects.ucm.obj import UCM

    tree = _seq(_loop(_xor(_leaf("A"), _leaf("B")), _tau()), _leaf("C"))
    log = [("c1", ["A", "B", "C"]), ("c2", ["A", "B", "C"]),
           ("c3", ["B", "C"])]
    ucm = pm4py_ucm.discover_ucm_inductive(
        log, parameters={"process_tree": tree})
    _scenarios.synthesize_scenarios(ucm, tree, _clustering.cluster(log, tree))

    decrements = [n for m in ucm.maps for n in m.nodes
                  if isinstance(n, UCM.RespRef)
                  and n.resp_def is not None
                  and (n.resp_def.expression or "").count("- 1")]
    assert decrements, "the loop must decrement its counter"
    for ref in decrements:
        targets = [a.target for a in ref.succ_connections]
        assert len(targets) == 1
        assert isinstance(targets[0], UCM.OrFork) and \
            targets[0].name == "LoopFork", (
                "the decrement must be the last thing in the body, so the "
                "body's choices see the counter's entering value; it is "
                f"followed by {type(targets[0]).__name__} "
                f"{targets[0].name!r}")


def test_loop_entry_guard_does_not_raise_an_and_joins_arity():
    """An AND-join fires only when EVERY incoming arc has delivered, so
    the loop-entry guard's bypass arc must not land on one directly: a
    2-branch parallel whose join suddenly needs 3 tokens can never fire.
    The bypass and the loop exit are alternatives and belong behind an
    OrJoin."""
    from pm4py_ucm.objects.ucm.obj import UCM

    # +( P, *( Q, tau ) ) — the loop's exit lands on the AND-join.
    tree = _par(_leaf("P"), _loop(_leaf("Q"), _tau()))
    log = [("c1", ["P", "Q"]), ("c2", ["Q", "P"]), ("c3", ["P", "Q", "Q"])]
    ucm = pm4py_ucm.discover_ucm_inductive(
        log, parameters={"process_tree": tree})
    and_joins = [n for m in ucm.maps for n in m.nodes
                 if isinstance(n, UCM.AndJoin)]
    assert and_joins, "this tree must produce an AND-join to test against"
    before = {id(n): len(n.pred_connections) for n in and_joins}
    assert all(v >= 2 for v in before.values())

    _scenarios.synthesize_scenarios(ucm, tree, _clustering.cluster(log, tree))

    for m in ucm.maps:
        for n in m.nodes:
            if isinstance(n, UCM.AndJoin) and id(n) in before:
                assert len(n.pred_connections) == before[id(n)], (
                    "the guard changed an AND-join's arity from "
                    f"{before[id(n)]} to {len(n.pred_connections)}")


def test_cluster_replays_once_per_distinct_sequence(monkeypatch):
    """A trace's parse depends only on its activity sequence, so cases
    sharing one must not be replayed again — the saving is largest on
    non-fitting traces, which only give up after exhausting the
    backtracking budget."""
    tree, log = _build_tree_and_log()
    log = list(log) + [("bad_1", ["X", "Y", "Z", "A", "NOPE"]),
                       ("bad_2", ["X", "Y", "Z", "A", "NOPE"])]
    calls = []
    real = _clustering._cs.replay

    def counting(tree_, trace, **kwargs):
        calls.append(tuple(trace))
        return real(tree_, trace, **kwargs)

    monkeypatch.setattr(_clustering._cs, "replay", counting)
    result = _clustering.cluster(log, tree)

    assert len(calls) == len(set(calls)), "a sequence was replayed twice"
    # 3 fitting sequences + 1 non-fitting one, over 102 cases.
    assert len(calls) == 4
    assert result.total_cases == 102
    # Memoising changes nothing a caller can observe.
    assert [v.frequency for v in result.variants] == [70, 30]
    assert list(result.noise_case_ids) == ["bad_1", "bad_2"]


def test_cluster_counts_every_case_even_when_sequences_repeat():
    """The per-case aggregates must still see each case individually."""
    tree = _seq(_leaf("X"), _xor(_leaf("A"), _leaf("B")))
    log = [(f"c{i}", ["X", "A"]) for i in range(5)]
    log += [(f"d{i}", ["X", "B"]) for i in range(3)]
    result = _clustering.cluster(log, tree)
    assert [v.frequency for v in result.variants] == [5, 3]
    # Branch totals are summed per case, not per sequence.
    totals = [sum(sum(b.values()) for b in (v.xor_branch_totals or {}).values())
              for v in result.variants]
    assert totals == [5, 3]


def test_cluster_reports_sequence_variant_count():
    tree, log = _build_tree_and_log()
    result = _clustering.cluster(log, tree)
    # Three distinct activity sequences in the log: X-Y-Z-A-W,
    # X-Y-Z-B-W, X-Z-Y-A-W. Two choice-signature variants. Ratio
    # 2/3 confirms compression.
    assert result.sequence_variant_count == 3
    assert result.compression_ratio == 2 / 3


def test_cluster_partial_order_expressions_are_populated():
    tree, log = _build_tree_and_log()
    result = _clustering.cluster(log, tree)
    for v in result.variants:
        assert v.partial_order_expression  # non-empty
        # Sequence elements appear in the expression
        for activity in ["X", "Y", "Z", "W"]:
            assert activity in v.partial_order_expression


def test_cluster_nonconforming_cases_bucketed_as_noise():
    tree, log = _build_tree_and_log()
    # Add a case with an activity not in the tree alphabet.
    log.append(("noise_case", ["X", "Y", "Z", "GARBAGE", "W"]))
    result = _clustering.cluster(log, tree)
    assert "noise_case" in result.noise_case_ids
    assert result.fitness_percentage < 1.0


# ---------------------------------------------------------------------------
# Scenario synthesis — UCM shape
# ---------------------------------------------------------------------------

def test_synthesis_populates_scenario_group_and_variables():
    tree, log = _build_tree_and_log()
    ucm = pm4py_ucm.convert_to_ucm(tree)
    result = _clustering.cluster(log, tree)
    group = _scenarios.synthesize_scenarios(ucm, tree, result)
    assert group in ucm.scenario_groups
    assert len(group.scenarios) == 2
    # One enum + one variable defined.
    assert len(ucm.enumeration_types) == 1
    assert ucm.enumeration_types[0].values == ["v1", "v2"]
    assert len(ucm.variables) == 1
    var = ucm.variables[0]
    assert var.name == "variant_id"
    # jUCMNav's type discriminator is lowercase ("enumeration", not
    # "Enumeration") — capital-E was rejected as an unknown type.
    assert var.type == "enumeration"
    # Every scenario initialises the variant_id.
    for sc in group.scenarios:
        assert len(sc.initializations) == 1
        init = sc.initializations[0]
        assert init.variable is var
        # Scenario name is ``<variant_id>_<short_suffix>`` (e.g.
        # ``v1_Quick``); the initialisation value is just the
        # ``variant_id`` prefix.
        assert sc.name.startswith(init.value + "_") or sc.name == init.value


def test_synthesis_attaches_start_and_end_points():
    tree, log = _build_tree_and_log()
    ucm = pm4py_ucm.convert_to_ucm(tree)
    result = _clustering.cluster(log, tree)
    group = _scenarios.synthesize_scenarios(ucm, tree, result)
    for sc in group.scenarios:
        assert sc.start_points, "scenario must reference at least one StartPoint"
        assert sc.end_points, "scenario must reference at least one EndPoint"


def test_synthesis_emits_orfork_conditions_for_non_loop_xors():
    tree, log = _build_tree_and_log()
    ucm = pm4py_ucm.convert_to_ucm(tree)
    result = _clustering.cluster(log, tree)
    _scenarios.synthesize_scenarios(ucm, tree, result, emit_conditions=True)
    # There is exactly one OR-fork (from the A × B XOR). Its outgoing
    # arcs should both have variant_id conditions.
    or_fork = next(
        n for m in ucm.maps for n in m.nodes
        if type(n).__name__ == "OrFork"
        and n.name not in ("LoopFork", "LoopEntryGuard")
    )
    succs = or_fork.succ_connections
    expressions = [a.condition.expression if a.condition else "true"
                   for a in succs]
    # At least one branch references variant_id (the variant-driven
    # encoding).
    assert any("variant_id" in expr for expr in expressions), (
        f"expected at least one variant_id condition; got {expressions}"
    )


def test_synthesis_emits_orfork_conditions_without_quotes_around_enum_values():
    """jUCMNav treats enum values as bare identifiers in expressions
    (``variant_id == v1``), not as string literals
    (``variant_id == "v1"``). Wrapping in quotes makes the parser
    treat the right side as a string and the equality fails the
    enum type check, which silently cascades to dropped scenario
    references."""
    tree, log = _build_tree_and_log()
    ucm = pm4py_ucm.convert_to_ucm(tree)
    result = _clustering.cluster(log, tree)
    _scenarios.synthesize_scenarios(ucm, tree, result, emit_conditions=True)
    or_fork = next(
        n for m in ucm.maps for n in m.nodes
        if type(n).__name__ == "OrFork"
        and n.name not in ("LoopFork", "LoopEntryGuard")
    )
    for arc in or_fork.succ_connections:
        expr = arc.condition.expression if arc.condition else ""
        if "variant_id" not in expr:
            continue
        assert '"' not in expr, (
            f"variant_id condition must use bare enum identifier, "
            f"not a quoted string: got {expr!r}"
        )


def test_synthesis_scenario_end_points_are_mandatory_by_default():
    """Synthesized scenarios should require the path to reach the
    end point — without ``mandatory=True``, jUCMNav reports the
    scenario as having succeeded even when the traversal halts
    before the end point, which defeats the variant-driven
    traceability story."""
    tree, log = _build_tree_and_log()
    ucm = pm4py_ucm.convert_to_ucm(tree)
    result = _clustering.cluster(log, tree)
    group = _scenarios.synthesize_scenarios(ucm, tree, result)
    for sc in group.scenarios:
        for ep in sc.end_points:
            assert ep.mandatory is True


def test_synthesis_creates_integer_counter_variable_for_loops():
    """Every LOOP tree node gets an integer counter variable so the
    LoopFork's exit/redo arcs can be guarded by mutually-exclusive
    conditions."""
    loop_tree = T(operator="->", children=[
        _leaf("Open"),
        T(operator="*", children=[_leaf("Review"), _leaf("Revise")]),
        _leaf("Close"),
    ])
    log = [
        ("c1", ["Open", "Review", "Close"]),
        ("c2", ["Open", "Review", "Revise", "Review", "Close"]),
    ]
    ucm = pm4py_ucm.convert_to_ucm(loop_tree)
    result = _clustering.cluster(log, loop_tree)
    _scenarios.synthesize_scenarios(
        ucm, loop_tree, result, emit_conditions=True,
    )
    # One integer variable named loop_counter_<tree_id> on the UCM.
    counters = [v for v in ucm.variables if _is_loop_counter_name(v.name)]
    assert len(counters) == 1
    assert counters[0].type == "integer"


def test_synthesis_loopfork_conditions_sit_on_direct_outgoing_arc():
    """jUCMNav only evaluates conditions on arcs **directly** leaving
    a fork. The converter's routing pass parks the original branch
    label one hop downstream past an EmptyPoint bend; the synthesizer
    must pull it back so the loop_counter condition reaches the
    arcs sourced from the LoopFork itself."""
    loop_tree = T(operator="->", children=[
        _leaf("Open"),
        T(operator="*", children=[_leaf("Review"), _leaf("Revise")]),
        _leaf("Close"),
    ])
    log = [
        ("c1", ["Open", "Review", "Close"]),
        ("c2", ["Open", "Review", "Revise", "Review", "Close"]),
    ]
    ucm = pm4py_ucm.convert_to_ucm(loop_tree)
    result = _clustering.cluster(log, loop_tree)
    _scenarios.synthesize_scenarios(
        ucm, loop_tree, result, emit_conditions=True,
    )
    loop_fork = next(
        n for m in ucm.maps for n in m.nodes
        if type(n).__name__ == "OrFork" and n.name == "LoopFork"
    )
    # Every direct outgoing arc of the LoopFork carries a counter
    # condition (no None and no stranded "true" on the bend hop).
    for arc in loop_fork.succ_connections:
        assert arc.condition is not None
        assert _LOOP_COUNTER_RE.search(arc.condition.expression or "")
    # Crucially the *immediate* downstream bend->target arc no longer
    # carries the counter condition (jUCMNav would ignore it there
    # and treat the LoopFork branch as default true).
    for arc in loop_fork.succ_connections:
        target = arc.target
        if not hasattr(target, "succ_connections"):
            continue
        for downstream in target.succ_connections:
            expr = downstream.condition.expression if downstream.condition else ""
            assert not _LOOP_COUNTER_RE.search(expr)


def test_synthesis_orfork_branch_conditions_sit_on_direct_outgoing_arc():
    """Symmetric to the LoopFork case for regular variant_id
    conditions on non-loop XORs."""
    tree = T(operator="->", children=[
        _leaf("X"),
        _xor(_leaf("A"), _leaf("B")),
    ])
    log = [
        ("c1", ["X", "A"]),
        ("c2", ["X", "B"]),
    ]
    ucm = pm4py_ucm.convert_to_ucm(tree)
    result = _clustering.cluster(log, tree)
    _scenarios.synthesize_scenarios(
        ucm, tree, result, emit_conditions=True,
    )
    or_fork = next(
        n for m in ucm.maps for n in m.nodes
        if type(n).__name__ == "OrFork"
        and n.name not in ("LoopFork", "LoopEntryGuard")
    )
    for arc in or_fork.succ_connections:
        assert arc.condition is not None
        assert "variant_id" in (arc.condition.expression or "")
    # No stranded variant_id condition one hop downstream.
    for arc in or_fork.succ_connections:
        target = arc.target
        if not hasattr(target, "succ_connections"):
            continue
        for downstream in target.succ_connections:
            expr = downstream.condition.expression if downstream.condition else ""
            assert "variant_id" not in expr


def test_synthesis_inserts_loop_entry_guard_before_loop_join():
    """The synthesizer must splice a ``LoopEntryGuard`` OR-fork
    between the upstream entry and the LoopJoin so a counter that
    starts at 0 bypasses the body entirely instead of running it
    once and exiting at counter = -1 — which would otherwise make
    M_V=0 and M_V=1 variants indistinguishable in execution. The
    guard's outgoing arcs use ``counter > 0`` (enter loop) and
    ``counter <= 0`` (bypass to post-loop target)."""
    loop_tree = T(operator="->", children=[
        _leaf("Open"),
        T(operator="*", children=[_leaf("Review"), _leaf("Revise")]),
        _leaf("Close"),
    ])
    log = [
        ("c1", ["Open", "Review", "Close"]),
        ("c2", ["Open", "Review", "Revise", "Review", "Close"]),
    ]
    ucm = pm4py_ucm.convert_to_ucm(loop_tree)
    result = _clustering.cluster(log, loop_tree)
    _scenarios.synthesize_scenarios(
        ucm, loop_tree, result, emit_conditions=True,
    )
    guards = [
        n for m in ucm.maps for n in m.nodes
        if type(n).__name__ == "OrFork" and n.name == "LoopEntryGuard"
    ]
    assert len(guards) == 1
    guard = guards[0]
    # Exactly two outgoing arcs, one enter (counter > 0) and one
    # bypass (counter <= 0). Both conditions reference the counter.
    arcs = guard.succ_connections
    assert len(arcs) == 2
    exprs = sorted(a.condition.expression for a in arcs)
    assert any("> 0" in e for e in exprs)
    assert any("<= 0" in e for e in exprs)
    for e in exprs:
        assert _LOOP_COUNTER_RE.search(e)
    # The bypass arc must land on the same post-loop node the
    # LoopFork's exit arc reaches — otherwise scenarios that
    # bypass the loop drop into the wrong continuation.
    bypass_arc = next(
        a for a in arcs if "<= 0" in (a.condition.expression or "")
    )
    bypass_target = bypass_arc.target
    loop_fork = next(
        n for m in ucm.maps for n in m.nodes
        if type(n).__name__ == "OrFork" and n.name == "LoopFork"
    )
    exit_arc = next(
        a for a in loop_fork.succ_connections
        if a.condition and a.condition.label == "exit"
    )
    # The LoopFork's exit arc target is a routing bend; one more
    # hop should hit the same post-loop node the guard bypassed to.
    bend = exit_arc.target
    post_loop = bend.succ_connections[0].target if bend.succ_connections else bend
    assert bypass_target is post_loop


def test_synthesis_loopfork_conditions_are_mutually_exclusive_and_exhaustive():
    """The LoopFork's exit arc should be ``counter <= 0`` and the
    redo arc ``counter > 0``. The pair is mutually exclusive **and**
    jointly exhaustive over every integer value — including
    negative ones, which crop up when a variant's counter
    initialises to 0 (loop not traversed in observed data) and the
    body's decrement responsibility still runs once due to the
    converter's loop structure. The earlier ``== 0`` version of
    the exit condition left jUCMNav stuck at the LoopFork with
    counter = -1 (both branches false)."""
    loop_tree = T(operator="->", children=[
        _leaf("Open"),
        T(operator="*", children=[_leaf("Review"), _leaf("Revise")]),
        _leaf("Close"),
    ])
    log = [
        ("c1", ["Open", "Review", "Close"]),
        ("c2", ["Open", "Review", "Revise", "Review", "Close"]),
    ]
    ucm = pm4py_ucm.convert_to_ucm(loop_tree)
    result = _clustering.cluster(log, loop_tree)
    _scenarios.synthesize_scenarios(
        ucm, loop_tree, result, emit_conditions=True,
    )
    text = _jucm_exporter.serialize_to_string(ucm)
    # Both halves expressed in terms of the counter; not the default
    # ``true`` and not the brittle ``== 0`` that fails on negatives.
    # ``<`` is XML-escaped to ``&lt;`` in attribute values.
    # Counter expressions use contextual names like ``Loop_Review`` —
    # accept either the new form or the historical
    # ``loop_counter_<id>`` fallback inside the XMI attribute.
    assert ('expression="Loop_' in text
            or 'expression="loop_counter_' in text)
    assert '&lt;= 0' in text
    assert '> 0' in text
    assert '== 0' not in text  # explicit guard against regression


def test_synthesis_inserts_loop_entry_guard_so_counter_zero_means_zero_iterations():
    """A ``LoopEntryGuard`` OR-fork sits between upstream and the
    LoopJoin so that ``counter == 0`` at scenario start bypasses
    the loop body entirely (0 iterations) instead of running it
    once and exiting at ``counter = -1``.

    The guard's two outgoing arcs must carry mutually-exclusive
    counter conditions, one entering the LoopJoin (``> 0``) and
    one bypassing to the loop's post-exit node (``<= 0``)."""
    loop_tree = T(operator="->", children=[
        _leaf("Open"),
        T(operator="*", children=[_leaf("Review"), _leaf("Revise")]),
        _leaf("Close"),
    ])
    log = [
        ("c1", ["Open", "Review", "Close"]),
        ("c2", ["Open", "Review", "Revise", "Review", "Close"]),
    ]
    ucm = pm4py_ucm.convert_to_ucm(loop_tree)
    result = _clustering.cluster(log, loop_tree)
    _scenarios.synthesize_scenarios(
        ucm, loop_tree, result, emit_conditions=True,
    )
    # The synthesizer added at least one LoopEntryGuard.
    guards = [
        n for m in ucm.maps for n in m.nodes
        if type(n).__name__ == "OrFork" and n.name == "LoopEntryGuard"
    ]
    assert len(guards) == 1
    guard = guards[0]
    # Two outgoing arcs, both with counter-based conditions.
    exprs = [a.condition.expression for a in guard.succ_connections
             if a.condition is not None]
    assert any("> 0" in e for e in exprs)
    assert any("<= 0" in e for e in exprs)
    # The bypass skips the loop: it must not re-enter the LoopJoin, which
    # is the whole point of the guard.
    #
    # This used to assert "the target is not an OrJoin", as a proxy for
    # "not the LoopJoin". The proxy stopped holding once the bypass began
    # merging through an OrJoin of its own before landing on a node that
    # admits one incoming segment — so assert the intent directly, by
    # identity, and check where the bypass actually leads.
    bypass_arc = next(
        a for a in guard.succ_connections
        if a.condition and "<= 0" in a.condition.expression
    )
    loop_join = next(
        a.target for a in guard.succ_connections
        if a.condition and "> 0" in a.condition.expression
    )
    assert bypass_arc.target is not loop_join

    # Follow the bypass to the first real node: it is the loop's exit, not
    # anything inside the body.
    node, hops = bypass_arc.target, 0
    while type(node).__name__ in ("OrJoin", "EmptyPoint") and hops < 10:
        node = node.succ_connections[0].target
        hops += 1
    assert node is not loop_join
    assert type(node).__name__ != "StartPoint"


def test_synthesis_loop_entry_guard_excluded_from_orfork_condition_emission():
    """The synthetic ``LoopEntryGuard`` OR-forks must NOT be paired
    with tree XOR nodes for variant_id condition emission. The
    counter-based conditions they carry are the correct ones; a
    variant_id overwrite would break the bypass semantics."""
    # Tree with both a loop AND an outside-loop XOR: LoopEntryGuard
    # and the XOR's OrFork are both present, but only the latter
    # should get variant_id conditions.
    loop_tree = T(operator="->", children=[
        _leaf("X"),
        _xor(_leaf("A"), _leaf("B")),
        T(operator="*", children=[_leaf("L"), _leaf("R")]),
    ])
    log = [
        ("c1", ["X", "A", "L"]),
        ("c2", ["X", "B", "L", "R", "L"]),
    ]
    ucm = pm4py_ucm.convert_to_ucm(loop_tree)
    result = _clustering.cluster(log, loop_tree)
    _scenarios.synthesize_scenarios(
        ucm, loop_tree, result, emit_conditions=True,
    )
    guard = next(
        n for m in ucm.maps for n in m.nodes
        if type(n).__name__ == "OrFork" and n.name == "LoopEntryGuard"
    )
    # Guard's conditions stay loop-counter-based, not variant_id-based.
    for arc in guard.succ_connections:
        expr = arc.condition.expression if arc.condition else ""
        assert "variant_id" not in expr
        assert _LOOP_COUNTER_RE.search(expr)


def test_synthesis_loop_body_carries_decrement():
    """The loop body carries the counter-decrement expression, so the
    counter steps down once per iteration.

    It rides on a dedicated node at the end of the body rather than on a
    body activity (``Review`` here): the position has to be one nothing
    can bypass AND after the body's choices — see
    ``test_loop_decrement_runs_after_the_bodys_choices`` and
    ``test_loop_decrement_is_not_placed_inside_a_choice``."""
    loop_tree = T(operator="->", children=[
        _leaf("Open"),
        T(operator="*", children=[_leaf("Review"), _leaf("Revise")]),
        _leaf("Close"),
    ])
    log = [
        ("c1", ["Open", "Review", "Close"]),
        ("c2", ["Open", "Review", "Revise", "Review", "Close"]),
    ]
    ucm = pm4py_ucm.convert_to_ucm(loop_tree)
    result = _clustering.cluster(log, loop_tree)
    _scenarios.synthesize_scenarios(
        ucm, loop_tree, result, emit_conditions=True,
    )
    decrements = [r for r in ucm.responsibilities
                  if "- 1" in (r.expression or "")]
    assert len(decrements) == 1, (
        f"expected exactly one decrement, got {[r.name for r in decrements]}")
    body_resp = decrements[0]
    assert _LOOP_COUNTER_RE.search(body_resp.expression or "")
    assert "= " in body_resp.expression
    # The activity itself is left clean — the decrement is not smuggled
    # onto a step the modeller wrote.
    review = next(r for r in ucm.responsibilities if r.name == "Review")
    assert not (review.expression or "")


def test_synthesis_per_variant_loop_counter_initialised_to_max_iterations():
    """Each variant's scenario initialises the loop counter to the
    maximum body-iteration count actually observed — capped at the
    ``max_loop_iterations`` ceiling so scenarios stay tractable. With
    the cap disabled the value matches the heaviest trace exactly."""
    loop_tree = T(operator="->", children=[
        _leaf("Open"),
        T(operator="*", children=[_leaf("Review"), _leaf("Revise")]),
        _leaf("Close"),
    ])
    log = (
        [(f"c1_{i}", ["Open", "Review", "Close"]) for i in range(10)]
        + [(f"c2_{i}", ["Open", "Review", "Revise", "Review", "Revise",
                        "Review", "Close"]) for i in range(5)]
    )
    ucm = pm4py_ucm.convert_to_ucm(loop_tree)
    result = _clustering.cluster(log, loop_tree)
    # Uncapped: v2's three iterations come through verbatim.
    group_uncapped = _scenarios.synthesize_scenarios(
        ucm, loop_tree, result, emit_conditions=True,
        max_loop_iterations=None,
    )
    # Scenario names now carry a contextual suffix (``v1_Review``,
    # ``v2_TwoReview`` etc.). Look them up by the ``v<N>`` prefix.
    def _find(scenarios, vid):
        return next(sc for sc in scenarios if sc.name.startswith(vid + "_")
                    or sc.name == vid)
    for vname, expected in (("v1", "1"), ("v2", "3")):
        sc = _find(group_uncapped.scenarios, vname)
        counter_inits = [
            i for i in sc.initializations
            if _is_loop_counter_name(i.variable.name)
        ]
        assert len(counter_inits) == 1
        assert counter_inits[0].value == expected
    # Capped at 2 (default): v2's three iterations clamp to 2.
    ucm2 = pm4py_ucm.convert_to_ucm(loop_tree)
    group_capped = _scenarios.synthesize_scenarios(
        ucm2, loop_tree, result, emit_conditions=True,
        max_loop_iterations=2,
    )
    sc_v2 = _find(group_capped.scenarios, "v2")
    assert next(
        i for i in sc_v2.initializations
        if _is_loop_counter_name(i.variable.name)
    ).value == "2"


def test_synthesis_synthesises_decrement_resp_when_body_is_tau():
    """A loop whose body is a single ``tau`` leaf has no real
    responsibility to decorate. The synthesizer must create a
    ``decrement_<counter>`` :class:`UCM.RespRef` so the counter still
    steps down once per iteration."""
    # *(tau, X) — body is tau, redo is X. Body alphabet empty, but
    # the converter still produces a LoopJoin + LoopFork pair.
    tree = T(operator="*", children=[_tau(), _leaf("X")])
    log = [("c1", []), ("c2", ["X"]), ("c3", ["X", "X"])]
    ucm = pm4py_ucm.convert_to_ucm(tree)
    result = _clustering.cluster(log, tree)
    _scenarios.synthesize_scenarios(
        ucm, tree, result, emit_conditions=True,
    )
    decrement_resps = [
        r for r in ucm.responsibilities if r.name.startswith("decrement_")
    ]
    assert decrement_resps, (
        "expected a synthetic decrement_<counter> Responsibility "
        "when the loop body has no real RespRef"
    )
    assert any(_LOOP_COUNTER_RE.search(r.expression or "") for r in decrement_resps)


def test_synthesis_outside_loop_xor_conditions_emitted_when_other_xors_are_inside_loops():
    """Regression for the ClaimsPaymentLog bug: when a tree contains
    XORs both inside *and* outside loops, the synthesizer previously
    counted only the outside-loop XORs, didn't match the UCM's full
    OrFork count, and silently bailed — leaving every OR-fork (even
    the outside-loop ones that should get variant_id conditions) at
    the default ``true``. The fix pairs every multi-child XOR with
    its OrFork and skips only the inside-loop subset for emission."""
    # Tree: X -> XOR(A, B) -> *( LoopBody, XOR(P, Q) ) -> XOR(M, N) -> end
    # Three multi-child XORs total: two outside, one inside the loop.
    tree = T(operator="->", children=[
        _leaf("X"),
        _xor(_leaf("A"), _leaf("B")),
        T(operator="*", children=[
            _leaf("LoopBody"),
            _xor(_leaf("P"), _leaf("Q")),
        ]),
        _xor(_leaf("M"), _leaf("N")),
        _leaf("end"),
    ])
    log = [
        ("c1", ["X", "A", "LoopBody", "M", "end"]),
        ("c2", ["X", "B", "LoopBody", "N", "end"]),
        ("c3", ["X", "A", "LoopBody", "P", "LoopBody", "M", "end"]),
    ]
    ucm = pm4py_ucm.convert_to_ucm(tree)
    result = _clustering.cluster(log, tree)
    _scenarios.synthesize_scenarios(
        ucm, tree, result, emit_conditions=True,
    )
    # Outside-loop OR-forks must have variant_id conditions; inside-
    # loop OR-fork stays at the default ``true``.
    outside_loop_or_forks_with_variant_id = 0
    for m in ucm.maps:
        for n in m.nodes:
            if type(n).__name__ != "OrFork" or n.name == "LoopFork":
                continue
            has_variant_id = any(
                arc.condition is not None
                and "variant_id" in (arc.condition.expression or "")
                for arc in n.succ_connections
            )
            if has_variant_id:
                outside_loop_or_forks_with_variant_id += 1
    # Every non-LoopFork OR-fork must carry variant_id conditions:
    # the two outside-loop ones via direct variant_id disjunctions,
    # and the inside-loop one via combined variant_id + loop_counter
    # range conditions.
    assert outside_loop_or_forks_with_variant_id == 3, (
        f"expected 3 OR-forks with variant_id conditions; "
        f"got {outside_loop_or_forks_with_variant_id}"
    )


def test_synthesis_orfork_branches_with_no_variant_get_false():
    """When no variant takes a particular OR-fork branch, the
    synthesizer must emit ``expression="false"`` rather than leaving
    the default ``true``. Together with the variant_id disjunctions
    on the taken branches this makes the OR-fork's outgoing
    conditions mutually exclusive **and** jointly exhaustive."""
    # Three-branch XOR where only branches 0 and 2 are exercised.
    tree = T(operator="->", children=[
        _leaf("X"),
        _xor(_leaf("A"), _leaf("B"), _leaf("C")),
    ])
    log = [
        ("c1", ["X", "A"]),
        ("c2", ["X", "C"]),
    ]
    ucm = pm4py_ucm.convert_to_ucm(tree)
    result = _clustering.cluster(log, tree)
    _scenarios.synthesize_scenarios(
        ucm, tree, result, emit_conditions=True,
    )
    or_fork = next(
        n for m in ucm.maps for n in m.nodes
        if type(n).__name__ == "OrFork"
        and n.name not in ("LoopFork", "LoopEntryGuard")
    )
    exprs = [a.condition.expression for a in or_fork.succ_connections
             if a.condition is not None]
    # At least one branch is false (the B branch nobody took).
    assert "false" in exprs


def test_synthesis_inside_loop_xor_distributes_branches_via_loop_counter():
    """Inside-loop XOR choices vary per iteration. The synthesizer
    combines ``variant_id`` with the enclosing loop counter so that
    each branch fires for the proportion of iterations the variant
    actually took it in: a variant that took branch 0 twice and
    branch 1 once across its traces gets branch 0 driven by
    ``counter > threshold`` for the first 2/3 of iterations and
    branch 1 by ``counter <= threshold`` for the last 1/3."""
    # Loop with XOR inside body.
    inner_xor = _xor(_leaf("A"), _leaf("B"))
    tree = T(operator="*", children=[inner_xor, _leaf("R")])
    log = [
        ("c1", ["A"]),
        ("c2", ["A", "R", "B"]),
        ("c3", ["B"]),
    ]
    ucm = pm4py_ucm.convert_to_ucm(tree)
    result = _clustering.cluster(log, tree)
    _scenarios.synthesize_scenarios(
        ucm, tree, result, emit_conditions=True,
    )
    inside_loop_or_forks = [
        n for m in ucm.maps for n in m.nodes
        if type(n).__name__ == "OrFork"
        and n.name not in ("LoopFork", "LoopEntryGuard")
    ]
    assert inside_loop_or_forks, "fixture must have an inside-loop OR-fork"
    for of in inside_loop_or_forks:
        exprs = [a.condition.expression if a.condition else ""
                 for a in of.succ_connections]
        # Both branches must mention variant_id AND a loop_counter
        # comparison — that's the whole point of the combined
        # condition. The fallback true/false split would fail this.
        has_variant_id_branch = any("variant_id" in e for e in exprs)
        has_counter_branch = any(_LOOP_COUNTER_RE.search(e) for e in exprs)
        assert has_variant_id_branch
        assert has_counter_branch


def test_synthesis_inside_loop_xor_uses_loop_counter_in_conditions():
    # Loop with XOR inside body. Inside-loop XOR conditions now
    # combine variant_id with the enclosing loop's counter so
    # branches distribute across iterations.
    inner_xor = _xor(_leaf("A"), _leaf("B"))
    tree = T(operator="*", children=[inner_xor, _leaf("R")])
    log = [
        ("c1", ["A"]),
        ("c2", ["A", "R", "B"]),
        ("c3", ["B"]),
    ]
    ucm = pm4py_ucm.convert_to_ucm(tree)
    result = _clustering.cluster(log, tree)
    _scenarios.synthesize_scenarios(ucm, tree, result, emit_conditions=True)
    inside_loop_orforks = [
        n for m in ucm.maps for n in m.nodes
        if type(n).__name__ == "OrFork"
        and n.name not in ("LoopFork", "LoopEntryGuard")
    ]
    # The fixture has exactly one inside-loop XOR.
    assert len(inside_loop_orforks) == 1
    for of in inside_loop_orforks:
        for arc in of.succ_connections:
            expr = arc.condition.expression if arc.condition else ""
            # Every branch's condition references the loop counter
            # (or is ``false`` for a branch nobody took).
            assert _LOOP_COUNTER_RE.search(expr) or expr == "false", (
                f"inside-loop XOR condition must reference the loop "
                f"counter or be ``false``; got {expr!r}"
            )


# ---------------------------------------------------------------------------
# CSV reports
# ---------------------------------------------------------------------------

def test_variants_report_csv_contains_one_row_per_variant_plus_totals():
    tree, log = _build_tree_and_log()
    result = _clustering.cluster(log, tree)
    buf = io.StringIO()
    _reports.write_variants_report(result, buf)
    text = buf.getvalue()
    assert "variant_id" in text  # header
    assert "v1" in text
    assert "v2" in text
    assert "totals" in text
    # Frequency 70 should appear (modal variant).
    assert "70" in text


def test_case_variant_map_csv_lists_every_case():
    tree, log = _build_tree_and_log()
    result = _clustering.cluster(log, tree)
    buf = io.StringIO()
    _reports.write_case_variant_map(result, buf)
    text = buf.getvalue()
    # 100 cases + 1 header = 101 lines.
    assert text.count("\n") >= 100
    # A sample case ID surfaces.
    assert "caseA_0" in text


# ---------------------------------------------------------------------------
# Exporter round-trip
# ---------------------------------------------------------------------------

def test_jucm_export_with_scenarios_is_well_formed_xml():
    tree, log = _build_tree_and_log()
    ucm = pm4py_ucm.convert_to_ucm(tree)
    result = _clustering.cluster(log, tree)
    _scenarios.synthesize_scenarios(ucm, tree, result)
    text = _jucm_exporter.serialize_to_string(ucm)
    # Smoke checks — full XML validity would need a schema.
    assert text.startswith("<?xml")
    assert "<urn:URNspec" in text
    assert "<scenarioGroups" in text
    assert "<scenarios" in text
    assert "<initializations" in text
    assert "<variables" in text
    assert "<enumerationTypes" in text
    assert "variant_id" in text
    # ParseXML must accept it.
    import xml.etree.ElementTree as ET
    ET.fromstring(text)


def test_jucm_export_back_references_scenario_start_and_end_points():
    """jUCMNav refuses to run scenarios when the path-level StartPoint /
    EndPoint nodes don't carry back-pointing ``scenarioStartPoints`` /
    ``scenarioEndPoints`` attributes listing the XPath of every
    ScenarioStartPoint / ScenarioEndPoint that references them. This
    test guards the back-reference attribute presence and the XPath
    format."""
    tree, log = _build_tree_and_log()
    ucm = pm4py_ucm.convert_to_ucm(tree)
    result = _clustering.cluster(log, tree)
    _scenarios.synthesize_scenarios(ucm, tree, result)
    text = _jucm_exporter.serialize_to_string(ucm)
    assert "scenarioStartPoints=\"//@ucmspec/@scenarioGroups.0" in text
    assert "scenarioEndPoints=\"//@ucmspec/@scenarioGroups.0" in text
    # Two scenarios in the test fixture, each referencing the single
    # StartPoint and EndPoint, so the back-reference list has two
    # space-separated XPaths.
    import re
    sp_match = re.search(r'scenarioStartPoints="([^"]+)"', text)
    ep_match = re.search(r'scenarioEndPoints="([^"]+)"', text)
    assert sp_match and len(sp_match.group(1).split()) == 2
    assert ep_match and len(ep_match.group(1).split()) == 2


def test_jucm_export_enumeration_type_lists_variable_instances():
    """jUCMNav binds enum-typed variables to their EnumerationType via
    a back-reference ``instances`` attribute on the type. Without it
    the variable shows up as untyped in the scenario panel."""
    tree, log = _build_tree_and_log()
    ucm = pm4py_ucm.convert_to_ucm(tree)
    result = _clustering.cluster(log, tree)
    _scenarios.synthesize_scenarios(ucm, tree, result)
    text = _jucm_exporter.serialize_to_string(ucm)
    # The single variable's ID should appear inside the
    # ``instances`` attribute of the single enumerationTypes element.
    import re
    et_match = re.search(r'<enumerationTypes [^>]*instances="([^"]+)"', text)
    assert et_match, "enumerationTypes must carry an instances back-ref"
    var_ids = et_match.group(1).split()
    assert len(var_ids) == 1
    # That ID must match the variant_id variable's id.
    var = ucm.variables[0]
    assert var_ids[0] == str(var.id)


def test_jucm_export_variable_type_is_lowercase_enumeration():
    """jUCMNav writes the type discriminator in lowercase
    (``type=\"enumeration\"``); capital-E is silently rejected."""
    tree, log = _build_tree_and_log()
    ucm = pm4py_ucm.convert_to_ucm(tree)
    result = _clustering.cluster(log, tree)
    _scenarios.synthesize_scenarios(ucm, tree, result)
    text = _jucm_exporter.serialize_to_string(ucm)
    assert 'type="enumeration"' in text
    assert 'type="Enumeration"' not in text


def test_jucm_export_endpoints_carry_mandatory_flag():
    """The exporter must emit ``mandatory="true"`` on synthesized
    ScenarioEndPoint elements — jUCMNav otherwise treats the end
    point as optional and the scenario as already-satisfied even
    when the traversal stops short."""
    tree, log = _build_tree_and_log()
    ucm = pm4py_ucm.convert_to_ucm(tree)
    result = _clustering.cluster(log, tree)
    _scenarios.synthesize_scenarios(ucm, tree, result)
    text = _jucm_exporter.serialize_to_string(ucm)
    assert '<endPoints enabled="true" mandatory="true"' in text


def test_jucm_export_does_not_escape_greater_than_in_attributes():
    """XML 1.0 doesn't require escaping ``>`` in attribute values and
    jUCMNav itself never emits ``&gt;`` — so the partial-order
    expression ``X -> (Y || Z)`` should appear raw in the scenario
    description, not as ``X -&gt; (Y || Z)``."""
    tree, log = _build_tree_and_log()
    ucm = pm4py_ucm.convert_to_ucm(tree)
    result = _clustering.cluster(log, tree)
    _scenarios.synthesize_scenarios(ucm, tree, result)
    text = _jucm_exporter.serialize_to_string(ucm)
    assert "&gt;" not in text
    assert "X -> (Y || Z) -> [A] -> W" in text


def test_jucm_export_orfork_conditions_use_bare_enum_identifiers():
    """End-to-end guard for the condition syntax — the serialized
    .jucm must contain ``variant_id == v1`` (no escaped quotes),
    not ``variant_id == &quot;v1&quot;``."""
    tree, log = _build_tree_and_log()
    ucm = pm4py_ucm.convert_to_ucm(tree)
    result = _clustering.cluster(log, tree)
    _scenarios.synthesize_scenarios(ucm, tree, result)
    text = _jucm_exporter.serialize_to_string(ucm)
    assert 'variant_id == v1' in text
    assert '&quot;v1&quot;' not in text


def test_synthesis_data_driven_strategy_creates_attribute_variables_not_variant_id():
    """When ``condition_strategy='data-driven'`` the synthesizer
    should NOT create a ``variant_id`` variable; instead it creates
    one URN Variable per case-constant attribute the log carries.
    Scenarios initialise those attributes to per-variant
    representative values (mode for enums, median for numerics)."""
    import pytest
    pytest.importorskip("sklearn")
    import pandas as pd

    # Tiny tree X -> XOR(A, B) -> C. Per-case attribute "category"
    # perfectly predicts the XOR branch.
    tree = T(operator="->", children=[
        _leaf("X"),
        _xor(_leaf("A"), _leaf("B")),
        _leaf("C"),
    ])
    rows = []
    cases = (
        [(f"c1_{i}", "low",  ["X", "A", "C"]) for i in range(20)]
        + [(f"c2_{i}", "high", ["X", "B", "C"]) for i in range(20)]
    )
    for case_id, category, trace in cases:
        for j, act in enumerate(trace):
            rows.append({
                "case:concept:name": case_id,
                "concept:name": act,
                "time:timestamp": pd.Timestamp("2026-01-01") + pd.Timedelta(j, "s"),
                "Category": category,
            })
    log_df = pd.DataFrame(rows)
    result = _clustering.cluster(log_df, tree)
    ucm = pm4py_ucm.convert_to_ucm(tree)
    group = _scenarios.synthesize_scenarios(
        ucm, tree, result,
        condition_strategy="data-driven",
        log=log_df,
        emit_conditions=True,
    )
    # variant_id is not created.
    var_names = {v.name for v in ucm.variables}
    assert "variant_id" not in var_names
    # The case-constant attribute is created as an enumeration.
    assert "Category" in var_names
    cat_var = next(v for v in ucm.variables if v.name == "Category")
    assert cat_var.type == "enumeration"
    # The XOR's branches reference Category in their conditions.
    or_fork = next(
        n for m in ucm.maps for n in m.nodes
        if type(n).__name__ == "OrFork"
        and n.name not in ("LoopFork", "LoopEntryGuard")
    )
    exprs = [a.condition.expression for a in or_fork.succ_connections]
    assert any("Category" in e for e in exprs), (
        f"expected at least one Category reference; got {exprs}"
    )


def test_synthesis_data_driven_abandons_with_warning_when_no_useful_attributes():
    """When the log has no case-constant attributes survive the
    type / cardinality filters, the data-driven path emits a warning
    and falls through with conditions left at default ``true``.
    There is no fallback to variant-driven."""
    import pandas as pd
    import warnings

    # Log with only IDs, activities, timestamps — nothing case-constant.
    tree = T(operator="->", children=[
        _leaf("X"),
        _xor(_leaf("A"), _leaf("B")),
    ])
    log_df = pd.DataFrame({
        "case:concept:name": ["c0", "c0", "c1", "c1"],
        "concept:name": ["X", "A", "X", "B"],
        "time:timestamp": pd.to_datetime(
            ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"],
        ),
    })
    result = _clustering.cluster(log_df, tree)
    ucm = pm4py_ucm.convert_to_ucm(tree)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        group = _scenarios.synthesize_scenarios(
            ucm, tree, result,
            condition_strategy="data-driven", log=log_df,
        )
    # Warning fired.
    assert any("abandoned" in str(wi.message).lower() for wi in w)
    # No attribute variables created.
    var_names = {v.name for v in ucm.variables}
    assert "variant_id" not in var_names


# ---------------------------------------------------------------------------
# Decomposition × scenarios
# ---------------------------------------------------------------------------

def test_synthesis_runs_with_decomposition_and_root_map_conditions_emit():
    """Scenarios synthesize cleanly on a decomposed (multi-map) UCM
    with static stubs + a single plug-in. The synthesizer was designed
    against a flat single-map UCM; this regression locks in the
    "no crash + serializable" contract for the decomposed case.

    Known limitation, intentionally not fixed here: OR-fork
    ``variant_id == V`` conditions are only emitted for forks that
    live in the root map. An XOR pushed into a plug-in keeps its
    converter-default arc condition. We assert *root-map* forks
    receive conditions; plug-in forks may or may not (callers who
    need every fork conditioned should use ``decomposition=None``
    or stay on the flat default).
    """
    # Top-level sequence with one small head and one meaty tail.
    # The meaty tail carries an XOR that becomes a plug-in OrFork;
    # the head's XOR (added below) stays in the root map.
    tree = _seq(
        _leaf("Start"),
        _xor(_leaf("RootA"), _leaf("RootB")),
        _seq(_leaf("S1"), _leaf("S2"), _leaf("S3"), _leaf("S4"), _leaf("S5")),
    )
    log = []
    for i in range(30):
        log.append((f"a{i}", ["Start", "RootA", "S1", "S2", "S3", "S4", "S5"]))
    for i in range(20):
        log.append((f"b{i}", ["Start", "RootB", "S1", "S2", "S3", "S4", "S5"]))

    decomp = {
        "on_root_sequence": True, "on_parallel": False,
        "on_alternative": False, "on_loop": False,
        "max_leaves_per_map": 1000, "min_leaves_to_decompose": 3,
        "balance_ratio": 0.0,
    }
    ucm = pm4py_ucm.convert_to_ucm(tree, decomposition=decomp)
    # Root + 1 plug-in: the "single plug-in" configuration this test
    # is named for.
    assert len(ucm.maps) == 2

    result = _clustering.cluster(log, tree)
    group = _scenarios.synthesize_scenarios(ucm, tree, result)

    # Two variants, two scenarios.
    assert len(group.scenarios) == 2

    # Round-trip the UCM through the exporter — guards against any
    # crash in serializing scenarios attached to a UCM whose XORs
    # live across multiple maps.
    text = _jucm_exporter.serialize_to_string(ucm)
    assert "<scenarioGroups" in text

    # Root map's XOR is conditioned. We don't assert which branch
    # carries which value — only that variant_id == ... lands in
    # the file somewhere.
    assert "variant_id == v1" in text or "variant_id == v2" in text


def test_synthesis_with_decomposition_conditions_every_plugin_orfork():
    """When decomposition pushes an XOR into a plug-in map, the
    synthesizer must still emit ``variant_id == V`` on that plug-in
    OR-fork — not only the root-map ones. Locks in the cross-map
    OR-fork walk; the original target_map-only code silently dropped
    conditions on plug-in forks because ``len(or_forks) != len(xor_seq)``
    bailed before any emission.
    """
    # Force the XOR into a plug-in by giving each XOR branch its own
    # 4-leaf body and asking the decomposer to extract every
    # alternative into its own plug-in.
    tree = _seq(
        _leaf("Start"),
        _xor(
            _seq(_leaf("A1"), _leaf("A2"), _leaf("A3"), _leaf("A4")),
            _seq(_leaf("B1"), _leaf("B2"), _leaf("B3"), _leaf("B4")),
        ),
    )
    log = []
    for i in range(20):
        log.append((f"a{i}", ["Start", "A1", "A2", "A3", "A4"]))
    for i in range(15):
        log.append((f"b{i}", ["Start", "B1", "B2", "B3", "B4"]))

    decomp = {
        "on_root_sequence": False, "on_parallel": False,
        "on_alternative": True, "on_loop": False,
        "max_leaves_per_map": 1000, "min_leaves_to_decompose": 3,
        "balance_ratio": 0.0,
    }
    ucm = pm4py_ucm.convert_to_ucm(tree, decomposition=decomp)
    # Root + 2 plug-ins.
    assert len(ucm.maps) == 3

    result = _clustering.cluster(log, tree)
    _scenarios.synthesize_scenarios(ucm, tree, result)
    text = _jucm_exporter.serialize_to_string(ucm)
    # Both variant_id values land in the model — one per branch.
    assert "variant_id == v1" in text
    assert "variant_id == v2" in text


def test_synthesis_data_driven_with_decomposition_emits_condition_mining_rows():
    """Companion regression for the data-driven path: when
    decomposition is active the mining results list must still cover
    every outside-loop OR-fork, not just the root-map ones.
    """
    pytest = __import__("pytest")
    pytest.importorskip("sklearn")
    import pandas as pd

    # Same shape as the variant-driven sibling test: XOR with two
    # 4-leaf branches, each pushed into its own plug-in. A case-
    # constant attribute "Category" perfectly predicts the branch
    # so the tree mines a clean expression on the (now plug-in)
    # OR-fork.
    tree = _seq(
        _leaf("Start"),
        _xor(
            _seq(_leaf("A1"), _leaf("A2"), _leaf("A3"), _leaf("A4")),
            _seq(_leaf("B1"), _leaf("B2"), _leaf("B3"), _leaf("B4")),
        ),
    )
    rows = []
    cases = (
        [(f"a{i}", "low",  ["Start", "A1", "A2", "A3", "A4"]) for i in range(20)]
        + [(f"b{i}", "high", ["Start", "B1", "B2", "B3", "B4"]) for i in range(15)]
    )
    for case_id, category, trace in cases:
        for j, act in enumerate(trace):
            rows.append({
                "case:concept:name": case_id,
                "concept:name": act,
                "time:timestamp": pd.Timestamp("2026-01-01") + pd.Timedelta(j, "s"),
                "Category": category,
            })
    log_df = pd.DataFrame(rows)

    decomp = {
        "on_root_sequence": False, "on_parallel": False,
        "on_alternative": True, "on_loop": False,
        "max_leaves_per_map": 1000, "min_leaves_to_decompose": 3,
        "balance_ratio": 0.0,
    }
    ucm = pm4py_ucm.convert_to_ucm(tree, decomposition=decomp)
    assert len(ucm.maps) == 3
    result = _clustering.cluster(log_df, tree)
    group = _scenarios.synthesize_scenarios(
        ucm, tree, result,
        condition_strategy="data-driven", log=log_df,
    )

    # Mining results were attached — empty list was the user-reported
    # symptom on a decomposed UCM.
    mining = getattr(group, "_mining_results", None) or []
    assert mining, "condition_mining results must be non-empty on a decomposed UCM"

    # Mined expression lands on the (plug-in!) OR-fork arc, not stuck
    # on the converter default.
    text = _jucm_exporter.serialize_to_string(ucm)
    assert "Category ==" in text


def test_synthesis_decomposed_pairs_each_loop_with_its_own_responsibility():
    """When the converter spreads multiple loops across maps, each
    loop's decrement expression must land on a responsibility inside
    that loop's own body — not on a responsibility from a different
    loop. The earlier positional cross-map correlation could pair the
    i-th loop tree node with the i-th cross-map LoopFork, which is
    wrong once root-map and plug-in-map LoopForks interleave: the
    decrement for loop B would attach to a body responsibility of
    loop A and the affected loop's counter never reached 0 (or
    decremented at the wrong time, leading to the v11 deadlock the
    user reported on ClaimsPaymentLog).
    """
    # Two distinct loops, body activities deliberately different
    # (``A`` vs ``B``) so the regression is visible by name. The
    # second loop sits inside a sub-sequence that decomposes into a
    # plug-in.
    tree = _seq(
        T(operator="*", children=[_leaf("Alpha"), _tau()]),
        _seq(
            _leaf("Pad1"),
            T(operator="*", children=[_leaf("Beta"), _tau()]),
            _leaf("Pad2"),
            _leaf("Pad3"),
        ),
    )
    log = []
    for i in range(20):
        log.append((f"a{i}", ["Alpha", "Pad1", "Beta", "Pad2", "Pad3"]))
    for i in range(10):
        log.append((f"b{i}", ["Alpha", "Alpha", "Pad1", "Beta", "Beta", "Pad2", "Pad3"]))

    decomp = {
        "on_root_sequence": True, "on_parallel": False,
        "on_alternative": False, "on_loop": False,
        "max_leaves_per_map": 1000, "min_leaves_to_decompose": 3,
        "balance_ratio": 0.0,
    }
    ucm = pm4py_ucm.convert_to_ucm(tree, decomposition=decomp)
    assert len(ucm.maps) == 2  # root + sub-sequence plug-in

    result = _clustering.cluster(log, tree)
    _scenarios.synthesize_scenarios(ucm, tree, result)

    # Each loop's decrement must land in the map holding that loop's
    # body. Alpha's loop is in the root map, Beta's in the plug-in; if
    # the cross-map correlation got mixed up, a decrement would be
    # spliced into the wrong map (and so into the wrong loop's body).
    from pm4py_ucm.objects.ucm.obj import UCM

    map_of: dict = {}          # responsibility name -> map name
    for m in ucm.maps:
        for n in m.nodes:
            if isinstance(n, UCM.RespRef) and n.resp_def is not None:
                map_of[n.resp_def.name] = m.name

    decrements = {}            # counter name -> responsibility name
    for resp in ucm.responsibilities:
        found = re.search(r"(Loop_\w+) = \1 - 1", resp.expression or "")
        if found:
            decrements[found.group(1)] = resp.name

    assert set(decrements) == {"Loop_Alpha", "Loop_Beta"}, (
        f"expected one decrement per loop, got {decrements}")
    assert map_of[decrements["Loop_Alpha"]] == map_of["Alpha"], (
        "Loop_Alpha's decrement landed in a different map from Alpha: "
        f"{map_of[decrements['Loop_Alpha']]!r} vs {map_of['Alpha']!r}")
    assert map_of[decrements["Loop_Beta"]] == map_of["Beta"], (
        "Loop_Beta's decrement landed in a different map from Beta: "
        f"{map_of[decrements['Loop_Beta']]!r} vs {map_of['Beta']!r}")
    assert map_of["Alpha"] != map_of["Beta"], (
        "fixture must place the two loops in different maps")


def test_synthesis_decomposed_loop_into_stub_keeps_single_bound_entry():
    """When the LoopEntryGuard's bypass arc would target a Stub, the
    synthesizer must splice an OrJoin in front so the stub keeps a
    single bound predecessor. Otherwise the stub ends up with two
    incoming arcs but only one InBinding — scenarios that take the
    bypass (0-iteration) path hit an unbound entry and the jUCMNav
    traversal halts mid-scenario (user-reported v12 / v23 failures
    on ClaimsPaymentLog decomposed export).
    """
    # Top-level sequence: loop body followed by a meaty tail that
    # decomposes into its own plug-in. The loop sits in the root
    # map; its post-loop continuation IS the stub for the tail.
    tree = _seq(
        T(operator="*", children=[_leaf("Body"), _tau()]),
        _seq(_leaf("T1"), _leaf("T2"), _leaf("T3"), _leaf("T4")),
    )
    log = []
    for i in range(20):
        log.append((f"once_{i}",  ["Body", "T1", "T2", "T3", "T4"]))
    for i in range(10):
        log.append((f"twice_{i}", ["Body", "Body", "T1", "T2", "T3", "T4"]))
    for i in range(5):
        log.append((f"zero_{i}",  ["T1", "T2", "T3", "T4"]))

    decomp = {
        "on_root_sequence": True, "on_parallel": False,
        "on_alternative": False, "on_loop": False,
        "max_leaves_per_map": 1000, "min_leaves_to_decompose": 3,
        "balance_ratio": 0.0,
    }
    from pm4py_ucm.objects.ucm.obj import UCM
    ucm = pm4py_ucm.convert_to_ucm(tree, decomposition=decomp)
    assert len(ucm.maps) == 2  # root + 1 plug-in (the meaty tail)

    result = _clustering.cluster(log, tree)
    _scenarios.synthesize_scenarios(ucm, tree, result)

    # Every stub on the root map must have exactly as many bound
    # InBindings as incoming arcs — no orphans.
    for m in ucm.maps:
        for n in m.nodes:
            if not isinstance(n, UCM.Stub):
                continue
            bound = set()
            for b in n.bindings:
                for ib in b.in_bindings:
                    bound.add(id(ib.stub_entry))
            unbound = [a for a in n.pred_connections if id(a) not in bound]
            assert not unbound, (
                f"stub {n.name!r} has {len(unbound)} unbound incoming arc(s); "
                "LoopEntryGuard bypass must be routed through an OrJoin"
            )


def test_synthesis_data_driven_sanitises_enum_values_starting_with_digits():
    """jUCMNav identifiers (variable names AND enum values) must
    start with a letter or underscore. Numeric / range-like attribute
    values (``"3"``, ``"3_5"``, ``"500"``) are common in real logs
    (claim-amount buckets, region codes) and would otherwise produce
    a model jUCMNav rejects at load. The sanitiser prefixes a single
    underscore when the resulting identifier would start with a
    digit; existing letter-leading values are untouched.
    """
    pytest = __import__("pytest")
    pytest.importorskip("sklearn")
    import pandas as pd

    tree = _seq(
        _leaf("Start"),
        _xor(_leaf("A"), _leaf("B")),
        _leaf("End"),
    )
    # ``Bucket`` takes raw values ``3`` (digit-leading) and
    # ``3_5`` (digit-leading with separator). Both must be rewritten
    # in the .jucm to ``_3`` / ``_3_5``.
    rows = []
    cases = (
        [(f"a{i}", "3",   ["Start", "A", "End"]) for i in range(20)]
        + [(f"b{i}", "3_5", ["Start", "B", "End"]) for i in range(15)]
    )
    for case_id, bucket, trace in cases:
        for j, act in enumerate(trace):
            rows.append({
                "case:concept:name": case_id,
                "concept:name": act,
                "time:timestamp": pd.Timestamp("2026-01-01") + pd.Timedelta(j, "s"),
                "Bucket": bucket,
            })
    log_df = pd.DataFrame(rows)

    ucm = pm4py_ucm.convert_to_ucm(tree)
    result = _clustering.cluster(log_df, tree)
    _scenarios.synthesize_scenarios(
        ucm, tree, result,
        condition_strategy="data-driven", log=log_df,
    )

    # The enumeration type lists sanitised values, not raw ones.
    bucket_enum = next(
        e for e in ucm.enumeration_types if e.name.startswith("Bucket")
    )
    assert "3" not in bucket_enum.values, (
        f"raw digit-leading enum value leaked into the model: "
        f"{bucket_enum.values!r}"
    )
    assert "_3" in bucket_enum.values
    assert "_3_5" in bucket_enum.values

    # Round-trip through the exporter to be sure the .jucm carries
    # the sanitised forms in scenario initialisations and arc
    # conditions (the two places jUCMNav actually parses them).
    text = _jucm_exporter.serialize_to_string(ucm)
    assert 'Bucket == _3' in text or 'Bucket == _3_5' in text
    # And the raw, illegal form must NOT appear as an identifier.
    import re as _re
    assert not _re.search(r'Bucket == 3(?![_\w])', text)
    assert not _re.search(r'Bucket == 3_5(?![_\w])', text)


def test_jucm_export_without_scenarios_remains_byte_stable_legacy():
    """A UCM with no scenario groups must produce identical output to
    the pre-scenarios legacy exporter (no spurious <scenarioGroups/>,
    <variables/>, or <enumerationTypes/> elements)."""
    tree, _ = _build_tree_and_log()
    ucm = pm4py_ucm.convert_to_ucm(tree)
    text = _jucm_exporter.serialize_to_string(ucm)
    assert "<ucmspec/>" in text
    assert "<scenarioGroups" not in text
    assert "<variables" not in text
    assert "<enumerationTypes" not in text


# ---------------------------------------------------------------------------
# Per-sequence branch labelling for the data-driven strategy
# ---------------------------------------------------------------------------
#
# The data-driven strategy has to know which branch every case took at
# every XOR. It used to answer that by replaying each case on its own —
# 150 370 replays on Road Traffic, a log with 231 distinct activity
# sequences. A trace's parse is a function of its activity sequence
# alone, so the labelling now replays once per distinct sequence and
# fans the result out over the cases sharing it. These tests pin that
# the two ways of asking agree, case for case.


def _branch_labels_per_case(tree, log_df, node_ids, xor_ids):
    """Reference labelling: replay every case separately.

    Deliberately a transcription of what
    ``_label_cases_by_xor_branch`` did before it learned to work per
    sequence — the whole point of the comparison is that nothing about
    the answer changed."""
    from pm4py_ucm.algo.discovery.variants import choice_signature as _cs_mod
    labels = {x: {} for x in xor_ids}
    for case_id, trace_df in log_df.groupby("case:concept:name"):
        counts = {}
        sig = _cs_mod.replay(
            tree, trace_df["concept:name"].tolist(), node_ids=node_ids,
            coarsen_loops=True, xor_branch_counts=counts,
        )
        if sig == _cs_mod.NOFIT:
            continue
        for xor_id, branch_counts in counts.items():
            if sum(branch_counts.values()) == 1:
                labels[xor_id][str(case_id)] = next(iter(branch_counts))
    return labels


def _labelling_tree_and_log():
    """A tree exercising every shape the labelling must survive, plus a
    log carrying many cases per distinct sequence.

    ``X -> (A × B) -> ((P × Q) ⟳ R) -> (Y ∥ Z) -> W``:

    * ``A × B`` is the outside-loop XOR — decided once per case, and
      the one the decision miner actually trains on;
    * ``P × Q`` sits inside the loop, so a two-iteration trace decides
      it twice and it drops out under the ``sum(counts) == 1`` rule;
    * ``Y ∥ Z`` gives two distinct sequences the same parse shape and
      drives replay through the parallel projections;
    * one trace does not replay at all and must contribute no label
      anywhere.

    Returns ``(tree, log_df, shapes)`` where ``shapes`` is the
    ``(sequence, attribute_value, n_cases)`` list the log was built
    from — one entry per distinct sequence."""
    import pandas as pd

    tree = _seq(
        _leaf("X"),
        _xor(_leaf("A"), _leaf("B")),
        _loop(_xor(_leaf("P"), _leaf("Q")), _leaf("R")),
        _par(_leaf("Y"), _leaf("Z")),
        _leaf("W"),
    )
    shapes = [
        (["X", "A", "P", "Y", "Z", "W"], "low", 40),
        (["X", "A", "P", "Z", "Y", "W"], "low", 25),
        (["X", "A", "Q", "R", "P", "Y", "Z", "W"], "low", 15),
        (["X", "B", "Q", "Y", "Z", "W"], "high", 30),
        (["X", "B", "Q", "R", "Q", "Z", "Y", "W"], "high", 20),
        (["X", "A", "UNKNOWN", "W"], "low", 5),  # never replays
    ]
    rows = []
    n = 0
    for trace, category, repeats in shapes:
        for _ in range(repeats):
            n += 1
            for j, act in enumerate(trace):
                rows.append({
                    "case:concept:name": f"c{n:04d}",
                    "concept:name": act,
                    "time:timestamp": (
                        pd.Timestamp("2026-01-01") + pd.Timedelta(j, "s")
                    ),
                    "Category": category,
                })
    return tree, pd.DataFrame(rows), shapes


def _ids_and_xors(tree):
    """``(node_ids, [multi_child_xor_tree_id, ...])`` for ``tree``."""
    from pm4py_ucm.algo.discovery.variants import choice_signature as _cs_mod
    node_ids = _cs_mod.assign_node_ids(tree)
    xor_ids = [
        x for x, _, _ in _scenarios._collect_multi_child_xors(tree, node_ids)
    ]
    return node_ids, xor_ids


def _count_replays(monkeypatch):
    """Count the :func:`choice_signature.replay` calls the parse table
    makes. Returns a one-element list holding the running count."""
    from pm4py_ucm.algo.discovery.variants import parses as _parses_mod
    calls = [0]
    real = _parses_mod._cs.replay

    def counting(*args, **kwargs):
        calls[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(_parses_mod._cs, "replay", counting)
    return calls


def test_branch_labels_per_sequence_agree_with_per_case(monkeypatch):
    """The per-sequence labelling returns exactly the mapping per-case
    replay did — and reaches it with one replay per distinct sequence
    rather than one per case."""
    tree, log_df, shapes = _labelling_tree_and_log()
    node_ids, xor_ids = _ids_and_xors(tree)

    expected = _branch_labels_per_case(tree, log_df, node_ids, xor_ids)
    calls = _count_replays(monkeypatch)
    actual = _scenarios._label_cases_by_xor_branch(
        tree, log_df, node_ids, xor_ids,
    )

    assert actual == expected
    assert calls[0] == len(shapes), (
        f"expected one replay per distinct sequence ({len(shapes)}); "
        f"got {calls[0]}"
    )
    n_cases = log_df["case:concept:name"].nunique()
    assert len(shapes) < n_cases  # the saving is real on this log

    xor_seq = _scenarios._collect_multi_child_xors(tree, node_ids)
    outside = next(x for x, _, loop in xor_seq if loop is None)
    inside = next(x for x, _, loop in xor_seq if loop is not None)
    n_nofit = next(r for trace, _c, r in shapes if "UNKNOWN" in trace)
    # Every case that replays is labelled at the outside-loop XOR.
    assert len(actual[outside]) == n_cases - n_nofit
    # The inside-loop XOR is labelled only where the loop ran once —
    # a second iteration decides it twice and it drops out. (The
    # emitter skips inside-loop XORs anyway; this pins the rule.)
    assert 0 < len(actual[inside]) < len(actual[outside])


def test_branch_labels_reuse_a_shared_parse_table(monkeypatch):
    """Handed the table clustering already built, the labelling
    replays nothing at all — and still returns the same mapping."""
    from pm4py_ucm.algo.discovery.variants import parses as _parses_mod

    tree, log_df, _shapes = _labelling_tree_and_log()
    node_ids, xor_ids = _ids_and_xors(tree)
    expected = _branch_labels_per_case(tree, log_df, node_ids, xor_ids)

    table = _parses_mod.build(log_df, tree)
    _clustering.cluster(log_df, tree, parses=table)

    calls = _count_replays(monkeypatch)
    actual = _scenarios._label_cases_by_xor_branch(
        tree, log_df, node_ids, xor_ids, parses=table,
    )
    assert actual == expected
    assert calls[0] == 0, "a complete table should need no further replay"


def test_branch_labels_survive_an_uncoarsened_parse_table():
    """A table built for ``coarsen_loops=False`` clustering labels
    identically. Coarsening changes how a fitting parse is *reported*,
    never which parse the search finds — the labelling leans on that to
    share one table with clustering whatever the caller asked for."""
    from pm4py_ucm.algo.discovery.variants import parses as _parses_mod

    tree, log_df, _shapes = _labelling_tree_and_log()
    node_ids, xor_ids = _ids_and_xors(tree)
    expected = _branch_labels_per_case(tree, log_df, node_ids, xor_ids)

    fine = _parses_mod.build(log_df, tree, coarsen_loops=False)
    assert fine.coarsen_loops is False
    actual = _scenarios._label_cases_by_xor_branch(
        tree, log_df, node_ids, xor_ids, parses=fine,
    )
    assert actual == expected


def test_branch_labels_replay_what_a_stale_parse_table_lacks(monkeypatch):
    """A supplied table is a performance hint, not a source of truth:
    sequences it does not carry are replayed on demand and remembered,
    never silently dropped."""
    from pm4py_ucm.algo.discovery.variants import parses as _parses_mod

    tree, log_df, shapes = _labelling_tree_and_log()
    node_ids, xor_ids = _ids_and_xors(tree)
    expected = _branch_labels_per_case(tree, log_df, node_ids, xor_ids)

    partial = _parses_mod.build(log_df, tree)
    dropped = sorted(partial.parses)[:2]
    for key in dropped:
        del partial.parses[key]

    calls = _count_replays(monkeypatch)
    actual = _scenarios._label_cases_by_xor_branch(
        tree, log_df, node_ids, xor_ids, parses=partial,
    )
    assert actual == expected
    assert calls[0] == len(dropped)
    assert len(partial.parses) == len(shapes)  # stragglers were remembered


def test_data_driven_conditions_identical_with_and_without_parse_table():
    """End to end: the .jucm the data-driven strategy writes does not
    depend on whether synthesis was handed a parse table."""
    import pytest
    pytest.importorskip("sklearn")
    from pm4py_ucm.algo.discovery.variants import parses as _parses_mod

    tree, log_df, _shapes = _labelling_tree_and_log()

    def synth(parses):
        clustering = _clustering.cluster(log_df, tree, parses=parses)
        ucm = pm4py_ucm.convert_to_ucm(tree)
        _scenarios.synthesize_scenarios(
            ucm, tree, clustering,
            condition_strategy="data-driven", log=log_df,
            emit_conditions=True, parses=parses,
        )
        text = _jucm_exporter.serialize_to_string(ucm)
        # The header stamps wall-clock time, so two serializations
        # either side of a second boundary differ for no reason.
        return re.sub(r' (?:created|modified)="[^"]*"', "", text)

    assert synth(None) == synth(_parses_mod.build(log_df, tree))


def test_branch_labels_need_no_dataframe_when_the_table_carries_the_cases():
    """The labelling reads the table's normalised case view rather than
    re-grouping the frame — so a complete table is all it needs.

    Re-deriving each case's sequence with a pandas groupby cost 46 s of
    an 80 s Road Traffic run, doing again what building the table had
    already done."""
    from pm4py_ucm.algo.discovery.variants import parses as _parses_mod

    tree, log_df, _shapes = _labelling_tree_and_log()
    node_ids, xor_ids = _ids_and_xors(tree)
    expected = _branch_labels_per_case(tree, log_df, node_ids, xor_ids)

    table = _parses_mod.build(log_df, tree)
    assert table.cases is not None
    actual = _scenarios._label_cases_by_xor_branch(
        tree, None, node_ids, xor_ids, parses=table,
    )
    assert actual == expected


def test_branch_labels_normalise_the_log_when_no_case_view_is_supplied():
    """Without a table — or with one built by ``replay_sequences``,
    which carries parses but no cases — the log is normalised the same
    way clustering normalises it, so the two agree on what each case's
    sequence is."""
    from pm4py_ucm.algo.discovery.variants import parses as _parses_mod

    tree, log_df, _shapes = _labelling_tree_and_log()
    node_ids, xor_ids = _ids_and_xors(tree)
    expected = _branch_labels_per_case(tree, log_df, node_ids, xor_ids)

    assert _scenarios._label_cases_by_xor_branch(
        tree, log_df, node_ids, xor_ids,
    ) == expected

    cases_only = _parses_mod.replay_sequences(
        tree, [seq for _cid, seq in _parses_mod.build(log_df, tree).cases],
    )
    assert cases_only.cases is None
    assert _scenarios._label_cases_by_xor_branch(
        tree, log_df, node_ids, xor_ids, parses=cases_only,
    ) == expected


# ---------------------------------------------------------------------------
# Reproducibility of the mined conditions
# ---------------------------------------------------------------------------
#
# Mining the same log twice used to be able to produce two different
# models. ``pm4py.read_xes`` does not fix the frame's column order
# between runs, and the one-hot feature columns the decision tree sees
# were built in that order — which is what settles a tie between two
# equally-informative splits. On ClaimsPaymentLog one run guarded a
# branch with ``Broker == Citadel_Insurance`` and the next with
# ``Broker == Greenline_Insurance_Services``, at identical accuracy.


def _tied_attribute_log():
    """A log whose branch is predicted *perfectly* by either of two
    attributes, so which one the tree splits on is settled purely by
    feature order — the tie the old code let the column order break."""
    import pandas as pd

    tree = _seq(_leaf("X"), _xor(_leaf("A"), _leaf("B")), _leaf("C"))
    rows = []
    for i in range(40):
        takes_a = i % 2 == 0
        for j, act in enumerate(["X", "A" if takes_a else "B", "C"]):
            rows.append({
                "case:concept:name": f"c{i:03d}",
                "concept:name": act,
                "time:timestamp": pd.Timestamp("2026-01-01") + pd.Timedelta(j, "s"),
                # Two attributes, each on its own a perfect predictor.
                "Alpha": "left" if takes_a else "right",
                "Zeta": "up" if takes_a else "down",
            })
    return tree, pd.DataFrame(rows)


def _mine(tree, log_df):
    """Mine the log's OR-fork conditions; return the per-branch
    expressions and the order the URN variables were created in."""
    clustering = _clustering.cluster(log_df, tree)
    ucm = pm4py_ucm.convert_to_ucm(tree)
    group = _scenarios.synthesize_scenarios(
        ucm, tree, clustering,
        condition_strategy="data-driven", log=log_df, emit_conditions=True,
    )
    results = {r.tree_xor_id: dict(r.per_branch_expression)
               for r in group._mining_results}
    return results, [v.name for v in ucm.variables]


def test_mined_conditions_do_not_depend_on_the_frames_column_order():
    """Reordering the log's columns must not change the mined model."""
    import pytest
    pytest.importorskip("sklearn")

    tree, log_df = _tied_attribute_log()
    forward, vars_forward = _mine(tree, log_df)
    reversed_, vars_reversed = _mine(
        tree, log_df.reindex(columns=list(reversed(log_df.columns))),
    )

    assert forward == reversed_
    assert vars_forward == vars_reversed
    # And the tie really is a tie — both attributes predict perfectly,
    # so nothing but the ordering rule decides which one is used.
    exprs = " ".join(e for r in forward.values() for e in r.values())
    assert "Alpha" in exprs and "Zeta" not in exprs, (
        f"expected the tie broken by name; got {exprs}"
    )


def test_mined_conditions_do_not_depend_on_case_iteration_order():
    """The trained tree depends on which cases took which branch, not
    on the order the caller collected them in."""
    import pytest
    pytest.importorskip("sklearn")
    from pm4py_ucm.algo.discovery.scenarios import decision_mining as _dm_mod

    tree, log_df = _tied_attribute_log()
    features_df, specs, columns, _raw = _dm_mod.extract_case_features(log_df)
    node_ids, xor_ids = _ids_and_xors(tree)
    labels = _scenarios._label_cases_by_xor_branch(
        tree, log_df, node_ids, xor_ids,
    )[xor_ids[0]]

    def mine(case_to_branch):
        return _dm_mod.mine_orfork_condition(
            features_df, specs, columns, case_to_branch, xor_ids[0], 2,
        ).per_branch_expression

    forward = mine(dict(labels))
    backward = mine({k: labels[k] for k in reversed(list(labels))})
    assert forward == backward
