# Python code export from a session

**Status:** design note, built (0.7.3, dashboards in 0.7.4, simulation in
0.8.0; `web/sessions/codegen.py`, exposed in the app's **Project** group as
*Export as Python*). The emitter, the scenario, simulation, family and
dashboards pipelines, the `.py` and `.ipynb` flavours and the reproduction
tests all ship. Not built: the optional LLM comment pass (§7).

## 1. Thesis

A GUI analysis is a black box; a script is a white box. pm4py-ucm already
stores a session as a **config-only declarative spec** — the session
parameter registry (`web/sessions/registry.py`, see
[`sessions.md`](sessions.md) §5) plus the `ProjectDoc`, where *only inputs
are stored and every derived artifact recomputes*. That spec **is** the
pipeline. So code export is a **deterministic template emitter** that walks the
registry and emits public-API calls — **no LLM in the core** (an LLM-authored
pipeline would be non-reproducible, the opposite of the goal).

Because `config-vs-derived` is enforced, the exported script is a **faithful
replay**: run it and it reproduces the *same* `.jucm` / report / dashboard
the GUI produced — not an approximation (§8 says exactly what is tested).

## 2. Business value

1. **Reproducibility & auditability** — "show me exactly how you got this" is
   mandatory in regulated / academic work. The script is the provenance record,
   and the registry guarantees it is complete.
2. **Operationalisation** — turn a one-off exploration into a scheduled job:
   re-run monthly on fresh data, regenerate dashboards/reports. The
   "graduate from GUI to pipeline" maturation path.
3. **Handoff & version control** — ship a colleague a script, not a click-path;
   diff two analyses; code-review a pipeline.
4. **Personalised tutorial** — the emitted script is the best teaching artifact:
   the library API by example, on the user's own log and choices. It extends the
   project's notebook pedagogy automatically.
5. **Extensibility escape hatch** — once in Python over a public, pip-installable
   API, the user can add custom metrics or wire into any data stack. The GUI
   ceiling stops being an adoption blocker.

## 3. Originality

- Most GUI analytics tools export **nothing**, or a **proprietary/opaque**
  config (Celonis PQL; KNIME/Alteryx workflow formats). Emitting **plain,
  runnable Python over a public API** is portable with **zero lock-in**, and it
  drops straight into the broader PM4Py ecosystem.
- The registry makes it a **deterministic replay**, which is stronger than
  "export approximates what you did."

## 4. The mapping (registry → code)

Nearly 1:1 with the public API (`pm4py_ucm.__all__`). Every value becomes an
upper-case constant in the script's `CONFIG` block (`NOISE_THRESHOLD`,
`FILTER_SPEC`, …), which is the part a user edits:

| Session config | Emitted code |
|---|---|
| `LogRef` (name, kind, CSV columns) | `DEFAULT_LOG`, `LOG_KIND`, `CSV_COLUMNS` and an inlined `read_log(...)` — XES, zipped XES or CSV, sniffing encoding and delimiter as the app does |
| `filter_spec` (incl. `rename_map`, `attr_expr`) | inlined `apply_log_filters(log, FILTER_SPEC)` (see §6a) |
| `noise_threshold`, `notation`, `decomposition` | `pm4py.discover_process_tree_inductive(log, noise_threshold=…)`, then `discover_ucm_inductive(log, parameters={"process_tree": tree, …}, decomposition=…)`; `notation` is the `style=` of every render |
| `resource_attribute`, `min_support` | an inlined `resource_params()` that builds the `resource_attribute` / `resource_parameters` keys passed in `parameters` |
| `overlay_nodes`, `overlay_edges`, `overlay_replay`, `overlay_heatmap*` | `annotate_performance(...)` (+ heat-map render args). When a `traversal_*` metric is selected the script first calls `compute_traversal_stats(tree, log)` and passes it — the emitted `wants_traversal()` helper gates that, so a session without those metrics pays nothing. |
| `family_*` | `discover_ucm_family(...)`, `write_ucm_family`, `save_vis_ucm_family`, `assemble_ucm_family`, `compute_family_stats`, `write_family_report` |
| `scenario_*` | `discover_scenarios(...)` on the same noise-thresholded tree + `write_variants_report` / `write_case_variant_map` / `write_condition_mining_report` |
| `simulation_*` | `traverse_all(ucm_s)` + `coverage(...)` / `compare(...)`, rendered through `coverage_render` / `comparison_render` into `simulation.svg` plus a summary CSV — **and** `filter_log_by_variants(...)` into `simulation_log.csv` / `.xes`, the log cut down to the selected variants, because in the app the same picker chooses what to highlight and what to keep. Emitted only with the scenarios section, since it replays the scenarios that section synthesizes. |
| `ProjectDoc.dashboards` | a `DASHBOARDS` constant + `run_dashboards(log, ucm)` (see §6b) |
| `active_view`, `compare_*` | not emitted — view state with no effect on the pipeline. `csv_columns` is emitted from the `LogRef`. |

