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

def test_parallel_projection_does_not_reuse_the_outer_traces_memo():
    """A parallel block replays each child against a PROJECTION of the
    window — a fresh list whose positions are their own coordinate
    space. The parse memo is keyed on (subtree, start, end) with no list
    identity, so sharing it across that boundary would let the parse of
    one window answer for a different one.

    Here the parallel block is entered twice inside a loop, once with a
    'P' and once with a 'Q'. Cross-contaminated memo entries make the
    second entry reuse the first's parse, reporting P twice and Q never.

    The projections must have the same LENGTH but different content for
    the keys to collide, so the contaminated branch is a choice between
    two activities: entry one projects one P, entry two one Q, and both
    ask the memo for the same one-event window of the same subtree.
    """
    # *( ->( S, +( X(tau, X(P, Q)), X(tau, Z) ) ), R )
    p_or_q = _xor(_leaf("P"), _leaf("Q"))
    left = _xor(_tau(), p_or_q)
    right = _xor(_tau(), _leaf("Z"))
    tree = _loop(_seq(_leaf("S"), _par(left, right)), _leaf("R"))

    ids = cs.assign_node_ids(tree)
    xor_counts = {}
    trace = ["S", "P", "R", "S", "Q"]
    sig = cs.replay(tree, trace, node_ids=ids, xor_branch_counts=xor_counts)
    assert sig != cs.NOFIT, "the trace does fit this tree"

    taken = xor_counts.get(ids[id(p_or_q)], {})
    assert (taken.get(0, 0), taken.get(1, 0)) == (1, 1), (
        "the trace has one P and one Q, so each branch of X(P, Q) ran "
        f"exactly once; got P={taken.get(0, 0)} Q={taken.get(1, 0)}"
    )


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


def _expr(tree, trace):
    node_ids = cs.assign_node_ids(tree)
    sig = cs.replay(tree, trace, node_ids=node_ids)
    return cs.partial_order_expression(sig, tree, node_ids)


class TestExpressionAesthetics:
    """Display-only rendering of the partial-order expression (no effect
    on clustering / signatures)."""

    def test_loop_once_uses_caret_one_without_parens(self):
        tree = _seq(_leaf("X"), _loop(_leaf("A"), _leaf("R")))
        expr = _expr(tree, ["X", "A"])          # one iteration
        assert "A^1" in expr
        assert "(A)" not in expr

    def test_loop_two_or_more_uses_caret_ge2_without_parens(self):
        tree = _seq(_leaf("X"), _loop(_leaf("A"), _leaf("R")))
        expr = _expr(tree, ["X", "A", "R", "A"])  # two iterations
        assert "A^>=2" in expr
        assert "(A)" not in expr

    def test_single_activity_parallel_drops_parens(self):
        # +(A, tau) with only A executed -> just "A" (A || tau ≡ A).
        tree = _seq(_leaf("X"), _par(_leaf("A"), _tau()))
        expr = _expr(tree, ["X", "A"])
        assert expr == "X -> A"

    def test_multi_branch_parallel_keeps_parens(self):
        tree = _seq(_leaf("X"), _par(_leaf("Y"), _leaf("Z")))
        expr = _expr(tree, ["X", "Y", "Z"])
        assert "(Y || Z)" in expr

    def test_single_compound_parallel_keeps_wrapper(self):
        # A skipped-optional parallel whose surviving branch is a
        # SEQUENCE keeps its parens so -> / || precedence stays clear.
        tree = _seq(
            _leaf("X"),
            _par(_seq(_leaf("A"), _leaf("B")), _tau()),
        )
        expr = _expr(tree, ["X", "A", "B"])
        assert "(A -> B)" in expr

    def test_taken_choice_keeps_brackets(self):
        # [A] still marks the taken XOR branch.
        tree = _seq(_leaf("X"), _xor(_leaf("A"), _leaf("B")))
        expr = _expr(tree, ["X", "A"])
        assert "[A]" in expr


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


# ---------------------------------------------------------------------------
# Termination — the budget must bound backtracking (regression)
# ---------------------------------------------------------------------------

def test_replay_is_bounded_on_pathological_backtracking():
    """A long trace on an alphabet-overlapping loop nest must terminate.

    Regression for the Family-statistics freeze: the sequence/loop backtrackers
    (``_seq_split_first`` / ``_loop_continue``) revisit the same memoised
    ``(tree, s, e)`` subproblem exponentially often while exploring peel
    splits. Memo hits used to skip the ``max_replay_states`` charge, so the
    budget — the sole termination guarantee — never depleted and replay spun
    forever on a 200+-event trace. The budget now charges every replay entry,
    so a trace that cannot be parsed within it is reported NOFIT promptly.

    Runs replay in a worker thread with a generous join timeout: with the fix
    it finishes in well under a second; a regression makes the thread outlive
    the timeout (the test fails after ~20 s rather than hanging the suite).
    """
    import threading

    # Nested single-letter loops accept any tiling of A's; the trailing Z can
    # never match, forcing the parser to exhaust every tiling before conceding.
    tree = _loop(_loop(_leaf("A"), _leaf("A")), _loop(_leaf("A"), _leaf("A")))
    trace = ["A"] * 40 + ["Z"]

    result: dict = {}

    def run() -> None:
        result["sig"] = cs.replay(tree, trace, max_replay_states=50_000)

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout=20)
    assert not th.is_alive(), (
        "replay did not terminate within 20 s — the max_replay_states budget "
        "is not bounding the sequence/loop backtracking (memo hits must be "
        "charged against it)"
    )
    assert result["sig"] == cs.NOFIT
