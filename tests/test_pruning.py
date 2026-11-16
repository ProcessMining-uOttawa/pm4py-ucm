"""Tests for pruning infrequent paths from a process tree.

The counting is done by the real replay (``compute_traversal_stats``), so
these tests exercise the thresholds on the numbers a user would see. The
trees are the duck-typed ones the traversal suite uses; one test at the
end checks a real PM4Py tree.
"""
from __future__ import annotations

import pytest

from pm4py_ucm.algo import pruning as pr
from pm4py_ucm.algo import traversal as tv


class T:
    def __init__(self, operator=None, label=None, children=None):
        self.operator = operator
        self.label = label
        self.children = children or []


def _seq(*c):
    return T(operator="->", children=list(c))


def _xor(*c):
    return T(operator="X", children=list(c))


def _or(*c):
    return T(operator="O", children=list(c))


def _par(*c):
    return T(operator="+", children=list(c))


def _loop(do, redo):
    return T(operator="*", children=[do, redo])


def _leaf(label):
    return T(label=label)


def _tau():
    return T()


def _show(n) -> str:
    """Compact rendering: ``->(A, X(B, tau))``."""
    if not n.children:
        return "tau" if n.label is None else n.label
    return f"{n.operator}({', '.join(_show(c) for c in n.children)})"


def _prune(tree, log, **kw):
    stats = tv.compute_traversal_stats(tree, log, repair=False)
    assert stats.fitting_cases == len(log), "test log must fit the tree"
    return pr.prune_infrequent_paths(tree, stats, **kw)


# ---------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------

def test_no_threshold_changes_nothing():
    tree = _seq(_leaf("A"), _xor(_leaf("B"), _leaf("C")))
    res = _prune(tree, [["A", "B"]] * 99 + [["A", "C"]])
    assert _show(res.tree) == "->(A, X(B, C))"
    assert not res.changed
    assert res.affected_cases == (0, 0)


def test_result_is_a_copy_and_the_input_is_untouched():
    tree = _seq(_leaf("A"), _xor(_leaf("B"), _leaf("C")))
    before = _show(tree)
    res = _prune(tree, [["A", "B"]] * 9 + [["A", "C"]], min_share=0.2)
    assert _show(tree) == before
    assert res.tree is not tree
    assert res.tree.children[0] is not tree.children[0]


def test_min_share_removes_a_rare_branch():
    tree = _xor(_leaf("A"), _leaf("B"), _leaf("C"))
    log = [["A"]] * 80 + [["B"]] * 17 + [["C"]] * 3
    res = _prune(tree, log, min_share=0.05)
    assert _show(res.tree) == "X(A, B)"
    (b,) = res.removed
    assert (b.kind, b.summary, b.traversals, b.cases) == ("choice", "C", 3, 3)
    assert b.share == pytest.approx(0.03)
    assert b.reason == "min_share"
    assert res.removed_activities == ["C"]
    assert res.affected_cases == (3, 3)


def test_min_cases_removes_a_rare_branch():
    tree = _xor(_leaf("A"), _leaf("B"), _leaf("C"))
    log = [["A"]] * 80 + [["B"]] * 17 + [["C"]] * 3
    res = _prune(tree, log, min_cases=5)
    assert _show(res.tree) == "X(A, B)"
    assert res.removed[0].reason == "min_cases"


def test_either_threshold_is_enough_and_both_are_reported():
    tree = _xor(_leaf("A"), _leaf("B"), _leaf("C"))
    log = [["A"]] * 80 + [["B"]] * 17 + [["C"]] * 3
    res = _prune(tree, log, min_cases=20, min_share=0.05)
    assert _show(res.tree) == "A"
    reasons = {b.summary: b.reason for b in res.removed}
    assert reasons == {"B": "min_cases", "C": "min_cases+min_share"}


def test_a_choice_left_with_one_branch_disappears():
    tree = _seq(_leaf("S"), _xor(_leaf("A"), _leaf("B")), _leaf("E"))
    res = _prune(tree, [["S", "A", "E"]] * 19 + [["S", "B", "E"]],
                 min_share=0.1)
    assert _show(res.tree) == "->(S, A, E)"
    assert res.collapsed_operators == 1


def test_the_most_frequent_branch_always_survives():
    tree = _xor(_leaf("A"), _leaf("B"), _leaf("C"))
    log = [["A"]] * 4 + [["B"]] * 3 + [["C"]] * 3
    res = _prune(tree, log, min_share=0.9)
    assert _show(res.tree) == "A"
    assert len(res.removed) == 2


