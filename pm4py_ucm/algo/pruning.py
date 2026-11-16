"""Prune infrequent paths from a process tree (prototype).

On a directly-follows graph, dropping infrequent behaviour is trivial:
delete the edges below a threshold. A UCM or BPMN model has connection
nodes — forks and joins — so an edge cannot simply be deleted; the whole
region between a fork and its join has to go, and the fork itself must
disappear when a single branch is left.

The process tree makes this easy, because it is block-structured:
removing a child of an operator always leaves a valid tree. This module
therefore prunes **the tree**, using the replay-based traversal counts
of :mod:`pm4py_ucm.algo.traversal`, and leaves the conversion to UCM (and
everything else that keys on the tree) untouched: convert the pruned tree
as usual.

What is removed
---------------

Only two places in a tree are real decision points, and only these are
thresholded:

* a **branch of a choice** (``X`` or ``O``) — its share is its traversals
  over the choice's own traversals;
* the **redo part of a loop** — its share is the redo's traversals over
  the body's, i.e. the fraction of body executions followed by a repeat
  rather than by the exit.

The branches of a parallel block are never thresholded directly: each
carries the fork's full inflow, so none is locally infrequent. What looks
like an infrequent path after an AND-fork is an *optional* branch,
``+(A, X(B, tau))`` with ``B`` rare. Removing ``B`` leaves a silent
branch, and the structural clean-up below then removes it from the fork.

Structural clean-up
-------------------

After the removals the tree is reduced bottom-up:

* a choice left with one branch is replaced by that branch;
* several silent branches of a choice merge into one;
* silent children of a sequence or of a concurrent block are dropped, and
  a block left with one child is replaced by it (with none: by a tau);
* a loop whose redo was removed is replaced by its body.

Thresholds are evaluated **once, on the counts of the original tree**.
Nothing is re-thresholded after the percentages of the surviving
branches have (implicitly) grown, so raising a threshold can only remove
more: the result is monotone in both thresholds.

The most frequent branch of a choice is always kept, whatever the
thresholds, so a block can never be emptied by a high percentage.

What happens to the affected cases
----------------------------------

By default the log is not touched. Cases that walked a removed branch no
longer fit the pruned tree; recomputing
:func:`~pm4py_ucm.algo.traversal.compute_traversal_stats` on it reports
them through its fitting ratio (and, with ``repair=True``, counts them
along their nearest remaining path).

The alternative is to **drop** them: :meth:`PruneResult.filter_log`
returns the log without the cases that walked a removed branch. Every
case that fitted the original tree and is kept still fits the pruned one,
so the remaining counts conserve and the percentages are those of the
remaining cases — at the price of a model, and of overlays, that describe
a sub-log. :attr:`PruneResult.affected_cases` says how many cases that
is.

The tree is duck-typed (``operator`` / ``label`` / ``children``), as in
the rest of the replay code, so PM4Py is not imported here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .discovery.variants import choice_signature as _cs
from .traversal import TraversalStats

_SEQUENCE = "->"
_XOR = "X"
_OR = "O"
_PARALLEL = "+"
_INTERLEAVING = "o"
_LOOP = "*"

_CHOICE_OPS = {_XOR, _OR}

#: Longest label summary kept for a removed branch.
_SUMMARY_LEAVES = 4


@dataclass
class PrunedBranch:
    """One branch removed by :func:`prune_infrequent_paths`."""

    #: ``"choice"`` (a branch of an ``X`` / ``O``) or ``"loop"`` (a redo).
    kind: str
    #: Operator of the decision point the branch hung from.
    operator: str
    #: Node id (in the **original** tree) of the decision point.
    parent_id: int
    #: Node id (in the **original** tree) of the removed branch's root.
    node_id: int
    #: Short description: the branch's first activities, or ``"(skip)"``
    #: for a silent branch.
    summary: str
    #: Activities removed with the branch (labelled leaves; with repeats
    #: if the tree has duplicate labels).
    activities: List[str]
    #: Times the branch was walked, over all counted cases.
    traversals: int
    #: Distinct cases that walked it at least once.
    cases: int
    #: Local share at the decision point, ``0.0``–``1.0``.
    share: float
    #: ``"min_cases"``, ``"min_share"`` or ``"min_cases+min_share"``.
    reason: str


@dataclass
class PruneResult:
    """Outcome of :func:`prune_infrequent_paths`."""

    #: The pruned tree — always a fresh copy; the input is not modified.
    tree: Any
    #: Branches removed, in pre-order of the original tree. Branches
    #: nested inside a removed branch are not listed separately.
    removed: List[PrunedBranch] = field(default_factory=list)
    #: Decision points (choices and loops) that disappeared, either
    #: because one branch was left or because the redo was removed.
    collapsed_operators: int = 0
    #: Cases the traversal counts describe (``stats.counted_cases``).
    counted_cases: int = 0
    #: Distinct activity sequences whose (possibly repaired) path walks a
    #: removed branch; ``None`` when the traversal counts did not record
    #: per-sequence paths (hand-built :class:`TraversalStats`).
    affected_sequences: Optional[frozenset] = None
    #: Exact number of cases carrying those sequences, or ``None``.
    affected_case_count: Optional[int] = None
    #: The repaired paths of the traversal counts the pruning was decided
    #: on. Pass them as ``repair_hints`` when replaying the log on
    #: :attr:`tree`: every non-fitting sequence whose nearest path avoided
    #: the removed branches is then counted without a second alignment.
    repair_hints: Dict[Tuple[str, ...], Tuple[str, ...]] = field(
        default_factory=dict)
    #: Set by :func:`pm4py_ucm.prune_infrequent_paths`: the log to go on
    #: with — the one passed in, or, with ``drop_cases=True``, that log
    #: without the cases that walked a removed branch.
    log: Any = None

    @property
    def changed(self) -> bool:
        return bool(self.removed)

    def dropped_case_ids(self, log) -> List[str]:
        """Ids (as strings) of the cases of ``log`` that walked a removed
        branch — the cases :meth:`filter_log` drops. ``log`` must be the
        log the traversal counts were computed on."""
        if self.affected_sequences is None:
            raise ValueError(
                "these traversal counts carry no per-sequence paths; "
                "compute them with compute_traversal_stats")
        if not self.affected_sequences:
            return []
        from .discovery.variants.clustering import _normalise_log
        return [cid for cid, trace in _normalise_log(log)
                if tuple(trace) in self.affected_sequences]

    def filter_log(self, log, case_id_col: Optional[str] = None):
        """``log`` without the cases that walked a removed branch.

        A DataFrame comes back as a DataFrame (``case_id_col`` defaults to
        ``case:concept:name``, else ``case_id``), a PM4Py ``EventLog`` as
        an ``EventLog``, and a list of traces as a list.
        """
        drop = set(self.dropped_case_ids(log))
        if not drop:
            return log
        if hasattr(log, "columns"):
            col = case_id_col or ("case:concept:name"
                                  if "case:concept:name" in log.columns
                                  else "case_id")
            return log[~log[col].astype(str).isin(drop)]
        from .discovery.variants.clustering import _normalise_log
        ids = [cid for cid, _ in _normalise_log(log)]
        kept = [t for cid, t in zip(ids, log) if cid not in drop]
        if hasattr(log, "attributes") and hasattr(log, "_list"):
            import copy
            out = copy.copy(log)
            out._list = kept
            return out
        return kept

    @property
    def removed_activities(self) -> List[str]:
        """Distinct activities that left the model, in removal order."""
        seen: Dict[str, None] = {}
        for b in self.removed:
            for a in b.activities:
                seen.setdefault(a)
        return list(seen)

    @property
    def affected_cases(self) -> Tuple[int, int]:
        """``(at_least, at_most)`` cases that walked a removed branch.

        The two are equal — the exact figure — whenever the traversal
        counts recorded per-sequence paths, which
        :func:`~pm4py_ucm.algo.traversal.compute_traversal_stats` always
        does. Otherwise a case can walk several removed branches, so the
        number is only known to lie between the largest single branch
        and the sum (capped at the counted cases).
        """
        if self.affected_case_count is not None:
            return (self.affected_case_count, self.affected_case_count)
        per_branch = [b.cases for b in self.removed]
        if not per_branch:
            return (0, 0)
        upper = sum(per_branch)
        if self.counted_cases:
            upper = min(upper, self.counted_cases)
        return (max(per_branch), upper)


# ---------------------------------------------------------------------------
# Intermediate representation
# ---------------------------------------------------------------------------

class _N:
    """A tree node annotated with its original id and counts."""

    __slots__ = ("src", "op", "label", "children", "nid", "count", "cases")

    def __init__(self, src, op, label, nid, count, cases):
        self.src = src
        self.op = op
        self.label = label
        self.children: List["_N"] = []
        self.nid = nid
        self.count = count
        self.cases = cases

    @property
    def is_tau(self) -> bool:
        return self.op is None and self.label is None


def _op_value(node) -> Optional[str]:
    op = getattr(node, "operator", None)
    return None if op is None else getattr(op, "value", op)


def _annotate(tree, stats: TraversalStats) -> _N:
    ids = _cs.assign_node_ids(tree)

    def rec(node) -> _N:
        nid = ids[id(node)]
        n = _N(node, _op_value(node), getattr(node, "label", None), nid,
               stats.node_counts.get(nid, 0), stats.node_cases.get(nid, 0))
        n.children = [rec(c) for c in (getattr(node, "children", None) or [])]
        return n

    return rec(tree)


def _flatten_choices(n: _N) -> None:
    """Merge a choice nested directly in a choice of the same kind.

    ``X(a, X(b, c))`` is the same decision as ``X(a, b, c)``; judging it
    as one decision keeps a branch's share independent of how the miner
    happened to nest it. Counts are absolute, so the lifted branches keep
    theirs.
    """
    for c in n.children:
        _flatten_choices(c)
    if n.op in _CHOICE_OPS:
        flat: List[_N] = []
        for c in n.children:
            flat.extend(c.children if c.op == n.op else [c])
        n.children = flat


def _activities(n: _N) -> List[str]:
    if not n.children:
        return [] if n.label is None else [str(n.label)]
    out: List[str] = []
    for c in n.children:
        out.extend(_activities(c))
    return out


def _summary(acts: List[str]) -> str:
    if not acts:
        return "(skip)"
    head = ", ".join(acts[:_SUMMARY_LEAVES])
    more = len(acts) - _SUMMARY_LEAVES
    return head if more <= 0 else f"{head}, … (+{more})"


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

class _Pruner:
    def __init__(self, min_cases: int, min_share: float, prune_loops: bool):
        self.min_cases = min_cases
        self.min_share = min_share
        self.prune_loops = prune_loops
        self.removed: List[PrunedBranch] = []
        self.collapsed = 0

    def _reason(self, cases: int, share: float) -> Optional[str]:
        why = []
        if self.min_cases > 0 and cases < self.min_cases:
            why.append("min_cases")
        if self.min_share > 0 and share < self.min_share:
            why.append("min_share")
        return "+".join(why) if why else None

    def _record(self, kind: str, parent: _N, branch: _N, share: float,
                reason: str) -> None:
        acts = _activities(branch)
        self.removed.append(PrunedBranch(
            kind=kind, operator=parent.op or "", parent_id=parent.nid,
            node_id=branch.nid, summary=_summary(acts), activities=acts,
            traversals=branch.count, cases=branch.cases, share=share,
            reason=reason))

    def prune(self, n: _N) -> _N:
        """Reduce ``n`` in place (bottom-up) and return what replaces it.

        A silent result is returned as a tau node; the caller decides
        whether a tau is dropped (sequence, concurrency) or kept (choice,
        loop).
        """
        if not n.children:
            return n
        if n.op in _CHOICE_OPS:
            return self._prune_choice(n)
        if n.op == _LOOP:
            return self._prune_loop(n)
        return self._prune_block(n)

    def _prune_choice(self, n: _N) -> _N:
        total = n.count
        shares = [(c.count / total if total else 0.0) for c in n.children]
        reasons = [self._reason(c.cases, s)
                   for c, s in zip(n.children, shares)]
        if all(r is not None for r in reasons):
            # Never empty a decision: keep its most frequent branch (the
            # first one on a tie, which is stable).
            best = max(range(len(n.children)),
                       key=lambda i: (n.children[i].count, -i))
            reasons[best] = None
        kept: List[_N] = []
        for c, s, r in zip(n.children, shares, reasons):
            if r is None:
                kept.append(c)
            else:
                self._record("choice", n, c, s, r)
        out: List[_N] = []
        has_tau = False
        for c in kept:
            c = self.prune(c)
            if c.is_tau:
                if has_tau:
                    continue
                has_tau = True
            out.append(c)
        if len(out) == 1:
            self.collapsed += 1
            return out[0]
        n.children = out
        return n

    def _prune_loop(self, n: _N) -> _N:
        do = n.children[0]
        if len(n.children) == 2 and self.prune_loops:
            redo = n.children[1]
            share = redo.count / do.count if do.count else 0.0
            reason = self._reason(redo.cases, share)
            if reason is not None:
                self._record("loop", n, redo, share, reason)
                self.collapsed += 1
                return self.prune(do)
        n.children = [self.prune(c) for c in n.children]
        if all(c.is_tau for c in n.children):
            return n.children[0]
        return n

    def _prune_block(self, n: _N) -> _N:
        out = [c for c in (self.prune(c) for c in n.children)
               if not c.is_tau]
        if not out:
            return _N(None, None, None, n.nid, n.count, n.cases)
        if len(out) == 1:
            return out[0]
        n.children = out
        return n


# ---------------------------------------------------------------------------
# Rebuilding a tree of the caller's own node type
# ---------------------------------------------------------------------------

def _rebuild(n: _N, node_type, template) -> Any:
    """Copy ``n`` into a fresh tree of ``node_type``.

    Operators are copied from the original nodes, so a PM4Py tree keeps
    its ``Operator`` enum members. ``parent`` is only maintained when the
    original nodes have one.
    """
    operator = getattr(n.src, "operator", None) if n.src is not None else None
    node = node_type(operator=operator, label=n.label)
    kids = [_rebuild(c, node_type, template) for c in n.children]
    node.children = kids
    if hasattr(template, "parent"):
        for k in kids:
            k.parent = node
    return node


def prune_infrequent_paths(
    tree,
    stats: TraversalStats,
    *,
    min_cases: int = 0,
    min_share: float = 0.0,
    prune_loops: bool = True,
) -> PruneResult:
    """Remove the infrequent paths of ``tree`` and tidy up what is left.

    Parameters
    ----------
    tree
        The process tree ``stats`` was computed on. Not modified.
    stats
        Its traversal counts
        (:func:`pm4py_ucm.algo.traversal.compute_traversal_stats`).
    min_cases
        Remove a branch walked by fewer than this many distinct cases.
        ``0`` disables the test; ``1`` removes only what was never
        walked.
    min_share
        Remove a branch whose local share at its decision point is below
        this fraction (``0.05`` = 5 %). ``0`` disables the test.
    prune_loops
        Also judge the redo part of a loop; when it goes, the loop is
        replaced by its body.

    A branch is removed when it fails **either** active test. With both
    at ``0`` nothing is removed, though nested choices of the same kind
    are still merged and the result is still a copy.

    Returns
    -------
    PruneResult
    """
    if min_cases < 0:
        raise ValueError("min_cases must be >= 0")
    if not 0.0 <= min_share <= 1.0:
        raise ValueError("min_share is a fraction between 0 and 1")

    root = _annotate(tree, stats)
    _flatten_choices(root)
    pruner = _Pruner(int(min_cases), float(min_share), prune_loops)
    root = pruner.prune(root)

    affected = n_affected = None
    seq_nodes = getattr(stats, "sequence_nodes", None)
    if seq_nodes or not stats.counted_cases:
        gone = frozenset(b.node_id for b in pruner.removed)
        affected = frozenset(
            seq for seq, nodes in (seq_nodes or {}).items()
            if not gone.isdisjoint(nodes))
        weights = getattr(stats, "sequence_cases", None) or {}
        n_affected = sum(weights.get(seq, 0) for seq in affected)
    return PruneResult(
        tree=_rebuild(root, type(tree), tree),
        removed=pruner.removed,
        collapsed_operators=pruner.collapsed,
        counted_cases=stats.counted_cases,
        affected_sequences=affected,
        affected_case_count=n_affected,
        repair_hints=dict(getattr(stats, "repaired_paths", None) or {}),
    )
