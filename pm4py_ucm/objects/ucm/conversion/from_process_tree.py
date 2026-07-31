"""
Conversion from a PM4Py *process tree* to a Use Case Map.

Process trees are the natural output of the Inductive Miner family
(``pm4py.discover_process_tree_inductive``); they are also a normal form
into which most other process-mining models can be projected. Mapping them
to UCMs is straightforward because UCMs natively support all of the tree
operators the Inductive Miner uses:

============== ============================ ==================================
Tree operator  UCM construction              Reasoning
============== ============================ ==================================
``→`` SEQUENCE Chain via ``EmptyPoint``s     A UCM path is sequential by
                                             default — empty points are the
                                             standard "intermediate
                                             milestone" symbol.
``×`` XOR      ``OrFork`` ... ``OrJoin``     Identical exclusive-choice
                                             semantics.
``∨`` OR       ``OrFork`` ... ``OrJoin``     UCMs do not distinguish
                                             inclusive from exclusive choice;
                                             treated as XOR with branch
                                             conditions left to the user.
``∧`` PARALLEL ``AndFork`` ... ``AndJoin``   Identical AND-fork/join
                                             semantics.
``⟲`` LOOP     ``OrJoin`` ⤴ do ⤳ ``OrFork``  Loop with redo branch flowing
                                             back to the OrJoin.
Leaf (label)   ``RespRef`` + ``Resp.``       Activity = responsibility ref.
Leaf (τ)       Direct edge                   No-op steps need no symbol.
============== ============================ ==================================

The converter is intentionally tolerant of input: it duck-types the input
tree by attribute names (``operator``, ``label``, ``children``) and matches
operators by their string ``value`` instead of by identity. This means the
implementation works whether the caller hands us a real
``pm4py.objects.process_tree.obj.ProcessTree`` (in which case the
``Operator`` enum is the one shipped by PM4Py) or any other object that
quacks the same way — useful for testing without an installed PM4Py.
"""
from __future__ import annotations

from typing import Callable, Iterable, List, Optional

from ..obj import UCM


# Operator symbols — kept in sync with ``pm4py.objects.process_tree.obj.Operator``.
# Comparing on ``.value`` avoids importing PM4Py just to obtain the enum.
_SEQUENCE = "->"
_XOR = "X"
_PARALLEL = "+"
_LOOP = "*"
_OR = "O"
_INTERLEAVING = "o"

_CHOICE_OPS = {_XOR, _OR}
_PARALLEL_OPS = {_PARALLEL, _INTERLEAVING}


# Hook used by the :mod:`pm4py_ucm.objects.ucm.conversion.decomposition`
# module to intercept subtrees that should become plug-in maps. When set,
# ``_attach`` calls it for every subtree it is about to expand; if the hook
# returns ``True`` the recursion stops (the hook has emitted a Stub instead).
#
# The decomposition module activates this via
# :func:`pm4py_ucm.objects.ucm.conversion.decomposition._cut_handler_active`,
# a context manager that scopes the swap so exceptions can't leak it.
_cut_handler: Optional[Callable[[object, "UCM.PathNode", "UCM.PathNode",
                                  "UCM.UCMmap"], bool]] = None


def _op_value(operator) -> Optional[str]:
    """Return the operator's string value, accepting either an Enum or a str."""
    if operator is None:
        return None
    return getattr(operator, "value", operator)


# ---------------------------------------------------------------------------
# Provenance: which tree node produced which UCM element
# ---------------------------------------------------------------------------
#
# Every node and connection ``_attach`` creates records the ``id()`` of the
# subtree it came from in ``_tree_python_id``. Two consumers rely on it:
#
# * the scenario synthesizer correlates each OR-fork with the tree XOR it
#   came from (lookup by id, not by cross-map walk position — the latter
#   happens to work for the flat case but is brittle once decomposition
#   interleaves root-map and plug-in-map forks);
# * :mod:`pm4py_ucm.algo.traversal` attributes replay-based traversal
#   counts to nodes and edges.
#
# Both pair it with ``choice_signature.assign_node_ids(tree)`` to get a
# stable integer id. The attribute is a plain Python attribute, never
# metadata, so it does not reach the ``.jucm`` export — and being an
# ``id()`` it is only meaningful while the originating tree is alive.

def _tag(element, tree):
    """Record ``tree`` as the origin of ``element``."""
    element._tree_python_id = id(tree)
    return element


def _connect(ucm_map, source, target, tree, condition=None):
    """``add_connection`` that records which subtree produced the arc."""
    return _tag(
        ucm_map.add_connection(source, target, condition=condition), tree,
    )


