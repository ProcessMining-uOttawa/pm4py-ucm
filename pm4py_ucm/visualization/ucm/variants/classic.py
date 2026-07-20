"""Classic graphviz-based visualization for Use Case Maps.

Renders a UCM model through one of two styles:

* ``"bpmn"`` — BPMN-friendly look for analysts coming from PM4Py's
  built-in process visualizers. Responsibilities are rounded rectangles
  ("activity" boxes); OR/AND forks/joins are diamond gateways with the
  corresponding ``X`` / ``+`` markers; the start event is a thin-line
  circle and the end event is a thick-bordered black-filled circle (the
  canonical BPMN end event).
* ``"ucm"`` — Z.151 / jUCMNav UCM look. The start point is a filled
  black circle, the end point a perpendicular bar, every responsibility
  reference is rendered with a small ``✕`` glyph plus its name on a
  second line, AND-forks/joins are thick perpendicular bars
  (synchronisation bars), and OR-forks/joins are small filled dots
  where the path simply branches. **Diamonds are reserved for stubs**
  in this style, as the UCM notation requires.

The ``style`` parameter selects between the two; both share the same
component-cluster machinery, edge routing, and label-wrapping logic.
"""

from __future__ import annotations

import tempfile
from typing import Any, Dict, Optional, Tuple

from graphviz import Digraph

from ...ucm.parameters import (
    DEFAULT_FONT,
    DEFAULT_FONT_SIZE,
    DEFAULT_RANKDIR,
    DEFAULT_BG,
    DEFAULT_SHOW_CONDITIONS,
)
from ....objects.ucm.obj import UCM
from ....objects.ucm.util.component_colors import component_color as _component_color
from ....objects.ucm.util.name_wrap import wrap_name as _wrap_name


def wrap_name(name: str, **kwargs) -> str:
    """Visualizer-side wrap that emits ``\\n`` line breaks.

    The shared :func:`pm4py_ucm.objects.ucm.util.name_wrap.wrap_name`
    helper returns ``\\r\\n``-joined output to match jUCMNav's own
    encoding (``&#xD;&#xA;`` in attribute values). Graphviz, however,
    treats ``\\r`` and ``\\n`` as *two* line breaks — which doubles the
    spacing of every wrapped label in the PNG. Normalise to ``\\n``
    here so the visualizer renders with single-spaced multi-line
    labels, leaving the exporter's wrapping unchanged.
    """
    return _wrap_name(name, **kwargs).replace("\r\n", "\n")


# ---------------------------------------------------------------------------
# Per-node-type style tables
# ---------------------------------------------------------------------------
#
# Each table maps a path-node subclass to a dict of graphviz attributes.
# The :func:`_node_style` helper composes the final attribute set by
# layering the per-type style over node-name handling rules.

_BPMN_STYLES: Dict[type, Dict[str, str]] = {
    UCM.StartPoint: dict(
        # BPMN start event: empty (white-filled) circle with a thin
        # black border. The thin border distinguishes it visually
        # from the end event, which uses the same shape with a
        # thicker stroke.
        shape="circle", style="filled", fillcolor="white",
        color="black", penwidth="1",
        fixedsize="true", width="0.30", height="0.30", label="",
    ),
    UCM.EndPoint: dict(
        # BPMN end event: empty (white-filled) circle with a thick
        # black border. Same shape as the start event but a noticeably
        # heavier stroke — the standard BPMN "Plain End Event" look.
        shape="circle", style="filled", fillcolor="white",
        color="black", penwidth="4",
        fixedsize="true", width="0.30", height="0.30", label="",
    ),
    UCM.RespRef: dict(
        shape="box", style="rounded,filled", fillcolor="#FFF7C0",
        color="#7A6200",
    ),
    UCM.OrFork: dict(  # BPMN exclusive (data-based) gateway
        shape="diamond", style="filled", fillcolor="#E0EEFF",
        color="#4A6BAA", fixedsize="true", width="0.45", height="0.45",
        label="X",
    ),
    UCM.OrJoin: dict(
        shape="diamond", style="filled", fillcolor="#E0EEFF",
        color="#4A6BAA", fixedsize="true", width="0.45", height="0.45",
        label="X",
    ),
    UCM.AndFork: dict(  # BPMN parallel gateway
        shape="diamond", style="filled", fillcolor="#D5F0D5",
        color="#3F7A3F", fixedsize="true", width="0.45", height="0.45",
        label="+",
    ),
    UCM.AndJoin: dict(
        shape="diamond", style="filled", fillcolor="#D5F0D5",
        color="#3F7A3F", fixedsize="true", width="0.45", height="0.45",
        label="+",
    ),
    UCM.WaitingPlace: dict(
        shape="circle", style="filled", fillcolor="#FFFFFF",
        color="#444444", fixedsize="true", width="0.30", height="0.30",
        label="",
    ),
    UCM.Timer: dict(
        shape="hexagon", style="filled", fillcolor="#FFE0B0",
        color="#A06000", fixedsize="true", width="0.45", height="0.30",
        label="⏲",
    ),
    UCM.Stub: dict(
        # BPMN sub-process: rounded rectangle with a decomposition
        # marker (bold ``+`` glyph) appended to the label below the
        # name. Pastel green fill with a deeper green contour
        # distinguishes sub-process activities at a glance from
        # ordinary RespRef boxes (which are pale yellow).
        shape="box", style="rounded,filled", fillcolor="#D6EFD6",
        color="#3F8A4B", penwidth="1.5", fixedsize="false",
    ),
    UCM.Connect: dict(
        shape="point", width="0.10", height="0.10", label="",
    ),
    UCM.DirectionArrow: dict(
        # In the model, DirectionArrow is a routing hint (e.g. the
        # EmptyPoint→DirectionArrow promotion the .jucm exporter
        # makes for the two empty points feeding a loop's
        # OR-join). In the PNG the path lines already carry their
        # own arrowheads at every fork/join, so an extra arrow
        # glyph here would just clutter the diagram. We render
        # DirectionArrow the same way as EmptyPoint — an
        # edge-coloured pixel — so re-imported models look like
        # the originals.
        shape="point", width="0.01", height="0.01",
        color="#444444", fillcolor="#444444", label="",
    ),
    UCM.FailurePoint: dict(
        shape="invtriangle", style="filled", fillcolor="#FFB0B0",
        color="#A00000", fixedsize="true", width="0.30", height="0.30",
        label="!",
    ),
    UCM.Anything: dict(
        shape="ellipse", style="dashed", label="?",
    ),
    UCM.EmptyPoint: dict(
        # Empty points are *routing waypoints*, not visible nodes —
        # they sit between segments of a single conceptual line.
        # Render them at the same colour as the edges and as close to
        # zero-size as graphviz allows, so they're indistinguishable
        # from the line passing through them. The connection-emission
        # code below also drops arrowheads on edges that terminate at
        # an EmptyPoint, completing the illusion of an unbroken line.
        shape="point", width="0.01", height="0.01",
        color="#444444", fillcolor="#444444", label="",
    ),
}


