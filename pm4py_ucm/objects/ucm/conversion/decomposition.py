"""
Hierarchical decomposition of process trees into multi-map UCMs.

The default :func:`from_process_tree.apply` converter produces a single
flat UCM map covering the entire tree. For complex trees this is
visually overwhelming. This module produces a *root* map plus one or
more *plug-in* (sub-)maps connected by UCM :class:`UCM.Stub` nodes via
:class:`UCM.PluginBinding`. The session-4.6 binding machinery is used
verbatim — no parallel implementation.

Four semantic boundary rules drive the decomposition:

* ``on_root_sequence`` — each child of a top-level ``->`` (sequence)
  operator becomes its own plug-in map. The root map reads as a
  left-to-right chain of stubs representing process "phases".
* ``on_parallel`` — each branch of a ``+`` (parallel) operator
  becomes its own plug-in map. Parallel branches are usually
  semantically independent units of work, and the AND-fork/join
  vertical-expansion cost is replaced by a single stub.
* ``on_alternative`` — each branch of an ``×`` (XOR) / ``∨`` (OR)
  operator becomes its own plug-in map. Alternative branches are
  semantically distinct paths through the model; replacing each with
  a single stub keeps the OR-fork/OR-join visible while moving the
  alternative-body detail to its own map.
* ``on_loop`` — each ``*`` (loop) operator's entire expansion
  becomes its own plug-in map. The parent map then reads as a
  forward flow with one stub representing the iteration. When the
  outermost operator of the input tree is itself a loop, the root
  map gets a single "loop" stub and the loop expansion is moved
  into a plug-in — the parent map can't *be* its own stub, so a
  synthetic root sequence is introduced around the loop.

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
    "on_alternative",
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
    "on_alternative": True,
    "on_loop": True,
    "max_leaves_per_map": 20,
    "min_leaves_to_decompose": 4,
    "balance_ratio": 0.2,
}

#: "aggressive" preset — same boundary rules, tighter cap.
AGGRESSIVE_DEFAULTS: Dict[str, Any] = dict(AUTO_DEFAULTS, max_leaves_per_map=10)

#: Sentinel for a dimension whose value is fitted to the tree shape at
#: :func:`apply`-time rather than fixed. A decomposition dict may carry it for
#: ``max_leaves_per_map`` / ``min_leaves_to_decompose`` / ``balance_ratio``;
#: :func:`suggest_decomposition` supplies the value. This is what the string
#: spec ``"auto"`` expands to.
AUTO_DIM = "auto"


def suggest_decomposition(tree, *, level: str = "auto") -> Dict[str, Any]:
    """Decomposition parameters *fitted to the shape* of ``tree``.

    The map layout is set mostly by the tree's operator boundaries (all four
    ``on_*`` rules are enabled — there is no value in decomposing on only some
    operator kinds), so the two size knobs are what a good default should scale
    with the tree's activity-leaf count ``N`` rather than fix at magic numbers:

    * ``max_leaves_per_map = clamp(round(k·√N), 6, 40)`` — a **sub-linear** cap
      (``k`` = 1.5 for ``"auto"``, 1.0 for ``"fine"``); the force-cut safety net
      for a wide, flat subtree with no internal boundary to split on.
    * ``min_leaves_to_decompose = clamp(round(0.15·N), 3, 12)`` — the real
      fragmentation floor: a large tree gets bigger plug-ins instead of dozens
      of tiny maps, while a small tree falls below it and renders flat.

    ``balance_ratio`` stays 0.2. Returns a complete options dict for
    :func:`apply` (no sentinels).
    """
    import math

    n = _annotate(tree)[id(tree)]["leaves"] if tree is not None else 0
    if n <= 0:
        cap, floor = 8, 4
    else:
        k = 1.0 if level == "fine" else 1.5
        cap = max(6, min(40, round(k * math.sqrt(n))))
        floor = max(3, min(12, round(0.15 * n)))
        floor = min(floor, cap - 1)
    return {
        "on_root_sequence": True, "on_parallel": True,
        "on_alternative": True, "on_loop": True,
        "max_leaves_per_map": int(cap),
        "min_leaves_to_decompose": int(floor),
        "balance_ratio": 0.2,
    }


def _resolve_shape_dims(opts: Optional[Dict[str, Any]], tree):
    """Replace any :data:`AUTO_DIM` sentinel in ``opts`` with the value
    :func:`suggest_decomposition` fits to ``tree``. A no-op when there is none,
    so the tree is only walked for leaf-count when actually needed."""
    if not opts:
        return opts
    autos = [k for k in ("max_leaves_per_map", "min_leaves_to_decompose",
                         "balance_ratio") if opts.get(k) == AUTO_DIM]
    if not autos:
        return opts
    fitted = suggest_decomposition(tree)
    opts = dict(opts)
    for k in autos:
        opts[k] = fitted[k]
    return opts


def parse_decomposition(spec) -> Optional[Dict[str, Any]]:
    """Coerce a user-supplied ``decomposition`` argument into a
    fully-specified options dict, or ``None`` for "no decomposition".

    Accepts:

    * ``None`` or ``"off"`` — returns ``None``.
    * ``"auto"`` — **shape-fitted**: all boundary rules on, the two size
      dimensions carried as the :data:`AUTO_DIM` sentinel and resolved against
      the tree in :func:`apply` (see :func:`suggest_decomposition`).
    * ``"aggressive"`` — returns the fixed :data:`AGGRESSIVE_DEFAULTS` preset.
    * a ``dict`` — merged onto :data:`AUTO_DEFAULTS`; a size dimension may be
      the :data:`AUTO_DIM` sentinel to fit just that one to the shape. Unknown
      keys raise :class:`ValueError` with the list of valid keys.

    Any other type raises :class:`ValueError`.
    """
    if spec is None or spec == "off":
        return None
    if spec == "auto":
        # Shape-fit: dimensions resolved against the tree in apply() via the
        # AUTO_DIM sentinels; all boundary rules on.
        return dict(AUTO_DEFAULTS, max_leaves_per_map=AUTO_DIM,
                    min_leaves_to_decompose=AUTO_DIM)
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
_XOR = "X"
_OR = "O"
_PARALLEL_OPS = {_PARALLEL, _INTERLEAVING}
_CHOICE_OPS = {_XOR, _OR}


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
            elif parent_op in _CHOICE_OPS and opts.get("on_alternative"):
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
    elif parent_op in _CHOICE_OPS:
        # XOR / OR branches: same first-to-last recipe as parallels.
        # We don't prefix with "alt" or "branch" — the OR-fork on the
        # parent already conveys that this map is an alternative.
        if first_lab == last_lab and first_lab:
            base = first_lab
        elif first_lab and last_lab:
            base = first_lab + " to " + last_lab
        else:
            base = first_lab or "alternative"
    elif parent_op == _SEQUENCE:
        if not first_lab:
            base = "stage"
        elif first_lab == last_lab:
            base = first_lab
        else:
            base = first_lab + " to " + last_lab
    else:
        # Fallback for cap-induced cuts inside non-{+, ×, ->} parents.
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
# Synthetic root wrapper (root-loop edge case)
# ---------------------------------------------------------------------------
#
# When the input tree's outermost operator is ``*`` (loop) and the user
# wants ``on_loop`` decomposition, the natural intent is "make the root
# map a stub pointing to the loop's plug-in." But the cut-frontier walk
# treats the map root as opaque (a map can't be its own stub), so a
# top-level loop would otherwise stay drawn inline. Workaround: wrap
# the tree in a synthetic sequence whose only child is the loop, before
# the plan is built. The on_loop rule then fires on the loop child
# normally — the root map ends up with one stub for the loop, and the
# loop expansion moves into its own plug-in.
#
# Symmetric behaviour for top-level ``+`` / ``×`` / ``∨`` would be
# pointless: with on_parallel / on_alternative the children already
# become plug-ins. Only ``*`` needs the wrap because its rule cuts
# *the operator itself* rather than its children.

class _SyntheticOp:
    """Duck-typed enum-like for the synthetic-sequence wrapper.

    The process-tree converter reads operator value via ``.value``
    (see :func:`_op_value`), so we expose the symbol the same way as
    PM4Py's real :class:`Operator` enum.
    """
    __slots__ = ("value",)

    def __init__(self, value: str):
        self.value = value


class _SyntheticNode:
    """Duck-typed wrapper node — a synthetic ``->`` parent around the
    original loop root, so the existing root-sequence machinery picks
    it up as a cut candidate.
    """
    __slots__ = ("operator", "label", "children")

    def __init__(self, operator: str, children):
        self.operator = _SyntheticOp(operator)
        self.label = None
        self.children = list(children)


def _maybe_wrap_root_loop(tree, opts: Dict[str, Any]):
    """If ``tree`` is a loop and ``on_loop`` is enabled, return a
    single-child synthetic sequence wrapping it. Otherwise return
    ``tree`` unchanged. Also enables the wrap silently when only
    ``on_root_sequence`` is on — a top-level loop wrapped in a
    single-element sequence is otherwise indistinguishable from the
    flat root.
    """
    op = _op_value(getattr(tree, "operator", None))
    if op != _LOOP:
        return tree
    if not opts.get("on_loop"):
        return tree
    return _SyntheticNode(_SEQUENCE, [tree])


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


def _build_plan(
    tree,
    opts: Dict[str, Any],
    reserved_names: Optional[Set[str]] = None,
) -> _PlanNode:
    """Recursively decide cuts for every map in the decomposition.

    ``reserved_names`` seeds the uniqueness set for derived plug-in map
    names — pass the names of maps already present in a shared container
    so a second decomposition into the same :class:`UCM` cannot produce
    a duplicate map name (duplicates get a `` 2`` / `` 3`` suffix).
    """
    info = _annotate(tree)
    parent_of = _parent_map(tree)
    used_names: Set[str] = set(reserved_names or ())

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

def _build_ucm(
    plan: _PlanNode,
    urn_name: str,
    root_map_name: str,
    container: Optional[UCM] = None,
) -> UCM:
    """Materialise every map of ``plan`` into a fresh :class:`UCM` (or
    into ``container`` when given — the model-family assembly path).

    For each map we install a cut handler that emits a Stub for each
    intended cut child, recording (stub, plug-in plan) for later binding.
    """
    ucm = container if container is not None else UCM(name=urn_name)
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
    # Fit any shape-derived dimensions (the "auto" spec) to this tree — done
    # here because this is the one place with both the options and the tree,
    # so the app, the exported pipeline, and every family cell resolve
    # identically.
    opts = _resolve_shape_dims(opts, tree)

    map_name: str = parameters.get("map_name", "DiscoveredMap")
    urn_name: str = parameters.get("urn_name", "PM4PyDiscovery")
    simplify: bool = parameters.get("simplify", True)
    routing_points: bool = parameters.get("routing_empty_points", True)
    performers = parameters.get("performers")
    performer_kind = parameters.get("performer_kind")
    additional_components = parameters.get("additional_components")
    container: Optional[UCM] = parameters.get("container")

    # Handle the root-loop edge case: when the tree itself is a loop
    # and on_loop is enabled, wrap in a synthetic single-child sequence
    # so the loop becomes a cut candidate (the map root cannot be its
    # own stub). The synthetic wrapper participates in the plan, the
    # cut, and the converter's ``_attach`` walk transparently — its
    # only effect is that the root map contains a single stub pointing
    # at the loop plug-in.
    tree = _maybe_wrap_root_loop(tree, opts)

    reserved = (
        {m.name for m in container.maps} | {map_name}
        if container is not None else None
    )
    plan = _build_plan(tree, opts, reserved_names=reserved)
    ucm, stubs_to_bind, map_for = _build_ucm(
        plan, urn_name, map_name, container=container,
    )

    # Post-processing is scoped to the maps this call created — with an
    # existing ``container``, previously added maps have already been
    # processed and must not be touched again.
    new_maps = list(map_for.values())
    if simplify:
        for m in new_maps:
            _ft._simplify_map(ucm, m)
    if routing_points:
        for m in new_maps:
            _ft._insert_routing_for_map(ucm, m)

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
