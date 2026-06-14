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


def _collect_xors_outside_loops(
    tree, node_ids: Dict[int, int],
) -> List[Tuple[int, int]]:
    """Pre-order list of ``(signature_int_id, n_branches)`` for every
    multi-child XOR/OR tree node whose ancestry contains no LOOP node.

    The converter creates :class:`UCM.OrFork` nodes for these XORs in
    the same pre-order, so list indices line up with the map's
    ``OrFork`` instances after we filter out LoopForks."""
    out: List[Tuple[int, int]] = []

    def _walk(node, in_loop: bool) -> None:
        op = _op_value(node)
        children = list(getattr(node, "children", None) or [])
        if op in (_XOR, _OR) and len(children) >= 2 and not in_loop:
            out.append((node_ids[id(node)], len(children)))
        next_in_loop = in_loop or (op == _LOOP)
        for c in children:
            _walk(c, next_in_loop)

    _walk(tree, False)
    return out


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
    xor_seq = _collect_xors_outside_loops(tree, node_ids)

    or_forks: List[UCM.OrFork] = [
        n for n in target_map.nodes
        if isinstance(n, UCM.OrFork) and n.name != "LoopFork"
    ]
    if len(or_forks) != len(xor_seq):
        return

    for of_idx, (tree_xor_id, n_branches) in enumerate(xor_seq):
        of = or_forks[of_idx]
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
            vids = branch_to_variants.get(k, [])
            if not vids:
                continue
            # jUCMNav's expression syntax treats enum values as bare
            # identifiers, not string literals. Wrapping them in quotes
            # ("variant_id == \"v1\"") makes the parser treat the
            # right-hand side as a string and the comparison fails the
            # type check against the enum variable.
            expr = " || ".join(
                f"{_VARIANT_VAR_NAME} == {vid}" for vid in vids
            )
            if arc.condition is None:
                arc.set_condition(UCM.Condition(
                    label=f"branch{k}", expression=expr,
                ))
            else:
                arc.condition.expression = expr