def apply(tree, parameters: Optional[dict] = None) -> UCM:
    """Convert a process tree into a Use Case Map.

    Parameters
    ----------
    tree
        A PM4Py process tree (or any object exposing ``operator``,
        ``label`` and ``children``).
    parameters
        Optional ``{key: value}`` map. Recognised keys:

        ``map_name``
            Name of the resulting UCMmap. Defaults to ``"DiscoveredMap"``.
        ``urn_name``
            Name of the URN spec. Defaults to ``"PM4PyDiscovery"``.
        ``container``
            Optional existing :class:`UCM` to add the converted map to,
            instead of creating a fresh container. Used by the
            model-family assembler so that several conversions share
            one URN spec — one ID counter and one set of
            responsibility/component *definitions*
            (``get_or_add_responsibility`` dedupes by name). When set,
            ``urn_name`` is ignored and post-processing (simplify /
            routing points) is scoped to the newly added map only.
            Note that ``performers`` binding is container-wide; family
            assembly binds performers once at the end instead of
            per conversion.
        ``simplify``
            Boolean (default ``True``) — after conversion, collapse
            sequences of empty points into a single edge.
        ``performers``
            Optional ``{activity_name: performer_name}`` mapping (e.g. the
            output of
            :func:`pm4py_ucm.algo.discovery.resources.algorithm.apply`).
            When supplied, the converter creates one
            :class:`UCM.ComponentElement` per unique performer, sets
            :attr:`UCM.Responsibility.performer` on every matching
            responsibility, and adds one :class:`UCM.ComponentRef` per
            used component to the map — binding each RespRef's
            :attr:`UCM.PathNode.cont_ref` accordingly. Values may also be
            ``(name, Kind)`` tuples to specify the component kind.
        ``performer_kind``
            Default :class:`UCM.ComponentElement.Kind` applied to every
            performer for which no explicit kind is given. Defaults to
            ``Kind.TEAM``.
        ``routing_empty_points``
            Boolean (default ``True``). After simplification, insert one
            :class:`UCM.EmptyPoint` on every incoming and outgoing arc of
            each fork/join node (OrFork, OrJoin, AndFork, AndJoin). These
            "elbow" points give the layouter and jUCMNav's renderer
            explicit way-points to bend connections at, producing
            smoother diagrams on complex models. They are pure layout
            hints — semantics-preserving and behaviour-neutral.

    Returns
    -------
    UCM
        A new UCM container with a single map representing ``tree``.
    """
    parameters = dict(parameters or {})

    # Hierarchical decomposition — when ``decomposition`` is set to a
    # non-``None`` / non-``"off"`` value, hand off to the decomposition
    # module which produces a multi-map UCM. The default (no key, or
    # ``None``, or ``"off"``) preserves the single-map output below
    # byte-for-byte.
    if parameters.get("decomposition") not in (None, "off"):
        # Imported here to avoid a circular import at module load.
        from . import decomposition as _decomp
        return _decomp.apply(tree, parameters=parameters)

    map_name: str = parameters.get("map_name", "DiscoveredMap")
    urn_name: str = parameters.get("urn_name", "PM4PyDiscovery")
    simplify: bool = parameters.get("simplify", True)
    performers: Optional[dict] = parameters.get("performers")
    performer_kind = parameters.get("performer_kind")
    additional_components = parameters.get("additional_components")
    routing_points: bool = parameters.get("routing_empty_points", True)
    container: Optional[UCM] = parameters.get("container")

    ucm = container if container is not None else UCM(name=urn_name)
    ucm_map = ucm.add_map(name=map_name)

    start = UCM.StartPoint(name="start")
    end = UCM.EndPoint(name="end")
    ucm_map.add_node(start)
    ucm_map.add_node(end)

    # ``_attach`` recurses on the tree structure; ``simplify_empty_points``
    # iterates. For deeply nested inputs (e.g. a process tree with a long
    # right spine, common in software-event-log mining), Python's default
    # recursion limit of 1000 may be reached. We raise it just for the
    # duration of conversion, then restore it.
    import sys as _sys
    _saved_limit = _sys.getrecursionlimit()
    _required = max(_saved_limit, _tree_depth(tree) + 200)
    if _required > _saved_limit:
        _sys.setrecursionlimit(_required)
    try:
        _attach(tree, start, end, ucm_map)
        # Post-processing is scoped to the map we just built — with an
        # existing ``container``, previously added maps have already been
        # processed and must not be touched again.
        if simplify:
            _simplify_map(ucm, ucm_map)
        # Routing empty points run *after* simplification so that the
        # cleanup pass does not undo them. They are inserted on every
        # in- and out-arc of every fork/join, providing layout flexibility
        # (each becomes its own column in the layered drawing) and
        # smoother lines when rendered.
        if routing_points:
            _insert_routing_for_map(ucm, ucm_map)
    finally:
        _sys.setrecursionlimit(_saved_limit)

    if performers or additional_components:
        ucm.bind_performers(
            performers or {},
            kind=performer_kind,
            additional_components=additional_components,
        )

    return ucm


