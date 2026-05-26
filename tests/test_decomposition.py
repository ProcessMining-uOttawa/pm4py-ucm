"""Tests for hierarchical decomposition of process trees into multi-map UCMs.

Covers the three boundary rules (``on_root_sequence``, ``on_parallel``,
``on_loop``), the safety-net parameters (``max_leaves_per_map``,
``min_leaves_to_decompose``, ``balance_ratio``), parameter-form
validation, byte-stable round-trip of the resulting model, the
regression guard that the ``"off"``/``None`` path is byte-identical to
pre-change output, and the stacked PNG rendering."""

import os
import re
import shutil
import tempfile
import unittest

from pm4py_ucm import (
    UCM, convert_to_ucm, save_vis_ucm, view_ucm,
)
from pm4py_ucm.objects.ucm.conversion import (
    decomposition as _decomp, from_process_tree as _ft,
)
from pm4py_ucm.objects.ucm.exporter.variants.jucm import serialize_to_string
from pm4py_ucm.objects.ucm.importer.variants.jucm import parse_string


_GRAPHVIZ = shutil.which("dot") is not None


class T:
    """Minimal duck-typed process tree node for tests.

    Mirrors the attributes the converter duck-types on:
    ``operator``, ``label``, ``children``.
    """

    def __init__(self, operator=None, label=None, children=None):
        self.operator = operator
        self.label = label
        self.children = children or []


def _seq(*labels):
    """Convenience: a sequence of leaves with the given labels."""
    return T(operator="->", children=[T(label=lab) for lab in labels])


def _count_stubs(ucm):
    return sum(1 for m in ucm.maps for n in m.nodes if isinstance(n, UCM.Stub))


# =========================================================================
# Parameter parsing
# =========================================================================

class ParameterParsingTests(unittest.TestCase):
    """The ``decomposition`` argument accepts the documented forms."""

    def test_none_and_off_disable_decomposition(self):
        self.assertIsNone(_decomp.parse_decomposition(None))
        self.assertIsNone(_decomp.parse_decomposition("off"))

    def test_auto_preset(self):
        opts = _decomp.parse_decomposition("auto")
        self.assertTrue(opts["on_root_sequence"])
        self.assertTrue(opts["on_parallel"])
        self.assertTrue(opts["on_loop"])
        self.assertEqual(opts["max_leaves_per_map"], 20)
        self.assertEqual(opts["min_leaves_to_decompose"], 4)
        self.assertAlmostEqual(opts["balance_ratio"], 0.2)

    def test_aggressive_preset(self):
        opts = _decomp.parse_decomposition("aggressive")
        self.assertEqual(opts["max_leaves_per_map"], 10)
        # boundaries unchanged
        self.assertTrue(opts["on_parallel"])

    def test_dict_merges_with_auto_defaults(self):
        opts = _decomp.parse_decomposition({"max_leaves_per_map": 5})
        # Overridden:
        self.assertEqual(opts["max_leaves_per_map"], 5)
        # Inherited from "auto":
        self.assertTrue(opts["on_root_sequence"])
        self.assertEqual(opts["min_leaves_to_decompose"], 4)

    def test_unknown_key_raises(self):
        with self.assertRaises(ValueError) as cm:
            _decomp.parse_decomposition({"on_root_sequence": True,
                                         "typo_max_leaves": 5})
        self.assertIn("typo_max_leaves", str(cm.exception))


# =========================================================================
# Individual rules
# =========================================================================

