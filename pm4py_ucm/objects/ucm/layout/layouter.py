"""
Compact left-to-right swim-lane auto-layout for UCM diagrams.

UCMs read like rivers from a StartPoint on the left to one or more
EndPoints on the right. Adding a *swim-lane* discipline — each top-level
:class:`UCM.ComponentRef` getting a disjoint horizontal band, nested
children getting sub-bands inside their parent — makes the rendering
also enforce the URN structural constraint that *components either
nest fully or do not overlap at all*:

* **Unrelated components** sit in disjoint Y-bands, so their rectangles
  cannot intersect regardless of how their bound nodes' X coordinates
  arrange themselves along the process flow.
* **Parent-child components** share Y-bands by construction — the
  child's allocated band is a sub-range of its parent's — so the child
  rectangle is geometrically contained in the parent rectangle.

The pipeline is a small Sugiyama-style drawing:

1.  **Measure labels.** For each path node we compute the on-screen
    footprint of its display label (using the same name-wrapping the
    exporter applies). This drives column widths, node-to-node spacing,
    and the size of every enclosing component rectangle.
2.  **Detect back-edges via DFS.** Loops translate to one back-edge
    from an OrFork to a preceding OrJoin. Layered drawing only works on
    DAGs, so back-edges are flagged and excluded from layer assignment.
3.  **Longest-path layer assignment.** Each node gets a layer
    (= its X coordinate index) equal to one more than the deepest layer
    of any predecessor (over forward edges).
4.  **Lane allocation.** Top-level component refs are ordered by the
    minimum layer of any node bound anywhere in their subtree, then
    stacked top-to-bottom. Children recurse into sub-bands.
5.  **Barycentric Y placement, constrained to the lane.** Each bound
    node's Y is the mean of its predecessors' Ys, clamped to the
    interior of its component's lane.
6.  **Component bounding boxes.** Each :class:`UCM.ComponentRef` has its
    Y-range pre-fixed by the lane allocation; the X-range is the X
    extent of its bound nodes (expanded by each node's actual label
    width) and any nested children, plus padding.

The layouter is deterministic and idempotent.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from ..obj import UCM
from ..util.name_wrap import label_dimensions, DEFAULT_MAX_WIDTH as _WRAP_WIDTH


# Default spacing — chosen for a tight but readable view.
DEFAULT_X_GAP = 90
DEFAULT_Y_GAP = 70
DEFAULT_X_ORIGIN = 60
DEFAULT_Y_ORIGIN = 100

#: Padding around contained nodes on each side, inside a lane.
DEFAULT_COMP_PADDING = 30
#: Extra height at the top of a lane reserved for the component's label.
DEFAULT_COMP_LABEL_PAD = 24
#: Vertical gap between two top-level (sibling) lanes.
DEFAULT_LANE_GAP = 20

#: Approximate per-character width in pixels at the default font size,
#: chosen to roughly match jUCMNav's default font rendering. Used to
#: turn a character-count label width into a pixel footprint.
_CHAR_WIDTH_PX = 7
#: Approximate per-line height in pixels.
_LINE_HEIGHT_PX = 16

#: Half-size in pixels of a small/control path node (StartPoint, OrFork,
#: AndJoin, …) — fixed, since these don't display a meaningful name.
_CONTROL_HALF = 18
#: Minimum half-width for a RespRef even when its name is short. Ensures
#: tiny labels like "A" or "Triage" still produce a reasonable rectangle.
_RESP_MIN_HALF = 32
#: Extra horizontal padding inside a RespRef rectangle around the text.
_RESP_X_PADDING = 14
#: Extra vertical padding inside a RespRef rectangle around the text.
_RESP_Y_PADDING = 10


def apply_layout(
    ucm: UCM,
    x_gap: int = DEFAULT_X_GAP,
    y_gap: int = DEFAULT_Y_GAP,
    x_origin: int = DEFAULT_X_ORIGIN,
    y_origin: int = DEFAULT_Y_ORIGIN,
) -> None:
    """Lay out every map of ``ucm`` in place.

    After this call every PathNode has integer ``x`` / ``y`` coordinates,
    and every ComponentRef has ``x`` / ``y`` / ``width`` / ``height``
    fitted so that unrelated component rectangles never overlap and
    nested ones are fully enclosed in their parent.

    The function is idempotent and overwrites any previous coordinates.
    """
    for ucm_map in ucm.maps:
        _layout_map(ucm_map, x_gap, y_gap, x_origin, y_origin)


def _node_half_size(n: "UCM.PathNode") -> Tuple[int, int]:
    """Return ``(half_width, half_height)`` of node ``n``'s display box,
    in pixels. RespRefs use their wrapped display name to compute width;
    every other path node uses the same fixed control-node size."""
    if not isinstance(n, UCM.RespRef):
        return _CONTROL_HALF, _CONTROL_HALF
    name = n.effective_name
    n_lines, longest_chars = label_dimensions(name)
    half_w = max(
        _RESP_MIN_HALF,
        (longest_chars * _CHAR_WIDTH_PX) // 2 + _RESP_X_PADDING,
    )
    half_h = max(
        _CONTROL_HALF,
        (n_lines * _LINE_HEIGHT_PX) // 2 + _RESP_Y_PADDING,
    )
    return half_w, half_h


def apply_layout(
    ucm: UCM,
    x_gap: int = DEFAULT_X_GAP,
    y_gap: int = DEFAULT_Y_GAP,
    x_origin: int = DEFAULT_X_ORIGIN,
    y_origin: int = DEFAULT_Y_ORIGIN,
) -> None:
    """Lay out every map of ``ucm`` in place.

    After this call every PathNode has integer ``x`` / ``y`` coordinates,
    and every ComponentRef has ``x`` / ``y`` / ``width`` / ``height``
    fitted so that unrelated component rectangles never overlap and
    nested ones are fully enclosed in their parent.

    The function is idempotent and overwrites any previous coordinates.
    """
    for ucm_map in ucm.maps:
        _layout_map(ucm_map, x_gap, y_gap, x_origin, y_origin)


# ---------------------------------------------------------------------------
# Per-map layout
# ---------------------------------------------------------------------------

def _layout_map(
    m: UCM.UCMmap, x_gap: int, y_gap: int, x_origin: int, y_origin: int,
) -> None:
    if not m.nodes:
        return

    # Per-node footprint. Used everywhere from column width assignment to
    # bounding-box fitting. RespRef widths depend on their (wrapped) name.
    half_size: Dict[UCM.PathNode, Tuple[int, int]] = {
        n: _node_half_size(n) for n in m.nodes
    }

    back_edges = _detect_back_edges(m)
    layer = _assign_layers(m, back_edges)

    # Build component lanes — each ComponentRef gets a Y-range that is
    # disjoint from unrelated lanes and nested inside its parent.
    lane_of = _build_lanes(m, layer, y_gap, half_size)

    # Map every bound node to its component's Y-range.
    node_band: Dict[UCM.PathNode, Tuple[float, float]] = {}
    for n in m.nodes:
        cr = n.cont_ref
        if cr is not None and cr in lane_of:
            node_band[n] = _interior_band(cr, lane_of, m)

    # Group nodes by layer.
    by_layer: Dict[int, List[UCM.PathNode]] = defaultdict(list)
    for n in m.nodes:
        by_layer[layer[n]].append(n)

    # ---- Initial Y --------------------------------------------------
    y_pos: Dict[UCM.PathNode, float] = {}
    for l in sorted(by_layer):
        for n in by_layer[l]:
            y_pos[n] = _initial_y(n, y_pos, y_gap, back_edges, node_band,
                                   half_size)
        _separate_within_lanes(by_layer[l], y_pos, y_gap, node_band, half_size)

    # ---- Refinement: two passes of barycentric smoothing -------------
    for _ in range(2):
        for l in sorted(by_layer):
            nodes_here = by_layer[l]
            for n in nodes_here:
                avg = _average_neighbour_y(n, y_pos, back_edges, side="pred")
                if avg is not None:
                    y_pos[n] = _clamp(avg, node_band.get(n))
            _separate_within_lanes(nodes_here, y_pos, y_gap, node_band,
                                    half_size)
        for l in sorted(by_layer, reverse=True):
            nodes_here = by_layer[l]
            for n in nodes_here:
                avg = _average_neighbour_y(n, y_pos, back_edges, side="succ")
                if avg is not None:
                    y_pos[n] = _clamp(avg, node_band.get(n))
            _separate_within_lanes(nodes_here, y_pos, y_gap, node_band,
                                    half_size)

    # ---- X coordinates -----------------------------------------------
    # Per-layer column width: widest node's half-width on either side,
    # plus the configured x_gap as a base. This way wider labels don't
    # crowd into their neighbours and short ones don't waste space.
    layer_indices = sorted(by_layer)
    layer_x: Dict[int, int] = {}
    cursor = x_origin
    prev_half_w = 0
    for l in layer_indices:
        widest_here = max(half_size[n][0] for n in by_layer[l])
        if l == layer_indices[0]:
            cursor = x_origin + widest_here
        else:
            cursor += prev_half_w + max(x_gap // 2, 1) + widest_here
        layer_x[l] = cursor
        prev_half_w = widest_here

    for n, raw_y in y_pos.items():
        n.x = layer_x[layer[n]]
        n.y = int(round(y_origin + raw_y))

    # ---- Fit component bounding boxes (Y comes from lanes, X from
    # ---- the X extent of bound nodes and nested children) ------------
    _fit_components(m, lane_of, x_origin, y_origin, half_size)


# ---------------------------------------------------------------------------
# Back-edge detection
# ---------------------------------------------------------------------------

def _detect_back_edges(m: UCM.UCMmap) -> Set["UCM.NodeConnection"]:
    """Detect back-edges via DFS: an edge from a vertex to one currently
    on the DFS stack ("gray") is a back-edge and is dropped from the
    DAG used by the layered drawing."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[UCM.PathNode, int] = {n: WHITE for n in m.nodes}
    back: Set[UCM.NodeConnection] = set()

    for root in m.nodes:
        if color[root] != WHITE:
            continue
        stack: List[Tuple[UCM.PathNode, int]] = [(root, 0)]
        color[root] = GRAY
        while stack:
            n, i = stack[-1]
            succs = n.succ_connections
            if i >= len(succs):
                color[n] = BLACK
                stack.pop()
                continue
            stack[-1] = (n, i + 1)
            tgt = succs[i].target
            if color.get(tgt, WHITE) == WHITE:
                color[tgt] = GRAY
                stack.append((tgt, 0))
            elif color[tgt] == GRAY:
                back.add(succs[i])
    return back


