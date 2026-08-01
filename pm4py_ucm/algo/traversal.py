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

import time as _time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .discovery.variants import choice_signature as _cs
from .discovery.variants import parses as _parses

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
    loop_total_counts: Dict[int, int],
) -> Dict[int, int]:
    """Executions per tree node for **one** replayed trace.

    Derived top-down from the two by-products a
    :func:`choice_signature.replay` already produces: a choice's branch
    counts and a loop's body-execution total, both **absolute** — summed
    over every visit the trace makes. Sequence and concurrent children
    inherit their parent's count — every child of a ``->`` or ``+`` runs
    whenever the block does, which is precisely the property a
    directly-follows count fails to capture.

    Loops are exact under nesting. A loop node visited ``V`` times whose
    body ran ``D`` times in total (over all those visits) executes its
    ``do`` child ``D`` times and its ``redo`` child ``D − V`` — one redo
    fewer than a ``do`` per visit, since ``do (redo do)*`` starts with a
    ``do``. Both numbers are absolute, so the recursion below a nested
    loop continues with the true execution count rather than a
    per-visit figure multiplied by the number of visits.
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
            # ``times`` is how often this loop is ENTERED; the recorded
            # total is how often its body RAN across those entries. They
            # differ only under nesting, which is exactly the case a
            # per-visit iteration count would get wrong.
            visits = times
            total = loop_total_counts.get(nid, visits)
            rec(children[0], max(0, total))
            if len(children) > 1:
                rec(children[1], max(0, total - visits))
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
    loop_totals: Dict[int, int] = {}
    signature = _cs.replay(
        tree, list(seq), node_ids=node_ids,
        xor_branch_counts=xor_counts, loop_total_counts=loop_totals,
        **kwargs,
    )
    if signature == _cs.NOFIT:
        return None
    return (signature,
            node_counts_for_parse(tree, node_ids, xor_counts, loop_totals),
            xor_counts)


# ---------------------------------------------------------------------------
# Alignment repair
# ---------------------------------------------------------------------------

def _aligned_paths(
    tree, sequences: List[Tuple[str, ...]],
    weights: Optional[Dict[Tuple[str, ...], int]] = None,
    seconds_per_sequence: Optional[float] = 1.0,
    seconds_total: Optional[float] = 10.0,
    ticker=None,
) -> Dict[Tuple[str, ...], Tuple[str, ...]]:
    """Align each non-fitting sequence to its nearest path through the
    model, returning ``{original_sequence: repaired_sequence}``.

    Repaired paths contain only the *model*'s moves, so replaying one is
    guaranteed to fit. Sequences that cannot be aligned are omitted
    rather than guessed at. Requires pm4py; returns an empty mapping if
    it is unavailable, which the caller reports as un-counted coverage.

    **Alignment is bounded**, because its cost is not merely high but
    wildly unpredictable: on a real 2 455-case log the same model took
    0.05 s for a 12-event sequence and 26 s for a 10-event one — the
    search space depends on the model's loops and choices, not on the
    trace's length, so there is no size threshold that makes it safe.
    Left unbounded, 893 non-fitting sequences projected to ~10 hours.

    Two limits therefore apply, both passed down to pm4py's aligner:
    ``seconds_per_sequence`` stops any single pathological sequence from
    eating the budget, and ``seconds_total`` bounds the whole phase.
    Sequences left unaligned are simply not repaired, and the caller
    reports them as unexplained coverage rather than pretending. Pass
    ``None`` to either for the unbounded behaviour.

    Sequences are attempted in order of how many **cases** carry them
    (``weights``), so a budget that cannot cover everything is spent
    where it buys the most coverage.
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
    if not sequences:
        return {}
    if weights:
        # Heaviest first: if the budget runs out, it has bought the most
        # cases it could.
        sequences = sorted(sequences, key=lambda s: -weights.get(s, 0))

    try:
        net, im, fm = pm4py.convert_to_petri_net(tree)
        from pm4py.algo.conformance.alignments.petri_net import (
            algorithm as _alignments,
        )
    except Exception:
        return {}

    variant = _alignments.Variants.VERSION_STATE_EQUATION_A_STAR
    params: Dict[Any, Any] = {
        "case_id_key": "case:concept:name",
        "activity_key": "concept:name",
        "timestamp_key": "time:timestamp",
    }
    try:
        limits = variant.value.Parameters
        if seconds_per_sequence is not None:
            params[limits.PARAM_MAX_ALIGN_TIME_TRACE] = float(
                seconds_per_sequence)
    except Exception:
        pass

    # One sequence per call, with the clock checked between them. pm4py's
    # own whole-log time parameter does not reliably stop a batch, and a
    # single unbounded sequence here can run for tens of seconds, so the
    # overall budget is enforced here rather than delegated.
    out: Dict[Tuple[str, ...], Tuple[str, ...]] = {}
    started = _time.perf_counter()
    for seq in sequences:
        if (seconds_total is not None
                and _time.perf_counter() - started >= seconds_total):
            # Out of budget. Report the rest as done so a progress
            # display settles instead of freezing at a partial count —
            # the shortfall is carried honestly in unexplained_cases.
            if ticker is not None:
                ticker.finish()
            break
        if ticker is not None:
            ticker.tick()
        frame = pd.DataFrame([
            {"case:concept:name": "__repair",
             "concept:name": activity,
             "time:timestamp": pd.Timestamp("2000-01-01")
             + pd.Timedelta(seconds=j)}
            for j, activity in enumerate(seq)
        ])
        try:
            results = _alignments.apply(frame, net, im, fm,
                                        variant=variant, parameters=params)
        except Exception:
            continue
        result = (results or [None])[0]
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
    max_repair_seconds: Optional[float] = 10.0,
    max_repair_seconds_per_sequence: Optional[float] = 1.0,
    coarsen_loops: bool = True,
    progress_callback=None,
    parses: "Optional[_parses.ParseTable]" = None,
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
    max_repair_seconds, max_repair_seconds_per_sequence
        Wall-clock bounds on the alignment phase as a whole and on any
        single sequence. Alignment cost is not merely high but
        unpredictable — it follows the model's loops and choices, not
        the trace's length, so a short sequence can cost seconds while a
        longer one costs milliseconds. Bounding it keeps a mine
        responsive; whatever is left unaligned is reported as
        unexplained rather than silently attributed. Pass ``None`` to
        either for the unbounded behaviour.
    coarsen_loops
        Passed to :func:`choice_signature.replay`; does not affect the
        counts, only how signatures are pooled.
    progress_callback
        Optional ``callback(stage, done, total)`` — replay and alignment
        dominate the cost on large logs.
    parses
        Optional pre-built
        :class:`~pm4py_ucm.algo.discovery.variants.parses.ParseTable`.
        Variant clustering needs exactly the same parses, so a caller
        doing both can replay the log once and share the table instead of
        paying the dominant cost twice. Must come from the same tree.

    Returns
    -------
    TraversalStats
    """
    from .discovery.variants.clustering import _normalise_log
    from ..util.progress import Ticker

    # A supplied table already carries the normalised log; redoing the
    # groupby costs more than the replay itself on many logs.
    cases = (parses.cases if parses is not None and parses.cases is not None
             else _normalise_log(log))
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

    # The replay itself comes from the shared pass, so a caller that also
    # clusters the log (scenario synthesis needs the identical parses)
    # can build the table once and hand it to both instead of paying the
    # dominant cost twice.
    if parses is None:
        parses = _parses.replay_sequences(
            tree, sequence_cases, node_ids=node_ids,
            coarsen_loops=coarsen_loops, progress_callback=progress_callback,
        )
    # Deliberately keep the LOCAL node_ids: the table's own mapping is
    # keyed on ``id()`` of the tree it was built from, which is a
    # different object once the table has been round-tripped through a
    # cache. The parse dictionaries inside it are keyed on the *integer*
    # ids, and those are assignment-order based, so they stay valid for
    # any structurally identical tree.
    parsed: Dict[Tuple[str, ...], Any] = {}
    nofit: List[Tuple[str, ...]] = []
    for seq in sequence_cases:
        parse = parses.parses.get(seq)
        if parse is None:
            parse = _parses.replay_sequences(
                tree, [seq], node_ids=node_ids,
                coarsen_loops=coarsen_loops).parses[seq]
            parses.parses[seq] = parse
        if not parse.fits:
            nofit.append(seq)
        else:
            parsed[seq] = (
                parse.signature,
                node_counts_for_parse(tree, node_ids, parse.xor_branch_counts,
                                      parse.loop_iter_totals),
                parse.xor_branch_counts,
            )

    # Repair the rest, unless switched off or too expensive to be worth
    # blocking on.
    repaired_paths: Dict[Tuple[str, ...], Tuple[str, ...]] = {}
    if repair and nofit and (max_repair_sequences is None
                             or len(nofit) <= max_repair_sequences):
        # Name the budget in the stage label: on a big log this phase can
        # stop early, and "will it ever finish?" is exactly the question a
        # bare spinner leaves open.
        stage = "Aligning unfitted variants"
        if max_repair_seconds is not None:
            stage += f" (≤{max_repair_seconds:g}s)"
        r_ticker = Ticker(progress_callback, stage, len(nofit))
        repaired_paths = _aligned_paths(
            tree, nofit, weights=sequence_cases,
            seconds_per_sequence=max_repair_seconds_per_sequence,
            seconds_total=max_repair_seconds,
            ticker=r_ticker,
        )
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
