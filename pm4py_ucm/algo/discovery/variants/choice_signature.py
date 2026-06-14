"""Choice-signature replay on a process tree.

The signature is a canonical, hashable representation of *which choices a
trace makes* when replayed on a process tree:

* ``SEQUENCE`` operators contribute their children's signatures, in order;
* ``XOR`` / ``OR`` operators contribute the chosen branch index plus the
  branch's own signature;
* ``PARALLEL`` operators contribute their children's signatures, **sorted
  canonically** so that ``X-Y-Z`` and ``X-Z-Y`` produce equal signatures
  when ``Y`` and ``Z`` are siblings of the same parallel block;
* ``LOOP`` operators contribute their *coarsened* iteration count
  (``0``, ``1``, or ``2`` for "two or more") under the default
  ``coarsen_loops=True``, or the full per-iteration signature when
  ``coarsen_loops=False``.

Traces that don't replay cleanly on the tree return ``NOFIT``. This
typically means the discovered tree is an over-approximation of the log
(or the log contains rare behaviour the miner filtered out).

The algorithm relies on **alphabet disjointness** at every operator's
children: two sibling subtrees should not share any non-``tau`` activity
label. This holds for trees produced by the Inductive Miner family.

Public entry points
-------------------

:func:`replay`
    Run a single trace against a tree; return its signature or
    :data:`NOFIT`.

:func:`assign_node_ids`
    Walk the tree once and number every node. Required as a one-shot
    preparation step; :func:`replay` accepts the resulting dict so
    signatures across many traces use consistent node IDs.

:func:`partial_order_expression`
    Turn a signature back into a compact human-readable string such as
    ``X -> (Y || Z) -> W^>=2`` for inclusion in a CSV or scenario
    description.

:func:`linearization_count`
    Number of total orders consistent with a signature's partial order —
    a single integer summarising how many sequence-variants the choice
    signature collapses into one concurrency-equivalent variant.
"""
from __future__ import annotations

from math import factorial
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union


# ---------------------------------------------------------------------------
# Operator constants — mirror ``pm4py.objects.process_tree.obj.Operator``.
# Comparing on string ``.value`` lets the module work without importing PM4Py.
# ---------------------------------------------------------------------------

_SEQUENCE = "->"
_XOR = "X"
_OR = "O"
_PARALLEL = "+"
_INTERLEAVING = "o"
_LOOP = "*"

_CHOICE_OPS = {_XOR, _OR}
_PARALLEL_OPS = {_PARALLEL, _INTERLEAVING}


#: Sentinel returned by :func:`replay` when the trace cannot be replayed
#: cleanly on the tree (a model-fitness failure).
NOFIT = "NOFIT"


# ---------------------------------------------------------------------------
# Tree introspection helpers (duck-typed)
# ---------------------------------------------------------------------------

def _op_value(node) -> Optional[str]:
    """Return the operator's ``.value`` string, or ``None`` for leaves."""
    op = getattr(node, "operator", None)
    if op is None:
        return None
    return getattr(op, "value", op)


def _label(node) -> Optional[str]:
    return getattr(node, "label", None)


def _children(node) -> List[Any]:
    return list(getattr(node, "children", None) or [])


def assign_node_ids(tree) -> Dict[int, int]:
    """Number every node in ``tree``. Returns ``{id(node): stable_int}``.

    Stable across calls: traversal is pre-order, and the same Python
    object always lands on the same integer ID."""
    ids: Dict[int, int] = {}
    counter = [0]

    def _walk(node):
        if id(node) in ids:
            return
        ids[id(node)] = counter[0]
        counter[0] += 1
        for c in _children(node):
            _walk(c)

    _walk(tree)
    return ids


def _alphabet(tree, cache: Dict[int, frozenset]) -> frozenset:
    """The set of non-tau activity labels reachable from ``tree``.

    Cached on ``id(tree)`` so repeated walks during replay are cheap."""
    key = id(tree)
    if key in cache:
        return cache[key]
    op = _op_value(tree)
    if op is None:
        lab = _label(tree)
        result = frozenset([lab]) if lab is not None else frozenset()
    else:
        result = frozenset()
        for c in _children(tree):
            result |= _alphabet(c, cache)
    cache[key] = result
    return result


# ---------------------------------------------------------------------------
# Replay — the heart of the algorithm
# ---------------------------------------------------------------------------

