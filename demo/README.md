# pm4py-ucm demos

Contents:

| File                                    | What it is                                                       |
|-----------------------------------------|------------------------------------------------------------------|
| `pm4py_ucm_tutorial.ipynb`              | **Main tutorial notebook** — guided tour of the full model-discovery feature surface against a realistic claims-payment log. Recommended starting point for the discovery / rendering / decomposition side of the package. |
| `scenario_synthesis_tutorial.ipynb`     | **Scenario-synthesis tutorial** — pedagogical walkthrough of the scenario layer: concurrency-aware variants, the `LoopEntryGuard`, variant-driven vs data-driven OR-fork conditions, transparent support for decomposed UCMs. Small synthetic examples per section. |
| `scenario_synthesis.ipynb`              | Empirical companion to the scenario tutorial — runs the full pipeline on `ClaimsPaymentLog` in both encodings side-by-side, with the numbers from the paper. |
| `IssueTracker.zip`                      | Small, readable event log (~100 K events / 11 K cases, 9 distinct activities, 5 roles). Drives sections 0-5 of the main notebook. |
| `ClaimsPaymentLog.zip`                  | Heavier, denser event log (~90 K events, more activities and actor types). Drives the second half of the main notebook and both scenario notebooks. |
| `mine_and_export.py`                    | Minimal command-line script that mines a UCM, exports a `.jucm`, and renders a PNG. |

## Main tutorial notebook

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

## Scenario-synthesis notebooks

Two complementary notebooks focus on the scenario-synthesis layer:

- **`scenario_synthesis_tutorial.ipynb`** — pedagogical, one concept
  per section on small synthetic examples. Start here if you're new
  to scenario synthesis.
  1. Why sequence-variant clustering over-counts and how the choice
     signature fixes it.
  2. Your first `discover_scenarios` call and what it populates
     (variables, initializations, arc conditions).
  3. Loop counters and the `LoopEntryGuard` — why 0-iteration
     scenarios need special treatment.
  4. Variant-driven vs data-driven condition encodings side-by-side.
  5. Decomposition is orthogonal: flat and multi-map UCMs get the
     same scenario coverage.
  6. What the three CSV reports contain and how to audit a run.
- **`scenario_synthesis.ipynb`** — empirical demonstration on
  `ClaimsPaymentLog`. Runs the full pipeline in both encodings,
  reports fitness / compression / per-fork accuracy, and drops both
  `.jucm` files into `scenario_output/` so you can open them
  side-by-side in jUCMNav.

Both notebooks require `pm4py` (for reading XES). The data-driven
sections additionally require `scikit-learn`.

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
