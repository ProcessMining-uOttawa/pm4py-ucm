"""Replay-based traversal counts: how often the *model* is walked.

Frequency overlays traditionally read two different things off the log:
an activity's number is its **event count**, while an edge's number is a
**directly-follows count** between the activities on either side. On a
sequential model the two agree. On a model with concurrency they do not,
and the disagreement is large:

* a directly-follows pair only exists when two events are *adjacent in
  the trace*, so on a parallel block the event that actually follows an
  activity usually belongs to a sibling branch — a transition the model
  has no edge for, so the traversal is counted nowhere;
* a silent (``tau``) branch produces no event at all, so a skip is
  invisible: an exclusive choice whose alternative is "do nothing"
  reports 100 % for the branch that happens to be observable.

The result is a diagram whose numbers do not conserve — an activity with
257 executions whose only outgoing edge reads 40.

This module computes the honest quantity instead: **how many cases walk
each part of the model**, by replaying the log on the process tree the
model was built from. Because a process tree is block-structured, a
replay yields an exact traversal count for every tree node, and those
counts conserve by construction — an activity's outgoing edge carries
its own count, the branches of a parallel fork all carry the fork's
inflow, and the branches of a choice sum to it.

Coverage
--------

A trace only has an exact parse if it *fits* the tree. A model mined
with a noise threshold deliberately discards infrequent behaviour, so a
simplified model typically explains only part of the log — and the
counts must say so rather than quietly describe a sub-log. Two
strategies are offered through ``repair``:

``repair=False``
    Count only the cases that fit the tree exactly. Every number is a
    real observed case, but they describe the fitting sub-log alone and
    systematically under-report.

``repair=True`` (default)
    Additionally align each non-fitting case to its nearest path through
    the model and count that path. Coverage reaches the whole log, at
    the cost of counting a *repaired* path rather than an observed one:
    for an activity the model treats as mandatory, alignment inserts the
    missing execution, so the traversal count can exceed the number of
    observed events. That gap is meaningful — it is the amount by which
    the model over-claims — and :class:`TraversalStats` reports the
    coverage needed to interpret it.

Either way :attr:`TraversalStats.fitting_cases` /
:attr:`~TraversalStats.repaired_cases` carry the provenance, so a caller
can label the diagram with what the numbers actually cover.

Cost
----

Traversal counts are a function of the *parse*, not of the trace: two
traces that replay to the same concurrency-aware signature walk the
model identically. The work is therefore deduplicated twice — once per
distinct activity sequence (which is what saves the replay itself) and
again per signature (which is what the counts are keyed on). Alignment
repair likewise runs once per distinct non-fitting sequence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .discovery.variants import choice_signature as _cs

#: Tree operators, matched on the enum's ``value`` so PM4Py need not be
#: imported here (mirrors ``objects.ucm.conversion.from_process_tree``).
_SEQUENCE = "->"
_XOR = "X"
_OR = "O"
_PARALLEL = "+"
_INTERLEAVING = "o"
_LOOP = "*"

_CHOICE_OPS = {_XOR, _OR}
_CONCURRENT_OPS = {_PARALLEL, _INTERLEAVING}


@dataclass
class TraversalStats:
    """Model-traversal counts keyed by process-tree node id.

    The ids are those of :func:`choice_signature.assign_node_ids`, so a
    caller correlates them with model elements through the originating
    tree node (UCM elements built by
    :mod:`~pm4py_ucm.objects.ucm.conversion.from_process_tree` carry the
    tree node's ``id()`` in ``_tree_python_id``).
    """

    #: ``{tree_node_id: traversals}`` — total walks over all counted
    #: cases. A node inside a loop body counts once per iteration.
    node_counts: Dict[int, int] = field(default_factory=dict)
    #: ``{tree_node_id: cases}`` — distinct cases traversing the node at
    #: least once.
    node_cases: Dict[int, int] = field(default_factory=dict)
    #: ``{choice_node_id: {branch_index: traversals}}`` for every XOR /
    #: OR node. Branch counts sum to the choice's own
    #: :attr:`node_counts` entry, which is what makes a split's
    #: percentages add up to 100 %.
    branch_counts: Dict[int, Dict[int, int]] = field(default_factory=dict)

    #: Cases in the log.
    total_cases: int = 0
    #: Cases that replayed on the tree exactly.
    fitting_cases: int = 0
    #: Non-fitting cases counted via an aligned (repaired) path.
    repaired_cases: int = 0
    #: Cases contributing to no count at all (non-fitting, and either
    #: repair was off or alignment was unavailable / failed).
    unexplained_cases: int = 0

    #: Distinct activity sequences in the log.
    n_sequences: int = 0
    #: Distinct concurrency-aware signatures among the counted cases.
    n_signatures: int = 0

    @property
    def counted_cases(self) -> int:
        """Cases contributing to the counts (fitting + repaired)."""
        return self.fitting_cases + self.repaired_cases

    @property
    def coverage(self) -> float:
        """Share of the log the counts describe, ``0.0``–``1.0``."""
        return (self.counted_cases / self.total_cases
                if self.total_cases else 0.0)

    @property
    def fitting_ratio(self) -> float:
        """Share of the log that fits the model exactly — the honest
        measure of how much of the behaviour the model explains, and the
        number to show the user when it is low."""
        return (self.fitting_cases / self.total_cases
                if self.total_cases else 0.0)


# ---------------------------------------------------------------------------
# Per-parse node counts
# ---------------------------------------------------------------------------

def _op_value(node) -> Optional[str]:
    op = getattr(node, "operator", None)
    return None if op is None else getattr(op, "value", op)


def _children(node) -> List[Any]:
    return list(getattr(node, "children", []) or [])


def node_counts_for_parse(
    tree,
    node_ids: Dict[int, int],
    xor_branch_counts: Dict[int, Dict[int, int]],
    loop_iter_counts: Dict[int, int],
) -> Dict[int, int]:
    """Executions per tree node for **one** replayed trace.

    Derived top-down from the two by-products a
    :func:`choice_signature.replay` already produces: a choice's branch
    counts (absolute, summed over the trace) and a loop's iteration
    count. Sequence and concurrent children inherit their parent's count
    — every child of a ``->`` or ``+`` runs whenever the block does,
    which is precisely the property a directly-follows count fails to
    capture.

    Loops record the number of ``do`` iterations, so the ``do`` child
    runs ``iterations`` times and the ``redo`` child ``iterations − 1``.
    A loop nested inside another loop is approximate: the underlying
    replay records the *maximum* iteration count per node rather than
    one entry per visit.
    """
    out: Dict[int, int] = {}

    def rec(node, times: int) -> None:
        if times <= 0:
            return
        nid = node_ids[id(node)]
        out[nid] = out.get(nid, 0) + times
        children = _children(node)
        if not children:
            return
        op = _op_value(node)
        if op in _CHOICE_OPS:
            # Branch counts are absolute for this trace, so they are used
            # as-is rather than scaled by ``times`` — a choice visited
            # twice already contributes twice to them.
            branches = xor_branch_counts.get(nid, {})
            for i, child in enumerate(children):
                rec(child, branches.get(i, 0))
        elif op == _LOOP:
            iterations = loop_iter_counts.get(nid, 1)
            rec(children[0], times * max(0, iterations))
            if len(children) > 1:
                rec(children[1], times * max(0, iterations - 1))
            for child in children[2:]:
                rec(child, times)
        else:
            # Sequence, parallel, interleaving, and any unknown operator
            # (which the converter also treats as a sequence).
            for child in children:
                rec(child, times)

    rec(tree, 1)
    return out


def _replay_sequence(tree, node_ids, seq: Sequence[str], **kwargs):
    """Replay one activity sequence → ``(node_counts, branch_counts)``
    or ``None`` when it does not fit."""
    xor_counts: Dict[int, Dict[int, int]] = {}
    loop_iters: Dict[int, int] = {}
    signature = _cs.replay(
        tree, list(seq), node_ids=node_ids,
        xor_branch_counts=xor_counts, loop_iter_counts=loop_iters,
        **kwargs,
    )
    if signature == _cs.NOFIT:
        return None
    return (signature,
            node_counts_for_parse(tree, node_ids, xor_counts, loop_iters),
            xor_counts)


# ---------------------------------------------------------------------------
# Alignment repair
# ---------------------------------------------------------------------------

def _aligned_paths(
    tree, sequences: List[Tuple[str, ...]],
) -> Dict[Tuple[str, ...], Tuple[str, ...]]:
    """Align each non-fitting sequence to its nearest path through the
    model, returning ``{original_sequence: repaired_sequence}``.

    Repaired paths contain only the *model*'s moves, so replaying one is
    guaranteed to fit. Sequences that cannot be aligned are omitted
    rather than guessed at. Requires pm4py; returns an empty mapping if
    it is unavailable, which the caller reports as un-counted coverage.
    """
    if not sequences:
        return {}
    try:
        import pandas as pd
        import pm4py
    except Exception:
        return {}

    # Case ids are zero-padded so that lexicographic order — which the
    # aligner may impose when it groups cases — matches the order of
    # ``sequences``. Without the padding ``__repair_10`` sorts before
    # ``__repair_2`` and the results pair up with the wrong sequences.
    # An empty sequence contributes no rows, so it would not come back
    # from the aligner and would shift every later result by one.
    sequences = [s for s in sequences if s]
    width = max(2, len(str(len(sequences))))
    rows = []
    for i, seq in enumerate(sequences):
        for j, activity in enumerate(seq):
            rows.append({"case:concept:name": f"__repair_{i:0{width}d}",
                         "concept:name": activity,
                         "time:timestamp": pd.Timestamp("2000-01-01")
                         + pd.Timedelta(seconds=j)})
    if not rows:
        return {}

    try:
        net, im, fm = pm4py.convert_to_petri_net(tree)
        results = pm4py.conformance_diagnostics_alignments(
            pd.DataFrame(rows), net, im, fm,
            case_id_key="case:concept:name",
            activity_key="concept:name",
            timestamp_key="time:timestamp",
        )
    except Exception:
        return {}

    out: Dict[Tuple[str, ...], Tuple[str, ...]] = {}
    for seq, result in zip(sequences, results or []):
        if not result:
            continue
        moves = result.get("alignment") or []
        # Keep the model side of each move: ``>>`` marks a log-only move
        # (an event the model cannot explain) and ``None`` a silent
        # transition; neither contributes a visible activity.
        path = tuple(m[1] for m in moves
                     if isinstance(m, (tuple, list)) and len(m) == 2
                     and m[1] is not None and m[1] != ">>")
        out[seq] = path
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def compute_traversal_stats(
    tree,
    log,
    *,
    repair: bool = True,
    max_repair_sequences: Optional[int] = 2000,
    coarsen_loops: bool = True,
    progress_callback=None,
) -> TraversalStats:
    """Count how often each part of ``tree`` is traversed by ``log``.

    Parameters
    ----------
    tree
        The process tree the model was built from.
    log
        Event log in any form
        :func:`pm4py_ucm.algo.discovery.variants.clustering._normalise_log`
        accepts (DataFrame, pm4py ``EventLog``, or a list of traces).
    repair
        When ``True`` (default) non-fitting cases are aligned to their
        nearest model path and counted, giving full coverage of the log;
        when ``False`` they are left out and reported as unexplained.
        See the module docstring for the trade-off.
    max_repair_sequences
        Safety valve: skip repair when the number of *distinct*
        non-fitting sequences exceeds this (alignment is the expensive
        step). ``None`` disables the guard. The resulting shortfall is
        visible as :attr:`TraversalStats.unexplained_cases`.
    coarsen_loops
        Passed to :func:`choice_signature.replay`; does not affect the
        counts, only how signatures are pooled.
    progress_callback
        Optional ``callback(stage, done, total)`` — replay and alignment
        dominate the cost on large logs.

    Returns
    -------
    TraversalStats
    """
    from .discovery.variants.clustering import _normalise_log
    from ..util.progress import Ticker

    cases = _normalise_log(log)
    node_ids = _cs.assign_node_ids(tree)

    # Cases sharing an activity sequence share a parse, so replay once
    # per distinct sequence and weight by how many cases carry it.
    sequence_cases: Dict[Tuple[str, ...], int] = {}
    for _case_id, trace in cases:
        key = tuple(trace)
        sequence_cases[key] = sequence_cases.get(key, 0) + 1

    stats = TraversalStats(
        total_cases=len(cases), n_sequences=len(sequence_cases),
    )
    if not cases:
        return stats

    ticker = Ticker(progress_callback, "Replaying variants",
                    len(sequence_cases))
    parsed: Dict[Tuple[str, ...], Any] = {}
    nofit: List[Tuple[str, ...]] = []
    for seq in sequence_cases:
        result = _replay_sequence(tree, node_ids, seq,
                                  coarsen_loops=coarsen_loops)
        if result is None:
            nofit.append(seq)
        else:
            parsed[seq] = result
        ticker.tick()
    ticker.finish()

    # Repair the rest, unless switched off or too expensive to be worth
    # blocking on.
    repaired_paths: Dict[Tuple[str, ...], Tuple[str, ...]] = {}
    if repair and nofit and (max_repair_sequences is None
                             or len(nofit) <= max_repair_sequences):
        r_ticker = Ticker(progress_callback, "Aligning variants", len(nofit))
        repaired_paths = _aligned_paths(tree, nofit)
        r_ticker.finish()

    # Aggregate. Counts are keyed on the signature, so sequences that
    # parse the same way are added up rather than recomputed.
    signatures: set = set()
    for seq, weight in sequence_cases.items():
        result = parsed.get(seq)
        if result is None:
            path = repaired_paths.get(seq)
            if path is None:
                stats.unexplained_cases += weight
                continue
            result = _replay_sequence(tree, node_ids, path,
                                      coarsen_loops=coarsen_loops)
            if result is None:
                # A model path that will not replay would be a bug in the
                # aligner or the tree; treat it as uncounted rather than
                # silently attributing it somewhere.
                stats.unexplained_cases += weight
                continue
            stats.repaired_cases += weight
        else:
            stats.fitting_cases += weight

        signature, counts, branches = result
        signatures.add(signature)
        for nid, n in counts.items():
            stats.node_counts[nid] = stats.node_counts.get(nid, 0) + n * weight
            stats.node_cases[nid] = stats.node_cases.get(nid, 0) + weight
        for nid, per_branch in branches.items():
            target = stats.branch_counts.setdefault(nid, {})
            for branch, n in per_branch.items():
                target[branch] = target.get(branch, 0) + n * weight

    stats.n_signatures = len(signatures)
    return stats
