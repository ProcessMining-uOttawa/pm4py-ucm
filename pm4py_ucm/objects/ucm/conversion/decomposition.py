"""
Hierarchical decomposition of process trees into multi-map UCMs.

The default :func:`from_process_tree.apply` converter produces a single
flat UCM map covering the entire tree. For complex trees this is
visually overwhelming. This module produces a *root* map plus one or
more *plug-in* (sub-)maps connected by UCM :class:`UCM.Stub` nodes via
:class:`UCM.PluginBinding`. The session-4.6 binding machinery is used
verbatim — no parallel implementation.

Three semantic boundary rules drive the decomposition:

* ``on_root_sequence`` — each child of a top-level ``->`` (sequence)
  operator becomes its own plug-in map. The root map reads as a
  left-to-right chain of stubs representing process "phases".
* ``on_parallel`` — each branch of a ``+`` (parallel) operator
  becomes its own plug-in map. Parallel branches are usually
  semantically independent units of work, and the AND-fork/join
  vertical-expansion cost is replaced by a single stub.
* ``on_loop`` — each ``*`` (loop) operator's entire expansion
  becomes its own plug-in map. The parent map then reads as a
  forward flow with one stub representing the iteration.

Layered on top:

* ``max_leaves_per_map`` — hard cap on the activity-leaf count of any
  single map. If a map exceeds it, the largest operator-subtree
  inside the map is force-cut, recursively, until the cap is met.
  Acts even when no boundary rule independently fires.
* ``min_leaves_to_decompose`` — floor: subtrees smaller than this
  are never cut, regardless of boundary rules. Prevents a
  parallel-of-2 from spawning a 3-node plug-in map that's worse
  than inlining.
* ``balance_ratio`` — siblings under a ``+`` or top-level ``->`` are
  only extracted when their share of the parent's total leaves is at
  least this fraction (default 0.2). Prevents a tiny child of an
  otherwise-dominant sibling from being pulled out into its own
  near-empty plug-in.

See :func:`parse_decomposition` for the parameter form and :func:`apply`
for the entry point.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Set, Tuple

from ..obj import UCM
from . import from_process_tree as _ft


# ---------------------------------------------------------------------------
# Parameter parsing
# ---------------------------------------------------------------------------

VALID_KEYS = {
    "on_root_sequence",
    "on_parallel",
    "on_loop",
    "max_leaves_per_map",
    "min_leaves_to_decompose",
    "balance_ratio",
}

#: "auto" preset — recommended default when the user just wants
#: decomposition without thinking about parameters.
AUTO_DEFAULTS: Dict[str, Any] = {
    "on_root_sequence": True,
    "on_parallel": True,
    "on_loop": True,
    "max_leaves_per_map": 20,
    "min_leaves_to_decompose": 4,
    "balance_ratio": 0.2,
}

#: "aggressive" preset — same boundary rules, tighter cap.
AGGRESSIVE_DEFAULTS: Dict[str, Any] = dict(AUTO_DEFAULTS, max_leaves_per_map=10)


def parse_decomposition(spec) -> Optional[Dict[str, Any]]:
    """Coerce a user-supplied ``decomposition`` argument into a
    fully-specified options dict, or ``None`` for "no decomposition".

    Accepts:

    * ``None`` or ``"off"`` — returns ``None``.
    * ``"auto"`` — returns the :data:`AUTO_DEFAULTS` preset.
    * ``"aggressive"`` — returns the :data:`AGGRESSIVE_DEFAULTS` preset.
    * a ``dict`` — merged onto :data:`AUTO_DEFAULTS`. Unknown keys raise
      :class:`ValueError` with the list of valid keys.

    Any other type raises :class:`ValueError`.
    """
    if spec is None or spec == "off":
        return None
    if spec == "auto":
        return dict(AUTO_DEFAULTS)
    if spec == "aggressive":
        return dict(AGGRESSIVE_DEFAULTS)
    if isinstance(spec, dict):
        unknown = set(spec) - VALID_KEYS
        if unknown:
            raise ValueError(
                f"Unknown decomposition keys: {sorted(unknown)}. "
                f"Valid keys: {sorted(VALID_KEYS)}"
            )
        merged = dict(AUTO_DEFAULTS)
        merged.update(spec)
        return merged
    raise ValueError(
        f"Invalid decomposition spec: {spec!r}. Expected None, 'off', "
        f"'auto', 'aggressive', or a dict of keys in "
        f"{sorted(VALID_KEYS)}."
    )


# ---------------------------------------------------------------------------
# Operator symbols (mirrors from_process_tree.py)
# ---------------------------------------------------------------------------

_SEQUENCE = "->"
_PARALLEL = "+"
_INTERLEAVING = "o"
_LOOP = "*"
_PARALLEL_OPS = {_PARALLEL, _INTERLEAVING}


def _op_value(operator):
    if operator is None:
        return None
    return getattr(operator, "value", operator)


# ---------------------------------------------------------------------------
# Annotation
# ---------------------------------------------------------------------------

def _annotate(tree) -> Dict[int, Dict[str, int]]:
    """Iterative post-order: ``{id(node): {"leaves": int}}``.

    A "leaf" here means an activity leaf (a tree leaf with a non-``None``
    label). Tau leaves count as zero, matching the converter's "tau
    collapses to a direct edge" semantics — they have no diagram footprint.
    """
    info: Dict[int, Dict[str, int]] = {}
    stack: List[Tuple[Any, bool]] = [(tree, False)]
    while stack:
        node, visited = stack.pop()
        if not visited:
            stack.append((node, True))
            for c in getattr(node, "children", []) or []:
                stack.append((c, False))
            continue
        children = getattr(node, "children", []) or []
        if not children:
            label = getattr(node, "label", None)
            leaves = 0 if label is None else 1
        else:
            leaves = sum(info[id(c)]["leaves"] for c in children)
        info[id(node)] = {"leaves": leaves}
    return info


# ---------------------------------------------------------------------------
# Cut-point decision
# ---------------------------------------------------------------------------

def _decide_frontier(
    map_root,
    opts: Dict[str, Any],
    info: Dict[int, Dict[str, int]],
    *,
    is_root_map: bool,
) -> List[Any]:
    """Return the ordered list of cut tree-nodes that become plug-ins of
    *this* map. Order: left-to-right DFS, so plug-in naming and stacked
    rendering are deterministic.
    """
    cuts: List[Any] = []
    balance = opts.get("balance_ratio", 0.0) or 0.0
    floor = opts.get("min_leaves_to_decompose", 0) or 0
    map_root_op = _op_value(getattr(map_root, "operator", None))

    def passes_balance(node, parent_node):
        if balance <= 0 or parent_node is None:
            return True
        siblings = getattr(parent_node, "children", []) or [node]
        total = sum(info[id(c)]["leaves"] for c in siblings)
        if total <= 0:
            return True
        return (info[id(node)]["leaves"] / total) >= balance

    def walk(node, parent_op, parent_node, is_top_level_seq_child):
        op = _op_value(getattr(node, "operator", None))
        leaves = info[id(node)]["leaves"]

        candidate = False
        if leaves >= floor:
            if is_top_level_seq_child and opts.get("on_root_sequence"):
                candidate = True
            elif parent_op in _PARALLEL_OPS and opts.get("on_parallel"):
                candidate = True
            elif op == _LOOP and opts.get("on_loop"):
                candidate = True

        if candidate and not passes_balance(node, parent_node):
            candidate = False

        if candidate:
            cuts.append(node)
            return  # don't descend into a cut subtree

        for c in getattr(node, "children", []) or []:
            child_is_top_seq = (
                is_root_map and node is map_root and op == _SEQUENCE
            )
            walk(c, op, node, child_is_top_seq)

    # Seed: descend into the map's root subtree. The map's own root is
    # never cut (a map can't be its own stub).
    for c in getattr(map_root, "children", []) or []:
        child_is_top_seq = is_root_map and map_root_op == _SEQUENCE
        walk(c, map_root_op, map_root, child_is_top_seq)

    return cuts


def _enforce_cap(
    map_root,
    cuts: List[Any],
    opts: Dict[str, Any],
    info: Dict[int, Dict[str, int]],
) -> List[Any]:
    """Add more cuts until the map's residual leaf count fits ``cap``.

    Force-cut the largest operator-subtree that's not yet a cut, that
    isn't the map's own root, and that satisfies ``min_leaves_to_decompose``.
    Repeat until under cap or no more cut candidates exist.
    """
    cap = opts.get("max_leaves_per_map")
    if not cap or cap <= 0:
        return cuts
    floor = opts.get("min_leaves_to_decompose", 0) or 0

    cuts = list(cuts)
    cut_ids: Set[int] = {id(c) for c in cuts}

    def map_leaves():
        return info[id(map_root)]["leaves"] - sum(
            info[id(c)]["leaves"] for c in cuts
        )

    while map_leaves() > cap:
        # Find the largest operator-descendant not already cut, not the
        # map's own root, and not inside an existing cut. We don't recurse
        # into cuts because their interior belongs to the plug-in map.
        best = None
        best_leaves = 0

        def visit(node, inside_cut):
            nonlocal best, best_leaves
            if id(node) in cut_ids:
                return  # treat the cut as opaque to this map
            op = _op_value(getattr(node, "operator", None))
            if op is not None and node is not map_root:
                leaves = info[id(node)]["leaves"]
                if leaves >= floor and leaves > best_leaves:
                    best = node
                    best_leaves = leaves
            for c in getattr(node, "children", []) or []:
                visit(c, inside_cut)

        visit(map_root, False)
        if best is None:
            break
        cuts.append(best)
        cut_ids.add(id(best))

    # Re-sort the cuts in left-to-right DFS order so naming is deterministic.
    order: Dict[int, int] = {}
    counter = [0]

    def assign(node):
        order[id(node)] = counter[0]
        counter[0] += 1
        for c in getattr(node, "children", []) or []:
            assign(c)

    assign(map_root)
    cuts.sort(key=lambda n: order.get(id(n), 0))
    return cuts


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

#: Characters we strip from derived names. Tabs/newlines become spaces;
#: control characters and a few XML-noisy punctuation marks are dropped
#: entirely. Regular spaces are preserved — readable names like
#: ``"Reserve Build Slot to Snapshot Repo"`` produce clearer plug-in
#: maps in jUCMNav, and the name-wrap helper splits them on spaces for
#: narrower stub boxes.
_LABEL_TRIM_RE = re.compile(r"[\x00-\x1f]")
_LABEL_COLLAPSE_WS_RE = re.compile(r"\s+")


def _label_clean(name: str) -> str:
    """Normalise a label for use in a derived plug-in name.

    Strips control characters, collapses runs of whitespace to a single
    space, and trims. Spaces are preserved so derived names like
    ``"Reserve Build Slot to Snapshot Repo"`` flow naturally through
    the name-wrapping helper (which splits on whitespace). Returns
    an empty string when the input has no usable content.
    """
    s = _LABEL_TRIM_RE.sub("", name or "")
    s = _LABEL_COLLAPSE_WS_RE.sub(" ", s).strip()
    return s


def _first_label(node) -> str:
    """Left-to-right DFS — first non-empty leaf label, or ``""``."""
    if _op_value(getattr(node, "operator", None)) is None:
        return getattr(node, "label", None) or ""
    for c in getattr(node, "children", []) or []:
        x = _first_label(c)
        if x:
            return x
    return ""


def _last_label(node) -> str:
    if _op_value(getattr(node, "operator", None)) is None:
        return getattr(node, "label", None) or ""
    for c in reversed(getattr(node, "children", []) or []):
        x = _last_label(c)
        if x:
            return x
    return ""


def _dominant_label(node) -> str:
    """Most-frequent non-empty leaf label in the subtree, or ``""``."""
    counts: Counter = Counter()

    def visit(n):
        if _op_value(getattr(n, "operator", None)) is None:
            lab = getattr(n, "label", None)
            if lab:
                counts[lab] += 1
        for c in getattr(n, "children", []) or []:
            visit(c)

    visit(node)
    if not counts:
        return ""
    return counts.most_common(1)[0][0]


#: Maximum number of whitespace-separated words to keep in a derived
#: plug-in/stub name. Names longer than this are truncated to the
#: first ``MAX_NAME_WORDS`` words — the wrap helper would otherwise
#: produce four- or five-line stub captions for long phrases.
MAX_NAME_WORDS = 6


def _truncate_words(base: str) -> str:
    """Keep at most :data:`MAX_NAME_WORDS` whitespace-separated words."""
    words = base.split()
    if len(words) <= MAX_NAME_WORDS:
        return base.strip()
    return " ".join(words[:MAX_NAME_WORDS])


def _derive_name(cut_node, parent_node, used_names: Set[str]) -> str:
    """Produce a content-derived plug-in map name; guarantee uniqueness.

    Names preserve spaces from the original activity labels so jUCMNav
    and the PNG visualizer display them naturally and the
    :func:`pm4py_ucm.objects.ucm.util.name_wrap.wrap_name` helper can
    split overly-long names across two or three lines. Truncated to
    :data:`MAX_NAME_WORDS` words (default 6) to keep stub captions
    readable.
    """
    op = _op_value(getattr(cut_node, "operator", None))
    parent_op = _op_value(getattr(parent_node, "operator", None)) if parent_node else None

    first_lab = _label_clean(_first_label(cut_node))
    last_lab = _label_clean(_last_label(cut_node))

    if op == _LOOP:
        dom = _label_clean(_dominant_label(cut_node)) or first_lab
        base = "loop " + dom if dom else "loop"
    elif parent_op in _PARALLEL_OPS:
        # Parallel branches: use the first activity in the branch as
        # the plug-in name. We *don't* prefix with "branch" — the
        # AND-fork already conveys the branching, and "branch X" was
        # adding visual noise without information.
        if first_lab == last_lab and first_lab:
            base = first_lab
        elif first_lab and last_lab:
            base = first_lab + " to " + last_lab
        else:
            base = first_lab or "parallel"
    elif parent_op == _SEQUENCE:
        if not first_lab:
            base = "stage"
        elif first_lab == last_lab:
            base = first_lab
        else:
            base = first_lab + " to " + last_lab
    else:
        # Fallback for cap-induced cuts inside non-{+, ->} parents.
        base = "sub " + first_lab if first_lab else "sub"

    base = _truncate_words(base) or "Map"

    name = base
    n = 2
    while name in used_names:
        name = f"{base} {n}"
        n += 1
    used_names.add(name)
    return name


# ---------------------------------------------------------------------------
# Cut-handler context manager
# ---------------------------------------------------------------------------

@contextmanager
def _cut_handler_active(handler):
    """Scope-bind ``_ft._cut_handler`` to ``handler``; restore on exit."""
    prev = _ft._cut_handler
    _ft._cut_handler = handler
    try:
        yield
    finally:
        _ft._cut_handler = prev


# ---------------------------------------------------------------------------
# Build plan
# ---------------------------------------------------------------------------

class _PlanNode:
    """Internal: one entry in the decomposition plan tree.

    Each node corresponds to one UCMmap-to-be. ``subtree`` is the slice
    of the process tree this map will materialise; ``cuts`` is the
    ordered list of descendants that become Stubs in this map; each
    cut has a corresponding child :class:`_PlanNode` representing its
    plug-in.
    """
    __slots__ = ("subtree", "name", "is_root", "parent_of_cut",
                 "cuts", "children")

    def __init__(self, subtree, name: str, is_root: bool,
                 parent_of_cut):
        self.subtree = subtree
        self.name = name
        self.is_root = is_root
        # The tree node directly above ``subtree`` in the *original* tree —
        # used for content-derived naming of this plug-in. ``None`` only
        # for the root map.
        self.parent_of_cut = parent_of_cut
        self.cuts: List[Any] = []
        self.children: List["_PlanNode"] = []


def _parent_map(tree) -> Dict[int, Any]:
    """Return ``{id(child): parent_node}`` for every node in ``tree``."""
    out: Dict[int, Any] = {}
    stack = [tree]
    while stack:
        n = stack.pop()
        for c in getattr(n, "children", []) or []:
            out[id(c)] = n
            stack.append(c)
    return out


def _build_plan(tree, opts: Dict[str, Any]) -> _PlanNode:
    """Recursively decide cuts for every map in the decomposition."""
    info = _annotate(tree)
    parent_of = _parent_map(tree)
    used_names: Set[str] = set()

    def recurse(subtree, name_hint: Optional[str], is_root: bool,
                parent_of_cut) -> _PlanNode:
        if is_root:
            # Root map keeps its caller-supplied name. ``name_hint`` is
            # the converter's ``map_name`` parameter.
            name = name_hint or "DiscoveredMap"
            used_names.add(name)
        else:
            name = _derive_name(subtree, parent_of_cut, used_names)
        plan = _PlanNode(subtree, name, is_root, parent_of_cut)

        cuts = _decide_frontier(subtree, opts, info, is_root_map=is_root)
        cuts = _enforce_cap(subtree, cuts, opts, info)
        plan.cuts = cuts

        for c in cuts:
            child_plan = recurse(c, None, False, parent_of.get(id(c)))
            plan.children.append(child_plan)
        return plan

    return recurse(tree, "root", True, None)


def _flatten_plan(plan: _PlanNode) -> List[_PlanNode]:
    """Pre-order traversal — root first, then plug-ins in cut order."""
    out: List[_PlanNode] = [plan]
    for c in plan.children:
        out.extend(_flatten_plan(c))
    return out


# ---------------------------------------------------------------------------
# Build the UCM from a plan
# ---------------------------------------------------------------------------

def _build_ucm(plan: _PlanNode, urn_name: str, root_map_name: str) -> UCM:
    """Materialise every map of ``plan`` into a fresh :class:`UCM`.

    For each map we install a cut handler that emits a Stub for each
    intended cut child, recording (stub, plug-in plan) for later binding.
    """
    ucm = UCM(name=urn_name)
    # Re-name root in plan to whatever the caller asked for so the root
    # map keeps its existing name (spec requirement).
    plan.name = root_map_name

    # Pre-create every map so cross-map references are resolvable when
    # we wire bindings. Maps appear in pre-order: root, then plug-ins
    # in left-to-right cut order.
    plans = _flatten_plan(plan)
    map_for: Dict[int, UCM.UCMmap] = {}
    for pn in plans:
        ucm_map = ucm.add_map(name=pn.name)
        map_for[id(pn)] = ucm_map

    # Record stubs that need bindings. Filled during the attach pass.
    # Each entry: (stub_node, plug-in plan).
    stubs_to_bind: List[Tuple[UCM.Stub, _PlanNode]] = []

    # Materialise each map in pre-order. Cuts in a map become Stubs;
    # nested decisions are already captured in child plans.
    for pn in plans:
        ucm_map = map_for[id(pn)]
        start = UCM.StartPoint(name="start")
        end = UCM.EndPoint(name="end")
        ucm_map.add_node(start)
        ucm_map.add_node(end)

        cut_lookup: Dict[int, _PlanNode] = {id(c.subtree): c for c in pn.children}

        def cut_handler(t, entry, exit, m, _lookup=cut_lookup,
                        _stubs=stubs_to_bind, _map=ucm_map):
            child_plan = _lookup.get(id(t))
            if child_plan is None:
                return False
            stub = UCM.Stub(name=child_plan.name)
            _map.add_node(stub)
            _map.add_connection(entry, stub)
            _map.add_connection(stub, exit)
            _stubs.append((stub, child_plan))
            return True

        # Defensively size the recursion limit (long right-spine trees
        # from software-instrumentation logs trip the default 1000).
        saved = sys.getrecursionlimit()
        required = max(saved, _ft._tree_depth(pn.subtree) + 200)
        if required > saved:
            sys.setrecursionlimit(required)
        try:
            with _cut_handler_active(cut_handler):
                _ft._attach(pn.subtree, start, end, ucm_map)
        finally:
            sys.setrecursionlimit(saved)

    return ucm, stubs_to_bind, map_for


def _wire_bindings(
    stubs_to_bind: List[Tuple[UCM.Stub, "_PlanNode"]],
    map_for: Dict[int, "UCM.UCMmap"],
) -> None:
    """Create one :class:`UCM.PluginBinding` per recorded stub.

    Called *after* ``simplify_empty_points`` and
    ``insert_routing_empty_points`` have finished mutating the maps,
    so that the in/out arcs we record are the ones the exporter will
    actually emit (the simplifier can otherwise replace the arc objects
    out from under us).
    """
    for stub, child_plan in stubs_to_bind:
        plugin_map = map_for[id(child_plan)]
        # In/out arc lookup: a Stub on a plug-in fragment has exactly one
        # predecessor and one successor — the entry/exit arcs of the
        # subtree it replaces.
        in_arcs = stub.pred_connections
        out_arcs = stub.succ_connections
        if not in_arcs or not out_arcs:
            continue
        in_arc = in_arcs[0]
        out_arc = out_arcs[0]
        # Locate the plug-in's start/end points. A plug-in map has exactly
        # one of each (we just built them in :func:`_build_ucm`).
        starts = [n for n in plugin_map.nodes if isinstance(n, UCM.StartPoint)]
        ends = [n for n in plugin_map.nodes if isinstance(n, UCM.EndPoint)]
        if not starts or not ends:
            continue
        binding = UCM.PluginBinding(stub=stub, plugin=plugin_map)
        binding.add_in(parent_connection=in_arc, plugin_start=starts[0])
        binding.add_out(plugin_end=ends[0], parent_connection=out_arc)
        stub.bindings.append(binding)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def apply(tree, parameters: Optional[Dict[str, Any]] = None) -> UCM:
    """Convert ``tree`` into a multi-map UCM under the
    ``decomposition`` parameter.

    Parameters mirror :func:`from_process_tree.apply` plus:

    ``decomposition``
        Required (callers without this key are routed through the
        flat-map converter instead). Accepts ``"auto"``,
        ``"aggressive"``, or a dict — see :func:`parse_decomposition`.
    """
    parameters = dict(parameters or {})
    spec = parameters.pop("decomposition", None)
    opts = parse_decomposition(spec)
    if opts is None:
        # Defensive: caller should have routed this to the flat path.
        return _ft.apply(tree, parameters=parameters)

    map_name: str = parameters.get("map_name", "DiscoveredMap")
    urn_name: str = parameters.get("urn_name", "PM4PyDiscovery")
    simplify: bool = parameters.get("simplify", True)
    routing_points: bool = parameters.get("routing_empty_points", True)
    performers = parameters.get("performers")
    performer_kind = parameters.get("performer_kind")
    additional_components = parameters.get("additional_components")

    plan = _build_plan(tree, opts)
    ucm, stubs_to_bind, map_for = _build_ucm(plan, urn_name, map_name)

    if simplify:
        _ft.simplify_empty_points(ucm)
    if routing_points:
        _ft.insert_routing_empty_points(ucm)

    # Bindings *after* simplification so that the in/out arcs we record
    # are the surviving ones (the simplifier can replace arc objects).
    _wire_bindings(stubs_to_bind, map_for)

    if performers or additional_components:
        ucm.bind_performers(
            performers or {},
            kind=performer_kind,
            additional_components=additional_components,
        )
        # When decomposition pushed every RespRef into plug-in maps, the
        # parent map ends up with only Stubs and no ComponentRefs — so
        # jUCMNav shows the root with no actor context, even though
        # every plug-in is bound to a team. Surface each plug-in's
        # components on its parent map so the root reads with the same
        # swim-lane structure as the plug-ins.
        _propagate_components_to_stubs(ucm)

    return ucm


def _gather_plugin_performers(plugin_map: "UCM.UCMmap") -> List["UCM.ComponentElement"]:
    """Return the ordered, de-duplicated list of components used by
    ``plugin_map``'s responsibilities (and, transitively, the
    responsibilities of any nested plug-ins reachable through Stubs)."""
    seen: Set[int] = set()
    out: List = []

    def visit(m):
        for n in m.nodes:
            if isinstance(n, UCM.RespRef):
                rd = n.resp_def
                if rd is not None and rd.performer is not None:
                    cid = id(rd.performer)
                    if cid not in seen:
                        seen.add(cid)
                        out.append(rd.performer)
            elif isinstance(n, UCM.Stub):
                for b in getattr(n, "bindings", []) or []:
                    if b.plugin is not None and b.plugin is not m:
                        visit(b.plugin)

    visit(plugin_map)
    return out


def _propagate_components_to_stubs(ucm: UCM) -> None:
    """For every bound Stub, surface its plug-in's components on the
    parent map and (when the plug-in uses a single component) draw the
    stub inside that component's rectangle.

    Existing :class:`UCM.ComponentRef`s on the parent map are reused, so
    the pass is idempotent. Stubs whose plug-in uses several distinct
    components stay outside any rectangle on the parent map — the
    multi-team context is conveyed by the plug-in itself.
    """
    for m in ucm.maps:
        existing: Dict[int, "UCM.ComponentRef"] = {
            id(cr.cont_def): cr for cr in m.cont_refs
        }
        for n in m.nodes:
            if not isinstance(n, UCM.Stub):
                continue
            bindings = getattr(n, "bindings", None) or []
            if not bindings:
                continue
            plugin = bindings[0].plugin
            if plugin is None:
                continue
            comps = _gather_plugin_performers(plugin)
            for comp in comps:
                if id(comp) not in existing:
                    existing[id(comp)] = m.add_component_ref(comp)
            if len(comps) == 1 and n.cont_ref is None:
                n.cont_ref = existing[id(comps[0])]
