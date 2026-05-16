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
from typing import Any, Dict, Optional

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
        # name. Light pastel pink fill with a deeper pink contour
        # distinguishes sub-process activities at a glance from
        # ordinary RespRef boxes (which are pale yellow).
        shape="box", style="rounded,filled", fillcolor="#FFD4E5",
        color="#C04079", penwidth="1.5", fixedsize="false",
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
        # UCM responsibility reference: the node itself is invisible
        # (single transparent point) — the responsibility marker is
        # drawn as a small black box arrowhead on the incoming edge
        # (see ``_emit_map`` below). This puts the marker *on* the
        # path line rather than as a separate × glyph floating
        # above the line. The activity name floats as an
        # ``xlabel`` (graphviz positions it externally).
        shape="point", width="0.01", height="0.01",
        color="transparent", fillcolor="transparent", label="",
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
        color="#444444", fixedsize="true", width="0.55", height="0.40",
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
        resp_name = _wrapped_resp_name(node)
        if style_name == STYLE_UCM:
            style["label"] = ""
            style["xlabel"] = resp_name
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
                          show_conditions=show_conditions)
    else:
        if not ucm.maps:
            return g
        idx = max(0, min(map_index, len(ucm.maps) - 1))
        _emit_map(g, ucm.maps[idx], style_table, style_name,
                  show_conditions=show_conditions)

    return g


def _emit_map(
    g: Digraph, ucm_map: "UCM.UCMmap",
    style_table: Dict[type, Dict[str, str]],
    style_name: str,
    prefix: str = "",
    show_conditions: bool = DEFAULT_SHOW_CONDITIONS,
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

    def emit_node(g_target: Digraph, node: "UCM.PathNode") -> None:
        attrs = _node_style(node, style_table, style_name)
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
                attrs["xlabel"] = label
            else:
                # UCM, bound to a ComponentRef: the cluster gives the
                # label space, so we keep it compact inside.
                attrs["label"] = label
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
                fontsize=str(int(DEFAULT_FONT_SIZE) + 1),
                penwidth="2" if is_actor else "1.2",
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
        # An EmptyPoint is a routing waypoint, not a visible node — the
        # edge should pass *through* it as if uninterrupted. Drop the
        # arrowhead when terminating at an EmptyPoint so the segment
        # before the empty point ends as a plain line, matching the
        # segment after it.
        #
        # In the UCM style, edges terminating at a RespRef draw a
        # small black ``box`` arrowhead — the responsibility marker
        # is the arrowhead itself, sitting *on* the path line.
        # Edges leaving a RespRef draw no tail decoration; the path
        # continues from the box. The BPMN style keeps the standard
        # forward arrowhead since its activities are boxed
        # destinations.
        if isinstance(c.target, (UCM.EmptyPoint, UCM.DirectionArrow)):
            edge_attrs["arrowhead"] = "none"
        elif (style_name == STYLE_UCM
                and isinstance(c.target, UCM.RespRef)):
            edge_attrs["arrowhead"] = "box"
            edge_attrs["arrowsize"] = "0.55"
            edge_attrs["color"] = "black"
        g.edge(node_id(c.source), node_id(c.target), **edge_attrs)
