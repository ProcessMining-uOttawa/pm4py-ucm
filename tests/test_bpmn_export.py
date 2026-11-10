"""Tests for the BPMN 2.0 exporter (``docs/bpmn_export.md``).

Four kinds of guarantee:

* **conformance** — every export validates against the OMG BPMN 2.0 XSD
  (vendored under ``tests/data/bpmn-xsd``; needs ``lxml``, skipped
  otherwise);
* **round trip** — ``pm4py.read_bpmn`` recovers the control flow of a
  stub-free model (pm4py's reader knows no ``callActivity``, so models
  with stubs are checked structurally instead);
* **lanes** — components become lanes that tile the pool: sibling bands
  are disjoint, contiguous and span the pool; every flow node is in
  exactly one leaf lane; nesting becomes ``childLaneSet``;
* **layout** — node centres are the UCM coordinates, routing points
  become waypoints, and a model whose components overlap vertically is
  re-laid out with a warning.
"""

import os
import re
import tempfile
import unittest
import warnings
import xml.etree.ElementTree as ET
from types import SimpleNamespace as NS

import pm4py_ucm
from pm4py_ucm import UCM
from pm4py_ucm.objects.ucm.conversion import decomposition
from pm4py_ucm.objects.ucm.conversion.from_process_tree import apply as convert
from pm4py_ucm.objects.ucm.exporter.variants import bpmn as bpmn_exporter
from pm4py_ucm.objects.ucm.exporter.variants.bpmn import serialize_to_string

try:
    from lxml import etree as _lxml
except ImportError:  # pragma: no cover
    _lxml = None

try:
    import pm4py as _pm4py
except ImportError:  # pragma: no cover
    _pm4py = None

HERE = os.path.dirname(__file__)
XSD = os.path.join(HERE, "data", "bpmn-xsd", "BPMN20.xsd")
FIXTURES = os.path.join(HERE, "fixtures")

NS_B = "{http://www.omg.org/spec/BPMN/20100524/MODEL}"
NS_DI = "{http://www.omg.org/spec/BPMN/20100524/DI}"
NS_DC = "{http://www.omg.org/spec/DD/20100524/DC}"
NS_DD = "{http://www.omg.org/spec/DD/20100524/DI}"
NS_U = "{urn:pm4py-ucm:bpmn:1.0}"


def leaf(label):
    return NS(operator=None, children=[], label=label)


def op(o, *kids):
    return NS(operator=NS(value=o), children=list(kids), label=None)


def claims_tree():
    """Sequence, choice, loop and parallelism in one tree."""
    return op("->", leaf("Register"),
              op("X", leaf("Check A"), leaf("Check B")),
              op("*", leaf("Review"), leaf("Rework")),
              op("+", leaf("Pay"), leaf("Notify")),
              leaf("Close"))


PERFORMERS = {"Register": "Clerk", "Check A": "Clerk", "Check B": "Expert",
              "Review": "Expert", "Rework": "Expert", "Pay": "Finance",
              "Notify": "Finance", "Close": "Clerk"}


def claims_ucm():
    ucm = convert(claims_tree())
    ucm.bind_performers(PERFORMERS)
    return ucm


def dynamic_stub_ucm():
    """``start → dynamic stub → end`` with two plug-ins guarded by
    preconditions, built by hand the way the family assembler does."""
    u = UCM(name="Dyn")
    root = u.add_map(UCM.UCMmap(name="Root"))
    s = UCM.StartPoint(name="go")
    st = UCM.Stub(name="Variants", dynamic=True)
    e = UCM.EndPoint(name="done")
    root.add_connection(s, st)
    root.add_connection(st, e)
    for i in range(2):
        m = u.add_map(UCM.UCMmap(name=f"Plug{i}"))
        ps = UCM.StartPoint(name="in")
        r = UCM.RespRef(name=f"Work{i}",
                        resp_def=u.get_or_add_responsibility(f"Work{i}"))
        pe = UCM.EndPoint(name="out")
        m.add_connection(ps, r)
        m.add_connection(r, pe)
        b = UCM.PluginBinding(st, m)
        b.precondition = UCM.Condition(label=f"v{i}", expression=f"variant == {i}")
        b.add_in(st.pred_connections[0], ps)
        b.add_out(pe, st.succ_connections[0])
        st.bindings.append(b)
    return u


def parse(xml: str):
    return ET.fromstring(xml.encode("utf-8"))


def shapes(root):
    """``bpmnElement → (x, y, w, h)`` over every BPMNShape."""
    out = {}
    for sh in root.iter(f"{NS_DI}BPMNShape"):
        b = sh.find(f"{NS_DC}Bounds")
        out[sh.get("bpmnElement")] = tuple(float(b.get(k))
                                           for k in ("x", "y", "width", "height"))
    return out


class XsdConformanceTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if _lxml is None:
            raise unittest.SkipTest("lxml not installed")
        cls.schema = _lxml.XMLSchema(_lxml.parse(XSD))

    def assertValid(self, xml: str):
        doc = _lxml.fromstring(xml.encode("utf-8"))
        ok = self.schema.validate(doc)
        self.assertTrue(ok, "\n".join(str(e) for e in self.schema.error_log))

    def test_flat_model(self):
        self.assertValid(serialize_to_string(claims_ucm()))

    def test_every_option(self):
        ucm = claims_ucm()
        self.assertValid(serialize_to_string(ucm, lanes="none"))
        self.assertValid(serialize_to_string(
            ucm, lane_names="with_kind", annotate="all", metrics="none"))

    def test_decomposed_model(self):
        tree = op("->", op("->", leaf("A1"), leaf("A2"), leaf("A3")),
                  op("X", op("->", leaf("B1"), leaf("B2"), leaf("B3")),
                     op("->", leaf("C1"), leaf("C2"), leaf("C3"))),
                  op("->", leaf("D1"), leaf("D2"), leaf("D3")))
        ucm = decomposition.apply(tree, {"decomposition": "aggressive"})
        self.assertGreater(len(ucm.maps), 1)
        self.assertValid(serialize_to_string(ucm))

    def test_dynamic_stub(self):
        self.assertValid(serialize_to_string(dynamic_stub_ucm()))

    def test_jucmnav_fixtures(self):
        for name in ("SimpleExample.jucm", "SimpleExampleWithStub.jucm"):
            ucm = pm4py_ucm.read_ucm(os.path.join(FIXTURES, name))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self.assertValid(serialize_to_string(ucm, validate=False))


class RoundTripTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if _pm4py is None:
            raise unittest.SkipTest("pm4py not installed")

    def test_pm4py_reads_the_control_flow_back(self):
        ucm = claims_ucm()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "claims.bpmn")
            self.assertEqual(pm4py_ucm.write_bpmn(ucm, path), path)
            graph = _pm4py.read_bpmn(path)
        nodes = graph.get_nodes()
        names = sorted(n.get_name() for n in nodes if type(n).__name__ == "Task")
        self.assertEqual(names, sorted(PERFORMERS))
        kinds = {}
        for n in nodes:
            kinds[type(n).__name__] = kinds.get(type(n).__name__, 0) + 1
        # 1 start, 1 end, 8 tasks, OR fork+join, loop join+fork, AND fork+join
        self.assertEqual(kinds.get("NormalStartEvent"), 1)
        self.assertEqual(kinds.get("NormalEndEvent"), 1)
        self.assertEqual(kinds.get("ExclusiveGateway"), 4)
        self.assertEqual(kinds.get("ParallelGateway"), 2)
        # Every routing point was folded into a flow: as many flows as
        # arcs between real nodes.
        self.assertEqual(len(graph.get_flows()), 18)


class MappingTests(unittest.TestCase):

    def setUp(self):
        self.ucm = claims_ucm()
        self.xml = serialize_to_string(self.ucm)
        self.root = parse(self.xml)
        self.process = self.root.find(f"{NS_B}process")

    def test_one_pool_one_process(self):
        parts = self.root.findall(f"{NS_B}collaboration/{NS_B}participant")
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].get("processRef"), self.process.get("id"))
        self.assertEqual(self.process.get("isExecutable"), "false")

    def test_element_kinds(self):
        tags = [c.tag.replace(NS_B, "") for c in self.process]
        self.assertEqual(tags.count("task"), 8)
        self.assertEqual(tags.count("startEvent"), 1)
        self.assertEqual(tags.count("endEvent"), 1)
        self.assertEqual(tags.count("exclusiveGateway"), 4)
        self.assertEqual(tags.count("parallelGateway"), 2)
        self.assertEqual(tags.count("sequenceFlow"), 18)
        self.assertNotIn("EmptyPoint", self.xml)

    def test_generated_gateway_names_are_dropped(self):
        for g in self.process.iter(f"{NS_B}exclusiveGateway"):
            self.assertIsNone(g.get("name"))
        directions = {g.get("gatewayDirection")
                      for g in self.process.iter(f"{NS_B}exclusiveGateway")}
        self.assertEqual(directions, {"Diverging", "Converging"})

    def test_unconditioned_choice_has_no_conditions(self):
        # The plain converter puts ``branch0``-style labels with a ``true``
        # expression on choice arcs; those are not guards and BPMN gets
        # neither a conditionExpression nor a default flow for them.
        self.assertNotIn("conditionExpression", self.xml)
        for g in self.process.iter(f"{NS_B}exclusiveGateway"):
            self.assertIsNone(g.get("default"))
        for f in self.process.iter(f"{NS_B}sequenceFlow"):
            self.assertIsNone(f.get("name"))

    def test_real_guards_become_condition_expressions(self):
        m = self.ucm.maps[0]
        fork = next(n for n in m.nodes if isinstance(n, UCM.OrFork)
                    and n.name == "OrFork")
        a, b = fork.succ_connections
        a.set_condition(UCM.Condition(label="small", expression="amount < 100"))
        b.set_condition(UCM.Condition(label="else", expression="true"))
        root = parse(serialize_to_string(self.ucm))
        flows = {f.get("id"): f for f in root.iter(f"{NS_B}sequenceFlow")}
        gw = next(g for g in root.iter(f"{NS_B}exclusiveGateway")
                  if g.get("default"))
        default = flows[gw.get("default")]
        self.assertIsNone(default.find(f"{NS_B}conditionExpression"))
        guarded = [f for f in flows.values()
                   if f.find(f"{NS_B}conditionExpression") is not None]
        self.assertEqual(len(guarded), 1)
        self.assertEqual(guarded[0].get("name"), "small")
        self.assertEqual(guarded[0].find(f"{NS_B}conditionExpression").text,
                         "amount < 100")

    def test_responsibility_traceability(self):
        task = next(t for t in self.process.iter(f"{NS_B}task")
                    if t.get("name") == "Pay")
        ext = task.find(f"{NS_B}extensionElements/{NS_U}ucm")
        self.assertEqual(ext.get("responsibility"), "Pay")
        self.assertEqual(ext.get("performer"), "Finance")

    def test_incoming_outgoing_match_flows(self):
        flows = {f.get("id"): (f.get("sourceRef"), f.get("targetRef"))
                 for f in self.process.iter(f"{NS_B}sequenceFlow")}
        for node in self.process:
            tag = node.tag.replace(NS_B, "")
            if tag in ("sequenceFlow", "laneSet", "extensionElements",
                       "documentation"):
                continue
            nid = node.get("id")
            for inc in node.findall(f"{NS_B}incoming"):
                self.assertEqual(flows[inc.text][1], nid)
            for out in node.findall(f"{NS_B}outgoing"):
                self.assertEqual(flows[out.text][0], nid)

    def test_options_are_checked(self):
        with self.assertRaises(ValueError):
            serialize_to_string(self.ucm, lanes="pools")
        with self.assertRaises(ValueError):
            serialize_to_string(self.ucm, annotate="everything")

    def test_output_is_deterministic(self):
        self.assertEqual(self.xml, serialize_to_string(claims_ucm()))


