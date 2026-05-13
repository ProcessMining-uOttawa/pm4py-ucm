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

    def test_edge_conditions_rendered(self):
        ucm = self._build()
        g = visualizer.apply(ucm)
        self.assertIn("[x]", g.source)
        self.assertIn("[y]", g.source)


if __name__ == "__main__":
    unittest.main()
