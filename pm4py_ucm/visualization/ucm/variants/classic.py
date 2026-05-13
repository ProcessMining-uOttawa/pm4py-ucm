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
)
from ....objects.ucm.obj import UCM
from ....objects.ucm.util.name_wrap import wrap_name


# ---------------------------------------------------------------------------
# Per-node-type style tables
# ---------------------------------------------------------------------------
#
# Each table maps a path-node subclass to a dict of graphviz attributes.
# The :func:`_node_style` helper composes the final attribute set by
# layering the per-type style over node-name handling rules.

_BPMN_STYLES: Dict[type, Dict[str, str]] = {
    UCM.StartPoint: dict(
        shape="circle", style="filled", fillcolor="black",
        fixedsize="true", width="0.25", height="0.25", label="",
    ),
    UCM.EndPoint: dict(
        # BPMN end event: thick-border filled black circle (the
        # standard, distinguishing it from the thinner-border start).
        shape="circle", style="filled", fillcolor="black",
        color="black", penwidth="3",
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
        # BPMN sub-process marker
        shape="box", style="rounded,filled", fillcolor="#FFFFFF",
        color="#444444", fixedsize="false",
    ),
    UCM.Connect: dict(
        shape="point", width="0.10", height="0.10", label="",
    ),
    UCM.DirectionArrow: dict(
        shape="rarrow", style="filled", fillcolor="#888888",
        fixedsize="true", width="0.40", height="0.20", label="",
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
        shape="point", width="0.06", height="0.06", label="",
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
        # UCM responsibility reference: an "X" glyph with the name on a
        # second line. ``shape="plaintext"`` removes the border so the
        # rendering looks like the path runs through a labelled X.
        shape="plaintext",
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
        shape="rarrow", style="filled", fillcolor="#888888",
        fixedsize="true", width="0.40", height="0.20", label="",
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
        shape="point", width="0.06", height="0.06", label="",
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
        #   - BPMN: name inside a yellow rounded box
        #   - UCM : a small "×" glyph above the name, no border. We use
        #     the Latin-1 MULTIPLICATION SIGN (U+00D7) rather than the
        #     less-widely-available MULTIPLICATION X (U+2715), since the
        #     former is present in virtually every Latin font (Helvetica,
        #     Arial, Times, Liberation, …) while the latter renders as
        #     a tofu box on systems with limited Unicode coverage.
        resp_name = _wrapped_resp_name(node)
        if style_name == STYLE_UCM:
            style["label"] = f"×\n{resp_name}"
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
    map_index = parameters.get("map_index", 0)

    filename = tempfile.NamedTemporaryFile(suffix=".gv", delete=False).name

    g = Digraph(name=ucm.name or "UCM",
                filename=filename,
                engine="dot",
                graph_attr={
                    "rankdir": rankdir,
                    "bgcolor": bgcolor,
                    "splines": "spline",
                    "nodesep": "0.40",
                    "ranksep": "0.55",
                    "fontname": DEFAULT_FONT,
                    "fontsize": font_size,
                    # newrank=true lets rankdir apply uniformly across
                    # cluster subgraphs, so left-to-right flow survives
                    # the component boundaries.
                    "newrank": "true",
                    "compound": "true",
                },
                node_attr={
                    "fontname": DEFAULT_FONT,
                    "fontsize": font_size,
                },
                edge_attr={
                    "fontname": DEFAULT_FONT,
                    "fontsize": str(max(8, int(font_size) - 2)),
                    "color": "#444444",
                    "arrowsize": "0.7",
                })
    g.format = fmt

    if map_index is None:
        for i, ucm_map in enumerate(ucm.maps):
            with g.subgraph(name=f"cluster_{i}") as sub:
                sub.attr(label=ucm_map.name or f"Map{i}",
                         style="rounded", color="#888888")
                _emit_map(sub, ucm_map, style_table, style_name,
                          prefix=f"m{i}_")
    else:
        if not ucm.maps:
            return g
        idx = max(0, min(map_index, len(ucm.maps) - 1))
        _emit_map(g, ucm.maps[idx], style_table, style_name)

    return g


def _emit_map(
    g: Digraph, ucm_map: "UCM.UCMmap",
    style_table: Dict[type, Dict[str, str]],
    style_name: str,
    prefix: str = "",
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
            attrs["label"] = label
        g_target.node(node_id(node), **attrs)

    # Find root ComponentRefs (no parent), and emit clusters top-down.
    roots = [cr for cr in ucm_map.cont_refs if cr.parent is None]

    def emit_cluster(parent_graph: Digraph, cr: "UCM.ComponentRef") -> None:
        cluster_name = f"cluster_{prefix}c{id(cr)}"
        with parent_graph.subgraph(name=cluster_name) as sub:
            is_actor = getattr(cr.cont_def.kind, "value", "") == "Actor"
            sub.attr(
                label=cr.cont_def.name or cr.name or "Component",
                style="rounded" if not is_actor else "rounded,bold",
                color="#3F7A3F" if is_actor else "#666666",
                bgcolor="#FAFFFA" if is_actor else "#F4F4F8",
                fontname=DEFAULT_FONT,
                fontsize=str(int(DEFAULT_FONT_SIZE) + 1),
                penwidth="1.5" if is_actor else "1.0",
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
        if c.condition:
            edge_attrs["label"] = f"[{c.condition}]"
        if c.name:
            existing = edge_attrs.get("label", "")
            edge_attrs["label"] = (existing + " " + c.name).strip()
        g.edge(node_id(c.source), node_id(c.target), **edge_attrs)