class LaneTests(unittest.TestCase):

    def _lanes(self, root):
        return {l.get("id"): l for l in root.iter(f"{NS_B}lane")}

    def test_components_are_lanes_that_tile_the_pool(self):
        root = parse(serialize_to_string(claims_ucm()))
        lanes = self._lanes(root)
        self.assertEqual(sorted(l.get("name") for l in lanes.values()),
                         ["Clerk", "Expert", "Finance"])
        sh = shapes(root)
        pool = sh[root.find(f"{NS_B}collaboration/{NS_B}participant").get("id")]
        bands = sorted((sh[lid][1], sh[lid][1] + sh[lid][3]) for lid in lanes)
        self.assertAlmostEqual(bands[0][0], pool[1])
        self.assertAlmostEqual(bands[-1][1], pool[1] + pool[3])
        for (a0, a1), (b0, b1) in zip(bands, bands[1:]):
            self.assertAlmostEqual(a1, b0)     # contiguous, no overlap
        for lid in lanes:
            x, y, w, h = sh[lid]
            self.assertAlmostEqual(x, pool[0] + bpmn_exporter.POOL_LABEL_STRIP)
            self.assertAlmostEqual(w, pool[2] - bpmn_exporter.POOL_LABEL_STRIP)

    def test_every_flow_node_in_exactly_one_lane(self):
        root = parse(serialize_to_string(claims_ucm()))
        process = root.find(f"{NS_B}process")
        node_ids = {c.get("id") for c in process
                    if c.tag.replace(NS_B, "") not in
                    ("sequenceFlow", "laneSet", "extensionElements",
                     "documentation")}
        refs = [r.text for r in root.iter(f"{NS_B}flowNodeRef")]
        self.assertEqual(sorted(refs), sorted(node_ids))

    def test_tasks_sit_in_their_performers_lane(self):
        root = parse(serialize_to_string(claims_ucm()))
        tasks = {t.get("id"): t.get("name")
                 for t in root.iter(f"{NS_B}task")}
        for lane in root.iter(f"{NS_B}lane"):
            for r in lane.findall(f"{NS_B}flowNodeRef"):
                if r.text in tasks:
                    self.assertEqual(PERFORMERS[tasks[r.text]], lane.get("name"))

    def test_nested_components_become_child_lane_sets(self):
        ucm = convert(op("->", leaf("A"), leaf("B"), leaf("C")))
        m = ucm.maps[0]
        outer = ucm.get_or_add_component("Outer")
        inner = ucm.get_or_add_component("Inner")
        outer_ref = m.add_component_ref(outer)
        inner_ref = m.add_component_ref(inner, parent=outer_ref)
        for n in m.resp_refs:
            n.cont_ref = inner_ref if n.resp_def.name == "B" else outer_ref
        root = parse(serialize_to_string(ucm))
        outer_lane = next(l for l in root.iter(f"{NS_B}lane")
                          if l.get("name") == "Outer" and
                          l.find(f"{NS_B}childLaneSet") is not None)
        children = outer_lane.findall(f"{NS_B}childLaneSet/{NS_B}lane")
        self.assertEqual(sorted(c.get("name") for c in children),
                         ["Inner", "Outer"])
        # A lane with children references no flow node itself.
        self.assertEqual(outer_lane.findall(f"{NS_B}flowNodeRef"), [])
        sh = shapes(root)
        ox, oy, ow, oh = sh[outer_lane.get("id")]
        for c in children:
            cx, cy, cw, ch = sh[c.get("id")]
            self.assertGreaterEqual(cy, oy - 1e-6)
            self.assertLessEqual(cy + ch, oy + oh + 1e-6)
            self.assertAlmostEqual(cx, ox + bpmn_exporter.LANE_LABEL_STRIP)

    def test_lane_kind_and_names(self):
        root = parse(serialize_to_string(claims_ucm(), lane_names="with_kind"))
        names = {l.get("name") for l in root.iter(f"{NS_B}lane")}
        self.assertIn("Clerk «Team»", names)
        lane = next(iter(root.iter(f"{NS_B}lane")))
        self.assertEqual(
            lane.find(f"{NS_B}extensionElements/{NS_U}ucm").get("kind"), "Team")

    def test_no_lanes_option(self):
        root = parse(serialize_to_string(claims_ucm(), lanes="none"))
        self.assertEqual(list(root.iter(f"{NS_B}laneSet")), [])
        self.assertEqual(len(root.findall(f"{NS_B}collaboration/{NS_B}participant")), 1)


