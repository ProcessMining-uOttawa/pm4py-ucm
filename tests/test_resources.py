"""Tests for resource mining and performer binding.

Resources in an event log become :class:`UCM.ComponentElement` definitions
in URN. Each :class:`UCM.Responsibility` is linked semantically to its
performer; each :class:`UCM.RespRef` is linked visually to a
:class:`UCM.ComponentRef` rectangle representing that performer on the
map. These tests cover the full pipeline."""

import unittest

from pm4py_ucm import UCM, discover_resources, bind_performers
from pm4py_ucm.algo.discovery.resources.variants import activity_attribute


class _Event(dict):
    """Minimal duck-typed event — supports ``ev.get(key)``."""
    def get(self, key, default=None):
        return dict.get(self, key, default)


def _trace(*events):
    return [_Event(ev) for ev in events]


class ResourceDiscoveryTests(unittest.TestCase):
    """Mining activity → performer from a synthetic event log."""

    def test_mode_strategy_picks_most_common(self):
        log = [
            _trace({"concept:name": "Login",  "org:resource": "Alice"},
                   {"concept:name": "Pay",    "org:resource": "Bob"}),
            _trace({"concept:name": "Login",  "org:resource": "Alice"},
                   {"concept:name": "Pay",    "org:resource": "Carol"}),  # outlier
            _trace({"concept:name": "Login",  "org:resource": "Alice"},
                   {"concept:name": "Pay",    "org:resource": "Bob"}),
        ]
        out = discover_resources(log)
        self.assertEqual(out["Login"], "Alice")
        self.assertEqual(out["Pay"], "Bob")  # 2/3 Bob, 1/3 Carol; min_support=0.5

    def test_min_support_drops_ambiguous_bindings(self):
        log = [
            _trace({"concept:name": "Pay", "org:resource": "Alice"}),
            _trace({"concept:name": "Pay", "org:resource": "Bob"}),
            _trace({"concept:name": "Pay", "org:resource": "Carol"}),
        ]
        # 33% support for the top performer ⇒ dropped at min_support=0.5.
        out = discover_resources(log, min_support=0.5)
        self.assertNotIn("Pay", out)
        # Lowering min_support makes the binding emerge.
        out = discover_resources(log, min_support=0.1)
        self.assertIn("Pay", out)

    def test_first_strategy_returns_first_seen(self):
        log = [
            _trace({"concept:name": "Pay", "org:resource": "Alice"}),
            _trace({"concept:name": "Pay", "org:resource": "Bob"}),
            _trace({"concept:name": "Pay", "org:resource": "Bob"}),
        ]
        out = discover_resources(log, strategy="first")
        self.assertEqual(out["Pay"], "Alice")

    def test_unbound_strategy_skips_multivalued(self):
        log = [
            _trace({"concept:name": "Triage", "org:resource": "Alice"}),
            _trace({"concept:name": "Triage", "org:resource": "Bob"}),
            _trace({"concept:name": "Stable", "org:resource": "Bob"}),
            _trace({"concept:name": "Stable", "org:resource": "Bob"}),
        ]
        out = discover_resources(log, strategy="unbound")
        self.assertNotIn("Triage", out)         # ambiguous
        self.assertEqual(out["Stable"], "Bob")  # unique

    def test_attribute_priority_falls_through(self):
        # Each event has either org:role or org:resource (not both).
        log = [
            _trace({"concept:name": "Login", "org:role": "Auth Team"},
                   {"concept:name": "Pay",   "org:resource": "Bob"}),
        ]
        out = discover_resources(
            log, attribute_priority=["org:role", "org:resource"])
        self.assertEqual(out["Login"], "Auth Team")
        self.assertEqual(out["Pay"], "Bob")

    def test_handles_missing_resource_attribute(self):
        log = [
            _trace({"concept:name": "Login"}),
            _trace({"concept:name": "Login", "org:resource": "Alice"}),
        ]
        # The first event has no resource; the second does. Mode should pick Alice.
        out = discover_resources(log)
        self.assertEqual(out["Login"], "Alice")