def replay(
    tree,
    trace: Sequence[str],
    node_ids: Optional[Dict[int, int]] = None,
    coarsen_loops: bool = True,
    loop_iter_counts: Optional[Dict[int, int]] = None,
    xor_branch_counts: Optional[Dict[int, Dict[int, int]]] = None,
) -> Union[tuple, str]:
    """Replay ``trace`` on ``tree`` and return its canonical signature.

    Parameters
    ----------
    tree
        A pm4py-compatible process tree (or any object exposing the
        ``operator`` / ``label`` / ``children`` attributes).
    trace
        The trace as a sequence of activity-label strings.
    node_ids
        Optional pre-computed mapping ``{id(node): int}`` from
        :func:`assign_node_ids`. When ``None`` a fresh mapping is built
        for this call — pass an explicit one when running many replays
        on the same tree to keep IDs consistent across traces.
    coarsen_loops
        When ``True`` (default), loop signature is the coarsened
        iteration count (``0`` / ``1`` / ``2`` for "2 or more"). When
        ``False``, every iteration contributes its full body signature
        to the result.
    loop_iter_counts
        Optional dict populated during replay. For every LOOP node
        encountered, ``loop_iter_counts[node_id] = actual_iter_count``
        is written **before** coarsening, so callers can recover the
        underlying iteration count even when the signature was
        coarsened for clustering purposes. Pass ``None`` to discard.
    xor_branch_counts
        Optional dict populated during replay. For every XOR/OR node
        encountered, the chosen branch index is counted:
        ``xor_branch_counts[node_id][branch_index] += 1`` per
        evaluation. For an outside-loop XOR this is exactly 1 (the
        variant always picks the same branch); for inside-loop XORs
        it is the number of times *this trace* picked each branch
        across all loop iterations. Used by scenario synthesis to
        distribute branches across iterations via the loop counter
        when the XOR sits inside a loop.

    Returns
    -------
    tuple | str
        A canonical signature (nested tuple) when the trace fits, or
        :data:`NOFIT` (string) when it does not.
    """
    if node_ids is None:
        node_ids = assign_node_ids(tree)
    alpha_cache: Dict[int, frozenset] = {}
    if loop_iter_counts is None:
        loop_iter_counts = {}
    if xor_branch_counts is None:
        xor_branch_counts = {}
    result = _replay(
        list(trace), tree, node_ids, alpha_cache,
        coarsen_loops, loop_iter_counts, xor_branch_counts,
    )
    if result is None:
        return NOFIT
    return result


def _replay(
    window: List[str],
    tree,
    node_ids: Dict[int, int],
    alpha_cache: Dict[int, frozenset],
    coarsen_loops: bool,
    loop_iter_counts: Dict[int, int],
    xor_branch_counts: Dict[int, Dict[int, int]],
) -> Optional[tuple]:
    """Recursive replay worker. Returns ``None`` on NOFIT, a tuple otherwise."""
    op = _op_value(tree)
    nid = node_ids[id(tree)]
    children = _children(tree)

    if op is None:
        return _replay_leaf(window, tree)

    if op == _SEQUENCE:
        return _replay_sequence(
            window, children, nid, node_ids, alpha_cache,
            coarsen_loops, loop_iter_counts, xor_branch_counts,
        )

    if op in _CHOICE_OPS:
        return _replay_xor(
            window, children, nid, node_ids, alpha_cache,
            coarsen_loops, loop_iter_counts, xor_branch_counts,
        )

    if op in _PARALLEL_OPS:
        return _replay_parallel(
            window, children, nid, node_ids, alpha_cache,
            coarsen_loops, loop_iter_counts, xor_branch_counts,
        )

    if op == _LOOP:
        return _replay_loop(
            window, children, nid, node_ids, alpha_cache,
            coarsen_loops, loop_iter_counts, xor_branch_counts,
        )

    # Unknown operator — NOFIT.
    return None


def _replay_leaf(window: List[str], tree) -> Optional[tuple]:
    lab = _label(tree)
    if lab is None:
        # tau — must consume nothing. The empty tuple distinguishes tau
        # from a visible activity in derived analytics (leaf counting,
        # partial-order printing).
        return () if not window else None
    # Visible activity — must consume exactly that one event. Carry the
    # label so derived analytics (linearization count, partial-order
    # expression) can identify it.
    if len(window) == 1 and window[0] == lab:
        return ("ACT", lab)
    return None