class IndividualRulesTests(unittest.TestCase):
    """Each rule, on its own, produces the expected number of plug-ins."""

    def test_on_root_sequence_isolates_each_top_level_child(self):
        # Top-level "->" with three meaty children. Each should become
        # its own plug-in.
        tree = T(operator="->", children=[
            _seq("A1", "A2", "A3", "A4"),
            _seq("B1", "B2", "B3", "B4"),
            _seq("C1", "C2", "C3", "C4"),
        ])
        ucm = convert_to_ucm(tree, decomposition={
            "on_root_sequence": True,
            "on_parallel": False,
            "on_loop": False,
            "max_leaves_per_map": 1000,
            "min_leaves_to_decompose": 3,
            "balance_ratio": 0.0,
        })
        # Root + 3 plug-ins.
        self.assertEqual(len(ucm.maps), 4)
        # Root map has 3 stubs.
        root_stubs = [n for n in ucm.maps[0].nodes if isinstance(n, UCM.Stub)]
        self.assertEqual(len(root_stubs), 3)
        # Every stub binds to a plug-in.
        for s in root_stubs:
            self.assertEqual(len(s.bindings), 1)

    def test_on_parallel_isolates_each_branch(self):
        tree = T(operator="+", children=[
            _seq("A1", "A2", "A3", "A4"),
            _seq("B1", "B2", "B3", "B4"),
        ])
        ucm = convert_to_ucm(tree, decomposition={
            "on_root_sequence": False,
            "on_parallel": True,
            "on_loop": False,
            "max_leaves_per_map": 1000,
            "min_leaves_to_decompose": 3,
            "balance_ratio": 0.0,
        })
        # Root + 2 plug-ins.
        self.assertEqual(len(ucm.maps), 3)
        # Root still contains the AndFork/AndJoin, plus 2 stubs.
        self.assertTrue(any(isinstance(n, UCM.AndFork) for n in ucm.maps[0].nodes))
        self.assertTrue(any(isinstance(n, UCM.AndJoin) for n in ucm.maps[0].nodes))
        root_stubs = [n for n in ucm.maps[0].nodes if isinstance(n, UCM.Stub)]
        self.assertEqual(len(root_stubs), 2)

    def test_on_alternative_isolates_each_xor_branch(self):
        # XOR with two meaty branches. Each becomes its own plug-in.
        tree = T(operator="X", children=[
            _seq("A1", "A2", "A3", "A4"),
            _seq("B1", "B2", "B3", "B4"),
        ])
        ucm = convert_to_ucm(tree, decomposition={
            "on_root_sequence": False,
            "on_parallel": False,
            "on_alternative": True,
            "on_loop": False,
            "max_leaves_per_map": 1000,
            "min_leaves_to_decompose": 3,
            "balance_ratio": 0.0,
        })
        # Root + 2 plug-ins.
        self.assertEqual(len(ucm.maps), 3)
        # Root still contains the OrFork/OrJoin around the alternative,
        # plus 2 stubs.
        self.assertTrue(any(isinstance(n, UCM.OrFork) for n in ucm.maps[0].nodes))
        self.assertTrue(any(isinstance(n, UCM.OrJoin) for n in ucm.maps[0].nodes))
        root_stubs = [n for n in ucm.maps[0].nodes if isinstance(n, UCM.Stub)]
        self.assertEqual(len(root_stubs), 2)

    def test_on_alternative_handles_or_operator_too(self):
        # PM4Py's "O" (or-of-the-non-empty-subsets) operator gets folded
        # into XOR by the converter, and on_alternative should fire on
        # its children too.
        tree = T(operator="O", children=[
            _seq("A1", "A2", "A3", "A4"),
            _seq("B1", "B2", "B3", "B4"),
        ])
        ucm = convert_to_ucm(tree, decomposition={
            "on_root_sequence": False,
            "on_parallel": False,
            "on_alternative": True,
            "on_loop": False,
            "max_leaves_per_map": 1000,
            "min_leaves_to_decompose": 3,
            "balance_ratio": 0.0,
        })
        self.assertEqual(len(ucm.maps), 3)

    def test_on_alternative_off_keeps_xor_inline(self):
        # With on_alternative explicitly off, an XOR of two big branches
        # stays inline (subject to cap, which is huge here).
        tree = T(operator="X", children=[
            _seq("A1", "A2", "A3"),
            _seq("B1", "B2", "B3"),
        ])
        ucm = convert_to_ucm(tree, decomposition={
            "on_root_sequence": False,
            "on_parallel": False,
            "on_alternative": False,
            "on_loop": False,
            "max_leaves_per_map": 1000,
            "min_leaves_to_decompose": 2,
            "balance_ratio": 0.0,
        })
        # Single flat map.
        self.assertEqual(len(ucm.maps), 1)
        self.assertEqual(_count_stubs(ucm), 0)

    def test_on_loop_collapses_loop_to_a_stub(self):
        # Loop with a body of four activities; everything else stays
        # forward in the root.
        loop_body = _seq("L1", "L2", "L3", "L4")
        tree = T(operator="->", children=[
            T(label="Before"),
            T(operator="*", children=[loop_body, T(label=None)]),
            T(label="After"),
        ])
        ucm = convert_to_ucm(tree, decomposition={
            "on_root_sequence": False,
            "on_parallel": False,
            "on_loop": True,
            "max_leaves_per_map": 1000,
            "min_leaves_to_decompose": 2,
            "balance_ratio": 0.0,
        })
        # Root + 1 plug-in (the loop body).
        self.assertEqual(len(ucm.maps), 2)
        root_stubs = [n for n in ucm.maps[0].nodes if isinstance(n, UCM.Stub)]
        self.assertEqual(len(root_stubs), 1)
        # The root no longer contains LoopFork/LoopJoin.
        for op_type in (UCM.OrFork, UCM.OrJoin):
            self.assertFalse(
                any(isinstance(n, op_type) for n in ucm.maps[0].nodes),
                f"Root still has {op_type.__name__}; loop machinery should be in plug-in",
            )
        # The plug-in *does* contain the loop machinery.
        plugin_nodes = ucm.maps[1].nodes
        self.assertTrue(any(isinstance(n, UCM.OrFork) for n in plugin_nodes))
        self.assertTrue(any(isinstance(n, UCM.OrJoin) for n in plugin_nodes))


