"""Tests for the graphviz-based layouter.

The graphviz layouter pipes the UCM through the same machinery the
PNG visualizer uses, then reads back the computed coordinates and
applies them to the model. The resulting ``.jucm`` lays out exactly
like the PNG.

The graphviz binary is required for these tests to run; they skip
silently when it isn't available (the package itself falls back to
the built-in layouter in that case, so end users see no failure)."""

import shutil
import unittest
from types import SimpleNamespace as NS

import pm4py_ucm
from pm4py_ucm import UCM
from pm4py_ucm.objects.ucm.layout.graphviz_layouter import (
    apply_graphviz_layout,
)


def _has_graphviz() -> bool:
    """Whether the ``dot`` binary is on ``PATH``."""
    return shutil.which("dot") is not None


leaf = lambda l: NS(operator=None, children=[], label=l)
op = lambda o, *kids: NS(operator=NS(value=o), children=list(kids), label=None)


@unittest.skipUnless(_has_graphviz(), "graphviz `dot` not available")
class GraphvizLayouterTests(unittest.TestCase):
    """End-to-end tests for ``apply_graphviz_layout``."""

    def _build_with_components(self):
        tree = op("->",
                  leaf("A"),
                  op("X", leaf("B"), leaf("C")),
                  leaf("D"))
        return pm4py_ucm.convert_to_ucm(tree, parameters={
            "performers": {"A": "T1", "B": "T1", "C": "T2", "D": "T2"},
            "routing_empty_points": False,
        })

    def test_returns_true_when_graphviz_available(self):
        ucm = self._build_with_components()
        self.assertTrue(apply_graphviz_layout(ucm))

    def test_assigns_nonzero_coordinates_to_every_node(self):
        """Every path node must get a real position from graphviz —
        leaving any node at (0, 0) is the symptom of an unresolved
        ID-mapping bug between the visualizer and the layouter."""
        ucm = self._build_with_components()
        apply_graphviz_layout(ucm)
        for n in ucm.maps[0].nodes:
            self.assertFalse(n.x == 0 and n.y == 0,
                             f"{n.effective_name} not positioned")

    def test_component_rectangles_get_proper_dimensions(self):
        """ComponentRefs need a non-default width/height after layout —
        otherwise the rectangle in jUCMNav appears at zero size."""
        ucm = self._build_with_components()
        apply_graphviz_layout(ucm)
        for cr in ucm.maps[0].cont_refs:
            self.assertGreater(cr.width, 0)
            self.assertGreater(cr.height, 0)

    def test_left_to_right_flow_preserved(self):
        """A pure sequence ``A → B → C`` should have strictly
        increasing X coordinates (the natural LR direction)."""
        tree = op("->", leaf("A"), leaf("B"), leaf("C"))
        ucm = pm4py_ucm.convert_to_ucm(tree, parameters={
            "routing_empty_points": False,
        })
        apply_graphviz_layout(ucm)
        nodes = {n.effective_name: n for n in ucm.maps[0].nodes
                 if n.effective_name in {"A", "B", "C", "start", "end"}}
        self.assertLess(nodes["start"].x, nodes["A"].x)
        self.assertLess(nodes["A"].x, nodes["B"].x)
        self.assertLess(nodes["B"].x, nodes["C"].x)
        self.assertLess(nodes["C"].x, nodes["end"].x)

    def test_components_enclose_their_bound_nodes(self):
        """After layout each ComponentRef rectangle must enclose every
        bound node — the property that makes the diagram readable."""
        ucm = self._build_with_components()
        apply_graphviz_layout(ucm)
        m = ucm.maps[0]
        for n in m.nodes:
            if n.cont_ref is None:
                continue
            cr = n.cont_ref
            self.assertGreaterEqual(n.x, cr.x,
                f"{n.effective_name}.x={n.x} outside {cr.cont_def.name}.x={cr.x}")
            self.assertLessEqual(n.x, cr.x + cr.width)
            self.assertGreaterEqual(n.y, cr.y)
            self.assertLessEqual(n.y, cr.y + cr.height)

    def test_y_axis_flipped_for_jucmnav(self):
        """Graphviz uses bottom-left origin; jUCMNav uses top-left.
        After the y-flip every y coordinate must be non-negative
        (negatives would indicate the flip didn't happen)."""
        ucm = self._build_with_components()
        apply_graphviz_layout(ucm)
        for n in ucm.maps[0].nodes:
            self.assertGreaterEqual(n.y, 0,
                f"{n.effective_name}.y={n.y} negative — y-flip missing")
        for cr in ucm.maps[0].cont_refs:
            self.assertGreaterEqual(cr.y, 0,
                f"{cr.cont_def.name}.y={cr.y} negative")

    def test_default_export_uses_graphviz_layout(self):
        """The exporter selects graphviz by default and produces
        different coordinates than the built-in (a smoke-test that
        the default really did fire)."""
        from pm4py_ucm.objects.ucm.exporter.variants.jucm import (
            serialize_to_string,
        )
        tree = op("->",
                  leaf("A"), leaf("B"), leaf("C"))
        ucm_gv = pm4py_ucm.convert_to_ucm(tree, parameters={
            "performers": {"A": "T1", "B": "T2", "C": "T1"},
            "routing_empty_points": False,
        })
        ucm_b = pm4py_ucm.convert_to_ucm(tree, parameters={
            "performers": {"A": "T1", "B": "T2", "C": "T1"},
            "routing_empty_points": False,
        })
        serialize_to_string(ucm_gv)  # default: graphviz
        serialize_to_string(ucm_b, layout_engine="builtin")
        # Differing coordinates implies different engines fired.
        coords_gv = [(n.x, n.y) for n in ucm_gv.maps[0].nodes]
        coords_b = [(n.x, n.y) for n in ucm_b.maps[0].nodes]
        self.assertNotEqual(coords_gv, coords_b)


class GraphvizLayouterFallbackTests(unittest.TestCase):
    """Behaviour when graphviz is unavailable — should fall back to
    the built-in layouter silently without raising."""

    def test_layout_engine_builtin_skips_graphviz(self):
        """Forcing ``layout_engine="builtin"`` runs the in-house
        layouter even when graphviz is available."""
        from pm4py_ucm.objects.ucm.exporter.variants.jucm import (
            serialize_to_string,
        )
        tree = op("->", leaf("A"), leaf("B"))
        ucm = pm4py_ucm.convert_to_ucm(tree)
        # Should not raise, should produce well-formed XML.
        xml = serialize_to_string(ucm, layout_engine="builtin")
        self.assertIn("<urn:URNspec", xml)
        # Every node should have a non-zero position.
        for n in ucm.maps[0].nodes:
            self.assertFalse(n.x == 0 and n.y == 0)


if __name__ == "__main__":
    unittest.main()
