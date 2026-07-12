"""Assemble a :class:`ModelFamily` into a single URN model.

Two assembly modes:

* :func:`assemble_combined` — every cell's model in one ``.jucm`` as
  independent root maps (plus their decomposition plug-ins). No stubs
  between cells; useful for side-by-side inspection in jUCMNav.
* :func:`assemble_umbrella` — one *overarching* model: the root map is
  the **shared skeleton** of the cell processes (computed by
  anti-unifying the per-cell process trees), with a **dynamic stub at
  every point where the cells' behaviour diverges**. Each stub's
  plug-ins are the distinct variant sub-maps, guarded by preconditions
  over the partition attributes
  (``cancer_type == Breast && age_group == _40_59``); a cell whose
  process has nothing at a variation point gets a pass-through
  ``skip`` plug-in. Behaviourally identical variants share a single
  plug-in whose precondition is the (domain-factored) OR of the member
  cells' clauses. **Resource variation counts as variation**: an
  activity performed by different actors in different cells becomes a
  variation point even under identical control flow, with each variant
  plug-in drawing the activity inside its cells' actor. One
  :class:`UCM.ScenarioDef` (strategy) per cell initialises the
  attribute variables so jUCMNav's scenario traversal selects the
  matching plug-in at every variation point. When nothing
  is shared at the root — or with ``skeleton=False`` — this
  degenerates to the plain ``start → dynamic stub → end`` umbrella
  with whole cell models as plug-ins.

Both modes convert every cell's process tree **into one shared
container** — a single ID counter and shared responsibility/component
*definitions* (``get_or_add_responsibility`` dedupes by name), so the
same activity appearing in several cells is one definition referenced
from many maps, and repeated runs produce byte-identical files.
Performers are mined once from the **whole** log and bound at the end:
definitions are shared, so the performer of a responsibility must be a
global decision, not a per-cell one.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import sys
from typing import Sequence

from ....objects.ucm.obj import UCM
from ....objects.ucm.conversion import from_process_tree as _converter
from ....objects.ucm.conversion.decomposition import (
    _SyntheticNode,
    _cut_handler_active,
    _first_label,
    _label_clean,
    _last_label,
    _propagate_components_to_stubs,
    _truncate_words,
)
from ..scenarios.expression_minimizer import minimize as _minimize
from .algorithm import resource_parameters_for, _RESOURCE_KEYS
from .family import FamilyCell, ModelFamily
from .partition import PartitionAttribute


_SEQ_OP = "->"
_LOOP_OP = "*"


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------

def _unique_map_name(container: UCM, base: str) -> str:
    """Deduplicate ``base`` against the container's existing map names
    (a cell label could collide with a derived plug-in name)."""
    existing = {m.name for m in container.maps}
    name = base
    n = 2
    while name in existing:
        name = f"{base} {n}"
        n += 1
    return name


def _bind_visual_performers(
    container: UCM,
    maps: List["UCM.UCMmap"],
    performers: Dict[str, str],
) -> None:
    """Visual-only performer binding: draw each RespRef on ``maps``
    inside its (cell- or variant-specific) performer's rectangle,
    WITHOUT touching the shared :attr:`UCM.Responsibility.performer`
    definitions — those may legitimately differ across variants, so
    the semantic link is only set where the whole family agrees (see
    :func:`_bind_family_performers`)."""
    for m in maps:
        comp_to_ref = {cr.cont_def: cr for cr in m.cont_refs}
        for n in m.nodes:
            if not isinstance(n, UCM.RespRef) or n.resp_def is None:
                continue
            perf_name = performers.get(n.resp_def.name)
            if not perf_name:
                continue
            comp = container.get_or_add_component(perf_name)
            ref = comp_to_ref.get(comp)
            if ref is None:
                ref = m.add_component_ref(comp)
                comp_to_ref[comp] = ref
            n.cont_ref = ref


def _convert_tree_into(
    container: UCM,
    family: ModelFamily,
    tree,
    map_name: str,
    visual_performers: Optional[Dict[str, str]] = None,
) -> "UCM.UCMmap":
    """Convert ``tree`` into ``container`` (with the family's converter
    parameters and per-cell decomposition setting) and return the root
    map this conversion added. ``visual_performers`` binds the given
    ``{activity: performer}`` mapping visually on every map the
    conversion created (root plus decomposition plug-ins)."""
    before = len(container.maps)
    params = {
        k: v for k, v in family.cell_parameters.items()
        if k not in _RESOURCE_KEYS
    }
    params["container"] = container
    params["map_name"] = _unique_map_name(container, map_name)
    if family.decomposition is not None:
        params["decomposition"] = family.decomposition
    _converter.apply(tree, parameters=params)
    new_maps = container.maps[before:]
    if visual_performers:
        _bind_visual_performers(container, new_maps, visual_performers)
    return new_maps[0]


def _convert_cell_into(
    container: UCM,
    family: ModelFamily,
    cell: FamilyCell,
    map_name: Optional[str] = None,
) -> "UCM.UCMmap":
    """Convert ``cell``'s process tree into ``container`` and return the
    cell's root map. The cell's own mined performers are bound visually
    on the cell's maps — resource differences between cells stay
    visible in the assembled model."""
    return _convert_tree_into(
        container, family, cell.tree, map_name or cell.label,
        visual_performers=cell.performers or None,
    )


def _unanimous_performers(family: ModelFamily) -> Dict[str, str]:
    """``{activity: performer}`` restricted to activities every cell
    (that observed the activity) agrees on. Only these are safe to set
    on the shared :class:`UCM.Responsibility` definitions."""
    agreed: Dict[str, str] = {}
    conflicted: set = set()
    for cell in family.cells:
        for act, perf in (cell.performers or {}).items():
            if not perf or act in conflicted:
                continue
            if act in agreed and agreed[act] != perf:
                del agreed[act]
                conflicted.add(act)
            elif act not in agreed:
                agreed[act] = perf
    return agreed


def _bind_family_performers(container: UCM, family: ModelFamily) -> None:
    """Semantic performer binding for the shared definitions.

    Only activities whose performer is **unanimous** across the cells
    get :attr:`UCM.Responsibility.performer` set — the definitions are
    shared, so a per-cell disagreement cannot be expressed there (it is
    expressed visually per variant map by
    :func:`_bind_visual_performers`, and — in the umbrella — as a
    variation point). The whole log's resource vocabulary is still
    surfaced as component definitions. No-op when resource mining is
    disabled."""
    performers, extras = resource_parameters_for(
        family.log_df, family.cell_parameters,
    )
    if performers is None:
        return
    agreed = _unanimous_performers(family)
    if not (agreed or extras):
        return
    container.bind_performers(
        agreed,
        kind=family.cell_parameters.get("performer_kind"),
        additional_components=extras or None,
    )
    # Decomposed cells: surface each plug-in's components on its
    # parent map, same as the single-model decomposition path.
    _propagate_components_to_stubs(container)


def _urn_name(family: ModelFamily, override: Optional[str]) -> str:
    return override or family.cell_parameters.get("urn_name", "PM4PyDiscovery")


# ---------------------------------------------------------------------------
# Combined (multi-root-map) assembly
# ---------------------------------------------------------------------------

def _cell_sub_log(family: ModelFamily, cells: List[FamilyCell]):
    """The family log restricted to the given cells' cases (``None``
    when the family's log was dropped)."""
    if family.log_df is None:
        return None
    case_ids: set = set()
    for cell in cells:
        case_ids.update(cell.case_ids)
    series = family.log_df["case:concept:name"].astype(str)
    return family.log_df[series.isin(case_ids)]