def _replay_sequence(
    window: List[str],
    children: List[Any],
    nid: int,
    node_ids: Dict[int, int],
    alpha_cache: Dict[int, frozenset],
    coarsen_loops: bool,
    loop_iter_counts: Dict[int, int],
    xor_branch_counts: Dict[int, Dict[int, int]],
) -> Optional[tuple]:
    if not children:
        return ("SEQ", nid, ()) if not window else None
    fragments = []
    idx = 0
    for c in children:
        alpha = _alphabet(c, alpha_cache)
        peel: List[str] = []
        while idx < len(window) and window[idx] in alpha:
            peel.append(window[idx])
            idx += 1
        frag = _replay(
            peel, c, node_ids, alpha_cache,
            coarsen_loops, loop_iter_counts, xor_branch_counts,
        )
        if frag is None:
            return None
        fragments.append(frag)
    if idx != len(window):
        return None
    return ("SEQ", nid, tuple(fragments))


def _replay_xor(
    window: List[str],
    children: List[Any],
    nid: int,
    node_ids: Dict[int, int],
    alpha_cache: Dict[int, frozenset],
    coarsen_loops: bool,
    loop_iter_counts: Dict[int, int],
    xor_branch_counts: Dict[int, Dict[int, int]],
) -> Optional[tuple]:
    if not children:
        return ("XOR", nid, -1, ()) if not window else None
    if len(children) == 1:
        return _replay(
            window, children[0], node_ids, alpha_cache,
            coarsen_loops, loop_iter_counts, xor_branch_counts,
        )

    window_set = frozenset(window)
    # Empty window must go to the unique tau branch if one exists.
    if not window:
        for i, c in enumerate(children):
            inner = _replay(
                window, c, node_ids, alpha_cache,
                coarsen_loops, loop_iter_counts, xor_branch_counts,
            )
            if inner is not None:
                _record_xor_choice(xor_branch_counts, nid, i)
                return ("XOR", nid, i, inner)
        return None

    # Visible activities — pick the branch whose alphabet covers them.
    for i, c in enumerate(children):
        calpha = _alphabet(c, alpha_cache)
        if not window_set.issubset(calpha):
            continue
        inner = _replay(
            window, c, node_ids, alpha_cache,
            coarsen_loops, loop_iter_counts, xor_branch_counts,
        )
        if inner is not None:
            _record_xor_choice(xor_branch_counts, nid, i)
            return ("XOR", nid, i, inner)
    return None


def _record_xor_choice(
    xor_branch_counts: Dict[int, Dict[int, int]],
    nid: int,
    branch: int,
) -> None:
    """Bump ``xor_branch_counts[nid][branch]`` by 1. The same XOR may
    be visited multiple times inside a loop body/redo; the count is
    the total across every visit in this single trace."""
    by_branch = xor_branch_counts.setdefault(nid, {})
    by_branch[branch] = by_branch.get(branch, 0) + 1


def _replay_parallel(
    window: List[str],
    children: List[Any],
    nid: int,
    node_ids: Dict[int, int],
    alpha_cache: Dict[int, frozenset],
    coarsen_loops: bool,
    loop_iter_counts: Dict[int, int],
    xor_branch_counts: Dict[int, Dict[int, int]],
) -> Optional[tuple]:
    if not children:
        return ("PAR", nid, ()) if not window else None
    if len(children) == 1:
        return _replay(
            window, children[0], node_ids, alpha_cache,
            coarsen_loops, loop_iter_counts, xor_branch_counts,
        )

    # Project the window onto each child's alphabet. The IM-disjointness
    # assumption guarantees each event belongs to at most one child.
    projections: List[List[str]] = [[] for _ in children]
    for e in window:
        placed = False
        for i, c in enumerate(children):
            if e in _alphabet(c, alpha_cache):
                projections[i].append(e)
                placed = True
                break
        if not placed:
            # Event not in any branch's alphabet — NOFIT.
            return None

    sub_fragments = []
    for c, proj in zip(children, projections):
        frag = _replay(
            proj, c, node_ids, alpha_cache,
            coarsen_loops, loop_iter_counts, xor_branch_counts,
        )
        if frag is None:
            return None
        sub_fragments.append(frag)

    # CANONICALIZE — sort by repr so that interleaving-equivalent traces
    # produce equal signatures regardless of original child order.
    sub_fragments.sort(key=repr)
    return ("PAR", nid, tuple(sub_fragments))