_UCM_STYLES: Dict[type, Dict[str, str]] = {
    UCM.StartPoint: dict(
        # UCM start: filled black circle.
        shape="circle", style="filled", fillcolor="black",
        fixedsize="true", width="0.22", height="0.22", label="",
    ),
    UCM.EndPoint: dict(
        # UCM end: perpendicular bar (a thin tall black rectangle in LR
        # layouts).
        shape="box", style="filled", fillcolor="black",
        fixedsize="true", width="0.06", height="0.40", label="",
    ),
    UCM.RespRef: dict(
        # UCM responsibility marker: a small filled black square that
        # sits *on* the path. We render the marker as the node
        # itself (rather than as a graphviz arrowhead glyph) so
        # adjacent path segments meet at the square's bbox boundary
        # with no offset — the line reads as uninterrupted through
        # the marker. The activity name floats next to the square
        # as an ``xlabel`` (graphviz positions it externally).
        # Edges in/out of the RespRef drop their arrowheads in
        # :func:`_emit_map` so the square is the only decoration
        # on the line.
        shape="square", style="filled",
        fillcolor="black", color="black",
        width="0.10", height="0.10",
        fixedsize="true", margin="0",
        label="",
    ),
    UCM.OrFork: dict(
        # OR-fork: the path simply branches; render as a small dot.
        shape="point", style="filled", fillcolor="black",
        width="0.10", height="0.10", label="",
    ),
    UCM.OrJoin: dict(
        shape="point", style="filled", fillcolor="black",
        width="0.10", height="0.10", label="",
    ),
    UCM.AndFork: dict(
        # AND-fork: synchronisation bar perpendicular to the path. In
        # rank-LR layouts "perpendicular" means tall-and-thin.
        shape="box", style="filled", fillcolor="black",
        fixedsize="true", width="0.06", height="0.40", label="",
    ),
    UCM.AndJoin: dict(
        shape="box", style="filled", fillcolor="black",
        fixedsize="true", width="0.06", height="0.40", label="",
    ),
    UCM.Stub: dict(
        # Diamonds are reserved for stubs in UCM — keep them prominent.
        shape="diamond", style="filled", fillcolor="#FFFFFF",
        color="#444444", penwidth="2.5",
        fixedsize="true", width="0.55", height="0.40",
    ),
    UCM.WaitingPlace: dict(
        # UCM waiting place: hollow circle.
        shape="circle", style="",
        color="black",
        fixedsize="true", width="0.30", height="0.30", label="",
    ),
    UCM.Timer: dict(
        # Hollow circle with a clock glyph inside.
        shape="circle", style="",
        color="black",
        fixedsize="true", width="0.35", height="0.35", label="⏲",
    ),
    UCM.Connect: dict(
        shape="point", width="0.10", height="0.10", label="",
    ),
    UCM.DirectionArrow: dict(
        # In the model, DirectionArrow is a routing hint (e.g. the
        # EmptyPoint→DirectionArrow promotion the .jucm exporter
        # makes for the two empty points feeding a loop's
        # OR-join). In the PNG the path lines already carry their
        # own arrowheads at every fork/join, so an extra arrow
        # glyph here would just clutter the diagram. We render
        # DirectionArrow the same way as EmptyPoint — an
        # edge-coloured pixel — so re-imported models look like
        # the originals.
        shape="point", width="0.01", height="0.01",
        color="#444444", fillcolor="#444444", label="",
    ),
    UCM.FailurePoint: dict(
        # Lightning-style failure marker — represented here as an
        # inverted black triangle with a "!" label.
        shape="invtriangle", style="filled", fillcolor="black",
        fontcolor="white",
        fixedsize="true", width="0.30", height="0.30", label="!",
    ),
    UCM.Anything: dict(
        shape="ellipse", style="dashed", label="?",
    ),
    UCM.EmptyPoint: dict(
        # Empty points are *routing waypoints*, not visible nodes —
        # they sit between segments of a single conceptual line.
        # Render them at the same colour as the edges and as close to
        # zero-size as graphviz allows, so they're indistinguishable
        # from the line passing through them. The connection-emission
        # code below also drops arrowheads on edges that terminate at
        # an EmptyPoint, completing the illusion of an unbroken line.
        shape="point", width="0.01", height="0.01",
        color="#444444", fillcolor="#444444", label="",
    ),
}


