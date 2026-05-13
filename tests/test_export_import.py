"""Tests for the jUCMNav-compatible XMI exporter and importer.

These tests check structural integrity (round-trip preservation of nodes,
edges, responsibilities, and connections), determinism (re-exporting an
imported model yields byte-identical XML), and well-formedness of the
emitted XMI.
"""

import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace as NS

from pm4py_ucm import UCM, read_ucm, write_ucm
from pm4py_ucm.objects.ucm.exporter.variants.jucm import serialize_to_string
from pm4py_ucm.objects.ucm.importer.variants.jucm import parse_string
from pm4py_ucm.objects.ucm.conversion import from_process_tree as conv


def leaf(label):
    return NS(operator=None, children=[], label=label)


def node(op, *children):
    return NS(operator=NS(value=op), children=list(children), label=None)


def small_ucm():
    return conv.apply(node("->", leaf("A"),
                           node("X", leaf("B"), leaf("C")),
                           leaf("D")))


class ExportImportTests(unittest.TestCase):

    def test_export_produces_valid_xml(self):
        ucm = small_ucm()
        xml = serialize_to_string(ucm)
        # Parse with a stdlib parser to confirm well-formedness
        root = ET.fromstring(xml)
        self.assertTrue(root.tag.endswith("URNspec"))

    def test_export_declares_required_namespaces(self):
        """The modern jUCMNav format declares four namespaces on the root:
        ``xmi``, ``xsi``, ``urn`` (with URI ``http:///urn.ecore``), and
        ``ucm.map`` (with URI ``http:///ucm/map.ecore``). It does **not**
        declare ``urncore`` or ``grl`` — those concepts use unqualified
        element names since they live inside the URN containment tree."""
        ucm = small_ucm()
        xml = serialize_to_string(ucm)
        for prefix in ("xmi", "xsi", "urn", "ucm.map"):
            self.assertIn(f"xmlns:{prefix}=", xml,
                          f"missing xmlns:{prefix} declaration")
        # Spot-check the URI form: must be the opaque ecore form, not the
        # bare package name we used in earlier (broken) versions.
        self.assertIn('xmlns:urn="http:///urn.ecore"', xml)
        self.assertIn('xmlns:ucm.map="http:///ucm/map.ecore"', xml)
        # The deprecated urncore / grl namespace prefixes should be absent.
        self.assertNotIn("xmlns:urncore=", xml)
        self.assertNotIn("xmlns:grl=", xml)

    def test_root_attributes_and_ordering(self):
        """The root carries the URN-spec metadata as attributes (not child
        elements) and the children appear in the canonical order
        ucmspec → grlspec → urndef."""
        ucm = small_ucm()
        xml = serialize_to_string(ucm)
        # Version/spec metadata are attributes on the root, not child elements.
        self.assertIn('specVersion="4"', xml)
        self.assertIn('urnVersion="1.27"', xml)
        self.assertIn("nextGlobalID=", xml)
        # No <urnVersion> child element from the old layout.
        self.assertNotIn("<urnVersion>", xml)
        # Children appear in the required order.
        self.assertLess(xml.index("<ucmspec"), xml.index("<grlspec"))
        self.assertLess(xml.index("<grlspec"), xml.index("<urndef"))

    def test_next_global_id_is_max_plus_one(self):
        """``nextGlobalID`` must equal ``max(all IDs) + 1`` — the value
        jUCMNav would assign to the next newly-created element."""
        ucm = small_ucm()
        xml = serialize_to_string(ucm)
        # Find the nextGlobalID attribute value
        import re as _re
        m = _re.search(r'nextGlobalID="(\d+)"', xml)
        self.assertIsNotNone(m)
        next_id = int(m.group(1))
        self.assertEqual(next_id, ucm.max_id() + 1)

    def test_connections_are_anonymous_with_integer_refs(self):
        """``<connections>`` elements carry no ``id`` and reference their
        endpoints by integer node IDs (not XPath fragments)."""
        ucm = small_ucm()
        xml = serialize_to_string(ucm)
        root = ET.fromstring(xml)
        diag = root.find(".//specDiagrams")
        for conn in diag.findall("connections"):
            self.assertIsNone(conn.get("id"),
                              "connections must not carry an id attribute")
            self.assertTrue(conn.get("source", "").isdigit(),
                            f"source must be integer ID, got {conn.get('source')}")
            self.assertTrue(conn.get("target", "").isdigit(),
                            f"target must be integer ID, got {conn.get('target')}")

    def test_node_succ_pred_use_xpath_fragments(self):
        """``succ``/``pred`` on nodes reference anonymous connections via
        XPath fragments — the *only* reference style for the connections,
        because they carry no ID."""
        ucm = small_ucm()
        xml = serialize_to_string(ucm)
        root = ET.fromstring(xml)
        diag = root.find(".//specDiagrams")
        found_succ = False
        for node_el in diag.findall("nodes"):
            succ = node_el.get("succ", "")
            if succ:
                found_succ = True
                for token in succ.split():
                    self.assertTrue(
                        token.startswith("//@urndef/@specDiagrams."),
                        f"succ token must be an XPath fragment, got {token!r}")
        self.assertTrue(found_succ, "expected at least one node with succ")
        # Legacy names from the old format must not appear.
        self.assertNotIn("succConnections=", xml)
        self.assertNotIn("predConnections=", xml)

    def test_definitions_carry_back_references(self):
        """Each responsibility definition lists the IDs of the RespRefs
        that point at it (``respRefs="36 39"``); each component definition
        lists the IDs of its ComponentRefs (``contRefs="14 42"``)."""
        ucm = small_ucm()
        # Add a component definition referenced from a fresh ComponentRef
        comp = ucm.get_or_add_component("System")
        ucm.maps[0].add_component_ref(comp, x=0, y=0, width=100, height=100)
        xml = serialize_to_string(ucm)
        root = ET.fromstring(xml)
        urndef = root.find("urndef")
        # Every responsibility used in the map has a respRefs back-reference.
        for r_el in urndef.findall("responsibilities"):
            self.assertIsNotNone(
                r_el.get("respRefs"),
                f"Responsibility {r_el.get('name')} missing respRefs back-ref")
        # Components used in the map have a contRefs back-reference.
        for c_el in urndef.findall("components"):
            if c_el.get("name") == "System":
                self.assertIsNotNone(c_el.get("contRefs"))

    def test_condition_label_distinct_from_expression(self):
        """A connection's ``<condition>`` puts the human-readable name in
        ``label`` and the logical guard in ``expression`` (default
        ``"true"``). The two are distinct fields."""
        ucm = small_ucm()
        xml = serialize_to_string(ucm)
        root = ET.fromstring(xml)
        diag = root.find(".//specDiagrams")
        labels_found = []
        for conn in diag.findall("connections"):
            cond = conn.find("condition")
            if cond is not None:
                labels_found.append(cond.get("label"))
                # The expression defaults to "true" even when label is set.
                self.assertEqual(cond.get("expression"), "true")
        self.assertTrue(any(l for l in labels_found),
                        "expected at least one condition with a non-empty label")

    def test_reference_jucm_file_round_trips(self):
        """The canonical SimpleExample.jucm reference file imports, exports,
        and re-imports without loss — proving the parser/writer pair fully
        understands the modern jUCMNav format."""
        import os.path
        here = os.path.dirname(__file__)
        ref_path = os.path.join(here, "fixtures", "SimpleExample.jucm")
        if not os.path.exists(ref_path):
            self.skipTest(f"reference fixture not present: {ref_path}")
        ucm1 = read_ucm(ref_path)
        # Structural facts the reference file establishes:
        self.assertEqual(len(ucm1.responsibilities), 3)
        self.assertEqual(len(ucm1.components), 3)
        # Actor is the only non-Team kind.
        kinds = sorted(c.kind.value for c in ucm1.components)
        self.assertEqual(kinds, ["Actor", "Team", "Team"])
        m = ucm1.maps[0]
        self.assertEqual(len(m.nodes), 10)
        self.assertEqual(len(m.cont_refs), 3)
        self.assertEqual(len(m.connections), 10)
        # The Sub-Component reference nests inside the Component reference.
        sub = next(cr for cr in m.cont_refs if cr.cont_def.name == "Sub-Component")
        self.assertIsNotNone(sub.parent)
        self.assertEqual(sub.parent.cont_def.name, "Component")
        # Conditions preserve label/expression separation.
        labelled = [c for c in m.connections if c.condition and c.condition.label]
        self.assertEqual(
            sorted(c.condition.label for c in labelled),
            ["FalseBranch", "TrueBranch"])
        for c in labelled:
            if c.condition.label == "TrueBranch":
                self.assertEqual(c.condition.expression, "true")
            elif c.condition.label == "FalseBranch":
                self.assertEqual(c.condition.expression, "!true")
        # Round-trip is deterministic.
        xml1 = serialize_to_string(ucm1, layout=False)
        ucm2 = parse_string(xml1)
        xml2 = serialize_to_string(ucm2, layout=False)
        # The timestamps differ between exports, so strip them before compare.
        import re as _re
        strip = lambda s: _re.sub(r' (created|modified)="[^"]*"', '', s)
        self.assertEqual(strip(xml1), strip(xml2))

    def test_roundtrip_preserves_structure(self):
        ucm = small_ucm()
        xml = serialize_to_string(ucm)
        ucm2 = parse_string(xml)
        m1 = ucm.maps[0]
        m2 = ucm2.maps[0]
        self.assertEqual(len(m1.nodes), len(m2.nodes))
        self.assertEqual(len(m1.connections), len(m2.connections))
        self.assertEqual(
            sorted(r.name for r in ucm.responsibilities),
            sorted(r.name for r in ucm2.responsibilities),
        )

    def test_roundtrip_is_deterministic(self):
        ucm = small_ucm()
        xml1 = serialize_to_string(ucm)
        ucm2 = parse_string(xml1)
        xml2 = serialize_to_string(ucm2)
        self.assertEqual(xml1, xml2)

    def test_write_ucm_to_disk_and_read_back(self):
        ucm = small_ucm()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "test.jucm")
            written = write_ucm(ucm, path)
            self.assertEqual(written, path)
            self.assertTrue(os.path.exists(path))
            ucm2 = read_ucm(path)
            self.assertEqual(len(ucm2.maps[0].nodes),
                             len(ucm.maps[0].nodes))

    def test_resp_ref_links_resolve_after_import(self):
        ucm = small_ucm()
        xml = serialize_to_string(ucm)
        ucm2 = parse_string(xml)
        for n in ucm2.maps[0].nodes:
            if isinstance(n, UCM.RespRef):
                self.assertIsNotNone(n.resp_def)
                self.assertIn(n.resp_def, ucm2.responsibilities)


if __name__ == "__main__":
    unittest.main()
