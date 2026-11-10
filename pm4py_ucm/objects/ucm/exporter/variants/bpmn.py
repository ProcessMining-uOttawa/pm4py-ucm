"""Exporter for **BPMN 2.0 XML with BPMNDI** diagram interchange.

Writes a UCM as a standard BPMN 2.0 file that opens in bpmn.io, Camunda
Modeler, Signavio and ``pm4py.read_bpmn``, laid out the way the PNG
renderer's ``style="bpmn"`` shows it. The mapping is the one documented
in ``docs/bpmn_export.md``; in short:

* the whole UCM is **one pool** (a ``participant`` in a single
  ``collaboration``) whose process is the root map; every other map is
  its own ``process`` with its own ``BPMNDiagram``;
* **components are lanes**, nested through ``childLaneSet``; lanes tile
  the pool as full-width horizontal bands derived from the component
  rectangles, so the swim-lane layouter's output is lane-compatible as is;
* ``RespRef`` → ``task``; ``OrFork``/``OrJoin`` → ``exclusiveGateway``;
  ``AndFork``/``AndJoin`` → ``parallelGateway``; ``StartPoint`` /
  ``EndPoint`` → ``startEvent`` / ``endEvent``; routing points
  (``EmptyPoint``, ``Connect``, ``DirectionArrow``) are merged into the
  sequence flow they sit on and survive as **waypoints**;
* a **static stub** becomes a ``callActivity`` calling the plug-in's
  process; a **dynamic stub** becomes a collapsed ``subProcess`` whose body
  is an inclusive-gateway choice over one ``callActivity`` per plug-in
  (Z.151 semantics: every plug-in whose precondition holds runs);
* **performance metadata** travels as ``extensionElements`` on tasks and
  flows (``perf_<metric>`` → ``<ucm:metric>``), and the selected edge
  overlay text becomes the flow's visible ``name`` (``annotate="edges"``);
* node centres transfer **1:1** from the UCM coordinates; shape sizes are
  the label footprints the swim-lane layouter already reserves.

The writer is hand-rolled, like the ``.jucm`` one, so the output is
deterministic and byte-stable for a given model.
"""

from __future__ import annotations

import io
import re
import warnings
from typing import IO, Dict, List, Optional, Sequence, Tuple, Union

from ...obj import UCM
from ...layout.layouter import apply_layout, _node_half_size

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NS_BPMN = "http://www.omg.org/spec/BPMN/20100524/MODEL"
NS_BPMNDI = "http://www.omg.org/spec/BPMN/20100524/DI"
NS_DC = "http://www.omg.org/spec/DD/20100524/DC"
NS_DI = "http://www.omg.org/spec/DD/20100524/DI"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"
#: Extension namespace for what BPMN has no slot for: UCM ids and kinds,
#: pre/post-conditions, stub bindings and performance metrics. A URN so the
#: package claims no web domain; it is only an identifier.
NS_UCM = "urn:pm4py-ucm:bpmn:1.0"
#: Prefix under which the extension namespace is declared.
UCM_PREFIX = "ucm"
#: OMG "BPMN in Color" (non-normative) and bpmn.io's older twin: the two
#: attribute sets viewers read heat-map colours from.
NS_COLOR = "http://www.omg.org/spec/BPMN/non-normative/color/1.0"
NS_BIOC = "http://bpmn.io/schema/bpmn/biocolor/1.0"

#: Fixed shape sizes (``sizes="layout"``), in BPMNDI pixels. Gateways and
#: events are drawn at the habitual BPMN sizes closest to the UCM glyphs
#: they replace; tasks take the label footprint the layouter reserved.
GATEWAY_SIZE = 36
EVENT_SIZE = 30
#: Standard BPMN shape sizes (``sizes="canonical"``): the layout is scaled
#: up uniformly until neighbouring shapes of these sizes no longer touch.
CANONICAL_TASK = (100, 80)
CANONICAL_GATEWAY = 50
CANONICAL_EVENT = 36
#: Clearance kept between two neighbouring canonical shapes.
CANONICAL_GAP = 20
#: Left strip of a horizontal pool that carries its name.
POOL_LABEL_STRIP = 30
#: Left strip of a lane that carries its name.
LANE_LABEL_STRIP = 30
#: Padding between the outermost shape and the pool border.
POOL_PADDING = 40

_ROUTING_TYPES = (UCM.EmptyPoint, UCM.Connect, UCM.DirectionArrow)

_STUBS = ("call", "inline")
_LANES = ("components", "none")
_LANE_NAMES = ("plain", "with_kind")
_SIZES = ("layout", "canonical")
_METRICS = ("extension", "none")
_ANNOTATE = ("none", "edges", "all")
_DATA = ("none", "objects")
_ROUTING = ("straight", "orthogonal")
#: Size of a data-object shape and the strip above the pool that holds them.
DATA_OBJECT_SIZE = (36, 50)
DATA_STRIP = 90

#: Name of the metadata entry holding the selected node overlay text.
_PERF_KEY = "_perf"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def apply(
    ucm: UCM,
    file_path: Union[str, IO],
    parameters: Optional[dict] = None,
) -> None:
    """Serialise ``ucm`` to ``file_path`` as BPMN 2.0 XML.

    ``parameters`` takes the keyword arguments of
    :func:`serialize_to_string`.
    """
    parameters = dict(parameters or {})
    encoding = parameters.pop("encoding", "UTF-8")
    text = serialize_to_string(ucm, encoding=encoding, **parameters)
    if hasattr(file_path, "write"):
        file_path.write(text)
    else:
        with open(file_path, "w", encoding=encoding) as f:
            f.write(text)