def test_a_rare_skip_makes_the_activity_mandatory():
    tree = _seq(_leaf("A"), _xor(_leaf("B"), _tau()))
    res = _prune(tree, [["A", "B"]] * 49 + [["A"]], min_share=0.05)
    assert _show(res.tree) == "->(A, B)"
    assert res.removed[0].summary == "(skip)"
    assert res.removed_activities == []


def test_a_rare_optional_activity_vanishes_with_its_choice():
    tree = _seq(_leaf("A"), _xor(_leaf("B"), _tau()), _leaf("C"))
    res = _prune(tree, [["A", "C"]] * 49 + [["A", "B", "C"]], min_share=0.05)
    assert _show(res.tree) == "->(A, C)"


def test_nested_choices_are_judged_as_one_decision():
    # C is 50 % of the inner choice but 5 % of the real decision.
    tree = _xor(_leaf("A"), _xor(_leaf("B"), _leaf("C")))
    log = [["A"]] * 90 + [["B"]] * 5 + [["C"]] * 5
    res = _prune(tree, log, min_share=0.08)
    assert _show(res.tree) == "A"
    assert {b.summary for b in res.removed} == {"B", "C"}
    for b in res.removed:
        assert b.share == pytest.approx(0.05)


def test_nested_choices_are_merged_even_without_a_threshold():
    tree = _xor(_leaf("A"), _xor(_leaf("B"), _leaf("C")))
    res = _prune(tree, [["A"], ["B"], ["C"]])
    assert _show(res.tree) == "X(A, B, C)"
    assert not res.changed


def test_branches_inside_a_removed_branch_are_not_listed():
    tree = _xor(_leaf("A"), _seq(_leaf("B"), _xor(_leaf("C"), _leaf("D"))))
    log = [["A"]] * 98 + [["B", "C"], ["B", "D"]]
    res = _prune(tree, log, min_share=0.05)
    assert _show(res.tree) == "A"
    (b,) = res.removed
    assert b.activities == ["B", "C", "D"]


def test_inclusive_choice_branch_is_removed():
    tree = _or(_leaf("A"), _leaf("B"))
    # The replay parses an inclusive choice one branch at a time, so the
    # log takes a single branch per case.
    log = [["A"]] * 95 + [["B"]] * 5
    res = _prune(tree, log, min_share=0.1)
    assert _show(res.tree) == "A"
    assert res.removed[0].operator == "O"


# ---------------------------------------------------------------------------
# Concurrency: pruned indirectly, through an optional branch
# ---------------------------------------------------------------------------

def test_parallel_branches_are_never_thresholded_directly():
    tree = _par(_leaf("A"), _leaf("B"))
    res = _prune(tree, [["A", "B"], ["B", "A"]], min_share=0.99, min_cases=99)
    assert _show(res.tree) == "+(A, B)"


def test_a_rare_optional_parallel_branch_removes_the_fork():
    # The situation the feature exists for: an AND-fork whose second
    # branch is almost always skipped.
    tree = _seq(_leaf("S"),
                _par(_leaf("A"), _xor(_leaf("B"), _tau())),
                _leaf("E"))
    log = [["S", "A", "E"]] * 97 + [["S", "A", "B", "E"]] * 2 \
        + [["S", "B", "A", "E"]]
    res = _prune(tree, log, min_share=0.05)
    assert _show(res.tree) == "->(S, A, E)"
    assert res.removed_activities == ["B"]
    assert res.affected_cases == (3, 3)


def test_a_parallel_block_keeps_its_other_branches():
    tree = _par(_leaf("A"), _leaf("B"), _xor(_leaf("C"), _tau()))
    log = [["A", "B"]] * 60 + [["B", "A"]] * 39 + [["A", "B", "C"]]
    res = _prune(tree, log, min_share=0.05)
    assert _show(res.tree) == "+(A, B)"


def test_a_block_reduced_to_nothing_becomes_silent():
    tree = _seq(_leaf("A"),
                _xor(_tau(), _par(_xor(_leaf("B"), _tau()),
                                  _xor(_leaf("C"), _tau()))))
    # The parallel block is entered often, but B and C are both rare.
    log = [["A"]] * 96 + [["A", "B"]] * 2 + [["A", "C"]] * 2
    res = _prune(tree, log, min_cases=5)
    assert _show(res.tree) == "A"


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------

