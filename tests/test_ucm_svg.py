"""Tests for inline-SVG rendering of UCM models
(:mod:`pm4py_ucm.visualization.ucm.svg`).

Covers: single-map direct render, multi-map stacking, static-stub direct
links, the dynamic-stub picker markup (targets + preconditions), per-model
id namespacing (no cross-member links), and well-formed output. Skipped
without graphviz on PATH."""
from __future__ import annotations

import shutil
from xml.dom import minidom

import pytest

pd = pytest.importorskip("pandas")

from pm4py_ucm import UCM, convert_to_ucm
from pm4py_ucm.visualization.ucm import svg as _svg

_GRAPHVIZ = shutil.which("dot") is not None
pytestmark = pytest.mark.skipif(not _GRAPHVIZ, reason="graphviz 'dot' absent")


class _T:
    """Minimal duck-typed process-tree node (operator/label/children)."""

    def __init__(self, operator=None, label=None, children=None):
        self.operator = operator
        self.label = label
        self.children = children or []


def _seq(*labels):
    return _T(operator="->", children=[_T(label=x) for x in labels])


def _decomposed_ucm():
    """A root + 3 plug-in maps, each reached by a STATIC single-binding
    stub — the classic decomposed model."""
    tree = _T(operator="->", children=[
        _seq("A1", "A2", "A3", "A4"),
        _seq("B1", "B2", "B3", "B4"),
        _seq("C1", "C2", "C3", "C4"),
    ])
    return convert_to_ucm(tree, decomposition={
        "on_root_sequence": True, "on_parallel": False, "on_loop": False,
        "max_leaves_per_map": 1000, "min_leaves_to_decompose": 3,
        "balance_ratio": 0.0,
    })


def _single_map_ucm():
    return convert_to_ucm(_seq("A", "B", "C"), decomposition=None)


def _umbrella_ucm():
    """Dynamic-stub umbrella from a tiny two-cell family."""
    from pm4py_ucm.algo.discovery.families import discover, assemble_umbrella

    rows = []
    ts = pd.Timestamp("2026-01-01")
    for i in range(6):
        rows += [
            {"case:concept:name": f"b{i}", "concept:name": a,
             "time:timestamp": ts, "case:kind": "X"}
            for a in ("Reg", "Triage", "Surgery", "Close")
        ]
    for i in range(6):
        rows += [
            {"case:concept:name": f"l{i}", "concept:name": a,
             "time:timestamp": ts, "case:kind": "Y"}
            for a in ("Reg", "Scan", "Chemo", "Close")
        ]
    df = pd.DataFrame(rows)

    def _toy(sublog, params=None):
        import pm4py
        return pm4py.discover_process_tree_inductive(sublog)

    fam = discover(df, ["kind"], min_cases=1,
                   parameters={"tree_miner": _toy,
                               "resource_attribute": False})
    return assemble_umbrella(fam)


def _wellformed(svg):
    minidom.parseString(svg)  # raises on malformed XML
    return minidom.parseString(svg)


class TestSingleMap:

    def test_plain_svg_no_chrome(self):
        svg = _svg.model_to_svg(_single_map_ucm(), "ucm")
        _wellformed(svg)
        assert svg.lstrip().startswith("<svg")
        assert "pm-map-" not in svg  # no stack wrapper
        assert "pm-stub-menu" not in svg


class TestDecomposedStaticStubs:

    def test_static_stubs_get_direct_map_links(self):
        ucm = _decomposed_ucm()
        assert len(ucm.maps) == 4
        svg = _svg.model_to_svg(ucm, "ucm")
        doc = _wellformed(svg)
        # Panels wrapped as pm-map-0..3.
        panel_ids = sorted(
            g.getAttribute("id") for g in doc.getElementsByTagName("g")
            if g.getAttribute("id").startswith("pm-map-"))
        assert panel_ids == ["pm-map-0", "pm-map-1", "pm-map-2", "pm-map-3"]
        # Every stub anchor is a direct #pm-map-N link (no picker menu).
        hrefs = [a.getAttribute("xlink:href")
                 for a in doc.getElementsByTagName("a")]
        assert hrefs, "no stub links emitted"
        assert all(h.startswith("#pm-map-") for h in hrefs)
        assert "pm-stub-menu" not in svg

    def test_navigable_false_drops_links(self):
        svg = _svg.model_to_svg(_decomposed_ucm(), "ucm", navigable=False)
        doc = _wellformed(svg)
        assert not doc.getElementsByTagName("a")


class TestDynamicStubPicker:

    def test_menu_markup_targets_and_preconditions(self):
        ucm = _umbrella_ucm()
        stubs = [n for m in ucm.maps for n in m.nodes
                 if isinstance(n, UCM.Stub)]
        assert any(s.dynamic and len(s.bindings) >= 2 for s in stubs)

        svg = _svg.model_to_svg(ucm, "ucm")
        doc = _wellformed(svg)
        menus = [g for g in doc.getElementsByTagName("g")
                 if g.getAttribute("class") == "pm-stub-menu"]
        assert menus, "no picker menu injected"
        binds = [g for g in doc.getElementsByTagName("g")
                 if g.getAttribute("class") == "pm-binding"]
        assert len(binds) >= 2
        panel_ids = {g.getAttribute("id")
                     for g in doc.getElementsByTagName("g")
                     if g.getAttribute("id").startswith("pm-map-")}
        for b in binds:
            assert b.getAttribute("data-target")[1:] in panel_ids
            assert b.getAttribute("data-label")
            assert b.getAttribute("data-cond")  # precondition present
        # The stub's own anchor points at the menu.
        hrefs = [a.getAttribute("xlink:href")
                 for a in doc.getElementsByTagName("a")]
        assert any(h.startswith("#pm-stub-menu-") for h in hrefs)

    def test_id_prefix_scopes_links_to_the_member(self):
        svg = _svg.model_to_svg(_umbrella_ucm(), "ucm", id_prefix="2-")
        doc = _wellformed(svg)
        for b in doc.getElementsByTagName("g"):
            if b.getAttribute("class") != "pm-binding":
                continue
            assert b.getAttribute("data-target").startswith("#pm-map-2-")
        for a in doc.getElementsByTagName("a"):
            href = a.getAttribute("xlink:href")
            if href.startswith("#pm-map-"):
                assert href.startswith("#pm-map-2-")
            if href.startswith("#pm-stub-menu-"):
                assert href.startswith("#pm-stub-menu-2-")