def _replay_loop(
    window: List[str],
    children: List[Any],
    nid: int,
    node_ids: Dict[int, int],
    alpha_cache: Dict[int, frozenset],
    coarsen_loops: bool,
    loop_iter_counts: Dict[int, int],
    xor_branch_counts: Dict[int, Dict[int, int]],
) -> Optional[tuple]:
    # PM4Py loops have two children: do (body) and redo. Semantics:
    #     do (redo do)*
    # Iteration count = number of body executions.
    if len(children) < 2:
        if children:
            return _replay(
                window, children[0], node_ids, alpha_cache,
                coarsen_loops, loop_iter_counts, xor_branch_counts,
            )
        return ("LOOP", nid, 0) if not window else None

    do_tree, redo_tree = children[0], children[1]
    do_alpha = _alphabet(do_tree, alpha_cache)
    redo_alpha = _alphabet(redo_tree, alpha_cache)

    # Pathological case: empty do alphabet means the body is tau. Treat
    # a non-empty window as NOFIT — we can't otherwise terminate.
    if not do_alpha and not redo_alpha and window:
        return None

    iter_fragments: List[tuple] = []
    idx = 0

    def _peel(target_alpha: frozenset) -> List[str]:
        nonlocal idx
        out: List[str] = []
        while idx < len(window) and window[idx] in target_alpha:
            out.append(window[idx])
            idx += 1
        return out

    # First body iteration (mandatory).
    do_peel = _peel(do_alpha)
    do_frag = _replay(
        do_peel, do_tree, node_ids, alpha_cache,
        coarsen_loops, loop_iter_counts, xor_branch_counts,
    )
    if do_frag is None:
        return None
    iter_fragments.append(do_frag)

    # Subsequent (redo, body) pairs.
    while idx < len(window):
        prev_idx = idx
        redo_peel = _peel(redo_alpha)
        redo_frag = _replay(
            redo_peel, redo_tree, node_ids, alpha_cache,
            coarsen_loops, loop_iter_counts, xor_branch_counts,
        )
        if redo_frag is None:
            return None
        do_peel = _peel(do_alpha)
        do_frag = _replay(
            do_peel, do_tree, node_ids, alpha_cache,
            coarsen_loops, loop_iter_counts, xor_branch_counts,
        )
        if do_frag is None:
            return None
        iter_fragments.append(do_frag)
        if idx == prev_idx:
            # No progress — bail to avoid an infinite loop on tau bodies.
            return None

    if idx != len(window):
        return None

    iter_count = len(iter_fragments)
    # Record the actual (pre-coarsening) iteration count so callers can
    # initialise per-variant loop counter values without re-replaying
    # with coarsen_loops=False.
    loop_iter_counts[nid] = max(
        loop_iter_counts.get(nid, 0), iter_count,
    )
    if coarsen_loops:
        coarse = 0 if iter_count == 0 else (1 if iter_count == 1 else 2)
        return ("LOOP", nid, coarse)
    return ("LOOP_FINE", nid, iter_count, tuple(iter_fragments))


# ---------------------------------------------------------------------------
# Derived artifacts from a signature
# ---------------------------------------------------------------------------

def partial_order_expression(
    signature: tuple,
    tree,
    node_ids: Dict[int, int],
) -> str:
    """Render a signature as a compact partial-order expression.

    Uses ``->`` for sequence, ``||`` for parallel, ``branch_i`` for XOR
    choices, ``LOOP^k`` for coarsened loops, and the activity label for
    leaves. Designed for inclusion in CSV cells and scenario
    descriptions; not a formal grammar."""
    id_to_node = {nid: None for nid in node_ids.values()}

    def _build(node):
        nid = node_ids[id(node)]
        id_to_node[nid] = node

    _walk_assign(tree, _build)

    def _render(sig: tuple) -> str:
        if not sig:
            return "tau"
        tag = sig[0]
        if tag == "ACT":
            return sig[1]
        if tag == "SEQ":
            _, nid, frags = sig
            parts = [_render(f) for f in frags if f]
            parts = [p for p in parts if p and p != "tau"]
            if not parts:
                return "tau"
            return " -> ".join(parts)
        if tag == "XOR":
            _, nid, choice_idx, inner = sig
            inner_str = _render(inner) if inner else "tau"
            return f"[{inner_str}]"
        if tag == "PAR":
            _, nid, frags = sig
            parts = [_render(f) for f in frags]
            parts = [p for p in parts if p and p != "tau"]
            if not parts:
                return "tau"
            return "(" + " || ".join(parts) + ")"
        if tag == "LOOP":
            _, nid, coarse = sig
            node = id_to_node.get(nid)
            body_label = "body"
            if node is not None:
                body_children = _children(node)
                if body_children:
                    body_label = _leaf_summary(body_children[0])
            suffix = {0: "^0", 1: "", 2: "^>=2"}[coarse]
            return f"({body_label}){suffix}"
        if tag == "LOOP_FINE":
            _, nid, iter_count, iter_frags = sig
            parts = [_render(f) for f in iter_frags]
            return "(" + " -> ".join(parts) + ")"
        return repr(sig)

    return _render(signature)