# =========================================================================
# All rules combined
# =========================================================================

class AutoPresetTests(unittest.TestCase):
    def test_auto_decomposes_a_realistic_tree(self):
        tree = T(operator="->", children=[
            _seq("A1", "A2", "A3", "A4", "A5"),
            T(operator="+", children=[
                _seq("B1", "B2", "B3", "B4"),
                _seq("C1", "C2", "C3", "C4"),
            ]),
            T(operator="*", children=[
                _seq("D1", "D2", "D3", "D4"),
                T(label=None),
            ]),
        ])
        ucm = convert_to_ucm(tree, decomposition="auto")
        # Many maps — root + the three top-level children become plug-ins,
        # then the + and * inside get further decomposed.
        self.assertGreater(len(ucm.maps), 3)
        # Every stub has a binding.
        for m in ucm.maps:
            for n in m.nodes:
                if isinstance(n, UCM.Stub):
                    self.assertEqual(
                        len(n.bindings), 1,
                        f"Stub {n.name!r} in map {m.name!r} has no binding",
                    )


# =========================================================================
# Cap and floor
# =========================================================================

class CapAndFloorTests(unittest.TestCase):
    def test_cap_force_cuts_when_no_rule_fires(self):
        # All boundary rules off; should still produce >1 map purely
        # via cap enforcement. The tree needs operator-subtrees to
        # decompose into; an XOR of nested sequences gives the cap
        # enforcer five sub-sequences (6 leaves each, op="->") to bite
        # into. Without those operator descendants there is nothing
        # for the cap enforcer to extract — that's a real algorithmic
        # limit on flat trees.
        subseqs = [
            T(operator="->", children=[T(label=f"sub{i}_{k}") for k in range(6)])
            for i in range(5)
        ]
        tree = T(operator="X", children=subseqs)
        ucm = convert_to_ucm(tree, decomposition={
            "on_root_sequence": False,
            "on_parallel": False,
            "on_loop": False,
            "max_leaves_per_map": 10,
            "min_leaves_to_decompose": 2,
            "balance_ratio": 0.0,
        })
        self.assertGreater(len(ucm.maps), 1)
        self.assertGreater(_count_stubs(ucm), 0)

    def test_cap_respected_with_nested_subtrees(self):
        # Each stage holds 4 pairs of leaves (8 leaves), and there are 3
        # stages. With cap=5, the cap enforcer must (a) cut some stages
        # out of the root, then (b) recurse and cut some pairs out of
        # each over-cap stage. Every map ends up ≤ 5 leaves.
        def stage(prefix):
            return T(operator="->", children=[
                T(operator="->", children=[
                    T(label=f"{prefix}{2*i}"), T(label=f"{prefix}{2*i+1}")
                ]) for i in range(4)
            ])
        tree = T(operator="->", children=[stage("A"), stage("B"), stage("C")])
        ucm = convert_to_ucm(tree, decomposition={
            "on_root_sequence": False,
            "on_parallel": False,
            "on_loop": False,
            "max_leaves_per_map": 5,
            "min_leaves_to_decompose": 2,
            "balance_ratio": 0.0,
        })
        for m in ucm.maps:
            resp_count = sum(1 for n in m.nodes if isinstance(n, UCM.RespRef))
            self.assertLessEqual(
                resp_count, 5,
                f"Map {m.name!r}: {resp_count} resprefs > 5",
            )

    def test_floor_prevents_micro_decomposition(self):
        # Parallel of two tiny branches (2 leaves each, total 4) under
        # ``on_parallel=True`` but ``min_leaves_to_decompose=10`` —
        # nothing should be extracted.
        tree = T(operator="+", children=[
            _seq("A", "B"),
            _seq("C", "D"),
        ])
        ucm = convert_to_ucm(tree, decomposition={
            "on_root_sequence": False,
            "on_parallel": True,
            "on_loop": False,
            "max_leaves_per_map": 1000,
            "min_leaves_to_decompose": 10,
            "balance_ratio": 0.0,
        })
        # No decomposition: single map.
        self.assertEqual(len(ucm.maps), 1)
        self.assertEqual(_count_stubs(ucm), 0)


