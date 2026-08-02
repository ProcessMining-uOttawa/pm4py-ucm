"""Numeric attributes serialised as strings must type as integers.

XES exports routinely stringify the one attribute that governs the
process. BPI Challenge 2012 ships ``case:AMOUNT_REQ`` as ``str`` with
631 distinct values and a case-constant fraction of 1.000 — exactly the
variable one would expect to drive a loan application — and a
dtype-based classifier discards it as a high-cardinality string,
leaving data-driven OR-fork mining with nothing to work with at all.

Typing is therefore decided by **content, not dtype**: a column whose
values parse as numbers is the same attribute as the ``int64`` column
carrying those numbers, and takes the same integer / scaled-integer
path. Cardinality does not enter into it — an ``int64`` column with
three distinct values is already an integer here, never an
enumeration, so a *string* column of three distinct numbers is too.
Genuinely non-numeric strings keep their existing behaviour.
"""
from __future__ import annotations

import warnings

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


def _classify(values, col="Amount", **kwargs):
    """Run extract_case_features on a one-event-per-case log whose only
    attribute is ``col`` = the given per-case values. Returns
    ``(features_df, spec_or_None)``."""
    rows = [
        {"case:concept:name": f"c{i}", "concept:name": "A",
         "time:timestamp": pd.Timestamp("2026-01-01") + pd.Timedelta(i, "s"),
         col: v}
        for i, v in enumerate(values)
    ]
    feats, specs, _, _ = _dm.extract_case_features(pd.DataFrame(rows), **kwargs)
    # Specs are keyed by sanitised jUCMNav name ("org:resource" ->
    # "org_resource"), so look the column up by its source name.
    spec = next((s for s in specs.values() if s.source_name == col), None)
    return feats, spec


# ---------------------------------------------------------------------------
# The four cases named in the report
# ---------------------------------------------------------------------------
class TestNumericStringClassification:

    def test_string_column_of_integers_is_integer(self):
        # dtype is object, values are integers-as-text.
        values = [str(v) for v in (100, 250, 375, 500, 625, 750)]
        feats, spec = _classify(values)
        assert spec is not None, "numeric-string column must not be dropped"
        assert spec.type == "integer"
        assert spec.scale_factor == 1
        # ...and it encodes to the numbers themselves.
        assert feats["Amount"].loc["c0"] == 100.0
        assert feats["Amount"].loc["c5"] == 750.0

    def test_string_column_of_decimals_picks_up_scale_factor(self):
        values = ["10.25", "20.50", "30.75", "40.00", "55.10"]
        feats, spec = _classify(values)
        assert spec.type == "integer"
        assert spec.scale_factor == 100      # two decimal places
        # The encoded feature is the scaled integer the mined
        # thresholds and the scenario initialisations both live in.
        assert feats["Amount"].loc["c0"] == 1025.0
        assert feats["Amount"].loc["c2"] == 3075.0

    def test_mixed_column_stays_an_enumeration(self):
        # Only half the values parse — far below the coercion
        # threshold, so this is a categorical column that happens to
        # contain some numeric labels.
        _, spec = _classify(["100", "200", "high", "low", "100", "high"])
        assert spec is not None
        assert spec.type == "enumeration"
        assert spec.scale_factor == 1

    def test_low_cardinality_numeric_string_is_integer_not_enumeration(self):
        # The confirmed intent: content wins over cardinality. Three
        # distinct numbers as *text* type exactly as three distinct
        # numbers as int64 would — an integer, never an enumeration.
        _, spec = _classify(["1", "2", "3", "1", "2", "3", "1"])
        assert spec.type == "integer"
        assert spec.enum_values == []

    def test_low_cardinality_native_ints_agree(self):
        # The consistency claim above, checked rather than assumed:
        # same values, int64 dtype, same classification.
        _, spec = _classify([1, 2, 3, 1, 2, 3, 1])
        assert spec.type == "integer"


