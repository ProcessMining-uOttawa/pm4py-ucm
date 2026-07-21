# Python code export from a session

> **Status: P0 + P1 implemented** (`web/sessions/codegen.py`, exposed in the app's
> **Project** rail as *Export as Python*). The core-API emitter, the scenario and
> family pipelines, the `.py` **and** `.ipynb` flavours, the CLI entry point, and
> the golden reproduction test all ship. **Not yet built:** dashboards codegen
> (P2, needs the reusable dashboards-module extraction) and the optional local-LLM
> comment pass (P3). The two friction points in §6 were handled by **inlining**
> the transform into the emitted script (see the note there), not by promoting a
> shared library helper.

## 1. Thesis

A GUI analysis is a black box; a script is a white box. pm4py-ucm already
stores a session as a **config-only declarative spec** — the 24-parameter
registry (`web/sessions/registry.py`) plus the `ProjectDoc`, where *only inputs
are stored and every derived artifact recomputes*. That spec **is** the
pipeline. So code export is a **deterministic template emitter** that walks the
registry and emits public-API calls — **no LLM in the core** (an LLM-authored
pipeline would be non-reproducible, the opposite of the goal).

Because `config-vs-derived` is enforced, the exported script is a **provably
faithful replay**: run it and it reproduces the *same* `.jucm` / report /
dashboard the GUI produced — not an approximation.

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

Nearly 1:1 with the public API (`pm4py_ucm.__all__`):

| Session config | Emitted code |
|---|---|
| `LogRef` + `csv_columns` | `pm4py.read_xes(...)` / read CSV + `format_dataframe(...)` |
| `filter_spec` (incl. `rename_map`) | pre-mining transform (see §6a) |
| `noise_threshold`, `min_support`, `notation`, `decomposition` | `discover_ucm_inductive(log, parameters=…, decomposition=…)` |
| `resource_attribute` | `discover_resources(...)` + `bind_performers(...)` |
| `overlay_nodes`, `overlay_edges`, `overlay_heatmap*` | `annotate_performance(...)` (+ heat-map render args) |
| `family_*` | `discover_ucm_family(...)`, `write_ucm_family`, `assemble_ucm_family`, `write_family_report` |
| `scenario_*` | `discover_scenarios(...)` + `write_variants_report` / `write_case_variant_map` / `write_condition_mining_report` |
| `active_view`, `compare_*` | comments / which artifacts the script writes |

## 5. Architecture

- A **Streamlit-free emitter** at `web/sessions/codegen.py`, unit-testable like
  the rest of `web/sessions/` — input is a `ProjectDoc` (or a `{id: value}`
  gather), output is a `str` of Python.
- Small, composable **section emitters** (imports → load → transform → mine →
  performers → overlay → export → family → scenarios), each guarded by whether
  its config is non-default, so the script only contains what the session used.
- **Two output flavours**: a `.py` script and a `.ipynb` notebook (same section
  emitters, wrapped differently) — the notebook doubles as the personalised
  tutorial.
- **Parameterised entry point**: emit a `def run(log_path): ...` (or a CLI
  `argparse`) so the pipeline is directly automatable on a new log path, with
  the session's log as the default.

## 6. The two refactors (honest scope)

Two things the emitter needs live in the app today, not the library:

**(a) Filters + rename** — `_apply_log_filters` (and the rename map inside
`filter_spec`) live in `streamlit_app_v5.py`. Options:
- *Inline* the pandas/pm4py transform into the script (the tutorial already
  shows the pattern), or
- **Promote to a tiny shared helper** (e.g. `pm4py_ucm`-level or a
  `web/shared/` module) that both the app and the generated script import.
