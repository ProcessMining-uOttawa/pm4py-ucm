"""Tests for the multi-line name-wrapping helper and its integration with
the exporter (issue 2 of the visual-improvements series).

A long responsibility name like ``Send for Credit Collection`` should be
broken across two or three lines in the ``.jucm`` so that jUCMNav draws
a narrow, taller rectangle. The break must be the exact ``\\r\\n``
sequence that jUCMNav itself uses (encoded as ``&#xD;&#xA;`` in the
attribute value)."""

import unittest

from pm4py_ucm import UCM, convert_to_ucm
from pm4py_ucm.objects.ucm.util.name_wrap import (
    wrap_name, label_dimensions, DEFAULT_MAX_WIDTH,
)
from pm4py_ucm.objects.ucm.exporter.variants.jucm import serialize_to_string
from pm4py_ucm.objects.ucm.importer.variants.jucm import parse_string

from types import SimpleNamespace as NS

leaf = lambda l: NS(operator=None, children=[], label=l)
op = lambda o, *kids: NS(operator=NS(value=o), children=list(kids), label=None)


class NameWrapTests(unittest.TestCase):
    """Unit tests for the pure wrap-helper module."""

    def test_short_names_pass_through_untouched(self):
        for s in ["", "A", "Pay", "Triage", "Send Fine"]:  # all <= 12 chars
            self.assertEqual(wrap_name(s), s,
                             f"{s!r} should not be wrapped")

    def test_long_name_breaks_on_whitespace(self):
        """Wrapping must break only at word boundaries — never inside a
        word — and use the CRLF sequence jUCMNav itself emits."""
        wrapped = wrap_name("Send for Credit Collection")
        self.assertIn("\r\n", wrapped)
        # Each fragment is a contiguous word from the original.
        original_words = "Send for Credit Collection".split()
        rebuilt_words = " ".join(wrapped.split("\r\n")).split()
        self.assertEqual(original_words, rebuilt_words)

    def test_caps_at_max_lines(self):
        """``max_lines`` is a hard upper bound; very long names re-pack
        rather than producing four-plus lines."""
        wrapped = wrap_name(
            "Receive Result Appeal from Prefecture Decision Notification",
            max_lines=3)
        self.assertLessEqual(wrapped.count("\r\n"), 2)  # at most 3 lines

    def test_label_dimensions_match_wrap(self):
        n_lines, longest = label_dimensions("Send for Credit Collection")
        wrapped = wrap_name("Send for Credit Collection")
        self.assertEqual(n_lines, wrapped.count("\r\n") + 1)
        # ``longest`` is the longest *line* (in characters).
        self.assertEqual(longest,
                          max(len(l) for l in wrapped.split("\r\n")))

    def test_unwrappable_long_word_returns_unchanged(self):
        """A single token longer than the budget is left alone — better
        to overflow than to break in the middle of a word."""
        long_word = "Supercalifragilisticexpialidocious"
        self.assertEqual(wrap_name(long_word), long_word)


class ExporterWrapTests(unittest.TestCase):
    """Integration tests: long names round-trip through the exporter
    with embedded ``\\r\\n``, and the importer canonicalises them back
    to plain space-separated form so logical equality is preserved."""

    def test_long_responsibility_name_split_in_xml(self):
        tree = op("->",
                  leaf("Send for Credit Collection"),
                  leaf("Pay"))
        ucm = convert_to_ucm(tree)
        xml = serialize_to_string(ucm)
        # The CRLF inside the attribute is escaped as &#xD;&#xA;
        self.assertIn("Send for&#xD;&#xA;Credit", xml)

    def test_wrap_can_be_disabled(self):
        tree = op("->",
                  leaf("Send for Credit Collection"),
                  leaf("Pay"))
        ucm = convert_to_ucm(tree)
        xml_off = serialize_to_string(ucm, wrap_names=False)
        self.assertIn("Send for Credit Collection", xml_off)
        self.assertNotIn("&#xD;&#xA;", xml_off)

    def test_importer_canonicalises_line_breaks(self):
        """Round-tripping a long name preserves the logical name (no
        embedded line breaks in the Python model), regardless of the
        wrapping applied during export."""
        tree = op("->",
                  leaf("Send for Credit Collection"),
                  leaf("Receive Result Appeal from Prefecture"))
        ucm1 = convert_to_ucm(tree)
        xml = serialize_to_string(ucm1)
        self.assertIn("&#xD;&#xA;", xml)  # wrapping happened
        ucm2 = parse_string(xml)
        names = sorted(r.name for r in ucm2.responsibilities)
        self.assertIn("Send for Credit Collection", names)
        self.assertIn("Receive Result Appeal from Prefecture", names)

    def test_full_roundtrip_idempotent(self):
        """Two consecutive export → import → export passes produce
        byte-identical XML (modulo the timestamps)."""
        import re as _re
        tree = op("->",
                  leaf("Send for Credit Collection"),
                  op("X", leaf("Pay"), leaf("Receive Result Appeal from Prefecture")),
                  leaf("Close"))
        ucm1 = convert_to_ucm(tree)
        xml1 = serialize_to_string(ucm1, layout=False)
        ucm2 = parse_string(xml1)
        xml2 = serialize_to_string(ucm2, layout=False)
        strip = lambda s: _re.sub(r' (created|modified)="[^"]*"', '', s)
        self.assertEqual(strip(xml1), strip(xml2))

    def test_short_component_names_not_wrapped(self):
        """ComponentElement names ("Triage Team", "538", …) are short
        identifiers — wrapping them would force wider lanes without
        benefit. The exporter skips wrapping for components."""
        from pm4py_ucm import bind_performers
        tree = op("->", leaf("A"), leaf("B"))
        ucm = convert_to_ucm(tree)
        bind_performers(ucm, {"A": "Some Particularly Long Team Name",
                              "B": "538"})
        xml = serialize_to_string(ucm)
        # Component name appears unwrapped on the <components ...> line.
        self.assertIn(
            'name="Some Particularly Long Team Name"', xml)


if __name__ == "__main__":
    unittest.main()
