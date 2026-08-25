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

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ....objects.ucm.obj import UCM
from ..variants import choice_signature as _cs
from ..variants import parses as _parses
from ..variants.clustering import ClusteringResult, Variant, _normalise_log
from . import decision_mining as _dm
from . import expression_minimizer as _emin


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
    parses: "Optional[_parses.ParseTable]" = None,
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
    parses
        Optional
        :class:`~pm4py_ucm.algo.discovery.variants.parses.ParseTable`
        from the **same tree**. Only the ``data-driven`` strategy uses
        it, to label which branch each case took at each outside-loop
        XOR. That labelling needs the log replayed and normalised into
        cases, and so did clustering, so passing the table clustering
        was given (or built) collapses the two into one pass — together
        those are the dominant cost on a large log. Sequences the table
        lacks are replayed on demand, so a stale or partial table costs
        time but never correctness.

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
        loop_counters = _wire_loop_counters(ucm, tree)

    # Decide, before anything is written, how many iterations each loop
    # runs per variant and which iterations take which branch. Both the
    # counter initialisations below and the branch conditions further
    # down read from this one plan, so they cannot disagree — which is
    # exactly how a branch used to end up with a range its scenario
    # never visited.
    loop_plan = _plan_loop_coverage(
        tree, list(clustering.variants), _cs.assign_node_ids(tree),
        max_loop_iterations,
    )

    # Build the ScenarioGroup with one ScenarioDef per variant.
    # Each scenario is named ``<variant_id>_<short_suffix>`` so the
    # jUCMNav scenarios panel shows a discriminator alongside the
    # generic v1/v2 IDs, and the description carries a plain-English
    # intent sentence in front of the technical breakdown.
    group = ucm.add_scenario_group(name=group_name)
    starts = target_map.start_points
    ends = target_map.end_points
    # Pre-compute the tree's signature-id map once; reuse for every
    # variant's suffix + intent.
    _scenario_node_ids = _cs.assign_node_ids(tree)
    for variant in clustering.variants:
        suffix = _variant_name_suffix(variant, tree, _scenario_node_ids)
        intent = _variant_intent(variant, tree, _scenario_node_ids)
        sc = UCM.ScenarioDef(
            name=f"{variant.variant_id}_{suffix}" if suffix else variant.variant_id,
            description=_format_description(variant, intent=intent),
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
        # Per-loop integer counter initialisation, from the coverage
        # plan: the variant's observed iteration count capped at
        # ``max_loop_iterations`` so scenarios remain tractable to step
        # through in jUCMNav, but never below the number of iterations
        # the body's choices need to all be demonstrable.
        for tree_loop_id, counter_var in loop_counters.items():
            max_iter = loop_plan.iterations.get(
                (tree_loop_id, variant.variant_id), 0)
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
                ucm, tree, clustering.variants, loop_counters,
                max_loop_iterations=max_loop_iterations,
                plan=loop_plan,
            )
        elif condition_strategy == "data-driven" and attribute_vars:
            or_fork_mining_results = _emit_orfork_conditions_data_driven(
                ucm, tree, clustering, log,
                attribute_specs, features_df, feature_columns,
                loop_counters,
                max_loop_iterations=max_loop_iterations,
                max_depth=decision_tree_max_depth,
                plan=loop_plan,
                parses=parses,
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

def _format_description(
    variant: Variant,
    intent: Optional[str] = None,
) -> str:
    """Compact human-readable description of a variant — partial order
    expression, frequency, linearization-count, and a truncated case-ID
    list. When an ``intent`` sentence is provided it is prepended as
    the first line so the jUCMNav scenario panel leads with plain
    English before showing the technical breakdown."""
    shown = variant.case_ids[:_MAX_CASE_IDS_IN_DESCRIPTION]
    extra = len(variant.case_ids) - len(shown)
    case_ids_part = ", ".join(shown)
    if extra > 0:
        case_ids_part += f", ... (+{extra} more)"
    header = f"Intent: {intent}\n" if intent else ""
    return (
        f"{header}"
        f"Partial-order: {variant.partial_order_expression}\n"
        f"Frequency: {variant.frequency} case(s); "
        f"linearizations: {variant.linearization_count}; "
        f"distinct sequences in log: {variant.sequence_variants}.\n"
        f"Case IDs: {case_ids_part}"
    )


# ---------------------------------------------------------------------------
# Variant naming and intent
# ---------------------------------------------------------------------------

#: Multi-iteration coarsened loops surface in the variant suffix as
#: ``"Two<Activity>"`` (e.g. ``TwoCloseAssessment``); single-iteration
#: loops are not mentioned (they're the common case).
_LOOP_COARSE_LABELS = {0: "Skip", 1: None, 2: "Two"}


def _variant_name_suffix(
    variant: Variant, tree, node_ids: Dict[int, int],
) -> str:
    """Derive a short descriptor for the variant's scenario name,
    e.g. ``"TwoCloseAssessment"`` for a variant whose distinctive
    feature is repeating the Close-Assessment loop.

    Order of preference for the discriminator:
      1. Any LOOP whose coarse iteration count is 2 (``>= 2``) — the
         most striking feature: this variant *repeats* something.
      2. The first non-loop XOR choice's branch activity — captures
         the "took A instead of B" decision.
      3. The variant's first activity in the partial-order
         expression — last resort, just gives the name *some*
         content.

    Falls back to ``"flow"`` for fully-trivial signatures."""
    # Map node IDs back to tree nodes for activity-name lookup.
    id_to_node = _build_id_to_node_map(tree, node_ids)

    def _collect(signature: tuple, loop_match: List[str],
                 xor_match: List[str], in_loop: bool) -> None:
        if not signature:
            return
        tag = signature[0]
        if tag == "ACT":
            return
        if tag == "SEQ":
            for f in signature[2]:
                _collect(f, loop_match, xor_match, in_loop)
        elif tag == "XOR":
            _, nid, idx, inner = signature
            if not in_loop:
                chosen_label = _first_activity_label(_xor_child(
                    id_to_node.get(nid), idx,
                ))
                if chosen_label:
                    xor_match.append(_short_activity_token(chosen_label))
            _collect(inner, loop_match, xor_match, in_loop)
        elif tag == "PAR":
            for f in signature[2]:
                _collect(f, loop_match, xor_match, in_loop)
        elif tag == "LOOP":
            _, nid, coarse = signature
            label = _LOOP_COARSE_LABELS.get(coarse)
            if label and label != "skip":
                body_label = _first_activity_label(_loop_body(
                    id_to_node.get(nid)
                ))
                if body_label:
                    loop_match.append(
                        f"{label}{_short_activity_token(body_label, max_len=14)}"
                    )
        elif tag == "LOOP_FINE":
            for f in signature[3]:
                _collect(f, loop_match, xor_match, True)

    loop_match: List[str] = []
    xor_match: List[str] = []
    _collect(variant.signature, loop_match, xor_match, False)
    pick = (loop_match[:1] or xor_match[:1] or ["flow"])
    return pick[0]


def _variant_intent(
    variant: Variant, tree, node_ids: Dict[int, int],
) -> str:
    """One-sentence plain-English description of a variant's intent
    — the recognisable shape of the cases it covers, in business
    terms rather than node IDs.

    The sentence template is:

      "{N} case(s) that {feature_1}, then {feature_2}, ..."

    where each feature mentions a distinct XOR branch taken, or a
    loop that the variant runs more than once. Single-iteration
    loops and structural sequence operators are not mentioned (they
    are the common case and would just inflate the sentence).
    Returns a generic ``"Covers {N} case(s)."`` when no
    distinguishing feature surfaces — true only for trivially-
    flat variants on trees without any choice or loop."""
    id_to_node = _build_id_to_node_map(tree, node_ids)
    features: List[str] = []

    def _walk(signature: tuple, in_loop: bool) -> None:
        if not signature:
            return
        tag = signature[0]
        if tag == "ACT":
            return
        if tag == "SEQ":
            for f in signature[2]:
                _walk(f, in_loop)
        elif tag == "XOR":
            _, nid, idx, inner = signature
            if not in_loop:
                label = _first_activity_label(_xor_child(
                    id_to_node.get(nid), idx,
                ))
                if label:
                    features.append(f"takes the {label} branch")
            _walk(inner, in_loop)
        elif tag == "PAR":
            for f in signature[2]:
                _walk(f, in_loop)
        elif tag == "LOOP":
            _, nid, coarse = signature
            body_label = _first_activity_label(_loop_body(
                id_to_node.get(nid)
            ))
            if body_label:
                if coarse == 2:
                    features.append(f"repeats {body_label}")
                elif coarse == 0:
                    features.append(f"skips {body_label}")
                # coarse == 1 is the common "loop runs once" case;
                # not worth mentioning in the intent sentence.
        elif tag == "LOOP_FINE":
            for f in signature[3]:
                _walk(f, True)

    _walk(variant.signature, False)
    if not features:
        return f"Covers {variant.frequency} case(s)."
    # Keep the sentence reasonable: at most three features, then ``...``.
    if len(features) > 3:
        features = features[:3] + ["..."]
    joined = ", then ".join(features)
    return f"Covers {variant.frequency} case(s) that {joined}."


def _build_id_to_node_map(tree, node_ids: Dict[int, int]) -> Dict[int, object]:
    """Invert ``node_ids`` so we can look up the original tree node
    object from its stable signature ID — needed to fetch activity
    labels for XOR branches and loop bodies."""
    out: Dict[int, object] = {}

    def _walk(node):
        out[node_ids[id(node)]] = node
        for c in (getattr(node, "children", None) or []):
            _walk(c)

    _walk(tree)
    return out


def _xor_child(xor_node, branch_idx: int):
    """Safely fetch ``xor_node.children[branch_idx]`` or ``None``."""
    if xor_node is None:
        return None
    children = getattr(xor_node, "children", None) or []
    if 0 <= branch_idx < len(children):
        return children[branch_idx]
    return None


def _loop_body(loop_node):
    """Return the body subtree (children[0]) of a LOOP, or ``None``."""
    if loop_node is None:
        return None
    children = getattr(loop_node, "children", None) or []
    return children[0] if children else None


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


def _children(node) -> List[Any]:
    return list(getattr(node, "children", None) or [])


def _index_ucm_forks_by_tree_id(
    ucm: UCM, node_ids: Dict[int, int],
) -> Dict[int, "UCM.PathNode"]:
    """Build ``{tree_stable_id: ucm_fork_or_join}`` over every map.

    The converter stamps ``_tree_python_id = id(tree_node)`` on each
    OR-fork / OR-join / LoopFork / LoopJoin it creates from a tree
    operator (see :mod:`pm4py_ucm.objects.ucm.conversion.\
from_process_tree`). Combined with the synthesizer's
    ``node_ids: {id(tree_node) -> stable_int}``, that gives a direct
    tree↔UCM correspondence — no cross-map pre-order assumption.

    Why this matters: an earlier position-based correlation worked
    accidentally on ClaimsPaymentLog because plug-in maps were created
    in tree-traversal order with no root-map fork *after* a plug-in
    fork. The general case interleaves them, and the resulting
    misalignment caused (a) loop decrement responsibilities being
    attached to the wrong activity, and (b) variant_id conditions
    being written onto OR-forks that didn't correspond to the
    intended tree XOR — leaving some variants without a matching
    branch, deadlocking their scenarios at runtime.

    Returns one mapping per stable id; nodes without a
    ``_tree_python_id`` (synthetic LoopEntryGuard, routing bends, …)
    are skipped."""
    out: Dict[int, "UCM.PathNode"] = {}
    for m in ucm.maps:
        for n in m.nodes:
            py_id = getattr(n, "_tree_python_id", None)
            if py_id is None:
                continue
            stable = node_ids.get(py_id)
            if stable is None:
                continue
            # OR-fork / OR-join pairs share the same stable id; the
            # synthesizer asks for the fork OR the join via type, so
            # storing both under separate sub-keys keeps both
            # reachable. We tag the value's role inline.
            key = (stable, type(n).__name__, getattr(n, "name", ""))
            out[key] = n  # type: ignore[assignment]
    return out


def _ucm_node_for(
    index: Dict[Tuple[int, str, str], "UCM.PathNode"],
    stable_id: int,
    cls_name: str,
    name_match: str,
) -> Optional["UCM.PathNode"]:
    """Look up a single UCM node by (tree_stable_id, class, fork name).
    ``cls_name`` is ``"OrFork"`` / ``"OrJoin"`` (the class repr name);
    ``name_match`` is the UCM node's ``.name`` attribute (``"OrFork"``,
    ``"OrJoin"``, ``"LoopFork"``, ``"LoopJoin"``)."""
    return index.get((stable_id, cls_name, name_match))


def _owning_map(ucm: UCM, node: "UCM.PathNode") -> Optional["UCM.UCMmap"]:
    """Return the map ``node`` is registered with (linear scan over
    ``ucm.maps``). ``None`` if not found — caller decides what to do.
    Used by the loop-counter scaffolding to splice the LoopEntryGuard
    into the right map when decomposition pushed a loop into a
    plug-in."""
    for m in ucm.maps:
        if node in m.nodes:
            return m
    return None


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
# Loop iteration budgeting and branch scheduling
# ---------------------------------------------------------------------------
#
# A scenario demonstrates a variant by walking the model once. Inside a
# loop, the only lever for "this iteration goes left, the next goes
# right" is the loop counter, so every choice in the body is expressed as
# a predicate over it. Two things then decide whether the generated
# scenarios collectively exercise **every** branch of the model:
#
#   1. the loop must run often enough. A body containing a 4-way choice
#      cannot show all four branches in 2 iterations, whatever the
#      conditions say;
#   2. the branch-to-iteration assignment must be exact. Distributing
#      branches by rounded proportions can round a branch down to an
#      empty range, and a nested choice — evaluated only on the
#      iterations where its parent branch is taken — needs its ranges
#      carved out of *those* iterations, not out of the whole run.
#
# The planner below settles both before any condition is written: it
# sizes each loop from the tree's own structure and then hands every
# (choice, branch, variant) an explicit, contiguous slice of iterations.


def _branches_to_cover(node, variant: "Variant",
                       node_ids: Dict[int, int]) -> List[int]:
    """Branch indices of ``node`` this variant actually takes."""
    counts = variant.xor_branch_totals.get(node_ids[id(node)], {})
    return sorted(k for k, n in counts.items() if n > 0)


def _iterations_to_cover(node, variant: Optional["Variant"],
                         node_ids: Dict[int, int]) -> int:
    """Body iterations needed to exercise every branch inside ``node``.

    The recursion is the whole idea:

    * a **choice** needs the *sum* of its branches' requirements — one
      iteration can only go down one branch, so they cannot share;
    * a **sequence** or **parallel** block needs the *max* — its children
      all run in the same iteration, so their requirements overlap;
    * a **nested loop** needs 1: it has its own counter, and covering
      what is inside it is that counter's job;
    * a leaf needs 1.

    With ``variant`` given, only the branches that variant takes count —
    a scenario has no reason to demonstrate behaviour its own cases never
    exhibited, and the other variants cover those branches.
    """
    op = _op_value(node)
    children = _children(node)
    if op in (_XOR, _OR) and children:
        wanted = (_branches_to_cover(node, variant, node_ids)
                  if variant is not None else list(range(len(children))))
        if not wanted:
            return 1
        return sum(_iterations_to_cover(children[k], variant, node_ids)
                   for k in wanted)
    if op == _LOOP:
        return 1
    if children:
        return max(_iterations_to_cover(c, variant, node_ids)
                   for c in children)
    return 1


def suggest_loop_iterations(tree) -> Dict[int, int]:
    """``{loop_node_id: iterations}`` — how many times each LOOP in
    ``tree`` must run for a scenario to be able to exercise every branch
    inside its body.

    Derived from the tree's shape alone (see
    :func:`_iterations_to_cover`), so it answers "how big does this loop
    have to be?" before any log is replayed. The synthesizer uses it as a
    floor on each scenario's loop counter; a caller can use it to predict
    how long the generated scenarios will be to step through.
    """
    node_ids = _cs.assign_node_ids(tree)
    out: Dict[int, int] = {}

    def walk(node) -> None:
        if _op_value(node) == _LOOP:
            children = _children(node)
            if children:
                need = _iterations_to_cover(children[0], None, node_ids)
                if len(children) > 1:
                    # The redo path runs once between iterations, so it
                    # gets one evaluation fewer than the body.
                    need = max(need, 1 + _iterations_to_cover(
                        children[1], None, node_ids))
                out[node_ids[id(node)]] = need
        for child in _children(node):
            walk(child)

    walk(tree)
    return out


@dataclass
class _LoopPlan:
    """Per-variant loop sizes and the branch schedule that fills them."""

    #: ``{(loop_node_id, variant_id): iterations}``
    iterations: Dict[Tuple[int, str], int] = field(default_factory=dict)
    #: ``{(xor_node_id, branch, variant_id): (lower, upper)}`` — the
    #: branch fires while ``lower < counter <= upper``.
    ranges: Dict[Tuple[int, int, str], Tuple[int, int]] = field(
        default_factory=dict)


def _schedule_branches(node, iters: List[int], variant: "Variant",
                       node_ids: Dict[int, int], total: int,
                       plan: "_LoopPlan") -> None:
    """Hand each choice under ``node`` a slice of ``iters``.

    ``iters`` are the 1-based iteration numbers on which ``node`` is
    evaluated; a choice splits them among the branches this variant
    takes, and each branch's subtree is then scheduled over its own
    slice. Slices are contiguous so the resulting predicate is a range
    on the counter rather than an enumeration of values.

    The counter runs *down*: it is initialised to ``total`` and
    decremented at the end of each iteration, so iteration ``j`` sees
    ``total - j + 1``.
    """
    op = _op_value(node)
    children = _children(node)
    if not children or not iters:
        return
    if op == _LOOP:
        return                      # its own counter; planned separately
    if op not in (_XOR, _OR):
        for child in children:      # sequence / parallel: same iterations
            _schedule_branches(child, iters, variant, node_ids, total, plan)
        return

    wanted = _branches_to_cover(node, variant, node_ids)
    if not wanted:
        return                      # this variant never reaches the choice
    xor_id = node_ids[id(node)]
    counts = variant.xor_branch_totals.get(xor_id, {})

    # Every branch gets at least what its own subtree needs; whatever is
    # left over is shared out in proportion to how often the variant
    # actually took each branch, so the busiest branch also looks busiest.
    sizes = [_iterations_to_cover(children[k], variant, node_ids)
             for k in wanted]
    if sum(sizes) > len(iters):
        # Not enough iterations for every branch's full subtree. The
        # planner sizes loops to prevent this, so reaching here means an
        # externally imposed budget; spend what there is one iteration
        # per branch, widest coverage first, rather than letting the
        # trailing branches fall off the end with nothing.
        sizes = [1] * len(wanted)
        while sum(sizes) > len(iters) and len(sizes) > 1:
            sizes.pop()
            wanted = wanted[:len(sizes)]
    spare = len(iters) - sum(sizes)
    if spare > 0:
        weights = [counts.get(k, 0) for k in wanted]
        weight_total = sum(weights) or len(wanted)
        for i, w in enumerate(weights):
            sizes[i] += spare * (w or 0) // weight_total
        # Hand any rounding remainder to the heaviest branches.
        for i in sorted(range(len(wanted)), key=lambda j: -weights[j]):
            if sum(sizes) >= len(iters):
                break
            sizes[i] += 1

    pos = 0
    for i, branch in enumerate(wanted):
        chunk = iters[pos:pos + sizes[i]]
        pos += sizes[i]
        if not chunk:
            continue
        # Iterations chunk[0]..chunk[-1] map to counter values
        # total-chunk[-1]+1 .. total-chunk[0]+1, i.e. the half-open
        # range (total-chunk[-1], total-chunk[0]+1].
        plan.ranges[(xor_id, branch, variant.variant_id)] = (
            total - chunk[-1], total - chunk[0] + 1,
        )
        _schedule_branches(children[branch], chunk, variant, node_ids,
                           total, plan)


def _plan_loop_coverage(
    tree, variants: List["Variant"], node_ids: Dict[int, int],
    max_loop_iterations: Optional[int],
) -> "_LoopPlan":
    """Size every loop per variant and schedule its body's choices.

    A loop's size is the largest of what the variant *observed* (capped
    by ``max_loop_iterations`` to keep scenarios steppable) and what
    covering that variant's own branches *requires*. The cap therefore
    trims a loop that merely ran many times, but never below the point
    where a branch would become undemonstrable — otherwise a scenario
    would silently omit part of the model it claims to describe.
    """
    plan = _LoopPlan()
    loop_nodes: List[Tuple[int, Any]] = []

    def collect(node) -> None:
        if _op_value(node) == _LOOP and _children(node):
            loop_nodes.append((node_ids[id(node)], node))
        for child in _children(node):
            collect(child)

    collect(tree)

    for loop_id, loop_node in loop_nodes:
        children = _children(loop_node)
        body = children[0]
        for variant in variants:
            raw_observed = variant.loop_iteration_max.get(loop_id, 0)
            if raw_observed <= 0:
                continue            # this variant never entered the loop
            observed = raw_observed
            if max_loop_iterations is not None:
                observed = min(observed, max_loop_iterations)
            needed = _iterations_to_cover(body, variant, node_ids)
            if len(children) > 1 and raw_observed >= 2:
                # The redo path runs once BETWEEN iterations, so it gets
                # total-1 evaluations. A choice there needs one more
                # iteration than it would in the body. Only variants that
                # actually loop back reach it.
                needed = max(needed, 1 + _iterations_to_cover(
                    children[1], variant, node_ids))
            total = max(observed, needed, 1)
            plan.iterations[(loop_id, variant.variant_id)] = total
            _schedule_branches(body, list(range(1, total + 1)), variant,
                               node_ids, total, plan)
            # The redo child is inside the loop too, but it runs BETWEEN
            # body iterations — once after each iteration that loops
            # back, so M-1 times. After iteration j the counter reads
            # M-j, which is what iteration index j+1 maps to, so the redo
            # schedules over indices 2..M under the same mapping.
            if len(children) > 1 and total > 1:
                _schedule_branches(children[1], list(range(2, total + 1)),
                                   variant, node_ids, total, plan)
    return plan


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
    return [nid for nid, _ in _collect_loop_tree_nodes(tree, node_ids)]


def _collect_loop_tree_nodes(
    tree, node_ids: Dict[int, int],
) -> List[Tuple[int, object]]:
    """Pre-order list of ``(tree_id, loop_tree_node)`` for every LOOP
    operator. Same iteration order as :func:`_collect_loop_tree_ids`
    so callers can pair UCM ``LoopJoin``/``LoopFork`` nodes with the
    originating tree subtree (needed to derive a human-readable
    counter name from the loop body's activities)."""
    out: List[Tuple[int, object]] = []

    def _walk(node):
        op = _op_value(node)
        children = list(getattr(node, "children", None) or [])
        if op == _LOOP and len(children) >= 2:
            out.append((node_ids[id(node)], node))
        for c in children:
            _walk(c)

    _walk(tree)
    return out


def _short_activity_token(text: str, max_len: int = 18) -> str:
    """Turn an activity label like ``"Amend Documentation"`` into a
    compact camel-case token like ``"AmendDoc"``.

    Drops non-word characters, title-cases each word, then truncates
    to ``max_len``. Falls back to ``"Step"`` for empty input."""
    if not text:
        return "Step"
    # Split on any non-word character *and* on underscores — activity
    # labels often contain ``_`` as a soft separator (e.g.
    # ``"Close Assessment_Human"``), and treating them as word
    # boundaries gives a cleaner camel-case token after truncation.
    parts = re.split(r"[\W_]+", str(text))
    parts = [p for p in parts if p]
    if not parts:
        return "Step"
    # Title-case each part; keep all-caps acronyms intact.
    pieces = [p if p.isupper() else p[:1].upper() + p[1:].lower()
              for p in parts]
    joined = "".join(pieces)
    if len(joined) <= max_len:
        return joined
    # Truncate evenly across pieces — keep at least the first piece
    # whole, then take prefixes of remaining pieces.
    out = pieces[0][:max_len]
    for p in pieces[1:]:
        if len(out) >= max_len:
            break
        room = max_len - len(out)
        out += p[:max(3, room)]
    return out[:max_len]


def _loop_context_name(
    loop_tree_node, fallback_id: int,
) -> str:
    """Derive a short, contextual counter-variable name for a LOOP
    tree node from its body and (if needed) its redo activities.

    Result format: ``Loop_<ShortToken>``, e.g. ``Loop_AmendDoc`` for
    a loop whose body or redo references ``"Amend Documentation"``.
    Falls back to ``loop_counter_<tree_id>`` (the historical naming)
    when no activity label is reachable from either branch."""
    children = list(getattr(loop_tree_node, "children", None) or [])
    body = children[0] if children else None
    redo = children[1] if len(children) > 1 else None

    # Prefer the body's first activity. Fall back to the redo's
    # first activity when the body is tau / unlabelled.
    chosen: Optional[str] = None
    for candidate in (body, redo):
        if candidate is None:
            continue
        label = _first_activity_label(candidate)
        if label:
            chosen = label
            break
    if not chosen:
        return f"loop_counter_{fallback_id}"
    return f"Loop_{_short_activity_token(chosen)}"


def _first_activity_label(tree_node) -> Optional[str]:
    """Depth-first search of ``tree_node`` for the first visible
    activity (non-``tau``) leaf, returning its label or ``None``."""
    op = _op_value(tree_node)
    if op is None:
        lab = _label(tree_node)
        return str(lab) if lab is not None else None
    for c in (getattr(tree_node, "children", None) or []):
        found = _first_activity_label(c)
        if found is not None:
            return found
    return None


def _label(node):
    return getattr(node, "label", None)


def _synthesize_decrement_resp_ref(
    ucm: UCM,
    target_map: UCM.UCMmap,
    loop_fork: UCM.PathNode,
    counter_name: str,
) -> UCM.RespRef:
    """Insert a synthetic ``decrement_<counter>`` :class:`UCM.RespRef`
    at the **end** of the loop body — on the arc entering ``loop_fork``
    — and return it.

    Two properties make this the right place, and both matter:

    * **it always runs.** Every path through the body converges on the
      LoopFork, so nothing can bypass it. Attaching the decrement to an
      arbitrary body responsibility instead risks a body that starts
      with a choice, where that responsibility sits in one branch and
      iterations taking another never decrement — the counter never
      reaches 0 and the scenario spins forever.
    * **it runs after the body's choices.** The inside-loop XOR
      conditions (see :func:`_set_inside_loop_xor_conditions`) partition
      the counter's range as ``(lower, upper]`` with ``upper`` starting
      at the counter's *initial* value ``M_V``, so they assume the
      counter still holds ``M_V`` the first time a body XOR is
      evaluated. Decrementing at the top of the body instead shifts
      every evaluation down by one, leaving the topmost range
      unreachable: the branch that should fire on the first iteration
      never fires at all.

    The visual layout coordinates default to (0, 0); the auto-layouter
    would normally re-flow on the next save, but this kind of edit
    happens after the converter's layout pass — see the comment in
    :func:`_wire_loop_counters` for the resulting trade-off."""
    name = f"decrement_{counter_name}"
    resp_def = ucm.get_or_add_responsibility(name)
    ref = UCM.RespRef(name=name, resp_def=resp_def)
    target_map.add_node(ref)
    # Splice into the body's arc INTO LoopFork: src -> ref -> LoopFork.
    in_arc = loop_fork.pred_connections[0]
    src, cond = in_arc.source, in_arc.condition
    target_map.remove_connection(in_arc)
    target_map.add_connection(src, ref, condition=cond)
    target_map.add_connection(ref, loop_fork)
    return ref


def _wire_loop_counters(
    ucm: UCM, tree,
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

    # Tree↔UCM correspondence by stable id. This replaces an earlier
    # cross-map positional walk that — once decomposition spread
    # loops across plug-in maps — paired the wrong LoopJoin with the
    # wrong tree LOOP node, with the visible consequence that loop
    # decrement responsibilities ended up on the wrong activity (and
    # the affected loop never terminated).
    ucm_index = _index_ucm_forks_by_tree_id(ucm, node_ids)

    # Pull the LOOP tree nodes too so each counter gets a contextual
    # name (e.g. ``Loop_AmendDoc``) derived from its body / redo
    # activities. The order matches tree_loop_ids exactly.
    loop_tree_nodes = _collect_loop_tree_nodes(tree, node_ids)
    name_taken: set = {v.name for v in ucm.variables}
    counters: Dict[int, UCM.Variable] = {}
    for i, tree_loop_id in enumerate(tree_loop_ids):
        lf = _ucm_node_for(ucm_index, tree_loop_id, "OrFork", "LoopFork")
        lj = _ucm_node_for(ucm_index, tree_loop_id, "OrJoin", "LoopJoin")
        if lf is None or lj is None:
            continue  # converter elided this loop (shouldn't happen)
        loop_node = loop_tree_nodes[i][1]
        # Pick a unique contextual name, falling back to the historic
        # ``loop_counter_<id>`` form on collision (rare — happens when
        # two loops share the same body activity name).
        base_name = _loop_context_name(loop_node, tree_loop_id)
        name = base_name
        suffix = 2
        while name in name_taken:
            name = f"{base_name}_{suffix}"
            suffix += 1
        name_taken.add(name)
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
        # Use the map the LoopJoin actually lives in — with decomposition
        # the loop may have been pushed into a plug-in map, and
        # synthesising the decrement responsibility into the root map
        # would put it outside the loop body.
        lj_map = _owning_map(ucm, lj) or ucm.maps[0]
        # Always a dedicated node at the end of the body, never an
        # arbitrary body responsibility: the position has to be both
        # unavoidable and after the body's choices, and only this one
        # is. See _synthesize_decrement_resp_ref for what goes wrong
        # with each of the alternatives.
        body_resp = _synthesize_decrement_resp_ref(ucm, lj_map, lf, name)
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
            _insert_loop_entry_guard(lj_map, lj, exit_target, name, ucm=ucm)

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


def _splice_orjoin_before_stub(
    ucm: UCM,
    target_map: UCM.UCMmap,
    stub: "UCM.Stub",
) -> "UCM.OrJoin":
    """Insert an OrJoin in front of ``stub`` and route all of the
    stub's existing predecessor arcs through it.

    Background: the converter wires a stub with a single predecessor
    arc and registers that arc as the stub's plug-in entry binding
    (``InBinding.stub_entry`` points at it). When the scenario
    synthesizer splices a LoopEntryGuard whose bypass arc lands on a
    stub, the stub ends up with two incoming arcs but still only one
    binding — scenarios that take the bypass arc hit an unbound
    entry and the jUCMNav traversal fails partway through (the
    user-reported v12 / v23 / … scenarios that exercise the 0-iteration
    path).

    Fix: merge the old and new incoming arcs through an OrJoin so the
    stub keeps its single bound entry. Every old predecessor arc's
    target moves to the OrJoin; a fresh OrJoin → Stub arc replaces
    the bindings' ``stub_entry``. Callers then point their new arc(s)
    at the returned OrJoin instead of at the stub directly.

    Idempotent: if the stub already has an OrJoin as its sole
    predecessor's source, returns that OrJoin.
    """
    preds = list(stub.pred_connections)
    if len(preds) == 1 and isinstance(preds[0].source, UCM.OrJoin):
        return preds[0].source  # already routed through a join

    orjoin = UCM.OrJoin(name="OrJoin")
    target_map.add_node(orjoin)

    # Re-route every existing predecessor to land on the OrJoin
    # instead of the stub. Conditions on those arcs survive the move.
    redirected: List[UCM.NodeConnection] = []
    for arc in preds:
        src = arc.source
        cond = arc.condition
        target_map.remove_connection(arc)
        new_arc = target_map.add_connection(src, orjoin, condition=cond)
        redirected.append(new_arc)

    # Single new arc OrJoin -> Stub. This is the arc that any
    # in_binding on this stub now refers to.
    stub_entry_arc = target_map.add_connection(orjoin, stub)

    # Update the stub's plug-in in_bindings: any binding whose
    # stub_entry was one of the removed arcs is repointed at the
    # new OrJoin -> Stub arc. Out_bindings don't change (the stub's
    # outgoing side is untouched).
    removed_set = {id(a) for a in preds}
    for binding in stub.bindings:
        for ib in binding.in_bindings:
            if id(ib.stub_entry) in removed_set:
                ib.stub_entry = stub_entry_arc
    return orjoin


def _splice_orjoin_before_join(
    target_map: UCM.UCMmap,
    and_join: "UCM.AndJoin",
    loop_join: "UCM.PathNode",
) -> "UCM.PathNode":
    """Insert an OrJoin on the arc by which the loop reaches
    ``and_join``, and return it for the bypass arc to land on.

    Only the ONE incoming arc that carries the loop's exit is
    re-routed: the AND-join's other predecessors are the concurrent
    branches it exists to synchronise, and folding them into an OrJoin
    would turn the parallel block into a choice.

    The loop's arc is the one reachable backwards from ``and_join``
    without passing through the loop's own join — i.e. the one fed by
    the loop body. Idempotent: an OrJoin already sitting there is
    reused."""
    loop_arc = None
    for arc in and_join.pred_connections:
        if isinstance(arc.source, UCM.OrJoin):
            return arc.source          # already merged here
        seen = {id(and_join)}
        stack = [arc.source]
        while stack:
            node = stack.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            if node is loop_join:
                break              # this arc is fed by the loop body
            stack.extend(p.source for p in node.pred_connections)
        else:
            continue
        loop_arc = arc
        break
    if loop_arc is None:
        return and_join                # not reachable from the loop; leave as is

    src, cond = loop_arc.source, loop_arc.condition
    target_map.remove_connection(loop_arc)
    orjoin = UCM.OrJoin(name="OrJoin")
    target_map.add_node(orjoin)
    target_map.add_connection(src, orjoin, condition=cond)
    target_map.add_connection(orjoin, and_join)
    return orjoin


def _splice_orjoin_before_single_entry(
    target_map: UCM.UCMmap,
    target: "UCM.PathNode",
) -> "UCM.PathNode":
    """Insert an OrJoin in front of a node that admits ONE incoming arc.

    Almost every path node does: a responsibility, an empty point, a fork
    of either kind and an end point all sit on exactly one path segment.
    Landing a second arc on one of them produces a model that serialises,
    renders and even traverses, but is not a UCM — jUCMNav draws the extra
    segment and its own metamodel forbids it.

    So the node's existing arc and the new one are merged through an
    OrJoin, which is the construct that exists for exactly this: several
    alternatives entering, one path leaving. Returns the node the caller
    should connect to instead.

    Idempotent, and a no-op when there is nothing to merge (no existing
    predecessor, or an OrJoin already in place)."""
    preds = list(target.pred_connections)
    if not preds:
        return target                  # nothing to merge with
    if len(preds) == 1 and isinstance(preds[0].source, UCM.OrJoin):
        return preds[0].source         # already merged here
    if len(preds) > 1:
        # Already malformed, or a kind that legitimately takes several.
        # Either way, adding one more arc is not this function's call.
        return target
    arc = preds[0]
    src, cond = arc.source, arc.condition
    target_map.remove_connection(arc)
    orjoin = UCM.OrJoin(name="OrJoin")
    target_map.add_node(orjoin)
    target_map.add_connection(src, orjoin, condition=cond)
    target_map.add_connection(orjoin, target)
    return orjoin


def _insert_loop_entry_guard(
    target_map: UCM.UCMmap,
    loop_join: "UCM.OrJoin",
    exit_target: "UCM.PathNode",
    counter_name: str,
    ucm: Optional[UCM] = None,
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
    # If the exit_target is a Stub, route through an OrJoin so the
    # stub keeps its single bound plug-in entry instead of growing a
    # second, unbound incoming arc (see _splice_orjoin_before_stub).
    # WHERE THE BYPASS MAY LAND.
    #
    # An OrJoin takes as many incoming arcs as you like — that is what it
    # is for — so the bypass can land on one directly. Every other kind
    # has a fixed arity, and landing a second arc on one produces a model
    # that exports and renders but is not a UCM: a responsibility with two
    # incoming path segments, an AND-fork with two. Those went unnoticed
    # because nothing downstream checks (see
    # :mod:`pm4py_ucm.objects.ucm.validate`, added with this fix).
    #
    # Stub and AndJoin need their own splices for reasons beyond arity,
    # so they keep them; everything else merges through a plain OrJoin.
    bypass_target = exit_target
    if isinstance(exit_target, UCM.OrJoin):
        pass                           # merges by nature; land directly
    elif isinstance(exit_target, UCM.Stub) and ucm is not None:
        bypass_target = _splice_orjoin_before_stub(
            ucm, target_map, exit_target,
        )
    elif isinstance(exit_target, UCM.AndJoin):
        # An AND-join waits for EVERY incoming arc, so landing the
        # bypass on one directly raises its arity: a 2-branch parallel
        # whose join now needs 3 tokens can never fire, and the whole
        # scenario deadlocks there. (Reported from jUCMNav as "Traversal
        # blocked on AndJoin" / "Infinite loop detected on AndJoin".)
        # The bypass and the loop's exit are ALTERNATIVES, so merge just
        # those two through an OrJoin — merging all of the join's
        # predecessors, as the Stub case does, would instead collapse
        # the parallelism itself.
        bypass_target = _splice_orjoin_before_join(
            target_map, exit_target, loop_join,
        )
    else:
        bypass_target = _splice_orjoin_before_single_entry(
            target_map, exit_target,
        )
    target_map.add_connection(
        guard, bypass_target,
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
        # Case-insensitive: a boolean column may carry raw "TRUE" /
        # "False" / "1" spellings (issue #6).
        return "true" if _dm._boolean_value(mode.iloc[0]) else "false"
    if spec.type == "integer":
        # Numeric — use median, apply scale factor, round to int.
        # The source column may be a coerced string column, so parse
        # with ``to_numeric``: an unparseable straggler drops out of
        # the median instead of costing the variant its initialisation.
        try:
            import pandas as pd

            numeric = pd.to_numeric(series, errors="coerce").dropna()
            if numeric.empty:
                return None
            median = float(numeric.median())
        except (TypeError, ValueError):
            return None
        scaled = int(round(median * spec.scale_factor))
        return str(scaled)
    return None


def _label_cases_by_xor_branch(
    tree,
    log,
    node_ids: Dict[int, int],
    xor_ids: List[int],
    parses: "Optional[_parses.ParseTable]" = None,
) -> Dict[int, Dict[str, int]]:
    """``{xor_node_id: {case_id: branch_index}}`` — which branch each
    case took, at every XOR the case decided exactly once.

    A trace's parse is a function of its activity sequence alone, so
    every case carrying the same sequence gets the same labels. This
    replays each **distinct sequence** once and fans the result out
    over the cases sharing it; it used to replay once per *case*, which
    on Road Traffic meant 150 370 replays where 231 suffice. That was
    the whole gap between the data-driven strategy's runtime and the
    variant-driven one's — the latter needs no replay of its own, since
    clustering already handed it per-variant branch counts drawn from
    the one pass in :mod:`..variants.parses`.

    ``parses`` is that same table: hand over the one clustering already
    built and the two share a single replay pass over the log — and its
    normalised ``[(case_id, [activity, …]), …]`` view as well, which
    saves walking the log a second time. Turning a 561 000-row frame
    into cases costs ~27 s, more than replaying its distinct sequences
    does, so it is worth doing once. Sequences the table does not carry
    are replayed on demand.

    Reading that view rather than re-deriving it is also what makes the
    labelling and the clustering agree by construction: both now see
    each case's events in the order :func:`_normalise_log` puts them in,
    where re-grouping the frame here used to take them in row order, and
    the two could disagree on a log whose events are not stored in
    timestamp order.

    Only ``xor_branch_counts`` and fit/no-fit are read from the parses,
    and both are independent of the table's ``coarsen_loops`` setting:
    coarsening changes how a fitting parse is *reported* (``LOOP``
    against ``LOOP_FINE``), never which parse the search finds or how
    much budget it spends getting there. A table built for uncoarsened
    clustering therefore labels identically.
    """
    labels: Dict[int, Dict[str, int]] = {x: {} for x in xor_ids}
    table = parses if parses is not None else _parses.ParseTable(
        node_ids=node_ids,
    )
    cases = (table.cases if table.cases is not None
             else _normalise_log(log))
    # ``{sequence: {xor_id: branch}}``, or ``None`` for a sequence that
    # does not replay — a non-conforming case contributes no label.
    per_sequence: Dict[Tuple[str, ...], Optional[Dict[int, int]]] = {}
    for case_id, trace in cases:
        sequence = tuple(trace)
        if sequence not in per_sequence:
            parse = table.ensure(tree, sequence, node_ids=node_ids)
            per_sequence[sequence] = None if not parse.fits else {
                # An outside-loop XOR is decided exactly once, so its
                # counts sum to 1. An inside-loop one is decided per
                # iteration and sums higher; it is skipped, since
                # per-iteration choices aren't captured by case
                # attributes anyway.
                xor_id: next(iter(counts))
                for xor_id, counts in parse.xor_branch_counts.items()
                if sum(counts.values()) == 1
            }
        chosen = per_sequence[sequence]
        if chosen is None:
            continue
        for xor_id, branch in chosen.items():
            # ``_normalise_log`` guarantees string case ids, which is
            # what ``features_df`` is indexed by.
            labels[xor_id][case_id] = branch
    return labels


def _emit_orfork_conditions_data_driven(
    ucm: UCM,
    tree,
    clustering: ClusteringResult,
    log,
    attribute_specs: Dict[str, "_dm.AttributeSpec"],
    features_df,
    feature_columns: List[str],
    loop_counters: Dict[int, UCM.Variable],
    max_loop_iterations: Optional[int],
    max_depth: int = 3,
    plan: Optional["_LoopPlan"] = None,
    parses: "Optional[_parses.ParseTable]" = None,
) -> List["_dm.OrForkMiningResult"]:
    """Replace outside-loop OR-fork conditions with decision-mined
    expressions trained on per-case attributes; keep inside-loop and
    LoopFork conditions on the existing counter machinery.

    Returns the per-OR-fork :class:`_dm.OrForkMiningResult` list so
    the report writer can describe the trained tree, accuracy, and
    feature set for each fork."""
    node_ids = _cs.assign_node_ids(tree)
    xor_seq = _collect_multi_child_xors(tree, node_ids)

    # Tree↔UCM correspondence by stable id. The previous cross-map
    # positional walk worked accidentally on simple shapes but paired
    # tree XORs with the wrong UCM OR-forks once root-map and
    # plug-in-map forks interleaved — variants without a matching
    # branch then deadlocked at runtime.
    ucm_index = _index_ucm_forks_by_tree_id(ucm, node_ids)

    # Per-XOR branch labels for every case (outside-loop only —
    # inside-loop XORs are skipped for decision mining since
    # per-iteration choices aren't captured by case attributes).
    case_to_branch_per_xor = _label_cases_by_xor_branch(
        tree, log, node_ids, [x for x, _, _ in xor_seq], parses=parses,
    )

    mining_results: List["_dm.OrForkMiningResult"] = []
    for of_idx, (tree_xor_id, n_branches, enclosing_loop_id) in enumerate(xor_seq):
        of = _ucm_node_for(ucm_index, tree_xor_id, "OrFork", "OrFork")
        if of is None:
            continue  # converter elided this XOR (e.g. single-child collapse)
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
            raw_expr = result.per_branch_expression.get(k, "false")
            # The decision-tree expressions can be highly verbose
            # because every leaf contributes its own AND-clause.
            # Run the simplifier to absorb complementary-literal
            # pairs and drop subsumed clauses. Pure tautologies on
            # the algorithmic side; the resulting expression
            # accepts exactly the same variable assignments as the
            # original.
            expr = _emin.minimize(raw_expr)
            result.per_branch_expression[k] = expr  # report sees the simplified text
            if arc.condition is None:
                arc.set_condition(UCM.Condition(
                    label=f"branch{k}", expression=expr,
                ))
            else:
                arc.condition.expression = expr
    return mining_results


def _set_inside_loop_xor_conditions_proportional(
    of: UCM.OrFork,
    tree_xor_id: int,
    n_branches: int,
    enclosing_loop_id: int,
    variants: List[Variant],
    counter_name: str,
    max_loop_iterations: Optional[int],
    variant_var_name: str,
) -> None:
    """Legacy split: treat each variant's branch counts as proportions
    of its iteration budget and hand out counter ranges from high to low.

    Superseded by the planner for the single-model path — proportional
    rounding can collapse a branch to an empty range, and it has no way
    to place a nested choice inside its parent's iterations. It remains
    the fallback for the model-family synthesizer, which drives the
    caller with re-keyed shim variants and no single tree to plan
    against, and where two-way XORs are the only case in play."""
    if n_branches > 2:
        _set_deterministic_true_false_split(of)
        return
    per_variant_clauses: List[List[str]] = [[] for _ in range(n_branches)]
    for v in variants:
        branch_counts = v.xor_branch_totals.get(tree_xor_id, {})
        total = sum(branch_counts.values())
        if total == 0:
            continue
        m_v = v.loop_iteration_max.get(enclosing_loop_id, 0)
        if max_loop_iterations is not None:
            m_v = min(m_v, max_loop_iterations)
        if m_v <= 0:
            continue
        upper = m_v
        for k in range(n_branches):
            piece = round(branch_counts.get(k, 0) / total * m_v)
            lower = 0 if k == n_branches - 1 else upper - piece
            if upper > lower:
                if upper >= m_v and lower <= 0:
                    clause = f"{variant_var_name} == {v.variant_id}"
                elif upper >= m_v:
                    clause = (f"{variant_var_name} == {v.variant_id} "
                              f"&& {counter_name} > {lower}")
                elif lower <= 0:
                    clause = (f"{variant_var_name} == {v.variant_id} "
                              f"&& {counter_name} <= {upper}")
                else:
                    clause = (f"{variant_var_name} == {v.variant_id} "
                              f"&& {counter_name} > {lower} "
                              f"&& {counter_name} <= {upper}")
                per_variant_clauses[k].append(clause)
            upper = lower

    for k, arc in enumerate(of.succ_connections):
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


def _set_inside_loop_xor_conditions(
    of: UCM.OrFork,
    tree_xor_id: int,
    n_branches: int,
    enclosing_loop_id: int,
    variants: List[Variant],
    loop_counters: Dict[int, UCM.Variable],
    max_loop_iterations: Optional[int] = None,
    variant_var_name: str = None,
    plan: Optional["_LoopPlan"] = None,
) -> None:
    """Wire an inside-loop XOR's outgoing arcs with combined
    ``variant_id`` + ``loop_counter`` conditions so each branch
    fires for a specific range of loop-counter values per variant.

    The ranges come from ``plan`` (see :func:`_plan_loop_coverage`),
    which has already decided — for every variant, and for every choice
    inside the loop including nested ones — which iterations take which
    branch. Each branch a variant takes owns a contiguous, non-empty
    slice of that variant's iterations, so the condition here is simply
    that slice expressed on the counter, guarded by ``variant_id == V``
    and joined with ``||`` across variants.

    Two properties follow, and both are the point of routing this
    through a plan rather than computing proportions locally:

    * **no branch is rounded away.** Sizing each branch from its own
      subtree's needs first, and only then sharing out the surplus by
      observed frequency, means a rarely-taken branch still gets an
      iteration — under proportional rounding it could collapse to an
      empty range and become undemonstrable;
    * **nested choices line up.** A choice inside a branch is only
      evaluated on the iterations where that branch is taken, so its
      own ranges are carved out of *those* iterations. Computing them
      against the whole run would place them where the choice is never
      reached.

    For variants that never traversed the XOR (because they didn't
    enter the loop, or the loop ran only once with the XOR in the redo
    path), no per-V clause is added — that variant contributes nothing
    to this OR-fork's expression.

    Falls back to a deterministic ``true`` / ``false`` split only if the
    enclosing loop's counter variable wasn't created (which can happen
    for tau-body loops the wiring step skipped). Multi-way XORs are
    handled like any other: the plan gives each branch its own slice.

    ``variant_var_name`` overrides the variable the guards test —
    used by the model-family synthesizer, whose variants live in the
    ``family_variant`` enumeration; the ids/keys in ``tree_xor_id``,
    ``enclosing_loop_id``, ``variants`` and ``loop_counters`` only
    need to be mutually consistent (this function just looks them
    up)."""
    if variant_var_name is None:
        variant_var_name = _VARIANT_VAR_NAME
    counter = loop_counters.get(enclosing_loop_id)
    if counter is None:
        # No counter to combine with (e.g. a tau-body loop the wiring
        # step skipped) — fall back to the deterministic split.
        _set_deterministic_true_false_split(of)
        return
    if plan is None:
        # No schedule available — the model-family synthesizer drives
        # this function with canonically re-keyed shim variants and no
        # single tree to plan against. Fall back to the proportional
        # split, which is what that path has always used.
        _set_inside_loop_xor_conditions_proportional(
            of, tree_xor_id, n_branches, enclosing_loop_id, variants,
            counter.name, max_loop_iterations, variant_var_name,
        )
        return

    counter_name = counter.name
    # Per-branch (variant_id, threshold-on-counter) clauses, read off the
    # schedule the planner computed. Every branch a variant takes has a
    # non-empty slice of that variant's iterations, so no branch can be
    # rounded out of existence, and a nested choice's slices are carved
    # from its parent branch's iterations rather than the whole run.
    per_variant_clauses: List[List[str]] = [[] for _ in range(n_branches)]
    for v in variants:
        m_v = plan.iterations.get((enclosing_loop_id, v.variant_id), 0)
        if m_v <= 0:
            continue
        for k in range(n_branches):
            window = plan.ranges.get((tree_xor_id, k, v.variant_id))
            if window is None:
                continue
            lower, upper = window
            if upper > lower:
                # Build the per-V clause for this branch.
                if upper >= m_v and lower <= 0:
                    # Trivial whole-range — this branch is the only
                    # one this variant ever takes.
                    clause = f"{variant_var_name} == {v.variant_id}"
                elif upper >= m_v:
                    clause = (
                        f"{variant_var_name} == {v.variant_id} "
                        f"&& {counter_name} > {lower}"
                    )
                elif lower <= 0:
                    clause = (
                        f"{variant_var_name} == {v.variant_id} "
                        f"&& {counter_name} <= {upper}"
                    )
                else:
                    clause = (
                        f"{variant_var_name} == {v.variant_id} "
                        f"&& {counter_name} > {lower} "
                        f"&& {counter_name} <= {upper}"
                    )
                per_variant_clauses[k].append(clause)

    if not any(per_variant_clauses):
        # No variant reaches this choice under the plan (e.g. it sits on
        # a redo path no variant loops back through). Leaving every
        # branch "false" would block the traversal outright if it ever
        # were reached, so fall back to a split that at least admits a
        # path.
        _set_deterministic_true_false_split(of)
        return

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
    ucm: UCM,
    tree,
    variants: List[Variant],
    loop_counters: Optional[Dict[int, UCM.Variable]] = None,
    max_loop_iterations: Optional[int] = None,
    plan: Optional["_LoopPlan"] = None,
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

    # Tree↔UCM correspondence by stable id (see commentary on the
    # data-driven sibling).
    ucm_index = _index_ucm_forks_by_tree_id(ucm, node_ids)

    for of_idx, (tree_xor_id, n_branches, enclosing_loop_id) in enumerate(xor_seq):
        of = _ucm_node_for(ucm_index, tree_xor_id, "OrFork", "OrFork")
        if of is None:
            continue  # converter elided this XOR (e.g. single-child collapse)
        if enclosing_loop_id is not None:
            # Inside-loop XOR. We combine variant_id with the
            # enclosing loop's counter to distribute branches across
            # iterations.
            _set_inside_loop_xor_conditions(
                of, tree_xor_id, n_branches, enclosing_loop_id,
                variants, loop_counters or {},
                max_loop_iterations=max_loop_iterations,
                plan=plan,
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