def _annotate_maps(
    container: UCM,
    family: ModelFamily,
    maps: List["UCM.UCMmap"],
    cells: List[FamilyCell],
    node_metrics,
    edge_metrics,
) -> None:
    """Performance-overlay the given maps from the given cells' sub-log."""
    from ...performance import annotate_performance
    sub_log = _cell_sub_log(family, cells)
    if sub_log is None or not len(sub_log):
        return
    annotate_performance(
        container, sub_log,
        node_metrics=node_metrics, edge_metrics=edge_metrics,
        maps=maps,
    )


def assemble_combined(
    family: ModelFamily,
    *,
    urn_name: Optional[str] = None,
    node_metrics: Sequence[str] = (),
    edge_metrics: Sequence[str] = (),
) -> UCM:
    """One URN spec containing every cell model as an independent root
    map (named after the cell's value combination), with shared
    responsibility/component definitions. Cells are added in the
    family's deterministic order. When ``node_metrics`` /
    ``edge_metrics`` are given, each cell's maps carry performance
    overlays computed from that cell's own sub-log."""
    container = UCM(name=_urn_name(family, urn_name))
    cell_maps: List[Tuple[FamilyCell, List["UCM.UCMmap"]]] = []
    for cell in family.cells:
        before = len(container.maps)
        _convert_cell_into(container, family, cell)
        cell_maps.append((cell, container.maps[before:]))
    _bind_family_performers(container, family)
    if node_metrics or edge_metrics:
        for cell, maps in cell_maps:
            _annotate_maps(container, family, maps, [cell],
                           node_metrics, edge_metrics)
    return container


