"""Tests for replay-based traversal counting.

The headline property is **conservation**: an activity's count equals the
count on its own incoming and outgoing edges, the branches of a parallel
fork all carry the fork's inflow, and the branches of a choice sum to it.
Event counts and directly-follows counts have none of these properties on
a model with concurrency, which is what the traversal metrics exist to
fix — so most tests here assert an equality that the old measures break.

The counting core is duck-typed on the process tree and needs no pm4py;
only the alignment-repair path does.
"""
from __future__ import annotations

import pytest

from pm4py_ucm.algo import traversal as tv
from pm4py_ucm.algo.discovery.variants import choice_signature as cs


# Same minimal duck-typed tree the choice-signature suite uses.
class T:
    def __init__(self, operator=None, label=None, children=None):
        self.operator = operator
        self.label = label
        self.children = children or []


def _seq(*c):
    return T(operator="->", children=list(c))


def _xor(*c):
    return T(operator="X", children=list(c))


def _par(*c):
    return T(operator="+", children=list(c))


def _loop(do, redo):
    return T(operator="*", children=[do, redo])


def _leaf(label):
    return T(label=label)


def _tau():
    return T()


def _counts_by_label(tree, stats):
    """``{activity: traversals}`` for the tree's labelled leaves."""
    ids = cs.assign_node_ids(tree)
    out = {}

    def walk(n):
        if n.label is not None:
            out[n.label] = stats.node_counts.get(ids[id(n)], 0)
        for c in n.children:
            walk(c)

    walk(tree)
    return out


# ---------------------------------------------------------------------------
# The core property: counts conserve
# ---------------------------------------------------------------------------

def test_sequence_counts_every_case_once():
    tree = _seq(_leaf("A"), _leaf("B"))
    stats = tv.compute_traversal_stats(tree, [["A", "B"]] * 7, repair=False)
    assert stats.total_cases == 7
    assert stats.fitting_cases == 7
    assert _counts_by_label(tree, stats) == {"A": 7, "B": 7}


def test_parallel_branches_all_carry_the_forks_inflow():
    # The case a directly-follows count gets wrong: every case runs BOTH
    # branches, but only one of them is ever adjacent to what preceded
    # the fork, so DF counts split the inflow between the branches.
    tree = _seq(_leaf("A"), _par(_leaf("B"), _leaf("C")))
    log = [["A", "B", "C"]] * 6 + [["A", "C", "B"]] * 4
    stats = tv.compute_traversal_stats(tree, log, repair=False)
    counts = _counts_by_label(tree, stats)
    assert counts == {"A": 10, "B": 10, "C": 10}


def test_interleavings_of_a_parallel_block_count_identically():
    # Two traces that differ only in the order of concurrent activities
    # replay to the same signature, so they must contribute identical
    # counts — this is what lets the counts be keyed on the signature.
    tree = _par(_leaf("B"), _leaf("C"))
    one = tv.compute_traversal_stats(tree, [["B", "C"]], repair=False)
    other = tv.compute_traversal_stats(tree, [["C", "B"]], repair=False)
    assert one.node_counts == other.node_counts
    assert one.n_signatures == other.n_signatures == 1


def test_choice_branches_sum_to_the_choice():
    tree = _xor(_leaf("A"), _leaf("B"))
    log = [["A"]] * 3 + [["B"]] * 5
    stats = tv.compute_traversal_stats(tree, log, repair=False)
    ids = cs.assign_node_ids(tree)
    choice_id = ids[id(tree)]
    branches = stats.branch_counts[choice_id]
    assert branches == {0: 3, 1: 5}
    assert sum(branches.values()) == stats.node_counts[choice_id] == 8