class LayoutTests(unittest.TestCase):

    def test_node_centres_are_the_ucm_coordinates(self):
        ucm = claims_ucm()
        root = parse(serialize_to_string(ucm))
        sh = shapes(root)
        tasks = {t.get("name"): t.get("id") for t in root.iter(f"{NS_B}task")}
        for n in ucm.maps[0].resp_refs:
            x, y, w, h = sh[tasks[n.resp_def.name]]
            self.assertAlmostEqual(x + w / 2, n.x)
            self.assertAlmostEqual(y + h / 2, n.y)

    def test_routing_points_become_waypoints(self):
        ucm = claims_ucm()
        m = ucm.maps[0]
        bends = [n for n in m.nodes if isinstance(n, UCM.EmptyPoint)]
        self.assertTrue(bends)
        root = parse(serialize_to_string(ucm))
        pts = set()
        for e in root.iter(f"{NS_DI}BPMNEdge"):
            for wp in e.findall(f"{NS_DD}waypoint"):
                pts.add((float(wp.get("x")), float(wp.get("y"))))
        for b in bends:
            self.assertIn((float(b.x), float(b.y)), pts)
        # Every edge has at least two waypoints.
        for e in root.iter(f"{NS_DI}BPMNEdge"):
            self.assertGreaterEqual(len(e.findall(f"{NS_DD}waypoint")), 2)

    def test_edge_ends_touch_shape_borders(self):
        root = parse(serialize_to_string(claims_ucm()))
        sh = shapes(root)
        flows = {f.get("id"): (f.get("sourceRef"), f.get("targetRef"))
                 for f in root.iter(f"{NS_B}sequenceFlow")}
        for e in root.iter(f"{NS_DI}BPMNEdge"):
            src, tgt = flows[e.get("bpmnElement")]
            wps = e.findall(f"{NS_DD}waypoint")
            for wp, box in ((wps[0], sh[src]), (wps[-1], sh[tgt])):
                x, y = float(wp.get("x")), float(wp.get("y"))
                bx, by, bw, bh = box
                on_border = (abs(x - bx) < 1e-6 or abs(x - bx - bw) < 1e-6
                             or abs(y - by) < 1e-6 or abs(y - by - bh) < 1e-6)
                self.assertTrue(on_border, (e.get("bpmnElement"), (x, y), box))
                self.assertTrue(bx - 1e-6 <= x <= bx + bw + 1e-6)
                self.assertTrue(by - 1e-6 <= y <= by + bh + 1e-6)

    def test_overlapping_components_trigger_relayout_warning(self):
        ucm = claims_ucm()
        pm4py_ucm.objects.ucm.layout.layouter.apply_layout(ucm)
        m = ucm.maps[0]
        a, b = m.cont_refs[0], m.cont_refs[1]
        b.y, b.height = a.y, a.height   # side by side: not lanes
        with warnings.catch_warnings(record=True) as ws:
            warnings.simplefilter("always")
            root = parse(serialize_to_string(ucm))
        self.assertTrue(any("re-laid out" in str(w.message) for w in ws))
        sh = shapes(root)
        bands = sorted((sh[l.get("id")][1], sh[l.get("id")][1] + sh[l.get("id")][3])
                       for l in root.iter(f"{NS_B}lane"))
        for (a0, a1), (b0, b1) in zip(bands, bands[1:]):
            self.assertLessEqual(a1, b0 + 1e-6)

    def test_unpositioned_model_is_laid_out(self):
        ucm = claims_ucm()
        self.assertTrue(all(n.x == 0 and n.y == 0 for n in ucm.maps[0].nodes))
        serialize_to_string(ucm)
        self.assertTrue(any(n.x or n.y for n in ucm.maps[0].nodes))


class StubTests(unittest.TestCase):

    def test_static_stub_is_a_call_activity_to_its_plugin_process(self):
        ucm = pm4py_ucm.read_ucm(os.path.join(FIXTURES, "SimpleExampleWithStub.jucm"))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            root = parse(serialize_to_string(ucm, validate=False))
        processes = {p.get("id"): p for p in root.findall(f"{NS_B}process")}
        self.assertEqual(len(processes), len(ucm.maps))
        calls = list(root.iter(f"{NS_B}callActivity"))
        self.assertEqual(len(calls), 1)
        self.assertIn(calls[0].get("calledElement"), processes)
        # One diagram per process, each with its own plane.
        self.assertEqual(len(root.findall(f"{NS_DI}BPMNDiagram")), len(ucm.maps))

    def test_dynamic_stub_is_an_inclusive_choice_of_call_activities(self):
        root = parse(serialize_to_string(dynamic_stub_ucm()))
        sub = next(iter(root.iter(f"{NS_B}subProcess")))
        self.assertEqual(sub.get("name"), "Variants")
        incl = sub.findall(f"{NS_B}inclusiveGateway")
        self.assertEqual({g.get("gatewayDirection") for g in incl},
                         {"Diverging", "Converging"})
        self.assertEqual(list(sub.iter(f"{NS_B}exclusiveGateway")), [])
        calls = sub.findall(f"{NS_B}callActivity")
        self.assertEqual(len(calls), 2)
        guarded = [f for f in sub.findall(f"{NS_B}sequenceFlow")
                   if f.find(f"{NS_B}conditionExpression") is not None]
        self.assertEqual(sorted(f.find(f"{NS_B}conditionExpression").text
                                for f in guarded),
                         ["variant == 0", "variant == 1"])
        # Collapsed in the parent, expanded in its own diagram.
        sh_attrs = {s.get("bpmnElement"): s for s in root.iter(f"{NS_DI}BPMNShape")}
        self.assertEqual(sh_attrs[sub.get("id")].get("isExpanded"), "false")
        planes = {p.get("bpmnElement") for p in root.iter(f"{NS_DI}BPMNPlane")}
        self.assertIn(sub.get("id"), planes)


class MetricsTests(unittest.TestCase):

    def _annotated(self):
        ucm = claims_ucm()
        m = ucm.maps[0]
        pay = next(n for n in m.resp_refs if n.resp_def.name == "Pay")
        pay.add_metadata("perf_frequency", "258")
        pay.add_metadata("perf_mean_time", "3.2 d")
        pay.add_metadata("_perf", "258 · 3.2 d")
        pay.add_metadata("note", "manual")
        fork = next(n for n in m.nodes if isinstance(n, UCM.OrFork)
                    and n.name == "OrFork")
        fork.add_metadata("perf_branch0_traversal_frequency", "40")
        fork.add_metadata("perf_branch0_traversal_percentage", "25% of 160")
        fork.add_metadata("_perf_branch0", "25% of 160")
        return ucm

    def test_metrics_become_extension_elements(self):
        root = parse(serialize_to_string(self._annotated()))
        pay = next(t for t in root.iter(f"{NS_B}task") if t.get("name") == "Pay")
        metrics = {m.get("name"): m.get("value")
                   for m in pay.iter(f"{NS_U}metric")}
        self.assertEqual(metrics, {"frequency": "258", "mean_time": "3.2 d"})
        plain = {m.get("name"): m.get("value")
                 for m in pay.iter(f"{NS_U}metadata")}
        self.assertEqual(plain, {"note": "manual"})
        self.assertEqual(pay.get("name"), "Pay")   # names stay clean

    def test_edge_metrics_land_on_the_flow(self):
        root = parse(serialize_to_string(self._annotated()))
        flows = [f for f in root.iter(f"{NS_B}sequenceFlow")
                 if f.find(f"{NS_B}extensionElements/{NS_U}metric") is not None]
        self.assertEqual(len(flows), 1)
        metrics = {m.get("name"): m.get("value")
                   for m in flows[0].iter(f"{NS_U}metric")}
        self.assertEqual(metrics, {"traversal_frequency": "40",
                                   "traversal_percentage": "25% of 160"})
        # annotate="edges" (default): the overlay text is the flow's name.
        self.assertEqual(flows[0].get("name"), "25% of 160")

    def test_annotate_none_and_all(self):
        ucm = self._annotated()
        root = parse(serialize_to_string(ucm, annotate="none"))
        for f in root.iter(f"{NS_B}sequenceFlow"):
            self.assertIsNone(f.get("name"))
        self.assertEqual(list(root.iter(f"{NS_B}textAnnotation")), [])
        root = parse(serialize_to_string(ucm, annotate="all"))
        notes = list(root.iter(f"{NS_B}textAnnotation"))
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].find(f"{NS_B}text").text, "258 · 3.2 d")
        assoc = next(iter(root.iter(f"{NS_B}association")))
        self.assertEqual(assoc.get("targetRef"), notes[0].get("id"))

    def test_metrics_none_drops_them(self):
        root = parse(serialize_to_string(self._annotated(), metrics="none"))
        self.assertEqual(list(root.iter(f"{NS_U}metric")), [])


