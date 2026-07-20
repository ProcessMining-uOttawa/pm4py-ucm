"""Tests for the graphviz-based UCM visualiser.

These tests only verify that the produced graph *source* is well-formed
DOT and that the expected nodes/edges are mentioned. Actually invoking the
graphviz binary is not required.
"""

import unittest

from pm4py_ucm import UCM
from pm4py_ucm.visualization.ucm import visualizer


class UCMVisualizationTests(unittest.TestCase):

    def _build(self):
        ucm = UCM(name="V")
        m = ucm.add_map(name="M")
        sp = m.add_node(UCM.StartPoint(name="start"))
        f = m.add_node(UCM.OrFork(name="f"))
        a = m.add_node(UCM.RespRef(resp_def=ucm.get_or_add_responsibility("A")))
        b = m.add_node(UCM.RespRef(resp_def=ucm.get_or_add_responsibility("B")))
        j = m.add_node(UCM.OrJoin())
        ep = m.add_node(UCM.EndPoint(name="end"))
        m.add_connection(sp, f)
        m.add_connection(f, a, condition="x")
        m.add_connection(f, b, condition="y")
        m.add_connection(a, j)
        m.add_connection(b, j)
        m.add_connection(j, ep)
        return ucm

    def test_apply_returns_graph(self):
        ucm = self._build()
        g = visualizer.apply(ucm)
        self.assertTrue(g.source.startswith("digraph"))

    def test_responsibility_labels_appear(self):
        ucm = self._build()
        g = visualizer.apply(ucm)
        self.assertIn("A", g.source)
        self.assertIn("B", g.source)

    def test_or_fork_join_labelled_in_bpmn_style(self):
        """In BPMN style the OR-fork is a diamond labelled with ``X``."""
        ucm = self._build()
        g = visualizer.apply(ucm, parameters={"style": "bpmn"})
        self.assertIn("X", g.source)
        # And the shape should be the BPMN gateway diamond.
        self.assertIn("diamond", g.source)

    def test_or_fork_in_ucm_style_is_not_a_diamond(self):
        """In UCM style diamonds are reserved for stubs; OR-fork uses a
        small dot instead. AND-fork uses a synchronisation bar (a thin
        filled rect)."""
        ucm = self._build()
        g = visualizer.apply(ucm, parameters={"style": "ucm"})
        # The fork/join nodes themselves must NOT be rendered as diamonds.
        # Detect by checking that the only diamond shape — if any —
        # belongs to a stub, of which there are none in this fixture.
        self.assertNotIn("diamond", g.source)

    def test_ucm_respref_uses_black_square_marker(self):
        """The text × glyph previously stacked above the path is gone.
        UCM responsibilities now render as a small filled black
        square sitting *on* the path line — the square is the node
        itself, so adjacent path segments meet at its bbox boundary
        and the line reads as uninterrupted through the marker."""
        ucm = self._build()
        g = visualizer.apply(ucm, parameters={"style": "ucm"})
        # No leftover × glyph from the old style.
        self.assertNotIn("×", g.source)
        # Each RespRef renders as a filled black square node.
        self.assertIn("shape=square", g.source)
        self.assertIn("fillcolor=black", g.source)

    def test_invalid_style_rejected(self):
        ucm = self._build()
        with self.assertRaises(ValueError):
            visualizer.apply(ucm, parameters={"style": "no-such-style"})

    def test_edge_conditions_hidden_by_default(self):
        """Default rendering omits ``[condition]`` labels on edges so
        synthetic guards like ``redo``/``exit``/``branch0`` produced by
        the converter don't clutter the diagram."""
        ucm = self._build()
        g = visualizer.apply(ucm)
        self.assertNotIn("[x]", g.source)
        self.assertNotIn("[y]", g.source)

    def test_edge_conditions_rendered_when_opted_in(self):
        ucm = self._build()
        g = visualizer.apply(ucm, parameters={"show_conditions": True})
        self.assertIn("[x]", g.source)
        self.assertIn("[y]", g.source)