## 5. Architecture

- A **Streamlit-free emitter** at `web/sessions/codegen.py`, unit-testable like
  the rest of `web/sessions/` — input is a `ProjectDoc`, output is a `str`:
  `generate_script(doc, *, out_dir="pm4py_ucm_output", include_scenarios=False,
  include_family=None, include_dashboards=None)`, and `generate_notebook(...)`
  with the same arguments.
- Ordered **blocks** (header → imports → config → helpers → model → scenarios
  → simulation → family → dashboards → `run` → `__main__`). Family and
  dashboards default to *auto* (on when the session picked family attributes
  / carries dashboards); scenarios are opt-in at the API, because a scenario
  re-mine is costly and a session carries scenario settings even when the
  view was never used. The app's three check-boxes default to on; a section
  the session did not use is a no-op at run time.
- **Two output flavours**: a `.py` script and a `.ipynb` notebook (same
  blocks, wrapped differently) — the notebook doubles as the personalised
  tutorial.
- **Parameterised entry point**: `run(log_path=DEFAULT_LOG)` plus an
  `argparse` main — `python pipeline.py [log] [--out DIR]` — so the pipeline
  is directly automatable on a new log, with the session's log as the default.
- **Deterministic**: the same project always emits the same text. The
  `GENERATOR_VERSION` written in the header is held equal to the package
  version by a test.

## 6. What the emitter had to carry

Two things the emitter needs live in the app, not the library.

**(a) Filters + rename — inlined.** `_apply_log_filters` (and the rename map
inside `filter_spec`) live in the Streamlit app. The alternative was a shared
helper that both the app and the generated script import; it would either
couple the library to a web concern or make the script import from `web`. So
the emitter inlines faithful copies of `read_log`, `apply_rename`,
`apply_log_filters`, `resource_params` and `_coerce_str_object` into the
generated script, which is therefore **self-contained and portable** — it
needs only `pandas` / `pm4py` / `pm4py_ucm` on the path, nothing from this
repo. **Drift is guarded two ways:** the golden test (§8), and a dedicated
test that parses the app's `_apply_log_filters` for the `filter_spec` keys it
reads and asserts every one appears in the emitted transform — so a new app
filter can't be silently dropped from generated scripts. (This caught exactly
that: the **cycle-time percentile** filter `duration_pct`, added to the app
after the exporter, was not in the inlined copy; it is now mirrored.)

**The heat-map is reproduced in the exported artifacts.** The emitter emits
`OVERLAY_HEATMAP` / `OVERLAY_HEATMAP_SCOPE` and inlines `model_heat_params()` /
`report_heat()` helpers (mirroring the app's `_model_heat_kwargs` /
`_heat_classic_kwargs` / `_heat_svg_kwargs`, including the shared cross-member
span for the `"family"` scale), which thread the colour/thickness into
`save_vis_ucm` (model PNG), `save_vis_ucm_family` (grid PNG) and
`write_family_report` (the HTML report's embedded per-cell images, through
its `heat=` argument) — so an exported analysis looks like what the user
saw. The performance **sub-lines** are carried regardless (they ride in the
`.jucm` metadata via `annotate_performance`).

**The replay opt-out travels with the metrics, and stays a live switch.**
`OVERLAY_NODES` / `OVERLAY_EDGES` hold the metrics the session *picked*;
`OVERLAY_REPLAY` holds whether it allowed the traversal replay. An inlined
`_effective_overlays()` resolves the pair at **run time** into the
`NODE_METRICS` / `EDGE_METRICS` the pipeline annotates with, swapping each
traversal metric for its event-based counterpart when the replay is off —
exactly the substitution the app makes at render time, so the script
reproduces what the user saw and `wants_traversal()` correctly skips the
replay. Resolving late rather than baking the fallback into the emitted
constants is deliberate: flipping `OVERLAY_REPLAY = True` in the generated
script recovers the conserving counts without re-picking any metric, which
is the sort of edit the export exists to make possible.