class VariantSelectionTests(unittest.TestCase):
    """Regression guard for an enum-aliasing bug.

    Both ``Variants`` members used to map to the same ``activity_attribute``
    module value, which made :class:`enum.Enum` collapse the second into
    an alias for the first. The ``if variant is Variants.ROLE_THEN_RESOURCE``
    guard in :func:`apply` then misfired on every call and injected the
    role-first ``attribute_priority`` list — so caller-supplied
    ``attribute="org:resource"`` was silently overridden by ``"org:role"``
    when the log carried both attributes, and both choices produced
    identical models.
    """

    def _log_with_both_attrs(self):
        # Same activity, but the role is coarse-grained ("Dev") and the
        # resource is fine-grained ("Alice" / "Bob"). Different choices
        # MUST yield different vocabularies.
        return [
            _trace({"concept:name": "Code", "org:role": "Dev", "org:resource": "Alice"},
                   {"concept:name": "Test", "org:role": "QA",  "org:resource": "Bob"}),
            _trace({"concept:name": "Code", "org:role": "Dev", "org:resource": "Carol"},
                   {"concept:name": "Test", "org:role": "QA",  "org:resource": "Dan"}),
        ]

    def test_role_and_resource_produce_distinct_vocabularies(self):
        from pm4py_ucm import discover_components
        log = self._log_with_both_attrs()
        roles = discover_components(log, attribute="org:role")
        resources = discover_components(log, attribute="org:resource")
        self.assertEqual(set(roles), {"Dev", "QA"})
        self.assertEqual(set(resources), {"Alice", "Bob", "Carol", "Dan"})
        self.assertNotEqual(set(roles), set(resources))

    def test_role_and_resource_produce_distinct_activity_bindings(self):
        log = self._log_with_both_attrs()
        by_role = discover_resources(log, attribute="org:role")
        by_resource = discover_resources(log, attribute="org:resource")
        # Role binds each activity to a single role.
        self.assertEqual(by_role["Code"], "Dev")
        # Resource binds the same activity to a (one of several) person —
        # not "Dev" — proving the attribute= parameter was actually honoured.
        self.assertIn(by_resource["Code"], {"Alice", "Carol"})
        self.assertNotEqual(by_role["Code"], by_resource["Code"])

    def test_variants_are_distinct_enum_members(self):
        # Belt and braces: defend against a future refactor that
        # accidentally reintroduces the aliasing collapse.
        from pm4py_ucm.algo.discovery.resources.algorithm import Variants
        self.assertIsNot(
            Variants.ACTIVITY_ATTRIBUTE, Variants.ROLE_THEN_RESOURCE,
            "Variants members must be distinct - same-value members "
            "become aliases under enum.Enum",
        )