#: Names ``style`` parameter values accept.
STYLE_BPMN = "bpmn"
STYLE_UCM = "ucm"

_STYLE_TABLES = {
    STYLE_BPMN: _BPMN_STYLES,
    STYLE_UCM: _UCM_STYLES,
}


def _node_style(
    node: "UCM.PathNode",
    style_table: Dict[type, Dict[str, str]],
    style_name: str,
) -> Dict[str, str]:
    """Compose the graphviz attribute dict for ``node`` under the
    selected style table. The label comes from the node's name (wrapped
    on whitespace) except where the per-type style explicitly sets it."""
    style = dict(style_table.get(type(node), {"shape": "ellipse"}))

    if isinstance(node, UCM.RespRef):
        # RespRef labels need special treatment per style:
        #   - BPMN: name inside a yellow rounded box.
        #   - UCM : node is invisible; the responsibility marker is
        #     a graphviz box arrowhead on the incoming edge (see
        #     ``_emit_map``). The activity name always floats as an
        #     ``xlabel``, both for bound (inside a cluster) and
        #     unbound RespRefs — keeps the text off the path line.
        # A ``_perf`` metadata entry (performance overlay) renders as
        # a smaller gray line under the name.
        resp_name = _wrapped_resp_name(node)
        perf = _perf_text(node)
        if style_name == STYLE_UCM:
            style["label"] = ""
            # Bold responsibility name so it stands out from path-line
            # labels and edge condition glyphs around the marker square.
            style["xlabel"] = _html_bold(resp_name, sub=perf)
        elif perf:
            style["label"] = _html_label(resp_name, sub=perf, bold=False)
        else:
            style["label"] = resp_name
        return style

    if "label" not in style or style["label"] == "":
        # Glyph-less node — promote the node name into the appropriate slot.
        if isinstance(node, (UCM.StartPoint, UCM.EndPoint, UCM.EmptyPoint,
                             UCM.Connect, UCM.DirectionArrow,
                             UCM.AndFork, UCM.AndJoin,
                             UCM.OrFork, UCM.OrJoin)):
            # Tiny shapes – external (xlabel) annotation when named.
            if node.name and node.name not in (
                "EmptyPoint", "Bend", "AndFork", "AndJoin",
                "OrFork", "OrJoin", "LoopFork", "LoopJoin",
            ):
                style["xlabel"] = wrap_name(node.name)
        elif node.name:
            style["label"] = wrap_name(node.name)
    return style


