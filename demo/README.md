# pm4py-ucm demo

End-to-end script for mining a UCM and exporting it to a
jUCMNav-compatible `.jucm` file plus a rendered PNG.

## Running

Without any optional dependencies (uses a hand-crafted process tree):

```
python demo/mine_and_export.py --synthetic
```

Hierarchical decomposition (root map plus plug-in maps, stacked PNG):

```
python demo/mine_and_export.py --synthetic --decompose
```

Real XES event log (requires `pip install pm4py`):

```
python demo/mine_and_export.py path/to/log.xes
python demo/mine_and_export.py path/to/log.xes --decompose
```

Outputs `out.jucm` and `out.png` by default; override with `--out base`.

## What `--decompose` does

Turns on `decomposition="auto"`, which splits the discovered UCM into:

- a **root map** holding the top-level skeleton, with one `Stub` per
  major phase / branch / loop;
- one **plug-in map** per `Stub`, holding that phase's internals.

The `.jucm` file then opens in jUCMNav as the root map plus a separate
plug-in map for each stub. The PNG vertically stacks every map (root at
the top, plug-ins below in pre-order DFS) with title strips and a thin
horizontal separator between adjacent panels.

See the *Hierarchical decomposition* section of the top-level
[README](../README.md#hierarchical-decomposition) for the full parameter
shape and per-rule semantics.