# ---------------------------------------------------------------------------
# Umbrella (dynamic stub) assembly
# ---------------------------------------------------------------------------

def _tree_signature(node) -> tuple:
    """Canonical, order-insensitive-where-permitted process-tree
    signature for plug-in deduplication.

    Children of commutative operators (XOR / OR / AND / interleaving)
    are sorted so that two cells whose miners emitted the same choices
    in a different order still compare equal; SEQUENCE and LOOP keep
    child order (it is semantic there)."""
    operator = getattr(node, "operator", None)
    op = getattr(operator, "value", operator)
    if op is None:
        return ("leaf", getattr(node, "label", None))
    children = [
        _tree_signature(c) for c in (getattr(node, "children", []) or [])
    ]
    if op in ("X", "O", "+", "o"):
        children = sorted(children, key=repr)
    return (op, tuple(children))


def _camel(name: str) -> str:
    """``cancer_type`` → ``CancerType`` — the jUCMNav enumeration-type
    naming convention (cf. ``variant_id`` / ``VariantId`` in scenario
    synthesis)."""
    return "".join(p[:1].upper() + p[1:] for p in name.split("_") if p)


# ---------------------------------------------------------------------------
# Skeleton merge (anti-unification of the per-cell process trees)
# ---------------------------------------------------------------------------
#
# The umbrella's value is showing WHERE the family's processes diverge,
# not that they diverge. ``_merge_variants`` computes the shared
# skeleton of the per-cell trees: structure common to every cell stays
# a plain (sub)tree; positions where cells differ become
# ``_VariationPoint`` marker nodes. The skeleton is then materialised
# as the root map, with each variation point emitted as a *dynamic
# stub* whose plug-ins are the distinct variant subtrees, guarded by
# preconditions over the partition attributes.
#
# Merge rules (conservative — sharing must be behaviour-preserving):
#
# * identical subtrees (canonical signature) are shared verbatim;
# * SEQUENCE nodes share their longest common prefix and suffix of
#   children; equal-length remainders merge position-wise (several
#   localized variation points), unequal-length remainders become one
#   variation point (a cell's empty remainder yields a "skip" plug-in);
# * LOOP nodes with two children merge position-wise on (do, redo);
# * anything else that differs (XOR/OR/AND children, differing
#   operators, differing leaves) becomes a variation point wholesale —
#   partial alignment of commutative operators is not attempted.
#
# When nothing is shared at the root, the skeleton degenerates to a
# single whole-model variation point — exactly the plain
# stub-per-family umbrella (also available explicitly via
# ``skeleton=False``).

class _VariationPoint:
    """Marker node in the merged skeleton: the cells disagree here.

    ``groups`` holds one ``(cells, subtree)`` entry per distinct
    behaviour. Duck-types the converter's tree protocol (``operator``
    / ``label`` / ``children``) so it can sit inside a skeleton tree;
    the materialiser intercepts it via the converter's cut handler
    before those attributes are ever interpreted."""

    operator = None
    label = None
    children: tuple = ()

    def __init__(self, groups: List[Tuple[List[FamilyCell], Any]]):
        self.groups = groups


