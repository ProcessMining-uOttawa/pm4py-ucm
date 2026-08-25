"""Inline-SVG rendering of UCM models.

SVG is the vector counterpart to :mod:`pm4py_ucm.visualization.ucm.stacked`
(which composites PNGs): it zooms and pans crisply, its text stays
selectable, and it is a fraction of a raster's size. This module is the
single source of truth for turning a :class:`UCM` into one self-contained
SVG string — used by the web app's on-screen viewer, the family grid, and
the interactive HTML reports, so they cannot drift.

A single-map model renders directly through the standard
:mod:`~pm4py_ucm.visualization.ucm.variants.classic` pipeline. A
decomposed (multi-map) model renders each of its maps to SVG and stacks
them into one document — nested ``<svg>`` panels, each with its own
viewport so the graphviz layout is preserved exactly — under centred
title strips with separators, mirroring the PNG composite.

Stubs become navigable: a stub with a single plug-in gets a
``#pm-map-N`` link (a viewer pans to that panel); a DYNAMIC stub with
several plug-ins gets a ``#pm-stub-menu-N`` link plus an inert, hidden
``<g>`` carrying each plug-in's target panel, name and precondition, so a
viewer can offer a picker. ``id_prefix`` namespaces every panel / menu
id, so several models stacked together (a family grid) keep each stub
link inside its own member — a stub can never resolve across members.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape, quoteattr

from ...objects.ucm.obj import UCM
from . import visualizer as _visualizer
from .variants import classic as _classic

# Chrome for the stacked SVG — mirrors the PNG composite in ``stacked.py``
# (a bold, centred map-name title strip above each panel and a thin
# separator between panels) so the SVG and PNG of a decomposed model read
# the same.
_TITLE_PAD_TOP = 18.0
_TITLE_FONT = 18.0
_TITLE_PAD_BOTTOM = 10.0
_SEP_MARGIN = 12.0
_SEP_THICKNESS = 2.0
_TITLE_COLOR = "#202020"
_SEP_COLOR = "#a0a0a0"


def svg_body(svg: str) -> str:
    """Strip graphviz's XML declaration / DOCTYPE, leaving the ``<svg>``."""
    i = svg.find("<svg")
    return svg[i:] if i >= 0 else svg


def svg_dimensions(svg: str) -> Tuple[float, float]:
    """``(width, height)`` in points from an ``<svg>``'s ``width``/
    ``height`` attributes, falling back to the ``viewBox`` extents."""
    m = re.search(
        r'<svg[^>]*\bwidth="([\d.]+)pt"[^>]*\bheight="([\d.]+)pt"', svg)
    if m:
        return float(m.group(1)), float(m.group(2))
    vb = re.search(r'viewBox="[\d.\-]+ [\d.\-]+ ([\d.]+) ([\d.]+)"', svg)
    if vb:
        return float(vb.group(1)), float(vb.group(2))
    return 100.0, 100.0


def svg_inner(svg: str) -> str:
    """The markup between the outer ``<svg …>`` and ``</svg>``."""
    return svg[svg.index(">", svg.index("<svg")) + 1: svg.rindex("</svg>")]


