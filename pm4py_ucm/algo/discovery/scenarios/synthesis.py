"""Scenario synthesis — build a UCM ScenarioGroup from a ClusteringResult.

Synthesis turns *every variant cluster* into one :class:`UCM.ScenarioDef`
on the UCM. Each ScenarioDef:

* initialises the synthetic ``variant_id`` :class:`UCM.Variable` to its
  own variant ID (e.g. ``variant_id = "v3"``);
* references the UCM's :class:`UCM.StartPoint` and
  :class:`UCM.EndPoint` so the jUCMNav traversal engine knows where to
  enter and where to expect the path to end;
* carries the variant's partial-order expression and the (truncated)
  list of underlying case IDs in its :attr:`description` field — so a
  user opening the model in jUCMNav can trace each scenario back to
  the cases of the original log that justify it.

For every XOR choice point outside any loop, the corresponding
``UCM.OrFork`` outgoing connection receives a disjunctive condition
listing the variant IDs that take that branch:

    expression = "variant_id == \\"v1\\" || variant_id == \\"v4\\""

XOR choices *inside* loops are deliberately left at the default
``true``: the variant-driven encoding can't distinguish per-iteration
choices, so condition synthesis there would over-promise and risk
mis-routing the jUCMNav traversal.

Public entry point: :func:`synthesize_scenarios`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ....objects.ucm.obj import UCM
from ..variants import choice_signature as _cs
from ..variants.clustering import ClusteringResult, Variant


_VARIANT_VAR_NAME = "variant_id"
_VARIANT_ENUM_NAME = "VariantId"
_DEFAULT_GROUP_NAME = "MinedScenarios"
_MAX_CASE_IDS_IN_DESCRIPTION = 10

# Process-tree operator string values (kept in lock-step with the
# converter and the choice-signature module so the three components see
# the same tree the same way).
_SEQUENCE = "->"
_XOR = "X"
_OR = "O"
_PARALLEL = "+"
_INTERLEAVING = "o"
_LOOP = "*"


def synthesize_scenarios(
    ucm: UCM,
    tree,
    clustering: ClusteringResult,
    group_name: str = _DEFAULT_GROUP_NAME,
    map_name: Optional[str] = None,
    emit_conditions: bool = True,
) -> UCM.ScenarioGroup:
    """Populate ``ucm`` with one scenario per variant in ``clustering``.

    Parameters
    ----------
    ucm
        The UCM produced from ``tree`` by the standard discovery
        pipeline. Mutated in place: a new :class:`UCM.EnumerationType`,
        :class:`UCM.Variable`, and :class:`UCM.ScenarioGroup` are added.
    tree
        The process tree the UCM was built from. Used for tree↔UCM
        correlation when emitting OR-fork conditions.
    clustering
        Result of
        :func:`pm4py_ucm.algo.discovery.variants.clustering.cluster`
        on the same tree and log.
    group_name
        Name for the new :class:`UCM.ScenarioGroup` (default
        ``"MinedScenarios"``).
    map_name
        Which UCM map's start/end points the scenarios should hook
        into. Defaults to the first map (``ucm.maps[0]``) — the
        flatten-before-synthesis decision means there is typically
        exactly one map at this stage.
    emit_conditions
        When ``True`` (default), set the ``variant_id`` disjunctive
        conditions on outgoing OR-fork connections (skipping OR-forks
        inside loops). Pass ``False`` to leave every connection alone
        — useful for debugging scenario load-in jUCMNav independently
        from condition logic.

    Returns
    -------
    UCM.ScenarioGroup
        The new scenario group, also registered under
        ``ucm.scenario_groups``.
    """
    if not clustering.variants:
        return ucm.add_scenario_group(name=group_name)

    target_map = _pick_map(ucm, map_name)

    # Define the variant_id enum + variable on the URN spec.
    variant_ids = [v.variant_id for v in clustering.variants]
    enum_type = ucm.get_or_add_enumeration_type(
        _VARIANT_ENUM_NAME, values=variant_ids,
    )
    # jUCMNav writes type discriminators in lowercase ("enumeration",
    # "integer"). Match that — capital "Enumeration" makes the editor
    # treat the variable as untyped and the scenario tool can't
    # initialise it.
    variant_var = ucm.get_or_add_variable(
        _VARIANT_VAR_NAME, type="enumeration", enumeration_type=enum_type,
    )

    # Loop counter scaffolding. For every LOOP tree node with at least
    # one body responsibility in the UCM, this creates an integer
    # variable, wires mutually-exclusive ``counter == 0`` / ``counter > 0``
    # conditions onto the LoopFork's exit / redo arcs, and appends a
    # decrement expression to a body responsibility. The returned dict
    # is ``{tree_loop_node_id: counter_variable}`` — used below to
    # initialise the counter per scenario.
    loop_counters: Dict[int, UCM.Variable] = {}
    if emit_conditions:
        loop_counters = _wire_loop_counters(ucm, target_map, tree)

    # Build the ScenarioGroup with one ScenarioDef per variant.
    group = ucm.add_scenario_group(name=group_name)
    starts = target_map.start_points
    ends = target_map.end_points
    for variant in clustering.variants:
        sc = UCM.ScenarioDef(
            name=variant.variant_id,
            description=_format_description(variant),
        )
        sc._owner = ucm
        sc.add_initialization(variant_var, variant.variant_id)
        # Per-loop integer counter initialisation. Use the variant's
        # max observed iteration count so the loop runs at least as
        # often as the heaviest trace in the cluster.
        for tree_loop_id, counter_var in loop_counters.items():
            max_iter = variant.loop_iteration_max.get(tree_loop_id, 0)
            sc.add_initialization(counter_var, str(max_iter))
        for sp in starts:
            sc.add_start_point(sp, enabled=True)
        for ep in ends:
            sc.add_end_point(ep, enabled=True)
        group.add_scenario(sc)

    # Set conditions on OR-fork outgoing arcs whose tree XOR is not
    # inside a loop. Skipped silently for inside-loop XORs.
    if emit_conditions:
        _emit_orfork_conditions(target_map, tree, clustering.variants)

    return group


# ---------------------------------------------------------------------------
# Description formatting
# ---------------------------------------------------------------------------

def _format_description(variant: Variant) -> str:
    """Compact human-readable description of a variant — partial order
    expression, frequency, linearization-count, and a truncated case-ID
    list. Surfaced inside jUCMNav's scenario panel."""
    shown = variant.case_ids[:_MAX_CASE_IDS_IN_DESCRIPTION]
    extra = len(variant.case_ids) - len(shown)
    case_ids_part = ", ".join(shown)
    if extra > 0:
        case_ids_part += f", ... (+{extra} more)"
    return (
        f"Partial-order: {variant.partial_order_expression}\n"
        f"Frequency: {variant.frequency} case(s); "
        f"linearizations: {variant.linearization_count}; "
        f"distinct sequences in log: {variant.sequence_variants}.\n"
        f"Case IDs: {case_ids_part}"
    )