def test_a_rare_redo_turns_the_loop_into_its_body():
    tree = _seq(_leaf("S"), _loop(_leaf("A"), _leaf("R")))
    log = [["S", "A"]] * 98 + [["S", "A", "R", "A"]] * 2
    res = _prune(tree, log, min_share=0.05)
    assert _show(res.tree) == "->(S, A)"
    (b,) = res.removed
    assert b.kind == "loop"
    # 2 redos over 102 body executions.
    assert b.share == pytest.approx(2 / 102)
    assert b.cases == 2


def test_a_frequent_redo_is_kept():
    tree = _loop(_leaf("A"), _leaf("R"))
    res = _prune(tree, [["A"]] * 5 + [["A", "R", "A"]] * 5, min_share=0.05)
    assert _show(res.tree) == "*(A, R)"


def test_loops_can_be_left_alone():
    tree = _loop(_leaf("A"), _leaf("R"))
    log = [["A"]] * 98 + [["A", "R", "A"]] * 2
    res = _prune(tree, log, min_share=0.05, prune_loops=False)
    assert _show(res.tree) == "*(A, R)"


def test_a_rare_silent_redo_removes_a_self_loop():
    tree = _loop(_leaf("A"), _tau())
    res = _prune(tree, [["A"]] * 99 + [["A", "A"]], min_share=0.05)
    assert _show(res.tree) == "A"


def test_min_cases_counts_cases_not_iterations():
    # One case repeats 10 times: 10 traversals of the redo, but 1 case.
    tree = _loop(_leaf("A"), _leaf("R"))
    log = [["A"]] * 20 + [["A"] + ["R", "A"] * 10]
    res = _prune(tree, log, min_cases=2)
    assert _show(res.tree) == "A"
    assert (res.removed[0].traversals, res.removed[0].cases) == (10, 1)


# ---------------------------------------------------------------------------
# Global properties
# ---------------------------------------------------------------------------

def test_raising_a_threshold_only_removes_more():
    tree = _seq(
        _leaf("S"),
        _xor(_leaf("A"), _leaf("B"), _seq(_leaf("C"),
                                          _xor(_leaf("D"), _tau()))),
        _par(_leaf("E"), _xor(_leaf("F"), _tau())),
        _loop(_leaf("G"), _leaf("R")),
    )
    log = ([["S", "A", "E", "G"]] * 60 + [["S", "B", "E", "F", "G"]] * 25
           + [["S", "C", "D", "F", "E", "G"]] * 8
           + [["S", "C", "E", "G", "R", "G"]] * 5
           + [["S", "B", "E", "G", "R", "G", "R", "G"]] * 2)
    stats = tv.compute_traversal_stats(tree, log, repair=False)
    assert stats.fitting_cases == len(log)

    def acts(n):
        return ({n.label} - {None} if not n.children
                else set().union(*(acts(c) for c in n.children)))

    previous = acts(tree)
    for share in (0.0, 0.03, 0.06, 0.1, 0.2, 0.4, 0.8, 1.0):
        res = pr.prune_infrequent_paths(tree, stats, min_share=share)
        now = acts(res.tree)
        assert now <= previous, share
        previous = now
    assert previous == {"S", "A", "E", "G"}


def test_the_frequent_cases_still_fit_the_pruned_tree():
    tree = _seq(_leaf("S"), _xor(_leaf("A"), _leaf("B")),
                _par(_leaf("E"), _xor(_leaf("F"), _tau())))
    log = ([["S", "A", "E"]] * 90 + [["S", "B", "E"]] * 4
           + [["S", "A", "F", "E"]] * 6)
    res = _prune(tree, log, min_share=0.05)
    assert _show(res.tree) == "->(S, A, +(E, X(F, tau)))"
    after = tv.compute_traversal_stats(res.tree, log, repair=False)
    assert after.fitting_cases == 96
    low, high = res.affected_cases
    assert low <= len(log) - after.fitting_cases <= high


def test_bad_thresholds_are_rejected():
    tree = _xor(_leaf("A"), _leaf("B"))
    stats = tv.compute_traversal_stats(tree, [["A"], ["B"]], repair=False)
    with pytest.raises(ValueError):
        pr.prune_infrequent_paths(tree, stats, min_share=5)
    with pytest.raises(ValueError):
        pr.prune_infrequent_paths(tree, stats, min_cases=-1)


# ---------------------------------------------------------------------------
# A real PM4Py tree
# ---------------------------------------------------------------------------