NS_COLOR = "{http://www.omg.org/spec/BPMN/non-normative/color/1.0}"
NS_BIOC = "{http://bpmn.io/schema/bpmn/biocolor/1.0}"


class HeatMapTests(unittest.TestCase):
    """The visible heat-map layer: BPMN-in-Color + bioc attributes on the
    diagram shapes and edges, with the PNG renderer's ramps."""

    def _annotated(self):
        ucm = claims_ucm()
        m = ucm.maps[0]
        values = {"Register": "100", "Check A": "40", "Check B": "60",
                  "Review": "100", "Rework": "20", "Pay": "100",
                  "Notify": "100", "Close": "100"}
        times = {"Register": "1h", "Check A": "2d", "Check B": "30m",
                 "Review": "1d", "Rework": "3d", "Pay": "1h",
                 "Notify": "5m", "Close": "1m"}
        for n in m.resp_refs:
            n.add_metadata("perf_frequency", values[n.resp_def.name])
            n.add_metadata("perf_mean_time", times[n.resp_def.name])
        fork = next(n for n in m.nodes if isinstance(n, UCM.OrFork)
                    and n.name == "OrFork")
        fork.add_metadata("perf_branch0_frequency", "40")
        fork.add_metadata("perf_branch1_frequency", "60")
        return ucm

    def _shape_attrs(self, root):
        out = {}
        for sh in root.iter(f"{NS_DI}BPMNShape"):
            out[sh.get("bpmnElement")] = sh
        return out

    def test_no_colours_without_a_heat_map(self):
        xml = serialize_to_string(self._annotated())
        self.assertNotIn("border-color", xml)
        self.assertNotIn("bioc:", xml.split("<bpmn:collaboration")[1])

    def test_node_heat_colours_the_task_shapes(self):
        root = parse(serialize_to_string(self._annotated(),
                                         heatmap_node="frequency"))
        tasks = {t.get("name"): t.get("id") for t in root.iter(f"{NS_B}task")}
        sh = self._shape_attrs(root)
        hot = sh[tasks["Register"]]
        cold = sh[tasks["Rework"]]
        # Count metric: the blue ramp, light to dark, both attribute sets.
        self.assertEqual(hot.get(f"{NS_COLOR}border-color"), "#14378c")
        self.assertEqual(hot.get(f"{NS_BIOC}stroke"), "#14378c")
        self.assertEqual(cold.get(f"{NS_COLOR}border-color"), "#96c3fa")
        self.assertIsNotNone(hot.get(f"{NS_COLOR}background-color"))
        self.assertIsNotNone(hot.get(f"{NS_BIOC}fill"))
        # Gateways and events carry no node heat.
        for g in root.iter(f"{NS_B}exclusiveGateway"):
            self.assertIsNone(sh[g.get("id")].get(f"{NS_COLOR}border-color"))

    def test_time_metric_uses_the_red_ramp(self):
        root = parse(serialize_to_string(self._annotated(),
                                         heatmap_node="mean_time"))
        tasks = {t.get("name"): t.get("id") for t in root.iter(f"{NS_B}task")}
        sh = self._shape_attrs(root)
        self.assertEqual(sh[tasks["Rework"]].get(f"{NS_COLOR}border-color"),
                         "#7f1414")
        self.assertEqual(sh[tasks["Close"]].get(f"{NS_COLOR}border-color"),
                         "#faaaaa")

    def test_edge_heat_colours_the_flows(self):
        root = parse(serialize_to_string(self._annotated(),
                                         heatmap_edge="frequency"))
        coloured = [e for e in root.iter(f"{NS_DI}BPMNEdge")
                    if e.get(f"{NS_COLOR}border-color")]
        self.assertEqual(len(coloured), 2)
        colours = sorted(e.get(f"{NS_COLOR}border-color") for e in coloured)
        self.assertEqual(colours, ["#14378c", "#96c3fa"])
        for e in coloured:
            self.assertEqual(e.get(f"{NS_BIOC}stroke"),
                             e.get(f"{NS_COLOR}border-color"))
            self.assertIsNone(e.get(f"{NS_COLOR}background-color"))

    def test_global_scale_spans_every_map(self):
        ucm = dynamic_stub_ucm()
        work = {n.resp_def.name: n for m in ucm.maps for n in m.resp_refs}
        work["Work0"].add_metadata("perf_frequency", "10")
        work["Work1"].add_metadata("perf_frequency", "90")
        local = parse(serialize_to_string(ucm, heatmap_node="frequency"))
        glob = parse(serialize_to_string(ucm, heatmap_node="frequency",
                                         heatmap_global=True))

        def colour(root, name):
            tid = next(t.get("id") for t in root.iter(f"{NS_B}task")
                       if t.get("name") == name)
            return self._shape_attrs(root)[tid].get(f"{NS_COLOR}border-color")

        # Alone on its map each task is the map's maximum: full emphasis.
        self.assertEqual(colour(local, "Work0"), "#14378c")
        self.assertEqual(colour(local, "Work1"), "#14378c")
        # Against the whole model, Work0 is the minimum.
        self.assertEqual(colour(glob, "Work0"), "#96c3fa")
        self.assertEqual(colour(glob, "Work1"), "#14378c")

    @unittest.skipIf(_lxml is None, "lxml not installed")
    def test_coloured_file_still_validates(self):
        schema = _lxml.XMLSchema(_lxml.parse(XSD))
        xml = serialize_to_string(self._annotated(), heatmap_node="frequency",
                                  heatmap_edge="frequency", heatmap_global=True)
        self.assertTrue(schema.validate(_lxml.fromstring(xml.encode("utf-8"))),
                        "; ".join(str(e) for e in schema.error_log))