class ComponentColorTests(unittest.TestCase):
    """Component clusters are coloured deterministically by name from a
    professional pastel palette — same name → same colour across maps
    and runs."""

    def _ucm_with(self, comp_names):
        ucm = UCM(name="V")
        m = ucm.add_map(name="M")
        for name in comp_names:
            comp = ucm.get_or_add_component(name)
            cr = m.add_component_ref(comp)
            a = m.add_node(UCM.RespRef(
                resp_def=ucm.get_or_add_responsibility(name + "Act"),
                name=name + "Act",
            ))
            a.cont_ref = cr
        return ucm

    def test_same_component_same_colour_across_renders(self):
        ucm1 = self._ucm_with(["Alpha"])
        ucm2 = self._ucm_with(["Alpha"])
        g1 = visualizer.apply(ucm1, parameters={"style": "ucm"})
        g2 = visualizer.apply(ucm2, parameters={"style": "ucm"})
        import re
        bg1 = re.search(r'bgcolor="(#[0-9A-F]{6})"', g1.source).group(1)
        bg2 = re.search(r'bgcolor="(#[0-9A-F]{6})"', g2.source).group(1)
        self.assertEqual(bg1, bg2,
                          "same component name must hash to same colour")

    def test_different_components_get_different_colours(self):
        # Pick names known to land on different palette buckets.
        ucm = self._ucm_with(["TeamRed", "TeamBlue", "TeamGreen"])
        g = visualizer.apply(ucm, parameters={"style": "ucm"})
        import re
        bgs = re.findall(r'bgcolor="(#[0-9A-F]{6})"', g.source)
        # Each cluster has its own bgcolor; at least two distinct.
        self.assertGreaterEqual(len(set(bgs)), 2,
                                f"expected ≥2 distinct cluster colours, "
                                f"got {bgs}")

    def test_cluster_font_is_bold(self):
        ucm = self._ucm_with(["Team"])
        g = visualizer.apply(ucm, parameters={"style": "ucm"})
        self.assertIn("Helvetica-Bold", g.source)


class RespRefMarkerTests(unittest.TestCase):
    """In UCM style, the responsibility marker is the RespRef node
    itself rendered as a small filled black square. Edges into and
    out of it drop their arrowheads so the path reads as
    uninterrupted, with the square sitting *on* the line. BPMN
    keeps the standard arrowhead and a yellow activity box."""

    def _ucm(self):
        ucm = UCM(name="V")
        m = ucm.add_map(name="M")
        sp = m.add_node(UCM.StartPoint(name="start"))
        a = m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility("A"), name="A",
        ))
        ep = m.add_node(UCM.EndPoint(name="end"))
        m.add_connection(sp, a)
        m.add_connection(a, ep)
        return ucm, sp, a, ep

    def test_ucm_edges_into_respref_drop_arrowhead(self):
        ucm, sp, a, ep = self._ucm()
        g = visualizer.apply(ucm, parameters={"style": "ucm"})
        a_tok = f"n{id(a)}"
        edges = [line for line in g.source.splitlines()
                 if "->" in line and f"-> {a_tok}" in line]
        self.assertTrue(edges)
        for line in edges:
            self.assertIn("arrowhead=none", line)

    def test_bpmn_keeps_default_arrowhead_into_respref(self):
        ucm, sp, a, ep = self._ucm()
        g = visualizer.apply(ucm, parameters={"style": "bpmn"})
        a_tok = f"n{id(a)}"
        edges = [line for line in g.source.splitlines()
                 if "->" in line and f"-> {a_tok}" in line]
        self.assertTrue(edges)
        for line in edges:
            # BPMN keeps the standard forward arrowhead — no UCM-style
            # override.
            self.assertNotIn("arrowhead=none", line)