- **Shipped decision: inline.** The emitter inlines faithful copies of
  `read_log`, `apply_rename`, `apply_log_filters`, `resource_params` and
  `_coerce_str_object` into the generated script, so it is **self-contained and
  portable** — it needs only `pandas` / `pm4py` / `pm4py_ucm` on the path,
  nothing from this repo. **Drift is guarded two ways:** the golden test asserts
  the emitted pipeline reproduces the app's mining output byte-for-byte, and a
  dedicated test parses the app's `_apply_log_filters` for the `filter_spec` keys
  it reads and asserts every one appears in the emitted transform — so a new app
  filter can't be silently dropped from generated scripts. (This caught exactly
  that: the **cycle-time percentile** filter `duration_pct`, added to the app
  after the exporter, was not in the inlined copy; it is now mirrored.) A shared
  helper remains the cleaner long-term fix and is left as future work.

  **The heat-map is reproduced in the exported artifacts.** The emitter emits
  `OVERLAY_HEATMAP` / `OVERLAY_HEATMAP_SCOPE` and inlines `model_heat_params()` /
  `report_heat()` helpers (mirroring the app's `_model_heat_kwargs` /
  `_heat_classic_kwargs` / `_heat_svg_kwargs`, including the shared cross-member
  span for the `"family"` scale), which thread the colour/thickness into
  `save_vis_ucm` (model PNG), `save_vis_ucm_family` (grid PNG) and
  `write_family_report` (the HTML report's embedded per-cell images) — so an
  exported analysis looks like what the user saw. The public `write_family_report`
  gained a `heat=` kwarg for this. The performance **sub-lines** are carried
  regardless (they ride in the `.jucm` metadata via `annotate_performance`).

**(b) Dashboards** — the fact table, metric catalog, ƒ-formula language and
widgets live entirely in `streamlit_app_v5.py`. "Generate code that rebuilds the
dashboards" needs a **reusable `pm4py_ucm`-level dashboards module first**;
otherwise the emitter re-implements app internals. **Treat dashboards codegen as
its own later slice** (§7, P2), gated on that extraction.

## 7. Phasing

| Phase | Deliverable | Status |
|---|---|---|
| **P0** | Core-API emitter: load → transform → mine → decomposition → performers → overlay → export `.jucm`/PNG. Transform **inlined** (6a). **Golden test** (§8). *Export as Python* in the Project rail. | ✅ shipped |
| **P1** | Family + scenarios + statistics-report emission; `.ipynb` flavour; parameterised `run(log_path)` / CLI entry point. | ✅ shipped |
| **P2** | Dashboards codegen — *after* the reusable dashboards-module extraction (6b). | ⬜ not started |
| **P3 (optional)** | LLM-generated docstring/comments, produced **locally** (agnostic + local seam from `docs/goal_insights.md`); never the backbone. | ⬜ not started |

## 8. Testing — the golden property

The regression test *is* the pitch:

1. Build a `ProjectDoc` for a fixture log + config.
2. Emit the script; execute it in a subprocess sandbox.
3. Assert its `.jucm` (and report) are **byte-identical** to what the
   library/GUI path produces for the same config — leveraging the existing
   byte-stable export guarantee.
4. A drift guard asserts every registry id is handled by some section emitter
   (mirrors the existing `collect()` contract), so the emitter can't silently
   fall behind a new setting.

## 9. UI

A **"⬇ Export Python"** control in the sidebar's **Project** group, next to
*Save settings* / *Save project bundle* — same `_proj_values` gather feeds the
emitter. Offer `.py` and `.ipynb`. Caption notes which views/artifacts the
script reproduces and whether dashboards are included (P2).

## 10. Open questions

- Log handling in the emitted script: reference a path, read a bundled sample by
  name, or embed a loader for the bundle zip?
- `.py` vs `.ipynb` default (notebook reads better as a tutorial; script reads
  better for automation).
- How much inline explanatory comment by default (before any optional LLM pass).
- Whether the shared transform helper (6a) lands in `pm4py_ucm` (public, but
  couples the lib to a web concern) or a `web/shared/` module (keeps the lib
  clean; the script imports from `web`).