def serialize_to_string(
    ucm: UCM,
    *,
    stubs: str = "call",
    lanes: str = "components",
    lane_names: str = "plain",
    sizes: str = "layout",
    metrics: str = "extension",
    annotate: str = "edges",
    heatmap_node: Optional[str] = None,
    heatmap_edge: Optional[str] = None,
    heatmap_global: bool = False,
    node_span: Optional[Tuple[float, float]] = None,
    edge_span: Optional[Tuple[float, float]] = None,
    data: str = "none",
    routing: str = "straight",
    layout: bool = True,
    validate: bool = True,
    encoding: str = "UTF-8",
) -> str:
    """Return the BPMN 2.0 XML for ``ucm`` as a string.

    Parameters
    ----------
    stubs
        ``"call"`` (default): a static stub is a ``callActivity`` calling
        the plug-in map's process, which keeps one process and one diagram
        per map. ``"inline"``: every stub, static or dynamic, is a collapsed
        ``subProcess`` whose body is the plug-in map itself (a dynamic stub's
        inclusive choice then leads to one nested sub-process per plug-in);
        only root maps become processes. Inline files have no call
        activities, so pm4py's reader (which knows none) reads them; a
        plug-in bound to several stubs is written once per stub.
    lanes
        ``"components"`` (default) draws every component as a lane;
        ``"none"`` emits a pool without lanes.
    lane_names
        ``"plain"`` names a lane after its component; ``"with_kind"``
        appends the component kind, e.g. ``Clerk «Actor»``.
    sizes
        ``"layout"`` (default): shapes take the footprint the layouter
        reserved, so node centres transfer 1:1 and the file opens looking
        like the ``style="bpmn"`` preview. ``"canonical"``: standard BPMN
        sizes (100 × 80 tasks, 50 × 50 gateways, 36 × 36 events) and the
        whole layout scaled up uniformly, by the smallest factor that keeps
        neighbouring shapes apart; topology and relative arrangement are
        preserved, absolute coordinates are not.
    metrics
        ``"extension"`` (default) writes every ``perf_*`` metadata entry as a
        ``<ucm:metric>`` extension element; ``"none"`` drops them.
    annotate
        Visible performance annotations: ``"none"``; ``"edges"`` (default)
        puts the selected edge overlay text in the sequence flow's name;
        ``"all"`` additionally attaches the node overlay text as a
        ``textAnnotation``.
    heatmap_node, heatmap_edge
        Metric keys (``"frequency"``, ``"mean_time"``, …) whose values
        colour the task boxes and the sequence flows, with the ramps the PNG
        heat-map uses (blue for counts, red for times): written as OMG
        *BPMN in Color* attributes and their bpmn.io ``bioc`` twins on the
        diagram shapes and edges, so any viewer that knows either shows
        them and every other ignores them.
    heatmap_global
        Normalise the heat-map against the whole model's min/max instead
        of each map's own (same meaning as in the PNG renderer).
    data
        ``"objects"`` writes every scenario :class:`UCM.Variable` as a
        ``dataObject`` (+ reference and shape in a strip above the pool),
        enumeration values in its documentation, and each
        :class:`UCM.ScenarioDef` as a documentation paragraph on the root
        process. ``"none"`` (default) leaves the scenario layer out; the
        guards are on the flows either way.
    routing
        ``"straight"`` (default) draws each flow through its UCM bend points
        as the UCM does. ``"orthogonal"`` turns every slanted segment into an
        elbow, horizontal first on a forward segment and vertical first on a
        backward one, so loop back-edges leave a task's box vertically
        instead of cutting through it.
    node_span, edge_span
        An explicit ``(lo, hi)`` range to normalise against instead, e.g.
        a family-wide range from
        :func:`pm4py_ucm.visualization.ucm.variants.classic.heat_span`, so
        every cell of a family reads on one scale. Overrides
        ``heatmap_global``.
    layout
        Run the bundled swim-lane layouter when no node is positioned yet.
        Positioned models keep their coordinates, unless two sibling
        components overlap vertically (graphviz clusters may), in which
        case the map is re-laid out so that lanes can tile the pool, and a
        :class:`UserWarning` says so.
    validate
        Refuse a structurally malformed model (see
        :func:`pm4py_ucm.objects.ucm.validate.check_ucm`).
    """
    _check_choice("stubs", stubs, _STUBS)
    _check_choice("lanes", lanes, _LANES)
    _check_choice("lane_names", lane_names, _LANE_NAMES)
    _check_choice("sizes", sizes, _SIZES)
    _check_choice("metrics", metrics, _METRICS)
    _check_choice("annotate", annotate, _ANNOTATE)
    _check_choice("data", data, _DATA)
    _check_choice("routing", routing, _ROUTING)

    if validate:
        from ...validate import check_ucm
        check_ucm(ucm)

    if layout and _needs_layout(ucm):
        apply_layout(ucm)
    if lanes == "components":
        for m in ucm.maps:
            if m.nodes and not _lanes_tile(m):
                warnings.warn(
                    f"Map {m.name!r}: sibling components overlap "
                    f"vertically, so they cannot be BPMN lanes; the map was "
                    f"re-laid out with the swim-lane layouter.",
                    UserWarning, stacklevel=2,
                )
                _relayout_map(ucm, m)

    opts = _Options(stubs=stubs, lanes=lanes, lane_names=lane_names,
                    sizes=sizes, metrics=metrics, annotate=annotate,
                    heatmap_node=heatmap_node, heatmap_edge=heatmap_edge,
                    heatmap_global=heatmap_global,
                    node_span=node_span, edge_span=edge_span,
                    data=data, routing=routing)
    model = _Model(ucm, opts)
    w = _XmlWriter(encoding=encoding)
    _emit_definitions(w, model)
    return w.text()


def _check_choice(name: str, value: str, allowed: Sequence[str]) -> None:
    if value not in allowed:
        raise ValueError(
            f"{name}={value!r} is not one of {', '.join(map(repr, allowed))}")


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

def _needs_layout(ucm: UCM) -> bool:
    """True when every node still sits at the origin."""
    for m in ucm.maps:
        for n in m.nodes:
            if n.x != 0 or n.y != 0:
                return False
    return any(m.nodes for m in ucm.maps)


def _lanes_tile(m: "UCM.UCMmap") -> bool:
    """True when, at every nesting level, sibling component rectangles
    occupy disjoint Y-ranges — the condition for drawing them as lanes."""
    def disjoint(siblings: List["UCM.ComponentRef"]) -> bool:
        bands = sorted((c.y, c.y + c.height) for c in siblings)
        for (a0, a1), (b0, b1) in zip(bands, bands[1:]):
            if b0 < a1:
                return False
        return all(disjoint(c.children) for c in siblings)
    return disjoint([c for c in m.cont_refs if c.parent is None])


def _relayout_map(ucm: UCM, m: "UCM.UCMmap") -> None:
    from ...layout.layouter import _layout_map, DEFAULT_X_GAP, DEFAULT_Y_GAP, \
        DEFAULT_X_ORIGIN, DEFAULT_Y_ORIGIN
    _layout_map(m, DEFAULT_X_GAP, DEFAULT_Y_GAP, DEFAULT_X_ORIGIN,
                DEFAULT_Y_ORIGIN)


def _shape_size(n: "UCM.PathNode", sizes: str = "layout") -> Tuple[int, int]:
    """``(width, height)`` of the BPMN shape standing in for ``n``."""
    if sizes == "canonical":
        if isinstance(n, (UCM.RespRef, UCM.Stub, UCM.Anything)):
            return CANONICAL_TASK
        if isinstance(n, (UCM.OrFork, UCM.OrJoin, UCM.AndFork, UCM.AndJoin)):
            return CANONICAL_GATEWAY, CANONICAL_GATEWAY
        if isinstance(n, _ROUTING_TYPES):
            return CANONICAL_GATEWAY, CANONICAL_GATEWAY
        return CANONICAL_EVENT, CANONICAL_EVENT
    if isinstance(n, (UCM.RespRef, UCM.Stub, UCM.Anything)):
        hw, hh = _node_half_size(n) if isinstance(n, UCM.RespRef) else (
            _label_half_size(_node_name(n)))
        return 2 * hw, 2 * hh
    if isinstance(n, (UCM.OrFork, UCM.OrJoin, UCM.AndFork, UCM.AndJoin)):
        return GATEWAY_SIZE, GATEWAY_SIZE
    return EVENT_SIZE, EVENT_SIZE