def _op_of(tree) -> Optional[str]:
    operator = getattr(tree, "operator", None)
    return getattr(operator, "value", operator)


def _children_of(tree) -> list:
    return list(getattr(tree, "children", []) or [])


def _activities_of(tree) -> Tuple[str, ...]:
    """Sorted activity labels of the subtree's leaves."""
    out: set = set()
    stack = [tree]
    while stack:
        node = stack.pop()
        children = getattr(node, "children", []) or []
        if children:
            stack.extend(children)
        else:
            label = getattr(node, "label", None)
            if label is not None:
                out.add(str(label))
    return tuple(sorted(out))


def _variant_key(
    cells: List[FamilyCell], tree, resource_variation: bool,
) -> tuple:
    """Equivalence key for merging: canonical control-flow signature,
    plus — when ``resource_variation`` — the performer of every
    activity in the subtree. Two cells whose processes have identical
    control flow but different actors executing an activity are then
    *different* variants: resource variation is variation. All cells
    in a group share the profile by construction, so the first cell
    speaks for the group."""
    sig = _tree_signature(tree)
    if not resource_variation:
        return sig
    perf = cells[0].performers if cells else {}
    profile = tuple(
        (act, (perf or {}).get(act)) for act in _activities_of(tree)
    )
    return (sig, profile)


def _group_by_signature(
    variants: List[Tuple[List[FamilyCell], Any]],
    resource_variation: bool = False,
) -> List[Tuple[List[FamilyCell], Any]]:
    """Group ``(cells, subtree)`` pairs by :func:`_variant_key`,
    merging cell lists; group order = first occurrence (deterministic
    because cells arrive in family order)."""
    order: List[tuple] = []
    by_sig: Dict[tuple, Tuple[List[FamilyCell], Any]] = {}
    for cells, tree in variants:
        key = _variant_key(cells, tree, resource_variation)
        if key not in by_sig:
            by_sig[key] = ([], tree)
            order.append(key)
        by_sig[key][0].extend(cells)
    return [by_sig[k] for k in order]


def _as_sequence_subtree(children: list):
    """A sequence remainder as a convertible subtree. An empty
    remainder becomes a tau leaf — its plug-in map is a direct
    start→end pass-through (that cell *skips* this part)."""
    if not children:
        return _SyntheticNode(None, [])
    if len(children) == 1:
        return children[0]
    return _SyntheticNode(_SEQ_OP, list(children))


def _make_vp(
    variants: List[Tuple[List[FamilyCell], Any]],
    dedup: bool,
    resource_variation: bool,
) -> _VariationPoint:
    if dedup:
        return _VariationPoint(
            _group_by_signature(variants, resource_variation)
        )
    expanded: List[Tuple[List[FamilyCell], Any]] = []
    for cells, tree in variants:
        for cell in cells:
            expanded.append(([cell], tree))
    return _VariationPoint(expanded)


def _merge_variants(
    variants: List[Tuple[List[FamilyCell], Any]],
    dedup: bool,
    resource_variation: bool,
):
    """Anti-unify the variant subtrees into a skeleton node."""
    groups = _group_by_signature(variants, resource_variation)
    if len(groups) == 1:
        return groups[0][1]  # identical everywhere — shared verbatim
    reps = [t for _, t in groups]
    ops = {_op_of(t) for t in reps}
    if ops == {_SEQ_OP}:
        return _merge_sequences(groups, dedup, resource_variation)
    if ops == {_LOOP_OP} and all(len(_children_of(t)) == 2 for t in reps):
        do = _merge_variants(
            [(c, _children_of(t)[0]) for c, t in groups],
            dedup, resource_variation)
        redo = _merge_variants(
            [(c, _children_of(t)[1]) for c, t in groups],
            dedup, resource_variation)
        return _SyntheticNode(_LOOP_OP, [do, redo])
    return _make_vp(groups, dedup, resource_variation)


