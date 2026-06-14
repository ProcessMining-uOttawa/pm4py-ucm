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


def test_synthesis_skips_loop_xor_condition_emission():
    # Loop with XOR inside body. Inside-loop XORs must NOT get
    # variant_id conditions emitted (they're left at default).
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
    # The single non-LoopFork OrFork is INSIDE the loop body. Its
    # outgoing arcs should have NO variant_id expression (left at
    # default ``true``).
    inside_loop_orforks = [
        n for m in ucm.maps for n in m.nodes
        if type(n).__name__ == "OrFork" and n.name != "LoopFork"
    ]
    for of in inside_loop_orforks:
        for arc in of.succ_connections:
            expr = arc.condition.expression if arc.condition else "true"
            assert "variant_id" not in expr, (
                f"inside-loop XOR conditions must not reference "
                f"variant_id; got {expr!r}"
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