def test_a_silently_skipped_branch_is_counted():
    # The reported "100 %" bug: the alternative to B is a tau, which
    # produces no event and therefore no directly-follows pair at all, so
    # the skip used to be invisible and B looked unanimous.
    tree = _seq(_leaf("A"), _xor(_tau(), _leaf("B")), _leaf("C"))
    log = [["A", "B", "C"]] * 2 + [["A", "C"]] * 8
    stats = tv.compute_traversal_stats(tree, log, repair=False)
    ids = cs.assign_node_ids(tree)
    choice = tree.children[1]
    branches = stats.branch_counts[ids[id(choice)]]
    assert branches[0] == 8, "the silent skip must be counted"
    assert branches[1] == 2
    assert sum(branches.values()) == 10


def test_loop_counts_do_per_iteration_and_redo_once_less():
    tree = _loop(_leaf("A"), _leaf("R"))
    # A, A R A and A R A R A -> 1 + 2 + 3 = 6 do's, 0 + 1 + 2 = 3 redo's.
    log = [["A"], ["A", "R", "A"], ["A", "R", "A", "R", "A"]]
    stats = tv.compute_traversal_stats(tree, log, repair=False)
    assert _counts_by_label(tree, stats) == {"A": 6, "R": 3}


def test_nested_loop_body_counts_every_iteration():
    """A loop entered several times runs a different number of body
    iterations each time, so its executions are the TOTAL over visits —
    not the per-visit maximum multiplied by the visit count, which is
    what a max-based iteration record would give."""
    # outer: *( ->(S, inner), Z ) with inner: *(A, B)
    inner = _loop(_leaf("A"), _leaf("B"))
    tree = _loop(_seq(_leaf("S"), inner), _leaf("Z"))

    # One case, two outer iterations:
    #   S A B A B A   (inner runs 3 times)  Z  S A   (inner runs 1)
    # inner do  A: 3 + 1 = 4        (a max would say 2 visits x 3 = 6)
    # inner redo B: 2 + 0 = 2       (a max would say 2 x 2 = 4)
    trace = ["S", "A", "B", "A", "B", "A", "Z", "S", "A"]
    stats = tv.compute_traversal_stats(tree, [trace], repair=False)
    assert stats.fitting_cases == 1, "the trace must fit for this to mean anything"
    counts = _counts_by_label(tree, stats)
    assert counts["S"] == 2, "outer body runs twice"
    assert counts["Z"] == 1, "outer redo runs once"
    assert counts["A"] == 4
    assert counts["B"] == 2
    # And the events actually observed agree with the counts.
    assert counts["A"] == trace.count("A")
    assert counts["B"] == trace.count("B")
    assert counts["S"] == trace.count("S")
    assert counts["Z"] == trace.count("Z")


def test_nested_loop_counts_conserve_against_the_log():
    """Across a mixed log, every activity's traversal count equals the
    number of times it was actually observed — the strongest available
    check that nesting is handled exactly."""
    inner = _loop(_leaf("A"), _leaf("B"))
    tree = _loop(_seq(_leaf("S"), inner), _leaf("Z"))
    log = [
        ["S", "A"],
        ["S", "A", "B", "A", "Z", "S", "A", "B", "A", "B", "A"],
        ["S", "A", "B", "A", "Z", "S", "A"],
        ["S", "A", "B", "A", "B", "A", "B", "A"],
    ]
    stats = tv.compute_traversal_stats(tree, log, repair=False)
    assert stats.fitting_cases == len(log)
    counts = _counts_by_label(tree, stats)
    for activity in ("S", "A", "B", "Z"):
        observed = sum(t.count(activity) for t in log)
        assert counts[activity] == observed, (
            f"{activity}: counted {counts[activity]}, observed {observed}")


def test_counts_match_observed_events_on_a_loop_and_parallel_nest():
    """The end-to-end guarantee: on cases that fit, an activity's
    traversal count IS the number of times it was observed. The shape
    here nests a parallel block with a multi-activity choice inside a
    loop — the combination that exercises both loop-visit accounting and
    the per-projection parse memo."""
    inner = _par(_xor(_tau(), _xor(_leaf("P"), _leaf("Q"))),
                 _xor(_tau(), _leaf("Z")))
    tree = _loop(_seq(_leaf("S"), inner), _leaf("R"))
    log = [
        ["S", "P", "R", "S", "Q"],
        ["S"],
        ["S", "Z", "R", "S", "P", "Z", "R", "S", "Q"],
        ["S", "Q", "R", "S"],
        ["S", "P", "Z"],
    ]
    stats = tv.compute_traversal_stats(tree, log, repair=False)
    assert stats.fitting_cases == len(log), "all of these fit by construction"
    counts = _counts_by_label(tree, stats)
    for activity in ("S", "P", "Q", "Z", "R"):
        observed = sum(t.count(activity) for t in log)
        assert counts.get(activity, 0) == observed, (
            f"{activity}: counted {counts.get(activity, 0)}, "
            f"observed {observed}")