def _html_escape(label: str) -> str:
    return (label
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<BR/>"))


#: Style of the performance-overlay sub-line under activity names.
_PERF_FONT = 'POINT-SIZE="9" COLOR="#555555"'


def _html_label(label: str, sub: "Optional[str]" = None,
                bold: bool = True) -> str:
    """Graphviz HTML-like label: the (wrapped) ``label``, optionally
    bold, with an optional smaller gray ``sub`` line under it (the
    performance overlay)."""
    safe = _html_escape(label)
    if bold:
        safe = f"<B>{safe}</B>"
    if sub:
        safe += f'<BR/><FONT {_PERF_FONT}>{_html_escape(sub)}</FONT>'
    return f"<{safe}>"


def _html_bold(label: str, sub: "Optional[str]" = None) -> str:
    """Wrap a wrapped (``\\n``-separated) plain label in a graphviz
    HTML-like ``<B>…</B>`` string (optionally with a performance
    sub-line)."""
    return _html_label(label, sub=sub, bold=True)


def _perf_text(element) -> "Optional[str]":
    """The element's ``_perf`` overlay metadata value, if any."""
    for md in getattr(element, "metadata", None) or []:
        if md.name == "_perf":
            return md.value
    return None


def _edge_perf_text(c: "UCM.NodeConnection") -> "Optional[str]":
    """The arc's overlay — stored on its SOURCE node under a
    branch-indexed key (jUCMNav's metamodel has no metadata on
    connections)."""
    try:
        branch = c.source.succ_connections.index(c)
    except ValueError:
        return None
    key = f"_perf_branch{branch}"
    for md in getattr(c.source, "metadata", None) or []:
        if md.name == key:
            return md.value
    return None


def _wrapped_resp_name(node: "UCM.RespRef") -> str:
    """Resolve and wrap the responsibility name behind a RespRef."""
    rd = getattr(node, "resp_def", None) or getattr(node, "respDef", None)
    raw = rd.name if (rd is not None and getattr(rd, "name", "")) else (
        node.name or "resp")
    return wrap_name(raw)


def _resp_label(node: "UCM.RespRef") -> str:
    """Kept for backward compatibility with older callers."""
    return _wrapped_resp_name(node)


# ---------------------------------------------------------------------------
# Performance heat-map (render-time emphasis)
# ---------------------------------------------------------------------------
#
# An optional Model-view overlay: colour and thicken activity contours (BPMN)
# or responsibility markers (UCM) and edges by the value of the FIRST selected
# performance metric, scaled *within each diagram* (per map — important under
# decomposition, where each sub-map reads on its own scale). A time-based metric
# uses a red ramp, any other a blue ramp; low → light+thin, high → dark+thick.
# The value comes from the ``perf_<metric>`` metadata the overlay already writes
# (so it round-trips through the .jucm), parsed back from its formatted form —
# the display rounding is immaterial to a gradient. No metric → no emphasis.

#: Activity-contour thickness ceiling, as a multiple of the base penwidth.
_HEAT_NODE_MAX = 5.0
#: Edge line-thickness ceiling per style (× the base edge penwidth). UCM paths
#: are already thick (2.6 pt), so a lower ceiling keeps hot edges legible
#: rather than slab-like; BPMN's thinner 1 pt base can take more.
_HEAT_EDGE_MAX = {STYLE_UCM: 4.0, STYLE_BPMN: 8.0}
#: Contour / marker ramp endpoints (light = lowest value, dark = highest), RGB.
_HEAT_RED = ((250, 170, 170), (127, 20, 20))    # pinkish → dark red
_HEAT_BLUE = ((150, 195, 250), (20, 55, 140))   # light blue → dark blue
#: FILL ramp for BPMN activity boxes — a paler wash than the contour ramp, so
#: the box interior reads as tinted (with value) while the border stays the
#: stronger signal. Endpoints kept lighter than the contour endpoints above.
_HEAT_RED_FILL = ((253, 236, 236), (244, 176, 176))   # near-white → pastel red
_HEAT_BLUE_FILL = ((235, 242, 253), (176, 202, 244))  # near-white → pastel blue
#: UCM responsibility marker: square half-extent (inches) at the lowest and
#: highest value. The marker grows with the value so a hot responsibility is
#: easy to spot; the small fixed 0.10 marker was hard to see.
_HEAT_UCM_MARKER = (0.16, 0.42)
#: Base edge penwidth per style — kept in sync with the ``edge_attr`` below.
_EDGE_BASE_PENWIDTH = {STYLE_UCM: 2.6, STYLE_BPMN: 1.0}

_DURATION_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0,
                   "d": 86400.0, "y": 365.25 * 86400.0}


def _metric_is_time(metric: "Optional[str]") -> bool:
    """A metric is time-based (→ red ramp) iff it is one of the ``*_time``
    aggregates; frequencies, coverage and shares (→ blue) are not."""
    return bool(metric) and metric.endswith("_time")


def _parse_perf_value(text: "Optional[str]") -> "Optional[float]":
    """A numeric value from a formatted ``perf_<metric>`` string
    (``"42"``, ``"50.0%"``, ``"15.2d"``). The display rounding is immaterial
    to a visual gradient, so it is accepted rather than stored exactly."""
    if not text:
        return None
    s = text.strip()
    if not s:
        return None
    if s.endswith("%"):
        try:
            return float(s[:-1])
        except ValueError:
            return None
    unit = _DURATION_UNITS.get(s[-1:])
    if unit is not None:
        try:
            return float(s[:-1]) * unit
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _perf_metric_value(element, metric: str) -> "Optional[float]":
    """A node's numeric value for ``metric`` from its ``perf_<metric>``
    metadata, or ``None`` if absent."""
    key = f"perf_{metric}"
    for md in getattr(element, "metadata", None) or []:
        if md.name == key:
            return _parse_perf_value(md.value)
    return None


def _edge_perf_metric_value(c: "UCM.NodeConnection",
                            metric: str) -> "Optional[float]":
    """An arc's numeric value for ``metric`` — stored on its SOURCE node
    under ``perf_branch<i>_<metric>`` (jUCMNav has no edge metadata)."""
    try:
        branch = c.source.succ_connections.index(c)
    except ValueError:
        return None
    key = f"perf_branch{branch}_{metric}"
    for md in getattr(c.source, "metadata", None) or []:
        if md.name == key:
            return _parse_perf_value(md.value)
    return None


#: Routing waypoints a path segment passes *through* — the overlay never
#: annotates an arc leaving one, so the heat-map carries the segment's value
#: across them. A real node (responsibility, stub, fork/join, start/end) ends
#: the segment, and the colour changes there.
_HEAT_ROUTING_TYPES = (UCM.EmptyPoint, UCM.DirectionArrow)


