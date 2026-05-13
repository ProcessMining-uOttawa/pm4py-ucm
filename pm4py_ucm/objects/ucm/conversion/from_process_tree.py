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

from typing import Iterable, List, Optional

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


def _op_value(operator) -> Optional[str]:
    """Return the operator's string value, accepting either an Enum or a str."""
    if operator is None:
        return None
    return getattr(operator, "value", operator)


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
    map_name: str = parameters.get("map_name", "DiscoveredMap")
    urn_name: str = parameters.get("urn_name", "PM4PyDiscovery")
    simplify: bool = parameters.get("simplify", True)
    performers: Optional[dict] = parameters.get("performers")
    performer_kind = parameters.get("performer_kind")
    additional_components = parameters.get("additional_components")
    routing_points: bool = parameters.get("routing_empty_points", True)

    ucm = UCM(name=urn_name)
    ucm_map = ucm.add_map(name=map_name)

    start = UCM.StartPoint(name="start")
    end = UCM.EndPoint(name="end")
    ucm.add_node(start)
    ucm.add_node(end)

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
        _attach(tree, start, end, ucm)
        if simplify:
            simplify_empty_points(ucm)
        # Routing empty points run *after* simplification so that the
        # cleanup pass does not undo them. They are inserted on every
        # in- and out-arc of every fork/join, providing layout flexibility
        # (each becomes its own column in the layered drawing) and
        # smoother lines when rendered.
        if routing_points:
            insert_routing_empty_points(ucm)
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

def _attach(tree, entry: UCM.PathNode, exit: UCM.PathNode, ucm: UCM) -> None:
    """Splice ``tree`` between ``entry`` and ``exit`` on the UCM's default map."""
    op = _op_value(getattr(tree, "operator", None))
    children = list(getattr(tree, "children", []) or [])
    label = getattr(tree, "label", None)

    if op is None:
        # Leaf
        if label is None:
            # tau / silent — collapse to a direct edge
            ucm.add_connection(entry, exit)
        else:
            r = UCM.RespRef(name=str(label))
            r.set_resp_def(ucm.get_or_add_responsibility(str(label)))
            ucm.add_node(r)
            ucm.add_connection(entry, r)
            ucm.add_connection(r, exit)
        return

    if op == _SEQUENCE:
        if not children:
            ucm.add_connection(entry, exit)
            return
        prev = entry
        for i, child in enumerate(children):
            if i == len(children) - 1:
                _attach(child, prev, exit, ucm)
            else:
                ep = UCM.EmptyPoint()
                ucm.add_node(ep)
                _attach(child, prev, ep, ucm)
                prev = ep
        return

    if op in _CHOICE_OPS:
        # Empty XOR / OR — treat like an empty sequence
        if not children:
            ucm.add_connection(entry, exit)
            return
        # Single-child XOR collapses to the child
        if len(children) == 1:
            _attach(children[0], entry, exit, ucm)
            return
        of = UCM.OrFork(name="OrFork")
        oj = UCM.OrJoin(name="OrJoin")
        ucm.add_node(of)
        ucm.add_node(oj)
        ucm.add_connection(entry, of)
        ucm.add_connection(oj, exit)
        for k, child in enumerate(children):
            label = f"branch{k}"  # default branch label; user can edit
            _attach_with_initial_label(child, of, oj, ucm, label)
        return

    if op in _PARALLEL_OPS:
        if not children:
            ucm.add_connection(entry, exit)
            return
        if len(children) == 1:
            _attach(children[0], entry, exit, ucm)
            return
        af = UCM.AndFork(name="AndFork")
        aj = UCM.AndJoin(name="AndJoin")
        ucm.add_node(af)
        ucm.add_node(aj)
        ucm.add_connection(entry, af)
        ucm.add_connection(aj, exit)
        for child in children:
            _attach(child, af, aj, ucm)
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
                _attach(children[0], entry, exit, ucm)
            else:
                ucm.add_connection(entry, exit)
            return
        do_tree, redo_tree = children[0], children[1]
        oj_in = UCM.OrJoin(name="LoopJoin")
        of_out = UCM.OrFork(name="LoopFork")
        ucm.add_node(oj_in)
        ucm.add_node(of_out)
        ucm.add_connection(entry, oj_in)
        _attach(do_tree, oj_in, of_out, ucm)
        # Branch out of the loop
        ucm.add_connection(of_out, exit, condition="exit")
        # Redo branch: of_out -> redo -> oj_in
        _attach_with_initial_label(redo_tree, of_out, oj_in, ucm, "redo")
        return

    # Unknown operator — fall back to sequence semantics
    _attach_sequence_fallback(children, entry, exit, ucm)


def _attach_sequence_fallback(children, entry, exit, ucm):
    if not children:
        ucm.add_connection(entry, exit)
        return
    prev = entry
    for i, child in enumerate(children):
        if i == len(children) - 1:
            _attach(child, prev, exit, ucm)
        else:
            ep = UCM.EmptyPoint()
            ucm.add_node(ep)
            _attach(child, prev, ep, ucm)
            prev = ep


def _attach_with_initial_label(tree, entry, exit, ucm, label: str) -> None:
    """Attach ``tree`` and tag the first connection out of ``entry`` it
    creates with ``condition=label`` — used to label OR-fork branches."""
    before = len(entry._succ)
    _attach(tree, entry, exit, ucm)
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

    # Drop the old arc, both from the map and from src/tgt's adjacency.
    m.remove_connection(arc)

    # Insert a bend point. It carries a name so the simplifier ignores it.
    bend = UCM.EmptyPoint(name=_ROUTING_POINT_NAME)
    m.add_node(bend)

    # New arcs. The condition (e.g. a fork-branch label) belongs on the
    # second half so the label still attaches to the arc *leaving* the
    # fork — that's what users expect to see in the diagram.
    m.add_connection(src, bend)
    m.add_connection(bend, tgt, condition=cond)
