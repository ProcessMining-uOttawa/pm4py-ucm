"""Tests for stub plug-in binding round-trip.

A UCM stub is a placeholder for a sub-map (called a plug-in). The
``<bindings>`` element on the stub names the plug-in map and connects
parent-side arcs to plug-in-side start/end points. These tests cover
the export/import of that binding shape end-to-end."""

import os
import re
import unittest

from pm4py_ucm import UCM
from pm4py_ucm.objects.ucm.exporter.variants.jucm import serialize_to_string
from pm4py_ucm.objects.ucm.importer.variants.jucm import (
    apply as import_jucm, parse_string,
)


# A jUCMNav-authored model (ISO-8859-1, specVersion 6), kept verbatim as
# the reference for the binding XPaths below: these tests exist to match
# what jUCMNav itself writes, so this file must never be regenerated from
# our own exporter's output.
_REFERENCE_FILE = os.path.join(
    os.path.dirname(__file__), "fixtures", "SimpleExampleWithStub.jucm")


class StubBindingObjectModelTests(unittest.TestCase):
    """Building a stub-with-plugin model directly in Python works."""

    def test_can_build_stub_with_plugin(self):
        ucm = UCM(name="X")
        root = ucm.add_map(name="Root")
        sub = ucm.add_map(name="Sub")

        # Root: start -> Stub -> end
        s = root.add_node(UCM.StartPoint(name="start"))
        stub = root.add_node(UCM.Stub(name="MyStub"))
        e = root.add_node(UCM.EndPoint(name="end"))
        in_arc = root.add_connection(s, stub)
        out_arc = root.add_connection(stub, e)

        # Sub: sub_start -> A -> sub_end
        ss = sub.add_node(UCM.StartPoint(name="ss"))
        a = sub.add_node(UCM.RespRef(resp_def=ucm.get_or_add_responsibility("A")))
        se = sub.add_node(UCM.EndPoint(name="se"))
        sub.add_connection(ss, a)
        sub.add_connection(a, se)

        binding = UCM.PluginBinding(stub=stub, plugin=sub)
        binding.add_in(parent_connection=in_arc, plugin_start=ss)
        binding.add_out(plugin_end=se, parent_connection=out_arc)
        stub.bindings.append(binding)

        # Object model wired correctly.
        self.assertEqual(len(stub.bindings), 1)
        b = stub.bindings[0]
        self.assertIs(b.plugin, sub)
        self.assertEqual(len(b.in_bindings), 1)
        self.assertIs(b.in_bindings[0].stub_entry, in_arc)
        self.assertIs(b.in_bindings[0].start_point, ss)
        self.assertEqual(len(b.out_bindings), 1)
        self.assertIs(b.out_bindings[0].end_point, se)
        self.assertIs(b.out_bindings[0].stub_exit, out_arc)


