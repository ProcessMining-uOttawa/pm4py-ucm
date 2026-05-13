"""Tests for the compact left-to-right auto-layouter.

The layouter must:

* leave X strictly increasing along the discovered process (no
  layer ever ends up to the right of a "later" layer);
* handle loops without blowing the layer count up — this was the
  bug behind the original layouter putting nodes at ``x=20000``;
* size :class:`UCM.ComponentRef` rectangles to enclose their
  bound nodes plus padding for the cluster label;
* be deterministic and idempotent (running it twice gives the
  same coordinates).
"""

import unittest
from types import SimpleNamespace as NS

from pm4py_ucm import UCM, bind_performers
from pm4py_ucm.objects.ucm.conversion.from_process_tree import apply as convert
from pm4py_ucm.objects.ucm.layout.layouter import apply_layout


def leaf(l): return NS(operator=None, children=[], label=l)
def op(o, *kids): return NS(operator=NS(value=o), children=list(kids), label=None)


class LayouterTests(unittest.TestCase):

    def test_sequence_lays_out_left_to_right(self):
        tree = op("->", leaf("A"), leaf("B"), leaf("C"))
        ucm = convert(tree)
        apply_layout(ucm)
        m = ucm.maps[0]
        # Find RespRefs in original order
        a = next(n for n in m.nodes
                 if isinstance(n, UCM.RespRef) and n.resp_def.name == "A")
        b = next(n for n in m.nodes
                 if isinstance(n, UCM.RespRef) and n.resp_def.name == "B")
        c = next(n for n in m.nodes
                 if isinstance(n, UCM.RespRef) and n.resp_def.name == "C")
        self.assertLess(a.x, b.x)
        self.assertLess(b.x, c.x)
        # Pure sequence: same Y throughout.
        self.assertEqual(a.y, b.y)
        self.assertEqual(b.y, c.y)

    def test_parallel_branches_spread_vertically(self):
        tree = op("+", leaf("A"), leaf("B"))
        ucm = convert(tree)
        apply_layout(ucm)
        m = ucm.maps[0]
        a = next(n for n in m.nodes
                 if isinstance(n, UCM.RespRef) and n.resp_def.name == "A")
        b = next(n for n in m.nodes
                 if isinstance(n, UCM.RespRef) and n.resp_def.name == "B")
        # Same X, different Y.
        self.assertEqual(a.x, b.x)
        self.assertNotEqual(a.y, b.y)

    def test_loop_does_not_explode_layout_width(self):
        """The classic broken-layouter symptom: back-edges bouncing
        layers up indefinitely until X reaches the tens of thousands.
        Modern layouter must keep X within reasonable bounds."""
        tree = op("->", leaf("A"),
                  op("*", leaf("Do"), leaf("Redo")),  # loop
                  leaf("B"))
        ucm = convert(tree)
        apply_layout(ucm)
        m = ucm.maps[0]
        max_x = max(n.x for n in m.nodes)
        # 6 logical layers x 90px = ~540px; allow generous slack but bar 5000+.
        self.assertLess(max_x, 2000,
                        f"loop pattern still blows X up to {max_x}")

    def test_layout_is_deterministic(self):
        """Same input → same output."""
        tree = op("->", leaf("A"),
                  op("X", leaf("B"), leaf("C")),
                  op("+", leaf("D"), leaf("E")),
                  leaf("F"))
        ucm1 = convert(tree); apply_layout(ucm1)
        ucm2 = convert(tree); apply_layout(ucm2)
        coords1 = [(type(n).__name__, n.x, n.y) for n in ucm1.maps[0].nodes]
        coords2 = [(type(n).__name__, n.x, n.y) for n in ucm2.maps[0].nodes]
        self.assertEqual(coords1, coords2)

    def test_layout_is_idempotent(self):
        """Running it twice gives the same coordinates."""
        tree = op("->", leaf("A"), op("X", leaf("B"), leaf("C")), leaf("D"))
        ucm = convert(tree)
        apply_layout(ucm)
        snap1 = [(n.x, n.y) for n in ucm.maps[0].nodes]
        apply_layout(ucm)
        snap2 = [(n.x, n.y) for n in ucm.maps[0].nodes]
        self.assertEqual(snap1, snap2)


