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

#: Default per-trace budget for the replay's backtracking search.
#: The greedy peel used in the flat case bites at most ``depth`` states
#: per trace; the backtracking path is bounded by this cap so a
#: pathologically ambiguous tree (Inductive-Miner output on a complex
#: real-world log with heavy alphabet overlap) can't stall a batch
#: replay. Traces that exceed the budget are reported as ``NOFIT``.
#: Chosen conservatively — well above the greedy path (~O(depth)) but
#: well below "run forever". Raise via ``max_replay_states`` if you see
#: legitimately fitting traces silently dropped as noise.
DEFAULT_MAX_REPLAY_STATES = 200_000


def replay(
    tree,
    trace: Sequence[str],
    node_ids: Optional[Dict[int, int]] = None,
    coarsen_loops: bool = True,
    loop_iter_counts: Optional[Dict[int, int]] = None,
    xor_branch_counts: Optional[Dict[int, Dict[int, int]]] = None,
    max_replay_states: int = DEFAULT_MAX_REPLAY_STATES,
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
    max_replay_states
        Cap on the number of subtree-replay attempts consumed by this
        one call before giving up. Backtracking on ambiguous trees
        (see below) can otherwise blow up combinatorially; the cap
        is a safety net. Defaults to
        :data:`DEFAULT_MAX_REPLAY_STATES` (200 000, well above the
        greedy peel's needs but far below unbounded).

    Returns
    -------
    tuple | str
        A canonical signature (nested tuple) when the trace fits, or
        :data:`NOFIT` (string) when it does not.

    Notes
    -----
    The replay used to be a pure greedy alphabet peel: at every
    sequence / loop / XOR boundary, take the *maximum* prefix of
    events whose labels are in the child's alphabet, then commit to
    that child. On Inductive-Miner trees with **disjoint** sibling
    alphabets this is correct and fast. On real-world logs — like
    BPI Challenge 2012 where the discovered tree wraps a large
    XOR/loop nest whose ``do`` and ``redo`` share their whole
    alphabet — the greedy commit either happens to succeed (all
    traces collapse to one variant) or silently drops the trace as
    ``NOFIT``.

    The current implementation is a **first-match backtracking**
    search: at every sequence / loop / XOR site it *first* tries the
    greedy peel (preserving the flat-case semantics that the existing
    test suite pins), and on failure retries with shorter peels or
    alternative children. It stops at the first fitting parse — so
    signatures are still deterministic — and is capped by the
    ``max_replay_states`` budget so a pathological tree can't stall
    a batch replay.
    """
    if node_ids is None:
        node_ids = assign_node_ids(tree)
    alpha_cache: Dict[int, frozenset] = {}
    if loop_iter_counts is None:
        loop_iter_counts = {}
    if xor_branch_counts is None:
        xor_branch_counts = {}
    budget = [max(1, int(max_replay_states))]
    # Per-trace memoisation: many subtrees are consulted with identical
    # ``(window, subtree)`` pairs during backtracking (e.g. every failed
    # loop iteration retries the same inner subtree on the same peel).
    # Caching the first-fit parse per pair collapses the exponential
    # blowup back into polynomial time for real-world logs like
    # BPI 2012 without changing the search semantics (we still return
    # the first fit under the same greedy-first ordering).
    trace_list = list(trace)
    # Index-based memo: keyed on (tree python id, start, end). O(1) hash
    # lookup vs O(N) hashing of a fresh tuple(window) each call, which
    # was dominating replay time on real-world logs like BPI 2012.
    memo: Dict[Tuple[int, int, int], Optional[_Parse]] = {}
    parse = _replay(
        trace_list, 0, len(trace_list), tree, node_ids, alpha_cache,
        coarsen_loops, budget, memo,
    )
    if parse is None:
        return NOFIT
    frag, loop_delta, xor_delta = parse
    # Merge deltas of the WINNING parse into the caller's dicts.
    # Backtracking discards deltas of every rejected attempt.
    for lid, ic in loop_delta.items():
        loop_iter_counts[lid] = max(loop_iter_counts.get(lid, 0), ic)
    for xid, by_branch in xor_delta.items():
        acc = xor_branch_counts.setdefault(xid, {})
        for b, c in by_branch.items():
            acc[b] = acc.get(b, 0) + c
    return frag


# Internal parse result: ``(fragment, loop_delta, xor_delta)`` where the
# deltas are the counter updates this particular parse would contribute
# to the caller-side ``loop_iter_counts`` / ``xor_branch_counts`` dicts.
# The public :func:`replay` merges them only for the winning parse; any
# rejected attempt is discarded intact.
_Delta = Tuple[Dict[int, int], Dict[int, Dict[int, int]]]
_Parse = Tuple[tuple, Dict[int, int], Dict[int, Dict[int, int]]]


def _merge_loop(a: Dict[int, int], b: Dict[int, int]) -> Dict[int, int]:
    if not a:
        return dict(b)
    if not b:
        return dict(a)
    out = dict(a)
    for k, v in b.items():
        out[k] = max(out.get(k, 0), v)
    return out


def _merge_xor(
    a: Dict[int, Dict[int, int]], b: Dict[int, Dict[int, int]],
) -> Dict[int, Dict[int, int]]:
    if not a:
        return {k: dict(v) for k, v in b.items()}
    if not b:
        return {k: dict(v) for k, v in a.items()}
    out = {k: dict(v) for k, v in a.items()}
    for k, by_branch in b.items():
        acc = out.setdefault(k, {})
        for br, cnt in by_branch.items():
            acc[br] = acc.get(br, 0) + cnt
    return out


_MemoKey = Tuple[int, int, int]
_Memo = Dict[_MemoKey, Optional[_Parse]]


def _replay(
    trace: List[str],
    s: int,
    e: int,
    tree,
    node_ids: Dict[int, int],
    alpha_cache: Dict[int, frozenset],
    coarsen_loops: bool,
    budget: List[int],
    memo: _Memo,
) -> Optional[_Parse]:
    """Recursive replay worker for ``trace[s:e]`` on ``tree``.

    Returns ``None`` when the ``(window, tree)`` pair is unparseable
    under any peel choice, or the state budget was exhausted. Otherwise
    returns the first-fitting ``(fragment, loop_delta, xor_delta)`` parse.

    The whole trace is passed by reference; ``s`` / ``e`` are absolute
    positions inside it. The memo key is therefore ``(id(tree), s, e)`` —
    O(1) hash — which keeps the memo lookup fast enough for real-world
    logs with heavy backtracking (BPI 2012 style).
    """
    if budget[0] <= 0:
        return None
    key = (id(tree), s, e)
    if key in memo:
        return memo[key]
    budget[0] -= 1
    op = _op_value(tree)
    nid = node_ids[id(tree)]
    children = _children(tree)

    if op is None:
        result = _replay_leaf(trace, s, e, tree)
    elif op == _SEQUENCE:
        result = _replay_sequence(
            trace, s, e, children, nid, node_ids, alpha_cache,
            coarsen_loops, budget, memo,
        )
    elif op in _CHOICE_OPS:
        result = _replay_xor(
            trace, s, e, children, nid, node_ids, alpha_cache,
            coarsen_loops, budget, memo,
        )
    elif op in _PARALLEL_OPS:
        result = _replay_parallel(
            trace, s, e, children, nid, node_ids, alpha_cache,
            coarsen_loops, budget, memo,
        )
    elif op == _LOOP:
        result = _replay_loop(
            trace, s, e, children, nid, node_ids, alpha_cache,
            coarsen_loops, budget, memo,
        )
    else:
        result = None

    # Only memoise fully-resolved results. If the budget was exhausted
    # mid-search we return None but do NOT cache it.
    if budget[0] > 0:
        memo[key] = result
    return result


def _replay_leaf(
    trace: List[str], s: int, e: int, tree,
) -> Optional[_Parse]:
    lab = _label(tree)
    if lab is None:
        # tau — must consume nothing.
        if s == e:
            return ((), {}, {})
        return None
    if e - s == 1 and trace[s] == lab:
        return (("ACT", lab), {}, {})
    return None


def _replay_sequence(
    trace: List[str],
    s: int,
    e: int,
    children: List[Any],
    nid: int,
    node_ids: Dict[int, int],
    alpha_cache: Dict[int, frozenset],
    coarsen_loops: bool,
    budget: List[int],
    memo: _Memo,
) -> Optional[_Parse]:
    if not children:
        return (("SEQ", nid, ()), {}, {}) if s == e else None
    result = _seq_split_first(
        trace, s, e, s, 0, children, node_ids, alpha_cache,
        coarsen_loops, budget, memo,
    )
    if result is None:
        return None
    frags, loop_d, xor_d = result
    return (("SEQ", nid, frags), loop_d, xor_d)


def _seq_split_first(
    trace: List[str],
    s: int,
    e: int,
    pos: int,
    ch_idx: int,
    children: List[Any],
    node_ids: Dict[int, int],
    alpha_cache: Dict[int, frozenset],
    coarsen_loops: bool,
    budget: List[int],
    memo: _Memo,
) -> Optional[Tuple[Tuple[tuple, ...], Dict[int, int], Dict[int, Dict[int, int]]]]:
    """Consume ``children[ch_idx:]`` over ``trace[pos:e]``.

    Peel lengths greedy-first (longest alphabet-compatible prefix),
    then progressively shorter down to zero.
    """
    if ch_idx == len(children):
        if pos == e:
            return ((), {}, {})
        return None
    child = children[ch_idx]
    is_last = ch_idx == len(children) - 1
    if is_last:
        r = _replay(
            trace, pos, e, child, node_ids, alpha_cache,
            coarsen_loops, budget, memo,
        )
        if r is None:
            return None
        frag, loop_d, xor_d = r
        return ((frag,), loop_d, xor_d)
    child_alpha = _alphabet(child, alpha_cache)
    max_end = pos
    while max_end < e and trace[max_end] in child_alpha:
        max_end += 1
    for end in range(max_end, pos - 1, -1):
        if budget[0] <= 0:
            return None
        r = _replay(
            trace, pos, end, child, node_ids, alpha_cache,
            coarsen_loops, budget, memo,
        )
        if r is None:
            continue
        frag, loop_d, xor_d = r
        tail = _seq_split_first(
            trace, s, e, end, ch_idx + 1, children, node_ids, alpha_cache,
            coarsen_loops, budget, memo,
        )
        if tail is None:
            continue
        tail_frags, tail_loop_d, tail_xor_d = tail
        return (
            (frag,) + tail_frags,
            _merge_loop(loop_d, tail_loop_d),
            _merge_xor(xor_d, tail_xor_d),
        )
    return None


def _replay_xor(
    trace: List[str],
    s: int,
    e: int,
    children: List[Any],
    nid: int,
    node_ids: Dict[int, int],
    alpha_cache: Dict[int, frozenset],
    coarsen_loops: bool,
    budget: List[int],
    memo: _Memo,
) -> Optional[_Parse]:
    if not children:
        return (("XOR", nid, -1, ()), {}, {}) if s == e else None
    if len(children) == 1:
        r = _replay(
            trace, s, e, children[0], node_ids, alpha_cache,
            coarsen_loops, budget, memo,
        )
        if r is None:
            return None
        inner_frag, loop_d, xor_d = r
        return (("XOR", nid, 0, inner_frag), loop_d, _bump_xor(xor_d, nid, 0))

    # Empty window must go to a branch that consumes nothing (tau branch).
    if s == e:
        for i, c in enumerate(children):
            r = _replay(
                trace, s, e, c, node_ids, alpha_cache,
                coarsen_loops, budget, memo,
            )
            if r is not None:
                inner_frag, loop_d, xor_d = r
                return (
                    ("XOR", nid, i, inner_frag),
                    loop_d,
                    _bump_xor(xor_d, nid, i),
                )
        return None

    window_set = frozenset(trace[s:e])
    for i, c in enumerate(children):
        calpha = _alphabet(c, alpha_cache)
        if not window_set.issubset(calpha):
            continue
        r = _replay(
            trace, s, e, c, node_ids, alpha_cache,
            coarsen_loops, budget, memo,
        )
        if r is None:
            continue
        inner_frag, loop_d, xor_d = r
        return (("XOR", nid, i, inner_frag), loop_d, _bump_xor(xor_d, nid, i))
    return None


def _bump_xor(
    xor_d: Dict[int, Dict[int, int]], nid: int, branch: int,
) -> Dict[int, Dict[int, int]]:
    out = {k: dict(v) for k, v in xor_d.items()}
    acc = out.setdefault(nid, {})
    acc[branch] = acc.get(branch, 0) + 1
    return out


def _replay_parallel(
    trace: List[str],
    s: int,
    e: int,
    children: List[Any],
    nid: int,
    node_ids: Dict[int, int],
    alpha_cache: Dict[int, frozenset],
    coarsen_loops: bool,
    budget: List[int],
    memo: _Memo,
) -> Optional[_Parse]:
    if not children:
        return (("PAR", nid, ()), {}, {}) if s == e else None
    if len(children) == 1:
        return _replay(
            trace, s, e, children[0], node_ids, alpha_cache,
            coarsen_loops, budget, memo,
        )

    # Project the window onto each child's alphabet. Materialise each
    # projection as its own list so per-child replays can share the memo
    # via ``id()`` on the sub-list (see below note).
    projections: List[List[str]] = [[] for _ in children]
    for j in range(s, e):
        ev = trace[j]
        placed = False
        for i, c in enumerate(children):
            if ev in _alphabet(c, alpha_cache):
                projections[i].append(ev)
                placed = True
                break
        if not placed:
            return None

    sub_fragments: List[tuple] = []
    merged_loop: Dict[int, int] = {}
    merged_xor: Dict[int, Dict[int, int]] = {}
    for c, proj in zip(children, projections):
        # Parallel projections are fresh lists — replay them against the
        # child subtree with their own [0, len(proj)] window. This does
        # NOT share the outer trace's memo (different list identity), but
        # parallel projections are rare in practice and typically small.
        r = _replay(
            proj, 0, len(proj), c, node_ids, alpha_cache,
            coarsen_loops, budget, memo,
        )
        if r is None:
            return None
        frag, loop_d, xor_d = r
        sub_fragments.append(frag)
        merged_loop = _merge_loop(merged_loop, loop_d)
        merged_xor = _merge_xor(merged_xor, xor_d)

    sub_fragments.sort(key=repr)
    return (("PAR", nid, tuple(sub_fragments)), merged_loop, merged_xor)


def _replay_loop(
    trace: List[str],
    s: int,
    e: int,
    children: List[Any],
    nid: int,
    node_ids: Dict[int, int],
    alpha_cache: Dict[int, frozenset],
    coarsen_loops: bool,
    budget: List[int],
    memo: _Memo,
) -> Optional[_Parse]:
    """Peel ``do (redo do)*`` on ``trace[s:e]``.

    Backtracking version: at every iteration, tries the greedy peel
    (longest alphabet-compatible prefix) first, then shorter peels on
    failure. Recurses into ``_loop_continue`` after each successful
    body iteration to try the ``(redo do)*`` tail.
    """
    if len(children) < 2:
        if children:
            return _replay(
                trace, s, e, children[0], node_ids, alpha_cache,
                coarsen_loops, budget, memo,
            )
        return (("LOOP", nid, 0), {}, {}) if s == e else None

    do_tree, redo_tree = children[0], children[1]
    do_alpha = _alphabet(do_tree, alpha_cache)
    redo_alpha = _alphabet(redo_tree, alpha_cache)

    if not do_alpha and not redo_alpha and s < e:
        return None

    # First (mandatory) body iteration.
    max_do = s
    while max_do < e and trace[max_do] in do_alpha:
        max_do += 1
    for do_end in range(max_do, s - 1, -1):
        if budget[0] <= 0:
            return None
        r_do = _replay(
            trace, s, do_end, do_tree, node_ids, alpha_cache,
            coarsen_loops, budget, memo,
        )
        if r_do is None:
            continue
        do_frag, do_loop_d, do_xor_d = r_do
        cont = _loop_continue(
            trace, s, e, do_end, redo_tree, do_tree, redo_alpha, do_alpha,
            node_ids, alpha_cache, coarsen_loops, budget, memo,
            iter_count=1, iter_frags=[do_frag],
            loop_d=do_loop_d, xor_d=do_xor_d,
        )
        if cont is None:
            continue
        iter_count, iter_frags, loop_d, xor_d = cont
        out_loop = dict(loop_d)
        out_loop[nid] = max(out_loop.get(nid, 0), iter_count)
        if coarsen_loops:
            coarse = 0 if iter_count == 0 else (1 if iter_count == 1 else 2)
            return (("LOOP", nid, coarse), out_loop, xor_d)
        return (
            ("LOOP_FINE", nid, iter_count, tuple(iter_frags)),
            out_loop, xor_d,
        )
    return None


def _loop_continue(
    trace: List[str],
    s: int,
    e: int,
    pos: int,
    redo_tree,
    do_tree,
    redo_alpha: frozenset,
    do_alpha: frozenset,
    node_ids: Dict[int, int],
    alpha_cache: Dict[int, frozenset],
    coarsen_loops: bool,
    budget: List[int],
    memo: _Memo,
    iter_count: int,
    iter_frags: List[tuple],
    loop_d: Dict[int, int],
    xor_d: Dict[int, Dict[int, int]],
) -> Optional[Tuple[int, List[tuple], Dict[int, int], Dict[int, Dict[int, int]]]]:
    """After ``iter_count`` body iterations, either end the loop (if
    ``pos == e``) or try one more ``(redo, do)`` pair.

    Explores peel lengths greedy-first; on failure of a pair, walks
    the redo peel down before conceding."""
    if pos == e:
        return (iter_count, iter_frags, loop_d, xor_d)
    if budget[0] <= 0:
        return None
    max_redo = pos
    while max_redo < e and trace[max_redo] in redo_alpha:
        max_redo += 1
    for redo_end in range(max_redo, pos - 1, -1):
        if budget[0] <= 0:
            return None
        r_redo = _replay(
            trace, pos, redo_end, redo_tree, node_ids, alpha_cache,
            coarsen_loops, budget, memo,
        )
        if r_redo is None:
            continue
        _redo_frag, redo_loop_d, redo_xor_d = r_redo
        max_do = redo_end
        while max_do < e and trace[max_do] in do_alpha:
            max_do += 1
        for do_end in range(max_do, redo_end - 1, -1):
            if budget[0] <= 0:
                return None
            # Forward-progress guard on the (redo, do) pair.
            if do_end == pos:
                continue
            r_do = _replay(
                trace, redo_end, do_end, do_tree, node_ids, alpha_cache,
                coarsen_loops, budget, memo,
            )
            if r_do is None:
                continue
            do_frag, do_loop_d, do_xor_d = r_do
            new_loop = _merge_loop(_merge_loop(loop_d, redo_loop_d), do_loop_d)
            new_xor = _merge_xor(_merge_xor(xor_d, redo_xor_d), do_xor_d)
            result = _loop_continue(
                trace, s, e, do_end, redo_tree, do_tree, redo_alpha, do_alpha,
                node_ids, alpha_cache, coarsen_loops, budget, memo,
                iter_count=iter_count + 1,
                iter_frags=iter_frags + [do_frag],
                loop_d=new_loop, xor_d=new_xor,
            )
            if result is not None:
                return result
    return None


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
