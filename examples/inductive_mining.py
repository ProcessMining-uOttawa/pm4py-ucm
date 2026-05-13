"""End-to-end inductive miner → UCM example.

This script reads an event log, runs PM4Py's inductive miner to obtain a
process tree, converts the tree into a Use Case Map, and writes the
resulting UCM both as a renderable PNG diagram and as a jUCMNav-compatible
``.jucm`` file.

It requires ``pm4py`` to be installed::

    pip install pm4py

Usage::

    python examples/inductive_mining.py path/to/log.xes [output_dir]

If no log path is supplied the script falls back to PM4Py's bundled
``running-example`` log (when available).
"""

from __future__ import annotations

import os
import sys
import tempfile

import pm4py_ucm


def discover_tree(log_path: str | None):
    import pm4py
    if log_path is None:
        # PM4Py ships a small XES file with the package for examples.
        candidate = os.path.join(
            os.path.dirname(pm4py.__file__),
            "tests", "input_data", "running-example.xes",
        )
        if not os.path.exists(candidate):
            raise FileNotFoundError(
                "No log path supplied and PM4Py's running-example.xes "
                "could not be located. Pass a path explicitly."
            )
        log_path = candidate

    log = pm4py.read_xes(log_path)
    return pm4py.discover_process_tree_inductive(log)


def main() -> None:
    log_path = sys.argv[1] if len(sys.argv) > 1 else None
    out_dir = sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(
        prefix="pm4py_ucm_mining_")
    os.makedirs(out_dir, exist_ok=True)

    tree = discover_tree(log_path)
    print(f"Discovered tree: {tree}")

    ucm = pm4py_ucm.convert_to_ucm(tree)
    print(f"Built {ucm}")

    jucm_path = os.path.join(out_dir, "discovered.jucm")
    pm4py_ucm.write_ucm(ucm, jucm_path)
    print(f"Wrote {jucm_path}")

    try:
        png_path = os.path.join(out_dir, "discovered.png")
        pm4py_ucm.save_vis_ucm(ucm, png_path)
        print(f"Wrote {png_path}")
    except Exception as exc:  # graphviz binary may not be installed
        print(f"(skipping PNG render: {exc})")


if __name__ == "__main__":
    main()