# ---------------------------------------------------------------------------
# Layer assignment
# ---------------------------------------------------------------------------

def _assign_layers(
    m: UCM.UCMmap, back: Set["UCM.NodeConnection"],
) -> Dict[UCM.PathNode, int]:
    """Longest-path layering on the DAG that remains after dropping the
    back-edges. Sources get layer 0; every other node gets
    ``1 + max(layer(pred))`` over its forward incoming edges.
    """
    fwd_in: Dict[UCM.PathNode, List[UCM.PathNode]] = defaultdict(list)
    fwd_out: Dict[UCM.PathNode, List[UCM.PathNode]] = defaultdict(list)
    for n in m.nodes:
        for c in n.succ_connections:
            if c in back:
                continue
            fwd_out[n].append(c.target)
            fwd_in[c.target].append(n)

    indeg = {n: len(fwd_in[n]) for n in m.nodes}
    layer: Dict[UCM.PathNode, int] = {n: 0 for n in m.nodes}
    queue: List[UCM.PathNode] = [n for n in m.nodes if indeg[n] == 0]
    queue.sort(key=m.nodes.index)
    head = 0
    while head < len(queue):
        n = queue[head]; head += 1
        for t in fwd_out[n]:
            layer[t] = max(layer[t], layer[n] + 1)
            indeg[t] -= 1
            if indeg[t] == 0:
                queue.append(t)
    return layer