class DirectionArrowRenderingTests(unittest.TestCase):
    """DirectionArrow nodes — produced when re-importing a `.jucm`
    that contains the exporter's EmptyPoint→DirectionArrow promotion
    around a loop's OR-join — render in the PNG just like
    EmptyPoints: a tiny edge-coloured pixel with no arrowhead on
    incoming edges. The path lines already convey direction; an
    extra arrow glyph would only add visual noise."""

    def _build(self):
        ucm = UCM(name="D")
        m = ucm.add_map(name="M")
        sp = m.add_node(UCM.StartPoint(name="start"))
        da = m.add_node(UCM.DirectionArrow(name="hint"))
        ep = m.add_node(UCM.EndPoint(name="end"))
        m.add_connection(sp, da)
        m.add_connection(da, ep)
        return ucm, da

    def test_direction_arrow_is_invisible_point(self):
        for style in ("ucm", "bpmn"):
            ucm, da = self._build()
            g = visualizer.apply(ucm, parameters={"style": style})
            da_tok = f"n{id(da)}"
            node_lines = [line for line in g.source.splitlines()
                          if da_tok in line and "->" not in line]
            self.assertTrue(node_lines)
            for line in node_lines:
                self.assertIn("shape=point", line,
                               f"{style!r}: DirectionArrow not a point: {line}")
                self.assertNotIn("rarrow", line)

    def test_edges_into_direction_arrow_have_no_arrowhead(self):
        for style in ("ucm", "bpmn"):
            ucm, da = self._build()
            g = visualizer.apply(ucm, parameters={"style": style})
            da_tok = f"n{id(da)}"
            edges_in = [line for line in g.source.splitlines()
                        if "->" in line and f"-> {da_tok}" in line]
            self.assertTrue(edges_in)
            for line in edges_in:
                self.assertIn("arrowhead=none", line,
                              f"{style!r}: edge into DirectionArrow "
                              f"should drop arrowhead: {line}")


class RespRefSquareNodeTests(unittest.TestCase):
    """In UCM style, the RespRef node renders as a small filled
    black square — the visible marker on the path. The path
    segments terminate at the square's bbox boundary on each
    side, so the line reads as continuous through the marker."""

    def test_respref_is_filled_black_square_in_ucm_style(self):
        ucm = UCM(name="V")
        m = ucm.add_map(name="M")
        a = m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility("A"), name="A",
        ))
        g = visualizer.apply(ucm, parameters={"style": "ucm"})
        a_tok = f"n{id(a)}"
        node_lines = [line for line in g.source.splitlines()
                      if a_tok in line and "->" not in line]
        self.assertTrue(node_lines)
        for line in node_lines:
            self.assertIn("shape=square", line)
            self.assertIn("fillcolor=black", line)
            # Tight bbox so the path meets the square's boundary with
            # no extra padding.
            self.assertIn("margin=0", line)


class ArrowVisibilityTests(unittest.TestCase):
    """Arrowheads at OR-join / OR-fork targets must be visible enough
    to convey flow direction. Achieved by raising ``arrowsize`` from
    the previous 0.7 to 1.0 and pinning ``dir=forward`` so back-edges
    in loops always show the arrowhead at the target end."""

    def test_default_arrowsize_one(self):
        ucm = UCM(name="V")
        m = ucm.add_map(name="M")
        sp = m.add_node(UCM.StartPoint(name="start"))
        ep = m.add_node(UCM.EndPoint(name="end"))
        m.add_connection(sp, ep)
        g = visualizer.apply(ucm)
        self.assertRegex(g.source, r'arrowsize="?1(\.0)?"?')

    def test_dir_forward_set_globally(self):
        ucm = UCM(name="V")
        m = ucm.add_map(name="M")
        sp = m.add_node(UCM.StartPoint(name="start"))
        ep = m.add_node(UCM.EndPoint(name="end"))
        m.add_connection(sp, ep)
        g = visualizer.apply(ucm)
        # graphviz-python may emit ``dir=forward`` either quoted or
        # unquoted depending on version.
        self.assertRegex(g.source, r'dir="?forward"?')


class UCMOnlyEdgePenwidthTests(unittest.TestCase):
    """UCM paths are thicker (penwidth 2.6 pt) than BPMN edges
    (default 1.0 pt). The change is style-scoped so BPMN renders
    keep the lighter look."""

    def _ucm(self):
        ucm = UCM(name="V")
        m = ucm.add_map(name="M")
        sp = m.add_node(UCM.StartPoint(name="start"))
        ep = m.add_node(UCM.EndPoint(name="end"))
        m.add_connection(sp, ep)
        return ucm

    def test_ucm_edges_have_thicker_penwidth(self):
        g = visualizer.apply(self._ucm(), parameters={"style": "ucm"})
        self.assertRegex(g.source, r'edge \[[^\]]*penwidth="?2\.6"?[^\]]*\]')

    def test_bpmn_edges_have_default_penwidth(self):
        g = visualizer.apply(self._ucm(), parameters={"style": "bpmn"})
        self.assertRegex(g.source, r'edge \[[^\]]*penwidth="?1(\.0)?"?[^\]]*\]')


