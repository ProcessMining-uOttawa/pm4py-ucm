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
from . import decision_mining as _dm


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
    max_loop_iterations: Optional[int] = 2,
    condition_strategy: str = "variant",
    log=None,
    decision_tree_max_depth: int = 3,
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
    max_loop_iterations
        Upper bound on the per-variant loop counter initialisation
        value. Defaults to ``2`` so scenarios traverse the loop body
        at most twice — enough to demonstrate "loop fires once vs
        more than once" behaviour without producing 8-deep
        iteration chains that take forever to step through in
        jUCMNav. Inside-loop XOR conditions also use this capped
        ``M_V`` as their counter range. Pass ``None`` to disable
        capping (counter initialises to the actual max observed in
        each variant's traces).
    condition_strategy
        How outside-loop OR-fork conditions are derived:

        * ``"variant"`` (default) — the original encoding: a
          ``variant_id`` enumeration variable plus a per-scenario
          ``variant_id == v_i`` initialisation; outside-loop
          conditions are ``variant_id`` disjunctions over the
          variants that took each branch.
        * ``"data-driven"`` — mine a decision tree per outside-
          loop OR-fork against the log's case-level attributes
          (lifted automatically from event-level columns that are
          constant per case). The tree predicts which branch each
          case took; its expression replaces the variant_id
          disjunction. No ``variant_id`` variable is created; each
          scenario instead initialises every mined attribute to a
          representative value for its variant (median for
          numerics, mode for enums / booleans). If the log has no
          usable case-constant attributes, conditions are left at
          the converter's default ``true`` and a warning is
          emitted — there is no fallback to variant-driven
          conditions in this mode.

        Inside-loop OR-forks and LoopFork counter conditions are
        the same in both modes.
    log
        Required when ``condition_strategy="data-driven"``: a
        pandas DataFrame in pm4py's event-log format (case +
        activity + attribute columns). Ignored for
        ``condition_strategy="variant"``.
    decision_tree_max_depth
        Maximum depth of the per-OR-fork decision tree (default
        ``3``). Deeper trees produce more accurate conditions
        but verbose expressions; ``3`` is a sweet spot that keeps
        the jUCMNav expression readable while still capturing
        the most informative splits.

    Returns
    -------
    UCM.ScenarioGroup
        The new scenario group, also registered under
        ``ucm.scenario_groups``.
    """
    if not clustering.variants:
        return ucm.add_scenario_group(name=group_name)

    if condition_strategy not in ("variant", "data-driven"):
        raise ValueError(
            f"condition_strategy must be 'variant' or 'data-driven'; "
            f"got {condition_strategy!r}"
        )

    target_map = _pick_map(ucm, map_name)

    variant_var: Optional[UCM.Variable] = None
    attribute_vars: Dict[str, UCM.Variable] = {}
    data_driven_features = None
    if condition_strategy == "variant":
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
            _VARIANT_VAR_NAME, type="enumeration",
            enumeration_type=enum_type,
        )
    else:
        # Data-driven: extract case-level features once, create one
        # Variable (and EnumerationType for enums) per usable
        # attribute. ``extract_case_features`` lifts event-level
        # columns that happen to be case-constant; if none survive
        # the type / cardinality filters we leave attribute_vars
        # empty and the OR-fork conditions stay at their default
        # ``true`` (with a warning).
        if log is None:
            raise ValueError(
                "condition_strategy='data-driven' requires "
                "the log= argument."
            )
        data_driven_features = _dm.extract_case_features(log)
        features_df, attribute_specs, feature_columns, per_case_raw = (
            data_driven_features
        )
        if features_df is not None:
            for jname, spec in attribute_specs.items():
                if spec.type == "enumeration":
                    et = ucm.get_or_add_enumeration_type(
                        f"{jname}_Type", values=list(spec.enum_values),
                    )
                    attribute_vars[jname] = ucm.get_or_add_variable(
                        jname, type="enumeration", enumeration_type=et,
                    )
                else:
                    attribute_vars[jname] = ucm.get_or_add_variable(
                        jname, type=spec.type,
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
        if condition_strategy == "variant" and variant_var is not None:
            sc.add_initialization(variant_var, variant.variant_id)
        elif condition_strategy == "data-driven" and attribute_vars:
            # Initialise every mined attribute to the variant's
            # representative value: median for numerics, mode for
            # enums / booleans. The cases in ``variant.case_ids``
            # form the cluster; we read their raw values from the
            # per-case original DataFrame returned by
            # extract_case_features and pick the representative
            # accordingly. Floats get the scale_factor applied so
            # the initialised integer matches what jUCMNav will
            # compare against in the mined conditions.
            for jname, var in attribute_vars.items():
                spec = attribute_specs[jname]
                value = _representative_value_for_variant(
                    variant, spec, per_case_raw,
                )
                if value is None:
                    continue
                sc.add_initialization(var, value)
        # Per-loop integer counter initialisation. Use the variant's
        # max observed iteration count, optionally capped at
        # ``max_loop_iterations`` so scenarios remain tractable to
        # step through in jUCMNav (and so nested loops don't blow up
        # multiplicatively).
        for tree_loop_id, counter_var in loop_counters.items():
            max_iter = variant.loop_iteration_max.get(tree_loop_id, 0)
            if max_loop_iterations is not None:
                max_iter = min(max_iter, max_loop_iterations)
            sc.add_initialization(counter_var, str(max_iter))
        for sp in starts:
            sc.add_start_point(sp, enabled=True)
        for ep in ends:
            sc.add_end_point(ep, enabled=True)
        group.add_scenario(sc)

    # Set conditions on OR-fork outgoing arcs. Outside-loop XORs get
    # variant_id disjunctions (or, in data-driven mode, decision-mined
    # expressions on case attributes); inside-loop XORs get combined
    # variant_id + loop_counter conditions so branches distribute
    # across loop iterations.
    or_fork_mining_results: List[_dm.OrForkMiningResult] = []
    if emit_conditions:
        if condition_strategy == "variant":
            _emit_orfork_conditions(
                target_map, tree, clustering.variants, loop_counters,
                max_loop_iterations=max_loop_iterations,
            )
        elif condition_strategy == "data-driven" and attribute_vars:
            or_fork_mining_results = _emit_orfork_conditions_data_driven(
                target_map, tree, clustering, log,
                attribute_specs, features_df, feature_columns,
                loop_counters,
                max_loop_iterations=max_loop_iterations,
                max_depth=decision_tree_max_depth,
            )
        # else: data-driven but no attributes — leave at default true
        # and rely on the warning already emitted by extract_case_features.

    # Stash mining results on the group so the report writer can find
    # them (the group is the only object the caller keeps a reference to
    # in the variant-driven path).
    group._mining_results = or_fork_mining_results  # type: ignore[attr-defined]

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
) -> List[Tuple[int, int, Optional[int]]]:
    """Pre-order list of ``(signature_int_id, n_branches,
    enclosing_loop_id)`` for every multi-child XOR/OR tree node.

    The converter creates one :class:`UCM.OrFork` node per such tree
    node, in the same pre-order traversal — so the i-th entry of this
    list always corresponds to the i-th non-LoopFork ``OrFork`` in the
    map's ``nodes`` list. ``enclosing_loop_id`` is ``None`` for an
    outside-loop XOR; for an inside-loop one it is the
    ``signature_int_id`` of the **innermost** enclosing LOOP tree
    node, which tells the synthesizer which loop counter to combine
    with ``variant_id`` to distribute branches across iterations."""
    out: List[Tuple[int, int, Optional[int]]] = []

    def _walk(node, enclosing_loop_id: Optional[int]) -> None:
        op = _op_value(node)
        children = list(getattr(node, "children", None) or [])
        if op in (_XOR, _OR) and len(children) >= 2:
            out.append((node_ids[id(node)], len(children), enclosing_loop_id))
        next_loop_id = (
            node_ids[id(node)] if op == _LOOP else enclosing_loop_id
        )
        for c in children:
            _walk(c, next_loop_id)

    _walk(tree, None)
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
        # before rewriting its expression. We also capture the
        # post-loop ``exit_target`` so the upstream LoopEntryGuard
        # can bypass the loop body when the counter starts at 0.
        exit_target: Optional[UCM.PathNode] = None
        for arc in lf.succ_connections:
            arc = _pull_condition_onto_direct_arc(arc)
            cond = arc.condition
            label = cond.label if cond else ""
            if label == "exit":
                # ``<= 0`` rather than ``== 0`` so the condition
                # stays exhaustive when the counter ends up
                # negative. The same ``<= 0`` predicate gates the
                # LoopEntryGuard below.
                expr = f"{name} <= 0"
                # The arc's target is a routing bend; following one
                # more hop gives the actual post-loop continuation
                # node the LoopEntryGuard should bypass to.
                exit_target = _resolve_post_loop_target(arc)
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

        # Splice a LoopEntryGuard OR-fork between upstream and
        # LoopJoin so a counter that starts at 0 bypasses the loop
        # body entirely (0 iterations) rather than running it once
        # and exiting at counter = -1 (1 iteration). The guard is
        # only checked on initial entry — the redo arc loops back
        # to LoopJoin directly, so subsequent iterations skip the
        # guard.
        if exit_target is not None:
            _insert_loop_entry_guard(target_map, lj, exit_target, name)

    return counters


def _resolve_post_loop_target(
    exit_arc: "UCM.NodeConnection",
) -> Optional["UCM.PathNode"]:
    """Follow ``exit_arc`` from LoopFork past a single routing bend
    to find the actual post-loop continuation node.

    The converter wires ``LoopFork -> exit_target``; the routing
    pass then splits that into ``LoopFork -> bend -> exit_target``.
    The LoopEntryGuard's bypass arc should land on
    ``exit_target`` (skipping the bend) so the loop body and the
    routing apparatus around the LoopFork are all bypassed in one
    hop."""
    target = exit_arc.target
    if isinstance(target, UCM.EmptyPoint) and len(target.succ_connections) == 1:
        return target.succ_connections[0].target
    return target


def _insert_loop_entry_guard(
    target_map: UCM.UCMmap,
    loop_join: "UCM.OrJoin",
    exit_target: "UCM.PathNode",
    counter_name: str,
) -> Optional["UCM.OrFork"]:
    """Splice a LoopEntryGuard OR-fork into the path between
    upstream and LoopJoin.

    Old: ``upstream -> LoopJoin`` (one incoming arc that came from
    outside the loop).
    New: ``upstream -> LoopEntryGuard``; LoopEntryGuard has two
    outgoing arcs, ``counter > 0`` to LoopJoin and ``counter <= 0``
    to ``exit_target``. The guard runs **only** on initial entry
    because the LoopFork's redo arc still feeds straight back into
    LoopJoin (not into the guard), so subsequent iterations bypass
    the guard. This restores the "counter = number of body
    executions" semantics: ``M_V = 0`` runs the body 0 times,
    ``M_V = 1`` runs it once, and so on.

    The guard is named ``LoopEntryGuard`` so the OR-fork-condition
    filter in :func:`_emit_orfork_conditions` can skip it (it's a
    synthetic node, not one of the XOR forks in the source tree)."""
    # The upstream incoming arc is the first added — the entry-side
    # arc that was wired before the redo. Routing typically left a
    # routing-bend EmptyPoint as its source.
    if not loop_join.pred_connections:
        return None
    upstream_arc = loop_join.pred_connections[0]
    upstream_src = upstream_arc.source

    guard = UCM.OrFork(name="LoopEntryGuard")
    target_map.add_node(guard)

    # Remove the existing upstream -> LoopJoin arc.
    target_map.remove_connection(upstream_arc)

    # upstream_src -> guard (no condition — passthrough).
    target_map.add_connection(upstream_src, guard)
    # guard -> LoopJoin when the counter still has iterations left.
    target_map.add_connection(
        guard, loop_join,
        condition=UCM.Condition(
            label="enter", expression=f"{counter_name} > 0",
        ),
    )
    # guard -> exit_target when the counter is at 0 (or below).
    target_map.add_connection(
        guard, exit_target,
        condition=UCM.Condition(
            label="bypass", expression=f"{counter_name} <= 0",
        ),
    )
    return guard


def _representative_value_for_variant(
    variant: Variant,
    spec: "_dm.AttributeSpec",
    per_case_raw,
) -> Optional[str]:
    """Pick the value to initialise this attribute to inside the
    variant's scenario.

    * Enumeration / boolean: modal value across the variant's cases
      (sanitised for enumerations so the literal matches what the
      mined expression compares against).
    * Integer: median across the variant's cases, multiplied by
      :attr:`AttributeSpec.scale_factor` and rounded so the
      initialiser value matches the encoded feature space.

    Returns ``None`` when no cases in the variant carry a non-null
    value for this attribute (no init emitted)."""
    case_ids = [str(cid) for cid in variant.case_ids]
    case_ids = [cid for cid in case_ids if cid in per_case_raw.index]
    if not case_ids:
        return None
    series = per_case_raw.loc[case_ids, spec.source_name].dropna()
    if series.empty:
        return None
    if spec.type == "enumeration":
        mode = series.mode()
        if mode.empty:
            return None
        return spec.sanitise_value(str(mode.iloc[0]))
    if spec.type == "boolean":
        mode = series.mode()
        if mode.empty:
            return None
        v = mode.iloc[0]
        return "true" if v in (True, 1, "true", "True") else "false"
    if spec.type == "integer":
        # Numeric — use median, apply scale factor, round to int.
        try:
            median = float(series.astype(float).median())
        except (TypeError, ValueError):
            return None
        scaled = int(round(median * spec.scale_factor))
        return str(scaled)
    return None


def _emit_orfork_conditions_data_driven(
    target_map: UCM.UCMmap,
    tree,
    clustering: ClusteringResult,
    log,
    attribute_specs: Dict[str, "_dm.AttributeSpec"],
    features_df,
    feature_columns: List[str],
    loop_counters: Dict[int, UCM.Variable],
    max_loop_iterations: Optional[int],
    max_depth: int = 3,
) -> List["_dm.OrForkMiningResult"]:
    """Replace outside-loop OR-fork conditions with decision-mined
    expressions trained on per-case attributes; keep inside-loop and
    LoopFork conditions on the existing counter machinery.

    Returns the per-OR-fork :class:`_dm.OrForkMiningResult` list so
    the report writer can describe the trained tree, accuracy, and
    feature set for each fork."""
    node_ids = _cs.assign_node_ids(tree)
    xor_seq = _collect_multi_child_xors(tree, node_ids)

    or_forks: List[UCM.OrFork] = [
        n for n in target_map.nodes
        if isinstance(n, UCM.OrFork)
        and n.name not in ("LoopFork", "LoopEntryGuard")
    ]
    if len(or_forks) != len(xor_seq):
        return []

    # Replay every case to collect per-XOR branch labels (outside-
    # loop only — inside-loop XORs are skipped for decision mining
    # since per-iteration choices aren't captured by case attributes).
    case_to_branch_per_xor: Dict[int, Dict[str, int]] = {
        x: {} for x, _, _ in xor_seq
    }
    case_id_col = "case:concept:name"
    for case_id, trace_df in log.groupby(case_id_col):
        trace = trace_df["concept:name"].tolist()
        xor_counts: Dict[int, Dict[int, int]] = {}
        sig = _cs.replay(
            tree, trace, node_ids=node_ids, coarsen_loops=True,
            xor_branch_counts=xor_counts,
        )
        if sig == _cs.NOFIT:
            continue
        for xor_id, branch_counts in xor_counts.items():
            # Outside-loop XOR: exactly one branch was taken (count 1).
            if sum(branch_counts.values()) == 1:
                chosen = next(iter(branch_counts))
                case_to_branch_per_xor[xor_id][str(case_id)] = chosen

    mining_results: List["_dm.OrForkMiningResult"] = []
    for of_idx, (tree_xor_id, n_branches, enclosing_loop_id) in enumerate(xor_seq):
        of = or_forks[of_idx]
        if enclosing_loop_id is not None:
            # Inside-loop XOR: data-driven can't disambiguate
            # per-iteration choices. Fall back to a deterministic
            # true/false split so the choice stays mutually
            # exclusive and exhaustive without involving variant_id.
            _set_deterministic_true_false_split(of)
            mining_results.append(_dm.OrForkMiningResult(
                tree_xor_id=tree_xor_id,
                n_branches=n_branches,
                skipped_reason="inside_loop",
                per_branch_expression={
                    k: ("true" if k == 0 else "false")
                    for k in range(n_branches)
                },
            ))
            continue
        # Train + translate.
        result = _dm.mine_orfork_condition(
            features_df, attribute_specs, feature_columns,
            case_to_branch_per_xor[tree_xor_id],
            tree_xor_id, n_branches,
            max_depth=max_depth,
        )
        mining_results.append(result)
        succs = of.succ_connections
        if len(succs) != n_branches:
            continue
        for k, arc in enumerate(succs):
            arc = _pull_condition_onto_direct_arc(arc)
            expr = result.per_branch_expression.get(k, "false")
            if arc.condition is None:
                arc.set_condition(UCM.Condition(
                    label=f"branch{k}", expression=expr,
                ))
            else:
                arc.condition.expression = expr
    return mining_results


def _set_inside_loop_xor_conditions(
    of: UCM.OrFork,
    tree_xor_id: int,
    n_branches: int,
    enclosing_loop_id: int,
    variants: List[Variant],
    loop_counters: Dict[int, UCM.Variable],
    max_loop_iterations: Optional[int] = None,
) -> None:
    """Wire an inside-loop XOR's outgoing arcs with combined
    ``variant_id`` + ``loop_counter`` conditions so each branch
    fires for a specific range of loop-counter values per variant.

    For each variant V that traversed this XOR, we know:

    * ``M_V`` — the variant's max loop iteration count
      (``variant.loop_iteration_max[enclosing_loop_id]``);
    * ``n_k_V`` — the total number of times branch ``k`` was taken
      across the variant's traces
      (``variant.xor_branch_totals[tree_xor_id][k]``).

    We treat the per-V counts as proportions of ``M_V`` evaluations
    and assign branches to counter ranges from high to low:

    * branch 0 fires while ``counter > M_V - n_0_V / sum * M_V``,
    * branch 1 fires while ``counter > M_V - (n_0_V + n_1_V) / sum * M_V``
      AND ``counter <= M_V - n_0_V / sum * M_V``,
    * and so on, with the last branch picking up everything ``<= 0``.

    The thresholds are integer-rounded so the partition is exact for
    the variant's specific counter trajectory. The per-V conditions
    are guarded by ``variant_id == V`` and joined with ``||`` across
    variants. For variants that never traversed the XOR (because
    they didn't enter the loop, or the loop ran only once with the
    XOR in the redo path), no per-V clause is added — that variant
    contributes nothing to this OR-fork's expression.

    Falls back to a deterministic ``true`` / ``false`` split if the
    enclosing loop's counter variable wasn't created (which can
    happen for tau-body loops the wiring step skipped) or if the
    XOR has more than two branches (range conditions on multi-way
    XORs are a follow-up)."""
    counter = loop_counters.get(enclosing_loop_id)
    if counter is None or n_branches > 2:
        # No counter to combine with, or multi-way XOR — fall back
        # to the simpler deterministic split.
        _set_deterministic_true_false_split(of)
        return

    counter_name = counter.name
    # Per-branch (variant_id, threshold-on-counter) clauses.
    per_variant_clauses: List[List[str]] = [[] for _ in range(n_branches)]
    for v in variants:
        branch_counts = v.xor_branch_totals.get(tree_xor_id, {})
        total = sum(branch_counts.values())
        if total == 0:
            continue
        m_v = v.loop_iteration_max.get(enclosing_loop_id, 0)
        # Cap M_V to match the counter initialisation cap so the
        # threshold arithmetic stays consistent with what jUCMNav
        # will actually see in the running scenario.
        if max_loop_iterations is not None:
            m_v = min(m_v, max_loop_iterations)
        if m_v <= 0:
            continue
        # Cumulative branch sums => integer thresholds on the counter.
        # branch i fires for counter in (lower_i, upper_i].
        upper = m_v
        for k in range(n_branches):
            n_k = branch_counts.get(k, 0)
            # Proportional split, integer-rounded so the partition
            # of [0, M_V] across branches sums to M_V exactly.
            piece = round(n_k / total * m_v)
            lower = upper - piece
            if k == n_branches - 1:
                lower = 0  # last branch picks up the remainder
            if upper > lower:
                # Build the per-V clause for this branch.
                if upper >= m_v and lower <= 0:
                    # Trivial whole-range — this branch is the only
                    # one this variant ever takes.
                    clause = f"{_VARIANT_VAR_NAME} == {v.variant_id}"
                elif upper >= m_v:
                    clause = (
                        f"{_VARIANT_VAR_NAME} == {v.variant_id} "
                        f"&& {counter_name} > {lower}"
                    )
                elif lower <= 0:
                    clause = (
                        f"{_VARIANT_VAR_NAME} == {v.variant_id} "
                        f"&& {counter_name} <= {upper}"
                    )
                else:
                    clause = (
                        f"{_VARIANT_VAR_NAME} == {v.variant_id} "
                        f"&& {counter_name} > {lower} "
                        f"&& {counter_name} <= {upper}"
                    )
                per_variant_clauses[k].append(clause)
            upper = lower

    succs = of.succ_connections
    for k, arc in enumerate(succs):
        arc = _pull_condition_onto_direct_arc(arc)
        clauses = per_variant_clauses[k] if k < len(per_variant_clauses) else []
        if not clauses:
            expr = "false"
        elif len(clauses) == 1:
            expr = clauses[0]
        else:
            expr = " || ".join(f"({c})" for c in clauses)
        if arc.condition is None:
            arc.set_condition(UCM.Condition(
                label=f"branch{k}", expression=expr,
            ))
        else:
            arc.condition.expression = expr


def _set_deterministic_true_false_split(of: UCM.OrFork) -> None:
    """Fallback for inside-loop XORs without a usable loop counter:
    pick branch 0 deterministically. Mutually exclusive and
    exhaustive, just not data-driven."""
    for k, arc in enumerate(of.succ_connections):
        arc = _pull_condition_onto_direct_arc(arc)
        expr = "true" if k == 0 else "false"
        if arc.condition is None:
            arc.set_condition(UCM.Condition(
                label=f"branch{k}", expression=expr,
            ))
        else:
            arc.condition.expression = expr


def _emit_orfork_conditions(
    target_map: UCM.UCMmap,
    tree,
    variants: List[Variant],
    loop_counters: Optional[Dict[int, UCM.Variable]] = None,
    max_loop_iterations: Optional[int] = None,
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
        if isinstance(n, UCM.OrFork)
        and n.name not in ("LoopFork", "LoopEntryGuard")
    ]
    if len(or_forks) != len(xor_seq):
        # Tree walk and converter disagree on XOR count; skip
        # condition emission rather than mis-attribute.
        return

    for of_idx, (tree_xor_id, n_branches, enclosing_loop_id) in enumerate(xor_seq):
        of = or_forks[of_idx]
        if enclosing_loop_id is not None:
            # Inside-loop XOR. We combine variant_id with the
            # enclosing loop's counter to distribute branches across
            # iterations.
            _set_inside_loop_xor_conditions(
                of, tree_xor_id, n_branches, enclosing_loop_id,
                variants, loop_counters or {},
                max_loop_iterations=max_loop_iterations,
            )
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