# ---------------------------------------------------------------------------
# Tree ↔ UCM correlation
# ---------------------------------------------------------------------------

def _pick_map(ucm: UCM, name: Optional[str]) -> UCM.UCMmap:
    if name is not None:
        for m in ucm.maps:
            if m.name == name:
                return m
        raise ValueError(f"No UCM map named {name!r}")
    if not ucm.maps:
        raise ValueError("UCM has no maps; cannot synthesize scenarios.")
    return ucm.maps[0]


def _op_value(node) -> Optional[str]:
    op = getattr(node, "operator", None)
    if op is None:
        return None
    return getattr(op, "value", op)


def _collect_multi_child_xors(
    tree, node_ids: Dict[int, int],
) -> List[Tuple[int, int, bool]]:
    """Pre-order list of ``(signature_int_id, n_branches, in_loop)`` for
    every multi-child XOR/OR tree node.

    The converter creates one :class:`UCM.OrFork` node per such tree
    node, in the same pre-order traversal — so the i-th entry of this
    list always corresponds to the i-th non-LoopFork ``OrFork`` in the
    map's ``nodes`` list. The ``in_loop`` flag tells the caller whether
    the corresponding OR-fork is reachable only from inside a LOOP
    body: variant signatures coarsen iteration counts and so cannot
    reliably distinguish per-iteration XOR choices, which is why the
    synthesizer leaves inside-loop OR-fork conditions at the default
    ``true`` for v1. Crucially, both subsets are still indexed so the
    pairing with UCM ``OrFork`` nodes stays consistent — skipping
    inside-loop entries from the list would put outside-loop XORs out
    of alignment with their OrForks whenever a tree contains XORs
    both inside and outside loops, the bug Daniel hit on
    ClaimsPaymentLog."""
    out: List[Tuple[int, int, bool]] = []

    def _walk(node, in_loop: bool) -> None:
        op = _op_value(node)
        children = list(getattr(node, "children", None) or [])
        if op in (_XOR, _OR) and len(children) >= 2:
            out.append((node_ids[id(node)], len(children), in_loop))
        next_in_loop = in_loop or (op == _LOOP)
        for c in children:
            _walk(c, next_in_loop)

    _walk(tree, False)
    return out