class StubBindingExportTests(unittest.TestCase):
    """The exporter writes the binding shape jUCMNav expects.
    These fixtures are deliberately **fragments**, not whole UCMs: the
    nodes are unconnected because each assertion is about one serialised
    attribute, not about a model anyone would open. The exporter refuses a
    structurally malformed model by default, so they pass
    ``validate=False`` — the escape hatch exists for exactly this, and for
    round-tripping a file authored elsewhere.
    """

    def _build(self):
        ucm = UCM(name="WithStub")
        root = ucm.add_map(name="Root")
        sub = ucm.add_map(name="Sub")
        s = root.add_node(UCM.StartPoint(name="start"))
        stub = root.add_node(UCM.Stub(name="MyStub"))
        e = root.add_node(UCM.EndPoint(name="end"))
        in_arc = root.add_connection(s, stub)
        out_arc = root.add_connection(stub, e)

        ss = sub.add_node(UCM.StartPoint(name="ss"))
        a = sub.add_node(UCM.RespRef(resp_def=ucm.get_or_add_responsibility("A")))
        se = sub.add_node(UCM.EndPoint(name="se"))
        sub.add_connection(ss, a)
        sub.add_connection(a, se)

        binding = UCM.PluginBinding(stub=stub, plugin=sub)
        binding.add_in(parent_connection=in_arc, plugin_start=ss)
        binding.add_out(plugin_end=se, parent_connection=out_arc)
        stub.bindings.append(binding)
        return ucm

    def test_emits_bindings_element_with_plugin_id(self):
        xml = serialize_to_string(self._build(), validate=False)
        self.assertIn("<bindings", xml)
        # plugin attribute carries the plug-in map's integer ID
        self.assertRegex(xml, r'<bindings plugin="\d+">')

    def test_emits_in_and_out_children(self):
        xml = serialize_to_string(self._build(), validate=False)
        self.assertRegex(xml,
            r'<in startPoint="\d+" stubEntry="//@urndef/@specDiagrams\.\d+/@connections\.\d+"/>')
        self.assertRegex(xml,
            r'<out endPoint="\d+" stubExit="//@urndef/@specDiagrams\.\d+/@connections\.\d+"/>')

    def test_emits_parent_stub_on_plugin_map(self):
        """The plug-in ``specDiagrams`` element gets a ``parentStub``
        attribute pointing back into the bindings element."""
        xml = serialize_to_string(self._build(), validate=False)
        self.assertRegex(xml,
            r'<specDiagrams[^>]*name="Sub"[^>]*parentStub="//@urndef/@specDiagrams\.\d+/@nodes\.\d+/@bindings\.0"')

    def test_emits_in_bindings_on_entry_connection(self):
        """The parent-map connection entering the stub gets an
        ``inBindings`` attribute pointing to its ``<in>`` element."""
        xml = serialize_to_string(self._build(), validate=False)
        self.assertRegex(xml,
            r'<connections[^>]*target="\d+"[^>]*inBindings="//@urndef/@specDiagrams\.\d+/@nodes\.\d+/@bindings\.0/@in\.0"')

    def test_emits_out_bindings_on_exit_connection(self):
        xml = serialize_to_string(self._build(), validate=False)
        self.assertRegex(xml,
            r'<connections[^>]*source="\d+"[^>]*outBindings="//@urndef/@specDiagrams\.\d+/@nodes\.\d+/@bindings\.0/@out\.0"')

    def test_emits_inbindings_on_plugin_start_point(self):
        xml = serialize_to_string(self._build(), validate=False)
        # The plug-in StartPoint should carry inBindings.
        self.assertRegex(xml,
            r'<nodes[^>]*xsi:type="ucm\.map:StartPoint"[^>]*name="ss"[^>]*inBindings="')

    def test_emits_outbindings_on_plugin_end_point(self):
        xml = serialize_to_string(self._build(), validate=False)
        self.assertRegex(xml,
            r'<nodes[^>]*xsi:type="ucm\.map:EndPoint"[^>]*name="se"[^>]*outBindings="')

    def test_dynamic_stub_emits_dynamic_attribute(self):
        ucm = UCM(name="D")
        m = ucm.add_map(name="M")
        stub = m.add_node(UCM.Stub(name="Dyn", dynamic=True))
        xml = serialize_to_string(ucm, validate=False)
        self.assertIn('dynamic="true"', xml)

    def test_static_stub_omits_dynamic_attribute(self):
        """Default static stubs match jUCMNav's output, which never
        writes ``dynamic="false"`` — it's the default."""
        ucm = UCM(name="S")
        m = ucm.add_map(name="M")
        m.add_node(UCM.Stub(name="Stat", dynamic=False))
        xml = serialize_to_string(ucm, validate=False)
        self.assertNotIn('dynamic="false"', xml)


@unittest.skipUnless(os.path.exists(_REFERENCE_FILE),
                     "reference SimpleExampleWithStub.jucm not present")