# ---------------------------------------------------------------------------
# Lane allocation
# ---------------------------------------------------------------------------

def _build_lanes(
    m: UCM.UCMmap,
    layer: Dict[UCM.PathNode, int],
    y_gap: int,
    half_size: Dict[UCM.PathNode, Tuple[int, int]],
) -> Dict["UCM.ComponentRef", Tuple[float, float]]:
    """Allocate a Y-band ``(y_top, y_bottom)`` to each ComponentRef on
    the map, top-down. Top-level components are stacked vertically (no
    overlap); children recursively occupy sub-bands inside their parent
    (full containment).

    The lane height for each component is sized from the actual label
    footprint of every bound node, so tall multi-line labels get a
    properly tall band.
    """
    lane: Dict[UCM.ComponentRef, Tuple[float, float]] = {}
    if not m.cont_refs:
        return lane

    # ---- Subtree nodes ------------------------------------------------
    def subtree_nodes(cr: "UCM.ComponentRef") -> List[UCM.PathNode]:
        out = list(m.nodes_in(cr))
        for c in cr.children:
            out.extend(subtree_nodes(c))
        return out

    def min_layer_for(cr: "UCM.ComponentRef") -> int:
        nodes = subtree_nodes(cr)
        return min((layer[n] for n in nodes), default=10**9)

    # ---- Required height of a subtree --------------------------------
    # The lane must vertically accommodate:
    #   - the tallest sum of node-heights in any one column (layer) of
    #     nodes bound directly to *this* component, separated by y_gap,
    #   - all of its children's lanes, stacked,
    #   - top/bottom padding plus a label band.
    def required_height(cr: "UCM.ComponentRef") -> float:
        own_nodes = m.nodes_in(cr)
        # Group own nodes by layer to find the column with the most
        # vertical extent — that's the lower bound on lane height.
        per_layer: Dict[int, List[UCM.PathNode]] = defaultdict(list)
        for n in own_nodes:
            per_layer[layer[n]].append(n)
        max_own_height = 0.0
        for l_nodes in per_layer.values():
            col_height = sum(2 * half_size[n][1] for n in l_nodes)
            if len(l_nodes) > 1:
                col_height += (len(l_nodes) - 1) * y_gap
            if col_height > max_own_height:
                max_own_height = col_height

        children_height = sum(required_height(c) for c in cr.children)
        if len(cr.children) > 1:
            children_height += (len(cr.children) - 1) * DEFAULT_LANE_GAP / 2

        total = max_own_height + children_height
        total += 2 * DEFAULT_COMP_PADDING + DEFAULT_COMP_LABEL_PAD
        minimum = DEFAULT_COMP_PADDING * 2 + DEFAULT_COMP_LABEL_PAD + y_gap
        if total < minimum:
            total = minimum
        return total

    def _own_band_height(cr: "UCM.ComponentRef") -> float:
        """Height needed for the component's *own* (direct) nodes — the
        same metric used by ``required_height`` so the parent's reserved
        own-band matches what was budgeted."""
        own_nodes = m.nodes_in(cr)
        per_layer: Dict[int, List[UCM.PathNode]] = defaultdict(list)
        for n in own_nodes:
            per_layer[layer[n]].append(n)
        h = 0.0
        for l_nodes in per_layer.values():
            col_h = sum(2 * half_size[n][1] for n in l_nodes)
            if len(l_nodes) > 1:
                col_h += (len(l_nodes) - 1) * y_gap
            if col_h > h:
                h = col_h
        return h

    # ---- Assign Y-bands top-down ------------------------------------
    def assign(cr: "UCM.ComponentRef", y_top: float, y_bottom: float) -> None:
        lane[cr] = (y_top, y_bottom)
        if not cr.children:
            return
        # Top of usable area: below this component's label band.
        usable_top = y_top + DEFAULT_COMP_LABEL_PAD + DEFAULT_COMP_PADDING
        usable_bot = y_bottom - DEFAULT_COMP_PADDING

        # Reserve own-node band at the top, stack children below.
        own_band = _own_band_height(cr)
        child_top = usable_top + own_band + (DEFAULT_LANE_GAP / 2 if own_band else 0)
        if child_top > usable_bot:
            # Parent was under-sized: collapse the own-band to keep the
            # children inside. Should not happen if required_height did
            # its job, but bound defensively just in case.
            child_top = usable_bot

        # Distribute the remaining space among children proportionally
        # to their required heights. We never inflate beyond what each
        # child asked for, and never run past ``usable_bot`` — the
        # combination guarantees the parent fully contains every child.
        child_heights = [required_height(c) for c in cr.children]
        total = sum(child_heights)
        remaining = max(0.0, usable_bot - child_top)
        if total > 0 and remaining > total:
            # Have extra room — distribute proportionally, but cap so the
            # last child can never exceed usable_bot.
            ratio = remaining / total
        else:
            ratio = 1.0
        cursor = child_top
        for child, h in zip(cr.children, child_heights):
            scaled = h * ratio
            child_bot = min(cursor + scaled, usable_bot)
            assign(child, cursor, child_bot)
            cursor = child_bot

    # ---- Top-level lanes --------------------------------------------
    roots = [cr for cr in m.cont_refs if cr.parent is None]
    roots.sort(key=min_layer_for)

    cursor = 0.0
    for cr in roots:
        h = required_height(cr)
        assign(cr, cursor, cursor + h)
        cursor += h + DEFAULT_LANE_GAP

    return lane