def stack_svgs(panels: List[Tuple[str, str]], *, id_prefix: str = "",
               wrap_anchor: bool = True) -> str:
    """Stack per-panel SVGs into one document with named title strips and
    separators, matching the PNG composite.

    ``panels`` is ``[(name, svg), …]``. Each panel is nested as an
    ``<svg>`` at an increasing y offset — its own viewport and coordinate
    system, so the graphviz layout inside is preserved exactly — under a
    centred title, with a separator between adjacent panels.

    ``wrap_anchor`` wraps each panel in ``<g id="pm-map-{id_prefix}{i}">``
    so a stub's ``#pm-map-…`` hyperlink lands on it. ``id_prefix``
    namespaces those ids per model, so stacking several models keeps every
    stub link inside its own member. A cell-level outer stack (which is
    not itself a link target) passes ``wrap_anchor=False``.
    """
    title_strip = _TITLE_PAD_TOP + _TITLE_FONT + _TITLE_PAD_BOTTOM
    sep_total = _SEP_MARGIN * 2 + _SEP_THICKNESS

    dims = []
    for name, svg in panels:
        w, h = svg_dimensions(svg)
        dims.append((name, w, h, svg_inner(svg)))

    total_w = max(w for _, w, _, _ in dims)
    total_h = sum(title_strip + h for _, _, h, _ in dims) \
        + sep_total * (len(dims) - 1)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{total_w:.0f}pt" height="{total_h:.0f}pt" '
        f'viewBox="0 0 {total_w:.2f} {total_h:.2f}">',
        # White backdrop so the chrome (title strips, gaps) matches the
        # PNG rather than showing the page through.
        f'<rect x="0" y="0" width="{total_w:.2f}" height="{total_h:.2f}" '
        f'fill="#ffffff"/>',
    ]
    y = 0.0
    for i, (name, w, h, inner) in enumerate(dims):
        out.append(
            f'<text x="{total_w / 2:.2f}" '
            f'y="{y + _TITLE_PAD_TOP + _TITLE_FONT * 0.8:.2f}" '
            f'text-anchor="middle" font-family="Arial, sans-serif" '
            f'font-weight="bold" font-size="{_TITLE_FONT:.0f}" '
            f'fill="{_TITLE_COLOR}">{escape(name)}</text>'
        )
        y += title_strip
        x_off = (total_w - w) / 2  # centre a narrower panel, like the PNG
        panel = (
            f'<svg x="{x_off:.2f}" y="{y:.2f}" width="{w:.2f}" '
            f'height="{h:.2f}" viewBox="0 0 {w:.2f} {h:.2f}">{inner}</svg>'
        )
        if wrap_anchor:
            out.append(f'<g id="pm-map-{id_prefix}{i}">{panel}</g>')
        else:
            out.append(panel)
        y += h
        if i != len(dims) - 1:
            y += _SEP_MARGIN
            out.append(
                f'<line x1="{_SEP_MARGIN:.2f}" y1="{y:.2f}" '
                f'x2="{total_w - _SEP_MARGIN:.2f}" y2="{y:.2f}" '
                f'stroke="{_SEP_COLOR}" '
                f'stroke-width="{_SEP_THICKNESS:.0f}"/>'
            )
            y += _SEP_THICKNESS + _SEP_MARGIN
    out.append("</svg>")
    return "\n".join(out)


_TITLE_RE = re.compile(r"(<title>)(.*?)(</title>)", re.S)


def _tooltip_names(ucm: "UCM", by_element_id: Dict[int, str]) -> Dict[str, str]:
    """Re-key hover text from ``id(element)`` to the graphviz object name.

    The classic renderer names a node ``n<id(node)>`` and an edge
    ``n<id(source)>&#45;&gt;n<id(target)>``, and graphviz writes that name
    into the SVG's ``<title>``. Matching on it is how a tooltip finds its
    element after rendering.
    """
    out: Dict[str, str] = {}
    for m in ucm.maps:
        for node in getattr(m, "nodes", []):
            text = by_element_id.get(id(node))
            if text:
                out[f"n{id(node)}"] = text
        for c in getattr(m, "connections", []):
            text = by_element_id.get(id(c))
            if text:
                out[f"n{id(c.source)}&#45;&gt;n{id(c.target)}"] = text
    return out


def _inject_tooltips(svg: str, tips: Dict[str, str]) -> str:
    """Replace graphviz's ``<title>`` text with the caller's hover text.

    graphviz only honours its own ``tooltip`` attribute when the element
    also carries a ``URL`` — it becomes the generated anchor's
    ``xlink:title`` — so an element without a link gets no hover text that
    way. What it *does* always emit is a ``<title>`` holding the internal
    object name, which browsers show on hover: unhelpful at best, since
    that name embeds a memory address. Rewriting it is both how the
    tooltip arrives and a small improvement on what was there.

    Only titles with an entry in ``tips`` are touched; everything else is
    left exactly as graphviz wrote it.
    """
    if not tips:
        return svg

    def repl(mo):
        text = tips.get(mo.group(2))
        if text is None:
            return mo.group(0)
        return f"{mo.group(1)}{escape(text)}{mo.group(3)}"

    return _TITLE_RE.sub(repl, svg)