class ReferenceFileRoundTripTests(unittest.TestCase):
    """Round-trip the user-provided reference file."""

    def test_imports_with_one_stub_one_binding(self):
        ucm = import_jucm(_REFERENCE_FILE)
        self.assertEqual([m.name for m in ucm.maps],
                          ["UCMmap2", "PlugInMap"])
        stubs = [n for m in ucm.maps for n in m.nodes
                 if isinstance(n, UCM.Stub)]
        self.assertEqual(len(stubs), 1)
        self.assertEqual(stubs[0].name, "StubExample")
        self.assertEqual(len(stubs[0].bindings), 1)
        b = stubs[0].bindings[0]
        self.assertEqual(b.plugin.name, "PlugInMap")
        self.assertEqual(len(b.in_bindings), 1)
        self.assertEqual(len(b.out_bindings), 1)

    def test_round_trip_is_byte_stable(self):
        """Re-exporting the imported reference file, then re-importing
        and re-exporting, produces byte-identical XML (modulo the two
        timestamp attributes)."""
        ucm1 = import_jucm(_REFERENCE_FILE)
        xml1 = serialize_to_string(ucm1, layout=False)
        ucm2 = parse_string(xml1)
        xml2 = serialize_to_string(ucm2, layout=False)
        strip = lambda s: re.sub(r' (created|modified)="[^"]*"', '', s)
        self.assertEqual(strip(xml1), strip(xml2))

    def test_binding_xpaths_match_reference(self):
        """The XPaths we emit for the binding cross-references match
        exactly what jUCMNav itself wrote in the reference file."""
        ucm = import_jucm(_REFERENCE_FILE)
        xml = serialize_to_string(ucm, layout=False)
        # These three XPaths appear verbatim in the original file.
        for expected in [
            'parentStub="//@urndef/@specDiagrams.0/@nodes.9/@bindings.0"',
            'inBindings="//@urndef/@specDiagrams.0/@nodes.9/@bindings.0/@in.0"',
            'outBindings="//@urndef/@specDiagrams.0/@nodes.9/@bindings.0/@out.0"',
        ]:
            self.assertIn(expected, xml)


class StubBindingImportTests(unittest.TestCase):
    """The importer resolves binding cross-references into live objects."""

    def test_round_trip_of_synthesised_model(self):
        ucm1 = UCM(name="X")
        root = ucm1.add_map(name="Root")
        sub = ucm1.add_map(name="Sub")
        s = root.add_node(UCM.StartPoint(name="start"))
        stub = root.add_node(UCM.Stub(name="S"))
        e = root.add_node(UCM.EndPoint(name="end"))
        in_arc = root.add_connection(s, stub)
        out_arc = root.add_connection(stub, e)
        ss = sub.add_node(UCM.StartPoint(name="ss"))
        a = sub.add_node(UCM.RespRef(resp_def=ucm1.get_or_add_responsibility("A")))
        se = sub.add_node(UCM.EndPoint(name="se"))
        sub.add_connection(ss, a); sub.add_connection(a, se)
        b = UCM.PluginBinding(stub=stub, plugin=sub)
        b.add_in(parent_connection=in_arc, plugin_start=ss)
        b.add_out(plugin_end=se, parent_connection=out_arc)
        stub.bindings.append(b)

        xml = serialize_to_string(ucm1, layout=False)
        ucm2 = parse_string(xml)

        s2 = next(n for m in ucm2.maps for n in m.nodes
                  if isinstance(n, UCM.Stub))
        self.assertEqual(len(s2.bindings), 1)
        b2 = s2.bindings[0]
        # Live cross-references re-resolved through the importer:
        self.assertIs(b2.plugin, ucm2.maps[1])
        self.assertEqual(len(b2.in_bindings), 1)
        self.assertIs(b2.in_bindings[0].start_point,
                       next(n for n in ucm2.maps[1].nodes
                            if n.name == "ss"))
        self.assertIs(b2.in_bindings[0].stub_entry,
                       ucm2.maps[0].connections[0])
        self.assertIs(b2.out_bindings[0].end_point,
                       next(n for n in ucm2.maps[1].nodes
                            if n.name == "se"))
        self.assertIs(b2.out_bindings[0].stub_exit,
                       ucm2.maps[0].connections[1])

    def test_unresolvable_binding_returns_none(self):
        """A ``<bindings>`` element whose ``plugin`` attribute cannot
        be resolved is silently dropped — the model still loads."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<urn:URNspec xmlns:xmi="http://www.omg.org/XMI" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:ucm.map="http:///ucm/map.ecore" xmlns:urn="http:///urn.ecore" name="X" specVersion="6" urnVersion="1.27" nextGlobalID="10">
<ucmspec/><grlspec><impactModel/><featureModel/></grlspec>
<urndef>
 <specDiagrams xsi:type="ucm.map:UCMmap" name="M" id="1">
  <nodes xsi:type="ucm.map:Stub" name="S" id="2" x="0" y="0">
   <label/>
   <bindings plugin="9999"/>
  </nodes>
 </specDiagrams>
</urndef>
</urn:URNspec>"""
        ucm = parse_string(xml)
        stub = next(n for n in ucm.maps[0].nodes if isinstance(n, UCM.Stub))
        # Binding silently dropped; model loaded successfully.
        self.assertEqual(stub.bindings, [])


if __name__ == "__main__":
    unittest.main()