def _merge_sequences(
    groups: List[Tuple[List[FamilyCell], Any]],
    dedup: bool,
    resource_variation: bool,
):
    """Sequence-specific merge: share the longest common prefix and
    suffix of children; merge equal-length remainders position-wise,
    collapse unequal-length remainders into one variation point.

    Prefix/suffix equality uses :func:`_variant_key` per child — with
    resource variation on, a child performed by different actors in
    different groups is NOT shared."""
    child_lists = [_children_of(t) for _, t in groups]
    sig_lists = [
        [_variant_key(cells, c, resource_variation) for c in cl]
        for (cells, _), cl in zip(groups, child_lists)
    ]
    min_len = min(len(cl) for cl in child_lists)

    prefix_len = 0
    while prefix_len < min_len and all(
            sl[prefix_len] == sig_lists[0][prefix_len]
            for sl in sig_lists):
        prefix_len += 1
    suffix_len = 0
    while suffix_len < min_len - prefix_len and all(
            sl[len(sl) - 1 - suffix_len]
            == sig_lists[0][len(sig_lists[0]) - 1 - suffix_len]
            for sl in sig_lists):
        suffix_len += 1

    middles = [
        (cells, cl[prefix_len:len(cl) - suffix_len])
        for (cells, _), cl in zip(groups, child_lists)
    ]

    out: list = list(child_lists[0][:prefix_len])
    lengths = {len(m) for _, m in middles}
    if lengths == {0}:
        pass  # fully shared — cannot happen with >1 group, but harmless
    elif len(lengths) == 1:
        length = next(iter(lengths))
        for i in range(length):
            out.append(_merge_variants(
                [(cells, m[i]) for cells, m in middles],
                dedup, resource_variation))
    else:
        out.append(_make_vp(
            [(cells, _as_sequence_subtree(m)) for cells, m in middles],
            dedup, resource_variation,
        ))
    if suffix_len:
        out.extend(child_lists[0][len(child_lists[0]) - suffix_len:])
    return _SyntheticNode(_SEQ_OP, out)


def _vp_stub_name(vp: _VariationPoint) -> str:
    """Content-derived dynamic-stub caption: the first activity of
    each variant, deduplicated (``"Triage | Scan"``)."""
    labels: List[str] = []
    for _, subtree in vp.groups:
        lab = _truncate_words(_label_clean(_first_label(subtree))) or "skip"
        if lab not in labels:
            labels.append(lab)
    shown = " | ".join(labels[:3])
    if len(labels) > 3:
        shown += " | ..."
    return shown or "Variants"


def _subtree_map_name(subtree) -> str:
    """Content-derived plug-in map name (``"Biopsy to Surgery"``)."""
    first = _label_clean(_first_label(subtree))
    last = _label_clean(_last_label(subtree))
    if not first:
        return "skip"
    if first == last:
        return _truncate_words(first)
    return _truncate_words(f"{first} to {last}")


def _attr_is_boolean(attr: PartitionAttribute) -> bool:
    """A boolean *variable* is only possible when every axis value is a
    boolean literal — an Unknown/Other bucket forces an enumeration."""
    return (
        attr.spec.type == "boolean"
        and not attr.binned
        and all(v.token in ("true", "false") for v in attr.values)
    )


def _make_variables(
    container: UCM, family: ModelFamily,
) -> List["UCM.Variable"]:
    """One scenario variable per partition attribute — boolean when the
    axis is purely true/false, otherwise an enumeration over the axis
    tokens."""
    out: List[UCM.Variable] = []
    for attr in family.attributes:
        if _attr_is_boolean(attr):
            var = container.get_or_add_variable(
                attr.variable_name, type="boolean",
            )
        else:
            et = container.get_or_add_enumeration_type(
                _camel(attr.variable_name),
                values=[v.token for v in attr.values],
            )
            var = container.get_or_add_variable(
                attr.variable_name, type="enumeration", enumeration_type=et,
            )
        out.append(var)
    return out


def _cell_clause(family: ModelFamily, cell: FamilyCell) -> str:
    """``a == X`` or ``a == X && b == Y`` for one cell."""
    literals = [
        f"{attr.variable_name} == {value.token}"
        for attr, value in zip(family.attributes, cell.values)
    ]
    return " && ".join(literals)