# =========================================================================
# Balance ratio
# =========================================================================

class BalanceRatioTests(unittest.TestCase):
    def test_balance_keeps_small_siblings_inline(self):
        # Sequence with one huge child (10 leaves) and two tiny ones
        # (1 leaf each). With balance_ratio=0.2, only the big child
        # crosses 20% of the total (10/12 ≈ 83% vs 1/12 ≈ 8%).
        tree = T(operator="->", children=[
            T(label="tinyA"),
            _seq(*[f"X{i}" for i in range(10)]),
            T(label="tinyB"),
        ])
        ucm = convert_to_ucm(tree, decomposition={
            "on_root_sequence": True,
            "on_parallel": False,
            "on_loop": False,
            "max_leaves_per_map": 1000,
            "min_leaves_to_decompose": 1,
            "balance_ratio": 0.2,
        })
        # Root + 1 plug-in (the big middle child). The two tiny siblings
        # stay inlined.
        self.assertEqual(len(ucm.maps), 2)
        self.assertEqual(_count_stubs(ucm), 1)
        # The two tiny children are still rendered as RespRefs in the root.
        names = {n.name for n in ucm.maps[0].nodes if isinstance(n, UCM.RespRef)}
        self.assertIn("tinyA", names)
        self.assertIn("tinyB", names)


# =========================================================================
# Regression and round-trip
# =========================================================================

class RoundTripTests(unittest.TestCase):
    def test_decomposed_ucm_round_trips_byte_stably(self):
        tree = T(operator="->", children=[
            _seq("A1", "A2", "A3", "A4", "A5"),
            T(operator="+", children=[
                _seq("B1", "B2", "B3", "B4"),
                _seq("C1", "C2", "C3", "C4"),
            ]),
        ])
        ucm = convert_to_ucm(tree, decomposition="auto")

        xml1 = serialize_to_string(ucm, layout=False)
        ucm2 = parse_string(xml1)
        xml2 = serialize_to_string(ucm2, layout=False)

        strip = lambda s: re.sub(r' (created|modified)="[^"]*"', '', s)
        self.assertEqual(strip(xml1), strip(xml2))

        # Importer recovers stub bindings to live objects.
        stubs1 = [n for m in ucm.maps for n in m.nodes if isinstance(n, UCM.Stub)]
        stubs2 = [n for m in ucm2.maps for n in m.nodes if isinstance(n, UCM.Stub)]
        self.assertEqual(len(stubs1), len(stubs2))
        for s in stubs2:
            self.assertEqual(len(s.bindings), 1)
            self.assertIsNotNone(s.bindings[0].plugin)


class RegressionGuardTests(unittest.TestCase):
    """The ``"off"``/None paths produce byte-identical output to the
    pre-decomposition converter."""

    _FIXTURE = os.path.join(
        os.path.dirname(__file__), "fixtures", "regression_no_decomp.jucm")

    def _build_reference_tree(self):
        return T(operator="->", children=[
            T(label="A"),
            T(operator="+", children=[T(label="B"), T(label="C")]),
            T(operator="*", children=[T(label="D"), T(label=None)]),
            T(operator="X", children=[T(label="E"), T(label="F")]),
        ])

    def test_default_None_matches_fixture(self):
        ucm = convert_to_ucm(self._build_reference_tree())  # no decomposition arg
        xml = serialize_to_string(ucm, layout=False)
        xml = re.sub(r' (created|modified)="[^"]*"', '', xml)
        with open(self._FIXTURE, "r", encoding="utf-8", newline="") as f:
            expected = f.read()
        self.assertEqual(xml, expected)

    def test_decomposition_off_matches_fixture(self):
        ucm = convert_to_ucm(self._build_reference_tree(), decomposition="off")
        xml = serialize_to_string(ucm, layout=False)
        xml = re.sub(r' (created|modified)="[^"]*"', '', xml)
        with open(self._FIXTURE, "r", encoding="utf-8", newline="") as f:
            expected = f.read()
        self.assertEqual(xml, expected)


# =========================================================================
# Stacked PNG rendering
# =========================================================================