# ---------------------------------------------------------------------------
# The BPI 2012 shape: no longer abandons
# ---------------------------------------------------------------------------
class TestHighCardinalityNumericString:

    def _amount_req_log(self, n_distinct=631):
        """A case-constant string-typed amount column with more distinct
        values than ``max_enum_cardinality`` — the AMOUNT_REQ shape."""
        rows = []
        for i in range(n_distinct):
            amount = str(1000 + i * 50)
            for j, act in enumerate(["Submit", "Assess", "Decide"]):
                rows.append({
                    "case:concept:name": f"c{i}",
                    "concept:name": act,
                    "time:timestamp": (
                        pd.Timestamp("2026-01-01") + pd.Timedelta(j, "s")),
                    "case:AMOUNT_REQ": amount,     # constant within the case
                })
        return pd.DataFrame(rows)

    def test_amount_req_survives_instead_of_being_dropped(self):
        log = self._amount_req_log()
        feats, specs, cols, per_case_raw = _dm.extract_case_features(log)
        # Previously: features_df is None and a "no attributes met the
        # type / cardinality filters" warning abandoned the whole log.
        assert feats is not None
        spec = specs["AMOUNT_REQ"]                 # ``case:`` prefix stripped
        assert spec.type == "integer"
        assert spec.source_name == "case:AMOUNT_REQ"
        assert len(feats) == 631
        assert feats["AMOUNT_REQ"].loc["c0"] == 1000.0

    def test_extraction_emits_no_abandonment_warning(self):
        log = self._amount_req_log(n_distinct=40)
        with warnings.catch_warnings(record=True) as record:
            warnings.simplefilter("always")
            feats, _, _, _ = _dm.extract_case_features(log)
        assert feats is not None
        assert not [w for w in record if "abandoned" in str(w.message)]

    def test_high_cardinality_non_numeric_string_still_dropped(self):
        # No regression for genuinely non-numeric high-cardinality
        # strings: still dropped, still abandons when it is all the log
        # carries.
        rows = [
            {"case:concept:name": f"c{i}", "concept:name": "A",
             "time:timestamp": pd.Timestamp("2026-01-01") + pd.Timedelta(i, "s"),
             "FreeText": f"note number {i} about the case"}
            for i in range(50)
        ]
        with pytest.warns(UserWarning, match="type / cardinality"):
            feats, specs, _, _ = _dm.extract_case_features(pd.DataFrame(rows))
        assert feats is None
        assert specs == {}


# ---------------------------------------------------------------------------
# The threshold is a knob, and it has an off position
# ---------------------------------------------------------------------------
class TestCoercionThreshold:

    def _mostly_numeric(self):
        """99 numbers + 1 ``"N/A"`` — exactly 0.99 convertible, the
        default threshold. A single missing-data marker in an otherwise
        numeric column is the case the tolerance exists for."""
        return [str(100 + 10 * i) for i in range(99)] + ["N/A"]

    def test_default_threshold_tolerates_a_few_unparseable_markers(self):
        feats, spec = _classify(self._mostly_numeric())
        assert spec.type == "integer"
        # The straggler becomes NaN rather than sinking the column;
        # the tree's own fillna handles it downstream.
        assert feats["Amount"].loc["c0"] == 100.0
        assert pd.isna(feats["Amount"].loc["c99"])

    def test_strict_threshold_keeps_the_marker_column_out_of_numerics(self):
        # At 1.0 the single "N/A" disqualifies the column, which then
        # falls to the string path — 100 distinct values, so dropped.
        with pytest.warns(UserWarning, match="type / cardinality"):
            feats, spec = _classify(
                self._mostly_numeric(), numeric_coercion_threshold=1.0)
        assert feats is None
        assert spec is None

    def test_threshold_above_one_disables_coercion_entirely(self):
        # The escape hatch back to dtype-only typing: a clean numeric
        # string column of high cardinality is dropped as before.
        values = [str(v) for v in range(1000, 1000 + 50)]
        with pytest.warns(UserWarning, match="type / cardinality"):
            feats, spec = _classify(
                values, numeric_coercion_threshold=1.01)
        assert feats is None
        assert spec is None

    def test_native_numeric_columns_ignore_the_threshold(self):
        # int64 columns never went through coercion and must not start.
        _, spec = _classify(
            [1.5, 2.5, 3.5, 4.5], numeric_coercion_threshold=1.01)
        assert spec.type == "integer"
        assert spec.scale_factor == 10


# ---------------------------------------------------------------------------
# A structured non-numeric minority is a category, not missing data
# ---------------------------------------------------------------------------
class TestMultiValuedCodes:
    """The shape that set the default threshold at 0.99.

    A clinical log records treatment as ``"1"`` / ``"2"`` / ``"3"``, with
    combination therapies written ``"2,3"`` and ``"1,2,3"`` — 97.7% of
    values parse as numbers. Coercing would reclassify six patients'
    combination therapy as *missing data*: the values become NaN, and the
    family partitioner files those cases under ``Unknown`` instead of
    giving them their own cell."""

    def _treatment_column(self):
        # 252 single codes + 5 "2,3" + 1 "1,2,3" = 258 values, 0.9767.
        values = [str(i % 4) for i in range(252)]
        return values + ["2,3"] * 5 + ["1,2,3"]

    def test_combination_codes_keep_the_column_categorical(self):
        _, spec = _classify(self._treatment_column(), col="Treatment")
        assert spec.type == "enumeration"
        # ...and the combinations survive as first-class enum values.
        assert set(spec.enum_value_mapping.values()) >= {"2,3", "1,2,3"}

    def test_a_looser_threshold_would_have_coerced_it(self):
        # Pins *why* the default is 0.99 rather than 0.95: at 0.95 this
        # very column types as an integer and the six combination
        # therapies silently become NaN.
        feats, spec = _classify(
            self._treatment_column(), col="Treatment",
            numeric_coercion_threshold=0.95)
        assert spec.type == "integer"
        assert pd.isna(feats["Treatment"].loc["c257"])   # "1,2,3" lost