class ComponentBoundingBoxTests(unittest.TestCase):
    """The layouter sizes ComponentRefs to enclose their contained nodes."""

    def test_component_box_encloses_its_nodes(self):
        tree = op("->", leaf("A"), leaf("B"), leaf("C"))
        ucm = convert(tree, parameters={
            "performers": {"A": "Team", "B": "Team", "C": "Team"},
        })
        apply_layout(ucm)
        m = ucm.maps[0]
        cr = m.cont_refs[0]
        bound_nodes = m.nodes_in(cr)
        self.assertGreater(len(bound_nodes), 0)
        # Every bound node must sit inside the rectangle.
        for n in bound_nodes:
            self.assertGreaterEqual(n.x, cr.x)
            self.assertLessEqual(n.x, cr.x + cr.width)
            self.assertGreaterEqual(n.y, cr.y)
            self.assertLessEqual(n.y, cr.y + cr.height)

    def test_multiple_components_get_independent_boxes(self):
        tree = op("->", leaf("A"), leaf("B"))
        ucm = convert(tree, parameters={
            "performers": {"A": "Team1", "B": "Team2"},
        })
        apply_layout(ucm)
        m = ucm.maps[0]
        # Two component refs, one per team.
        self.assertEqual(len(m.cont_refs), 2)
        boxes = [(cr.x, cr.y, cr.x + cr.width, cr.y + cr.height)
                 for cr in m.cont_refs]
        # The two boxes should be different (they enclose different nodes).
        self.assertNotEqual(boxes[0], boxes[1])

    def test_nested_component_fits_inside_parent(self):
        ucm = UCM(name="Test")
        m = ucm.add_map(name="Main")

        outer_def = ucm.get_or_add_component("Outer")
        inner_def = ucm.get_or_add_component("Inner")
        outer_ref = m.add_component_ref(outer_def)
        inner_ref = m.add_component_ref(inner_def, parent=outer_ref)

        # Add a node bound to the inner ref, and one bound to the outer.
        n_inner = m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility("Inside")))
        n_inner.cont_ref = inner_ref
        n_outer = m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility("Outside")))
        n_outer.cont_ref = outer_ref
        m.add_connection(n_outer, n_inner)

        apply_layout(ucm)
        # Inner rectangle must fit inside outer's (with possible label pad on top).
        self.assertGreaterEqual(inner_ref.x, outer_ref.x)
        self.assertGreaterEqual(inner_ref.y, outer_ref.y)
        self.assertLessEqual(inner_ref.x + inner_ref.width,
                             outer_ref.x + outer_ref.width)
        self.assertLessEqual(inner_ref.y + inner_ref.height,
                             outer_ref.y + outer_ref.height)