# ---------------------------------------------------------------------------
# Loop counter scaffolding
# ---------------------------------------------------------------------------

def _pull_condition_onto_direct_arc(
    arc: UCM.NodeConnection,
) -> UCM.NodeConnection:
    """Ensure the OR-fork's *direct* outgoing arc carries the branch
    condition, moving it up past a single routing bend if needed.

    The converter's ``insert_routing_empty_points`` pass splits every
    arc adjacent to a fork/join into ``src -> bend -> tgt`` and parks
    the original branch condition on the *second* half (so the label
    sits visually near the fork in the diagram). jUCMNav, however,
    only evaluates conditions on arcs **directly** leaving a fork —
    a condition stranded one hop downstream past an EmptyPoint reads
    as no-condition (default ``true``), which is exactly the bug
    Daniel reported.

    This helper moves the downstream condition back onto the direct
    arc and clears the downstream slot so jUCMNav doesn't see a
    redundant copy. The visual placement of the label-deltas is
    unchanged — only the arc the condition belongs to. Returns the
    (now condition-carrying) direct arc."""
    if arc.condition is not None:
        return arc
    target = arc.target
    if not (isinstance(target, UCM.EmptyPoint)
            and len(target.succ_connections) == 1):
        return arc
    downstream = target.succ_connections[0]
    if downstream.condition is None:
        return arc
    arc.set_condition(downstream.condition)
    downstream._condition = None
    return arc


def _collect_loop_tree_ids(tree, node_ids: Dict[int, int]) -> List[int]:
    """Pre-order list of stable signature IDs for every LOOP tree node.

    The converter creates a ``LoopJoin`` + ``LoopFork`` pair per LOOP
    node in the same pre-order, so list indices line up after
    filtering the UCM map's nodes by name."""
    out: List[int] = []

    def _walk(node):
        op = _op_value(node)
        children = list(getattr(node, "children", None) or [])
        if op == _LOOP and len(children) >= 2:
            out.append(node_ids[id(node)])
        for c in children:
            _walk(c)

    _walk(tree)
    return out


def _find_loop_body_resp_ref(
    loop_join: UCM.PathNode, loop_fork: UCM.PathNode,
) -> Optional[UCM.RespRef]:
    """BFS forward from ``loop_join`` and return the first
    :class:`UCM.RespRef` found on the path to ``loop_fork``.

    The body is the sub-graph between LoopJoin and LoopFork. Walking
    forward from LoopJoin without traversing past LoopFork enumerates
    exactly the body nodes — the redo back-edge runs the other way
    (LoopFork -> redo subtree -> LoopJoin) and is never reached. The
    first RespRef encountered is the natural site for the loop-counter
    decrement expression: it runs once per body iteration, so the
    counter steps down predictably."""
    visited = {id(loop_join)}
    queue: List[UCM.PathNode] = [
        arc.target for arc in loop_join.succ_connections
    ]
    while queue:
        node = queue.pop(0)
        if id(node) in visited:
            continue
        visited.add(id(node))
        if node is loop_fork:
            continue
        if isinstance(node, UCM.RespRef) and node.resp_def is not None:
            return node
        for arc in node.succ_connections:
            queue.append(arc.target)
    return None