def _interior_band(
    cr: "UCM.ComponentRef",
    lane: Dict["UCM.ComponentRef", Tuple[float, float]],
    m: UCM.UCMmap,
) -> Tuple[float, float]:
    """Return the (y_min, y_max) sub-region of ``cr``'s lane that the
    component's *own* (not its descendants') nodes may occupy.

    The region sits above any nested children's bands and below the
    parent's label/padding. Children eat from the bottom of the parent's
    usable area, so the parent's own residual band is whatever remains
    at the top."""
    y_top, y_bottom = lane[cr]
    inner_top = y_top + DEFAULT_COMP_LABEL_PAD + DEFAULT_COMP_PADDING
    inner_bot = y_bottom - DEFAULT_COMP_PADDING
    if cr.children:
        # Lowest top among the children's lanes is the boundary.
        child_top = min(lane[c][0] for c in cr.children)
        inner_bot = min(inner_bot, child_top - DEFAULT_LANE_GAP / 2)
    if inner_bot < inner_top:
        inner_bot = inner_top
    return (inner_top, inner_bot)


# ---------------------------------------------------------------------------
# Y placement
# ---------------------------------------------------------------------------

def _initial_y(
    n: UCM.PathNode,
    y_pos: Dict[UCM.PathNode, float],
    y_gap: int,
    back: Set["UCM.NodeConnection"],
    node_band: Dict[UCM.PathNode, Tuple[float, float]],
    half_size: Dict[UCM.PathNode, Tuple[int, int]],
) -> float:
    """Compute initial Y for ``n``, then clamp into its lane if any."""
    band = node_band.get(n)
    preds = [c.source for c in n.pred_connections
             if c not in back and c.source in y_pos]
    if not preds:
        if band is not None:
            return (band[0] + band[1]) / 2.0
        return 0.0
    if len(preds) == 1:
        p = preds[0]
        sibs = [c.target for c in p.succ_connections if c not in back]
        if len(sibs) > 1 and n in sibs:
            idx = sibs.index(n)
            # Spread fork outputs based on the cumulative height of siblings.
            # Using max half-height across siblings gives a stable, simple
            # spread that respects the tallest label among the branches.
            sib_half = max(half_size[s][1] for s in sibs)
            step = max(y_gap, 2 * sib_half + 10)
            offset = (idx - (len(sibs) - 1) / 2.0) * step
            y = y_pos[p] + offset
        else:
            y = y_pos[p]
    else:
        y = sum(y_pos[p] for p in preds) / len(preds)
    return _clamp(y, band)