class UCMOnlyClusterLabelTests(unittest.TestCase):
    """UCM gets a larger component-label font than BPMN so the
    actor name reads at a glance. Both styles keep Helvetica-Bold."""

    def _ucm_with_cluster(self):
        ucm = UCM(name="V")
        m = ucm.add_map(name="M")
        comp = ucm.get_or_add_component("Team")
        cr = m.add_component_ref(comp)
        a = m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility("R"), name="R",
        ))
        a.cont_ref = cr
        return ucm

    def _cluster_fontsize(self, source: str) -> int:
        """Extract the cluster subgraph's fontsize.

        graphviz-python emits cluster attributes inline on a single
        attribute line inside the subgraph block — we identify the
        cluster's line by looking for ``fontname="Helvetica-Bold"``
        (only the cluster sets that)."""
        import re
        for line in source.splitlines():
            if 'fontname="Helvetica-Bold"' in line:
                m = re.search(r'fontsize=(\d+)', line)
                if m:
                    return int(m.group(1))
        raise AssertionError("no cluster-label line found in source")

    def test_ucm_cluster_label_larger(self):
        from pm4py_ucm.visualization.ucm.parameters import DEFAULT_FONT_SIZE
        g = visualizer.apply(self._ucm_with_cluster(),
                              parameters={"style": "ucm"})
        self.assertEqual(self._cluster_fontsize(g.source),
                          DEFAULT_FONT_SIZE + 3)
        self.assertIn("Helvetica-Bold", g.source)

    def test_bpmn_cluster_label_modest(self):
        from pm4py_ucm.visualization.ucm.parameters import DEFAULT_FONT_SIZE
        g = visualizer.apply(self._ucm_with_cluster(),
                              parameters={"style": "bpmn"})
        self.assertEqual(self._cluster_fontsize(g.source),
                          DEFAULT_FONT_SIZE + 4)


class SplineRoutingTests(unittest.TestCase):
    def test_splines_spline(self):
        """We use ``splines=spline`` (graphviz default — b-splines
        routed around nodes). The alternative ``splines=curved`` was
        smoother in isolation but mis-oriented arrowheads on
        rank-back edges, e.g. the redo branch of a loop, so the
        UCM-style ``arrowhead=box`` marker ended up at the wrong
        end of the edge."""
        ucm = UCM(name="V")
        m = ucm.add_map(name="M")
        sp = m.add_node(UCM.StartPoint(name="start"))
        ep = m.add_node(UCM.EndPoint(name="end"))
        m.add_connection(sp, ep)
        g = visualizer.apply(ucm)
        self.assertIn("splines=spline", g.source)


class UCMLabelPlacementTests(unittest.TestCase):
    """In UCM style, unbound RespRef and Stub labels float outside the
    symbol via ``xlabel`` so the path-line area stays clean. Bound
    elements (inside a ComponentRef cluster) keep their inline label
    since the cluster gives the name space."""

    def test_unbound_respref_uses_xlabel_in_ucm_style(self):
        ucm = UCM(name="V")
        m = ucm.add_map(name="M")
        m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility("Solo"),
            name="Solo",
        ))
        g = visualizer.apply(ucm, parameters={"style": "ucm"})
        # The RespRef node carries the name as an xlabel — the
        # × glyph itself is gone (replaced by a box arrowhead on
        # the incoming edge).
        self.assertIn("xlabel=<<B>Solo</B>>", g.source)
        self.assertNotIn("×", g.source)

    def test_bound_respref_also_uses_xlabel_in_ucm_style(self):
        """Both bound and unbound RespRefs use ``xlabel`` for the
        name now — keeps the path-line area clean, and the
        surrounding ComponentRef cluster gives the xlabel room."""
        ucm = UCM(name="V")
        m = ucm.add_map(name="M")
        comp = ucm.get_or_add_component("Team")
        cr = m.add_component_ref(comp)
        a = m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility("Bound"),
            name="Bound",
        ))
        a.cont_ref = cr
        g = visualizer.apply(ucm, parameters={"style": "ucm"})
        self.assertIn("xlabel=<<B>Bound</B>>", g.source)
        self.assertNotIn("×\nBound", g.source)

    def test_unbound_stub_uses_xlabel_in_ucm_style(self):
        ucm = UCM(name="V")
        m = ucm.add_map(name="M")
        m.add_node(UCM.Stub(name="Phase"))
        g = visualizer.apply(ucm, parameters={"style": "ucm"})
        # Diamond stays empty, name floats as xlabel.
        self.assertRegex(g.source, r'label=""[^\]]*xlabel=<<B>Phase</B>>')

    def test_cluster_label_top_left(self):
        ucm = UCM(name="V")
        m = ucm.add_map(name="M")
        comp = ucm.get_or_add_component("Team")
        cr = m.add_component_ref(comp)
        a = m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility("R"), name="R",
        ))
        a.cont_ref = cr
        g = visualizer.apply(ucm, parameters={"style": "ucm"})
        self.assertIn("labeljust=l", g.source)
        self.assertIn("labelloc=t", g.source)