@unittest.skipUnless(_GRAPHVIZ, "graphviz not on PATH")
class StackedRenderingTests(unittest.TestCase):
    def _decomposed_ucm(self):
        tree = T(operator="->", children=[
            _seq("A1", "A2", "A3", "A4"),
            _seq("B1", "B2", "B3", "B4"),
            _seq("C1", "C2", "C3", "C4"),
        ])
        return convert_to_ucm(tree, decomposition={
            "on_root_sequence": True,
            "on_parallel": False,
            "on_loop": False,
            "max_leaves_per_map": 1000,
            "min_leaves_to_decompose": 3,
            "balance_ratio": 0.0,
        })

    def test_stacked_save_writes_a_png_with_expected_height(self):
        ucm = self._decomposed_ucm()
        self.assertEqual(len(ucm.maps), 4)  # root + 3 plug-ins
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "stacked.png")
            save_vis_ucm(ucm, path)
            # The file exists and is a valid PNG with non-trivial height.
            from PIL import Image
            with Image.open(path) as img:
                self.assertEqual(img.format, "PNG")
                # 4 panels each ≥ 50 px after rendering — generous lower bound.
                self.assertGreater(img.height, 200)

    def test_map_filter_produces_single_panel(self):
        ucm = self._decomposed_ucm()
        # Pick the second map by name.
        target = ucm.maps[1].name
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "single.png")
            save_vis_ucm(ucm, path, map=target)
            from PIL import Image
            with Image.open(path) as img:
                self.assertEqual(img.format, "PNG")


# =========================================================================
# Error handling
# =========================================================================

class ErrorHandlingTests(unittest.TestCase):
    def test_invalid_decomposition_value_raises(self):
        with self.assertRaises(ValueError):
            convert_to_ucm(_seq("a", "b"), decomposition="bogus")


# =========================================================================
# Plug-in layout and human-readable names
# =========================================================================

@unittest.skipUnless(_GRAPHVIZ, "graphviz not on PATH")
class PluginLayoutTests(unittest.TestCase):
    """Plug-in maps must get auto-layout coordinates, not stay at (0, 0).

    Regression: the original graphviz layouter was called with a fixed
    ``map_index=0`` and only laid out the root map. The decomposed
    output then had every plug-in map with every node stacked at the
    origin in jUCMNav, which is unusable.
    """

    def test_plugin_maps_have_nonzero_coordinates(self):
        tree = T(operator="->", children=[
            _seq("A1", "A2", "A3", "A4", "A5"),
            _seq("B1", "B2", "B3", "B4", "B5"),
            _seq("C1", "C2", "C3", "C4", "C5"),
        ])
        ucm = convert_to_ucm(tree, decomposition={
            "on_root_sequence": True,
            "on_parallel": False,
            "on_loop": False,
            "max_leaves_per_map": 1000,
            "min_leaves_to_decompose": 3,
            "balance_ratio": 0.0,
        })
        self.assertEqual(len(ucm.maps), 4)  # root + 3 plug-ins

        # Trigger the layouter via the exporter (its default).
        serialize_to_string(ucm)

        # Every map should have at least one node with non-zero
        # coordinates — i.e. the layouter ran on it.
        for ucm_map in ucm.maps:
            non_origin = [n for n in ucm_map.nodes if n.x != 0 or n.y != 0]
            self.assertTrue(
                non_origin,
                f"Map {ucm_map.name!r}: every node at (0, 0); "
                "layouter did not run on this map.",
            )