def _synthesize_decrement_resp_ref(
    ucm: UCM,
    target_map: UCM.UCMmap,
    loop_join: UCM.PathNode,
    counter_name: str,
) -> UCM.RespRef:
    """Insert a synthetic ``decrement_<counter>`` :class:`UCM.RespRef`
    immediately after ``loop_join`` and return it.

    Called for loops whose body contains no real responsibility (e.g.
    a tau body). The synthesizer needs *some* node inside the body to
    carry the ``counter = counter - 1`` expression, so we create one
    and splice it onto the first outgoing connection of LoopJoin. The
    visual layout coordinates default to (0, 0); the auto-layouter
    would normally re-flow on the next save, but this kind of edit
    happens after the converter's layout pass — see the comment in
    :func:`_wire_loop_counters` for the resulting trade-off."""
    name = f"decrement_{counter_name}"
    resp_def = ucm.get_or_add_responsibility(name)
    ref = UCM.RespRef(name=name, resp_def=resp_def)
    target_map.add_node(ref)
    # Splice into LoopJoin's outgoing arc: src -> ref -> original_target.
    out_arc = loop_join.succ_connections[0]
    next_node = out_arc.target
    target_map.remove_connection(out_arc)
    target_map.add_connection(loop_join, ref)
    target_map.add_connection(ref, next_node)
    return ref


def _wire_loop_counters(
    ucm: UCM, target_map: UCM.UCMmap, tree,
) -> Dict[int, UCM.Variable]:
    """Create per-LOOP integer counter variables, set mutually-exclusive
    LoopFork conditions, and attach decrement expressions inside the
    loop body.

    For each LOOP tree node:

    1. Pair it with the matching ``LoopJoin`` / ``LoopFork`` UCM nodes
       (same pre-order as the converter creates them).
    2. Create an integer :class:`UCM.Variable` named
       ``loop_counter_<tree_id>``.
    3. Set the LoopFork's outgoing conditions:

       * exit arc (label ``"exit"``)  -> ``counter == 0``
       * redo arc (label ``"redo"``)  -> ``counter > 0``

       These are mutually exclusive and jointly exhaustive at runtime,
       resolving the non-determinism Daniel flagged on the LoopFork.
    4. Append the decrement expression
       ``counter = counter - 1;`` to a body responsibility's
       :attr:`Responsibility.expression`. If no
       :class:`UCM.RespRef` exists in the body, one is synthesised
       (via :func:`_synthesize_decrement_resp_ref`) and spliced in
       right after LoopJoin.

    Returns a mapping ``{tree_loop_node_id: counter_variable}`` so the
    caller can add per-scenario initialisations.
    """
    node_ids = _cs.assign_node_ids(tree)
    tree_loop_ids = _collect_loop_tree_ids(tree, node_ids)

    loop_forks = [
        n for n in target_map.nodes
        if isinstance(n, UCM.OrFork) and n.name == "LoopFork"
    ]
    loop_joins = [
        n for n in target_map.nodes
        if isinstance(n, UCM.OrJoin) and n.name == "LoopJoin"
    ]

    if not (len(loop_forks) == len(loop_joins) == len(tree_loop_ids)):
        # Disagreement between the tree walk and the converter's output.
        # Skip silently rather than mis-attribute conditions.
        return {}

    counters: Dict[int, UCM.Variable] = {}
    for i, tree_loop_id in enumerate(tree_loop_ids):
        lf, lj = loop_forks[i], loop_joins[i]
        name = f"loop_counter_{tree_loop_id}"
        counter = ucm.get_or_add_variable(name, type="integer")
        counters[tree_loop_id] = counter

        # Mutually exclusive LoopFork conditions. The converter's
        # routing_empty_points pass put the branch label/condition on
        # the bend->target arc; jUCMNav, however, only evaluates
        # conditions on arcs **directly** leaving the fork, so we
        # pull each condition onto the LoopFork's direct outgoing arc
        # before rewriting its expression.
        for arc in lf.succ_connections:
            arc = _pull_condition_onto_direct_arc(arc)
            cond = arc.condition
            label = cond.label if cond else ""
            if label == "exit":
                expr = f"{name} == 0"
            elif label == "redo":
                expr = f"{name} > 0"
            else:
                continue
            if cond is None:
                arc.set_condition(
                    UCM.Condition(label=label, expression=expr),
                )
            else:
                cond.expression = expr

        # Attach decrement to a body responsibility (synthesise if absent).
        body_resp = _find_loop_body_resp_ref(lj, lf)
        if body_resp is None or body_resp.resp_def is None:
            body_resp = _synthesize_decrement_resp_ref(
                ucm, target_map, lj, name,
            )
        decrement = f"{name} = {name} - 1;"
        resp_def = body_resp.resp_def
        existing = resp_def.expression or ""
        if decrement not in existing:
            resp_def.expression = (
                existing + " " + decrement if existing else decrement
            )

    return counters