def test_loop_iteration_max_is_still_the_max_for_scenario_synthesis():
    """The traversal fix must not disturb the max the synthesizer sizes
    its loop counter with."""
    inner = _loop(_leaf("A"), _leaf("B"))
    tree = _loop(_seq(_leaf("S"), inner), _leaf("Z"))
    ids = cs.assign_node_ids(tree)
    maxes, totals = {}, {}
    sig = cs.replay(tree, ["S", "A", "B", "A", "B", "A", "Z", "S", "A"],
                    node_ids=ids, loop_iter_counts=maxes,
                    loop_total_counts=totals)
    assert sig != cs.NOFIT
    inner_id = ids[id(inner)]
    assert maxes[inner_id] == 3, "heaviest visit ran 3 body iterations"
    assert totals[inner_id] == 4, "but the body ran 4 times in all"


def test_counts_scale_with_repeated_cases():
    # Deduplicating by activity sequence must not lose the multiplicity.
    tree = _seq(_leaf("A"), _leaf("B"))
    stats = tv.compute_traversal_stats(tree, [["A", "B"]] * 40, repair=False)
    assert stats.n_sequences == 1
    assert stats.n_signatures == 1
    assert _counts_by_label(tree, stats) == {"A": 40, "B": 40}


# ---------------------------------------------------------------------------
# Coverage reporting
# ---------------------------------------------------------------------------

def test_non_fitting_cases_are_reported_not_hidden():
    tree = _seq(_leaf("A"), _leaf("B"))
    log = [["A", "B"]] * 3 + [["A", "B", "ZZZ"]] * 2
    stats = tv.compute_traversal_stats(tree, log, repair=False)
    assert stats.total_cases == 5
    assert stats.fitting_cases == 3
    assert stats.unexplained_cases == 2
    assert stats.repaired_cases == 0
    assert stats.coverage == pytest.approx(0.6)
    assert stats.fitting_ratio == pytest.approx(0.6)
    # The counts describe only the cases that fit.
    assert _counts_by_label(tree, stats) == {"A": 3, "B": 3}


def test_empty_log_is_not_a_division_by_zero():
    tree = _seq(_leaf("A"))
    stats = tv.compute_traversal_stats(tree, [], repair=False)
    assert stats.total_cases == 0
    assert stats.coverage == 0.0
    assert stats.fitting_ratio == 0.0
    assert stats.node_counts == {}


def test_repair_guard_skips_alignment_when_there_are_too_many_variants():
    tree = _seq(_leaf("A"), _leaf("B"))
    log = [["A", "B", "X"], ["A", "B", "Y"], ["A", "B", "Z"]]
    stats = tv.compute_traversal_stats(tree, log, max_repair_sequences=1)
    # Guard tripped: nothing aligned, and the shortfall is visible.
    assert stats.repaired_cases == 0
    assert stats.unexplained_cases == 3


# ---------------------------------------------------------------------------
# Alignment repair (needs pm4py)
# ---------------------------------------------------------------------------