class BindPerformersTests(unittest.TestCase):
    """Attaching a {activity: performer} mapping to a UCM."""

    def _build_ucm(self):
        ucm = UCM(name="Test")
        m = ucm.add_map(name="Main")
        start = m.add_node(UCM.StartPoint(name="start"))
        end   = m.add_node(UCM.EndPoint(name="end"))
        a = m.add_node(UCM.RespRef(resp_def=ucm.get_or_add_responsibility("A")))
        b = m.add_node(UCM.RespRef(resp_def=ucm.get_or_add_responsibility("B")))
        m.add_connection(start, a)
        m.add_connection(a, b)
        m.add_connection(b, end)
        return ucm, m, a, b

    def test_creates_component_definitions(self):
        ucm, _, _, _ = self._build_ucm()
        bind_performers(ucm, {"A": "Team1", "B": "Team2"})
        names = sorted(c.name for c in ucm.components)
        self.assertEqual(names, ["Team1", "Team2"])

    def test_sets_responsibility_performer(self):
        ucm, _, _, _ = self._build_ucm()
        bind_performers(ucm, {"A": "Team1", "B": "Team2"})
        r_a = next(r for r in ucm.responsibilities if r.name == "A")
        r_b = next(r for r in ucm.responsibilities if r.name == "B")
        self.assertEqual(r_a.performer.name, "Team1")
        self.assertEqual(r_b.performer.name, "Team2")

    def test_creates_component_refs_on_map(self):
        ucm, m, _, _ = self._build_ucm()
        bind_performers(ucm, {"A": "Team1", "B": "Team2"})
        self.assertEqual(len(m.cont_refs), 2)
        cont_def_names = sorted(cr.cont_def.name for cr in m.cont_refs)
        self.assertEqual(cont_def_names, ["Team1", "Team2"])

    def test_one_component_ref_per_component_per_map(self):
        """Two activities performed by the same team share *one*
        ComponentRef rectangle on the map."""
        ucm, m, _, _ = self._build_ucm()
        bind_performers(ucm, {"A": "Shared", "B": "Shared"})
        self.assertEqual(len(m.cont_refs), 1)
        for n in m.nodes:
            if isinstance(n, UCM.RespRef):
                self.assertIs(n.cont_ref, m.cont_refs[0])

    def test_binds_resp_ref_cont_ref(self):
        ucm, m, a, b = self._build_ucm()
        bind_performers(ucm, {"A": "Team1", "B": "Team2"})
        self.assertIsNotNone(a.cont_ref)
        self.assertIsNotNone(b.cont_ref)
        self.assertEqual(a.cont_ref.cont_def.name, "Team1")
        self.assertEqual(b.cont_ref.cont_def.name, "Team2")

    def test_kind_override_via_tuple(self):
        """Values may be ``(name, kind)`` tuples to set the component kind."""
        ucm, _, _, _ = self._build_ucm()
        bind_performers(ucm, {
            "A": ("Customer", UCM.ComponentElement.Kind.ACTOR),
            "B": "Backend",
        })
        cust = next(c for c in ucm.components if c.name == "Customer")
        back = next(c for c in ucm.components if c.name == "Backend")
        self.assertEqual(cust.kind, UCM.ComponentElement.Kind.ACTOR)
        self.assertEqual(back.kind, UCM.ComponentElement.Kind.TEAM)


