# pm4py-ucm documentation

The narrative documents behind `pm4py-ucm`: what each number means, how each
feature is built, and why it was built that way. For installation, a quick
start and the API tour, read the [root README](../README.md); for runnable
walk-throughs, the notebooks in [`demo/`](../demo/README.md).

The documents here are of five kinds, and it helps to know which one you are
reading. Each says which it is under its title.

- **Contract** — a specification the code is held to. A test fails when the
  code and the document disagree, so it describes what the package does
  *now*.
- **Guide** — how to use a feature, with its options and limitations.
- **Design note** — how a built feature works and why it was shaped that
  way: the decisions, the alternatives rejected, what changed between the
  design and the code. Where it and the code differ on a detail, the code
  and the [changelog](../CHANGELOG.md) are right.
- **Measurements** — a dated study. True for the versions and logs it names.
- **Proposal** — direction for something not built yet, or built only in
  part; it says which part.

## Where to look

| If you want to know… | Read |
|---|---|
| what a number on a model, report or dashboard means, exactly | [`metrics.md`](metrics.md) |
| how to define a dashboard widget, or what the engine computes for it | [`dashboards.md`](dashboards.md) |
| why a dashboard was proposed for your log, and what the rules are | [`dashboard_suggestions.md`](dashboard_suggestions.md) |
| how to compare a process across case attributes | [`model_families.md`](model_families.md) |
| how a UCM becomes a BPMN 2.0 diagram, element by element | [`bpmn_export.md`](bpmn_export.md) |
| why a log is slow to mine or replay, and what actually helps | [`miner_performance.md`](miner_performance.md) |
| what scenario simulation, coverage and A/B comparison do | [`scenario_simulation.md`](scenario_simulation.md) |
| what a saved project contains, or how to add a setting to the app | [`sessions.md`](sessions.md) |
| how the app turns a session into a Python script or notebook | [`code_export.md`](code_export.md) |
| how the Python object model fits together | [the class diagram](#the-ucm-class-diagram) |
| where the project is heading on AI and goal models | [`ai_insights.md`](ai_insights.md), [`goal_insights.md`](goal_insights.md) |

## The documents

### Metrics and dashboards

| Document | Kind | What it covers | Held to by |
|---|---|---|---|
| [`metrics.md`](metrics.md) | Contract | Every activity, edge, process, replay and traversal metric: its definition, its denominator, the order of aggregation, and the semantics decisions (what counts as rework, how concurrency is treated). | [`test_metric_validation.py`](../tests/test_metric_validation.py), with hand-computed expectations |
| [`dashboards.md`](dashboards.md) | Contract | The per-case fact table, the metric catalog and the ƒ formula language, segmentation (including the Activity axis and behavioural variants), chart types and their guards, targets, and the widget spec a dashboard file is made of. | [`test_dashboards.py`](../tests/test_dashboards.py), including Python–JavaScript engine parity |
| [`dashboard_suggestions.md`](dashboard_suggestions.md) | Design note + proposal | How whole dashboards are proposed from the log alone (tier 1, built), and the designs for having an external LLM write a dashboard definition (tier 2) and deriving one from a goal model (tier 3). | [`test_dashboard_suggestions.py`](../tests/test_dashboard_suggestions.py) for tier 1 |

Notebook: [`dashboards_tutorial.ipynb`](../demo/dashboards_tutorial.ipynb).

### Models

| Document | Kind | What it covers | Held to by |
|---|---|---|---|
| [`model_families.md`](model_families.md) | Guide | Partitioning a log by one or two case attributes and mining one model per combination; the overarching model with dynamic stubs; performance overlays; the family statistics report; known limitations. | [`test_family.py`](../tests/test_family.py), [`test_family_stats.py`](../tests/test_family_stats.py), [`test_performance.py`](../tests/test_performance.py) |
| [`bpmn_export.md`](bpmn_export.md) | Design note (built in 0.8.5) | The UCM → BPMN 2.0 element mapping, components as one pool with nested lanes, how the layout is carried over, performance annotations, families, and the decisions taken (call activities, inclusive gateways, no task typing). | [`test_bpmn_export.py`](../tests/test_bpmn_export.py), against the OMG schema |
| [`scenario_simulation.md`](scenario_simulation.md) | Design note (built in 0.8.0) | Running a UCM's scenarios the way jUCMNav does, highlighting what a scenario covers, comparing two scenarios, and exporting the log for the selected variants; the simulator's API, the decisions, and the measurements taken while it was built. | [`test_scenario_traversal.py`](../tests/test_scenario_traversal.py), [`test_scenario_coverage.py`](../tests/test_scenario_coverage.py) |
| [`miner_performance.md`](miner_performance.md) | Measurements (2026-08-15) | What mining and replay cost across twelve logs, why there is no faster path that preserves the model, why `multi_processing=True` does not help, how to estimate a run before committing to it, and what the package and the app adopted from it. | [`test_complexity.py`](../tests/test_complexity.py), for the screening code only |

Notebooks: [`pm4py_ucm_tutorial.ipynb`](../demo/pm4py_ucm_tutorial.ipynb),
[`model_families_tutorial.ipynb`](../demo/model_families_tutorial.ipynb),
[`scenario_synthesis_tutorial.ipynb`](../demo/scenario_synthesis_tutorial.ipynb).

### The web app

| Document | Kind | What it covers | Held to by |
|---|---|---|---|
| [`sessions.md`](sessions.md) | Design note (built in 0.7.1) | Saving, sharing and resuming a project: the session parameter registry, the drift guard that stops a new setting being silently dropped, file formats, migration, and the dashboards bridge. §17 is the checklist for adding a setting. | [`test_sessions.py`](../tests/test_sessions.py), [`test_sessions_registry.py`](../tests/test_sessions_registry.py) |
| [`code_export.md`](code_export.md) | Design note (built in 0.7.3; the optional LLM comment pass is not) | Exporting a session as a runnable Python script or notebook: the registry → code mapping and the property that the script reproduces what the app showed. | [`test_sessions_codegen.py`](../tests/test_sessions_codegen.py) |

Notebook: [`sessions_tutorial.ipynb`](../demo/sessions_tutorial.ipynb). The
app itself is described in [`web/README.md`](../web/README.md).

### Proposals

| Document | Kind | What it covers |
|---|---|---|
| [`ai_insights.md`](ai_insights.md) | Proposal, partly built | LLM-assisted analysis and recommendations, the grounding contract (the LLM never sources a number) and a provider-agnostic, local-first seam that the newer external-LLM route in `dashboard_suggestions.md` §3 has not been reconciled with. The two deterministic parts have shipped: the decomposition auto-tuner (`suggest_decomposition`, 0.7.7) and the partition advisor (`rank_partition_attributes`, 0.7.8). No LLM capability is built. |
| [`goal_insights.md`](goal_insights.md) | Proposal, not built | Synthesising a GRL goal model from the mined UCM and its metrics — indicators, softgoals, contribution links, per-cohort strategies, traceable to the UCM — with optional LLM naming specified in `ai_insights.md`. |

## The UCM class diagram

The Python object model in one picture — definitions and references, maps,
path nodes, components, stubs and their bindings, variables and scenarios:

- [`ucm_class_diagram.svg`](ucm_class_diagram.svg) — vector, paper-ready
- [`ucm_class_diagram.png`](ucm_class_diagram.png) — preview
- [`ucm_class_diagram.puml`](ucm_class_diagram.puml) — PlantUML source, to
  re-render or extend

## API reference

The API reference is generated from the docstrings, not kept here. It is
published at <https://processmining-uottawa.github.io/pm4py-ucm/> and
redeployed on each push to `main`. To build it locally:

```bash
pip install pdoc
pdoc pm4py_ucm pm4py_ucm.algo pm4py_ucm.api pm4py_ucm.objects pm4py_ucm.util pm4py_ucm.visualization -o docs/api
```

Then open `docs/api/index.html`. The output directory is git-ignored.

## Adding or changing a document

- **Open with what it is.** A status line under the title — contract, design
  note, measurements or proposal, and whether it is built — saves the reader
  from finding out halfway down. Update it when the feature ships.
- **Links are checked.** `tests/test_release_consistency.py` fails on a local
  link or image that does not resolve, in every `docs/*.md` and in the
  READMEs. Relative links are fine here; only the root README needs absolute
  GitHub URLs, because PyPI renders it.
- **List it.** Add the document to the tables above and to the
  *Documentation* section of the root README.
- **Keep confidential logs out.** Measurements on non-public logs appear as
  aggregate counts under neutral labels, as in `miner_performance.md`.
