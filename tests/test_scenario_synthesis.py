"""End-to-end smoke tests for variant clustering + scenario synthesis.

Each test builds a synthetic process tree, fabricates a tiny event log
covering selected variants, runs the full pipeline (convert → cluster
→ synthesize), and asserts the shape of the resulting UCM and
ClusteringResult."""
from __future__ import annotations

import io

import pm4py_ucm
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
        assert init.value == sc.name  # variant_id matches scenario name


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
        if type(n).__name__ == "OrFork" and n.name != "LoopFork"
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
        if type(n).__name__ == "OrFork" and n.name != "LoopFork"
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
    counters = [v for v in ucm.variables if v.name.startswith("loop_counter_")]
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
        assert "loop_counter_" in (arc.condition.expression or "")
    # Crucially the *immediate* downstream bend->target arc no longer
    # carries the counter condition (jUCMNav would ignore it there
    # and treat the LoopFork branch as default true).
    for arc in loop_fork.succ_connections:
        target = arc.target
        if not hasattr(target, "succ_connections"):
            continue
        for downstream in target.succ_connections:
            expr = downstream.condition.expression if downstream.condition else ""
            assert "loop_counter_" not in expr


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
        if type(n).__name__ == "OrFork" and n.name != "LoopFork"
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


def test_synthesis_loopfork_conditions_are_mutually_exclusive():
    """The LoopFork's exit arc should be ``counter == 0`` and the
    redo arc ``counter > 0`` — at any point exactly one of these
    holds, eliminating the non-determinism the default ``true``
    conditions caused."""
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
    # Both halves of the loop's branch must be expressed in terms of
    # the counter, not the default ``true``.
    assert 'expression="loop_counter_' in text
    assert '== 0' in text
    assert '> 0' in text


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
    assert "loop_counter_" in (body_resp.expression or "")
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
    by_name = {sc.name: sc for sc in group_uncapped.scenarios}
    for vname, expected in (("v1", "1"), ("v2", "3")):
        sc = by_name[vname]
        counter_inits = [
            i for i in sc.initializations
            if i.variable.name.startswith("loop_counter_")
        ]
        assert len(counter_inits) == 1
        assert counter_inits[0].value == expected
    # Capped at 2 (default): v2's three iterations clamp to 2.
    ucm2 = pm4py_ucm.convert_to_ucm(loop_tree)
    group_capped = _scenarios.synthesize_scenarios(
        ucm2, loop_tree, result, emit_conditions=True,
        max_loop_iterations=2,
    )
    by_name = {sc.name: sc for sc in group_capped.scenarios}
    assert next(
        i for i in by_name["v2"].initializations
        if i.variable.name.startswith("loop_counter_")
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
    assert any("loop_counter_" in (r.expression or "") for r in decrement_resps)


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
        if type(n).__name__ == "OrFork" and n.name != "LoopFork"
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
        if type(n).__name__ == "OrFork" and n.name != "LoopFork"
    ]
    assert inside_loop_or_forks, "fixture must have an inside-loop OR-fork"
    for of in inside_loop_or_forks:
        exprs = [a.condition.expression if a.condition else ""
                 for a in of.succ_connections]
        # Both branches must mention variant_id AND a loop_counter
        # comparison — that's the whole point of the combined
        # condition. The fallback true/false split would fail this.
        has_variant_id_branch = any("variant_id" in e for e in exprs)
        has_counter_branch = any("loop_counter_" in e for e in exprs)
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
        if type(n).__name__ == "OrFork" and n.name != "LoopFork"
    ]
    # The fixture has exactly one inside-loop XOR.
    assert len(inside_loop_orforks) == 1
    for of in inside_loop_orforks:
        for arc in of.succ_connections:
            expr = arc.condition.expression if arc.condition else ""
            # Every branch's condition references the loop counter
            # (or is ``false`` for a branch nobody took).
            assert "loop_counter_" in expr or expr == "false", (
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