def _label_half_size(name: str) -> Tuple[int, int]:
    """Footprint of a labelled box (stubs), by the layouter's rules."""
    from ...layout.layouter import (
        _RESP_MIN_HALF, _CHAR_WIDTH_PX, _RESP_X_PADDING, _CONTROL_HALF,
        _LINE_HEIGHT_PX, _RESP_Y_PADDING,
    )
    from ...util.name_wrap import label_dimensions
    n_lines, longest = label_dimensions(name)
    return (max(_RESP_MIN_HALF, (longest * _CHAR_WIDTH_PX) // 2 + _RESP_X_PADDING),
            max(_CONTROL_HALF, (n_lines * _LINE_HEIGHT_PX) // 2 + _RESP_Y_PADDING))


_GENERATED_GATEWAY_NAME = re.compile(r"^(Or|And|Loop)(Fork|Join)\d*$")


def _node_name(n: "UCM.PathNode") -> str:
    """Display name without allocating an id for an unnamed node. The
    converters name forks and joins after their kind (``OrFork``,
    ``LoopJoin``); such names say nothing a BPMN gateway glyph does not,
    so they are dropped."""
    if isinstance(n, UCM.RespRef) and n.resp_def is not None:
        return n.resp_def.name or n.name
    if (isinstance(n, (UCM.OrFork, UCM.OrJoin, UCM.AndFork, UCM.AndJoin))
            and _GENERATED_GATEWAY_NAME.match(n.name or "")):
        return ""
    return n.name


def _clip_to_box(cx: float, cy: float, w: float, h: float,
                 tx: float, ty: float) -> Tuple[float, float]:
    """Point where the segment from the box centre ``(cx, cy)`` towards
    ``(tx, ty)`` leaves the ``w × h`` box."""
    dx, dy = tx - cx, ty - cy
    if dx == 0 and dy == 0:
        return cx, cy
    sx = (w / 2) / abs(dx) if dx else float("inf")
    sy = (h / 2) / abs(dy) if dy else float("inf")
    s = min(sx, sy, 1.0)
    return cx + dx * s, cy + dy * s


# ---------------------------------------------------------------------------
# Intermediate model
# ---------------------------------------------------------------------------

class _Options:
    def __init__(self, stubs, lanes, lane_names, sizes, metrics, annotate,
                 heatmap_node=None, heatmap_edge=None,
                 heatmap_global=False, node_span=None, edge_span=None,
                 data="none", routing="straight") -> None:
        self.stubs = stubs
        self.lanes = lanes
        self.lane_names = lane_names
        self.sizes = sizes
        self.metrics = metrics
        self.annotate = annotate
        self.heatmap_node = heatmap_node
        self.heatmap_edge = heatmap_edge
        self.heatmap_global = heatmap_global
        self.node_span = node_span
        self.edge_span = edge_span
        self.data = data
        self.routing = routing


class _Flow:
    """One BPMN sequence flow: a chain of UCM connections through routing
    points, from a real node to a real node."""

    __slots__ = ("id", "source", "target", "arcs", "bends", "condition",
                 "branch")

    def __init__(self, fid: str, arcs: List["UCM.NodeConnection"]) -> None:
        self.id = fid
        self.arcs = arcs
        self.source = arcs[0].source
        self.target = arcs[-1].target
        self.bends = [(a.target.x, a.target.y) for a in arcs[:-1]]
        self.condition = next((a.condition for a in arcs
                               if a.condition is not None), None)
        # Index of the first arc among its source's outgoing arcs: the
        # key under which the performance code filed this edge's metrics.
        try:
            self.branch = self.source.succ_connections.index(arcs[0])
        except ValueError:
            self.branch = -1


class _Lane:
    __slots__ = ("id", "name", "ref", "nodes", "children", "y0", "y1")

    def __init__(self, lid: str, name: str, ref) -> None:
        self.id = lid
        self.name = name
        self.ref = ref
        self.nodes: List["UCM.PathNode"] = []
        self.children: List["_Lane"] = []
        self.y0 = self.y1 = 0.0


class _ProcessModel:
    """Everything emitted for one UCM map."""

    def __init__(self, ucm: UCM, m: "UCM.UCMmap", index,
                 opts: _Options) -> None:
        self.ucm = ucm
        self.map = m
        self.index = index
        self.opts = opts
        self.id = f"Process_{index}"
        self.participant_id = f"Participant_{index}"
        self.node_ids: Dict["UCM.PathNode", str] = {}
        self.nodes: List["UCM.PathNode"] = []
        self.flows: List[_Flow] = []
        self.lanes: List[_Lane] = []
        self.bounds = (0.0, 0.0, 0.0, 0.0)
        #: Uniform scale applied to every UCM coordinate (1.0 unless
        #: ``sizes="canonical"`` needs room).
        self.s = 1.0
        self.sizes: Dict["UCM.PathNode", Tuple[int, int]] = {}
        self.node_heat: Dict["UCM.PathNode", float] = {}
        self.edge_heat: Dict[_Flow, float] = {}
        #: ``stubs="inline"``: the sub-process models standing in for each
        #: stub's plug-ins, one per binding, in binding order.
        self.inline: Dict["UCM.PathNode", List["_ProcessModel"]] = {}
        self._build_nodes()
        self._build_flows()
        self.sizes = {n: _shape_size(n, opts.sizes) for n in self.nodes}
        if opts.sizes == "canonical":
            self.s = self._canonical_scale()
        self._build_bounds()
        if opts.lanes == "components":
            self._build_lanes()

    # -- coordinates ------------------------------------------------------
    def xy(self, n) -> Tuple[float, float]:
        return n.x * self.s, n.y * self.s

    def pt(self, x, y) -> Tuple[float, float]:
        return x * self.s, y * self.s

    def _canonical_scale(self) -> float:
        """Smallest factor >= 1 that keeps every flow's two ends, and every
        two shapes that share a column or a row, ``CANONICAL_GAP`` apart."""
        need = 1.0
        pairs = {(f.source, f.target) for f in self.flows}
        by_x: Dict[int, List] = {}
        by_y: Dict[int, List] = {}
        for n in self.nodes:
            by_x.setdefault(n.x, []).append(n)
            by_y.setdefault(n.y, []).append(n)
        for group in list(by_x.values()) + list(by_y.values()):
            group = sorted(group, key=lambda n: (n.x, n.y))
            for a, b in zip(group, group[1:]):
                pairs.add((a, b))
        for a, b in pairs:
            if a is b:
                continue
            dx, dy = abs(a.x - b.x), abs(a.y - b.y)
            (aw, ah), (bw, bh) = self.sizes[a], self.sizes[b]
            if dx >= dy and dx > 0:
                need = max(need, ((aw + bw) / 2 + CANONICAL_GAP) / dx)
            elif dy > 0:
                need = max(need, ((ah + bh) / 2 + CANONICAL_GAP) / dy)
        return need

    # -- nodes ------------------------------------------------------------
    def _build_nodes(self) -> None:
        k = 0
        for n in self.map.nodes:
            if isinstance(n, _ROUTING_TYPES) and _is_pass_through(n):
                continue
            self.node_ids[n] = (f"{_id_prefix(n, self.opts.stubs == 'inline')}"
                                f"_{self.index}_{k}")
            self.nodes.append(n)
            k += 1

    # -- flows ------------------------------------------------------------
    def _build_flows(self) -> None:
        k = 0
        for n in self.nodes:
            for c in n.succ_connections:
                arcs = [c]
                while (isinstance(arcs[-1].target, _ROUTING_TYPES)
                       and _is_pass_through(arcs[-1].target)):
                    arcs.append(arcs[-1].target.succ_connections[0])
                self.flows.append(_Flow(f"Flow_{self.index}_{k}", arcs))
                k += 1

    def flows_from(self, n) -> List[_Flow]:
        return [f for f in self.flows if f.source is n]

    def flows_to(self, n) -> List[_Flow]:
        return [f for f in self.flows if f.target is n]

    # -- geometry ---------------------------------------------------------
    def _build_bounds(self) -> None:
        xs: List[float] = []
        ys: List[float] = []
        for n in self.nodes:
            w, h = self.sizes[n]
            x, y = self.xy(n)
            xs += [x - w / 2, x + w / 2]
            ys += [y - h / 2, y + h / 2]
        for f in self.flows:
            for bx, by in f.bends:
                x, y = self.pt(bx, by)
                xs.append(x)
                ys.append(y)
        for c in self.map.cont_refs:
            xs += [c.x * self.s, (c.x + c.width) * self.s]
            ys += [c.y * self.s, (c.y + c.height) * self.s]
        if not xs:
            xs, ys = [0.0, 100.0], [0.0, 100.0]
        x0 = min(xs) - POOL_PADDING - POOL_LABEL_STRIP
        y0 = min(ys) - POOL_PADDING
        x1 = max(xs) + POOL_PADDING
        y1 = max(ys) + POOL_PADDING
        self.bounds = (x0, y0, x1 - x0, y1 - y0)

    # -- lanes ------------------------------------------------------------
    def _build_lanes(self) -> None:
        if not self.map.cont_refs:
            return
        counter = [0]

        def new_lane(name: str, ref) -> _Lane:
            lane = _Lane(f"Lane_{self.index}_{counter[0]}", name, ref)
            counter[0] += 1
            return lane

        def lane_name(cr: "UCM.ComponentRef") -> str:
            name = cr.cont_def.name or cr.name
            if self.opts.lane_names == "with_kind":
                return f"{name} «{cr.cont_def.kind.value}»"
            return name

        def build(cr: "UCM.ComponentRef") -> _Lane:
            lane = new_lane(lane_name(cr), cr)
            own = [n for n in self.nodes if n.cont_ref is cr]
            kids = sorted(cr.children, key=lambda c: (c.y, c.x))
            if kids:
                lane.children = [build(c) for c in kids]
                if own:
                    # A lane with child lanes cannot reference flow nodes
                    # itself, so the component's directly-bound nodes get
                    # a child lane named after the component.
                    own_lane = new_lane(lane_name(cr), None)
                    own_lane.nodes = own
                    ys = [self.xy(n)[1] for n in own]
                    own_lane.y0, own_lane.y1 = min(ys) - 20, max(ys) + 20
                    lane.children.append(own_lane)
                    lane.children.sort(key=lambda l: l.y0)
            else:
                lane.nodes = own
            lane.y0 = cr.y * self.s
            lane.y1 = (cr.y + cr.height) * self.s
            return lane

        top = sorted((c for c in self.map.cont_refs if c.parent is None),
                     key=lambda c: (c.y, c.x))
        self.lanes = [build(c) for c in top]

        # Nodes bound to no component (start/end points, forks, joins) go
        # by geometry into the lane whose band holds them, or the nearest
        # one: a separate "(unassigned)" band would have to move them.
        for n in self.nodes:
            if n.cont_ref is None:
                self._leaf_lane_at(self.lanes, self.xy(n)[1]).nodes.append(n)

        # Tile: lanes share boundaries at the midpoints between bands and
        # the outermost ones reach the pool border.
        x0, y0, w, h = self.bounds
        self._tile(self.lanes, y0, y0 + h)

    def _leaf_lane_at(self, lanes: List[_Lane], y: float) -> _Lane:
        """The leaf lane whose band holds ``y``, or the nearest one."""
        inside = [l for l in lanes if l.y0 <= y <= l.y1]
        lane = inside[0] if inside else min(
            lanes, key=lambda l: min(abs(l.y0 - y), abs(l.y1 - y)))
        if lane.children:
            return self._leaf_lane_at(lane.children, y)
        return lane

    def _tile(self, lanes: List[_Lane], top: float, bottom: float) -> None:
        if not lanes:
            return
        lanes.sort(key=lambda l: l.y0)
        edges = [top]
        for a, b in zip(lanes, lanes[1:]):
            edges.append((a.y1 + b.y0) / 2)
        edges.append(bottom)
        for lane, (e0, e1) in zip(lanes, zip(edges, edges[1:])):
            lane.y0, lane.y1 = e0, e1
            self._tile(lane.children, e0, e1)

    def all_lanes(self) -> List[_Lane]:
        out: List[_Lane] = []

        def walk(ls):
            for l in ls:
                out.append(l)
                walk(l.children)
        walk(self.lanes)
        return out


def _compute_heat(model: _Model) -> None:
    """Fill each process's ``node_heat`` / ``edge_heat`` with the heat-map
    emphasis ``t`` in ``[0, 1]`` of every task and flow that has a value for
    the chosen metric -- read, attributed and normalised by the same helpers
    the PNG renderer uses, so the colours agree."""
    from pm4py_ucm.visualization.ucm.variants import classic as _c
    opts = model.opts
    node_span, edge_span = opts.node_span, opts.edge_span
    if opts.heatmap_global and node_span is None and edge_span is None:
        node_span, edge_span = _c.heat_span(
            [model.ucm], opts.heatmap_node, opts.heatmap_edge)
    for p in model.all_models():
        if opts.heatmap_node:
            vals = {}
            for n in p.nodes:
                if isinstance(n, UCM.RespRef):
                    v = _c._perf_metric_value(n, opts.heatmap_node)
                    if v is not None:
                        vals[n] = v
            p.node_heat = _c._heat_normalize(vals, span=node_span)
        if opts.heatmap_edge:
            vals = {}
            for f in p.flows:
                v = _c._edge_perf_metric_value(f.arcs[0], opts.heatmap_edge)
                if v is not None:
                    vals[f] = v
            p.edge_heat = _c._heat_normalize(vals, span=edge_span)


def _heat_attrs(t: float, metric: str, fill: bool) -> List[Tuple[str, str]]:
    """BPMN-in-Color and bioc attributes for emphasis ``t`` of ``metric``."""
    from pm4py_ucm.visualization.ucm.variants import classic as _c
    time_based = _c._metric_is_time(metric)
    stroke = _c._heat_color(t, time_based)
    attrs = [("color:border-color", stroke), ("bioc:stroke", stroke)]
    if fill:
        wash = _c._heat_fill_color(t, time_based)
        attrs += [("color:background-color", wash), ("bioc:fill", wash)]
    return attrs


def _is_pass_through(n: "UCM.PathNode") -> bool:
    return len(n.pred_connections) == 1 and len(n.succ_connections) == 1


def _id_prefix(n: "UCM.PathNode", inline: bool = False) -> str:
    if isinstance(n, UCM.StartPoint):
        return "StartEvent"
    if isinstance(n, UCM.EndPoint):
        return "EndEvent"
    if isinstance(n, UCM.RespRef):
        return "Task"
    if isinstance(n, (UCM.OrFork, UCM.OrJoin, UCM.AndFork, UCM.AndJoin)):
        return "Gateway"
    if isinstance(n, UCM.Stub):
        return "SubProcess" if (n.dynamic or inline) else "CallActivity"
    if isinstance(n, UCM.Timer):
        return "TimerEvent"
    if isinstance(n, UCM.WaitingPlace):
        return "WaitEvent"
    if isinstance(n, UCM.FailurePoint):
        return "FailureEvent"
    if isinstance(n, _ROUTING_TYPES):
        return "Gateway"      # a routing point that merges or splits
    return "Task"


class _Model:
    def __init__(self, ucm: UCM, opts: _Options) -> None:
        self.ucm = ucm
        self.opts = opts
        plugin_maps = {b.plugin for m in ucm.maps for n in m.nodes
                       if isinstance(n, UCM.Stub) for b in n.bindings}
        if opts.stubs == "inline":
            # Only root maps are processes; every plug-in is nested under
            # the stub that binds it, once per binding.
            self.processes = [_ProcessModel(ucm, m, i, opts)
                              for i, m in enumerate(ucm.maps)
                              if m not in plugin_maps]
            if not self.processes and ucm.maps:
                self.processes = [_ProcessModel(ucm, ucm.maps[0], 0, opts)]
            self.children: List[_ProcessModel] = []
            for p in list(self.processes):
                self._nest(p, set())
        else:
            self.processes = [_ProcessModel(ucm, m, i, opts)
                              for i, m in enumerate(ucm.maps)]
            self.children = []
        if opts.heatmap_node or opts.heatmap_edge:
            _compute_heat(self)
        self.by_map = {p.map: p for p in self.processes}
        roots = [p for p in self.processes if p.map not in plugin_maps]
        self.root = roots[0] if roots else (
            self.processes[0] if self.processes else None)

    def _nest(self, p: _ProcessModel, ancestry) -> None:
        """Build the inline child models of every stub on ``p``'s map."""
        ancestry = ancestry | {p.map}
        for n in p.nodes:
            if not isinstance(n, UCM.Stub):
                continue
            kids = []
            for k, b in enumerate(n.bindings):
                if b.plugin in ancestry:
                    continue      # a plug-in that contains its own caller
                child = _ProcessModel(self.ucm, b.plugin,
                                      f"{p.index}_{p.node_ids[n].rsplit('_', 1)[-1]}_{k}",
                                      self.opts)
                kids.append(child)
                self.children.append(child)
                self._nest(child, ancestry)
            p.inline[n] = kids

    def all_models(self) -> List[_ProcessModel]:
        return list(self.processes) + list(self.children)


# ---------------------------------------------------------------------------
# Emission — semantics
# ---------------------------------------------------------------------------

def _emit_definitions(w: "_XmlWriter", model: _Model) -> None:
    ucm = model.ucm
    from pm4py_ucm import __version__ as _pkg_version  # lazy: avoids a cycle
    w.open("bpmn:definitions", [
        ("xmlns:bpmn", NS_BPMN),
        ("xmlns:bpmndi", NS_BPMNDI),
        ("xmlns:dc", NS_DC),
        ("xmlns:di", NS_DI),
        ("xmlns:xsi", NS_XSI),
        (f"xmlns:{UCM_PREFIX}", NS_UCM),
        ("xmlns:color", NS_COLOR),
        ("xmlns:bioc", NS_BIOC),
        ("id", "Definitions_1"),
        ("name", ucm.name or "UCM"),
        ("targetNamespace", NS_UCM),
        ("exporter", "pm4py-ucm"),
        ("exporterVersion", _pkg_version),
    ])
    if model.root is not None:
        w.open("bpmn:collaboration", [("id", "Collaboration_1")])
        for p in model.processes:
            attrs = [("id", p.participant_id), ("processRef", p.id)]
            if p is model.root:
                attrs.insert(1, ("name", ucm.name or "UCM"))
            else:
                attrs.insert(1, ("name", p.map.name))
            w.empty("bpmn:participant", attrs)
        w.close("bpmn:collaboration")
    for p in model.processes:
        _emit_process(w, model, p)
    for p in model.processes:
        _emit_diagram(w, model, p)
        _emit_stub_diagrams(w, model, p)
    w.close("bpmn:definitions")


def _emit_stub_diagrams(w, model: _Model, p: _ProcessModel) -> None:
    """The extra diagrams a map's stubs need: a dynamic stub's own plane,
    and (``stubs="inline"``) one plane per nested plug-in sub-process."""
    for n in p.nodes:
        if not isinstance(n, UCM.Stub):
            continue
        if n.dynamic:
            _emit_dynamic_stub_diagram(w, model, p, n)
        if model.opts.stubs == "inline":
            body = _StubBody(model, p, n) if n.dynamic else None
            for k, child in enumerate(p.inline.get(n, [])):
                plane_el = body.calls[k][0] if body else p.node_ids[n]
                _emit_child_diagram(w, model, child, plane_el)
                _emit_stub_diagrams(w, model, child)


def _emit_child_diagram(w, model: _Model, child: _ProcessModel,
                        plane_el: str) -> None:
    """The plane of a collapsed sub-process standing in for a plug-in:
    the plug-in map drawn with its own coordinates and lanes."""
    w.open("bpmndi:BPMNDiagram", [("id", f"Diagram_{child.index}"),
                                  ("name", child.map.name)])
    w.open("bpmndi:BPMNPlane", [("id", f"Plane_{child.index}"),
                                ("bpmnElement", plane_el)])
    _emit_plane_content(w, model, child, with_pool=False)
    w.close("bpmndi:BPMNPlane")
    w.close("bpmndi:BPMNDiagram")


def _emit_process(w: "_XmlWriter", model: _Model, p: _ProcessModel) -> None:
    ucm = model.ucm
    attrs = [("id", p.id), ("name", p.map.name), ("isExecutable", "false")]
    w.open("bpmn:process", attrs)
    doc = []
    if p is model.root and ucm.description:
        doc.append(ucm.description)
    if p is model.root and ucm.author:
        doc.append(f"Author: {ucm.author}")
    if p.map.description:
        doc.append(p.map.description)
    for d in doc:
        w.text_element("bpmn:documentation", [], d)
    if p is model.root and model.opts.data == "objects":
        for d in _scenario_documentation(ucm):
            w.text_element("bpmn:documentation", [], d)
    _emit_extensions(w, [("map", p.map.name)], _plain_metadata(p.map))
    _emit_map_body(w, model, p)
    if p is model.root and model.opts.data == "objects":
        _emit_data_objects(w, model)
    w.close("bpmn:process")


def _emit_map_body(w, model: _Model, p: _ProcessModel) -> None:
    """Lane set, flow nodes, flows and annotations of one map — the content
    of a ``process`` or, inlined, of a ``subProcess``."""
    if p.lanes:
        _emit_lane_set(w, p, p.lanes, f"LaneSet_{p.index}")
    for n in p.nodes:
        _emit_flow_node(w, model, p, n)
    for f in p.flows:
        _emit_sequence_flow(w, p, f)
    if model.opts.annotate == "all":
        for n in p.nodes:
            text = _metadata_value(n, _PERF_KEY)
            if text:
                nid = p.node_ids[n]
                w.text_element("bpmn:textAnnotation",
                               [("id", f"Annotation_{nid}")],
                               text, inner="bpmn:text")
                w.empty("bpmn:association", [
                    ("id", f"Association_{nid}"),
                    ("sourceRef", nid),
                    ("targetRef", f"Annotation_{nid}"),
                ])


# -- scenario layer -------------------------------------------------------------

def _data_object_ids(model: _Model):
    """``[(variable, dataObject id, reference id)]`` in model order."""
    return [(v, f"DataObject_{i}", f"DataObjectRef_{i}")
            for i, v in enumerate(model.ucm.variables)]


def _emit_data_objects(w, model: _Model) -> None:
    for v, oid, rid in _data_object_ids(model):
        attrs = [("id", oid), ("name", v.name)]
        doc = [f"type: {v.type}"]
        if v.enumeration_type is not None:
            doc.append(f"enumeration {v.enumeration_type.name}: "
                       + ", ".join(v.enumeration_type.values))
        if v.description:
            doc.append(v.description)
        w.open("bpmn:dataObject", attrs)
        w.text_element("bpmn:documentation", [], "; ".join(doc))
        w.close("bpmn:dataObject")
        w.empty("bpmn:dataObjectReference",
                [("id", rid), ("name", v.name), ("dataObjectRef", oid)])


def _scenario_documentation(ucm: UCM) -> List[str]:
    out = []
    for g in ucm.scenario_groups:
        for sc in g.scenarios:
            inits = ", ".join(f"{i.variable.name} = {i.value}"
                              for i in sc.initializations)
            starts = ", ".join(sp.start_point.name or "start"
                               for sp in sc.start_points if sp.enabled)
            ends = ", ".join(
                (ep.end_point.name or "end") + (" (mandatory)" if ep.mandatory else "")
                for ep in sc.end_points if ep.enabled)
            text = f"Scenario {g.name} / {sc.name}"
            if inits:
                text += f": {inits}"
            if starts:
                text += f"; starts at {starts}"
            if ends:
                text += f"; ends at {ends}"
            if sc.description:
                text += f". {sc.description}"
            out.append(text)
    return out


def _emit_lane_set(w, p: _ProcessModel, lanes: List[_Lane], lsid: str,
                   tag: str = "bpmn:laneSet") -> None:
    w.open(tag, [("id", lsid)])
    for lane in lanes:
        w.open("bpmn:lane", [("id", lane.id), ("name", lane.name)])
        ext = []
        if lane.ref is not None:
            ext.append(("kind", lane.ref.cont_def.kind.value))
            if lane.ref.cont_def.description:
                ext.append(("description", lane.ref.cont_def.description))
        _emit_extensions(w, ext, _plain_metadata(lane.ref) if lane.ref else [])
        for n in lane.nodes:
            w.text_element("bpmn:flowNodeRef", [], p.node_ids[n])
        if lane.children:
            _emit_lane_set(w, p, lane.children, f"{lane.id}_children",
                           tag="bpmn:childLaneSet")
        w.close("bpmn:lane")
    w.close(tag)


def _emit_flow_node(w, model: _Model, p: _ProcessModel, n) -> None:
    nid = p.node_ids[n]
    name = _node_name(n)
    attrs = [("id", nid)]
    if name:
        attrs.append(("name", name))
    tag, ext, children = _classify(model, p, n, attrs)
    w.open(tag, attrs)
    if n.description:
        w.text_element("bpmn:documentation", [], n.description)
    if isinstance(n, UCM.RespRef) and n.resp_def is not None and n.resp_def.expression:
        w.text_element("bpmn:documentation", [], n.resp_def.expression)
    _emit_extensions(w, ext, _node_metrics(model, n))
    for f in p.flows_to(n):
        w.text_element("bpmn:incoming", [], f.id)
    for f in p.flows_from(n):
        w.text_element("bpmn:outgoing", [], f.id)
    for child in children:
        child(w)
    if isinstance(n, UCM.Stub):
        if n.dynamic:
            _emit_dynamic_stub_body(w, model, p, n)
        elif model.opts.stubs == "inline":
            kids = p.inline.get(n, [])
            if kids:
                _emit_map_body(w, model, kids[0])
    w.close(tag)


def _classify(model: _Model, p: _ProcessModel, n, attrs):
    """``(tag, extension attrs, deferred child emitters)`` for node ``n``."""
    ext: List[Tuple[str, str]] = []
    children = []
    if isinstance(n, UCM.StartPoint):
        if n.pre_condition is not None:
            ext += _condition_ext("precondition", n.pre_condition)
        return "bpmn:startEvent", ext, children
    if isinstance(n, UCM.EndPoint):
        if n.post_condition is not None:
            ext += _condition_ext("postcondition", n.post_condition)
        return "bpmn:endEvent", ext, children
    if isinstance(n, UCM.RespRef):
        if n.resp_def is not None:
            ext.append(("responsibility", n.resp_def.name))
            if n.resp_def.performer is not None:
                ext.append(("performer", n.resp_def.performer.name))
        return "bpmn:task", ext, children
    if isinstance(n, (UCM.OrFork, UCM.OrJoin)):
        inclusive = _metadata_value(n, "_op") == "OR"
        tag = "bpmn:inclusiveGateway" if inclusive else "bpmn:exclusiveGateway"
        attrs.append(("gatewayDirection",
                      "Diverging" if isinstance(n, UCM.OrFork) else "Converging"))
        default = _default_flow(p, n) if isinstance(n, UCM.OrFork) else None
        if default is not None:
            attrs.append(("default", default.id))
        return tag, ext, children
    if isinstance(n, (UCM.AndFork, UCM.AndJoin)):
        attrs.append(("gatewayDirection",
                      "Diverging" if isinstance(n, UCM.AndFork) else "Converging"))
        return "bpmn:parallelGateway", ext, children
    if isinstance(n, UCM.Stub):
        ext.append(("stub", "dynamic" if n.dynamic else "static"))
        if n.dynamic or model.opts.stubs == "inline":
            attrs.append(("triggeredByEvent", "false"))
            return "bpmn:subProcess", ext, children
        if n.bindings:
            target = model.by_map.get(n.bindings[0].plugin)
            if target is not None:
                attrs.append(("calledElement", target.id))
        return "bpmn:callActivity", ext, children
    if isinstance(n, UCM.Timer):
        children.append(lambda w: w.empty(
            "bpmn:timerEventDefinition", [("id", f"TimerDef_{p.node_ids[n]}")]))
        return "bpmn:intermediateCatchEvent", ext, children
    if isinstance(n, UCM.WaitingPlace):
        children.append(lambda w: w.empty(
            "bpmn:conditionalEventDefinition",
            [("id", f"CondDef_{p.node_ids[n]}")]))
        return "bpmn:intermediateCatchEvent", ext, children
    if isinstance(n, UCM.FailurePoint):
        children.append(lambda w: w.empty(
            "bpmn:errorEventDefinition", [("id", f"ErrorDef_{p.node_ids[n]}")]))
        return "bpmn:intermediateThrowEvent", ext, children
    if isinstance(n, _ROUTING_TYPES):
        # A routing point that merges or splits paths: an unnamed XOR.
        ext.append(("kind", type(n).__name__))
        attrs.append(("gatewayDirection", "Mixed"))
        return "bpmn:exclusiveGateway", ext, children
    ext.append(("kind", type(n).__name__))
    return "bpmn:task", ext, children


def _default_flow(p: _ProcessModel, fork) -> Optional[_Flow]:
    """The branch that acts as the gateway's ``default``: the single
    unconditioned one among otherwise conditioned branches. When every
    branch is unconditioned the choice is plain and nothing is default."""
    flows = p.flows_from(fork)
    cond = [f for f in flows if _has_condition(f)]
    plain = [f for f in flows if not _has_condition(f)]
    if cond and len(plain) == 1:
        return plain[0]
    return None


def _has_condition(f: _Flow) -> bool:
    c = f.condition
    return c is not None and bool(c.expression) and c.expression.strip() != "true"


def _emit_sequence_flow(w, p: _ProcessModel, f: _Flow) -> None:
    attrs = [("id", f.id), ("sourceRef", p.node_ids[f.source]),
             ("targetRef", p.node_ids[f.target])]
    label_parts = []
    if _has_condition(f) and f.condition.label:
        label_parts.append(f.condition.label)
    if p.opts.annotate in ("edges", "all") and f.branch >= 0:
        text = _metadata_value(f.source, f"{_PERF_KEY}_branch{f.branch}")
        if text:
            label_parts.append(text)
    if label_parts:
        attrs.insert(1, ("name", "\n".join(label_parts)))
    w.open("bpmn:sequenceFlow", attrs)
    if f.condition is not None and f.condition.description:
        w.text_element("bpmn:documentation", [], f.condition.description)
    ext = []
    if f.arcs[0].threshold:
        ext.append(("threshold", repr(f.arcs[0].threshold)))
    _emit_extensions(w, ext, _edge_metrics(p, f))
    is_default = (isinstance(f.source, UCM.OrFork)
                  and _default_flow(p, f.source) is f)
    if _has_condition(f) and not is_default:
        w.text_element("bpmn:conditionExpression",
                       [("xsi:type", "bpmn:tFormalExpression")],
                       f.condition.expression)
    w.close("bpmn:sequenceFlow")


def _condition_ext(kind: str, c: "UCM.Condition") -> List[Tuple[str, str]]:
    out = []
    if c.label:
        out.append((f"{kind}-label", c.label))
    if c.expression:
        out.append((f"{kind}", c.expression))
    return out


# -- dynamic stubs ------------------------------------------------------------

class _StubBody:
    """Ids and positions of the elements inside a dynamic stub's
    sub-process: ``start → inclusive fork → one callActivity per plug-in →
    inclusive join → end``, laid out left to right in its own diagram."""

    def __init__(self, model: _Model, p: _ProcessModel, stub) -> None:
        sid = p.node_ids[stub]
        self.sid = sid
        self.start = f"{sid}_start"
        self.fork = f"{sid}_fork"
        self.join = f"{sid}_join"
        self.end = f"{sid}_end"
        self.calls = []
        for i, b in enumerate(stub.bindings):
            target = model.by_map.get(b.plugin)
            self.calls.append((f"{sid}_call{i}", b,
                               target.id if target is not None else None))
        n = max(1, len(self.calls))
        self.row_h = 90
        self.call_w = 140
        self.call_h = 60
        self.height = n * self.row_h + 60
        self.width = 60 + 100 + self.call_w + 100 + 60 + 2 * EVENT_SIZE
        self.cy = self.height / 2

    def flows(self) -> List[Tuple[str, str, str, Optional["UCM.Condition"]]]:
        out = [(f"{self.sid}_f0", self.start, self.fork, None)]
        for i, (cid, b, _) in enumerate(self.calls):
            out.append((f"{self.sid}_fin{i}", self.fork, cid, b.precondition))
            out.append((f"{self.sid}_fout{i}", cid, self.join, None))
        out.append((f"{self.sid}_f1", self.join, self.end, None))
        return out

    def positions(self) -> Dict[str, Tuple[float, float, float, float]]:
        """``id → (x, y, w, h)`` with the top-left at the origin."""
        x = 20.0
        pos = {}
        pos[self.start] = (x, self.cy - EVENT_SIZE / 2, EVENT_SIZE, EVENT_SIZE)
        x += EVENT_SIZE + 60
        pos[self.fork] = (x, self.cy - GATEWAY_SIZE / 2, GATEWAY_SIZE, GATEWAY_SIZE)
        x += GATEWAY_SIZE + 60
        for i, (cid, _, _) in enumerate(self.calls):
            y = 30 + i * self.row_h
            pos[cid] = (x, y, self.call_w, self.call_h)
        x += self.call_w + 60
        pos[self.join] = (x, self.cy - GATEWAY_SIZE / 2, GATEWAY_SIZE, GATEWAY_SIZE)
        x += GATEWAY_SIZE + 60
        pos[self.end] = (x, self.cy - EVENT_SIZE / 2, EVENT_SIZE, EVENT_SIZE)
        return pos


def _emit_dynamic_stub_body(w, model: _Model, p: _ProcessModel, stub) -> None:
    body = _StubBody(model, p, stub)
    flows = body.flows()

    def io(eid):
        for fid, s, t, _ in flows:
            if t == eid:
                w.text_element("bpmn:incoming", [], fid)
        for fid, s, t, _ in flows:
            if s == eid:
                w.text_element("bpmn:outgoing", [], fid)

    w.open("bpmn:startEvent", [("id", body.start)])
    io(body.start)
    w.close("bpmn:startEvent")
    w.open("bpmn:inclusiveGateway", [("id", body.fork),
                                     ("gatewayDirection", "Diverging")])
    io(body.fork)
    w.close("bpmn:inclusiveGateway")
    kids = p.inline.get(stub, []) if model.opts.stubs == "inline" else []
    for k, (cid, b, target) in enumerate(body.calls):
        attrs = [("id", cid), ("name", b.plugin.name)]
        if model.opts.stubs == "inline":
            attrs.append(("triggeredByEvent", "false"))
            w.open("bpmn:subProcess", attrs)
            io(cid)
            if k < len(kids):
                _emit_map_body(w, model, kids[k])
            w.close("bpmn:subProcess")
            continue
        if target is not None:
            attrs.append(("calledElement", target))
        w.open("bpmn:callActivity", attrs)
        io(cid)
        w.close("bpmn:callActivity")
    w.open("bpmn:inclusiveGateway", [("id", body.join),
                                     ("gatewayDirection", "Converging")])
    io(body.join)
    w.close("bpmn:inclusiveGateway")
    w.open("bpmn:endEvent", [("id", body.end)])
    io(body.end)
    w.close("bpmn:endEvent")
    for fid, s, t, cond in flows:
        attrs = [("id", fid), ("sourceRef", s), ("targetRef", t)]
        if cond is not None and cond.label:
            attrs.insert(1, ("name", cond.label))
        if cond is not None and cond.expression and cond.expression != "true":
            w.open("bpmn:sequenceFlow", attrs)
            w.text_element("bpmn:conditionExpression",
                           [("xsi:type", "bpmn:tFormalExpression")],
                           cond.expression)
            w.close("bpmn:sequenceFlow")
        else:
            w.empty("bpmn:sequenceFlow", attrs)


def _emit_dynamic_stub_diagram(w, model: _Model, p: _ProcessModel, stub) -> None:
    body = _StubBody(model, p, stub)
    pos = body.positions()
    sid = body.sid
    w.open("bpmndi:BPMNDiagram", [("id", f"Diagram_{sid}"),
                                  ("name", _node_name(stub) or sid)])
    w.open("bpmndi:BPMNPlane", [("id", f"Plane_{sid}"), ("bpmnElement", sid)])
    call_ids = {cid for cid, _, _ in body.calls}
    for eid, (x, y, bw, bh) in pos.items():
        attrs = [("id", f"{eid}_di"), ("bpmnElement", eid)]
        if eid in call_ids and model.opts.stubs == "inline":
            attrs.append(("isExpanded", "false"))
        w.open("bpmndi:BPMNShape", attrs)
        w.empty("dc:Bounds", _bounds(x, y, bw, bh))
        w.close("bpmndi:BPMNShape")
    for fid, s, t, _ in body.flows():
        sx, sy, sw, sh = pos[s]
        tx, ty, tw, th = pos[t]
        scx, scy = sx + sw / 2, sy + sh / 2
        tcx, tcy = tx + tw / 2, ty + th / 2
        p0 = _clip_to_box(scx, scy, sw, sh, tcx, tcy)
        p1 = _clip_to_box(tcx, tcy, tw, th, scx, scy)
        w.open("bpmndi:BPMNEdge", [("id", f"{fid}_di"), ("bpmnElement", fid)])
        w.empty("di:waypoint", _point(*p0))
        w.empty("di:waypoint", _point(*p1))
        w.close("bpmndi:BPMNEdge")
    w.close("bpmndi:BPMNPlane")
    w.close("bpmndi:BPMNDiagram")


# -- extensions ---------------------------------------------------------------

def _emit_extensions(w, attrs: List[Tuple[str, str]],
                     entries: List[Tuple[str, str, str]]) -> None:
    """``<extensionElements>`` with one ``<ucm:ucm .../>`` carrying the
    attribute pairs and one ``<ucm:metric>``/``<ucm:metadata>`` per entry
    (``(tag, name, value)``). Nothing is written when both are empty."""
    if not attrs and not entries:
        return
    w.open("bpmn:extensionElements", [])
    if attrs:
        w.empty(f"{UCM_PREFIX}:ucm", attrs)
    for tag, name, value in entries:
        w.empty(f"{UCM_PREFIX}:{tag}", [("name", name), ("value", value)])
    w.close("bpmn:extensionElements")


def _plain_metadata(element) -> List[Tuple[str, str, str]]:
    """Non-performance metadata of ``element``, as ``metadata`` entries."""
    if element is None:
        return []
    return [("metadata", md.name, md.value) for md in element.metadata
            if not _is_perf_name(md.name)]


def _node_metrics(model: _Model, n) -> List[Tuple[str, str, str]]:
    out = _plain_metadata(n)
    if model.opts.metrics == "extension":
        for md in n.metadata:
            if md.name.startswith("perf_") and not md.name.startswith("perf_branch"):
                out.append(("metric", md.name[len("perf_"):], md.value))
    return out


def _edge_metrics(p: _ProcessModel, f: _Flow) -> List[Tuple[str, str, str]]:
    if p.opts.metrics != "extension" or f.branch < 0:
        return []
    prefix = f"perf_branch{f.branch}_"
    return [("metric", md.name[len(prefix):], md.value)
            for md in f.source.metadata if md.name.startswith(prefix)]


def _is_perf_name(name: str) -> bool:
    return name.startswith("perf_") or name.startswith(_PERF_KEY) or name == "_op"


def _metadata_value(element, name: str) -> Optional[str]:
    for md in element.metadata:
        if md.name == name:
            return md.value
    return None


# ---------------------------------------------------------------------------
# Emission — diagram interchange
# ---------------------------------------------------------------------------

def _emit_diagram(w, model: _Model, p: _ProcessModel) -> None:
    w.open("bpmndi:BPMNDiagram", [("id", f"Diagram_{p.index}"),
                                  ("name", p.map.name)])
    w.open("bpmndi:BPMNPlane", [("id", f"Plane_{p.index}"),
                                ("bpmnElement", "Collaboration_1"
                                 if model.root is not None else p.id)])
    _emit_plane_content(w, model, p, with_pool=model.root is not None)
    if p is model.root and model.opts.data == "objects":
        x0, y0, pw, ph = p.bounds
        dw, dh = DATA_OBJECT_SIZE
        for i, (v, oid, rid) in enumerate(_data_object_ids(model)):
            dx = x0 + POOL_LABEL_STRIP + 20 + i * 130
            dy = y0 - DATA_STRIP + 10
            w.open("bpmndi:BPMNShape", [("id", f"{rid}_di"), ("bpmnElement", rid)])
            w.empty("dc:Bounds", _bounds(dx, dy, dw, dh))
            w.open("bpmndi:BPMNLabel", [])
            w.empty("dc:Bounds", _bounds(dx - 30, dy + dh + 2, dw + 60, 14))
            w.close("bpmndi:BPMNLabel")
            w.close("bpmndi:BPMNShape")
    w.close("bpmndi:BPMNPlane")
    w.close("bpmndi:BPMNDiagram")


def _route(pts: List[Tuple[float, float]], routing: str) -> List[Tuple[float, float]]:
    """Waypoints through the UCM bend points, or with every slanted
    segment turned into an elbow (``"orthogonal"``): horizontal first on
    a forward segment, vertical first on a backward one."""
    if routing != "orthogonal":
        return pts
    out = [pts[0]]
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        if ax != bx and ay != by:
            out.append((bx, ay) if bx >= ax else (ax, by))
        out.append((bx, by))
    # Drop repeated points an elbow may have produced.
    dedup = [out[0]]
    for q in out[1:]:
        if q != dedup[-1]:
            dedup.append(q)
    return dedup


def _emit_plane_content(w, model: _Model, p: _ProcessModel,
                        with_pool: bool) -> None:
    x0, y0, pw, ph = p.bounds
    if with_pool:
        w.open("bpmndi:BPMNShape", [("id", f"{p.participant_id}_di"),
                                    ("bpmnElement", p.participant_id),
                                    ("isHorizontal", "true")])
        w.empty("dc:Bounds", _bounds(x0, y0, pw, ph))
        w.close("bpmndi:BPMNShape")
    lane_x0 = x0 + POOL_LABEL_STRIP
    lane_w = pw - POOL_LABEL_STRIP
    for lane in p.all_lanes():
        depth = _lane_depth(p, lane)
        lx = lane_x0 + depth * LANE_LABEL_STRIP
        w.open("bpmndi:BPMNShape", [("id", f"{lane.id}_di"),
                                    ("bpmnElement", lane.id),
                                    ("isHorizontal", "true")])
        w.empty("dc:Bounds", _bounds(lx, lane.y0,
                                     lane_w - depth * LANE_LABEL_STRIP,
                                     lane.y1 - lane.y0))
        w.close("bpmndi:BPMNShape")
    sizes = p.sizes
    for n in p.nodes:
        sw, sh = sizes[n]
        nx, ny = p.xy(n)
        attrs = [("id", f"{p.node_ids[n]}_di"), ("bpmnElement", p.node_ids[n])]
        if isinstance(n, UCM.Stub) and (n.dynamic or model.opts.stubs == "inline"):
            attrs.append(("isExpanded", "false"))
        if n in p.node_heat:
            attrs += _heat_attrs(p.node_heat[n], model.opts.heatmap_node, True)
        w.open("bpmndi:BPMNShape", attrs)
        w.empty("dc:Bounds", _bounds(nx - sw / 2, ny - sh / 2, sw, sh))
        if not isinstance(n, (UCM.RespRef, UCM.Stub)) and _node_name(n):
            # Events and gateways show their name beside the glyph.
            lw = max(40, min(120, 7 * len(_node_name(n))))
            w.open("bpmndi:BPMNLabel", [])
            w.empty("dc:Bounds", _bounds(nx - lw / 2, ny + sh / 2 + 4, lw, 14))
            w.close("bpmndi:BPMNLabel")
        w.close("bpmndi:BPMNShape")
    for f in p.flows:
        sw, sh = sizes[f.source]
        tw, th = sizes[f.target]
        pts = [p.xy(f.source)]
        pts += [p.pt(bx, by) for bx, by in f.bends]
        pts.append(p.xy(f.target))
        pts = _route(pts, model.opts.routing)
        pts[0] = _clip_to_box(pts[0][0], pts[0][1], sw, sh, *pts[1])
        pts[-1] = _clip_to_box(pts[-1][0], pts[-1][1], tw, th, *pts[-2])
        attrs = [("id", f"{f.id}_di"), ("bpmnElement", f.id)]
        if f in p.edge_heat:
            attrs += _heat_attrs(p.edge_heat[f], model.opts.heatmap_edge, False)
        w.open("bpmndi:BPMNEdge", attrs)
        for x, y in pts:
            w.empty("di:waypoint", _point(x, y))
        if _has_condition(f) and f.condition.label:
            lx = pts[0][0] + 8 + f.condition.delta_x
            ly = pts[0][1] - 18 + f.condition.delta_y
            lw = max(30, min(140, 7 * len(f.condition.label)))
            w.open("bpmndi:BPMNLabel", [])
            w.empty("dc:Bounds", _bounds(lx, ly, lw, 14))
            w.close("bpmndi:BPMNLabel")
        w.close("bpmndi:BPMNEdge")
    if model.opts.annotate == "all":
        for n in p.nodes:
            text = _metadata_value(n, _PERF_KEY)
            if not text:
                continue
            nid = p.node_ids[n]
            sw, sh = sizes[n]
            nx, ny = p.xy(n)
            aw = max(60, min(200, 7 * max(len(l) for l in text.splitlines())))
            ah = 14 * len(text.splitlines()) + 8
            ax, ay = nx - aw / 2, ny + sh / 2 + 6
            w.open("bpmndi:BPMNShape", [("id", f"Annotation_{nid}_di"),
                                        ("bpmnElement", f"Annotation_{nid}")])
            w.empty("dc:Bounds", _bounds(ax, ay, aw, ah))
            w.close("bpmndi:BPMNShape")
            w.open("bpmndi:BPMNEdge", [("id", f"Association_{nid}_di"),
                                       ("bpmnElement", f"Association_{nid}")])
            w.empty("di:waypoint", _point(nx, ny + sh / 2))
            w.empty("di:waypoint", _point(nx, ay))
            w.close("bpmndi:BPMNEdge")


def _lane_depth(p: _ProcessModel, lane: _Lane) -> int:
    def depth_in(lanes, d):
        for l in lanes:
            if l is lane:
                return d
            found = depth_in(l.children, d + 1)
            if found is not None:
                return found
        return None
    return depth_in(p.lanes, 0) or 0


def _num(v: float) -> str:
    r = round(v)
    return str(int(r)) if abs(v - r) < 1e-6 else f"{v:.1f}"


def _bounds(x, y, w, h):
    return [("x", _num(x)), ("y", _num(y)), ("width", _num(w)), ("height", _num(h))]


def _point(x, y):
    return [("x", _num(x)), ("y", _num(y))]


# ---------------------------------------------------------------------------
# XML writer
# ---------------------------------------------------------------------------

class _XmlWriter:
    """Minimal indenting XML writer; same spirit as the ``.jucm`` one."""

    def __init__(self, encoding: str = "UTF-8", indent: str = "  ") -> None:
        self._buf = io.StringIO()
        self._depth = 0
        self._indent = indent
        self._buf.write(f'<?xml version="1.0" encoding="{encoding}"?>\n')

    def open(self, tag: str, attrs) -> None:
        self._buf.write(self._indent * self._depth)
        self._buf.write(f"<{tag}{self._attrs(attrs)}>\n")
        self._depth += 1

    def close(self, tag: str) -> None:
        self._depth -= 1
        self._buf.write(self._indent * self._depth)
        self._buf.write(f"</{tag}>\n")

    def empty(self, tag: str, attrs) -> None:
        self._buf.write(self._indent * self._depth)
        self._buf.write(f"<{tag}{self._attrs(attrs)}/>\n")

    def text_element(self, tag: str, attrs, text: str,
                     inner: Optional[str] = None) -> None:
        """``<tag attrs>text</tag>``, or with ``inner`` set,
        ``<tag attrs><inner>text</inner></tag>``."""
        self._buf.write(self._indent * self._depth)
        if inner is None:
            self._buf.write(
                f"<{tag}{self._attrs(attrs)}>{_escape_text(text)}</{tag}>\n")
        else:
            self._buf.write(
                f"<{tag}{self._attrs(attrs)}><{inner}>{_escape_text(text)}"
                f"</{inner}></{tag}>\n")

    def text(self) -> str:
        return self._buf.getvalue()

    @staticmethod
    def _attrs(pairs) -> str:
        return "".join(f' {k}="{_escape_attr(str(v))}"' for k, v in pairs)


def _escape_attr(s: str) -> str:
    return (s.replace("&", "&amp;").replace('"', "&quot;")
             .replace("<", "&lt;").replace(">", "&gt;")
             .replace("\n", "&#10;").replace("\r", "").replace("\t", "&#9;"))


def _escape_text(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace("\r", ""))