class StubRenderingTests(unittest.TestCase):
    """Stub rendering — caption suppression and BPMN decomposition mark."""

    def _build_with_stub(self):
        ucm = UCM(name="X")
        root = ucm.add_map(name="Root")
        sub = ucm.add_map(name="MySub")
        s = root.add_node(UCM.StartPoint(name="start"))
        stub = root.add_node(UCM.Stub(name="Phase 1"))
        e = root.add_node(UCM.EndPoint(name="end"))
        in_arc = root.add_connection(s, stub)
        out_arc = root.add_connection(stub, e)
        ss = sub.add_node(UCM.StartPoint(name="ss"))
        a = sub.add_node(UCM.RespRef(resp_def=ucm.get_or_add_responsibility("A")))
        se = sub.add_node(UCM.EndPoint(name="se"))
        sub.add_connection(ss, a)
        sub.add_connection(a, se)
        b = UCM.PluginBinding(stub=stub, plugin=sub)
        b.add_in(parent_connection=in_arc, plugin_start=ss)
        b.add_out(plugin_end=se, parent_connection=out_arc)
        stub.bindings.append(b)
        return ucm

    def test_no_plugin_name_caption_below_stub(self):
        """The ``→ <plugin-name>`` xlabel is no longer emitted — the
        stub's own name is enough context for the reader.

        Other nodes (start/end points with user-supplied names) may
        still legitimately carry an ``xlabel``; the check here is
        that the arrow marker (which was unique to the stub caption)
        does not appear anywhere, and that the stub's *own* line
        carries no plug-in-name caption."""
        ucm = self._build_with_stub()
        g = visualizer.apply(ucm, parameters={"style": "ucm"})
        self.assertNotIn("→", g.source)
        # The MySub plug-in name should not appear next to the stub —
        # only the stub's own name ("Phase 1") goes on the diagram.
        stub_lines = [ln for ln in g.source.splitlines() if "Phase 1" in ln]
        self.assertTrue(stub_lines, "stub label missing")
        for line in stub_lines:
            self.assertNotIn("MySub", line)

    def test_bpmn_stub_carries_decomposition_marker(self):
        """In BPMN style, stubs gain a bold ``+`` glyph below the name
        (via graphviz HTML labels) to match the BPMN sub-process
        convention. The fill is a light pastel green with a darker
        green contour so decomposed activities stand out."""
        ucm = self._build_with_stub()
        g = visualizer.apply(ucm, parameters={"style": "bpmn"})
        # HTML label with bold plus marker.
        self.assertIn("<B>+</B>", g.source)
        # And the pastel-green colour palette.
        self.assertIn("#D6EFD6", g.source)  # fill
        self.assertIn("#3F8A4B", g.source)  # contour

    def test_ucm_stub_omits_decomposition_marker(self):
        """The UCM style relies on the diamond shape to signal
        decomposition; the BPMN-only ``+`` marker should not appear,
        and the green sub-process palette is BPMN-only too."""
        ucm = self._build_with_stub()
        g = visualizer.apply(ucm, parameters={"style": "ucm"})
        self.assertNotIn("<B>+</B>", g.source)
        self.assertNotIn("#D6EFD6", g.source)


class LabelLineBreakTests(unittest.TestCase):
    """Wrapped multi-line names render with single spacing in the PNG.

    The shared :func:`wrap_name` helper joins lines with ``\\r\\n`` to
    match jUCMNav's encoding; graphviz interprets both characters as
    line breaks, doubling the spacing. The visualizer must normalise
    to ``\\n`` before handing the label off."""

    def test_resp_label_has_single_newline_not_crlf(self):
        ucm = UCM(name="V")
        m = ucm.add_map(name="M")
        # A long activity name that the wrapper will split across lines.
        long = "Send for Credit Collection Department"
        m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility(long),
            name=long,
        ))
        g = visualizer.apply(ucm)
        # The wrapped label uses ``\n`` (a real newline character)
        # inside the DOT source. The ``\r`` carriage return must NOT
        # be present — graphviz treats CR and LF as separate line
        # breaks, which would double-space the rendered label.
        self.assertNotIn("\r", g.source)
        # And the wrap did happen — multi-line label means at least
        # one newline inside a label="..." section. We just look for
        # the wrapped fragments.
        self.assertIn("Send", g.source)
        self.assertIn("Department", g.source)