def _stub_cond_text(cond) -> str:
    """A dynamic-stub binding's guard, as short display text: the logical
    expression unless it is the default ``true``, else the label."""
    if cond is None:
        return ""
    expr = (getattr(cond, "expression", "") or "").strip()
    if expr and expr.lower() != "true":
        return expr
    return (getattr(cond, "label", "") or "").strip()


def _inject_stub_menus(svg: str, menus: List[Any]) -> str:
    """Embed dynamic-stub picker data as an inert, hidden ``<g>`` inside
    the SVG so the artifact stays self-contained.

    ``menus`` is ``[(menu_id, stub_name, [(target_href, label, cond)]), …]``.
    Each becomes ``<g id="{menu_id}" class="pm-stub-menu">`` with one
    ``<g class="pm-binding" data-target=… data-label=… data-cond=…>`` per
    plug-in. A viewer reads these on a stub click to build the picker; a
    standalone SVG just carries hidden metadata (dynamic stubs cannot pick
    without a viewer's JS). ``quoteattr`` escapes so a stray quote or
    ``<`` in a name or guard cannot break the markup.
    """
    if not menus:
        return svg
    parts = ['<g class="pm-stub-menus" style="display:none">']
    for menu_id, stub_name, entries in menus:
        parts.append(f'<g id="{menu_id}" class="pm-stub-menu" '
                     f'data-stub={quoteattr(stub_name)}>')
        for href, label, cond in entries:
            parts.append(
                f'<g class="pm-binding" data-target={quoteattr(href)} '
                f'data-label={quoteattr(label)} '
                f'data-cond={quoteattr(cond)}></g>')
        parts.append('</g>')
    parts.append('</g>')
    markup = "".join(parts)
    idx = svg.rindex("</svg>")
    return svg[:idx] + markup + svg[idx:]