class ComponentNonOverlapTests(unittest.TestCase):
    """Components in URN/UCM must either nest fully or not overlap at all.

    The swim-lane layouter enforces this by allocating each top-level
    component a disjoint Y-band, then nesting children inside their
    parent's band. These tests check that the constraint actually
    holds across the full converter → layouter pipeline."""

    @staticmethod
    def _rects_overlap(a, b):
        """True iff rectangles ``a`` and ``b`` overlap (share area)."""
        ax0, ay0 = a.x, a.y
        ax1, ay1 = a.x + a.width, a.y + a.height
        bx0, by0 = b.x, b.y
        bx1, by1 = b.x + b.width, b.y + b.height
        return ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1

    @staticmethod
    def _rect_contains(outer, inner):
        """True iff ``outer`` fully contains ``inner``."""
        return (outer.x <= inner.x and outer.y <= inner.y and
                outer.x + outer.width >= inner.x + inner.width and
                outer.y + outer.height >= inner.y + inner.height)

    def test_unrelated_components_do_not_overlap(self):
        """Process where one team performs activities at both ends and
        another team works in between — the team rectangles would
        overlap horizontally under any naive bounding-box layout, but
        swim-lanes keep them in disjoint Y-bands."""
        tree = op("->",
                  leaf("A"), leaf("B"), leaf("C"), leaf("D"),
                  leaf("E"))
        ucm = convert(tree, parameters={
            "performers": {
                "A": "Team1",  # early
                "B": "Team2",  # middle
                "C": "Team2",
                "D": "Team2",
                "E": "Team1",  # late — wraps around in process order
            },
        })
        apply_layout(ucm)
        m = ucm.maps[0]
        # Both teams have multiple activities; Team1's nodes span the
        # full X range. Under bounding-box-only fitting, Team1 and Team2
        # would overlap. Swim-lanes must prevent that.
        team1 = next(cr for cr in m.cont_refs if cr.cont_def.name == "Team1")
        team2 = next(cr for cr in m.cont_refs if cr.cont_def.name == "Team2")
        self.assertFalse(
            self._rects_overlap(team1, team2),
            f"Team1 ({team1.x},{team1.y},{team1.width}x{team1.height}) "
            f"overlaps Team2 ({team2.x},{team2.y},{team2.width}x{team2.height})")

    def test_all_pairs_of_top_level_components_disjoint(self):
        """For the issue-tracker-style topology with five teams whose
        nodes interleave throughout the process, every pair of
        top-level component rectangles must be disjoint."""
        tree = op("->",
                  leaf("Create"), leaf("Triage"),
                  op("+", leaf("AssignDev"), leaf("AssignReviewer")),
                  leaf("Implement"),
                  op("*", leaf("Test"), leaf("FixBug")),
                  leaf("Deploy"), leaf("Close"))
        ucm = convert(tree, parameters={
            "performers": {
                "Create": "Reporter",
                "Triage": "Triage Team",
                "AssignDev": "Triage Team",
                "AssignReviewer": "Triage Team",
                "Implement": "Dev Team",
                "Test": "QA Team",
                "FixBug": "Dev Team",
                "Deploy": "DevOps Team",
                "Close": "Triage Team",
            },
        })
        apply_layout(ucm)
        m = ucm.maps[0]
        roots = [cr for cr in m.cont_refs if cr.parent is None]
        for i in range(len(roots)):
            for j in range(i + 1, len(roots)):
                self.assertFalse(
                    self._rects_overlap(roots[i], roots[j]),
                    f"{roots[i].cont_def.name} overlaps "
                    f"{roots[j].cont_def.name}")

    def test_child_component_fully_contained_in_parent(self):
        """A nested ComponentRef must be geometrically contained in its
        parent rectangle (every edge of the child sits inside the
        parent)."""
        ucm = UCM(name="Nested")
        m = ucm.add_map(name="Main")
        outer_def = ucm.get_or_add_component("Outer")
        inner_def = ucm.get_or_add_component("Inner")
        outer_ref = m.add_component_ref(outer_def)
        inner_ref = m.add_component_ref(inner_def, parent=outer_ref)
        # Two nodes inside the inner ref, two more in the outer-only region.
        a = m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility("A")))
        a.cont_ref = inner_ref
        b = m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility("B")))
        b.cont_ref = inner_ref
        c = m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility("C")))
        c.cont_ref = outer_ref
        d = m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility("D")))
        d.cont_ref = outer_ref
        m.add_connection(c, a)
        m.add_connection(a, b)
        m.add_connection(b, d)

        apply_layout(ucm)
        self.assertTrue(
            self._rect_contains(outer_ref, inner_ref),
            f"Inner ({inner_ref.x},{inner_ref.y},{inner_ref.width}x"
            f"{inner_ref.height}) is not fully contained in Outer "
            f"({outer_ref.x},{outer_ref.y},{outer_ref.width}x"
            f"{outer_ref.height})")

    def test_bound_nodes_stay_inside_their_component(self):
        """Every path node bound to a ComponentRef must end up inside
        that rectangle after layout — otherwise the diagram is
        misleading."""
        tree = op("->",
                  leaf("A"), leaf("B"), leaf("C"),
                  op("X", leaf("D"), leaf("E")),
                  leaf("F"))
        ucm = convert(tree, parameters={
            "performers": {
                "A": "Front", "B": "Back", "C": "Back",
                "D": "Front", "E": "Back", "F": "Front",
            },
        })
        apply_layout(ucm)
        m = ucm.maps[0]
        for n in m.nodes:
            cr = n.cont_ref
            if cr is None:
                continue
            self.assertGreaterEqual(n.x, cr.x,
                f"node {n.effective_name} x={n.x} left of comp {cr.name} x={cr.x}")
            self.assertLessEqual(n.x, cr.x + cr.width,
                f"node {n.effective_name} x={n.x} right of comp x+w={cr.x+cr.width}")
            self.assertGreaterEqual(n.y, cr.y,
                f"node {n.effective_name} y={n.y} above comp {cr.name} y={cr.y}")
            self.assertLessEqual(n.y, cr.y + cr.height,
                f"node {n.effective_name} y={n.y} below comp y+h={cr.y+cr.height}")


