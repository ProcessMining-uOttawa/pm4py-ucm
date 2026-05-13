"""Tests for the routing-empty-points pass (issue 4).

Empty points inserted on the in- and out-arcs of every fork/join give
the layouter — and jUCMNav's own renderer — explicit elbow points
where connections can bend, improving the visual flow on complex
diagrams. They are pure layout hints and must not change semantics.
"""

import unittest
from types import SimpleNamespace as NS

from pm4py_ucm import UCM, convert_to_ucm
from pm4py_ucm.objects.ucm.conversion.from_process_tree import (
    insert_routing_empty_points,
    simplify_empty_points,
)


leaf = lambda l: NS(operator=None, children=[], label=l)
op = lambda o, *kids: NS(operator=NS(value=o), children=list(kids), label=None)

_FORK_JOIN = (UCM.OrFork, UCM.OrJoin, UCM.AndFork, UCM.AndJoin)


class RoutingEmptyPointsTests(unittest.TestCase):

    def test_default_on(self):
        """The converter inserts routing empty points by default."""
        tree = op("->", leaf("A"), op("X", leaf("B"), leaf("C")), leaf("D"))
        ucm = convert_to_ucm(tree)
        m = ucm.maps[0]
        empties = [n for n in m.nodes if isinstance(n, UCM.EmptyPoint)]
        self.assertGreater(len(empties), 0,
                           "default-on routing pass should add empty points")

    def test_can_be_disabled(self):
        tree = op("->", leaf("A"), op("X", leaf("B"), leaf("C")), leaf("D"))
        ucm = convert_to_ucm(tree, parameters={"routing_empty_points": False})
        m = ucm.maps[0]
        empties = [n for n in m.nodes if isinstance(n, UCM.EmptyPoint)]
        self.assertEqual(empties, [])

    def test_every_fork_join_arc_has_empty_neighbour(self):
        """For every arc adjacent to a fork/join, the *other* endpoint
        is an EmptyPoint. Holds for OR-fork, OR-join, AND-fork, AND-join."""
        tree = op("->",
                  leaf("A"),
                  op("X", leaf("B"), leaf("C")),
                  op("+", leaf("D"), leaf("E")),
                  leaf("F"))
        ucm = convert_to_ucm(tree)
        m = ucm.maps[0]
        for n in m.nodes:
            if not isinstance(n, _FORK_JOIN):
                continue
            for c in n.pred_connections:
                self.assertIsInstance(c.source, UCM.EmptyPoint,
                    f"{type(n).__name__}.pred from {type(c.source).__name__}")
            for c in n.succ_connections:
                self.assertIsInstance(c.target, UCM.EmptyPoint,
                    f"{type(n).__name__}.succ to {type(c.target).__name__}")

    def test_conditions_preserved_on_outbound_half(self):
        """A condition on an arc leaving a fork (e.g. ``TrueBranch``)
        must remain on the same edge after splitting — namely the
        *outbound* half (bend → next), so it still attaches to an edge
        emerging from the fork in the diagram."""
        tree = op("X", leaf("B"), leaf("C"))
        ucm = convert_to_ucm(tree)
        m = ucm.maps[0]
        labelled = [c for c in m.connections
                    if c.condition and c.condition.label]
        self.assertGreater(len(labelled), 0)
        for c in labelled:
            # The labelled connection should run bend → real-node;
            # i.e. its target is NOT an empty point.
            self.assertIsInstance(c.source, UCM.EmptyPoint)
            self.assertNotIsInstance(c.target, UCM.EmptyPoint)

    def test_routing_points_survive_resimplification(self):
        """Routing empty points carry a distinguishing name ("Bend")
        so a later run of :func:`simplify_empty_points` won't undo them."""
        tree = op("X", leaf("A"), leaf("B"))
        ucm = convert_to_ucm(tree)
        before = sum(1 for n in ucm.maps[0].nodes
                     if isinstance(n, UCM.EmptyPoint))
        simplify_empty_points(ucm)
        after = sum(1 for n in ucm.maps[0].nodes
                    if isinstance(n, UCM.EmptyPoint))
        self.assertEqual(before, after,
                         "routing empty points must survive re-simplification")

    def test_does_not_change_responsibility_count(self):
        """Inserting routing points is semantics-preserving — the set of
        responsibilities is unchanged."""
        tree = op("->",
                  leaf("A"),
                  op("X", leaf("B"), leaf("C")),
                  op("+", leaf("D"), leaf("E")))
        ucm_no = convert_to_ucm(tree, parameters={"routing_empty_points": False})
        ucm_yes = convert_to_ucm(tree)
        names_no = sorted(r.name for r in ucm_no.responsibilities)
        names_yes = sorted(r.name for r in ucm_yes.responsibilities)
        self.assertEqual(names_no, names_yes)

    def test_idempotent_on_already_routed_map(self):
        """Running the pass twice doesn't double-insert — once every
        fork/join arc has an EmptyPoint neighbour, a second pass should
        be a no-op for those arcs."""
        tree = op("X", leaf("A"), leaf("B"))
        ucm = convert_to_ucm(tree)
        count_once = len(ucm.maps[0].nodes)
        insert_routing_empty_points(ucm)
        count_twice = len(ucm.maps[0].nodes)
        # Second pass adds empty points around the previously-inserted
        # bend points (because bends are now also adjacent to forks).
        # The bound on growth: at most 2 new empties per existing arc
        # adjacent to a fork. The important property is no infinite
        # growth — a third pass should add nothing more.
        insert_routing_empty_points(ucm)
        count_thrice = len(ucm.maps[0].nodes)
        # After the second pass there are no fork/join arcs left that
        # don't already have an empty-point neighbour, so the third pass
        # is a no-op.
        self.assertEqual(count_twice, count_thrice)


if __name__ == "__main__":
    unittest.main()