**(b) Dashboards — no new module was needed.** The dashboards engine is a
reusable library package (`pm4py_ucm.algo.dashboards`) that exposes
`build_fact_table` + `write_dashboard`; only the interactive *builder* lives
in the app. And a project already **carries its dashboard specs**
(`ProjectDoc.dashboards`, the versioned island-registry envelope). So the
emitter unwraps the specs at emit time into a `DASHBOARDS` data constant, and
emits `run_dashboards(log, ucm)` that builds the fact table over the same
filtered log and calls `write_dashboard(...)` **once** with the whole registry
(`dashboards=` / `active=`) → **one** self-contained interactive
`dashboards.html` whose read-only header switcher moves between the
dashboards. When a dashboard segments or filters by behavioural variant, the
script first clusters the log on the same noise-thresholded tree, so its
`v1`, `v2`, … are the app's. The mined UCM is rendered to SVG for both
notations (with the heat-map) and passed as `renders` / `model_svg` **only
when a dashboard actually pins the model**, so a pinned-model widget shows the
model instead of a grey box and nothing is embedded for dashboards that don't
display it.

Every raster artifact is also written as **vector `.svg`**: `run_model` writes
`model.svg` next to `model.png`, and `run_family` writes `family_grid.svg` next
to `family_grid.png` (with the heat-map).

**The notebook is an interactive tutorial, not a single `run()`.** The `.py`
keeps the function-plus-`run()` structure (good for automation); the `.ipynb`
instead defines each stage and **immediately invokes it**, showing the
intermediate result inline — the loaded log (`.head()`), the case counts before
and after filtering, the mined model image, the variants table, the family grid,
the dashboard file list — so the notebook reads like a personalised walkthrough.
It is emitted as nbformat 4.5 JSON by hand, cell ids included: 4.5 requires them,
and Jupyter's habit of patching a missing one in is documented to end. The ids
are **positional**, not random, so exporting the same project twice still yields
byte-identical files.

## 7. What is not built

- **LLM-generated docstrings/comments** for the emitted script. If added, it
  stays optional and outside the emitter, which must remain deterministic.
  How an LLM would be involved at all is the stance question recorded in
  [`ai_insights.md`](ai_insights.md) §3.
- **A shared transform helper** in place of the inlined copy (§6a). The
  inlined copy is guarded, so this is a matter of tidiness, not correctness.

## 8. Testing — the golden property

Two layers (`tests/test_sessions_codegen.py`):

1. **Structure, fast.** The emitted script compiles, is deterministic (same
   input, same text), carries each config value, and emits each optional
   section only when asked. The notebook is valid nbformat 4.5 with stable
   cell ids.
2. **Golden.** Build a `ProjectDoc` for a bundled sample + config, emit the
   script, run it in a subprocess, and assert its `model.jucm` equals what
   the same public-API pipeline produces directly — byte for byte once the
   exporter's wall-clock `created` / `modified` stamps are neutralised. A
   second golden test does the same for the simulation partition.

Drift guards: every registry id must appear in the emitted script or be on
the test's explicit ignore set (`active_view`, `compare_a`, `compare_b`,
`csv_columns`), so the emitter can't silently fall behind a new setting; and
the emitted transform must cover every filter key the app reads (§6a).

What the golden test does **not** do is drive the Streamlit app: it compares
against the public-API calls the app makes, and the inlined-transform guard
covers the part of the app's pipeline that is not public API.

## 9. UI

**Export as Python** sits in the sidebar's **Project** group, under *Save
settings* / *Save project bundle* — the same `_proj_values` gather feeds the
emitter. Three check-boxes (scenario synthesis, model family, dashboards; all
on by default) choose the sections, and one button downloads
`<log>_pipeline.zip` holding both `<log>_pipeline.py` and
`<log>_pipeline.ipynb`.

## 10. Questions settled during the build

- **Log handling in the emitted script:** it references the session's log by
  file name (`DEFAULT_LOG`) and takes any path on the command line; it does
  not read a project bundle.
- **`.py` vs `.ipynb`:** both, in one zip — the script for automation, the
  notebook as the tutorial.
- **Shared transform helper:** not adopted; the transform is inlined (§6a).