class InductiveAutoResourceTests(unittest.TestCase):
    """Resource discovery is on by default in ``discover_ucm_inductive``.

    When the log carries any of the standard XES "who" attributes
    (``org:resource`` / ``org:role`` / ``org:group``), components are
    generated even when the caller doesn't pass ``resource_attribute``
    explicitly — and the result is consistent between flat and
    decomposed outputs.
    """

    def _log(self):
        import pandas as pd  # local: pandas is a pm4py transitive dep
        events = []
        acts = [
            ("A1", "r1"), ("A2", "r1"), ("A3", "r1"), ("A4", "r1"),
            ("B1", "r2"), ("B2", "r2"), ("B3", "r2"), ("B4", "r2"),
            ("C1", "r3"), ("C2", "r3"), ("C3", "r3"), ("C4", "r3"),
        ]
        for case in range(10):
            for a, r in acts:
                events.append({
                    "case:concept:name": str(case),
                    "concept:name": a,
                    "org:resource": r,
                    "time:timestamp": pd.Timestamp("2026-01-01")
                    + pd.Timedelta(minutes=len(events)),
                })
        return pd.DataFrame(events)

    def _tree(self):
        return T(operator="->", children=[
            T(operator="->", children=[T(label=f"A{i+1}") for i in range(4)]),
            T(operator="->", children=[T(label=f"B{i+1}") for i in range(4)]),
            T(operator="->", children=[T(label=f"C{i+1}") for i in range(4)]),
        ])

    def test_components_generated_without_explicit_resource_attribute(self):
        try:
            import pandas  # noqa: F401
        except ImportError:
            self.skipTest("pandas not installed")
        from pm4py_ucm.algo.discovery.ucm.variants import inductive

        # No resource_attribute passed — auto-detection must still fire.
        ucm = inductive.apply(self._log(), parameters={
            "process_tree": self._tree(),
            "decomposition": "auto",
        })
        self.assertEqual({c.name for c in ucm.components},
                          {"r1", "r2", "r3"})
        # Every map has at least one ComponentRef (root via stub
        # propagation; plug-ins from their own RespRefs).
        for m in ucm.maps:
            self.assertTrue(
                m.cont_refs,
                f"map {m.name!r} has no ComponentRefs",
            )

    def test_resource_attribute_false_disables_auto_discovery(self):
        try:
            import pandas  # noqa: F401
        except ImportError:
            self.skipTest("pandas not installed")
        from pm4py_ucm.algo.discovery.ucm.variants import inductive

        ucm = inductive.apply(self._log(), parameters={
            "process_tree": self._tree(),
            "decomposition": "auto",
            "resource_attribute": False,
        })
        # No components mined because auto-discovery was opted out.
        self.assertEqual(ucm.components, [])


class ComponentPropagationTests(unittest.TestCase):
    """When decomposition pushes every RespRef into plug-in maps, the
    root map ends up holding only Stubs. The propagation pass must
    surface each plug-in's components on its parent map so the root
    still reads with actor context.
    """

    def test_root_inherits_components_from_stubs(self):
        tree = T(operator="->", children=[
            T(operator="->", children=[T(label=f"A{i}") for i in range(4)]),
            T(operator="->", children=[T(label=f"B{i}") for i in range(4)]),
        ])
        performers = {f"A{i}": "team-A" for i in range(4)}
        performers.update({f"B{i}": "team-B" for i in range(4)})

        ucm = convert_to_ucm(tree, parameters={"performers": performers},
                              decomposition={
            "on_root_sequence": True,
            "on_parallel": False,
            "on_loop": False,
            "max_leaves_per_map": 1000,
            "min_leaves_to_decompose": 3,
            "balance_ratio": 0.0,
        })
        # Plug-in maps still get their own components.
        plugin_components = {
            m.name: {cr.cont_def.name for cr in m.cont_refs}
            for m in ucm.maps[1:]
        }
        self.assertEqual(
            set().union(*plugin_components.values()),
            {"team-A", "team-B"},
        )
        # And the root map now also lists both components — the bug
        # this test guards against was that the root had cont_refs=[].
        root_comps = {cr.cont_def.name for cr in ucm.maps[0].cont_refs}
        self.assertEqual(root_comps, {"team-A", "team-B"})

    def test_single_component_stub_is_drawn_inside_component(self):
        # When a plug-in uses exactly one performer, the parent map's
        # Stub gets that performer's ComponentRef as its cont_ref —
        # the stub renders inside the team's swim-lane.
        tree = T(operator="->", children=[
            T(operator="->", children=[T(label=f"A{i}") for i in range(4)]),
            T(operator="->", children=[T(label=f"B{i}") for i in range(4)]),
        ])
        performers = {f"A{i}": "team-A" for i in range(4)}
        performers.update({f"B{i}": "team-B" for i in range(4)})

        ucm = convert_to_ucm(tree, parameters={"performers": performers},
                              decomposition={
            "on_root_sequence": True,
            "on_parallel": False,
            "on_loop": False,
            "max_leaves_per_map": 1000,
            "min_leaves_to_decompose": 3,
            "balance_ratio": 0.0,
        })
        root = ucm.maps[0]
        stub_lanes = {n.name: (n.cont_ref.cont_def.name if n.cont_ref else None)
                      for n in root.nodes if isinstance(n, UCM.Stub)}
        # Each stub's plug-in uses one team, so each stub is bound.
        self.assertEqual(set(stub_lanes.values()), {"team-A", "team-B"})