def _segment_metric_value(c: "UCM.NodeConnection",
                          metric: str) -> "Optional[float]":
    """The value of the path *segment* ``c`` belongs to.

    The overlay annotates only a segment's first arc (the one leaving a real
    node); every later arc of that segment starts at a routing waypoint (an
    empty point or direction arrow) and carries no metadata, so it would render
    black. This walks such an arc back through the routing chain to the
    segment's first arc and returns its value — so the whole run of line
    between two real nodes shares one colour and thickness, exactly as a single
    conceptual path should. Returns ``None`` when the segment has no value or
    the chain fans in ambiguously at a waypoint."""
    arc = c
    seen = set()
    while True:
        v = _edge_perf_metric_value(arc, metric)
        if v is not None:
            return v
        src = arc.source
        if not isinstance(src, _HEAT_ROUTING_TYPES) or id(arc) in seen:
            return None
        seen.add(id(arc))
        preds = getattr(src, "pred_connections", None) or []
        if len(preds) != 1:      # a real fan-in (a join) is a segment boundary
            return None
        arc = preds[0]


def _heat_normalize(values, span=None):
    """Map each raw value to ``t`` in ``[0, 1]``.

    By default the range is the values' own min/max (a **local**, per-diagram
    scale). Pass ``span=(lo, hi)`` to normalise against an external range — the
    **global** whole-model min/max — so the same value reads the same in every
    map. A degenerate range (all equal / single value / ``hi <= lo``) has no
    gradient, so everything maps to full emphasis (``1.0``)."""
    if not values:
        return {}
    lo, hi = span if span is not None else (
        min(values.values()), max(values.values()))
    if hi <= lo:
        return {k: 1.0 for k in values}
    d = hi - lo
    return {k: max(0.0, min(1.0, (v - lo) / d)) for k, v in values.items()}


def _lerp_rgb(lo, hi, t: float) -> str:
    return "#" + "".join(
        f"{round(lo[i] + (hi[i] - lo[i]) * t):02x}" for i in range(3))


def _heat_color(t: float, time_based: bool) -> str:
    """Contour / marker colour: the stronger ramp (light → dark)."""
    return _lerp_rgb(*(_HEAT_RED if time_based else _HEAT_BLUE), t)


