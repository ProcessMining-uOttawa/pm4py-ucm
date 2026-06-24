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
    # The bypass arc lands on the post-loop node (not the LoopJoin).
    bypass_arc = next(
        a for a in guard.succ_connections
        if a.condition and "<= 0" in a.condition.expression
    )
    assert type(bypass_arc.target).__name__ != "OrJoin"


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


def test_synthesis_loop_body_responsibility_carries_decrement():
    """A body responsibility (Review in this fixture) is decorated
    with the counter-decrement expression so the counter steps down
    once per loop iteration."""
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
    body_resp = next(r for r in ucm.responsibilities if r.name == "Review")
    assert _LOOP_COUNTER_RE.search(body_resp.expression or "")
    assert "= " in body_resp.expression
    assert "- 1" in body_resp.expression


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