class ParallelNamingTests(unittest.TestCase):
    """Plug-in names for ``+`` (parallel) branches must not start with
    ``branch`` — the AND-fork already conveys the branching, and the
    prefix was visual noise that added no information."""

    def test_parallel_plugin_names_drop_branch_prefix(self):
        tree = T(operator="+", children=[
            _seq("alpha", "beta", "gamma", "delta"),
            _seq("xi", "omicron", "pi", "rho"),
        ])
        ucm = convert_to_ucm(tree, decomposition={
            "on_root_sequence": False,
            "on_parallel": True,
            "on_loop": False,
            "max_leaves_per_map": 1000,
            "min_leaves_to_decompose": 2,
            "balance_ratio": 0.0,
        })
        plugin_names = [m.name for m in ucm.maps[1:]]
        self.assertTrue(plugin_names)
        for n in plugin_names:
            self.assertFalse(
                n.lower().startswith("branch"),
                f"parallel plug-in name still starts with 'branch': {n!r}",
            )


class RootLoopWrapTests(unittest.TestCase):
    """When the tree's outermost operator is a loop and ``on_loop`` is
    enabled, the decomposition wraps the loop in a synthetic
    single-child sequence so the loop becomes a cut candidate. The
    root map ends up with a single stub pointing to the loop plug-in;
    the loop expansion (OrFork / OrJoin / body) moves into the plug-in.
    """

    def test_root_loop_extracted_into_plugin(self):
        tree = T(operator="*", children=[
            _seq("L1", "L2", "L3", "L4"),
            T(label=None),
        ])
        ucm = convert_to_ucm(tree, decomposition={
            "on_root_sequence": False,
            "on_parallel": False,
            "on_alternative": False,
            "on_loop": True,
            "max_leaves_per_map": 1000,
            "min_leaves_to_decompose": 2,
            "balance_ratio": 0.0,
        })
        self.assertEqual(len(ucm.maps), 2, "want root + loop plug-in")
        root_nodes = ucm.maps[0].nodes
        root_stubs = [n for n in root_nodes if isinstance(n, UCM.Stub)]
        self.assertEqual(len(root_stubs), 1, "root should have one stub")
        # The root no longer contains LoopFork/LoopJoin (OrFork/OrJoin
        # from the loop machinery).
        for op_type in (UCM.OrFork, UCM.OrJoin):
            self.assertFalse(
                any(isinstance(n, op_type) for n in root_nodes),
                f"Root still has {op_type.__name__}; loop machinery should be in plug-in",
            )
        # The plug-in does contain the loop machinery.
        plugin_nodes = ucm.maps[1].nodes
        self.assertTrue(any(isinstance(n, UCM.OrFork) for n in plugin_nodes))
        self.assertTrue(any(isinstance(n, UCM.OrJoin) for n in plugin_nodes))
        # And the stub binds to the plug-in.
        self.assertEqual(len(root_stubs[0].bindings), 1)
        self.assertIs(root_stubs[0].bindings[0].plugin, ucm.maps[1])

    def test_root_loop_left_alone_when_on_loop_off(self):
        # With on_loop=False the wrap should not fire — the loop renders
        # inline in the single (root) map.
        tree = T(operator="*", children=[
            _seq("L1", "L2", "L3"),
            T(label=None),
        ])
        ucm = convert_to_ucm(tree, decomposition={
            "on_root_sequence": False,
            "on_parallel": False,
            "on_alternative": False,
            "on_loop": False,
            "max_leaves_per_map": 1000,
            "min_leaves_to_decompose": 2,
            "balance_ratio": 0.0,
        })
        self.assertEqual(len(ucm.maps), 1)
        self.assertEqual(_count_stubs(ucm), 0)
        # Loop machinery present inline.
        self.assertTrue(any(isinstance(n, UCM.OrFork) for n in ucm.maps[0].nodes))


class XorNamingTests(unittest.TestCase):
    """Plug-in names for cuts under ``×`` / ``∨`` follow the same
    first-to-last recipe as parallel branches — no "alt" or "branch"
    prefix, since the OR-fork on the parent already conveys the
    alternative."""

    def test_xor_plugin_names_use_first_to_last(self):
        tree = T(operator="X", children=[
            _seq("alpha", "beta", "gamma", "delta"),
            _seq("xi", "omicron", "pi", "rho"),
        ])
        ucm = convert_to_ucm(tree, decomposition={
            "on_root_sequence": False,
            "on_parallel": False,
            "on_alternative": True,
            "on_loop": False,
            "max_leaves_per_map": 1000,
            "min_leaves_to_decompose": 2,
            "balance_ratio": 0.0,
        })
        plugin_names = [m.name for m in ucm.maps[1:]]
        self.assertTrue(plugin_names)
        for nm in plugin_names:
            # No leftover prefixes.
            self.assertFalse(nm.lower().startswith(("alt", "branch", "sub")),
                             f"XOR plug-in name {nm!r} has stale prefix")
        # Specifically: first plug-in starts with "alpha".
        self.assertEqual(plugin_names[0].split()[0], "alpha")