class CanonicalSizeTests(unittest.TestCase):

    def _boxes(self, root):
        sh = shapes(root)
        tasks = {t.get("name"): sh[t.get("id")]
                 for t in root.iter(f"{NS_B}task")}
        gateways = [sh[g.get("id")]
                    for tag in ("exclusiveGateway", "parallelGateway")
                    for g in root.iter(f"{NS_B}{tag}")]
        events = [sh[e.get("id")] for tag in ("startEvent", "endEvent")
                  for e in root.iter(f"{NS_B}{tag}")]
        return sh, tasks, gateways, events

    def test_standard_sizes(self):
        root = parse(serialize_to_string(claims_ucm(), sizes="canonical"))
        sh, tasks, gateways, events = self._boxes(root)
        for x, y, w, h in tasks.values():
            self.assertEqual((w, h), bpmn_exporter.CANONICAL_TASK)
        for x, y, w, h in gateways:
            self.assertEqual((w, h), (bpmn_exporter.CANONICAL_GATEWAY,) * 2)
        for x, y, w, h in events:
            self.assertEqual((w, h), (bpmn_exporter.CANONICAL_EVENT,) * 2)

    def test_no_two_shapes_overlap_and_order_is_kept(self):
        ucm = claims_ucm()
        root = parse(serialize_to_string(ucm, sizes="canonical"))
        sh, tasks, gateways, events = self._boxes(root)
        boxes = list(tasks.values()) + gateways + events
        for i, (ax, ay, aw, ah) in enumerate(boxes):
            for bx, by, bw, bh in boxes[i + 1:]:
                separated = (ax + aw <= bx or bx + bw <= ax
                             or ay + ah <= by or by + bh <= ay)
                self.assertTrue(separated, ((ax, ay, aw, ah), (bx, by, bw, bh)))
        # Relative arrangement: a task left of (above) another in the UCM
        # stays left of (above) it.
        centres = {name: (x + w / 2, y + h / 2)
                   for name, (x, y, w, h) in tasks.items()}
        for a in ucm.maps[0].resp_refs:
            for b in ucm.maps[0].resp_refs:
                if a.x < b.x:
                    self.assertLess(centres[a.resp_def.name][0],
                                    centres[b.resp_def.name][0])
                if a.y < b.y:
                    self.assertLess(centres[a.resp_def.name][1],
                                    centres[b.resp_def.name][1])

    def test_lanes_still_tile_and_scale_with_the_layout(self):
        root = parse(serialize_to_string(claims_ucm(), sizes="canonical"))
        sh = shapes(root)
        pool = sh[root.find(f"{NS_B}collaboration/{NS_B}participant").get("id")]
        lanes = [sh[l.get("id")] for l in root.iter(f"{NS_B}lane")]
        bands = sorted((y, y + h) for x, y, w, h in lanes)
        self.assertAlmostEqual(bands[0][0], pool[1])
        self.assertAlmostEqual(bands[-1][1], pool[1] + pool[3])
        for (a0, a1), (b0, b1) in zip(bands, bands[1:]):
            self.assertAlmostEqual(a1, b0)
        # Every task's centre lies inside its lane's band.
        lane_of = {}
        for lane in root.iter(f"{NS_B}lane"):
            for r in lane.findall(f"{NS_B}flowNodeRef"):
                lane_of[r.text] = sh[lane.get("id")]
        for t in root.iter(f"{NS_B}task"):
            x, y, w, h = sh[t.get("id")]
            lx, ly, lw, lh = lane_of[t.get("id")]
            self.assertGreaterEqual(y + h / 2, ly)
            self.assertLessEqual(y + h / 2, ly + lh)

    def test_layout_sizes_are_unchanged_by_default(self):
        root = parse(serialize_to_string(claims_ucm()))
        sh = shapes(root)
        for t in root.iter(f"{NS_B}task"):
            self.assertNotEqual(tuple(sh[t.get("id")][2:]),
                                bpmn_exporter.CANONICAL_TASK)