def test_pm4py_tree_round_trip_and_conversion():
    pytest.importorskip("pm4py")
    from pm4py.objects.process_tree.obj import Operator, ProcessTree
    from pm4py_ucm.objects.ucm.conversion import from_process_tree

    def node(op=None, label=None, children=()):
        n = ProcessTree(operator=op, label=label)
        for c in children:
            c.parent = n
        n.children = list(children)
        return n

    tree = node(Operator.SEQUENCE, children=[
        node(label="S"),
        node(Operator.PARALLEL, children=[
            node(label="A"),
            node(Operator.XOR, children=[node(label="B"), node()]),
        ]),
        node(label="E"),
    ])
    log = [["S", "A", "E"]] * 97 + [["S", "A", "B", "E"]] * 3
    res = _prune(tree, log, min_share=0.05)

    assert isinstance(res.tree, ProcessTree)
    assert res.tree.operator is Operator.SEQUENCE
    assert [c.label for c in res.tree.children] == ["S", "A", "E"]
    assert all(c.parent is res.tree for c in res.tree.children)
    assert res.tree.parent is None

    # The pruned tree converts like any other, with no fork left.
    ucm = from_process_tree.apply(res.tree)
    kinds = {type(n).__name__ for n in ucm.get_path_nodes()}
    assert kinds, "the conversion produced no path nodes"
    assert not any("Fork" in k or "Join" in k for k in kinds), kinds


# ---------------------------------------------------------------------------
# The affected cases: counted exactly, and optionally dropped
# ---------------------------------------------------------------------------

def test_affected_cases_are_exact_when_a_case_walks_two_removed_branches():
    tree = _seq(_xor(_leaf("B"), _tau()), _leaf("M"),
                _xor(_leaf("C"), _tau()))
    # 3 cases take B, 3 take C, and 2 of them take both: 4 distinct cases.
    log = ([["M"]] * 96 + [["B", "M", "C"]] * 2 + [["B", "M"]]
           + [["M", "C"]])
    res = _prune(tree, log, min_share=0.05)
    assert _show(res.tree) == "M"
    assert sum(b.cases for b in res.removed) == 6
    assert res.affected_cases == (4, 4)


def test_dropping_the_affected_cases_from_a_list_of_traces():
    tree = _seq(_leaf("S"), _xor(_leaf("A"), _leaf("B")))
    log = [("k%d" % i, ["S", "A"]) for i in range(19)] + [("rare", ["S", "B"])]
    stats = tv.compute_traversal_stats(tree, log, repair=False)
    res = pr.prune_infrequent_paths(tree, stats, min_share=0.1)
    assert res.dropped_case_ids(log) == ["rare"]
    kept = res.filter_log(log)
    assert len(kept) == 19 and all(cid != "rare" for cid, _ in kept)
    # What is left fits the pruned tree exactly, so its counts conserve.
    after = tv.compute_traversal_stats(res.tree, kept, repair=False)
    assert after.fitting_cases == after.total_cases == 19


def test_nothing_removed_returns_the_log_itself():
    tree = _xor(_leaf("A"), _leaf("B"))
    log = [["A"], ["B"]]
    stats = tv.compute_traversal_stats(tree, log, repair=False)
    res = pr.prune_infrequent_paths(tree, stats, min_share=0.1)
    assert res.filter_log(log) is log
    assert res.affected_cases == (0, 0)


def test_hand_built_counts_give_bounds_and_cannot_drop():
    tree = _xor(_leaf("A"), _leaf("B"))
    stats = tv.TraversalStats(
        node_counts={0: 100, 1: 97, 2: 3}, node_cases={0: 100, 1: 97, 2: 3},
        branch_counts={0: {0: 97, 1: 3}}, total_cases=100, fitting_cases=100)
    res = pr.prune_infrequent_paths(tree, stats, min_share=0.05)
    assert _show(res.tree) == "A"
    assert res.affected_cases == (3, 3)      # one branch: the bounds meet
    assert res.affected_sequences is None
    with pytest.raises(ValueError):
        res.dropped_case_ids([["A"]])


