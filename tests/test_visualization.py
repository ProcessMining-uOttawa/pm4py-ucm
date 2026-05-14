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

    def test_ucm_style_uses_x_glyph_for_resprefs(self):
        """RespRefs in UCM style are drawn with a × glyph alongside the name.

        We use the Latin-1 multiplication sign (U+00D7) rather than the
        less-widely-available U+2715 so that the glyph renders correctly
        in every font, including the default Times-Roman that graphviz
        falls back to on some systems."""
        ucm = self._build()
        g = visualizer.apply(ucm, parameters={"style": "ucm"})
        self.assertIn("×", g.source)

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
        convention. The fill is a light pastel pink with a darker
        pink contour so decomposed activities stand out."""
        ucm = self._build_with_stub()
        g = visualizer.apply(ucm, parameters={"style": "bpmn"})
        # HTML label with bold plus marker.
        self.assertIn("<B>+</B>", g.source)
        # And the pastel-pink colour palette.
        self.assertIn("#FFD4E5", g.source)  # fill
        self.assertIn("#C04079", g.source)  # contour

    def test_ucm_stub_omits_decomposition_marker(self):
        """The UCM style relies on the diamond shape to signal
        decomposition; the BPMN-only ``+`` marker should not appear,
        and the pink palette is BPMN-only too."""
        ucm = self._build_with_stub()
        g = visualizer.apply(ucm, parameters={"style": "ucm"})
        self.assertNotIn("<B>+</B>", g.source)
        self.assertNotIn("#FFD4E5", g.source)


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