class FamilyExportTests(unittest.TestCase):
    """``write_bpmn_family``: one .bpmn per cell, the assembled model as
    ``family_umbrella.bpmn``, and an optional family-wide heat-map scale."""

    def _family(self):
        """A stand-in family: two cells with one task each, carrying
        frequencies 10 and 90 so the scale is observable."""
        cells = []
        for i, freq in enumerate(("10", "90")):
            u = convert(op("->", leaf(f"Work{i}")))
            u.maps[0].resp_refs[0].add_metadata("perf_frequency", freq)
            cells.append(NS(ucm=u, labels=(f"c{i}",)))
        fam = NS(cells=cells, file_stem=lambda c: "cell_" + "_".join(c.labels))
        return fam

    def _colour(self, xml, name):
        root = parse(xml)
        tid = next(t.get("id") for t in root.iter(f"{NS_B}task")
                   if t.get("name") == name)
        sh = next(s for s in root.iter(f"{NS_DI}BPMNShape")
                  if s.get("bpmnElement") == tid)
        return sh.get(f"{NS_COLOR}border-color")

    def test_zip_holds_one_file_per_cell_and_the_umbrella(self):
        import zipfile
        fam = self._family()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "fam.zip")
            pm4py_ucm.write_bpmn_family(fam, path, assembled=dynamic_stub_ucm())
            with zipfile.ZipFile(path) as zf:
                names = sorted(zf.namelist())
                umbrella = zf.read("family_umbrella.bpmn").decode("utf-8")
        self.assertEqual(names, ["cell_c0.bpmn", "cell_c1.bpmn",
                                 "family_umbrella.bpmn"])
        self.assertIn("inclusiveGateway", umbrella)

    def test_directory_output_and_no_umbrella(self):
        fam = self._family()
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "fam")
            pm4py_ucm.write_bpmn_family(fam, out)
            self.assertEqual(sorted(os.listdir(out)),
                             ["cell_c0.bpmn", "cell_c1.bpmn"])

    def test_family_scale_shares_one_range(self):
        import zipfile
        fam = self._family()
        with tempfile.TemporaryDirectory() as d:
            local = os.path.join(d, "local.zip")
            shared = os.path.join(d, "shared.zip")
            pm4py_ucm.write_bpmn_family(fam, local, heatmap_node="frequency")
            pm4py_ucm.write_bpmn_family(fam, shared, heatmap_node="frequency",
                                        family_scale=True)
            with zipfile.ZipFile(local) as zf:
                l0 = zf.read("cell_c0.bpmn").decode("utf-8")
            with zipfile.ZipFile(shared) as zf:
                s0 = zf.read("cell_c0.bpmn").decode("utf-8")
                s1 = zf.read("cell_c1.bpmn").decode("utf-8")
        # Alone on its own scale a single task is the maximum.
        self.assertEqual(self._colour(l0, "Work0"), "#14378c")
        # On the family scale cell 0 (10) is the minimum, cell 1 (90) the max.
        self.assertEqual(self._colour(s0, "Work0"), "#96c3fa")
        self.assertEqual(self._colour(s1, "Work1"), "#14378c")

    def test_explicit_span_overrides_global(self):
        ucm = claims_ucm()
        for n in ucm.maps[0].resp_refs:
            n.add_metadata("perf_frequency", "50")
        xml = serialize_to_string(ucm, heatmap_node="frequency",
                                  heatmap_global=True, node_span=(0.0, 100.0))
        self.assertEqual(self._colour(xml, "Pay"),
                         "#%02x%02x%02x" % tuple(
                             round(lo + (hi - lo) * 0.5)
                             for lo, hi in zip((150, 195, 250), (20, 55, 140))))


def decomposed_ucm():
    tree = op("->", op("->", leaf("A1"), leaf("A2"), leaf("A3")),
              op("X", op("->", leaf("B1"), leaf("B2"), leaf("B3")),
                 op("->", leaf("C1"), leaf("C2"), leaf("C3"))),
              op("->", leaf("D1"), leaf("D2"), leaf("D3")))
    ucm = decomposition.apply(tree, {"decomposition": "aggressive"})
    ucm.bind_performers({"A1": "Intake", "A2": "Intake", "A3": "Intake",
                         "B1": "Desk", "B2": "Desk", "B3": "Desk",
                         "C1": "Field", "C2": "Field", "C3": "Field",
                         "D1": "Intake", "D2": "Intake", "D3": "Intake"})
    return ucm


class InlineStubTests(unittest.TestCase):
    """``stubs="inline"``: plug-ins nest under their stub as collapsed
    sub-processes, each with its own plane; no call activities remain."""

    def test_static_stubs_become_nested_sub_processes(self):
        ucm = decomposed_ucm()
        root = parse(serialize_to_string(ucm, stubs="inline"))
        processes = root.findall(f"{NS_B}process")
        self.assertEqual(len(processes), 1)          # only the root map
        self.assertEqual(list(root.iter(f"{NS_B}callActivity")), [])
        subs = list(root.iter(f"{NS_B}subProcess"))
        n_stubs = sum(isinstance(n, UCM.Stub) for m in ucm.maps for n in m.nodes)
        self.assertEqual(len(subs), n_stubs)
        for sub in subs:
            # The plug-in's body is inside: tasks, events, and its lanes.
            self.assertTrue(sub.findall(f"{NS_B}task"))
            self.assertEqual(len(sub.findall(f"{NS_B}startEvent")), 1)
            self.assertTrue(sub.find(f"{NS_B}laneSet") is not None)
        # One plane per nested sub-process, each with the sub-process as
        # its element, and collapsed in the parent.
        planes = {pl.get("bpmnElement") for pl in root.iter(f"{NS_DI}BPMNPlane")}
        shapes_by_el = {sh.get("bpmnElement"): sh
                        for sh in root.iter(f"{NS_DI}BPMNShape")}
        for sub in subs:
            self.assertIn(sub.get("id"), planes)
            self.assertEqual(shapes_by_el[sub.get("id")].get("isExpanded"), "false")
        # Every task is in exactly one lane somewhere.
        tasks = {t.get("id") for t in root.iter(f"{NS_B}task")}
        refs = [r.text for r in root.iter(f"{NS_B}flowNodeRef")]
        self.assertTrue(tasks <= set(refs))

    def test_dynamic_stub_nests_one_sub_process_per_plugin(self):
        root = parse(serialize_to_string(dynamic_stub_ucm(), stubs="inline"))
        self.assertEqual(len(root.findall(f"{NS_B}process")), 1)
        outer = next(iter(root.iter(f"{NS_B}subProcess")))
        inner = outer.findall(f"{NS_B}subProcess")
        self.assertEqual(sorted(i.get("name") for i in inner), ["Plug0", "Plug1"])
        for i in inner:
            self.assertEqual([t.get("name") for t in i.findall(f"{NS_B}task")],
                             [f"Work{i.get('name')[-1]}"])
        self.assertEqual(list(root.iter(f"{NS_B}callActivity")), [])
        planes = {pl.get("bpmnElement") for pl in root.iter(f"{NS_DI}BPMNPlane")}
        for i in inner:
            self.assertIn(i.get("id"), planes)

    def test_pm4py_reads_an_inline_decomposed_model(self):
        if _pm4py is None:
            self.skipTest("pm4py not installed")
        ucm = decomposed_ucm()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inline.bpmn")
            pm4py_ucm.write_bpmn(ucm, path, stubs="inline")
            graph = _pm4py.read_bpmn(path)
        names = sorted(n.get_name() for n in graph.get_nodes()
                       if type(n).__name__ == "Task")
        self.assertEqual(names, [f"{a}{i}" for a in "ABCD" for i in (1, 2, 3)])
        self.assertTrue(any(type(n).__name__ == "SubProcess"
                            for n in graph.get_nodes()))

    @unittest.skipIf(_lxml is None, "lxml not installed")
    def test_inline_files_validate(self):
        schema = _lxml.XMLSchema(_lxml.parse(XSD))
        for ucm in (decomposed_ucm(), dynamic_stub_ucm()):
            xml = serialize_to_string(ucm, stubs="inline")
            self.assertTrue(schema.validate(_lxml.fromstring(xml.encode("utf-8"))),
                            "; ".join(str(e) for e in schema.error_log))