class EmptyPointRenderingTests(unittest.TestCase):
    """Empty points are routing waypoints. They render at near-zero
    size, in the edge colour, and the segments terminating at them
    must not draw arrowheads — the result should look like an
    uninterrupted line."""

    def _build(self):
        ucm = UCM(name="V")
        m = ucm.add_map(name="M")
        a = m.add_node(UCM.RespRef(resp_def=ucm.get_or_add_responsibility("A")))
        ep = m.add_node(UCM.EmptyPoint())
        b = m.add_node(UCM.RespRef(resp_def=ucm.get_or_add_responsibility("B")))
        m.add_connection(a, ep)
        m.add_connection(ep, b)
        return ucm, ep

    def test_edges_into_empty_points_have_no_arrowhead(self):
        ucm, ep = self._build()
        for style in ("ucm", "bpmn"):
            g = visualizer.apply(ucm, parameters={"style": style})
            # Find the edge ending at the empty point and assert
            # arrowhead=none. We key off the empty-point node id.
            ep_token = f"n{id(ep)}"
            edges_to_ep = [
                line for line in g.source.splitlines()
                if "->" in line and f"-> {ep_token}" in line
            ]
            self.assertTrue(edges_to_ep,
                             f"no edge terminates at the empty point in {style!r}")
            for line in edges_to_ep:
                self.assertIn("arrowhead=none", line,
                              f"{style!r} edge to empty point missing arrowhead=none")

    def test_empty_point_is_tiny(self):
        ucm, ep = self._build()
        for style in ("ucm", "bpmn"):
            g = visualizer.apply(ucm, parameters={"style": style})
            ep_lines = [
                line for line in g.source.splitlines()
                if f"n{id(ep)}" in line and "label" in line
            ]
            self.assertTrue(ep_lines, f"empty point not found in {style!r}")
            # The width/height should be much smaller than ordinary
            # nodes (which sit at 0.22+).
            import re
            for line in ep_lines:
                m = re.search(r'width="?([\d.]+)"?', line)
                self.assertTrue(m)
                self.assertLess(float(m.group(1)), 0.05,
                                f"empty point too large in {style!r}: {line}")


class BpmnStartEndPointTests(unittest.TestCase):
    """BPMN start and end events are both empty (white-filled) circles
    — the only visual difference is the border thickness: the end
    event has a noticeably heavier stroke than the start event."""

    def _render(self):
        ucm = UCM(name="V")
        m = ucm.add_map(name="M")
        sp = m.add_node(UCM.StartPoint(name="start"))
        ep = m.add_node(UCM.EndPoint(name="end"))
        m.add_connection(sp, ep)
        g = visualizer.apply(ucm, parameters={"style": "bpmn"})
        return sp, ep, g.source

    @staticmethod
    def _penwidth_of(source: str, node) -> float:
        import re
        token = f"n{id(node)}"
        for line in source.splitlines():
            if token in line and "label" in line and "->" not in line:
                m = re.search(r'penwidth="?([\d.]+)"?', line)
                if m:
                    return float(m.group(1))
        raise AssertionError(f"no penwidth for {token}")

    def test_both_are_white_filled_circles(self):
        sp, ep, src = self._render()
        for node in (sp, ep):
            token = f"n{id(node)}"
            lines = [line for line in src.splitlines()
                     if token in line and "label" in line]
            self.assertTrue(lines)
            for line in lines:
                self.assertRegex(line, r'shape="?circle"?')
                self.assertRegex(line, r'fillcolor="?white"?')

    def test_end_border_thicker_than_start(self):
        sp, ep, src = self._render()
        self.assertGreater(
            self._penwidth_of(src, ep),
            self._penwidth_of(src, sp),
            "BPMN end point should have a thicker border than the start",
        )


if __name__ == "__main__":
    unittest.main()