def _emit_orfork_conditions(
    target_map: UCM.UCMmap,
    tree,
    variants: List[Variant],
) -> None:
    """Set ``variant_id``-disjunction conditions on outgoing OR-fork
    connections that correspond to non-loop XORs in the process tree.

    Pairs multi-child XOR tree nodes with the map's OR-forks in
    pre-order. For each pair, gathers
    ``{branch_index: [variant_id, ...]}`` from the variants' choice
    signatures and writes a disjunctive condition expression on every
    outgoing connection of the OR-fork. Branches that no variant
    takes keep their default (typically ``true``).

    The function is silent if the tree↔UCM XOR counts disagree (which
    would indicate the converter and the choice-signature walker
    interpret the tree differently): in that case the function returns
    a no-op rather than misattributing conditions.
    """
    node_ids = _cs.assign_node_ids(tree)
    xor_seq = _collect_multi_child_xors(tree, node_ids)

    or_forks: List[UCM.OrFork] = [
        n for n in target_map.nodes
        if isinstance(n, UCM.OrFork) and n.name != "LoopFork"
    ]
    if len(or_forks) != len(xor_seq):
        # Tree walk and converter disagree on XOR count; skip
        # condition emission rather than mis-attribute.
        return

    for of_idx, (tree_xor_id, n_branches, in_loop) in enumerate(xor_seq):
        of = or_forks[of_idx]
        if in_loop:
            # Inside-loop XOR: variant signatures coarsen loop
            # iterations, so we cannot tell which branch each variant
            # takes on each pass — but the converter's default
            # leaves every outgoing arc at ``true``, which jUCMNav
            # treats as non-deterministic. Make the choice
            # deterministic instead by setting the first branch to
            # ``true`` and every other branch to ``false``: the
            # scenario engine will reliably pick branch 0 on every
            # iteration. The choice is mutually exclusive and
            # jointly exhaustive at the cost of always replaying
            # the same branch (combining with the loop counter to
            # distribute per-iteration choices is a follow-up).
            for k, arc in enumerate(of.succ_connections):
                arc = _pull_condition_onto_direct_arc(arc)
                expr = "true" if k == 0 else "false"
                if arc.condition is None:
                    arc.set_condition(UCM.Condition(
                        label=f"branch{k}", expression=expr,
                    ))
                else:
                    arc.condition.expression = expr
            continue
        branch_to_variants: Dict[int, List[str]] = {}
        for v in variants:
            chosen = _cs.collect_xor_choices(v.signature).get(tree_xor_id)
            if chosen is None:
                continue
            branch_to_variants.setdefault(chosen, []).append(v.variant_id)

        succs = of.succ_connections
        if len(succs) != n_branches:
            continue
        for k, arc in enumerate(succs):
            # Pull any routed-down condition back up so the variant_id
            # expression lands on the arc directly leaving the
            # OR-fork — that's the only place jUCMNav evaluates it.
            arc = _pull_condition_onto_direct_arc(arc)
            vids = branch_to_variants.get(k, [])
            # Every branch gets an explicit expression: a variant_id
            # disjunction for branches at least one variant takes,
            # ``false`` for branches no observed variant ever takes.
            # Together the per-OR-fork conditions are mutually
            # exclusive (one variant matches exactly one disjunct)
            # and jointly exhaustive (every variant matches some
            # branch). Without the explicit ``false`` on idle
            # branches, the default ``true`` would let the jUCMNav
            # engine non-deterministically pick a branch no scenario
            # was designed for.
            #
            # jUCMNav's expression syntax treats enum values as bare
            # identifiers, not string literals — quoted form makes
            # the parser treat the right side as a string and the
            # comparison fails the enum type check.
            if vids:
                expr = " || ".join(
                    f"{_VARIANT_VAR_NAME} == {vid}" for vid in vids
                )
            else:
                expr = "false"
            if arc.condition is None:
                arc.set_condition(UCM.Condition(
                    label=f"branch{k}", expression=expr,
                ))
            else:
                arc.condition.expression = expr