class DataObjectTests(unittest.TestCase):

    def _ucm(self):
        ucm = claims_ucm()
        enum = ucm.get_or_add_enumeration_type("Region", ["EU", "US"])
        ucm.get_or_add_variable("region", "enumeration", enum)
        ucm.get_or_add_variable("retries", "integer")
        group = ucm.add_scenario_group("Mined")
        sc = UCM.ScenarioDef(name="EU fast")
        group.scenarios.append(sc)
        m = ucm.maps[0]
        sc.initializations.append(UCM.Initialization(ucm.variables[0], "EU"))
        sc.start_points.append(UCM.ScenarioStartPoint(m.start_points[0], True))
        sc.end_points.append(UCM.ScenarioEndPoint(m.end_points[0], True, True))
        return ucm

    def test_none_by_default(self):
        xml = serialize_to_string(self._ucm())
        self.assertNotIn("dataObject", xml)
        self.assertNotIn("Scenario Mined", xml)

    def test_variables_become_data_objects_with_shapes(self):
        root = parse(serialize_to_string(self._ucm(), data="objects"))
        objs = {o.get("name"): o for o in root.iter(f"{NS_B}dataObject")}
        self.assertEqual(sorted(objs), ["region", "retries"])
        self.assertIn("EU, US", objs["region"].find(f"{NS_B}documentation").text)
        refs = list(root.iter(f"{NS_B}dataObjectReference"))
        self.assertEqual(len(refs), 2)
        sh = shapes(root)
        pool = sh[root.find(f"{NS_B}collaboration/{NS_B}participant").get("id")]
        for r in refs:
            self.assertEqual(r.get("dataObjectRef"), objs[r.get("name")].get("id"))
            x, y, w, h = sh[r.get("id")]
            self.assertLess(y + h, pool[1])          # in the strip above the pool
        docs = [d.text for d in root.find(f"{NS_B}process").findall(f"{NS_B}documentation")]
        self.assertTrue(any(d.startswith("Scenario Mined / EU fast: region = EU")
                            for d in docs))

    @unittest.skipIf(_lxml is None, "lxml not installed")
    def test_data_file_validates(self):
        schema = _lxml.XMLSchema(_lxml.parse(XSD))
        xml = serialize_to_string(self._ucm(), data="objects")
        self.assertTrue(schema.validate(_lxml.fromstring(xml.encode("utf-8"))),
                        "; ".join(str(e) for e in schema.error_log))


class RoutingTests(unittest.TestCase):

    def _segments(self, root):
        for e in root.iter(f"{NS_DI}BPMNEdge"):
            pts = [(float(w.get("x")), float(w.get("y")))
                   for w in e.findall(f"{NS_DD}waypoint")]
            for a, b in zip(pts, pts[1:]):
                yield e.get("bpmnElement"), a, b

    def test_straight_keeps_slanted_segments(self):
        root = parse(serialize_to_string(claims_ucm()))
        self.assertTrue(any(a[0] != b[0] and a[1] != b[1]
                            for _, a, b in self._segments(root)))

    def test_orthogonal_segments_are_axis_aligned(self):
        root = parse(serialize_to_string(claims_ucm(), routing="orthogonal"))
        for fid, a, b in self._segments(root):
            self.assertTrue(abs(a[0] - b[0]) < 1e-6 or abs(a[1] - b[1]) < 1e-6,
                            (fid, a, b))

    def test_orthogonal_back_edge_leaves_vertically(self):
        from pm4py_ucm.objects.ucm.exporter.variants.bpmn import _route
        # forward slant: horizontal first; backward slant: vertical first
        self.assertEqual(_route([(0, 0), (10, 5)], "orthogonal"),
                         [(0, 0), (10, 0), (10, 5)])
        self.assertEqual(_route([(10, 0), (0, 5)], "orthogonal"),
                         [(10, 0), (10, 5), (0, 5)])
        self.assertEqual(_route([(0, 0), (10, 0)], "orthogonal"),
                         [(0, 0), (10, 0)])

    @unittest.skipIf(_lxml is None, "lxml not installed")
    def test_orthogonal_file_validates(self):
        schema = _lxml.XMLSchema(_lxml.parse(XSD))
        xml = serialize_to_string(claims_ucm(), routing="orthogonal")
        self.assertTrue(schema.validate(_lxml.fromstring(xml.encode("utf-8"))),
                        "; ".join(str(e) for e in schema.error_log))


class ApiTests(unittest.TestCase):

    def test_convert_to_bpmn_xml_matches_write(self):
        ucm = claims_ucm()
        text = pm4py_ucm.convert_to_bpmn_xml(ucm)
        self.assertTrue(text.startswith("<?xml"))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.bpmn")
            pm4py_ucm.write_bpmn(ucm, path)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), text)

    def test_validation_refuses_a_malformed_model(self):
        u = UCM(name="bad")
        m = u.add_map(UCM.UCMmap(name="M"))
        m.add_node(UCM.StartPoint(name="dangling"))
        with self.assertRaises(ValueError):
            pm4py_ucm.convert_to_bpmn_xml(u)
        pm4py_ucm.convert_to_bpmn_xml(u, validate=False)


if __name__ == "__main__":
    unittest.main()
