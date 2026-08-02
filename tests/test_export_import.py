"""Tests for the jUCMNav-compatible XMI exporter and importer.

These tests check structural integrity (round-trip preservation of nodes,
edges, responsibilities, and connections), determinism (re-exporting an
imported model yields byte-identical XML), and well-formedness of the
emitted XMI.
"""

import os
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace as NS

from pm4py_ucm import UCM, read_ucm, write_ucm
from pm4py_ucm.objects.ucm.exporter.variants.jucm import serialize_to_string
from pm4py_ucm.objects.ucm.importer.variants.jucm import (
    _forbid_dtd,
    parse_string,
)
from pm4py_ucm.objects.ucm.conversion import from_process_tree as conv


def leaf(label):
    return NS(operator=None, children=[], label=label)


def node(op, *children):
    return NS(operator=NS(value=op), children=list(children), label=None)


def small_ucm():
    return conv.apply(node("->", leaf("A"),
                           node("X", leaf("B"), leaf("C")),
                           leaf("D")))


class ExportFilterTests(unittest.TestCase):
    """Three .jucm-only export filters introduced for jUCMNav readability:

    1. ``<condition>`` elements are suppressed by default.
    2. EmptyPoints preceding a loop's OR-join are emitted as
       DirectionArrows.
    3. ComponentRefs that have nothing bound to them are omitted.
    """

    def test_conditions_suppressed_by_default(self):
        ucm = small_ucm()
        xml = serialize_to_string(ucm)
        # The default no longer emits ``<condition>`` children. The
        # ``connections`` elements should be empty (self-closing).
        self.assertNotIn("<condition", xml)

    def test_conditions_can_be_enabled(self):
        ucm = small_ucm()
        xml = serialize_to_string(ucm, emit_conditions=True)
        self.assertIn("<condition", xml)

    def test_pre_loop_join_empty_points_emitted_as_direction_arrows(self):
        """A process tree with a loop has, after routing-point insertion,
        two ``EmptyPoint`` "Bend" nodes feeding the ``LoopJoin``. Both
        must be emitted with ``xsi:type="ucm.map:DirectionArrow"``."""
        tree = NS(operator=NS(value="*"),
                  children=[leaf("body"), leaf(None)],
                  label=None)
        ucm = conv.apply(tree)
        xml = serialize_to_string(ucm, layout=False)
        # ``ucm.map:DirectionArrow`` should appear at least twice — one
        # on the entry side of the LoopJoin, one on the redo back-edge.
        self.assertGreaterEqual(
            xml.count("ucm.map:DirectionArrow"), 2,
            "expected ≥2 DirectionArrows around the loop's OR-join",
        )

    def test_component_color_attributes_emitted(self):
        """Each ``<components>`` definition carries ``lineColor``,
        ``fillColor`` and ``filled="true"`` attributes (RGB triplets)
        — the standard jUCMNav attributes for per-component colours.
        The colour is shared across every ``<contRefs>`` that
        references this definition."""
        ucm = UCM(name="L")
        m = ucm.add_map(name="M")
        comp = ucm.get_or_add_component("Team")
        cr = m.add_component_ref(comp)
        a = m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility("A"), name="A",
        ))
        a.cont_ref = cr
        xml = serialize_to_string(ucm, layout=False)
        # Component definition has lineColor + fillColor + filled.
        self.assertRegex(xml,
            r'<components [^>]*name="Team"[^>]*'
            r'lineColor="\d+,\d+,\d+"[^>]*'
            r'fillColor="\d+,\d+,\d+"[^>]*'
            r'filled="true"')

    def test_component_color_deterministic_by_name(self):
        """The same component name always picks the same colour pair."""
        def _colors_for(name):
            ucm = UCM(name="L")
            m = ucm.add_map(name="M")
            comp = ucm.get_or_add_component(name)
            cr = m.add_component_ref(comp)
            a = m.add_node(UCM.RespRef(
                resp_def=ucm.get_or_add_responsibility("A"), name="A",
            ))
            a.cont_ref = cr
            xml = serialize_to_string(ucm, layout=False)
            import re
            line = re.search(r'lineColor="([^"]+)"', xml).group(1)
            fill = re.search(r'fillColor="([^"]+)"', xml).group(1)
            return line, fill

        self.assertEqual(_colors_for("Alpha"), _colors_for("Alpha"))
        # Sanity check: a different name picks (almost always) a
        # different palette slot.
        self.assertNotEqual(_colors_for("Alpha"), _colors_for("Beta"))

    def test_unbound_label_above_when_clear(self):
        """An unbound RespRef whose "above" region is empty gets the
        default ``<label/>`` — jUCMNav then renders the name above the
        symbol."""
        ucm = UCM(name="L")
        m = ucm.add_map(name="M")
        sp = m.add_node(UCM.StartPoint(name="start"))
        a = m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility("Solo"),
            name="Solo",
        ))
        ep = m.add_node(UCM.EndPoint(name="end"))
        m.add_connection(sp, a)
        m.add_connection(a, ep)
        sp.x, sp.y = 50, 100
        a.x, a.y = 150, 100
        ep.x, ep.y = 250, 100
        xml = serialize_to_string(ucm, layout=False)
        # The RespRef's <label/> stays empty — no deltaY override.
        self.assertRegex(xml,
            r'<nodes [^>]*name="Solo"[^>]*>\s*<label/>')

    def test_unbound_label_flips_below_when_above_clashes(self):
        """An unbound RespRef with a sibling element sitting directly
        above it (e.g. a parallel branch one row up) gets a negative
        ``deltaY`` so jUCMNav draws the name below the symbol.
        jUCMNav's deltaY axis is inverted relative to model pixel y —
        negative deltaY moves the label *downward* in the diagram."""
        ucm = UCM(name="L")
        m = ucm.add_map(name="M")
        # Two parallel branches: ``upper`` at y=50, ``lower`` at y=100.
        # The label on ``lower`` would clash with the upper branch's
        # path; deltaY must flip it below.
        upper = m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility("Upper"),
            name="Upper",
        ))
        lower = m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility("Lower"),
            name="Lower",
        ))
        upper.x, upper.y = 150, 50
        lower.x, lower.y = 150, 100
        xml = serialize_to_string(ucm, layout=False)
        # The "Lower" element gets a negative deltaY (label below
        # the symbol in jUCMNav's coordinate convention).
        self.assertRegex(xml,
            r'<nodes [^>]*name="Lower"[^>]*>\s*<label deltaY="-\d+"/>')

    def test_bound_resp_label_stays_default(self):
        """A RespRef bound to a ComponentRef inherits the cluster's
        spacing — no label-position override is needed."""
        ucm = UCM(name="L")
        m = ucm.add_map(name="M")
        comp = ucm.get_or_add_component("Team")
        cr = m.add_component_ref(comp)
        a = m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility("BoundResp"),
            name="BoundResp",
        ))
        a.cont_ref = cr
        a.x, a.y = 150, 100
        # Even with a competing element above, the bound RespRef gets
        # no deltaY override.
        upper = m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility("Other"),
            name="Other",
        ))
        upper.x, upper.y = 150, 50
        xml = serialize_to_string(ucm, layout=False)
        self.assertRegex(xml,
            r'<nodes [^>]*name="BoundResp"[^>]*>\s*<label/>')

    def test_unbound_component_refs_are_omitted(self):
        """A ComponentRef with no PathNode bound to it (and no
        descendants either) does not survive into the export."""
        ucm = small_ucm()
        comp = ucm.get_or_add_component("Lonely")
        ucm.maps[0].add_component_ref(comp, x=0, y=0, width=100, height=100)
        # Don't bind any node to ``comp``.
        xml = serialize_to_string(ucm, layout=False)
        # The component definition stays (it lives at the URN level
        # and may be referenced from other maps later)…
        root = ET.fromstring(xml)
        urndef = root.find("urndef")
        comp_names = [c.get("name") for c in urndef.findall("components")]
        self.assertIn("Lonely", comp_names)
        # …but no ComponentRef on the map references it.
        for diag in urndef.findall("specDiagrams"):
            for cr in diag.findall("contRefs"):
                self.assertNotEqual(
                    cr.get("contDef"),
                    str(comp.id),
                    "unbound ComponentRef should not be emitted",
                )
        # And the definition's ``contRefs`` back-reference is empty.
        lonely_el = next(c for c in urndef.findall("components")
                         if c.get("name") == "Lonely")
        self.assertIsNone(lonely_el.get("contRefs"))


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
        that point at it (``respRefs="36 39"``); each component
        definition lists the IDs of its ComponentRefs
        (``contRefs="14 42"``).

        The exporter drops unbound ComponentRefs, so we bind one of
        the existing RespRefs to the freshly-created ComponentRef
        before exporting; otherwise the filter would (correctly)
        suppress the ComponentRef and its ``contRefs`` back-reference."""
        ucm = small_ucm()
        comp = ucm.get_or_add_component("System")
        cr = ucm.maps[0].add_component_ref(
            comp, x=0, y=0, width=100, height=100,
        )
        # Bind a node to the new ComponentRef so the export filter
        # keeps it.
        for n in ucm.maps[0].nodes:
            if isinstance(n, UCM.RespRef):
                n.cont_ref = cr
                break
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
        ``"true"``). The two are distinct fields.

        Conditions are suppressed by default in the .jucm export (the
        converter-generated ``redo``/``exit``/``branch0`` labels are
        synthetic and clutter the diagram); pass
        ``emit_conditions=True`` to opt back in."""
        ucm = small_ucm()
        xml = serialize_to_string(ucm, emit_conditions=True)
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
        # The created/modified attributes are stamped with the current
        # time at each serialization — strip them so the comparison
        # cannot flake when the two exports straddle a second boundary
        # (same convention as test_family / test_decomposition).
        def strip_ts(xml):
            return re.sub(r' (created|modified)="[^"]*"', '', xml)

        ucm = small_ucm()
        xml1 = serialize_to_string(ucm)
        ucm2 = parse_string(xml1)
        xml2 = serialize_to_string(ucm2)
        self.assertEqual(strip_ts(xml1), strip_ts(xml2))

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