def _disjunction(var: str, tokens: List[str]) -> str:
    return " || ".join(f"{var} == {t}" for t in tokens)


def _group_expression(family: ModelFamily, cells: List[FamilyCell]) -> str:
    """Selection expression for a (possibly merged) plug-in group.

    Unlike the generic expression minimizer, this knows the attribute
    *domains* (the partition axes), so it factors aggressively. For a
    two-attribute family the member combinations are grouped by which
    second-attribute values each first-attribute value covers:

    * full cover drops the second attribute entirely
      (``broker == AIIS`` instead of three OR'd conjunctions);
    * partial cover factors into
      ``(a == X || a == Y) && (b == U || b == V)``.

    Single-attribute groups are a plain disjunction run through the
    expression minimizer."""
    if len(family.attributes) == 1:
        var = family.attributes[0].variable_name
        return _minimize(
            _disjunction(var, [c.values[0].token for c in cells])
        )

    var1 = family.attributes[0].variable_name
    var2 = family.attributes[1].variable_name
    axis2 = [v.token for v in family.attributes[1].values]
    axis2_pos = {t: i for i, t in enumerate(axis2)}

    # cover: first-attribute token → covered second-attribute tokens.
    cover: Dict[str, List[str]] = {}
    order1: List[str] = []
    for c in cells:
        t1, t2 = c.values[0].token, c.values[1].token
        if t1 not in cover:
            cover[t1] = []
            order1.append(t1)
        cover[t1].append(t2)

    # Group first-attribute tokens sharing the same cover set.
    grouped: Dict[Tuple[str, ...], List[str]] = {}
    group_order: List[Tuple[str, ...]] = []
    for t1 in order1:
        key = tuple(sorted(cover[t1], key=axis2_pos.get))
        if key not in grouped:
            grouped[key] = []
            group_order.append(key)
        grouped[key].append(t1)

    full = tuple(axis2)
    clauses: List[str] = []
    for key in group_order:
        t1s = grouped[key]
        left = _disjunction(var1, t1s)
        if key == full:
            # Covers every value of the second attribute → drop it.
            clauses.append(left if len(t1s) == 1 else f"({left})")
            continue
        right = _disjunction(var2, list(key))
        left_p = left if len(t1s) == 1 else f"({left})"
        right_p = right if len(key) == 1 else f"({right})"
        clauses.append(f"{left_p} && {right_p}")
    if len(clauses) > 1:
        # Parenthesise conjunctive clauses so the disjunction never
        # relies on &&-over-|| precedence.
        clauses = [f"({c})" if " && " in c else c for c in clauses]
    return " || ".join(clauses)


def _group_map_name(cells: List[FamilyCell]) -> str:
    """Deterministic plug-in name for a (possibly merged) cell group."""
    if len(cells) == 1:
        return cells[0].label
    if len(cells) <= 3:
        return " | ".join(c.label for c in cells)
    return f"{cells[0].label} (+{len(cells) - 1} more)"