class ScaleTests(unittest.TestCase):
    """Smoke tests guarding against O(N²) regressions.

    Software event logs (e.g. instrumentation traces) can yield process
    trees with thousands of activities. The full pipeline — converter,
    layouter, exporter, importer — should complete in a few seconds at
    that scale; if it doesn't, a quadratic regression has crept in."""

    @staticmethod
    def _flat_sequence(n):
        return op("->", *[leaf(f"a{i}") for i in range(n)])

    def test_thousand_activity_sequence_converts_quickly(self):
        """1000-activity flat SEQUENCE should convert+layout+export under
        2 seconds (the bar is generous on purpose — a quadratic
        regression would take 10s+)."""
        import time
        tree = self._flat_sequence(1000)
        t = time.time()
        ucm = convert(tree)
        apply_layout(ucm)
        from pm4py_ucm.objects.ucm.exporter.variants.jucm import serialize_to_string
        _ = serialize_to_string(ucm, layout=False)
        elapsed = time.time() - t
        self.assertLess(elapsed, 2.0,
                        f"1000-activity pipeline took {elapsed:.2f}s "
                        f"(expected < 2s); likely O(N²) regression")

    def test_activity_names_with_special_characters(self):
        """NASA-style method names contain dots and parentheses
        (``cev.Failures(cev.ErrorLog)``). They must round-trip through
        the converter, exporter and importer unchanged."""
        special = [
            "cev.TestCEV()",
            "cev.Failures(cev.ErrorLog)",
            "java.util.List<String>.add",
            "Some \"quoted\" name",
            "name & ampersand",
            "name with <angle> brackets",
        ]
        tree = op("->", *[leaf(name) for name in special])
        ucm = convert(tree)
        from pm4py_ucm.objects.ucm.exporter.variants.jucm import serialize_to_string
        from pm4py_ucm.objects.ucm.importer.variants.jucm import parse_string
        xml = serialize_to_string(ucm)
        ucm2 = parse_string(xml)
        resp_names = sorted(r.name for r in ucm2.responsibilities)
        self.assertEqual(resp_names, sorted(special))

    def test_deeply_right_nested_tree_does_not_overflow(self):
        """A 1500-deep right-spine tree must not blow Python's default
        recursion limit. The converter raises the limit defensively."""
        # Build iteratively to avoid blowing the test's own stack.
        node = leaf("a0")
        for i in range(1, 1500):
            node = op("->", leaf(f"a{i}"), node)
        # Should not raise.
        ucm = convert(node)
        # All 1500 activities recognised as responsibilities.
        self.assertEqual(len(ucm.responsibilities), 1500)


if __name__ == "__main__":
    unittest.main()
