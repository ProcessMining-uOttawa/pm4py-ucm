# pm4py-ucm demos

Five tutorial notebooks, two sample logs and a command-line script. The
notebooks are committed with their outputs, so they can be read on GitHub
without running anything; to run one, start Jupyter from this directory
(the main tutorial reads the logs by relative path) with `pm4py` installed
and the Graphviz `dot` binary on `PATH`.

| File                                    | What it is                                                       |
|-----------------------------------------|------------------------------------------------------------------|
| `pm4py_ucm_tutorial.ipynb`              | **Main tutorial notebook** — guided tour of model discovery on both sample logs: log filtering and renaming, rendering, performers, decomposition, `.jucm` and standard BPMN 2.0 export. The recommended starting point. |
| `scenario_synthesis_tutorial.ipynb`     | **Scenario-synthesis tutorial** — the scenario layer end to end: concurrency-aware variants, the `LoopEntryGuard`, both OR-fork condition encodings (variant-driven and **data-driven / decision-mining**, with the per-fork accuracy report), decomposed UCMs, and a capstone running both encodings on `ClaimsPaymentLog`. |
| `model_families_tutorial.ipynb`         | **Model-families + performance-overlays tutorial** — partitions `ClaimsPaymentLog` by case attributes, mines one model per combination, renders the stack/matrix grid, exports per-cell / combined / dynamic-stub-umbrella `.jucm` files, overlays frequencies and times on activities and edges (with the heat-map), and compares members via the family statistics + interactive HTML report. |
| `dashboards_tutorial.ipynb`             | **Dashboards tutorial** — builds the per-case fact table, computes widgets (KPIs, segmented breakdowns, tables) from the metric catalog, adds filters / targets / a scorecard, writes custom metrics in the **ƒ formula language**, charts every activity at once (the **Activity axis**), segments by **behavioural variants**, lets the log **suggest** whole dashboards, and exports a self-contained interactive HTML dashboard (plus an embedded mined model). |
| `sessions_tutorial.ipynb`               | **Sessions tutorial** — the web app's save / share / resume project files, driven from Python through the `web/sessions` API: the parameter registry, the settings file, the bundle, and loading either back. |
| `IssueTracker.zip`                      | Synthetic issue-tracking log with a simple, readable process (100,008 events / 11,284 cases, 9 activities, 5 roles, 10 resources). Drives sections 1-5 of the main notebook. |
| `ClaimsPaymentLog.zip`                  | Synthetic insurance claims-payment log with a denser process (78,126 events / 5,600 cases, 25 activities, 10 roles, 58 resources). Drives the main notebook from section 6, the scenario capstone, and the families / dashboards / sessions tutorials. |
| `mine_and_export.py`                    | Minimal command-line script that mines a UCM, exports a `.jucm`, and renders a PNG (optionally a `.bpmn`). |

## Main tutorial notebook

Open `pm4py_ucm_tutorial.ipynb` in JupyterLab, VS Code, or any
notebook-aware editor. Its sections:

1. Loading the event log, **filtering** it before mining (variants,
   activities, a time window) and **renaming / merging** activities — what
   the web app's log filters and rename dialog do.
2. Discovering a first UCM, rendering it as PNG and writing the `.jucm`.
3. Switching to the BPMN-flavoured rendering.
4. Mining performers giving priority to roles vs individual
   resources, with both visualisations.
5. Providing manual performer mappings — and watching the
   deterministic colour hashing follow the new team names.
6. Hierarchical decomposition: the four boundary rules
   (`on_root_sequence`, `on_parallel`, `on_alternative`, `on_loop`) in
   isolation, the combined `"auto"` preset, filtering to a single map, and
   rendering as **navigable SVG** (click a stub to reach its plug-in map).
7. Round-tripping a model through `.jucm` (open in jUCMNav, read
   it back here), and exporting it to standard **BPMN 2.0**.
8. Programmatic construction — both directly against the `UCM`
   object model and from a hand-built process tree.
9. Smaller helpers worth knowing: `discover_resources`,
   `discover_components`, and the underlying process tree.

Each section writes its outputs to a local `output/` directory so
you can open them in jUCMNav, peek at the XML, or include in a
report.

## Scenario-synthesis notebook

**`scenario_synthesis_tutorial.ipynb`** teaches the scenario layer one
concept per section on small synthetic examples, then finishes on a
real log:

1. Why sequence-variant clustering over-counts and how the choice
   signature fixes it.
2. Your first `discover_scenarios` call and what it populates
   (variables, initializations, arc conditions).
3. Loop counters and the `LoopEntryGuard` — why 0-iteration scenarios
   need special treatment — and sizing a loop so every branch can be shown.
4. The two OR-fork condition encodings — **variant-driven** (lossless)
   and **data-driven / decision-mining** — including how the decision
   tree selects features and how to read the per-fork accuracy report.