class DtdGuardTests(unittest.TestCase):
    """The .jucm importer refuses DTDs / DOCTYPEs before parsing, to
    block XML entity-expansion ("billion laughs") DoS on untrusted
    input — a zero-dependency alternative to defusedxml."""

    _BOMB = (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE lolz [\n'
        '  <!ENTITY lol "lol">\n'
        '  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">\n'
        ']>\n'
        '<urn:URNspec>&lol2;</urn:URNspec>'
    )

    def test_parse_string_rejects_dtd(self):
        with self.assertRaises(ValueError):
            parse_string(self._BOMB)

    def test_read_ucm_file_rejects_dtd(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "evil.jucm")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self._BOMB)
            with self.assertRaises(ValueError):
                read_ucm(path)

    def test_guard_accepts_prolog_declaration_and_comment(self):
        # No false positive on an XML declaration / comment before the
        # root element — as str and as bytes.
        _forbid_dtd('<?xml version="1.0"?><!-- note --><urn:URNspec/>')
        _forbid_dtd(b'<?xml version="1.0"?><!-- note --><urn:URNspec/>')

    def test_guard_blocks_doctype_after_comment(self):
        with self.assertRaises(ValueError):
            _forbid_dtd('<!-- note -->\n<!DOCTYPE r><r/>')

    def test_normal_export_still_round_trips(self):
        # A real (DTD-free) export is unaffected by the guard.
        ucm = small_ucm()
        xml = serialize_to_string(ucm)
        self.assertIsNotNone(parse_string(xml))


