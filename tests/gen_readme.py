#!/usr/bin/env python
"""Regenerate ``tests/README.md`` — a per-file map of the test suite.

The table's three data columns come from three sources that stay in sync
with the code by construction:

* **Tests** — the number of items pytest *collects* from each file
  (parametrised cases counted individually), via ``pytest --collect-only``.
  So the totals here always match ``pytest``.
* **Purpose** — the first sentence of each test module's docstring, read
  with :mod:`ast` (no import, so this runs even without the test
  dependencies installed for collection to succeed).
* **Area** — a small curated grouping below. A new ``test_*.py`` with no
  entry lands under *Other*, which is the cue to add it here.

Usage::

    python tests/gen_readme.py         # rewrites tests/README.md
    python tests/gen_readme.py --check  # exit 1 if it would change

Run it after adding or removing tests; CI does not enforce it, so it is a
maintenance convenience, not a gate.
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
README = TESTS_DIR / "README.md"

#: File -> area. Order here is the order sections appear in the README.
AREAS: dict[str, list[str]] = {
    "Object model & jUCMNav I/O": [
        "test_obj.py",
        "test_export_import.py",
        "test_stub_bindings.py",
        "test_name_wrap.py",
    ],
    "Discovery & conversion": [
        "test_conversion.py",
        "test_decomposition.py",
        "test_choice_signature.py",
        "test_boolean_detection.py",
        "test_expression_minimizer.py",
    ],
    "Resources & performers": [
        "test_resources.py",
    ],
    "Scenario synthesis": [
        "test_scenario_synthesis.py",
    ],
    "Model families": [
        "test_family.py",
        "test_family_stats.py",
    ],
    "Performance & metrics": [
        "test_performance.py",
        "test_metric_validation.py",
    ],
    "Layout": [
        "test_layout.py",
        "test_graphviz_layout.py",
        "test_routing_points.py",
    ],
    "Visualization": [
        "test_visualization.py",
        "test_ucm_svg.py",
    ],
    "Dashboards": [
        "test_dashboards.py",
    ],
    "Sessions (save / share / resume)": [
        "test_sessions.py",
        "test_sessions_registry.py",
    ],
    "Progress / infrastructure": [
        "test_progress.py",
    ],
}


def _area_of(filename: str) -> str:
    for area, files in AREAS.items():
        if filename in files:
            return area
    return "Other"


def collect_counts() -> Counter[str]:
    """``{filename: n_collected}`` from ``pytest --collect-only``."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(TESTS_DIR),
         "--collect-only", "-q"],
        capture_output=True, text=True,
    )
    if proc.returncode not in (0, 5):  # 5 = no tests collected
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit(
            f"pytest --collect-only failed (exit {proc.returncode}); "
            "install the test dependencies (`pip install -e .[dev]`)."
        )
    counts: Counter[str] = Counter()
    for line in proc.stdout.splitlines():
        m = re.match(r".*?(test_[\w-]+\.py)::", line.strip())
        if m:
            counts[m.group(1)] += 1
    if not counts:
        raise SystemExit("no tests collected — is pytest finding the suite?")
    return counts


def first_sentence(path: Path) -> str:
    """The first sentence of a module's docstring, whitespace-collapsed."""
    doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
    if not doc:
        return "_(no module docstring)_"
    flat = " ".join(doc.strip().split())
    # Drop reStructuredText inline-role prefixes (``:mod:`x``` -> ```x```)
    # so the sentence reads as plain Markdown.
    flat = re.sub(r":[a-z]+:(?=`)", "", flat)
    m = re.search(r"\.(?:\s|$)", flat)
    sentence = flat[: m.end()].strip() if m else flat
    return sentence.rstrip(".") + "."


def build_markdown(counts: Counter[str]) -> str:
    files = sorted(counts)
    total = sum(counts.values())

    # Group by area, preserving the AREAS order, then Other last.
    order = list(AREAS) + ["Other"]
    by_area: dict[str, list[str]] = {a: [] for a in order}
    for f in files:
        by_area[_area_of(f)].append(f)

    lines = [
        "# Test suite",
        "",
        f"**{total} tests** across **{len(files)} modules**, grouped by area "
        "below. Counts are pytest-collected items (parametrised cases counted "
        "individually), so they match `pytest` exactly.",
        "",
        "> Regenerate this file with `python tests/gen_readme.py` after adding "
        "or removing tests. Purposes are the first sentence of each module's "
        "docstring; edit the docstring, not this table.",
        "",
        "| Area | Module | Purpose | Tests |",
        "| --- | --- | --- | ---: |",
    ]

    for area in order:
        area_files = by_area[area]
        if not area_files:
            continue
        subtotal = sum(counts[f] for f in area_files)
        first = True
        for f in area_files:
            area_cell = f"**{area}**" if first else ""
            purpose = first_sentence(TESTS_DIR / f).replace("|", "\\|")
            lines.append(
                f"| {area_cell} | [`{f}`]({f}) | {purpose} | {counts[f]} |"
            )
            first = False
        # An area subtotal only earns its row when the area has >1 file.
        if len(area_files) > 1:
            lines.append(f"| | _{area} subtotal_ | | **{subtotal}** |")

    lines.append(f"| | **Total** | | **{total}** |")
    lines.append("")
    lines.append(
        "See the repository [`README.md`](../README.md#testing) for how to "
        "run the suite, and [`docs/metrics.md`](../docs/metrics.md) for the "
        "metric definitions the validation tests enforce."
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if README.md is out of date")
    args = ap.parse_args()

    markdown = build_markdown(collect_counts())

    if args.check:
        current = README.read_text(encoding="utf-8") if README.exists() else ""
        if current != markdown:
            sys.stderr.write(
                "tests/README.md is out of date — run "
                "`python tests/gen_readme.py`.\n")
            return 1
        return 0

    README.write_text(markdown, encoding="utf-8")
    print(f"wrote {README} ({markdown.count(chr(10))} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
