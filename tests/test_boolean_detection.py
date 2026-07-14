"""Case-insensitive boolean type detection in decision mining (issue #6).

A case-constant attribute whose string values are boolean literals in
**mixed case** (``"True"`` / ``"FALSE"`` / ``"TRUE"``) must classify as a
jUCMNav *boolean* variable — emitting ``x == true`` / ``x == false`` and
enabling the expression minimizer's complement rule — instead of a
two-value enumeration (``x == True_``). Clean lowercase / native-bool /
0-1 columns keep classifying as boolean exactly as before (no
regression, byte-stable exports).
"""
from __future__ import annotations

import pytest

pd = pytest.importorskip("pandas")

import pm4py_ucm
from pm4py_ucm.algo.discovery.scenarios import decision_mining as _dm
from pm4py_ucm.algo.discovery.scenarios import synthesis as _scenarios
from pm4py_ucm.algo.discovery.variants import clustering as _clustering


class T:
    def __init__(self, operator=None, label=None, children=None):
        self.operator = operator
        self.label = label
        self.children = children or []


def _leaf(x):
    return T(label=x)


def _xor(*ch):
    return T(operator="X", children=list(ch))


# ---------------------------------------------------------------------------
# The interpreter helper
# ---------------------------------------------------------------------------
class TestBooleanValueHelper:

    def test_case_insensitive_strings(self):
        bv = _dm._boolean_value
        assert bv("True") is True
        assert bv("TRUE") is True
        assert bv("true") is True
        assert bv("False") is False
        assert bv("FALSE") is False
        assert bv("  True  ") is True          # whitespace tolerated

    def test_native_bool_and_numbers(self):
        bv = _dm._boolean_value
        assert bv(True) is True and bv(False) is False
        assert bv(1) is True and bv(0) is False
        assert bv("1") is True and bv("0") is False

    def test_non_boolean_returns_none(self):
        bv = _dm._boolean_value
        assert bv("maybe") is None
        assert bv("2") is None
        assert bv(2) is None
        assert bv(None) is None


# ---------------------------------------------------------------------------
# Classification in extract_case_features
# ---------------------------------------------------------------------------
def _classify(flag_values):
    """Run extract_case_features on a one-event-per-case log whose only
    attribute is ``Flag`` = the given per-case values."""
    rows = [
        {"case:concept:name": f"c{i}", "concept:name": "A",
         "time:timestamp": pd.Timestamp("2026-01-01") + pd.Timedelta(i, "s"),
         "Flag": v}
        for i, v in enumerate(flag_values)
    ]
    feats, specs, _, _ = _dm.extract_case_features(pd.DataFrame(rows))
    return feats, specs.get("Flag")


class TestClassification:

    def test_mixed_case_strings_classify_boolean(self):
        _, spec = _classify(
            ["True", "FALSE", "TRUE", "false", "True", "False"])
        assert spec is not None
        assert spec.type == "boolean"          # not "enumeration"

    def test_lowercase_still_boolean(self):
        _, spec = _classify(["true", "false", "true", "false"])
        assert spec.type == "boolean"

    def test_native_bool_still_boolean(self):
        _, spec = _classify([True, False, True, False])
        assert spec.type == "boolean"

    def test_zero_one_still_boolean(self):
        _, spec = _classify([0, 1, 0, 1])
        assert spec.type == "boolean"

    def test_three_valued_string_column_not_boolean(self):
        _, spec = _classify(["true", "false", "maybe", "true", "maybe"])
        # A non-boolean third value -> enumeration (or dropped), never
        # a boolean variable.
        assert spec is None or spec.type != "boolean"

    def test_mixed_case_encoding_maps_true_false(self):
        feats, spec = _classify(["TRUE", "false", "True", "FALSE"])
        assert spec.type == "boolean"
        col = feats["Flag"]
        assert col.loc["c0"] == 1.0   # TRUE
        assert col.loc["c1"] == 0.0   # false
        assert col.loc["c2"] == 1.0   # True
        assert col.loc["c3"] == 0.0   # FALSE


# ---------------------------------------------------------------------------
# Expression emission
# ---------------------------------------------------------------------------
class TestExpressionEmission:

    def test_split_to_expressions_boolean(self):
        spec = _dm.AttributeSpec(
            source_name="Flag", jucmnav_name="Flag", type="boolean")
        left, right = _dm._split_to_expressions("Flag", 0.5, {"Flag": spec})
        assert left == "Flag == false"
        assert right == "Flag == true"


# ---------------------------------------------------------------------------
# End-to-end data-driven synthesis
# ---------------------------------------------------------------------------
class TestDataDrivenEndToEnd:

    def _mixed_case_boolean_log(self):
        tree = T(operator="->", children=[
            _leaf("X"), _xor(_leaf("A"), _leaf("B")), _leaf("C"),
        ])
        rows = []
        cases = (
            [(f"t{i}", "True", ["X", "A", "C"]) for i in range(20)]
            + [(f"f{i}", "FALSE", ["X", "B", "C"]) for i in range(20)]
        )
        for case_id, flag, trace in cases:
            for j, act in enumerate(trace):
                rows.append({
                    "case:concept:name": case_id,
                    "concept:name": act,
                    "time:timestamp": (
                        pd.Timestamp("2026-01-01") + pd.Timedelta(j, "s")),
                    "Flag": flag,
                })
        return tree, pd.DataFrame(rows)

    def test_mixed_case_boolean_yields_boolean_variable_and_conditions(self):
        pytest.importorskip("sklearn")
        tree, log_df = self._mixed_case_boolean_log()
        result = _clustering.cluster(log_df, tree)
        ucm = pm4py_ucm.convert_to_ucm(tree)
        _scenarios.synthesize_scenarios(
            ucm, tree, result,
            condition_strategy="data-driven", log=log_df,
            emit_conditions=True,
        )
        # Mixed-case "True"/"FALSE" -> a BOOLEAN variable, not an enum.
        flag_var = next(v for v in ucm.variables if v.name == "Flag")
        assert flag_var.type == "boolean"
        assert not ucm.enumeration_types  # no enum synthesized for Flag

        or_fork = next(
            n for m in ucm.maps for n in m.nodes
            if type(n).__name__ == "OrFork"
            and n.name not in ("LoopFork", "LoopEntryGuard")
        )
        exprs = [a.condition.expression for a in or_fork.succ_connections
                 if a.condition]
        joined = " ".join(exprs)
        # Boolean comparisons (== true / == false), never an enum token.
        assert "Flag == true" in joined or "Flag == false" in joined
        assert "True_" not in joined and "FALSE" not in joined

    def test_scenario_init_uses_lowercase_boolean_literal(self):
        pytest.importorskip("sklearn")
        tree, log_df = self._mixed_case_boolean_log()
        result = _clustering.cluster(log_df, tree)
        ucm = pm4py_ucm.convert_to_ucm(tree)
        group = _scenarios.synthesize_scenarios(
            ucm, tree, result,
            condition_strategy="data-driven", log=log_df,
            emit_conditions=True,
        )
        # Every Flag initialisation value is a lowercase boolean literal
        # (mode of raw "True"/"FALSE" spellings mapped correctly).
        flag_inits = [
            init.value
            for sc in group.scenarios for init in sc.initializations
            if init.variable.name == "Flag"
        ]
        assert flag_inits
        assert set(flag_inits) <= {"true", "false"}