def _heat_fill_color(t: float, time_based: bool) -> str:
    """BPMN activity-box fill: a paler wash than the contour, tinted by value."""
    return _lerp_rgb(*(_HEAT_RED_FILL if time_based else _HEAT_BLUE_FILL), t)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def apply(ucm: UCM, parameters: Optional[Dict[str, Any]] = None) -> Digraph:
    """Render a :class:`UCM` instance as a :class:`graphviz.Digraph`.

    Parameters
    ----------
    ucm
        The Use Case Map model to visualise.
    parameters
        Optional dictionary, supports:

        * ``style`` – ``"ucm"`` (default) for the Z.151 / jUCMNav UCM
          notation, or ``"bpmn"`` for a BPMN-friendly look (activity
          boxes, gateway diamonds, BPMN start/end events).
        * ``format`` – output format passed to graphviz (``"png"``, ``"svg"``,
          ``"pdf"`` …). Defaults to ``"png"``.
        * ``dpi`` – raster resolution in dots per inch for bitmap
          formats (graphviz default: 96). Layout is computed in points
          and is DPI-independent, so a higher value scales the whole
          drawing — including text — proportionally. ``192`` doubles
          the pixel size of every glyph.
        * ``rankdir`` – ``"LR"`` (default) or ``"TB"``.
        * ``bgcolor`` – background colour. Defaults to transparent.
        * ``font_size`` – integer font size for node labels.
        * ``map_index`` – which map to visualise (default ``0``). If ``None``
          all maps are emitted as graphviz subgraphs.
        * ``show_conditions`` – boolean (default ``False``). When ``True``,
          render ``[condition]`` labels on edges. Discovered UCMs carry
          synthetic conditions (``redo``, ``exit``, ``branch0``…) that
          add visual clutter without information; the default omits them
          from the PNG while leaving them intact in the underlying model
          (and the exported ``.jucm``).
    """
    parameters = parameters or {}
    style_name = parameters.get("style", STYLE_UCM)
    if style_name not in _STYLE_TABLES:
        raise ValueError(
            f"Unknown style {style_name!r}; expected one of "
            f"{sorted(_STYLE_TABLES)}"
        )
    style_table = _STYLE_TABLES[style_name]
    fmt = parameters.get("format", "png")
    rankdir = parameters.get("rankdir", DEFAULT_RANKDIR)
    bgcolor = parameters.get("bgcolor", DEFAULT_BG)
    font_size = str(parameters.get("font_size", DEFAULT_FONT_SIZE))
    show_conditions = bool(parameters.get(
        "show_conditions", DEFAULT_SHOW_CONDITIONS,
    ))
    map_index = parameters.get("map_index", 0)
    # Optional hyperlinks from stub / sub-process nodes to their plug-in
    # map, for interactive navigation in an SVG render. ``{id(stub_node):
    # (href, tooltip)}``. Absent by default, so every existing caller
    # (and the PNG path, where links have no effect) is unchanged.
    stub_links = parameters.get("stub_links") or {}
    # Optional performance heat-map (see the helpers above): each is
    # ``(metric, time_based)`` or ``None``. Drives colour + thickness on
    # activities (``heatmap_node``) and edges (``heatmap_edge``).
    heatmap_node = parameters.get("heatmap_node")
    heatmap_edge = parameters.get("heatmap_edge")
    # ``heatmap_global``: scale against the WHOLE model's min/max (so a value
    # reads the same in every map) rather than each map's own range. Computed
    # here over all maps and handed to every ``_emit_map`` as an explicit span;
    # ``None`` keeps the default local (per-map) scale. With one map the two
    # are identical.
    heatmap_global = bool(parameters.get("heatmap_global", False))
    node_span = edge_span = None
    if heatmap_global and heatmap_node:
        _nm = heatmap_node[0]
        _nv = [_perf_metric_value(n, _nm) for m in ucm.maps for n in m.nodes
               if isinstance(n, UCM.RespRef)]
        _nv = [v for v in _nv if v is not None]
        if _nv:
            node_span = (min(_nv), max(_nv))
    if heatmap_global and heatmap_edge:
        _em = heatmap_edge[0]
        _ev = [_segment_metric_value(c, _em)
               for m in ucm.maps for c in m.connections]
        _ev = [v for v in _ev if v is not None]
        if _ev:
            edge_span = (min(_ev), max(_ev))
    # ``map_name`` (or the public ``map=`` kwarg piped in via parameters)
    # selects a map by name and overrides ``map_index``. Unknown names
    # raise rather than silently falling back to the first map — better
    # to surface a typo immediately.
    map_name = parameters.get("map_name", None)
    if map_name is not None:
        matching = [i for i, m in enumerate(ucm.maps) if m.name == map_name]
        if not matching:
            raise ValueError(
                f"No map named {map_name!r} in UCM "
                f"(available: {[m.name for m in ucm.maps]})"
            )
        map_index = matching[0]
    dpi = parameters.get("dpi", None)

    filename = tempfile.NamedTemporaryFile(suffix=".gv", delete=False).name

    g = Digraph(name=ucm.name or "UCM",
                filename=filename,
                engine="dot",
                graph_attr={
                    "rankdir": rankdir,
                    "bgcolor": bgcolor,
                    # ``spline`` is graphviz's default — b-spline
                    # curves routed *around* nodes. The shorter
                    # ``curved`` alternative is smoother in isolation
                    # but mis-orients arrowheads on rank-back edges
                    # (the redo branch of a loop draws its arrow on
                    # the wrong end), so we stay with ``spline``.
                    "splines": "spline",
                    # Wider spacing gives the external (``xlabel``)
                    # names of unbound RespRefs and Stubs room to
                    # breathe instead of overlapping adjacent
                    # paths and component boxes.
                    "nodesep": "0.75",
                    "ranksep": "1.00",
                    "fontname": DEFAULT_FONT,
                    "fontsize": font_size,
                    # newrank=true lets rankdir apply uniformly across
                    # cluster subgraphs, so left-to-right flow survives
                    # the component boundaries.
                    "newrank": "true",
                    "compound": "true",
                    # External (xlabel) labels are now used for the
                    # name of unbound RespRef and Stub elements. Force
                    # them to render even when graphviz would otherwise
                    # drop them under tight spacing.
                    "forcelabels": "true",
                    # Raster resolution for bitmap outputs. Layout is
                    # in points, so this scales the whole drawing —
                    # text included — without changing its shape.
                    # Only emitted when the caller asked for it, so
                    # default output stays byte-identical.
                    **({"dpi": str(int(dpi))} if dpi else {}),
                },
                node_attr={
                    "fontname": DEFAULT_FONT,
                    "fontsize": font_size,
                },
                edge_attr={
                    "fontname": DEFAULT_FONT,
                    "fontsize": str(max(8, int(font_size) - 2)),
                    "color": "#444444",
                    # arrowsize 1.0 (graphviz default) — at the
                    # previous 0.7 the arrowheads at tiny target
                    # shapes (OR-fork / OR-join dots) were too small
                    # to read direction at a glance.
                    "arrowsize": "1.0",
                    # ``dir=forward`` is the graphviz default, but we
                    # set it explicitly so curved-spline routing of
                    # back-edges (e.g. the redo branch of a loop)
                    # always draws the arrowhead at the *target*
                    # side — even when graphviz lays the source out
                    # to the right of the target.
                    "dir": "forward",
                    # Thicker UCM paths (1.8 pt) match jUCMNav's
                    # rendering and read more clearly through the
                    # responsibility-marker squares; BPMN keeps
                    # graphviz's default 1.0 pt for the activity-box
                    # look.
                    "penwidth": (
                        "2.6" if style_name == STYLE_UCM else "1.0"
                    ),
                })
    g.format = fmt

    if map_index is None:
        # Map-name labels get a bold, slightly larger font so the
        # reader can see at a glance which cluster they're looking at.
        map_label_size = str(int(font_size) + 3)
        for i, ucm_map in enumerate(ucm.maps):
            with g.subgraph(name=f"cluster_{i}") as sub:
                sub.attr(label=ucm_map.name or f"Map{i}",
                         style="rounded", color="#888888",
                         fontname=f"{DEFAULT_FONT}-Bold",
                         fontsize=map_label_size)
                _emit_map(sub, ucm_map, style_table, style_name,
                          prefix=f"m{i}_",
                          show_conditions=show_conditions,
                          stub_links=stub_links,
                          heatmap_node=heatmap_node,
                          heatmap_edge=heatmap_edge,
                          node_span=node_span, edge_span=edge_span)
    else:
        if not ucm.maps:
            return g
        idx = max(0, min(map_index, len(ucm.maps) - 1))
        _emit_map(g, ucm.maps[idx], style_table, style_name,
                  show_conditions=show_conditions, stub_links=stub_links,
                  heatmap_node=heatmap_node, heatmap_edge=heatmap_edge,
                  node_span=node_span, edge_span=edge_span)

    return g


