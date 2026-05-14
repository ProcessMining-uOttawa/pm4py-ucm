"""End-to-end pm4py-ucm demo: mine a UCM and export to ``.jucm`` + PNG.

The script accepts either a path to an XES event log (requires
``pm4py``) or, with ``--synthetic``, builds a hand-crafted process
tree so the demo runs without any optional dependencies.

Usage
-----

::

    # Synthetic tree, flat single map
    python demo/mine_and_export.py --synthetic

    # Synthetic tree, hierarchical decomposition
    python demo/mine_and_export.py --synthetic --decompose

    # Real log, hierarchical decomposition
    python demo/mine_and_export.py path/to/log.xes --decompose

Outputs ``out.jucm`` and ``out.png`` in the current directory.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _build_synthetic_tree():
    """Hand-built process tree exercising sequence, parallel, and loop."""

    class T:
        def __init__(self, operator=None, label=None, children=None):
            self.operator = operator
            self.label = label
            self.children = children or []

    seq = lambda *labels: T(
        operator="->",
        children=[T(label=lab) for lab in labels],
    )

    return T(operator="->", children=[
        seq("Create Ticket", "Triage", "Assign Engineer"),
        T(operator="+", children=[
            seq("Reserve Build Slot", "Snapshot Repo"),
            seq("Notify Reviewer", "Wait for Approval"),
        ]),
        T(operator="*", children=[
            seq("Implement Fix", "Test Fix", "Code Review"),
            T(label=None),
        ]),
        seq("Deploy Fix", "Close Ticket"),
    ])


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Mine a UCM from a process tree or event log."
    )
    parser.add_argument(
        "log", nargs="?",
        help="Path to a .xes event log (requires pm4py). Mutually "
             "exclusive with --synthetic.",
    )
    parser.add_argument(
        "--synthetic", action="store_true",
        help="Use a hand-crafted process tree (no pm4py needed).",
    )
    parser.add_argument(
        "--decompose", action="store_true",
        help="Enable hierarchical decomposition (decomposition='auto'). "
             "Produces a root map plus plug-in maps connected by Stubs, "
             "rendered as a vertically stacked PNG.",
    )
    parser.add_argument(
        "--out", default="out",
        help="Output base name; .jucm and .png are appended (default: out).",
    )
    args = parser.parse_args(argv)

    if not args.synthetic and not args.log:
        parser.error("either a log path or --synthetic is required")

    import pm4py_ucm

    if args.synthetic:
        tree = _build_synthetic_tree()
        decomp = "auto" if args.decompose else None
        ucm = pm4py_ucm.convert_to_ucm(tree, decomposition=decomp)
        what = "synthetic process tree"
    else:
        try:
            import pm4py  # noqa: F401
        except ImportError:
            print("error: pm4py is required to read XES logs; "
                  "install with `pip install pm4py`, or use --synthetic.",
                  file=sys.stderr)
            return 2
        log_path = Path(args.log)
        if not log_path.exists():
            print(f"error: {log_path}: no such file", file=sys.stderr)
            return 2
        decomp = "auto" if args.decompose else None
        ucm = pm4py_ucm.discover_ucm_inductive(
            str(log_path), decomposition=decomp,
        )
        what = log_path.name

    jucm_path = args.out + ".jucm"
    png_path = args.out + ".png"
    pm4py_ucm.write_ucm(ucm, jucm_path)
    pm4py_ucm.save_vis_ucm(ucm, png_path)

    print(f"mined UCM from {what}: {len(ucm.maps)} map(s)")
    for m in ucm.maps:
        n_nodes = len(m.nodes)
        n_stubs = sum(1 for n in m.nodes if getattr(n, "bindings", None))
        print(f"  - {m.name!r}: {n_nodes} nodes, {n_stubs} stub(s)")
    print(f"wrote {jucm_path} and {png_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