5. Decomposition is orthogonal: flat and multi-map UCMs get the same
   scenario coverage.
6. What the three CSV reports contain and how to audit a run.
7. A capstone running **both encodings on `ClaimsPaymentLog`** with
   real numbers, written side by side for jUCMNav.
8. **Running** the synthesized scenarios offline under jUCMNav's own
   traversal semantics, then measuring **coverage** (elements and path
   segments, separately, against the whole model) and comparing two
   scenarios **A vs B** — including painting either highlight onto the
   diagram as SVG.

Requires `pm4py` (for reading XES). The data-driven sections and the
capstone use `scikit-learn`, which installs with the package. Outputs land
in `tutorial_output/`. The simulator's semantics are specified in
[`docs/scenario_simulation.md`](../docs/scenario_simulation.md).

## Model-families notebook

**`model_families_tutorial.ipynb`** covers model families and performance
overlays on `ClaimsPaymentLog`:

1. Detecting the case-constant attributes usable as partition axes.
2. Previewing the partition (a coverage table) *before* mining.
3. Mining one UCM per combination and rendering the family as a
   stack / matrix grid with per-cell `n (%)` captions.
4. Exporting: one `.jucm` per cell (zip) and the combined single-file
   form with shared definitions.
5. Assembling the **umbrella** — the shared skeleton with dynamic
   stubs at the points of divergence, attribute-guarded plug-ins,
   resource variation as variation, and executable **path scenarios**
   (one per combination × behavioural variant).
6. Partitioning on **two attributes**, binning a numeric one, and
   filtering values.
7. **Performance overlays** — frequencies and times on activities and
   edges, both rendered and exported as jUCMNav metadata, the **heat-map**
   (colour and thickness by value), plus programmatic use of the same
   statistics (busiest activities, slowest hand-offs).
8. **Family statistics** — the per-member table, pairwise comparison of
   two members, and the self-contained interactive HTML report.

Outputs land in `output/families/`; every `.jucm` opens in jUCMNav.
See [`docs/model_families.md`](../docs/model_families.md) for the full
reference.

## Dashboards notebook

**`dashboards_tutorial.ipynb`** covers the dashboards layer on
`ClaimsPaymentLog`:

1. building the per-case **fact table** (`build_fact_table`) — the compact
   snapshot every widget reads;
2. the **metric catalog** and computing individual **widgets**
   (`compute_widget`) — KPIs, one-axis bars, two-axis tables;
3. **filters**, segmentation, the chart shapes, **targets**, and the
   **scorecard**;
4. the **ƒ custom-formula language** (`compile_formula`) — per-case
   expressions with a `where` clause, for metrics the catalog does not
   name;
5. the **Activity axis** — one widget covering every activity, as bars or
   as an activity × month heatmap — and the per-activity **rework rate**;
6. **behavioural variants** — the concurrency-aware clustering as a
   segment axis and filter (`build_fact_table(behaviour=…)`), and the
   heatmap showing what each variant consists of;
7. **suggested dashboards** (`suggest_dashboards`) — whole dashboards
   proposed from the log itself, each widget with its reason;
8. assembling a dashboard and **exporting** it as one self-contained
   interactive HTML file (`write_dashboard`) — the same artifact the web
   app's Dashboards view shows;
9. related exports — embedding a mined model as inline SVG, and the
   multi-section session report.

Outputs land in `output/dashboards/`. See
[`docs/dashboards.md`](../docs/dashboards.md) for the semantic contract
(every metric's exact definition, the ƒ grammar, and the engine's
rounding / weighting decisions) and
[`docs/dashboard_suggestions.md`](../docs/dashboard_suggestions.md) for
the rules behind the suggestions. Everything is also point-and-click in the
deployed web app (`web/streamlit_app_v6.py`, **Dashboards** view).

## Sessions notebook

**`sessions_tutorial.ipynb`** shows what a saved web-app project is, using
the `web/sessions` package directly (it lives in the repository, not in the
installed `pm4py_ucm` package):

1. the **parameter registry** — every persistable setting declared once;
2. a **project document** and the **settings file** (configuration only, no
   event data);
3. how **dashboards** travel in a project;
4. the **project bundle** — configuration plus the event log in one zip;
5. **resuming** — `load()` round-trips either file;
6. forward compatibility — unknown keys are preserved.

See [`docs/sessions.md`](../docs/sessions.md) for the design, and
[`web/README.md`](../web/README.md#10--save-share--resume-a-project) for
the same feature in the app.

## Mine-and-export script

`mine_and_export.py` is an end-to-end command-line example.

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
Add `--bpmn` to also write `out.bpmn`, a standard BPMN 2.0 file (opens in
bpmn.io or Camunda Modeler; plug-in maps nest as collapsed sub-processes).

`--decompose` turns on `decomposition="auto"`, which splits the discovered UCM into:

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