class ConverterPerformerTests(unittest.TestCase):
    """The from_process_tree converter accepts a `performers` parameter."""

    def _tree(self):
        from types import SimpleNamespace as NS
        leaf = lambda l: NS(operator=None, children=[], label=l)
        op = lambda o, *kids: NS(operator=NS(value=o), children=list(kids),
                                  label=None)
        return op("->", leaf("Login"), leaf("Pay"), leaf("Logout"))

    def test_performers_passed_through_converter(self):
        from pm4py_ucm import convert_to_ucm
        ucm = convert_to_ucm(self._tree(), parameters={
            "performers": {"Login": "Auth", "Pay": "Billing", "Logout": "Auth"},
        })
        # Two components (Auth used twice), three RespRefs all bound.
        self.assertEqual(len(ucm.components), 2)
        m = ucm.maps[0]
        bound = [n for n in m.nodes
                 if isinstance(n, UCM.RespRef) and n.cont_ref is not None]
        self.assertEqual(len(bound), 3)

    def test_performers_exported_to_jucm(self):
        """The performer binding survives a jUCMNav export → re-import."""
        from pm4py_ucm import convert_to_ucm
        from pm4py_ucm.objects.ucm.exporter.variants.jucm import serialize_to_string
        from pm4py_ucm.objects.ucm.importer.variants.jucm import parse_string

        ucm = convert_to_ucm(self._tree(), parameters={
            "performers": {"Login": "Auth", "Pay": "Billing", "Logout": "Auth"},
        })
        xml = serialize_to_string(ucm)
        # Every RespRef should declare its contRef.
        self.assertEqual(xml.count('xsi:type="ucm.map:RespRef"'), 3)
        # Re-import and verify links.
        ucm2 = parse_string(xml)
        m2 = ucm2.maps[0]
        for n in m2.nodes:
            if isinstance(n, UCM.RespRef):
                self.assertIsNotNone(n.cont_ref)
                self.assertIsNotNone(n.cont_ref.cont_def)


    def test_mode_default_admits_modal_performer_without_majority(self):
        """The default ``min_support=0.0`` picks the modal performer even
        when no one owns a majority. Critical for logs like Road Traffic
        Fines where 147 different people perform the same activity and
        the modal one only owns ~6% of events."""
        log = [
            _trace({"concept:name": "Create", "org:resource": "Alice"}),
            _trace({"concept:name": "Create", "org:resource": "Bob"}),
            _trace({"concept:name": "Create", "org:resource": "Carol"}),
            _trace({"concept:name": "Create", "org:resource": "Alice"}),
            # Alice has 2/4 = 50% (top), then Bob and Carol at 25% each.
            # Pre-fix default of min_support=0.5 dropped anything < 50%;
            # but real logs commonly disperse below that. With the new
            # default of 0.0, Alice (the modal performer) is bound.
        ]
        out = discover_resources(log)  # uses new default
        self.assertEqual(out["Create"], "Alice")

    def test_discover_components_returns_full_vocabulary(self):
        """The full set of distinct performer values — including those
        that lose the mode-tie and therefore wouldn't appear in
        ``discover_resources`` output — is returned by
        :func:`discover_components`."""
        from pm4py_ucm import discover_components
        log = [
            _trace(
                {"concept:name": "A", "org:resource": "Alice"},
                {"concept:name": "B", "org:resource": "Bob"},
                {"concept:name": "C", "org:resource": "Carol"},
            ),
            _trace(
                {"concept:name": "A", "org:resource": "Dan"},
                {"concept:name": "B", "org:resource": "Bob"},
            ),
        ]
        components = discover_components(log)
        self.assertEqual(components, ["Alice", "Bob", "Carol", "Dan"])

    def test_discover_components_handles_attribute_priority(self):
        """``discover_components`` follows the same fallback chain as
        ``discover_resources``."""
        from pm4py_ucm import discover_components
        log = [
            _trace(
                {"concept:name": "A", "org:role": "AuthTeam"},
                {"concept:name": "B", "org:resource": "Bob"},
            ),
        ]
        out = discover_components(
            log, attribute_priority=["org:role", "org:resource"])
        self.assertEqual(out, ["AuthTeam", "Bob"])

    def test_bind_performers_accepts_additional_components(self):
        """Components passed via ``additional_components`` get defined
        on the URN spec but NOT given a ComponentRef on the map (since
        no responsibility is bound to them)."""
        from pm4py_ucm import UCM, bind_performers
        ucm = UCM(name="X")
        m = ucm.add_map(name="M")
        a = m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility("Pay")))
        m.add_connection(m.add_node(UCM.StartPoint(name="start")), a)
        m.add_connection(a, m.add_node(UCM.EndPoint(name="end")))

        bind_performers(ucm, {"Pay": "Alice"},
                        additional_components=["Bob", "Carol"])
        # All three component definitions exist
        names = sorted(c.name for c in ucm.components)
        self.assertEqual(names, ["Alice", "Bob", "Carol"])
        # Only Alice has a ComponentRef on the map (since only Pay is bound)
        ref_names = sorted(cr.cont_def.name for cr in m.cont_refs)
        self.assertEqual(ref_names, ["Alice"])

    def test_additional_components_deduplicates(self):
        """A name already present (as a bound performer) is not added
        twice when also listed in ``additional_components``."""
        from pm4py_ucm import UCM, bind_performers
        ucm = UCM(name="X")
        m = ucm.add_map(name="M")
        a = m.add_node(UCM.RespRef(
            resp_def=ucm.get_or_add_responsibility("Pay")))
        m.add_connection(m.add_node(UCM.StartPoint(name="start")), a)
        m.add_connection(a, m.add_node(UCM.EndPoint(name="end")))

        bind_performers(ucm, {"Pay": "Alice"},
                        additional_components=["Alice", "Bob"])
        names = sorted(c.name for c in ucm.components)
        self.assertEqual(names, ["Alice", "Bob"])  # Alice only once


if __name__ == "__main__":
    unittest.main()