class ScenarioImportTests(unittest.TestCase):
    """The importer reads ``<ucmspec>`` — variables and scenarios.

    It used to skip it entirely ("the round-trip is UCM-only"), so a
    model read back had no scenarios at all. Anything consuming them was
    handed an empty list, and could not tell "nothing to check" apart
    from "checked, and found nothing wrong".
    """

    def _model(self):
        """A UCM with one scenario over one enum and one integer variable."""
        u = UCM(name="Scenarios")
        m = u.add_map(name="main")
        sp = m.add_node(UCM.StartPoint(name="start"))
        ep = m.add_node(UCM.EndPoint(name="end"))
        other = m.add_node(UCM.EndPoint(name="aborted"))
        fork = m.add_node(UCM.OrFork(name="Choice"))
        m.add_connection(sp, fork)
        et = u.get_or_add_enumeration_type("VariantId", values=["v1", "v2"])
        variant = u.get_or_add_variable(
            "variant_id", type="enumeration", enumeration_type=et)
        counter = u.get_or_add_variable("Loop_Assess", type="integer")
        m.add_connection(fork, ep, condition=UCM.Condition(
            label="take", expression="variant_id == v1"))
        m.add_connection(fork, other, condition=UCM.Condition(
            label="skip", expression="variant_id == v2"))
        group = u.add_scenario_group(name="MinedScenarios")
        sc = UCM.ScenarioDef(name="v1_flow", description="covers 3 cases")
        sc._owner = u
        sc.add_initialization(variant, "v1")
        sc.add_initialization(counter, "4")
        sc.add_start_point(sp, enabled=True)
        sc.add_end_point(ep, enabled=True, mandatory=True)
        sc.add_end_point(other, enabled=False, mandatory=False)
        group.add_scenario(sc)
        return u

    def test_scenarios_survive_a_round_trip(self):
        back = parse_string(serialize_to_string(self._model()))
        self.assertEqual(len(back.scenario_groups), 1)
        group = back.scenario_groups[0]
        self.assertEqual(group.name, "MinedScenarios")
        self.assertEqual(len(group.scenarios), 1)
        sc = group.scenarios[0]
        self.assertEqual(sc.name, "v1_flow")
        self.assertEqual(sc.description, "covers 3 cases")

    def test_initializations_keep_their_variable_and_value(self):
        back = parse_string(serialize_to_string(self._model()))
        sc = back.scenario_groups[0].scenarios[0]
        got = {i.variable.name: i.value for i in sc.initializations}
        self.assertEqual(got, {"variant_id": "v1", "Loop_Assess": "4"})

    def test_variables_and_enumeration_types_come_back(self):
        back = parse_string(serialize_to_string(self._model()))
        by_name = {v.name: v for v in back.variables}
        self.assertEqual(set(by_name), {"variant_id", "Loop_Assess"})
        self.assertEqual(by_name["Loop_Assess"].type, "integer")
        self.assertEqual(by_name["variant_id"].type, "enumeration")
        # The enum variable still points at its type, not a fresh one.
        et = by_name["variant_id"].enumeration_type
        self.assertIsNotNone(et)
        self.assertEqual(et.name, "VariantId")
        self.assertEqual(list(et.values), ["v1", "v2"])
        self.assertIn(et, back.enumeration_types)

    def test_start_and_end_points_resolve_to_the_right_nodes(self):
        back = parse_string(serialize_to_string(self._model()))
        sc = back.scenario_groups[0].scenarios[0]
        self.assertEqual([sp.start_point.name for sp in sc.start_points],
                         ["start"])
        self.assertEqual([ep.end_point.name for ep in sc.end_points],
                         ["end", "aborted"])
        # ...and the flags that decide whether a miss is an error.
        flags = {ep.end_point.name: (ep.enabled, ep.mandatory)
                 for ep in sc.end_points}
        self.assertEqual(flags, {"end": (True, True),
                                 "aborted": (False, False)})

    def test_round_trip_does_not_consume_ids(self):
        """Re-importing must adopt the file's ids, not allocate fresh ones.

        Reserving a parsed id without assigning it leaves the object with
        a newly minted id *and* the parsed one burnt, so ``nextGlobalID``
        creeps upward on every round-trip and two consecutive exports
        differ.
        """
        xml1 = serialize_to_string(self._model())
        xml2 = serialize_to_string(parse_string(xml1))
        grab = lambda s: re.search(r'nextGlobalID="(\d+)"', s).group(1)
        self.assertEqual(grab(xml1), grab(xml2))

    def test_reference_fixture_scenarios_are_imported(self):
        here = os.path.dirname(__file__)
        ref = os.path.join(here, "fixtures", "SimpleExample.jucm")
        if not os.path.exists(ref):
            self.skipTest(f"reference fixture not present: {ref}")
        ucm = read_ucm(ref)
        self.assertEqual(len(ucm.scenario_groups), 1)
        self.assertEqual([s.name for s in ucm.scenario_groups[0].scenarios],
                         ["ScenarioDef6"])

    def test_model_without_a_ucmspec_still_imports(self):
        xml = serialize_to_string(self._model())
        stripped = re.sub(r"<ucmspec>.*?</ucmspec>", "", xml, flags=re.S)
        back = parse_string(stripped)
        self.assertEqual(back.scenario_groups, [])
        self.assertTrue(back.maps)          # the UCM half is unaffected


if __name__ == "__main__":
    unittest.main()