def test_alignment_repair_covers_the_whole_log():
    pytest.importorskip("pm4py")
    pytest.importorskip("pandas")
    import pm4py
    from pm4py.objects.process_tree.obj import Operator, ProcessTree

    # Build a real pm4py tree: A -> (B x tau) -> C
    root = ProcessTree(operator=Operator.SEQUENCE)
    a = ProcessTree(label="A", parent=root)
    choice = ProcessTree(operator=Operator.XOR, parent=root)
    c = ProcessTree(label="C", parent=root)
    root.children = [a, choice, c]
    b = ProcessTree(label="B", parent=choice)
    skip = ProcessTree(parent=choice)
    choice.children = [b, skip]
    assert pm4py is not None

    log = [["A", "B", "C"]] * 4 + [["A", "C"]] * 4 + [["A", "B", "QQ", "C"]] * 2
    without = tv.compute_traversal_stats(root, log, repair=False)
    assert without.fitting_cases == 8
    assert without.unexplained_cases == 2

    with_repair = tv.compute_traversal_stats(root, log, repair=True)
    assert with_repair.fitting_cases == 8
    assert with_repair.repaired_cases == 2
    assert with_repair.unexplained_cases == 0
    assert with_repair.coverage == pytest.approx(1.0)
    # Fit is unchanged by repair — it measures the model, not the counting.
    assert with_repair.fitting_ratio == pytest.approx(0.8)
    # The two repaired cases still walk A and C, and their B survives the
    # alignment (only the unexplainable QQ is dropped).
    counts = _counts_by_label(root, with_repair)
    assert counts["A"] == 10 and counts["C"] == 10
    assert counts["B"] == 6


def test_repair_orders_alignments_with_their_sequences():
    """Regression: synthetic case ids must not sort lexicographically out
    of step with the sequences they stand for, or every count lands on the
    wrong branch."""
    pytest.importorskip("pm4py")
    pytest.importorskip("pandas")
    from pm4py.objects.process_tree.obj import Operator, ProcessTree

    root = ProcessTree(operator=Operator.SEQUENCE)
    kids = []
    for name in ("A", "B", "C"):
        kids.append(ProcessTree(label=name, parent=root))
    root.children = kids

    # Enough distinct non-fitting sequences that ids pass 9 -> the point
    # where "..._10" would sort before "..._2".
    log = [["A", "B", "C", f"X{i}"] for i in range(12)]
    stats = tv.compute_traversal_stats(root, log, repair=True)
    assert stats.repaired_cases == 12
    # Every case walks the whole sequence exactly once.
    assert _counts_by_label(root, stats) == {"A": 12, "B": 12, "C": 12}


# ---------------------------------------------------------------------------
# End-to-end: counts reach the model as conserving annotations
# ---------------------------------------------------------------------------

def _annotated(tree, log_rows):
    """Convert ``tree`` to a UCM and annotate it with both stat kinds."""
    pd = pytest.importorskip("pandas")
    from pm4py_ucm.algo.performance import annotate_performance
    from pm4py_ucm.objects.ucm.conversion import from_process_tree

    rows = []
    for i, acts in enumerate(log_rows):
        for j, a in enumerate(acts):
            rows.append({"case:concept:name": f"c{i}", "concept:name": a,
                         "time:timestamp": pd.Timestamp("2024-01-01")
                         + pd.Timedelta(hours=j)})
    df = pd.DataFrame(rows)
    ucm = from_process_tree.apply(tree)
    stats = tv.compute_traversal_stats(tree, log_rows, repair=False)
    annotate_performance(
        ucm, df,
        node_metrics=("traversal_frequency",),
        edge_metrics=("traversal_frequency", "traversal_percentage"),
        traversal=stats, tree=tree,
    )
    return ucm


def _md(element):
    return {m.name: m.value for m in element.metadata}


def test_activity_out_edge_matches_the_activity_under_concurrency():
    from pm4py_ucm.objects.ucm.obj import UCM

    # A -> (B || C): B's successor in the trace is usually C, so B's
    # directly-follows count toward the join is a fraction of its
    # executions. The traversal count must not be.
    tree = _seq(_leaf("A"), _par(_leaf("B"), _leaf("C")))
    ucm = _annotated(tree, [["A", "B", "C"]] * 5 + [["A", "C", "B"]] * 5)

    seen = 0
    for m in ucm.maps:
        for n in m.nodes:
            if not isinstance(n, UCM.RespRef):
                continue
            md = _md(n)
            own = md.get("perf_traversal_frequency")
            assert own == "10", f"{n.resp_def.name} counted {own}"
            outs = [v for k, v in md.items()
                    if k.startswith("perf_branch")
                    and k.endswith("_traversal_frequency")]
            assert outs, "activity should carry an outgoing edge count"
            assert sum(int(v) for v in outs) == 10
            seen += 1
    assert seen == 3