def test_api_keeps_or_drops_the_cases_of_a_dataframe():
    pd = pytest.importorskip("pandas")
    pm4py = pytest.importorskip("pm4py")
    import pm4py_ucm

    rows, t = [], pd.Timestamp("2024-01-01", tz="UTC")
    for n, trace in enumerate([["a", "b", "d"]] * 48 + [["a", "c", "d"]] * 2):
        for step in trace:
            t += pd.Timedelta(minutes=5)
            rows.append({"case:concept:name": f"c{n}", "concept:name": step,
                         "time:timestamp": t})
    df = pd.DataFrame(rows)
    tree = pm4py.discover_process_tree_inductive(df, noise_threshold=0.0)

    keep = pm4py_ucm.prune_infrequent_paths(tree, df, min_share=0.1)
    assert keep.log is df
    assert keep.affected_cases == (2, 2)

    drop = pm4py_ucm.prune_infrequent_paths(tree, df, min_share=0.1,
                                            drop_cases=True)
    assert drop.log["case:concept:name"].nunique() == 48
    assert set(drop.log["concept:name"]) == {"a", "b", "d"}
    after = pm4py_ucm.compute_traversal_stats(drop.tree, drop.log)
    assert after.fitting_ratio == 1.0

    with pytest.raises(ValueError):
        pm4py_ucm.prune_infrequent_paths(
            tree, stats=pm4py_ucm.compute_traversal_stats(tree, df),
            min_share=0.1, drop_cases=True)


# ---------------------------------------------------------------------------
# Families and split models: each cell pruned from its own cases
# ---------------------------------------------------------------------------

def _family_log():
    pd = pytest.importorskip("pandas")
    pytest.importorskip("pm4py")
    rows, t = [], pd.Timestamp("2024-01-01", tz="UTC")
    traces = ([("north", ["a", "b", "d"])] * 38 + [("north", ["a", "c", "d"])] * 2
              + [("south", ["a", "c", "d"])] * 20 + [("south", ["a", "b", "d"])] * 20)
    for n, (region, trace) in enumerate(traces):
        for step in trace:
            t += pd.Timedelta(minutes=5)
            rows.append({"case:concept:name": f"c{n}", "concept:name": step,
                         "time:timestamp": t, "region": region})
    return pd.DataFrame(rows)


def _acts(ucm):
    return {r.name for r in ucm.responsibilities}


def test_family_cells_are_pruned_from_their_own_cases():
    import pm4py_ucm
    df = _family_log()
    plain = pm4py_ucm.discover_ucm_family(df, ["region"], min_cases=1)
    assert all(c.prune is None for c in plain.cells)

    fam = pm4py_ucm.discover_ucm_family(
        df, ["region"], min_cases=1, prune={"min_share": 0.1})
    by = {c.label: c for c in fam.cells}
    # c is 5 % of north's choice and half of south's: only north loses it.
    assert _acts(by["north"].ucm) == {"a", "b", "d"}
    assert _acts(by["south"].ucm) == {"a", "b", "c", "d"}
    assert by["north"].prune.removed_activities == ["c"]
    assert not by["south"].prune.changed
    # Kept by default: the cells and the family log are whole.
    assert (by["north"].n_cases, by["north"].pruned_cases) == (40, 0)
    assert fam.pruned_cases == 0
    assert fam.log_df["case:concept:name"].nunique() == 80


def test_family_can_drop_the_cases_on_removed_paths():
    import pm4py_ucm
    df = _family_log()
    fam = pm4py_ucm.discover_ucm_family(
        df, ["region"], min_cases=1,
        prune={"min_share": 0.1, "drop_cases": True})
    by = {c.label: c for c in fam.cells}
    assert (by["north"].n_cases, by["north"].pruned_cases) == (38, 2)
    assert len(by["north"].case_ids) == 38
    assert (by["south"].n_cases, by["south"].pruned_cases) == (40, 0)
    assert fam.pruned_cases == 2
    assert fam.total_cases == 80
    assert fam.log_df["case:concept:name"].nunique() == 78
    # The statistics follow the cells.
    stats = pm4py_ucm.compute_family_stats(fam)
    assert stats is not None


def test_split_model_is_pruned_per_value():
    import pm4py_ucm
    from pm4py_ucm.objects.ucm.obj import UCM
    df = _family_log()
    fit = {}
    ucm, fam = pm4py_ucm.discover_ucm_by_attribute(
        df, "region", decomposition="off",
        node_metrics=["traversal_frequency"],
        edge_metrics=["traversal_frequency"],
        prune={"min_share": 0.1, "drop_cases": True},
        return_family=True, fit=fit)
    by = {c.label: c for c in fam.cells}
    assert by["north"].prune.removed_activities == ["c"]
    # Each stub carries its value's remaining cases, and they all fit.
    counts = {s.name: {m.name: m.value for m in s._metadata}
                      ["perf_branch0_traversal_frequency"]
              for s in ucm.maps[0].nodes if isinstance(s, UCM.Stub)}
    assert counts == {"north": "38", "south": "40"}
    assert fit["fitting_cases"] == fit["total_cases"] == 78