def _tree_depth(tree) -> int:
    """Cheap iterative depth bound — counts the longest child-chain
    without recursing. Used only to size the recursion limit before
    ``_attach`` runs, so it does not need to be exact."""
    max_depth = 0
    # Iterative DFS with explicit stack to avoid recursion overflow here.
    stack = [(tree, 0)]
    while stack:
        node, d = stack.pop()
        if d > max_depth:
            max_depth = d
        for c in getattr(node, "children", None) or []:
            stack.append((c, d + 1))
    return max_depth


# ---------------------------------------------------------------------------
# Recursive expansion
# ---------------------------------------------------------------------------

def _attach(tree, entry: UCM.PathNode, exit: UCM.PathNode,
            ucm_map: "UCM.UCMmap") -> None:
    """Splice ``tree`` between ``entry`` and ``exit`` on ``ucm_map``.

    When the module-level :data:`_cut_handler` is set, it is consulted
    before any expansion: if it returns ``True`` the recursion stops
    (the handler has emitted a placeholder — typically a Stub — to stand
    in for ``tree`` in the current map). This is how the decomposition
    module turns selected subtrees into plug-in maps.
    """
    if _cut_handler is not None and _cut_handler(tree, entry, exit, ucm_map):
        return

    op = _op_value(getattr(tree, "operator", None))
    children = list(getattr(tree, "children", []) or [])
    label = getattr(tree, "label", None)

    if op is None:
        # Leaf
        if label is None:
            # tau / silent — collapse to a direct edge. The edge still
            # carries the leaf's provenance, which is what lets a silent
            # skip report how many cases took it.
            _connect(ucm_map, entry, exit, tree)
        else:
            r = UCM.RespRef(name=str(label))
            r.set_resp_def(ucm_map._owner.get_or_add_responsibility(str(label)))
            ucm_map.add_node(_tag(r, tree))
            _connect(ucm_map, entry, r, tree)
            _connect(ucm_map, r, exit, tree)
        return

    if op == _SEQUENCE:
        if not children:
            _connect(ucm_map, entry, exit, tree)
            return
        prev = entry
        for i, child in enumerate(children):
            if i == len(children) - 1:
                _attach(child, prev, exit, ucm_map)
            else:
                ep = UCM.EmptyPoint()
                ucm_map.add_node(_tag(ep, tree))
                _attach(child, prev, ep, ucm_map)
                prev = ep
        return

    if op in _CHOICE_OPS:
        # Empty XOR / OR — treat like an empty sequence
        if not children:
            _connect(ucm_map, entry, exit, tree)
            return
        # Single-child XOR collapses to the child
        if len(children) == 1:
            _attach(children[0], entry, exit, ucm_map)
            return
        of = UCM.OrFork(name="OrFork")
        oj = UCM.OrJoin(name="OrJoin")
        ucm_map.add_node(_tag(of, tree))
        ucm_map.add_node(_tag(oj, tree))
        _connect(ucm_map, entry, of, tree)
        _connect(ucm_map, oj, exit, tree)
        for k, child in enumerate(children):
            label = f"branch{k}"  # default branch label; user can edit
            _attach_with_initial_label(child, of, oj, ucm_map, label)
        return

    if op in _PARALLEL_OPS:
        if not children:
            _connect(ucm_map, entry, exit, tree)
            return
        if len(children) == 1:
            _attach(children[0], entry, exit, ucm_map)
            return
        af = UCM.AndFork(name="AndFork")
        aj = UCM.AndJoin(name="AndJoin")
        ucm_map.add_node(_tag(af, tree))
        ucm_map.add_node(_tag(aj, tree))
        _connect(ucm_map, entry, af, tree)
        _connect(ucm_map, aj, exit, tree)
        for child in children:
            _attach(child, af, aj, ucm_map)
        return

    if op == _LOOP:
        # PM4Py loop has 2 children: do, redo. Semantics:
        #   do (redo do)*
        # Translation:
        #   entry -> [oj_in] -> do -> [of_out] -> exit
        #             ^                  |
        #             |____ redo  _______|
        if len(children) < 2:
            # Degenerate loop; fall back to attaching the only body
            if children:
                _attach(children[0], entry, exit, ucm_map)
            else:
                _connect(ucm_map, entry, exit, tree)
            return
        do_tree, redo_tree = children[0], children[1]
        oj_in = UCM.OrJoin(name="LoopJoin")
        of_out = UCM.OrFork(name="LoopFork")
        ucm_map.add_node(_tag(oj_in, tree))
        ucm_map.add_node(_tag(of_out, tree))
        _connect(ucm_map, entry, oj_in, tree)
        _attach(do_tree, oj_in, of_out, ucm_map)
        # Branch out of the loop
        _connect(ucm_map, of_out, exit, tree, condition="exit")
        # Redo branch: of_out -> redo -> oj_in
        _attach_with_initial_label(redo_tree, of_out, oj_in, ucm_map, "redo")
        return

    # Unknown operator — fall back to sequence semantics
    _attach_sequence_fallback(children, entry, exit, ucm_map, tree)