class AutoPresetIncludesAlternativeTests(unittest.TestCase):
    """``"auto"`` and ``"aggressive"`` presets must include
    ``on_alternative=True``."""

    def test_auto_includes_on_alternative(self):
        opts = _decomp.parse_decomposition("auto")
        self.assertTrue(opts["on_alternative"])

    def test_aggressive_includes_on_alternative(self):
        opts = _decomp.parse_decomposition("aggressive")
        self.assertTrue(opts["on_alternative"])

    def test_unknown_key_still_raises(self):
        # Make sure the new key is recognised as a valid key, and that
        # other typos still raise.
        opts = _decomp.parse_decomposition({"on_alternative": False})
        self.assertFalse(opts["on_alternative"])
        with self.assertRaises(ValueError):
            _decomp.parse_decomposition({"on_alternatives": True})  # typo


class NameWordLimitTests(unittest.TestCase):
    def test_long_phrases_truncate_to_max_words(self):
        # First leaf has six words; second has eight. The
        # ``first to last`` recipe would produce a 16-word name; the
        # truncator caps it at the first 6.
        long_first = "alpha beta gamma delta epsilon zeta eta theta"
        long_last = "iota kappa lambda mu nu xi omicron pi"
        tree = T(operator="->", children=[
            _seq(long_first, "x1", "x2"),
            _seq(long_last, "y1", "y2"),
        ])
        ucm = convert_to_ucm(tree, decomposition={
            "on_root_sequence": True,
            "on_parallel": False,
            "on_loop": False,
            "max_leaves_per_map": 1000,
            "min_leaves_to_decompose": 2,
            "balance_ratio": 0.0,
        })
        for m in ucm.maps[1:]:  # plug-ins
            self.assertLessEqual(
                len(m.name.split()), 6,
                f"plug-in name {m.name!r} has >6 words",
            )


class HumanReadableNamesTests(unittest.TestCase):
    def test_stub_names_use_spaces_not_underscores(self):
        # Sequence of human-readable phrases — derived plug-in names
        # should preserve spaces.
        tree = T(operator="->", children=[
            _seq("Create Ticket", "Triage", "Assign"),
            _seq("Implement Fix", "Test Fix", "Deploy", "Close"),
        ])
        ucm = convert_to_ucm(tree, decomposition={
            "on_root_sequence": True,
            "on_parallel": False,
            "on_loop": False,
            "max_leaves_per_map": 1000,
            "min_leaves_to_decompose": 2,
            "balance_ratio": 0.0,
        })
        names = [m.name for m in ucm.maps]
        # Every plug-in name has a space (the human-readable derivation).
        plugin_names = names[1:]  # skip root
        self.assertTrue(plugin_names, "no plug-ins were produced")
        for nm in plugin_names:
            self.assertIn(" ", nm,
                          f"plug-in name {nm!r} should contain spaces")
            self.assertNotIn("_", nm,
                             f"plug-in name {nm!r} still has underscores")

        # And the stub on the root map carries the same name.
        stub_names = [n.name for n in ucm.maps[0].nodes
                      if isinstance(n, UCM.Stub)]
        self.assertEqual(set(stub_names), set(plugin_names))

    def test_long_stub_names_wrap_in_jucm_export(self):
        # A long phrase that exceeds wrap_name's threshold should split
        # in the serialised .jucm. jUCMNav encodes the line break as
        # ``&#xD;&#xA;`` inside the name attribute.
        tree = T(operator="->", children=[
            _seq("Reserve Build Slot",
                 "Snapshot Repo",
                 "Notify Reviewer"),
            _seq("Wait For Approval",
                 "Apply Patch",
                 "Run Tests"),
        ])
        ucm = convert_to_ucm(tree, decomposition={
            "on_root_sequence": True,
            "on_parallel": False,
            "on_loop": False,
            "max_leaves_per_map": 1000,
            "min_leaves_to_decompose": 2,
            "balance_ratio": 0.0,
        })
        xml = serialize_to_string(ucm, layout=False)
        # At least one Stub element name carries the wrapped CRLF marker.
        # We look for a Stub node whose name attribute contains the
        # CRLF entity reference.
        self.assertRegex(
            xml,
            r'xsi:type="ucm\.map:Stub"[^>]*name="[^"]*&#xD;&#xA;[^"]*"',
        )


if __name__ == "__main__":
    unittest.main()
