"""Smoke tests for the concurrency-aware choice signature.

The headline correctness property: two traces that interleave the same
parallel-branch activities should produce the *same* signature. Two
traces that take different XOR branches should produce *different*
signatures."""
from __future__ import annotations

from pm4py_ucm.algo.discovery.variants import choice_signature as cs


# Minimal duck-typed process tree node used throughout the suite.
# Mirrors the surface PM4Py's ProcessTree exposes (operator/label/children).
class T:
    def __init__(self, operator=None, label=None, children=None):
        self.operator = operator
        self.label = label
        self.children = children or []


def _seq(*children):
    return T(operator="->", children=list(children))


def _xor(*children):
    return T(operator="X", children=list(children))


def _par(*children):
    return T(operator="+", children=list(children))


def _loop(do, redo):
    return T(operator="*", children=[do, redo])


def _leaf(label):
    return T(label=label)


def _tau():
    return T()


# ---------------------------------------------------------------------------
# Sequence + parallel — the cornerstone case
# ---------------------------------------------------------------------------

def test_parallel_interleavings_share_signature():
    # X -> (Y || Z)
    tree = _seq(_leaf("X"), _par(_leaf("Y"), _leaf("Z")))
    sig_xyz = cs.replay(tree, ["X", "Y", "Z"])
    sig_xzy = cs.replay(tree, ["X", "Z", "Y"])
    assert sig_xyz != cs.NOFIT
    assert sig_xzy != cs.NOFIT
    assert sig_xyz == sig_xzy, (
        f"interleavings of (Y || Z) must collapse to a single signature; "
        f"got {sig_xyz!r} vs {sig_xzy!r}"
    )


def test_different_xor_branches_differ():
    # X -> (Y || Z) -> (A x B)
    tree = _seq(
        _leaf("X"),
        _par(_leaf("Y"), _leaf("Z")),
        _xor(_leaf("A"), _leaf("B")),
    )
    sig_yza = cs.replay(tree, ["X", "Y", "Z", "A"])
    sig_zyb = cs.replay(tree, ["X", "Z", "Y", "B"])
    assert sig_yza != cs.NOFIT
    assert sig_zyb != cs.NOFIT
    assert sig_yza != sig_zyb, "XOR choice difference must distinguish signatures"


def test_nofit_on_unknown_activity():
    tree = _seq(_leaf("X"), _leaf("Y"))
    assert cs.replay(tree, ["X", "Z"]) == cs.NOFIT


def test_nofit_on_missing_activity():
    tree = _seq(_leaf("X"), _leaf("Y"))
    assert cs.replay(tree, ["X"]) == cs.NOFIT


def test_tau_leaves_consume_nothing():
    # A -> tau -> B  --  same signature as A -> B (tau is a no-op).
    tree = _seq(_leaf("A"), _tau(), _leaf("B"))
    sig = cs.replay(tree, ["A", "B"])
    assert sig != cs.NOFIT


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------

def test_loop_zero_or_more_iterations_coarsened():
    # *(A, B): A then optional (B, A)+
    tree = _loop(_leaf("A"), _leaf("B"))
    # One iteration: just A. Coarsened to LOOP=1.
    sig_one = cs.replay(tree, ["A"])
    # Two iterations: A, B, A. Coarsened to LOOP=2 (">=2").
    sig_two = cs.replay(tree, ["A", "B", "A"])
    # Three iterations: A, B, A, B, A. Also LOOP=2.
    sig_three = cs.replay(tree, ["A", "B", "A", "B", "A"])
    assert sig_one != cs.NOFIT
    assert sig_two != cs.NOFIT
    assert sig_three != cs.NOFIT
    assert sig_one != sig_two
    assert sig_two == sig_three, (
        "coarsened loops must collapse iteration counts >= 2"
    )


def test_loop_fine_grained_distinguishes_iteration_counts():
    tree = _loop(_leaf("A"), _leaf("B"))
    sig_two = cs.replay(tree, ["A", "B", "A"], coarsen_loops=False)
    sig_three = cs.replay(tree, ["A", "B", "A", "B", "A"], coarsen_loops=False)
    assert sig_two != sig_three


# ---------------------------------------------------------------------------
# Linearization count and partial-order expression
# ---------------------------------------------------------------------------

def test_linearization_count_sequence_is_one():
    # No parallelism — single ordering.
    tree = _seq(_leaf("X"), _leaf("Y"), _leaf("Z"))
    sig = cs.replay(tree, ["X", "Y", "Z"])
    assert cs.linearization_count(sig) == 1


def test_linearization_count_parallel_two_singletons_is_two():
    # X -> (Y || Z): two valid orderings — XYZ and XZY.
    tree = _seq(_leaf("X"), _par(_leaf("Y"), _leaf("Z")))
    sig = cs.replay(tree, ["X", "Y", "Z"])
    assert cs.linearization_count(sig) == 2


def test_linearization_count_parallel_three_singletons_is_six():
    tree = _par(_leaf("A"), _leaf("B"), _leaf("C"))
    sig = cs.replay(tree, ["A", "B", "C"])
    assert cs.linearization_count(sig) == 6  # 3! / (1! 1! 1!)


def test_partial_order_expression_renders_human_readable():
    tree = _seq(_leaf("X"), _par(_leaf("Y"), _leaf("Z")))
    node_ids = cs.assign_node_ids(tree)
    sig = cs.replay(tree, ["X", "Y", "Z"], node_ids=node_ids)
    expr = cs.partial_order_expression(sig, tree, node_ids)
    assert "X" in expr
    assert "Y" in expr
    assert "Z" in expr
    assert "||" in expr or "->" in expr  # at least one operator surfaces


# ---------------------------------------------------------------------------
# Collect XOR choices — the bridge to OR-fork condition emission
# ---------------------------------------------------------------------------

def test_collect_xor_choices_returns_branch_indices():
    # X -> (A x B)
    xor_node = _xor(_leaf("A"), _leaf("B"))
    tree = _seq(_leaf("X"), xor_node)
    node_ids = cs.assign_node_ids(tree)
    sig_a = cs.replay(tree, ["X", "A"], node_ids=node_ids)
    sig_b = cs.replay(tree, ["X", "B"], node_ids=node_ids)
    choices_a = cs.collect_xor_choices(sig_a)
    choices_b = cs.collect_xor_choices(sig_b)
    xor_id = node_ids[id(xor_node)]
    assert choices_a[xor_id] == 0
    assert choices_b[xor_id] == 1
