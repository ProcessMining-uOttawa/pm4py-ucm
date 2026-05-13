"""Unit tests for the UCM object model."""

import unittest

from pm4py_ucm import UCM


class UCMObjectModelTests(unittest.TestCase):

    def test_create_empty_ucm(self):
        ucm = UCM(name="empty")
        self.assertEqual(ucm.name, "empty")
        self.assertEqual(ucm.maps, [])
        self.assertEqual(ucm.responsibilities, [])
        self.assertEqual(ucm.components, [])

    def test_add_map_and_default_map(self):
        ucm = UCM()
        m = ucm.add_map(name="root")
        self.assertIs(m, ucm.default_map)
        self.assertEqual(m.name, "root")

    def test_path_node_subclasses_exist(self):
        # All ucm.map.PathNode subclasses from the URN metamodel must be
        # accessible as nested classes on ``UCM``.
        for cls_name in [
            "StartPoint", "EndPoint", "EmptyPoint", "RespRef",
            "OrFork", "OrJoin", "AndFork", "AndJoin",
            "WaitingPlace", "Timer", "Stub", "Connect",
            "DirectionArrow", "FailurePoint", "Anything",
        ]:
            self.assertTrue(hasattr(UCM, cls_name),
                            f"UCM.{cls_name} missing")

    def test_node_connection_auto_wires(self):
        ucm = UCM()
        m = ucm.add_map()
        a = m.add_node(UCM.StartPoint())
        b = m.add_node(UCM.EndPoint())
        c = m.add_connection(a, b)
        self.assertIn(c, a._succ)
        self.assertIn(c, b._pred)
        self.assertEqual(m.out_degree(a), 1)
        self.assertEqual(m.in_degree(b), 1)

    def test_responsibility_deduplication(self):
        ucm = UCM()
        r1 = ucm.get_or_add_responsibility("A")
        r2 = ucm.get_or_add_responsibility("A")
        self.assertIs(r1, r2)
        self.assertEqual(len(ucm.responsibilities), 1)

    def test_remove_node_cleans_connections(self):
        ucm = UCM()
        m = ucm.add_map()
        a = m.add_node(UCM.StartPoint())
        b = m.add_node(UCM.EmptyPoint())
        c = m.add_node(UCM.EndPoint())
        m.add_connection(a, b)
        m.add_connection(b, c)
        m.remove_node(b)
        self.assertNotIn(b, m.nodes)
        self.assertEqual(len(m.connections), 0)

    def test_unique_ids(self):
        ucm = UCM()
        m = ucm.add_map()
        a = m.add_node(UCM.StartPoint())
        b = m.add_node(UCM.EndPoint())
        self.assertNotEqual(a.id, b.id)


if __name__ == "__main__":
    unittest.main()
