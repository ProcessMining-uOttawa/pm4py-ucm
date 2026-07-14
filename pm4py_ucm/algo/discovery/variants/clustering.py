"""Cluster an event log by concurrency-aware choice signatures.

Top-level entry point :func:`cluster` takes a log (a pm4py
``EventLog``-like object, a pandas ``DataFrame``, or a plain list of
``(case_id, [activities])`` tuples) and a process tree, and returns a
:class:`ClusteringResult` describing:

* the list of named variants (``v1``, ``v2``, …), each carrying the case
  IDs that fall into it, its choice signature, its partial-order
  expression, and a linearization count;
* the list of *non-conforming* case IDs whose traces could not be
  replayed cleanly on the tree;
* summary statistics — the **fitness percentage**, the
  sequence-variant vs choice-signature-variant counts, and the
  compression ratio between them. These numbers are the empirical
  payload of the scenario-synthesis pipeline.

The clustering is a hard partition: every case lives in exactly one
variant or in the noise bucket. Variants are sorted by descending
frequency before numbering, so ``v1`` is always the modal variant.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import choice_signature as _cs


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class Variant:
    """One concurrency-equivalent variant cluster.

    Attributes
    ----------
    variant_id
        Stable name (``"v1"``, ``"v2"``, …) assigned by
        :func:`cluster` in descending-frequency order.
    signature
        The canonical choice signature shared by every case in this
        cluster.
    case_ids
        Hard list of case IDs that fell into this cluster.
    frequency
        ``len(case_ids)`` — repeated as a field for convenience.
    partial_order_expression
        Compact textual partial-order rendering (e.g.
        ``"X -> (Y || Z)"``). Useful for the variant table and for
        :class:`UCM.ScenarioDef.description`.
    linearization_count
        Number of total orders consistent with this partial order; in
        other words, the number of sequence-variants that fold into
        this one concurrency-variant.
    sequence_variants
        How many *distinct sequence variants* (= distinct activity
        sequences) actually appeared in the log for this cluster.
        Always ``<= linearization_count``; the gap is "interleavings
        the model admits but the log never exhibited".
    loop_iteration_max
        For each LOOP tree node id encountered by traces in this
        variant, the maximum body-iteration count observed. Used by
        the scenario synthesizer to initialise the per-loop integer
        counter variable so the loop runs the right number of times.
        Empty when the variant traverses no loops.
    xor_branch_totals
        ``{xor_tree_node_id: {branch_idx: total_count_across_traces}}``.
        For an outside-loop XOR, ``total_count`` is just the number
        of cases in the cluster that picked that branch (and only
        one branch has a non-zero count). For an inside-loop XOR,
        it is the total number of evaluations summed across every
        trace's iterations — the scenario synthesizer uses the ratio
        ``n_branch / sum_branches`` to distribute branches across
        loop iterations via the loop counter, so a variant whose
        traces took branch 0 60% of the time and branch 1 40% gets
        roughly that split inside its scenario.
    """

    variant_id: str
    signature: tuple
    case_ids: List[str]
    frequency: int
    partial_order_expression: str
    linearization_count: int
    sequence_variants: int
    loop_iteration_max: Dict[int, int] = field(default_factory=dict)
    xor_branch_totals: Dict[int, Dict[int, int]] = field(default_factory=dict)


@dataclass
class ClusteringResult:
    """Aggregate output of :func:`cluster`.

    Carries enough information to drive scenario synthesis, write the
    case-to-variant CSV report, and report the empirical headline
    numbers for the paper."""

    variants: List[Variant]
    noise_case_ids: List[str]
    sequence_variant_count: int
    total_cases: int

    @property
    def fitness_percentage(self) -> float:
        """Fraction of cases that replayed cleanly on the tree, in
        ``[0, 1]``. ``1 - fitness_percentage`` is the noise share."""
        if self.total_cases == 0:
            return 1.0
        fit = self.total_cases - len(self.noise_case_ids)
        return fit / self.total_cases

    @property
    def compression_ratio(self) -> float:
        """How much the concurrency-aware clustering shrinks the
        variant count vs naive sequence variants.

        ``1.0`` means no compression (every sequence variant is its own
        cluster); ``< 1.0`` is a reduction (lower is better). Returns
        ``1.0`` for empty results."""
        if self.sequence_variant_count == 0:
            return 1.0
        return len(self.variants) / self.sequence_variant_count


# ---------------------------------------------------------------------------
# Log normalisation
# ---------------------------------------------------------------------------

def _normalise_log(log: Any) -> List[Tuple[str, List[str]]]:
    """Coerce ``log`` to a list of ``(case_id, [activities])`` tuples.

    Accepts:

    * a list of ``(case_id, [activities])`` tuples — returned as-is;
    * a list of ``[activity]`` lists — each gets a synthetic
      ``"case_<i>"`` ID;
    * a pm4py ``EventLog`` — case IDs are taken from
      ``trace.attributes["concept:name"]`` if present, else synthetic;
    * a pandas ``DataFrame`` with ``case:concept:name`` / ``concept:name``
      / ``case_id`` / ``activity`` columns — auto-detected. The frame is
      sorted by timestamp (``time:timestamp`` or ``timestamp``) within
      each case if such a column exists."""
    # pandas DataFrame
    if hasattr(log, "columns") and hasattr(log, "iterrows"):
        return _normalise_dataframe(log)
    # pm4py EventLog: iterable of trace objects
    if hasattr(log, "__iter__"):
        out: List[Tuple[str, List[str]]] = []
        for i, trace in enumerate(log):
            # pm4py Trace
            if hasattr(trace, "attributes") and hasattr(trace, "_list"):
                case_id = str(trace.attributes.get("concept:name", f"case_{i}"))
                acts = [
                    str(ev.get("concept:name", "")) for ev in trace
                ]
                out.append((case_id, acts))
                continue
            # Tuple
            if isinstance(trace, tuple) and len(trace) == 2:
                cid, acts = trace
                out.append((str(cid), [str(a) for a in acts]))
                continue
            # Plain list of activities
            if isinstance(trace, (list, tuple)):
                out.append((f"case_{i}", [str(a) for a in trace]))
                continue
            raise TypeError(
                f"Unrecognised trace shape at index {i}: {type(trace).__name__}"
            )
        return out
    raise TypeError(f"Unrecognised log shape: {type(log).__name__}")


def _normalise_dataframe(df) -> List[Tuple[str, List[str]]]:
    cols = list(df.columns)
    case_col = next(
        (c for c in ("case:concept:name", "case_id", "case", "CaseID") if c in cols),
        None,
    )
    act_col = next(
        (c for c in ("concept:name", "activity", "Activity", "event") if c in cols),
        None,
    )
    ts_col = next(
        (c for c in ("time:timestamp", "timestamp", "Timestamp") if c in cols),
        None,
    )
    if case_col is None or act_col is None:
        raise ValueError(
            "DataFrame must have case + activity columns (case:concept:name / "
            "concept:name, or case_id / activity)."
        )
    work = df[[case_col, act_col] + ([ts_col] if ts_col else [])].copy()
    if ts_col:
        work = work.sort_values([case_col, ts_col], kind="stable")
    out: List[Tuple[str, List[str]]] = []
    for cid, group in work.groupby(case_col, sort=False):
        out.append((str(cid), [str(a) for a in group[act_col].tolist()]))
    return out


# ---------------------------------------------------------------------------
# Cluster driver
# ---------------------------------------------------------------------------

def cluster(
    log: Any,
    tree,
    coarsen_loops: bool = True,
    progress_callback=None,
) -> ClusteringResult:
    """Replay every case in ``log`` on ``tree`` and group by signature.

    Parameters
    ----------
    log
        Event log in any of the forms accepted by :func:`_normalise_log`.
    tree
        A pm4py process tree (or any duck-type with ``operator`` /
        ``children`` / ``label``).
    coarsen_loops
        Passed straight through to :func:`choice_signature.replay`.
        ``True`` (default) collapses loop iterations to
        ``{0, 1, >=2}`` before clustering. Pass ``False`` to keep every
        loop iteration's internal choices distinct — useful for the
        loop-sensitivity row of the empirical table.
    progress_callback
        Optional ``callback(stage, done, total)`` fired per replayed
        case (throttled) — replay is the pipeline's dominant cost on
        large logs. See :mod:`pm4py_ucm.util.progress`.

    Returns
    -------
    ClusteringResult
        Hard partition over conforming cases, plus a noise bucket and
        summary statistics.
    """
    from ....util.progress import Ticker

    cases = _normalise_log(log)
    node_ids = _cs.assign_node_ids(tree)
    ticker = Ticker(progress_callback, "Replaying cases", len(cases))

    # First pass — replay every trace, bucket by signature.
    buckets: Dict[tuple, List[str]] = {}
    bucket_sequences: Dict[tuple, set] = {}
    bucket_loop_max: Dict[tuple, Dict[int, int]] = {}
    bucket_xor_totals: Dict[tuple, Dict[int, Dict[int, int]]] = {}
    noise: List[str] = []
    sequence_variants_seen: set = set()
    for case_id, trace in cases:
        seq_key = tuple(trace)
        sequence_variants_seen.add(seq_key)
        trace_loop_iters: Dict[int, int] = {}
        trace_xor_counts: Dict[int, Dict[int, int]] = {}
        signature = _cs.replay(
            tree, trace, node_ids=node_ids, coarsen_loops=coarsen_loops,
            loop_iter_counts=trace_loop_iters,
            xor_branch_counts=trace_xor_counts,
        )
        if signature == _cs.NOFIT:
            noise.append(case_id)
            ticker.tick()
            continue
        buckets.setdefault(signature, []).append(case_id)
        bucket_sequences.setdefault(signature, set()).add(seq_key)
        # Track per-cluster max loop iteration count for every LOOP node
        # this trace traversed. The scenario synthesizer initialises the
        # integer counter to this max so the loop runs at least as many
        # times as the heaviest trace in the cluster.
        cluster_loops = bucket_loop_max.setdefault(signature, {})
        for loop_nid, ic in trace_loop_iters.items():
            cluster_loops[loop_nid] = max(cluster_loops.get(loop_nid, 0), ic)
        # Track per-cluster per-XOR branch totals across traces. We
        # use the SUM (not max) so the ratio of per-branch counts
        # reflects the variant's empirical mix of branch choices —
        # which the synthesizer turns into iteration ranges via the
        # loop counter.
        cluster_xor = bucket_xor_totals.setdefault(signature, {})
        for xor_nid, branch_counts in trace_xor_counts.items():
            existing = cluster_xor.setdefault(xor_nid, {})
            for branch, cnt in branch_counts.items():
                existing[branch] = existing.get(branch, 0) + cnt
        ticker.tick()
    ticker.finish()

    # Second pass — order buckets by frequency, assign v1, v2, ...,
    # compute derived artifacts.
    ordered = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), repr(kv[0])))
    variants: List[Variant] = []
    for i, (sig, case_ids) in enumerate(ordered):
        try:
            po_expr = _cs.partial_order_expression(sig, tree, node_ids)
        except Exception:
            # Don't let pretty-printing errors break the pipeline.
            po_expr = repr(sig)
        try:
            lin_count = _cs.linearization_count(sig)
        except Exception:
            lin_count = 0
        variants.append(Variant(
            variant_id=f"v{i + 1}",
            signature=sig,
            case_ids=list(case_ids),
            frequency=len(case_ids),
            partial_order_expression=po_expr,
            linearization_count=lin_count,
            sequence_variants=len(bucket_sequences.get(sig, set())),
            loop_iteration_max=dict(bucket_loop_max.get(sig, {})),
            xor_branch_totals={
                xor_nid: dict(branches)
                for xor_nid, branches in bucket_xor_totals.get(sig, {}).items()
            },
        ))

    return ClusteringResult(
        variants=variants,
        noise_case_ids=noise,
        sequence_variant_count=len(sequence_variants_seen),
        total_cases=len(cases),
    )