def _attach_sequence_fallback(children, entry, exit, ucm_map, tree=None):
    if not children:
        _connect(ucm_map, entry, exit, tree)
        return
    prev = entry
    for i, child in enumerate(children):
        if i == len(children) - 1:
            _attach(child, prev, exit, ucm_map)
        else:
            ep = UCM.EmptyPoint()
            ucm_map.add_node(_tag(ep, tree))
            _attach(child, prev, ep, ucm_map)
            prev = ep


def _attach_with_initial_label(tree, entry, exit, ucm_map, label: str) -> None:
    """Attach ``tree`` and tag the first connection out of ``entry`` it
    creates with ``condition=label`` — used to label OR-fork branches."""
    before = len(entry._succ)
    _attach(tree, entry, exit, ucm_map)
    after = len(entry._succ)
    if after > before:
        # The first new outgoing connection from ``entry`` is the branch.
        for c in entry._succ[before:]:
            if c.condition is None:
                c.set_condition(label)
                break


# ---------------------------------------------------------------------------
# Post-processing: collapse trivial empty points
# ---------------------------------------------------------------------------

def simplify_empty_points(ucm: UCM) -> None:
    """Remove every EmptyPoint that has exactly one in-arc and one out-arc
    (i.e. degree-2 silent passes); rewire the predecessor directly to the
    successor.

    This produces compact maps that match how a human would draw them. We
    do **not** remove EmptyPoints that have more than one neighbour or that
    carry user metadata.

    Runs in O(N) — empty points are collected once into a worklist, each
    is processed once, and a single bulk rebuild of the map's node and
    connection lists at the end avoids the per-removal ``list.remove``
    cost that would otherwise make this O(N²) on large discovered maps.
    """
    for ucm_map in list(ucm.maps):
        _simplify_map(ucm, ucm_map)


def _simplify_map(ucm: UCM, ucm_map: "UCM.UCMmap") -> None:
    """Single-pass empty-point contraction for one map."""
    removed_nodes: set = set()
    removed_conns: set = set()
    new_conns: List["UCM.NodeConnection"] = []

    for node in ucm_map.nodes:
        if not isinstance(node, UCM.EmptyPoint):
            continue
        if node in removed_nodes:
            continue
        if node.metadata or node.name not in ("", "EmptyPoint"):
            continue
        # Find the live in/out arcs (those not yet removed) of this node.
        live_in = [a for a in node._pred if a not in removed_conns]
        live_out = [a for a in node._succ if a not in removed_conns]
        if len(live_in) != 1 or len(live_out) != 1:
            continue
        in_arc, out_arc = live_in[0], live_out[0]
        src = in_arc.source
        dst = out_arc.target
        if src is dst:
            continue
        # Stage the contraction.
        cond = in_arc.condition or out_arc.condition
        removed_conns.add(in_arc)
        removed_conns.add(out_arc)
        removed_nodes.add(node)
        # Detach from neighbours' adjacency.
        try: src._succ.remove(in_arc)
        except ValueError: pass
        try: dst._pred.remove(out_arc)
        except ValueError: pass
        new_arc = UCM.NodeConnection(src, dst, condition=cond)
        new_arc._owner = ucm
        # Contracting a pass-through must not lose the arc's provenance —
        # the two halves came from the same block, so either tag serves.
        origin = (getattr(in_arc, "_tree_python_id", None)
                  or getattr(out_arc, "_tree_python_id", None))
        if origin is not None:
            new_arc._tree_python_id = origin
        new_conns.append(new_arc)

    if not removed_nodes and not new_conns:
        return

    # Bulk-rebuild the map's lists in one O(N) pass.
    ucm_map.nodes = [n for n in ucm_map.nodes if n not in removed_nodes]
    ucm_map._node_set = set(ucm_map.nodes)
    ucm_map.connections = [
        c for c in ucm_map.connections if c not in removed_conns
    ]
    ucm_map.connections.extend(new_conns)
    ucm_map._connection_set = set(ucm_map.connections)
    for n in removed_nodes:
        n._map = None


