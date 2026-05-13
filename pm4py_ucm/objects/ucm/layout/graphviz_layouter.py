"""
Graphviz-based layouter for UCM models.

The :func:`apply_graphviz_layout` function pipes the UCM through the
same graphviz machinery the PNG visualizer uses, then reads back the
computed positions and applies them to the model. The result is a
``.jucm`` file whose jUCMNav layout matches the PNG rendering exactly.

Compared to the in-house Sugiyama-style layouter in
:mod:`pm4py_ucm.objects.ucm.layout.layouter`, this approach:

* produces the same cluster nesting and edge routing as the PNG,
* respects component swim-lanes by construction (graphviz draws
  clusters as bounding boxes, just like the visualizer),
* handles arbitrary fork/join topologies without back-edge gymnastics,
* requires the graphviz binary to be available on ``PATH`` — falls back
  silently to the built-in layouter when not.

Coordinate-system note
----------------------

Graphviz emits coordinates in points (1/72 inch), with the **origin at
the bottom-left** of the canvas and the y axis pointing up. jUCMNav, by
contrast, uses **top-left origin** with y pointing down — the same as
every Eclipse-based GEF editor. The layouter flips ``y`` accordingly:
``y_jucm = y_max - y_gv``. Node coordinates are emitted as integer
pixel values rounded to the nearest unit; jUCMNav itself stores
coordinates as integers.
"""

from __future__ import annotations

import json
from typing import Dict, Optional, Tuple

from ..obj import UCM


def apply_graphviz_layout(
    ucm: UCM,
    style: str = "ucm",
    parameters: Optional[dict] = None,
) -> bool:
    """Lay out every map of ``ucm`` in place using graphviz.

    Returns ``True`` on success, ``False`` if the graphviz binary is
    unavailable or its output could not be parsed — in which case the
    caller is expected to fall back to the built-in layouter.

    The ``style`` parameter selects the visual style used to build the
    intermediate graphviz graph; for layout purposes ``"ucm"`` and
    ``"bpmn"`` produce very similar coordinates, but ``"ucm"`` is the
    natural default for a UCM file."""
    try:
        from ....visualization.ucm.variants.classic import apply as build_graph
    except ImportError:
        return False

    params = dict(parameters or {})
    params.setdefault("style", style)
    # Single-map mode — graphviz cluster names use empty prefix then.
    params.setdefault("map_index", 0)

    try:
        gviz = build_graph(ucm, parameters=params)
        json_bytes = gviz.pipe(format="json")
    except Exception:
        # graphviz binary missing, broken, or model triggers a graphviz
        # error. Either way, fall back.
        return False

    try:
        data = json.loads(json_bytes)
    except (ValueError, TypeError):
        return False

    _apply_layout_from_json(ucm, data)
    return True


def _apply_layout_from_json(ucm: UCM, data: dict) -> None:
    """Walk a graphviz JSON output and copy positions onto the model."""
    bb = _parse_bb(data.get("bb", ""))
    if bb is None:
        return
    y_max = bb[3]  # max Y in graphviz space — used for the y-axis flip

    # ID-based reverse maps. The visualizer uses ``n{id(node)}`` for path
    # nodes and ``cluster_c{id(cref)}`` for component-ref subgraphs (with
    # an empty prefix when ``map_index`` is set to a specific map).
    node_by_gv: Dict[str, "UCM.PathNode"] = {}
    cref_by_gv: Dict[str, "UCM.ComponentRef"] = {}
    for m in ucm.maps:
        for n in m.nodes:
            node_by_gv[f"n{id(n)}"] = n
        for cr in m.cont_refs:
            cref_by_gv[f"cluster_c{id(cr)}"] = cr

    for obj in data.get("objects", []):
        name = obj.get("name", "")
        if name in cref_by_gv:
            _apply_cluster_bb(cref_by_gv[name], obj.get("bb", ""), y_max)
        elif name in node_by_gv:
            _apply_node_pos(node_by_gv[name], obj.get("pos", ""), y_max)


def _apply_node_pos(node, pos_str: str, y_max: float) -> None:
    """Set ``node.x`` / ``node.y`` from a graphviz ``"x,y"`` pair."""
    parts = pos_str.split(",")
    if len(parts) < 2:
        return
    try:
        x = float(parts[0])
        y = float(parts[1])
    except ValueError:
        return
    node.x = int(round(x))
    node.y = int(round(y_max - y))  # flip y axis


def _apply_cluster_bb(cref, bb_str: str, y_max: float) -> None:
    """Set ``cref.x/y/width/height`` from a graphviz cluster bb.

    ``bb`` is ``"x_min,y_min,x_max,y_max"`` in graphviz's bottom-left
    coordinate space; the top of the cluster (visually) is at
    ``y_max_cluster`` in graphviz, which becomes ``y_max - y_max_cluster``
    in jUCMNav's top-left coordinate space.
    """
    bb = _parse_bb(bb_str)
    if bb is None:
        return
    x_min, y_min_gv, x_max, y_max_gv = bb
    # Flip y: the visual top corresponds to the largest graphviz y.
    top_jucm = y_max - y_max_gv
    bot_jucm = y_max - y_min_gv
    cref.x = int(round(x_min))
    cref.y = int(round(top_jucm))
    cref.width = int(round(x_max - x_min))
    cref.height = int(round(bot_jucm - top_jucm))


def _parse_bb(bb_str: str) -> Optional[Tuple[float, float, float, float]]:
    """Parse a graphviz bounding-box string into a 4-tuple of floats."""
    parts = bb_str.split(",")
    if len(parts) != 4:
        return None
    try:
        return (float(parts[0]), float(parts[1]),
                float(parts[2]), float(parts[3]))
    except ValueError:
        return None
