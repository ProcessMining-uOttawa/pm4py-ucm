"""One replay pass, shared by everything that needs a parse.

Replaying a log on a process tree is the most expensive step in the
package — on BPI Challenge 2012 at ``noise_threshold=0.0`` it is ~23
minutes for 4 366 distinct sequences, dwarfing the 39 seconds the miner
itself takes. Two consumers need exactly the same parses:

* :func:`~pm4py_ucm.algo.discovery.variants.clustering.cluster` groups
  cases by concurrency-aware signature for scenario synthesis;
* :func:`~pm4py_ucm.algo.traversal.compute_traversal_stats` counts how
  often each part of the model is walked.

They used to replay independently, so a session that looked at both the
Model view and the Scenarios view paid the full cost twice. This module
holds the single pass they now share: replay each **distinct activity
sequence** once, keep everything either consumer could want, and hand
the table to both.

The table is keyed by sequence rather than by case because a trace's
parse is a function of its activity sequence alone — and because the
saving is largest on exactly the sequences that cost the most, since a
``NOFIT`` verdict is only reached after the backtracking search
exhausts its state budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import choice_signature as _cs


@dataclass
class SequenceParse:
    """What one distinct activity sequence's replay yielded.

    Every field is **read-only** once built: one instance serves every
    case carrying the sequence, so a consumer must not mutate it.
    """

    #: The canonical signature, or :data:`choice_signature.NOFIT`.
    signature: Any = _cs.NOFIT
    #: ``{xor_node_id: {branch: times}}`` summed over the sequence.
    xor_branch_counts: Dict[int, Dict[int, int]] = field(default_factory=dict)
    #: ``{loop_node_id: max body iterations over that node's visits}`` —
    #: what scenario synthesis sizes a loop counter with.
    loop_iter_max: Dict[int, int] = field(default_factory=dict)
    #: ``{loop_node_id: total body iterations over all visits}`` — what
    #: an execution count needs. The two differ only under nesting.
    loop_iter_totals: Dict[int, int] = field(default_factory=dict)

    @property
    def fits(self) -> bool:
        return self.signature != _cs.NOFIT


@dataclass
class ParseTable:
    """``{activity_sequence: SequenceParse}`` plus the ids it is keyed on.

    The parse dictionaries are keyed by the **integer** ids
    :func:`choice_signature.assign_node_ids` assigns. Those are
    traversal-order based, so they remain valid for any structurally
    identical tree — a table survives being cached and handed back
    alongside a fresh copy of the same tree.

    :attr:`node_ids` does **not**: it maps ``id(node)`` to those
    integers, and object identity does not survive a round trip. Use it
    only with the very tree the table was built from; recompute it
    otherwise.
    """

    parses: Dict[Tuple[str, ...], SequenceParse] = field(default_factory=dict)
    node_ids: Dict[int, int] = field(default_factory=dict)
    #: ``True`` when loops were coarsened during replay — signatures from
    #: a coarsened pass must not be compared with uncoarsened ones.
    coarsen_loops: bool = True
    #: The normalised log, ``[(case_id, [activity, …]), …]``, when the
    #: table was built by :func:`build`. Turning a DataFrame into this
    #: costs real time — 1.6 s on the 5 600-case claims log, twenty times
    #: what replaying its 164 distinct sequences costs — and every
    #: consumer needs it, so the table carries it rather than making each
    #: one redo the groupby.
    cases: Optional[List[Tuple[str, List[str]]]] = None

    def get(self, sequence: Iterable[str]) -> Optional[SequenceParse]:
        return self.parses.get(tuple(sequence))

    def __len__(self) -> int:
        return len(self.parses)


def build(
    log,
    tree,
    node_ids: Optional[Dict[int, int]] = None,
    coarsen_loops: bool = True,
    progress_callback=None,
    stage: str = "Replaying variants",
) -> ParseTable:
    """Normalise ``log`` once, replay its distinct sequences once, and
    return a table carrying both.

    This is the entry point for a caller that will feed several
    consumers — the normalised log and the parses are each expensive
    enough that repeating either shows up on the clock.
    """
    from .clustering import _normalise_log

    cases = _normalise_log(log)
    table = replay_sequences(
        tree, (seq for _cid, seq in cases), node_ids=node_ids,
        coarsen_loops=coarsen_loops, progress_callback=progress_callback,
        stage=stage,
    )
    table.cases = cases
    return table


def replay_sequences(
    tree,
    sequences: Iterable[Iterable[str]],
    node_ids: Optional[Dict[int, int]] = None,
    coarsen_loops: bool = True,
    progress_callback=None,
    stage: str = "Replaying variants",
    max_replay_states: int = _cs.DEFAULT_MAX_REPLAY_STATES,
) -> ParseTable:
    """Replay each distinct sequence once and return the shared table.

    ``sequences`` may repeat; duplicates are collapsed before any work
    happens. Pass ``node_ids`` when the caller already has one for this
    tree, so the table's keys line up with the caller's own.
    """
    from ....util.progress import Ticker

    if node_ids is None:
        node_ids = _cs.assign_node_ids(tree)
    distinct: List[Tuple[str, ...]] = []
    seen: set = set()
    for seq in sequences:
        key = tuple(seq)
        if key not in seen:
            seen.add(key)
            distinct.append(key)

    table = ParseTable(node_ids=node_ids, coarsen_loops=coarsen_loops)
    ticker = Ticker(progress_callback, stage, len(distinct))
    for key in distinct:
        xor_counts: Dict[int, Dict[int, int]] = {}
        loop_max: Dict[int, int] = {}
        loop_totals: Dict[int, int] = {}
        signature = _cs.replay(
            tree, list(key), node_ids=node_ids,
            coarsen_loops=coarsen_loops,
            xor_branch_counts=xor_counts,
            loop_iter_counts=loop_max,
            loop_total_counts=loop_totals,
            max_replay_states=max_replay_states,
        )
        table.parses[key] = SequenceParse(
            signature=signature,
            xor_branch_counts=xor_counts,
            loop_iter_max=loop_max,
            loop_iter_totals=loop_totals,
        )
        ticker.tick()
    ticker.finish()
    return table