# ---------------------------------------------------------------------------
# Routing empty points (issue 4: smoother lines around forks/joins)
# ---------------------------------------------------------------------------

#: Node types that benefit from "elbow" empty points on every adjacent arc.
_FORK_JOIN_TYPES = (UCM.OrFork, UCM.OrJoin, UCM.AndFork, UCM.AndJoin)

#: Distinguishing name given to inserted routing empty points so that the
#: simplifier — if it ever runs again — leaves them alone. This is purely
#: descriptive (jUCMNav ignores the name on EmptyPoints in its renderer).
_ROUTING_POINT_NAME = "Bend"


def insert_routing_empty_points(ucm: UCM) -> None:
    """Insert one :class:`UCM.EmptyPoint` on every incoming and outgoing
    arc of every fork/join node.

    Each insertion replaces the arc ``src → fork`` with the pair
    ``src → bend → fork`` (and analogously for outgoing arcs). The new
    empty points carry the literal name ``"Bend"`` so they're
    distinguishable from the structural empty points the converter
    produces during ``_attach``; they also no longer satisfy the
    "name in {'', 'EmptyPoint'}" criterion of
    :func:`simplify_empty_points`, so re-running the simplifier won't
    remove them.

    The added nodes are semantically transparent — they have exactly one
    predecessor and one successor each, and they preserve any conditions
    on the arcs they replace (the condition is carried on the
    *outbound* half so that fork-branch labels like ``[TrueBranch]``
    still attach to the right edge in the visualizer)."""
    for m in list(ucm.maps):
        _insert_routing_for_map(ucm, m)


def _insert_routing_for_map(ucm: UCM, m: "UCM.UCMmap") -> None:
    """Single-map worker for :func:`insert_routing_empty_points`."""
    # Snapshot the fork/join nodes; the loop mutates m.nodes/m.connections.
    targets = [n for n in m.nodes if isinstance(n, _FORK_JOIN_TYPES)]
    if not targets:
        return

    # Identify the arcs to split. An arc adjacent to a fork/join only
    # needs a new bend point on the side *away* from the fork/join if
    # that side is not already an EmptyPoint — otherwise the desired
    # elbow already exists and a second insertion would just add a
    # redundant pass-through node. Skipping in that case keeps the pass
    # idempotent.
    arcs_to_split: List["UCM.NodeConnection"] = []
    seen: set = set()
    for n in targets:
        for c in list(n.pred_connections):
            if c in seen or isinstance(c.source, UCM.EmptyPoint):
                continue
            seen.add(c)
            arcs_to_split.append(c)
        for c in list(n.succ_connections):
            if c in seen or isinstance(c.target, UCM.EmptyPoint):
                continue
            seen.add(c)
            arcs_to_split.append(c)

    for arc in arcs_to_split:
        _split_arc_with_empty(ucm, m, arc)


def _split_arc_with_empty(
    ucm: UCM, m: "UCM.UCMmap", arc: "UCM.NodeConnection",
) -> None:
    """Replace ``src → tgt`` with ``src → bend → tgt``, preserving any
    condition on the original arc on the *outbound* half."""
    src, tgt = arc.source, arc.target
    cond = arc.condition
    origin = getattr(arc, "_tree_python_id", None)

    # Drop the old arc, both from the map and from src/tgt's adjacency.
    m.remove_connection(arc)

    # Insert a bend point. It carries a name so the simplifier ignores it.
    bend = UCM.EmptyPoint(name=_ROUTING_POINT_NAME)
    m.add_node(bend)

    # New arcs. The condition (e.g. a fork-branch label) belongs on the
    # second half so the label still attaches to the arc *leaving* the
    # fork — that's what users expect to see in the diagram. Both halves
    # inherit the split arc's provenance so traversal counts survive.
    first = m.add_connection(src, bend)
    second = m.add_connection(bend, tgt, condition=cond)
    if origin is not None:
        first._tree_python_id = origin
        second._tree_python_id = origin
        bend._tree_python_id = origin