def assemble_umbrella(
    family: ModelFamily,
    *,
    urn_name: Optional[str] = None,
    root_map_name: str = "Overview",
    stub_name: Optional[str] = None,
    dedup: bool = True,
    strategies: bool = True,
    group_name: str = "FamilyStrategies",
    skeleton: bool = True,
    resource_variation: bool = True,
    path_scenarios: bool = True,
    max_variants_per_cell: int = 5,
    max_loop_iterations: int = 2,
    node_metrics: Sequence[str] = (),
    edge_metrics: Sequence[str] = (),
) -> UCM:
    """Assemble the family into one overarching model whose root map is
    the **shared skeleton** of the cell processes, with a dynamic stub
    at every point where the cells' behaviour diverges. Each stub's
    plug-ins are the distinct variant sub-maps, selectable through
    preconditions over the partition attributes; a cell whose process
    skips a variation point gets a pass-through ``skip`` plug-in.

    Parameters
    ----------
    family
        The mined :class:`ModelFamily`.
    root_map_name
        Name of the top map holding the shared skeleton.
    stub_name
        Caption used when the merge degenerates to a single
        whole-model stub (nothing shared at the root); defaults to
        ``"by <attr1>[ / <attr2>]"``. Localized variation points get
        content-derived names (``"Triage | Scan"``).
    dedup
        Merge behaviourally identical variants at each variation point
        into a single plug-in whose precondition ORs the member cells'
        clauses (factored over the attribute domains). The shared
        plug-ins *show* which sub-populations follow the same process.
    strategies
        Emit one :class:`UCM.ScenarioDef` per cell, initialising the
        attribute variables, so jUCMNav's scenario traversal picks the
        matching plug-in at every variation point.
    skeleton
        ``True`` (default): merge the cell trees into the shared
        skeleton described above. ``False``: the plain whole-model
        umbrella — a single ``start → dynamic stub → end`` root map
        with one complete cell model per plug-in.
    resource_variation
        ``True`` (default): *resource variation is variation* — an
        activity performed by different actors in different cells
        becomes a variation point even when control flow is identical,
        and each variant plug-in binds the activity to its cells'
        actor. ``False``: merge on control flow only.
    path_scenarios
        ``True`` (default): replay each cell's sub-log to emit one
        executable scenario per (cell × behavioural variant) — with
        ``family_variant`` branch conditions on outside-loop OR-forks
        and loop-counter scaffolding — instead of one plain
        configuration strategy per cell. Requires ``family.log_df``
        (falls back to plain strategies when it was dropped).
    max_variants_per_cell
        Cap on scenarios per cell (most frequent behavioural variants
        first); omitted coverage is noted on the scenario group.
    max_loop_iterations
        Per-scenario cap on loop-counter initialisations (see
        :func:`pm4py_ucm.discover_scenarios`).
    node_metrics / edge_metrics
        When given, performance overlays are attached: the shared
        skeleton (root map) from the **whole** family log, each
        variant plug-in from its **covering cells'** sub-log. See
        :func:`pm4py_ucm.annotate_performance` for the metric names.
    """
    import warnings

    container = UCM(name=_urn_name(family, urn_name))

    root = container.add_map(name=root_map_name)
    start = UCM.StartPoint(name="start")
    end = UCM.EndPoint(name="end")
    root.add_node(start)
    root.add_node(end)

    variables = _make_variables(container, family)

    variants: List[Tuple[List[FamilyCell], Any]] = [
        ([cell], cell.tree) for cell in family.cells
    ]
    if skeleton:
        merged = _merge_variants(variants, dedup, resource_variation)
    else:
        merged = _make_vp(variants, dedup, resource_variation)

    if stub_name is None:
        stub_name = "by " + " / ".join(
            a.display_name for a in family.attributes
        )
    whole_model_vp = merged if isinstance(merged, _VariationPoint) else None

    # Materialise the skeleton as the root map. Variation points are
    # intercepted by the converter's cut handler and emitted as
    # dynamic stubs; everything else converts as usual.
    stubs_to_bind: List[Tuple["UCM.Stub", _VariationPoint]] = []

    def cut_handler(t, entry, exit, m):
        if not isinstance(t, _VariationPoint):
            return False
        name = stub_name if t is whole_model_vp else _vp_stub_name(t)
        vp_stub = UCM.Stub(name=name, dynamic=True)
        m.add_node(vp_stub)
        m.add_connection(entry, vp_stub)
        m.add_connection(vp_stub, exit)
        stubs_to_bind.append((vp_stub, t))
        return True

    saved_limit = sys.getrecursionlimit()
    required = max(saved_limit, _converter._tree_depth(merged) + 200)
    if required > saved_limit:
        sys.setrecursionlimit(required)
    try:
        with _cut_handler_active(cut_handler):
            _converter._attach(merged, start, end, root)
    finally:
        sys.setrecursionlimit(saved_limit)
    _converter._simplify_map(container, root)
    _converter._insert_routing_for_map(container, root)

    if skeleton and not stubs_to_bind:
        warnings.warn(
            "Family umbrella: every cell is behaviourally identical "
            "(control flow and performers) with respect to the chosen "
            "attributes — the umbrella has no variation points and is "
            "a single shared model. Consider partitioning on different "
            "attributes."
        )

    # Plug-ins are wired AFTER simplification so the recorded in/out
    # arcs are the surviving ones (same rule as decomposition). Each
    # variant's plug-in binds ITS cells' performers visually — this is
    # where resource variation stays visible.
    # Plug-in map ranges per variant group, for the performance
    # overlay pass at the end.
    group_maps: List[Tuple[List[FamilyCell], List["UCM.UCMmap"]]] = []

    for vp_stub, vp in stubs_to_bind:
        in_arcs = vp_stub.pred_connections
        out_arcs = vp_stub.succ_connections
        for cells, subtree in vp.groups:
            # Plug-in names carry the attribute values they cover
            # ("Register Claim [AUS | NZL]") — a bare content name
            # with a " 2" suffix means nothing in jUCMNav's tree.
            map_name = (
                _group_map_name(cells) if vp is whole_model_vp
                else (f"{_subtree_map_name(subtree)} "
                      f"[{_group_map_name(cells)}]")
            )
            variant_acts = set(_activities_of(subtree))
            variant_performers = {
                act: perf
                for act, perf in (cells[0].performers or {}).items()
                if act in variant_acts and perf
            }
            maps_before = len(container.maps)
            plugin_root = _convert_tree_into(
                container, family, subtree, map_name,
                visual_performers=variant_performers or None,
            )
            group_maps.append((cells, container.maps[maps_before:]))
            starts = plugin_root.start_points
            ends = plugin_root.end_points
            binding = UCM.PluginBinding(stub=vp_stub, plugin=plugin_root)
            if in_arcs and out_arcs and starts and ends:
                binding.add_in(
                    parent_connection=in_arcs[0], plugin_start=starts[0],
                )
                binding.add_out(
                    plugin_end=ends[0], parent_connection=out_arcs[0],
                )
            binding.precondition = UCM.Condition(
                label=_group_map_name(cells),
                expression=_group_expression(family, cells),
            )
            vp_stub.bindings.append(binding)

    # Strategies. With ``path_scenarios`` (default), replay each cell's
    # sub-log on its configured tree and emit one executable scenario
    # per (cell × behavioural variant), with family_variant branch
    # conditions on the outside-loop OR-forks and loop-counter
    # scaffolding — so different strategies actually traverse the
    # different paths of each combination. Otherwise: one plain
    # configuration strategy per cell (plug-in selection only).
    if strategies and path_scenarios and family.log_df is not None:
        from .scenarios import synthesize_family_scenarios
        synthesize_family_scenarios(
            container, family, merged,
            [vp for _, vp in stubs_to_bind],
            variables, start, end,
            group_name=group_name,
            max_loop_iterations=max_loop_iterations,
            max_variants_per_cell=max_variants_per_cell,
        )
    elif strategies:
        sg = container.add_scenario_group(
            name=group_name,
            description=(
                "One strategy per attribute combination; initialising "
                "the partition variables selects the matching plug-in "
                "of the dynamic stub."
            ),
        )
        for cell in family.cells:
            assignments = "; ".join(
                f"{a.display_name} = {v.label}"
                for a, v in zip(family.attributes, cell.values)
            )
            sd = UCM.ScenarioDef(
                name=cell.label,
                description=f"{assignments}. Cases: {cell.caption}.",
            )
            sg.add_scenario(sd)
            for var, value in zip(variables, cell.values):
                sd.add_initialization(var, value.token)
            sd.add_start_point(start)
            sd.add_end_point(end, mandatory=True)

    _bind_family_performers(container, family)

    if node_metrics or edge_metrics:
        # Shared skeleton (root map): statistics from the WHOLE family
        # log; each variant plug-in: from its covering cells' sub-log.
        _annotate_maps(container, family, [root], family.cells,
                       node_metrics, edge_metrics)
        for cells, maps in group_maps:
            _annotate_maps(container, family, maps, cells,
                           node_metrics, edge_metrics)
    return container