def test_fork_branch_shares_carry_their_base_and_sum_to_the_fork():
    from pm4py_ucm.objects.ucm.obj import UCM

    tree = _seq(_leaf("A"), _xor(_tau(), _leaf("B")))
    ucm = _annotated(tree, [["A", "B"]] * 3 + [["A"]] * 9)

    forks = [n for m in ucm.maps for n in m.nodes
             if isinstance(n, UCM.OrFork)]
    assert forks, "the XOR should produce an OR-fork"
    md = _md(forks[0])
    counts = sorted(
        int(v) for k, v in md.items()
        if k.startswith("perf_branch") and k.endswith("_traversal_frequency"))
    assert counts == [3, 9]
    shares = sorted(
        v for k, v in md.items()
        if k.startswith("perf_branch") and k.endswith("_traversal_percentage"))
    # The base travels with the share, so "25%" can never be read as a
    # share of something else.
    assert shares == ["25% of 12", "75% of 12"]


def test_traversal_metrics_reach_the_jucm_and_survive_a_round_trip():
    """Like every other metric, the new ones are written to the `.jucm`
    as jUCMNav metadata — every available metric, not just the ≤2 shown
    on the diagram — and re-importing preserves them."""
    pytest.importorskip("pandas")
    import re
    import tempfile
    from pathlib import Path

    from pm4py_ucm.objects.ucm.exporter.variants.jucm import (
        serialize_to_string,
    )
    from pm4py_ucm.objects.ucm.importer.variants.jucm import parse_string

    tree = _seq(_leaf("A"), _xor(_tau(), _leaf("B")))
    ucm = _annotated(tree, [["A", "B"]] * 3 + [["A"]] * 9)
    text = serialize_to_string(ucm)

    names = set(re.findall(r'name="(perf_[^"]+)"', text))
    assert "perf_traversal_frequency" in names, "activity count not exported"
    assert any(n.endswith("_traversal_frequency") and n.startswith("perf_branch")
               for n in names), "edge count not exported"
    assert any(n.endswith("_traversal_percentage") for n in names), \
        "branch share not exported"
    assert 'value="25% of 12"' in text, "the share must keep its base"

    # Re-import and re-export: the metadata is preserved verbatim.
    reloaded = parse_string(text)
    again = serialize_to_string(reloaded)
    strip = lambda s: re.sub(r'(created|modified)="[^"]*"', "", s)  # noqa: E731
    assert strip(again) == strip(text)

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "m.jucm"
        path.write_text(text, encoding="utf-8")
        assert path.read_text(encoding="utf-8").count("traversal") >= 4


def test_traversal_metrics_are_absent_without_the_stats():
    """Passing no traversal stats leaves every other metric untouched."""
    pd = pytest.importorskip("pandas")
    from pm4py_ucm.algo.performance import annotate_performance
    from pm4py_ucm.objects.ucm.conversion import from_process_tree
    from pm4py_ucm.objects.ucm.obj import UCM

    tree = _seq(_leaf("A"), _leaf("B"))
    rows = [{"case:concept:name": "c0", "concept:name": a,
             "time:timestamp": pd.Timestamp("2024-01-01")
             + pd.Timedelta(hours=j)}
            for j, a in enumerate(["A", "B"])]
    ucm = from_process_tree.apply(tree)
    annotate_performance(ucm, pd.DataFrame(rows),
                         node_metrics=("frequency",),
                         edge_metrics=("frequency",))
    for m in ucm.maps:
        for n in m.nodes:
            md = _md(n)
            assert not any("traversal" in k for k in md)
            if isinstance(n, UCM.RespRef):
                assert md.get("perf_frequency") == "1"