def model_to_svg(ucm: "UCM", style: str = "ucm", *,
                 id_prefix: str = "", navigable: bool = True,
                 heatmap: bool = False,
                 node_metric: Optional[str] = None,
                 edge_metric: Optional[str] = None,
                 heatmap_global: bool = False,
                 node_span: Optional[Tuple[float, float]] = None,
                 edge_span: Optional[Tuple[float, float]] = None,
                 coverage: Optional[dict] = None) -> str:
    """One model as a single inline SVG string.

    A single-map model renders directly. A decomposed (multi-map) model
    renders each map to SVG and stacks them; when ``navigable`` (the
    default) each stub / sub-process is hyperlinked to its plug-in:

    * a stub with a single plug-in gets a direct ``#pm-map-…`` link;
    * a DYNAMIC stub with several plug-ins gets a ``#pm-stub-menu-…``
      link plus the hidden picker data (see :func:`_inject_stub_menus`).

    ``id_prefix`` namespaces every panel / menu id, so a member rendered
    inside a family grid only ever links within itself. ``style`` is the
    classic renderer's ``"ucm"`` / ``"bpmn"`` notation.
    """
    style = (style or "ucm").lower()
    # Performance heat-map (optional): pass the driving metric + its time-ness
    # to the classic renderer, which colours/thickens per diagram. ``None``
    # when off or no metric, so every existing caller renders unchanged.
    heat_node = ((node_metric, node_metric.endswith("_time"))
                 if heatmap and node_metric else None)
    heat_edge = ((edge_metric, edge_metric.endswith("_time"))
                 if heatmap and edge_metric else None)
    # ``node_span`` / ``edge_span`` are an optional explicit scale (a
    # family-wide range); ``None`` lets ``heatmap_global`` (or the per-map
    # default) decide. Threaded to every map of a decomposed model so each
    # panel shares the imposed scale.
    # Scenario coverage highlight: ``{"colors": …, "tooltips": …}`` keyed by
    # ``id(element)``, as built by
    # :func:`pm4py_ucm.algo.scenario_coverage.coverage_render` /
    # :func:`~pm4py_ucm.algo.scenario_coverage.comparison_render`. Supplying
    # it turns the heat-map off in the renderer: the two compete for the
    # same colour channel, so only one can be read at a time.
    _cov = {
        "coverage_colors": (coverage or {}).get("colors") or {},
        "coverage_tooltips": (coverage or {}).get("tooltips") or {},
    }
    _heat = {"heatmap_node": heat_node, "heatmap_edge": heat_edge,
             "heatmap_global": bool(heatmap_global),
             "node_span": node_span if heatmap else None,
             "edge_span": edge_span if heatmap else None,
             **_cov}
    _tips = _tooltip_names(ucm, _cov["coverage_tooltips"])
    if len(ucm.maps) <= 1:
        gviz = _visualizer.apply(ucm, parameters={"style": style, **_heat})
        return _inject_tooltips(
            svg_body(gviz.pipe(format="svg").decode("utf-8")), _tips)

    stub_links: Dict[int, Any] = {}
    menus: List[Any] = []
    if navigable:
        map_index = {id(m): i for i, m in enumerate(ucm.maps)}
        #: plug-in map id → index of the parent map that binds it (via a stub).
        map_parent: Dict[int, int] = {}
        sid = 0
        for parent_i, m in enumerate(ucm.maps):
            for node in m.nodes:
                if not isinstance(node, UCM.Stub):
                    continue
                entries = []
                for b in node.bindings:
                    pi = map_index.get(id(b.plugin))
                    if pi is None:
                        continue
                    entries.append((
                        f"#pm-map-{id_prefix}{pi}",
                        b.plugin.name or f"Map{pi}",
                        _stub_cond_text(getattr(b, "precondition", None)),
                    ))
                    # Remember the first parent that binds each plug-in, for the
                    # reverse (back-to-parent) link below.
                    map_parent.setdefault(id(b.plugin), parent_i)
                if not entries:
                    continue
                if len(entries) == 1:
                    href, label, _ = entries[0]
                    stub_links[id(node)] = (href, f"Go to sub-map: {label}")
                else:
                    menu_id = f"pm-stub-menu-{id_prefix}{sid}"
                    sid += 1
                    stub_links[id(node)] = (
                        f"#{menu_id}",
                        f"Choose sub-map for {node.name or 'stub'} "
                        f"({len(entries)} plug-ins)",
                    )
                    menus.append((menu_id, node.name or "stub", entries))
        # Reverse navigation: a plug-in map's end point(s) link UP to the
        # parent map's panel (the stub can't be its own back-target). The
        # classic renderer applies the link to any node in ``stub_links``.
        for m in ucm.maps:
            parent_i = map_parent.get(id(m))
            if parent_i is None:
                continue
            parent_name = ucm.maps[parent_i].name or f"Map{parent_i}"
            for node in m.nodes:
                if isinstance(node, UCM.EndPoint):
                    stub_links.setdefault(id(node), (
                        f"#pm-map-{id_prefix}{parent_i}",
                        f"Back to parent map: {parent_name}"))

    panels = []
    for idx, ucm_map in enumerate(ucm.maps):
        gviz = _classic.apply(
            ucm, parameters={"style": style, "map_index": idx,
                             "format": "svg", "stub_links": stub_links,
                             **_heat})
        name = ucm_map.name or f"Map{idx}"
        panels.append((name, svg_body(gviz.pipe(format="svg").decode("utf-8"))))
    return _inject_tooltips(
        _inject_stub_menus(stack_svgs(panels, id_prefix=id_prefix), menus),
        _tips)
