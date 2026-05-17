# pm4py-ucm demos

Three things in this directory:

| File                          | What it is                                                       |
|-------------------------------|------------------------------------------------------------------|
| `pm4py_ucm_tutorial.ipynb`    | **Jupyter notebook tutorial** — guided tour of the full feature surface against a realistic claims-payment log. Recommended starting point. |
| `IssueTracker.zip`            | Small, readable event log (~100 K events / 11 K cases, 9 distinct activities, 5 roles). Drives sections 0-5 of the notebook. |
| `ClaimsPaymentLog.zip`        | Heavier, denser event log (~90 K events, more activities and actor types). Drives section 6 onward — decomposition gets to work on a non-trivial tree. |
| `mine_and_export.py`          | Minimal command-line script that mines a UCM, exports a `.jucm`, and renders a PNG. |

## Tutorial notebook

Open `pm4py_ucm_tutorial.ipynb` in JupyterLab, VS Code, or any
notebook-aware editor. It covers, with runnable cells:

1. Discovering a UCM from an event log (PNG + `.jucm` outputs).
2. Switching to the BPMN-flavoured rendering.
3. Mining performers giving priority to roles vs individual
   resources, with both visualisations.
4. Providing manual performer mappings — and watching the
   deterministic colour hashing follow the new team names.
5. Hierarchical decomposition: the three boundary rules
   (`on_root_sequence`, `on_parallel`, `on_loop`) in isolation,
   plus the combined `"auto"` preset. All in BPMN style.
6. Round-tripping a model through `.jucm` (open in jUCMNav, read
   it back here).
7. Programmatic construction — both directly against the `UCM`
   object model and from a hand-built process tree.
8. Smaller helpers worth knowing: `discover_resources`,
   `discover_components`, and the underlying process tree.

Each section writes its outputs to a local `output/` directory so
you can open them in jUCMNav, peek at the XML, or include in a
report.

## Mine-and-export script

End-to-end command-line example.

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