# ---------------------------------------------------------------------------
# Identifiers name entities, so their digits are labels
# ---------------------------------------------------------------------------
class TestIdentifierColumns:

    def test_org_resource_of_numbers_is_never_numeric(self):
        # High-cardinality: falls to the string path and is dropped,
        # exactly as it did before coercion existed. A guard like
        # `org_resource <= 250` is meaningless.
        values = [str(1000 + i) for i in range(50)]
        with pytest.warns(UserWarning, match="type / cardinality"):
            feats, spec = _classify(values, col="org:resource")
        assert feats is None
        assert spec is None

    def test_low_cardinality_identifier_is_still_an_enumeration(self):
        _, spec = _classify(["10", "20", "30", "10", "20"],
                            col="org:resource")
        assert spec.type == "enumeration"
        assert set(spec.enum_value_mapping.values()) == {"10", "20", "30"}

    def test_case_prefixed_and_mixed_case_identifiers_match(self):
        _, spec = _classify(["10", "20", "30", "10"], col="case:Org:Resource")
        assert spec.type == "enumeration"

    def test_exclusion_governs_only_the_coercion_path(self):
        # A natively-numeric identifier column typed as an integer before
        # this change and still does — the exclusion must not retype it.
        _, spec = _classify([10, 20, 30, 40, 50], col="org:resource")
        assert spec.type == "integer"

    def test_a_non_identifier_column_of_the_same_values_coerces(self):
        _, spec = _classify([str(1000 + i) for i in range(50)], col="Amount")
        assert spec.type == "integer"


# ---------------------------------------------------------------------------
# Precedence against the existing branches
# ---------------------------------------------------------------------------
class TestPrecedence:

    def test_zero_one_strings_are_still_boolean_not_integer(self):
        # The boolean branch runs first and must keep winning
        # (issue #6's behaviour is unchanged).
        _, spec = _classify(["0", "1", "1", "0"], col="Flag")
        assert spec.type == "boolean"

    def test_true_false_strings_are_still_boolean(self):
        _, spec = _classify(["True", "FALSE", "true", "False"], col="Flag")
        assert spec.type == "boolean"

    def test_non_numeric_low_cardinality_strings_still_enumerate(self):
        _, spec = _classify(["gold", "silver", "bronze", "gold"], col="Tier")
        assert spec.type == "enumeration"
        assert set(spec.enum_values) == {"gold", "silver", "bronze"}

    def test_timestamp_strings_are_not_numbers(self):
        _, spec = _classify(
            ["2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01"],
            col="Due")
        assert spec is None or spec.type != "integer"


# ---------------------------------------------------------------------------
# End-to-end data-driven synthesis over a numeric-string column
# ---------------------------------------------------------------------------
class TestDataDrivenEndToEnd:

    def _amount_log(self):
        """Small amounts take branch A, large amounts take branch B."""
        tree = T(operator="->", children=[
            _leaf("Submit"), _xor(_leaf("FastTrack"), _leaf("Review")),
            _leaf("Close"),
        ])
        rows = []
        cases = (
            [(f"s{i}", str(100 + i), ["Submit", "FastTrack", "Close"])
             for i in range(20)]
            + [(f"l{i}", str(9000 + i), ["Submit", "Review", "Close"])
               for i in range(20)]
        )
        for case_id, amount, trace in cases:
            for j, act in enumerate(trace):
                rows.append({
                    "case:concept:name": case_id,
                    "concept:name": act,
                    "time:timestamp": (
                        pd.Timestamp("2026-01-01") + pd.Timedelta(j, "s")),
                    "case:AMOUNT_REQ": amount,
                })
        return tree, pd.DataFrame(rows)

    def _synthesize(self):
        tree, log_df = self._amount_log()
        result = _clustering.cluster(log_df, tree)
        ucm = pm4py_ucm.convert_to_ucm(tree)
        group = _scenarios.synthesize_scenarios(
            ucm, tree, result,
            condition_strategy="data-driven", log=log_df,
            emit_conditions=True,
        )
        return ucm, group

    def test_yields_an_integer_variable_and_threshold_conditions(self):
        pytest.importorskip("sklearn")
        ucm, _ = self._synthesize()
        var = next(v for v in ucm.variables if v.name == "AMOUNT_REQ")
        assert var.type == "integer"
        assert not ucm.enumeration_types      # no 631-value enum

        or_fork = next(
            n for m in ucm.maps for n in m.nodes
            if type(n).__name__ == "OrFork"
            and n.name not in ("LoopFork", "LoopEntryGuard")
        )
        exprs = [a.condition.expression for a in or_fork.succ_connections
                 if a.condition]
        joined = " ".join(exprs)
        # Ordered comparisons, which is the whole point of typing this
        # as a number rather than as an enumeration of 631 literals.
        assert "AMOUNT_REQ <=" in joined or "AMOUNT_REQ >" in joined
        assert "==" not in joined

    def test_scenario_inits_are_plain_integers(self):
        pytest.importorskip("sklearn")
        _, group = self._synthesize()
        inits = [
            init.value
            for sc in group.scenarios for init in sc.initializations
            if init.variable.name == "AMOUNT_REQ"
        ]
        assert inits
        for value in inits:
            int(value)          # parses as an integer literal, unquoted