def _average_neighbour_y(
    n: UCM.PathNode,
    y_pos: Dict[UCM.PathNode, float],
    back: Set["UCM.NodeConnection"],
    side: str,
) -> Optional[float]:
    if side == "pred":
        conns = n.pred_connections
        get = lambda c: c.source
    else:
        conns = n.succ_connections
        get = lambda c: c.target
    neighbours = [get(c) for c in conns if c not in back and get(c) in y_pos]
    if not neighbours:
        return None
    return sum(y_pos[v] for v in neighbours) / len(neighbours)


def _clamp(y: float, band: Optional[Tuple[float, float]]) -> float:
    """Clamp Y into ``band`` if one is given."""
    if band is None:
        return y
    y_min, y_max = band
    if y < y_min:
        return y_min
    if y > y_max:
        return y_max
    return y


def _separate_within_lanes(
    nodes_here: List[UCM.PathNode],
    y_pos: Dict[UCM.PathNode, float],
    y_gap: int,
    node_band: Dict[UCM.PathNode, Tuple[float, float]],
    half_size: Dict[UCM.PathNode, Tuple[int, int]],
) -> None:
    """Resolve vertical overlaps inside each layer, *separately for each
    lane plus a free-floating group for unbound nodes*. Bound nodes are
    additionally re-clamped into their lane so the sweep cannot push
    them out of the rectangle.

    Each pair of neighbouring nodes must be at least the sum of their
    half-heights plus ``y_gap`` apart, so tall multi-line labels don't
    collide with their neighbours.
    """
    if len(nodes_here) <= 1:
        return
    groups: Dict[Optional[Tuple[float, float]], List[UCM.PathNode]] = defaultdict(list)
    for n in nodes_here:
        groups[node_band.get(n)].append(n)
    for band, group in groups.items():
        if len(group) <= 1:
            continue
        group.sort(key=lambda v: y_pos[v])
        for i in range(1, len(group)):
            prev, cur = group[i - 1], group[i]
            min_gap = half_size[prev][1] + half_size[cur][1] + y_gap // 2
            if y_pos[cur] < y_pos[prev] + min_gap:
                y_pos[cur] = y_pos[prev] + min_gap
        if band is not None:
            y_min, y_max = band
            top = y_pos[group[0]]
            bot = y_pos[group[-1]]
            if bot > y_max:
                dy = bot - y_max
                shift = min(dy, max(0.0, top - y_min))
                for v in group:
                    y_pos[v] -= shift
            for v in group:
                y_pos[v] = _clamp(y_pos[v], band)


