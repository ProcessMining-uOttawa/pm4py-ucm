"""Unit tests for the boolean simplifier used on data-driven OR-fork
conditions."""
from pm4py_ucm.algo.discovery.scenarios import expression_minimizer as m


# ---------------------------------------------------------------------------
# Single-rule cases (each tests one absorption)
# ---------------------------------------------------------------------------

def test_complement_absorption_integer():
    """``(A == X && B > 100) || (A == X && B <= 100)`` -> ``A == X``."""
    src = "(A == X && B > 100) || (A == X && B <= 100)"
    assert m.minimize(src) == "A == X"


def test_complement_absorption_enum():
    """``(A == X && B == Y) || (A == X && B != Y)`` -> ``A == X``."""
    src = "(A == X && B == Y) || (A == X && B != Y)"
    assert m.minimize(src) == "A == X"


def test_subsumption():
    """``A || (A && B)`` -> ``A`` (the larger conjunction is
    redundant once a strict subset already covers everything)."""
    src = "A == X || (A == X && B > 1000)"
    assert m.minimize(src) == "A == X"


def test_strict_bound_within_clause():
    """``B > 100 && B > 50`` -> ``B > 100`` (the looser lower bound
    is implied by the stricter one). Symmetric on ``<=``."""
    src = "B > 100 && B > 50 && B <= 5000 && B <= 1000000"
    out = m.minimize(src)
    assert "B > 100" in out
    assert "B > 50" not in out
    assert "B <= 5000" in out
    assert "B <= 1000000" not in out


def test_dedup():
    """``A || A`` -> ``A``."""
    assert m.minimize("A == X || A == X") == "A == X"


# ---------------------------------------------------------------------------
# Compound simplifications (multiple rules cooperating)
# ---------------------------------------------------------------------------

def test_quad_collapse_to_tautology():
    """Four clauses partitioning two binary features fully tile
    ``true``: ``(A && B) || (A && !B) || (!A && B) || (!A && !B)``."""
    src = (
        "(A == X && B > 1) || (A == X && B <= 1) "
        "|| (A != X && B > 1) || (A != X && B <= 1)"
    )
    assert m.minimize(src) == "true"


def test_partial_collapse():
    """``(A && B) || (A && !B) || (C && D)`` -> ``A || (C && D)`` —
    the first two collapse via complement absorption, the third
    survives."""
    src = (
        "(A == X && B > 100) || (A == X && B <= 100) "
        "|| (C != Y && D == Z)"
    )
    out = m.minimize(src)
    # Order isn't guaranteed; check structural content.
    assert "A == X" in out
    assert "(C != Y && D == Z)" in out
    assert "||" in out


# ---------------------------------------------------------------------------
# Degenerate inputs
# ---------------------------------------------------------------------------

def test_passthrough_true():
    assert m.minimize("true") == "true"


def test_passthrough_false():
    assert m.minimize("false") == "false"


def test_passthrough_unparseable():
    """Expressions outside the supported grammar are returned
    unchanged — the minimiser must never silently mangle conditions
    it doesn't fully understand."""
    weird = "some_func(x) > 5 && other.thing"
    assert m.minimize(weird) == weird


def test_empty_string():
    assert m.minimize("") == ""


# ---------------------------------------------------------------------------
# Decision-mining-shape regression
# ---------------------------------------------------------------------------

def test_decision_tree_complement_pair_simplifies():
    """The pattern the decision tree emits at any leaf split: two
    sibling leaves share a parent's path condition and differ only
    on the leaf-level comparison. The minimiser must collapse them."""
    # Synthetic shape from the ClaimsPaymentLog mined output.
    src = (
        "(Broker != NFP_Corp && Claim_Value > 1070805) "
        "|| (Broker != NFP_Corp && Claim_Value <= 1070805) "
        "|| (Broker == NFP_Corp && Product_Group == Motor)"
    )
    out = m.minimize(src)
    assert "Broker != NFP_Corp" in out
    # The two complement-pair clauses must have collapsed —
    # ``Claim_Value`` shouldn't appear in the first conjunct.
    assert "Claim_Value" not in out.split("||")[0]