def _walk_assign(tree, fn) -> None:
    fn(tree)
    for c in _children(tree):
        _walk_assign(c, fn)


def _leaf_summary(tree) -> str:
    """One-token name for an arbitrary subtree, used in partial-order
    expressions when we just need a short reference (loop bodies)."""
    op = _op_value(tree)
    if op is None:
        lab = _label(tree)
        return lab if lab is not None else "tau"
    return {
        _SEQUENCE: "seq", _XOR: "xor", _OR: "or",
        _PARALLEL: "par", _INTERLEAVING: "interleaving",
        _LOOP: "loop",
    }.get(op, "subtree")


def linearization_count(signature: tuple) -> int:
    """Number of total orders consistent with a signature's partial order.

    For a sequence of subtrees: product of children's counts.
    For a parallel block of subtrees of sizes ``s_1, ..., s_k``:
        ``multinomial(s_1, ..., s_k) * product(child_counts)``.
    For XOR: the chosen branch's count.
    For LOOP coarsened: 1 (we lost iteration detail). For LOOP_FINE: the
    product of per-iteration counts.

    Numbers can grow large; callers may want to cap with a log scale
    for display."""
    if not signature:
        return 1
    tag = signature[0]
    if tag == "SEQ":
        prod = 1
        for f in signature[2]:
            prod *= linearization_count(f)
        return prod
    if tag == "XOR":
        return linearization_count(signature[3]) if signature[3] else 1
    if tag == "PAR":
        sizes = [_leaf_count(f) for f in signature[2]]
        total = sum(sizes)
        # Multinomial = total! / product(size_i!)
        mult = factorial(total)
        for s in sizes:
            mult //= factorial(s) if s > 0 else 1
        prod = 1
        for f in signature[2]:
            prod *= linearization_count(f)
        return mult * prod
    if tag == "LOOP":
        return 1
    if tag == "LOOP_FINE":
        prod = 1
        for f in signature[3]:
            prod *= linearization_count(f)
        return prod
    return 1


def _leaf_count(signature: tuple) -> int:
    """Number of visible activity leaves in a signature — used as the
    weight in a multinomial linearization-count calculation. Tau leaves
    (empty tuples) and pure structural nodes contribute zero."""
    if not signature:
        return 0
    tag = signature[0]
    if tag == "ACT":
        return 1
    if tag == "SEQ":
        return sum(_leaf_count(f) for f in signature[2])
    if tag == "XOR":
        return _leaf_count(signature[3]) if signature[3] else 0
    if tag == "PAR":
        return sum(_leaf_count(f) for f in signature[2])
    if tag == "LOOP":
        # Coarsened — we don't know exact contents. Approximate as 1 to
        # avoid contaminating the multinomial.
        return 1
    if tag == "LOOP_FINE":
        return sum(_leaf_count(f) for f in signature[3])
    return 0


def collect_xor_choices(signature: tuple) -> Dict[int, int]:
    """Return ``{tree_xor_node_id: chosen_branch_index}`` for every XOR
    node encountered in ``signature``.

    Used by the scenario synthesizer to emit ``variant_id == "v_i"``
    conditions on the OR-fork outgoing connections that correspond to
    XOR nodes outside any LOOP (within-loop choices are not stable per
    variant and so are left as the default ``true`` condition)."""
    out: Dict[int, int] = {}

    def _walk(sig):
        if not sig:
            return
        tag = sig[0]
        if tag == "SEQ":
            for f in sig[2]:
                _walk(f)
        elif tag == "XOR":
            _, nid, choice_idx, inner = sig
            out[nid] = choice_idx
            _walk(inner)
        elif tag == "PAR":
            for f in sig[2]:
                _walk(f)
        elif tag == "LOOP":
            # Coarsened — no inner choices captured.
            pass
        elif tag == "LOOP_FINE":
            for f in sig[3]:
                _walk(f)

    _walk(signature)
    return out