# ---------------------------------------------------------------------------
# Component bounding boxes
# ---------------------------------------------------------------------------

def _fit_components(
    m: UCM.UCMmap,
    lane: Dict["UCM.ComponentRef", Tuple[float, float]],
    x_origin: int,
    y_origin: int,
    half_size: Dict[UCM.PathNode, Tuple[int, int]],
) -> None:
    """Size every ComponentRef. Y comes from the pre-allocated lane;
    X comes from the X-extent of the component's bound nodes (using
    each node's actual label half-width) and any nested child
    rectangles, plus padding."""
    if not m.cont_refs:
        return

    order: List[UCM.ComponentRef] = []
    visited: Set[UCM.ComponentRef] = set()

    def visit(cr: "UCM.ComponentRef") -> None:
        if cr in visited:
            return
        visited.add(cr)
        for child in cr.children:
            visit(child)
        order.append(cr)

    for cr in m.cont_refs:
        visit(cr)

    for cr in order:
        own_nodes = m.nodes_in(cr)
        child_boxes = [(c.x, c.x + c.width) for c in cr.children]
        xs: List[float] = []
        for n in own_nodes:
            hw = half_size[n][0]
            xs.append(n.x - hw)
            xs.append(n.x + hw)
        for x0, x1 in child_boxes:
            xs.append(x0 - DEFAULT_COMP_PADDING / 2)
            xs.append(x1 + DEFAULT_COMP_PADDING / 2)

        if xs:
            x_min = min(xs) - DEFAULT_COMP_PADDING
            x_max = max(xs) + DEFAULT_COMP_PADDING
        else:
            x_min = x_origin
            x_max = x_origin + 120

        y_top, y_bot = lane[cr]
        cr.x = int(round(x_min))
        cr.y = int(round(y_origin + y_top))
        cr.width = int(round(x_max - x_min))
        cr.height = int(round(y_bot - y_top))