def _emit_map(
    g: Digraph, ucm_map: "UCM.UCMmap",
    style_table: Dict[type, Dict[str, str]],
    style_name: str,
    prefix: str = "",
    show_conditions: bool = DEFAULT_SHOW_CONDITIONS,
    stub_links: Optional[Dict[int, Any]] = None,
    heatmap_node: "Optional[Tuple[str, bool]]" = None,
    heatmap_edge: "Optional[Tuple[str, bool]]" = None,
    node_span: "Optional[Tuple[float, float]]" = None,
    edge_span: "Optional[Tuple[float, float]]" = None,
) -> None:
    """Render a single :class:`UCM.UCMmap` into the given graphviz graph.

    Path nodes bound to a :class:`UCM.ComponentRef` (via
    :attr:`UCM.PathNode.cont_ref`) are emitted inside a graphviz cluster
    subgraph labelled with the component's name. Nested ComponentRefs
    become nested clusters. Nodes with no cont_ref are emitted at the
    top level. Edges always go at the top level — graphviz handles
    cluster-spanning edges natively.
    """
    node_id = lambda n: f"{prefix}n{id(n)}"

    # Per-diagram heat-map normalisation: gather this map's activity and edge
    # values for the driving metric and scale each to [0, 1] on the map's own
    # min/max, so every sub-map reads on its own scale under decomposition.
    node_heat: Dict[int, float] = {}
    node_heat_time = False
    if heatmap_node:
        _metric, node_heat_time = heatmap_node
        _nvals = {
            id(n): _perf_metric_value(n, _metric)
            for n in ucm_map.nodes
            if isinstance(n, UCM.RespRef)
            and _perf_metric_value(n, _metric) is not None
        }
        node_heat = _heat_normalize(_nvals, span=node_span)
    edge_heat: Dict[int, float] = {}
    edge_heat_time = False
    if heatmap_edge:
        _metric, edge_heat_time = heatmap_edge
        # Each arc takes its whole segment's value (propagated across empty
        # points), so a path reads as one coloured/thick line up to the next
        # real node rather than only its first arc being emphasised.
        _evals: Dict[int, float] = {}
        for c in ucm_map.connections:
            v = _segment_metric_value(c, _metric)
            if v is not None:
                _evals[id(c)] = v
        edge_heat = _heat_normalize(_evals, span=edge_span)

    def emit_node(g_target: Digraph, node: "UCM.PathNode") -> None:
        attrs = _node_style(node, style_table, style_name)
        if isinstance(node, UCM.RespRef) and id(node) in node_heat:
            # Emphasis by value. BPMN: tint the box fill (a pale wash) and put
            # the stronger ramp on the box contour, thickened 1×→_HEAT_NODE_MAX.
            # UCM: the responsibility has no box, so the marker square itself
            # carries the colour and GROWS with the value (a small marker was
            # hard to spot), with only a light border.
            t = node_heat[id(node)]
            color = _heat_color(t, node_heat_time)
            if style_name == STYLE_UCM:
                lo, hi = _HEAT_UCM_MARKER
                sz = f"{lo + t * (hi - lo):.3f}"
                attrs["fillcolor"] = color
                attrs["color"] = color
                attrs["width"] = sz
                attrs["height"] = sz
                attrs["fixedsize"] = "true"
                attrs["penwidth"] = f"{1.0 + t * 1.5:.2f}"
            else:
                attrs["fillcolor"] = _heat_fill_color(t, node_heat_time)
                attrs["color"] = color
                attrs["penwidth"] = f"{1.0 + t * (_HEAT_NODE_MAX - 1.0):.2f}"
        if isinstance(node, UCM.Stub):
            label = wrap_name(node.name or "stub")
            if getattr(node, "is_synchronizing", False):
                label = f"≣ {label}"
            if style_name == STYLE_BPMN:
                # BPMN sub-process: a bold ``+`` glyph below the name
                # marks the activity as decomposed. We use graphviz's
                # HTML-like label so the marker can be rendered bold
                # (plain ``label="..."`` has no inline formatting).
                # ``&``/``<``/``>`` must be escaped, and newlines from
                # the wrap helper become ``<BR/>``.
                safe = (label
                        .replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;")
                        .replace("\n", "<BR/>"))
                attrs["label"] = f"<{safe}<BR/><B>+</B>>"
            elif node.cont_ref is None:
                # UCM, unbound: the diamond stays empty and the name
                # floats next to it as an external label, so the
                # path-line area around the diamond stays clean.
                attrs["label"] = ""
                attrs["xlabel"] = _html_bold(label)
            else:
                # UCM, bound to a ComponentRef: the cluster gives the
                # label space, so we keep it compact inside.
                attrs["label"] = _html_bold(label)
            # Hyperlink a stub / sub-process to its plug-in map, so an SVG
            # render is navigable: graphviz turns ``URL`` into an SVG
            # ``<a xlink:href>`` around the node. A same-document fragment
            # (``#pm-map-N``) needs no ``target`` — it navigates within the
            # SVG when opened standalone, and the embedded viewer
            # intercepts the click to pan to the panel.
            link = (stub_links or {}).get(id(node))
            if link:
                href, tooltip = link
                attrs["URL"] = href
                if tooltip:
                    attrs["tooltip"] = tooltip
        g_target.node(node_id(node), **attrs)

    # Find root ComponentRefs (no parent), and emit clusters top-down.
    roots = [cr for cr in ucm_map.cont_refs if cr.parent is None]

    def emit_cluster(parent_graph: Digraph, cr: "UCM.ComponentRef") -> None:
        cluster_name = f"cluster_{prefix}c{id(cr)}"
        with parent_graph.subgraph(name=cluster_name) as sub:
            is_actor = getattr(cr.cont_def.kind, "value", "") == "Actor"
            # Pastel colour chosen deterministically from the component
            # name. Different components get different colours, the
            # same component keeps its colour across maps and runs.
            comp_name = cr.cont_def.name or cr.name or ""
            fill, border = _component_color(comp_name)
            # UCM gets a noticeably larger, heavier component label
            # so the actor's name reads at a glance — jUCMNav uses a
            # similar visual emphasis. BPMN keeps a more restrained
            # +1pt over the node-label size.
            cluster_fontsize = str(int(DEFAULT_FONT_SIZE) + (
                3 if style_name == STYLE_UCM else 4
            ))
            sub.attr(
                label=comp_name or "Component",
                # ``labeljust=l, labelloc=t`` pins the component name to
                # the top-left of its rectangle — matches jUCMNav's
                # convention and keeps the name away from path lines
                # crossing through the middle of the box.
                labeljust="l",
                labelloc="t",
                style="rounded,bold" if is_actor else "rounded",
                color=border,
                bgcolor=fill,
                # Bold font for the component name so it pops against
                # the pastel fill and the in-cluster nodes.
                fontname=f"{DEFAULT_FONT}-Bold",
                fontsize=cluster_fontsize,
                penwidth=(
                    ("3" if is_actor else "2.2")
                    if style_name == STYLE_UCM
                    else ("2" if is_actor else "1.2")
                ),
                margin="14",
            )
            for node in ucm_map.nodes:
                if node.cont_ref is cr:
                    emit_node(sub, node)
            for child in cr.children:
                emit_cluster(sub, child)

    for root in roots:
        emit_cluster(g, root)

    for node in ucm_map.nodes:
        if node.cont_ref is None:
            emit_node(g, node)

    for c in ucm_map.connections:
        edge_attrs: Dict[str, str] = {}
        # Branch conditions ("redo", "exit", "branch0", …) emitted by the
        # converter carry no domain information for the reader, so they
        # are suppressed in the PNG by default. The exporter still
        # writes them to the .jucm — that's where the user can rename or
        # delete them. Set ``show_conditions=True`` in the visualizer
        # parameters to render them anyway.
        if show_conditions and c.condition:
            edge_attrs["label"] = f"[{c.condition}]"
        if c.name:
            existing = edge_attrs.get("label", "")
            edge_attrs["label"] = (existing + " " + c.name).strip()
        # Performance overlay on the edge — small and gray so path
        # structure stays dominant.
        perf = _edge_perf_text(c)
        if perf:
            existing = edge_attrs.get("label", "")
            edge_attrs["label"] = (
                existing + "\n" + perf if existing else perf
            )
            edge_attrs.setdefault("fontsize", "9")
            edge_attrs.setdefault("fontcolor", "#555555")
        # An EmptyPoint is a routing waypoint, not a visible node — the
        # edge should pass *through* it as if uninterrupted. Drop the
        # arrowhead when terminating at an EmptyPoint so the segment
        # before the empty point ends as a plain line, matching the
        # segment after it.
        #
        # In the UCM style, the RespRef is itself a small filled
        # black square that visually marks the responsibility on
        # the path. Edges in/out of it drop their arrowheads so the
        # square is the only decoration — the path then reads as
        # uninterrupted, meeting at the square's bbox boundary on
        # each side. The BPMN style keeps the standard forward
        # arrowhead since its activities are boxed flow
        # destinations.
        if isinstance(c.target, (UCM.EmptyPoint, UCM.DirectionArrow)):
            edge_attrs["arrowhead"] = "none"
        elif (style_name == STYLE_UCM
                and isinstance(c.target, UCM.RespRef)):
            edge_attrs["arrowhead"] = "none"
        # Heat-map emphasis: colour the arc on the ramp and scale its penwidth
        # from the style's base up to _HEAT_EDGE_MAX×. Only arcs that carry the
        # metric are touched; the rest keep the default edge colour/weight.
        if id(c) in edge_heat:
            t = edge_heat[id(c)]
            base = _EDGE_BASE_PENWIDTH.get(style_name, 1.0)
            emax = _HEAT_EDGE_MAX.get(style_name, 8.0)
            edge_attrs["color"] = _heat_color(t, edge_heat_time)
            edge_attrs["penwidth"] = (
                f"{base * (1.0 + t * (emax - 1.0)):.2f}")
        g.edge(node_id(c.source), node_id(c.target), **edge_attrs)
