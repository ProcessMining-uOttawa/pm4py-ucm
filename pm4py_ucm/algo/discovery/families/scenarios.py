"""Per-cell path scenarios for the family umbrella.

The attribute strategies of :func:`~.assembly.assemble_umbrella` select
*plug-ins* (which variant sub-map fills each dynamic stub), but they
cannot select *branches*: within one attribute combination, the choice
taken at an OR-fork varies from case to case. This module closes that
gap by reusing the concurrency-aware variant machinery:

1. For every cell, replay the cell's sub-log on the cell's **configured
   tree** — the merged skeleton with each variation point substituted
   by the cell's variant subtree. Configured trees are assembled from
   the *same node objects* the umbrella maps were converted from, which
   is what lets replay results be correlated back to the UCM's
   OR-forks (the converter stashes ``id(tree_node)`` on each fork).
2. Emit one executable scenario per (cell × behavioural variant): it
   initialises the partition-attribute variables (selecting the right
   plug-in at every dynamic stub), a ``family_variant`` enumeration
   value (selecting the right branch at every outside-loop OR-fork),
   and the per-loop iteration counters.
3. Set each outside-loop OR-fork branch condition to the disjunction of
   the ``family_variant`` values that take it; give each *inside-loop*
   two-way XOR combined ``family_variant`` + loop-counter range
   conditions (branches distributed across iterations by the observed
   per-variant branch proportions — the same mechanism as the
   single-model synthesizer); and wire loop-counter scaffolding (entry
   guards, decrements, exit conditions) — once per conversion unit,
   because the wiring pass is not idempotent.

Conditions always land on the arc *directly leaving* the fork
(``_pull_condition_onto_direct_arc``) — the only place jUCMNav's
traversal engine evaluates them. Remaining limitation: inside-loop
XORs with more than two branches fall back to a deterministic
``true``/``false`` split (multi-way counter ranges are a follow-up in
the single-model synthesizer too).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from ....objects.ucm.obj import UCM
from ..scenarios.synthesis import (
    _collect_multi_child_xors,
    _pull_condition_onto_direct_arc,
    _set_inside_loop_xor_conditions,
    _wire_loop_counters,
)
from ..variants import choice_signature as _cs
from ..variants import clustering as _clustering
from .family import FamilyCell, ModelFamily


def _configured_tree(node, chooser, alias: Dict[int, Any]):
    """The merged skeleton with every variation point replaced by
    ``chooser(vp)`` — reusing the original node objects wherever
    possible so replay ids correlate with the converted maps. Rebuilt
    wrapper nodes (ancestors of a substitution) are recorded in
    ``alias`` as ``{id(new_node): original_node}``."""
    from .assembly import _SyntheticNode, _VariationPoint, _children_of, _op_of

    if isinstance(node, _VariationPoint):
        return chooser(node)
    children = _children_of(node)
    if not children:
        return node
    new_children = [
        _configured_tree(c, chooser, alias) for c in children
    ]
    if all(nc is c for nc, c in zip(new_children, children)):
        return node
    rebuilt = _SyntheticNode(_op_of(node), new_children)
    alias[id(rebuilt)] = node
    return rebuilt


def _cell_token(cell: FamilyCell) -> str:
    """jUCMNav-safe identifier for the cell (``AUS``, ``Breast__40_59``)."""
    return "_".join(v.token.strip("_") or "V" for v in cell.values)


def _iter_units(merged, vps) -> List[Any]:
    """Conversion units: the merged skeleton plus every variation
    point's variant subtrees — each unit's node objects were converted
    into the umbrella exactly once, so per-unit passes (loop wiring)
    touch every loop exactly once."""
    units = [merged]
    for vp in vps:
        units.extend(subtree for _, subtree in vp.groups)
    return units


def synthesize_family_scenarios(
    container: UCM,
    family: ModelFamily,
    merged,
    vps: List[Any],
    attribute_variables: List["UCM.Variable"],
    start: "UCM.StartPoint",
    end: "UCM.EndPoint",
    *,
    group_name: str = "FamilyStrategies",
    max_loop_iterations: int = 2,
    max_variants_per_cell: int = 5,
    case_id_col: str = "case:concept:name",
) -> "UCM.ScenarioGroup":
    """Emit per-(cell × variant) executable scenarios on the umbrella.

    Returns the created :class:`UCM.ScenarioGroup`."""
    # ------------------------------------------------------------------
    # 1. Loop scaffolding — once per conversion unit (not idempotent).
    #    Re-key the returned counters from unit-tree positional ids to
    #    node objects (python ids), the coordinate system shared with
    #    the converted maps.
    # ------------------------------------------------------------------
    loop_var_by_pyid: Dict[int, UCM.Variable] = {}
    for unit in _iter_units(merged, vps):
        counters = _wire_loop_counters(container, unit)
        if not counters:
            continue
        inverse = {v: k for k, v in _cs.assign_node_ids(unit).items()}
        for tree_id, var in counters.items():
            loop_var_by_pyid[inverse[tree_id]] = var

    # OR-forks of the umbrella, by the tree-node python id the
    # converter stashed. LoopForks are correlated to LOOP nodes and are
    # governed by the counters above, so exclude them here.
    # ``fork_order`` gives a deterministic (map, node) position for
    # emission ordering — python ids vary run to run and must never
    # drive anything observable.
    fork_by_pyid: Dict[int, UCM.PathNode] = {}
    fork_order: Dict[int, Tuple[int, int]] = {}
    loop_pyids = set(loop_var_by_pyid)
    for m_idx, m in enumerate(container.maps):
        for n_idx, n in enumerate(m.nodes):
            py_id = getattr(n, "_tree_python_id", None)
            if (py_id is not None and isinstance(n, UCM.OrFork)
                    and py_id not in loop_pyids):
                fork_by_pyid[py_id] = n
                fork_order[py_id] = (m_idx, n_idx)

    # ------------------------------------------------------------------
    # 2. Per-cell replay on the configured tree.
    # ------------------------------------------------------------------
    variant_enum = container.get_or_add_enumeration_type("FamilyVariant")
    variant_var = container.get_or_add_variable(
        "family_variant", type="enumeration",
        enumeration_type=variant_enum,
    )

    group = container.add_scenario_group(
        name=group_name,
        description=(
            "One executable scenario per attribute combination and "
            "behavioural variant. The attribute variables select the "
            "plug-in at every dynamic stub; family_variant selects "
            "the branch at every outside-loop OR-fork; loop counters "
            "bound the iterations."
        ),
    )

    # branch selections: (fork python id, branch index) -> [enum values]
    taken: Dict[Tuple[int, int], List[str]] = {}
    reached_forks: set = set()
    # inside-loop XORs discovered on the configured trees:
    # canonical xor python id -> (n_branches, canonical loop python id)
    inside_loop_xors: Dict[int, Tuple[int, int]] = {}
    # Namespaced variant shims for the inside-loop condition emitter:
    # same fields it reads from a real Variant, but with variant_id
    # already namespaced and the id-keyed dicts re-keyed to canonical
    # node python-ids.
    shim_variants: List[SimpleNamespace] = []

    case_series = family.log_df[case_id_col].astype(str)
    from .assembly import _VariationPoint  # local: avoid import cycle

    for cell, variables_values in (
            (c, [v.token for v in c.values]) for c in family.cells):
        def chooser(vp: "_VariationPoint", _cell=cell):
            for cells, subtree in vp.groups:
                if any(c is _cell for c in cells):
                    return subtree
            return vp.groups[0][1]  # unreachable if groups cover cells

        alias: Dict[int, Any] = {}
        ctree = _configured_tree(merged, chooser, alias)
        ctree_ids = _cs.assign_node_ids(ctree)
        inverse_ids = {v: k for k, v in ctree_ids.items()}

        def canonical(tree_id: int) -> Optional[int]:
            """Positional id in the configured tree → python id of
            the CONVERTED node (resolving rebuilt wrappers)."""
            py_id = inverse_ids.get(tree_id)
            if py_id is None:
                return None
            original = alias.get(py_id)
            return id(original) if original is not None else py_id

        # Inside-loop XOR discovery happens on the CONFIGURED tree —
        # a loop in the shared skeleton and an XOR in a variant
        # subtree only meet here (the per-unit view would see each
        # without the other and miss the enclosure).
        for txid, n_branches, encl in _collect_multi_child_xors(
                ctree, ctree_ids):
            if encl is None:
                continue
            xor_key = canonical(txid)
            loop_key = canonical(encl)
            if xor_key is not None and loop_key is not None:
                inside_loop_xors.setdefault(
                    xor_key, (n_branches, loop_key))

        cell_df = family.log_df[case_series.isin(set(cell.case_ids))]
        clustering = _clustering.cluster(cell_df, ctree)

        token = _cell_token(cell)
        kept = clustering.variants[:max_variants_per_cell]
        covered = sum(v.frequency for v in kept)
        for variant in kept:
            value = f"{token}_{variant.variant_id}"
            if value not in variant_enum.values:
                variant_enum.values.append(value)

            scenario = UCM.ScenarioDef(
                name=f"{cell.label} {variant.variant_id}",
                description=(
                    f"{cell.label}: variant {variant.variant_id}, "
                    f"{variant.frequency} of {cell.n_cases} cases."
                ),
            )
            group.add_scenario(scenario)
            for var, val in zip(attribute_variables, variables_values):
                scenario.add_initialization(var, val)
            scenario.add_initialization(variant_var, value)

            # Loop counters — re-key positional ids to node objects,
            # resolving rebuilt wrappers back to the converted nodes.
            loops_pyid: Dict[int, int] = {}
            for tree_id, iterations in (
                    variant.loop_iteration_max or {}).items():
                key = canonical(tree_id)
                if key is None:
                    continue
                loops_pyid[key] = int(iterations)
                var = loop_var_by_pyid.get(key)
                if var is not None:
                    capped = min(int(iterations), int(max_loop_iterations))
                    scenario.add_initialization(var, str(capped))

            scenario.add_start_point(start)
            scenario.add_end_point(end, mandatory=True)

            # Outside-loop XOR choices → branch selections.
            for tree_id, branch in _cs.collect_xor_choices(
                    variant.signature).items():
                key = canonical(tree_id)
                if key is not None and key in fork_by_pyid:
                    reached_forks.add(key)
                    taken.setdefault((key, branch), []).append(value)

            # Inside-loop branch totals, re-keyed for the combined
            # counter-range condition emitter below.
            totals_pyid: Dict[int, Dict[int, int]] = {}
            for tree_id, counts in (
                    variant.xor_branch_totals or {}).items():
                key = canonical(tree_id)
                if key is not None:
                    totals_pyid[key] = dict(counts)
            shim_variants.append(SimpleNamespace(
                variant_id=value,
                xor_branch_totals=totals_pyid,
                loop_iteration_max=loops_pyid,
            ))

        if covered < cell.n_cases or clustering.noise_case_ids:
            skipped_variants = len(clustering.variants) - len(kept)
            scenario_note = (
                f"{cell.label}: {covered}/{cell.n_cases} cases covered "
                f"by {len(kept)} scenario(s)"
            )
            if skipped_variants:
                scenario_note += (
                    f"; {skipped_variants} low-frequency variant(s) "
                    f"omitted (max_variants_per_cell="
                    f"{max_variants_per_cell})"
                )
            group.set_description(
                group.description + " " + scenario_note + "."
            )

    # ------------------------------------------------------------------
    # 3. Branch conditions. Outside-loop OR-forks get family_variant
    #    disjunctions; inside-loop two-way XORs get combined
    #    family_variant + counter-range conditions (the single-model
    #    synthesizer's mechanism, fed with canonically re-keyed data).
    #    Conditions are pulled onto the arc DIRECTLY leaving the fork
    #    — the only arc jUCMNav's traversal evaluates.
    # ------------------------------------------------------------------
    for key in sorted(reached_forks, key=lambda k: fork_order[k]):
        fork = fork_by_pyid[key]
        for index in range(len(fork.succ_connections)):
            arc = _pull_condition_onto_direct_arc(
                fork.succ_connections[index],
            )
            values = taken.get((key, index), [])
            expression = (
                " || ".join(f"family_variant == {v}" for v in values)
                if values else "false"
            )
            if arc.condition is None:
                arc.set_condition(UCM.Condition(
                    label=f"branch{index}", expression=expression,
                ))
            else:
                arc.condition.expression = expression

    for xor_py in sorted(
            (k for k in inside_loop_xors if k in fork_by_pyid),
            key=lambda k: fork_order[k]):
        n_branches, loop_py = inside_loop_xors[xor_py]
        _set_inside_loop_xor_conditions(
            fork_by_pyid[xor_py], xor_py, n_branches, loop_py,
            shim_variants, loop_var_by_pyid,
            max_loop_iterations=max_loop_iterations,
            variant_var_name="family_variant",
        )
    return group
