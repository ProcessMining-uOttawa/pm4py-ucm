"""End-to-end demo: discover scenarios for an executable UCM.

The script builds a synthetic process tree

    X -> (Y || Z) -> (A x B) -> W

and a tiny event log covering three observed behaviours:

    * 60 cases  X-Y-Z-A-W
    * 30 cases  X-Y-Z-B-W
    * 10 cases  X-Z-Y-A-W      (parallel re-order — concurrency-equivalent to the first)

Run it without arguments to print the variant table and write
``out.jucm`` / ``out.scenarios.csv`` / ``out.case_variant_map.csv`` to
the current directory.

Usage::

    python examples/scenario_synthesis.py
    python examples/scenario_synthesis.py --no-conditions
    python examples/scenario_synthesis.py --fine-loops

The ``--no-conditions`` flag suppresses OR-fork condition emission so
you can inspect the metamodel layer separately. ``--fine-loops``
turns off loop coarsening — irrelevant for this synthetic tree but a
documented switch for empirical sensitivity analysis on real logs.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Use the package as a sibling import target.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pm4py_ucm
from pm4py_ucm.algo.discovery.variants import clustering as _clustering
from pm4py_ucm.algo.discovery.scenarios import synthesis as _scenarios
from pm4py_ucm.algo.discovery.scenarios import reports as _reports


class T:
    """Duck-typed process tree node — the converter and the clustering
    pass both accept anything that exposes ``operator``, ``label`` and
    ``children``."""

    def __init__(self, operator=None, label=None, children=None):
        self.operator = operator
        self.label = label
        self.children = children or []


def _build_tree():
    return T(operator="->", children=[
        T(label="X"),
        T(operator="+", children=[T(label="Y"), T(label="Z")]),
        T(operator="X", children=[T(label="A"), T(label="B")]),
        T(label="W"),
    ])


def _build_log():
    log = []
    for i in range(60):
        log.append((f"caseA_{i}", ["X", "Y", "Z", "A", "W"]))
    for i in range(30):
        log.append((f"caseB_{i}", ["X", "Y", "Z", "B", "W"]))
    for i in range(10):
        log.append((f"caseA_alt_{i}", ["X", "Z", "Y", "A", "W"]))
    return log


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", default="out",
        help="Output base name (default: 'out' -> out.jucm + out.scenarios.csv + out.case_variant_map.csv)",
    )
    parser.add_argument(
        "--no-conditions", action="store_true",
        help="Skip OR-fork condition emission (metamodel layer only)",
    )
    parser.add_argument(
        "--fine-loops", action="store_true",
        help="Disable loop coarsening (sensitivity analysis)",
    )
    args = parser.parse_args(argv)

    tree = _build_tree()
    log = _build_log()

    ucm = pm4py_ucm.convert_to_ucm(tree)
    clustering = _clustering.cluster(
        log, tree, coarsen_loops=not args.fine_loops,
    )
    _scenarios.synthesize_scenarios(
        ucm, tree, clustering,
        emit_conditions=not args.no_conditions,
    )

    print(f"Total cases:                 {clustering.total_cases}")
    print(f"Concurrency-aware variants:  {len(clustering.variants)}")
    print(f"Sequence variants in log:    {clustering.sequence_variant_count}")
    print(f"Fitness percentage:          {clustering.fitness_percentage:.1%}")
    print(f"Compression ratio:           {clustering.compression_ratio:.3f} "
          f"(<1.0 = concurrency-aware compresses)")
    print()
    print(f"{'Variant':<10} {'Freq':>6} {'Seqs':>6} {'Linearizations':>15}  Partial-order expression")
    print("-" * 80)
    for v in clustering.variants:
        print(
            f"{v.variant_id:<10} {v.frequency:>6} {v.sequence_variants:>6} "
            f"{v.linearization_count:>15}  {v.partial_order_expression}"
        )
    if clustering.noise_case_ids:
        print(f"{'noise':<10} {len(clustering.noise_case_ids):>6}")
    print()

    jucm_path = args.out + ".jucm"
    scenarios_csv = args.out + ".scenarios.csv"
    case_map_csv = args.out + ".case_variant_map.csv"
    pm4py_ucm.write_ucm(ucm, jucm_path)
    _reports.write_variants_report(clustering, scenarios_csv)
    _reports.write_case_variant_map(clustering, case_map_csv)

    print(f"wrote {jucm_path}, {scenarios_csv}, {case_map_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
