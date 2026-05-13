"""Tests for the process tree → UCM converter.

PM4Py is not assumed to be installed in this environment, so the tests
build *duck-typed* tree structures that match the converter's expectations
(``operator.value`` plus ``children`` / ``label``).
"""

import unittest
from types import SimpleNamespace as NS

from pm4py_ucm import UCM
from pm4py_ucm.objects.ucm.conversion import from_process_tree as conv


def leaf(label):
    return NS(operator=None, children=[], label=label)


def node(op, *children):
    return NS(operator=NS(value=op), children=list(children), label=None)


class ProcessTreeConversionTests(unittest.TestCase):

    def _ucm(self, tree):
        return conv.apply(tree, parameters={"simplify": True})

    def test_single_activity(self):
        ucm = self._ucm(leaf("A"))
        m = ucm.default_map
        # start → resp(A) → end
        self.assertEqual(len(m.start_points), 1)
        self.assertEqual(len(m.end_points), 1)
        resps = [n for n in m.nodes if isinstance(n, UCM.RespRef)]
        self.assertEqual(len(resps), 1)
        self.assertEqual(resps[0].resp_def.name, "A")

    def test_sequence(self):
        ucm = self._ucm(node("->", leaf("A"), leaf("B"), leaf("C")))
        m = ucm.default_map
        labels = [n.resp_def.name for n in m.nodes
                  if isinstance(n, UCM.RespRef)]
        self.assertEqual(sorted(labels), ["A", "B", "C"])

    def test_xor_creates_or_fork_join(self):
        ucm = self._ucm(node("X", leaf("A"), leaf("B")))
        m = ucm.default_map
        forks = [n for n in m.nodes if isinstance(n, UCM.OrFork)]
        joins = [n for n in m.nodes if isinstance(n, UCM.OrJoin)]
        self.assertEqual(len(forks), 1)
        self.assertEqual(len(joins), 1)

    def test_parallel_creates_and_fork_join(self):
        ucm = self._ucm(node("+", leaf("A"), leaf("B")))
        m = ucm.default_map
        forks = [n for n in m.nodes if isinstance(n, UCM.AndFork)]
        joins = [n for n in m.nodes if isinstance(n, UCM.AndJoin)]
        self.assertEqual(len(forks), 1)
        self.assertEqual(len(joins), 1)

    def test_loop_uses_or_constructs(self):
        # Loop: do A; while redo do B then A
        ucm = self._ucm(node("*", leaf("A"), leaf("B")))
        m = ucm.default_map
        # A loop must create at least one OR-join + OR-fork pair
        self.assertTrue(any(isinstance(n, UCM.OrJoin) for n in m.nodes))
        self.assertTrue(any(isinstance(n, UCM.OrFork) for n in m.nodes))

    def test_tau_leaf_does_not_create_resp(self):
        ucm = self._ucm(node("X", leaf("A"), leaf(None)))
        m = ucm.default_map
        # exactly one RespRef (for "A") because tau leaves are silent
        resps = [n for n in m.nodes if isinstance(n, UCM.RespRef)]
        self.assertEqual(len(resps), 1)

    def test_well_formed_paths(self):
        # Every node lies on a path from a start point to an end point.
        ucm = self._ucm(node("->", leaf("A"),
                             node("X", leaf("B"), leaf("C")), leaf("D")))
        m = ucm.default_map
        starts = m.start_points
        self.assertGreaterEqual(len(starts), 1)
        # Reachability check from any start
        seen = set()
        stack = list(starts)
        while stack:
            n = stack.pop()
            if id(n) in seen:
                continue
            seen.add(id(n))
            for c in n._succ:
                stack.append(c.target)
        # Every node is reachable from some start
        self.assertEqual(len(seen), len(m.nodes))


if __name__ == "__main__":
    unittest.main()
