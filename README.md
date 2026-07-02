# pm4py-ucm

[![tests](https://github.com/ProcessMining-uOttawa/pm4py-ucm/actions/workflows/tests.yml/badge.svg)](https://github.com/ProcessMining-uOttawa/pm4py-ucm/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org/)
[![License: GPL v3+](https://img.shields.io/badge/license-GPLv3%2B-blue.svg)](LICENSE)
[![Streamlit V1](https://img.shields.io/badge/Streamlit-V1%20model-FF4B4B?logo=streamlit&logoColor=white)](https://pm4py-ucm.streamlit.app/)
[![Streamlit V2](https://img.shields.io/badge/Streamlit-V2%20scenarios-FF4B4B?logo=streamlit&logoColor=white)](https://pm4py-ucm-scenarios.streamlit.app/)

**Use Case Map (UCM) extension for [PM4Py](https://github.com/process-intelligence-solutions/pm4py).**

`pm4py-ucm` adds first-class support for the [Use Case Map][ucm-wiki]
modelling notation — part of the ITU-T Z.151 *User Requirements Notation*
(URN) standard, supported by the open-source [jUCMNav][jucmnav] tool — to
PM4Py-style process-mining workflows.

The package is structured as a **drop-in companion** to PM4Py's existing
BPMN support: every public helper has the same shape (`read_*`, `write_*`,
`discover_*_inductive`, `view_*`, `convert_to_*`) so adopting UCM as an
additional output of process mining is a one-word change in user code.

```python
import pm4py
import pm4py_ucm

log = pm4py.read_xes("running-example.xes")
ucm = pm4py_ucm.discover_ucm_inductive(log)
pm4py_ucm.view_ucm(ucm)
pm4py_ucm.write_ucm(ucm, "running-example.jucm")  # opens in jUCMNav
```

**Four ways to get started:**

- [`demo/pm4py_ucm_tutorial.ipynb`](demo/pm4py_ucm_tutorial.ipynb) — end-to-end Jupyter walkthrough on a real claims-payment log (discovery, BPMN/UCM rendering, performer mining, hierarchical decomposition, `.jucm` round-trips).
- [`demo/scenario_synthesis_tutorial.ipynb`](demo/scenario_synthesis_tutorial.ipynb) — pedagogical tutorial for the **scenario-synthesis** layer: concurrency-aware variants, per-loop counters + `LoopEntryGuard`, variant-driven vs data-driven OR-fork conditions, transparent support for decomposed UCMs. Empirical companion in [`demo/scenario_synthesis.ipynb`](demo/scenario_synthesis.ipynb).
- [`web/streamlit_app.py`](web/streamlit_app.py) (V1, model-only, hosted at https://pm4py-ucm.streamlit.app/) and [`web/streamlit_app_v2.py`](web/streamlit_app_v2.py) (V2, model + scenarios, hosted at https://pm4py-ucm-scenarios.streamlit.app/) — click, don't code: upload an XES/CSV, tune the miner, download the result.
- The rest of this README — reference docs for the public API.

[ucm-wiki]: https://en.wikipedia.org/wiki/Use_Case_Maps
[jucmnav]: https://github.com/JUCMNAV/projetseg-update

## Web front-end

Two [Streamlit](https://streamlit.io) front-ends ship in the
[`web/`](web/) directory:

- **`streamlit_app.py` (V1)** — the original model-only flow. Upload an
  event log (XES or CSV), tune the inductive miner / decomposition /
  performer settings interactively, preview the diagram in UCM or BPMN
  notation, and download the rendered PNG plus the `.jucm` file.
- **`streamlit_app_v2.py` (V2)** — superset of V1. Adds a **Scenarios
  tab** that runs concurrency-aware variant clustering and synthesizes
  one executable jUCMNav `ScenarioDef` per variant. Both variant-driven
  and data-driven OR-fork encodings are exposed; the tab surfaces
  headline metrics (variant count, sequence variants, compression
  ratio, fitness %, per-fork condition-mining accuracies) and offers
  four downloads: the `.jucm` with the synthesized scenario group,
  `variants.csv`, `case_variant_map.csv`, and (data-driven mode)
  `condition_mining.csv`. Runs on flat and decomposed UCMs alike.

Both are deployed on Streamlit Community Cloud:

- V1 (model only): https://pm4py-ucm.streamlit.app/
- V2 (model + scenarios): https://pm4py-ucm-scenarios.streamlit.app/

![Overview of the PM4Py-UCM web interface](web/WebInterfaceOverview.png)

Run either locally with:

```bash
pip install -r web/requirements.txt
streamlit run web/streamlit_app.py       # V1 (model only)
streamlit run web/streamlit_app_v2.py    # V2 (model + scenarios)
```

See [`web/README.md`](web/README.md) for the full feature walkthrough and
Streamlit Community Cloud deployment instructions.

## Why UCM alongside BPMN?

BPMN is excellent for procedural choreographies, but it forces a single
abstraction level: every flow object is a step in *the* process. UCM, by
contrast, is a *scenario* notation. Its elements (path nodes — start
points, responsibilities, OR/AND forks and joins, stubs, timers, …) are
laid over a backdrop of **components**, which lets a single map describe a
behaviour that crosses architectural boundaries. This makes UCM a natural
target when the discovered process tree describes a workflow that spans
multiple services or organisational units, or when the goal is requirements
engineering rather than execution. See ITU-T Recommendation Z.151 for the
full notation reference.

## Installation

```bash
pip install pm4py-ucm           # core package + graphviz Python bindings
pip install pm4py-ucm[pm4py]    # also install pm4py for discovery
pip install pm4py-ucm[viz]      # add matplotlib for inline notebook display
pip install pm4py-ucm[dev]      # everything (pytest, pm4py, matplotlib)
```

The graphviz **system binary** must be on `PATH` for rendering (the
`graphviz` Python wheel only provides bindings):

```bash
# Debian / Ubuntu
sudo apt-get install graphviz
# macOS
brew install graphviz
# Windows
choco install graphviz
```

## Quick tour

### Build a UCM by hand

```python
from pm4py_ucm import UCM, write_ucm

ucm = UCM(name="OnlineShop")
m = ucm.add_map(name="ShoppingFlow")

start = m.add_node(UCM.StartPoint(name="start"))
login = m.add_node(UCM.RespRef(resp_def=ucm.get_or_add_responsibility("Login")))
fork  = m.add_node(UCM.OrFork(name="choose"))
search = m.add_node(UCM.RespRef(resp_def=ucm.get_or_add_responsibility("Search")))
browse = m.add_node(UCM.RespRef(resp_def=ucm.get_or_add_responsibility("Browse")))
join   = m.add_node(UCM.OrJoin())
checkout = m.add_node(UCM.RespRef(resp_def=ucm.get_or_add_responsibility("Checkout")))
end    = m.add_node(UCM.EndPoint(name="end"))

m.add_connection(start, login)
m.add_connection(login, fork)
m.add_connection(fork, search, condition="search")    # label on the OR branch
m.add_connection(fork, browse, condition="browse")    # label on the OR branch
m.add_connection(search, join)
m.add_connection(browse, join)
m.add_connection(join, checkout)
m.add_connection(checkout, end)

write_ucm(ucm, "online_shop.jucm")  # open in jUCMNav
```

### Mine a UCM from an event log

```python
import pm4py
import pm4py_ucm

log = pm4py.read_xes("log.xes")
ucm = pm4py_ucm.discover_ucm_inductive(log)
pm4py_ucm.view_ucm(ucm)                                # UCM notation
pm4py_ucm.save_vis_ucm(ucm, "diagram_ucm.png")         # UCM notation
pm4py_ucm.save_vis_ucm(ucm, "diagram_bpmn.png",        # BPMN notation
                       style="bpmn")
pm4py_ucm.write_ucm(ucm, "log.jucm")
```

`discover_ucm_inductive` is a thin wrapper around
`pm4py.discover_process_tree_inductive` followed by the bundled
process-tree → UCM converter, so all of PM4Py's tuning parameters for the
inductive miner are available via the `parameters` dict.

The PNG renderer supports two visual styles:

* **`style="ucm"` (default)** — Z.151 / jUCMNav notation: filled circle
  for the start point, perpendicular bar for the end point, `×` glyph
  with the responsibility name underneath, thick perpendicular bar for
  AND-fork / AND-join (synchronisation bars), small filled dot for
  OR-fork / OR-join, diamond reserved for stubs.
* **`style="bpmn"`** — BPMN-friendly look: activity boxes for
  responsibilities, gateway diamonds with `X` / `+` markers for
  XOR / AND gateways, thin-border start circle and thick-border end
  circle (the canonical BPMN end event).

Both styles preserve the swim-lane layout (one rectangle per
component, never overlapping unless nested) and wrap long
responsibility names onto two or three lines so the diagram stays
compact.

### `.jucm` layout matches the PNG layout

When writing a `.jucm` file, pm4py-ucm uses graphviz's `dot` engine to
compute the coordinates — exactly the same engine that drives the PNG
renderer. Open the resulting file in jUCMNav and you'll see the same
arrangement of nodes and component rectangles as in the rendered PNG.

If the graphviz binary isn't on `PATH`, the exporter falls back
silently to the bundled Sugiyama-style layouter so writing still
works; the visual result will be a layered drawing rather than a
graphviz one. Force the built-in layouter explicitly with:

```python
pm4py_ucm.write_ucm(ucm, "log.jucm",
                    parameters={"layout_engine": "builtin"})
```

### Mine performers too — surface them as URN components

If the log records who performed each activity (typically the
`org:resource` or `org:role` event attribute), pm4py-ucm can mine the
activity→performer mapping and use it to populate URN components
automatically: each unique performer becomes a `ComponentElement`, each
`Responsibility` is linked to its performer, and on every map the
`RespRef` symbol for that activity is drawn inside the component's
rectangle.

```python
import pm4py
import pm4py_ucm

log = pm4py.read_xes("log.xes")

# One-shot: mine + bind in a single call.
ucm = pm4py_ucm.discover_ucm_inductive(log, parameters={
    "resource_attribute": ["org:role", "org:resource"],  # priority list
})

# Or, the explicit three-step flow with more control.
performers = pm4py_ucm.discover_resources(
    log,
    attribute_priority=["org:role", "org:resource"],
    strategy="mode",        # or "first", "all", "unbound"
    min_support=0.0,        # default — pick the modal performer
                            # even when no single one owns a majority
)
ucm = pm4py_ucm.discover_ucm_inductive(log)
pm4py_ucm.bind_performers(ucm, performers)

pm4py_ucm.write_ucm(ucm, "log.jucm")
```

#### Component discovery vs activity binding

There are two related questions to answer when mining resources:

* **Which activities have a clearly-identified performer?** This is what
  `discover_resources` answers — it returns one performer per activity
  using the configured aggregation strategy (`mode` by default). An
  activity with many performers spread roughly equally gets bound to
  the modal one; an activity with no `org:resource` annotation at all
  is omitted.
* **Which actors exist in the log?** This is what `discover_components`
  answers — it returns the full *vocabulary* of every distinct
  performer value that appears anywhere in the log, sorted. The
  high-level `discover_ucm_inductive` calls both, so every distinct
  actor becomes a URN `ComponentElement` even when no single
  responsibility is cleanly bound to it. The unbound components show
  up in the URN tree but not as rectangles on the map.

This matters for logs with a dispersed resource pool — e.g. the BPI
Road Traffic Fines log has 148 distinct `org:resource` values where the
modal performer of *Create Fine* owns only 5.7% of events. Without these
two adjustments (modal binding without majority support, and full
component-vocabulary discovery), only one of the 148 actors would
appear in the resulting URN spec.

In the rendered diagram and the exported `.jucm`, each activity now
appears inside the rectangle of the team that owns it. See
[Definitions vs references](#definitions-vs-references) below for the
data-model story.

### Read an existing jUCMNav file

```python
from pm4py_ucm import read_ucm

ucm = read_ucm("requirements.jucm")
print(ucm)                          # → UCM(name='…', maps=…, responsibilities=…)
for n in ucm.maps[0].nodes:
    print(n)
```

## Process-tree → UCM mapping

The converter implements the following correspondences between PM4Py's
process tree operators and UCM constructs. UCM has no native loop, so loops
are encoded as an OR-fork/OR-join pair guarded by `[redo]` / `[exit]`
conditions — the canonical idiom in jUCMNav.

| Process tree                            | UCM construct                                                                 |
|-----------------------------------------|-------------------------------------------------------------------------------|
| Activity leaf with label `A`            | `RespRef` referencing a `Responsibility` named `A`                            |
| Silent (τ) leaf                         | A direct `NodeConnection` with no responsibility                              |
| `→ (sequence)` of children              | Children chained with `EmptyPoint` connectors (collapsed by simplifier)        |
| `× (xor)` choice                        | `OrFork` → branches → `OrJoin`                                                |
| `+ (parallel)`                          | `AndFork` → branches → `AndJoin`                                              |
| `o (interleaving)`                      | Treated as `+ (parallel)`                                                     |
| `∨ (or)`                                | Treated as `× (xor)`                                                          |
| `↻ (loop, do, redo)`                    | `OrJoin` → *do* → `OrFork` with `[redo]` back-edge and `[exit]` forward edge   |

After conversion, an `EmptyPoint` simplification pass collapses chains of
unnamed degree-2 connectors so that the resulting map renders compactly.

## Hierarchical decomposition

For complex process trees, a single flat UCM map quickly becomes visually
overwhelming. The optional `decomposition=` keyword on
`discover_ucm_inductive` and `convert_to_ucm` splits the result into a
**root map plus plug-in (sub-)maps connected by Stubs**. The same
`PluginBinding` machinery the package already uses for hand-built models
ties everything together — every round-trip stays byte-stable through the
exporter and importer.

```python
import pm4py
import pm4py_ucm

log = pm4py.read_xes("running-example.xes")

# Default: one flat map (current behaviour, byte-stable with old exports)
flat = pm4py_ucm.discover_ucm_inductive(log)
assert len(flat.maps) == 1

# Hierarchical: a root map + one plug-in per "phase" / branch / loop body
hier = pm4py_ucm.discover_ucm_inductive(log, decomposition="auto")
assert len(hier.maps) >= 1

pm4py_ucm.view_ucm(hier)                   # all maps stacked in one PNG
pm4py_ucm.view_ucm(hier, map="loop_Test")  # just one plug-in
pm4py_ucm.write_ucm(hier, "out.jucm")      # opens in jUCMNav as root+plug-ins
```

The `decomposition` argument accepts:

| Value          | Effect                                                                                 |
|----------------|----------------------------------------------------------------------------------------|
| `None` / `"off"` | No decomposition. Output byte-stable with pre-decomposition exports.                |
| `"auto"`       | All three boundary rules on, `max_leaves_per_map=20`, `min_leaves_to_decompose=4`.     |
| `"aggressive"` | Same boundary rules, `max_leaves_per_map=10`.                                          |
| `dict`         | Any subset of the six keys below; unspecified keys take the `"auto"` defaults.         |

Configurable keys:

| Key                         | Default | Meaning                                                                                                                                  |
|-----------------------------|---------|------------------------------------------------------------------------------------------------------------------------------------------|
| `on_root_sequence`          | `True`  | Each child of a top-level `→` becomes a plug-in. Root map reads as a chain of phase stubs.                                                |
| `on_parallel`               | `True`  | Each `+` branch becomes a plug-in. AND-fork/join vertical-expansion cost is replaced by a single stub per branch.                          |
| `on_alternative`            | `True`  | Each `×` (XOR) / `∨` (OR) branch becomes a plug-in. OR-fork/join stays on the parent map; alternative bodies move into per-branch plug-ins. |
| `on_loop`                   | `True`  | Each `*` operator's entire expansion becomes a plug-in. Parent map reads as forward flow with one stub for the iteration. A loop at the *root* of the tree is wrapped in a synthetic sequence so the root map gets a single loop stub. |
| `max_leaves_per_map`        | `20`    | Hard cap; over-sized maps recursively force-cut the largest operator-subtree until the cap is met.                                         |
| `min_leaves_to_decompose`   | `4`     | Floor — subtrees smaller than this stay inlined regardless of rules.                                                                       |
| `balance_ratio`             | `0.2`   | Sibling share threshold under `→` and `+`. A child needs at least this fraction of the parent's leaves to be pulled out independently.     |

Unknown keys raise `ValueError`.

When the UCM has multiple maps, `view_ucm` and `save_vis_ucm` compose
every map vertically into a single PNG (`Pillow` does the stacking).
Each panel has a title strip and adjacent panels are separated by a thin
horizontal rule. Bound stubs gain a `→ <plug-in name>` external label so
the reader can follow each stub to its plug-in map.

## Scenario synthesis

The `discover_scenarios` pipeline turns an event log into an
*executable* UCM: a `.jucm` carrying one URN `ScenarioDef` per
behavioural variant discovered in the log, with typed variables,
per-loop integer counters, and mutually-exclusive OR-fork conditions
that let jUCMNav step through each scenario deterministically.

```python
import pm4py
import pm4py_ucm

log = pm4py.read_xes("log.xes")

# Variant-driven (default) — lossless replay of every observed variant
ucm, clustering = pm4py_ucm.discover_scenarios(log)
pm4py_ucm.write_ucm(ucm, "log.jucm")
pm4py_ucm.write_variants_report(clustering, "variants.csv")
pm4py_ucm.write_case_variant_map(clustering, "case_variant_map.csv")

# Data-driven — mine per-fork decision trees over case attributes,
# emit conditions like `Broker == Spot_Health_Insurance && Claim_Value <= 1417646`
ucm_dd, _ = pm4py_ucm.discover_scenarios(
    log, condition_strategy="data-driven",
    decision_tree_max_depth=3,
)
group = ucm_dd.scenario_groups[0]
pm4py_ucm.write_ucm(ucm_dd, "log.data_driven.jucm")
pm4py_ucm.write_condition_mining_report(group, "condition_mining.csv")
```

### What the synthesizer populates

- **`EnumerationType` `VariantId`** with values `[v1, v2, …]`
  (variant-driven only), plus one `EnumerationType` per case-constant
  string attribute the log carries (data-driven only).
- **`Variable`s** — a `variant_id` enum (variant-driven) or one variable
  per mined case attribute (data-driven), plus one `integer` per loop
  operator in the discovered tree (contextually named e.g.
  `Loop_AnalyzeClaim`).
- **One `ScenarioDef` per variant**, each with an `Init` per variable
  (variant-driven initialises `variant_id`; data-driven initialises
  each attribute to a representative value for its variant — mode for
  enum/bool, scaled median for integer), plus a per-loop counter init
  capped at `max_loop_iterations` (default 2), plus `ScenarioStartPoint`
  / `ScenarioEndPoint` refs. Names carry a short discriminator
  (`v3_TwoCloseAssessmen`, `v8_QuickAssessment`); descriptions start
  with a plain-English `Intent:` line.
- **Arc conditions on every non-loop OR-fork** — variant-driven writes
  `variant_id == v_i` disjunctions (with an inside-loop variant that
  combines the disjunction with counter thresholds); data-driven writes
  mined boolean expressions over case attributes.
- **A `LoopEntryGuard` OR-fork** per loop, spliced between the loop's
  upstream arc and its `LoopJoin`, with mutually-exclusive `counter > 0`
  / `counter <= 0` conditions. This restores the semantics
  "counter = number of body executions" — including zero. When a
  loop's post-loop continuation is a `Stub`, an `OrJoin` is spliced
  before the stub so its plug-in binding stays complete.

### Concurrency-aware variants

Two traces that differ only in the interleaving order of activities
inside a parallel block share the same **choice signature** and
therefore the same variant. `X → (Y ∥ Z) → W` traces `X-Y-Z-W` and
`X-Z-Y-W` cluster as one; sequence-variant analysis splits them. The
`compression_ratio` (concurrency-aware / sequence-variant count) on
`ClaimsPaymentLog` is 0.146, meaning naive clustering over-counts by
~7×.

Loop iteration counts are coarsened to `{0, 1, ≥2}` by default to keep
the variant count small; pass `coarsen_loops=False` to distinguish
every iteration count.

### Two condition-encoding strategies

| Strategy       | Arc conditions                                                                | Trade-off                                                                                                     |
|----------------|-------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------|
| `variant`      | `variant_id == v_i` disjunctions per branch                                   | **Lossless** — replaying scenario `v_i` reproduces `v_i`'s choice signature exactly. Doesn't *explain* choices. |
| `data-driven`  | Boolean expressions over case attributes, mined per-fork by decision trees    | Business-readable rules on every fork; requires case-constant attributes; **abandons with a warning** otherwise. |

Inside-loop OR-forks (XORs sitting inside a loop body): variant-driven
combines `variant_id` with the enclosing counter to distribute branches
across iterations. Data-driven falls back, only for inside-loop forks,
to a deterministic `true`/`false` split — case attributes are static per
case and can't disambiguate per-iteration choices.

### Reports

Three CSVs alongside the `.jucm`:

- **`variants.csv`** — one row per variant with frequency, sequence-
  variant count, linearization count, partial-order expression, and a
  truncated case-ID list. Trailing rows for `noise` and `totals`
  (fitness + compression).
- **`case_variant_map.csv`** — one row per case, mapping case ID to
  variant ID (or `noise` for non-conforming cases).
- **`condition_mining.csv`** — data-driven mode only. One row per
  `(OR-fork, branch)` with accuracy, sample size, feature set,
  `skipped_reason` (`inside_loop`, `no_labelled_cases`), and the
  post-minimisation expression emitted on the arc.

### Interaction with decomposition

`discover_scenarios` accepts the same `decomposition=` argument as
`discover_ucm_inductive` and honours it fully: OR-forks that land in
plug-in maps receive the same conditions they would in the flat case,
and loops pushed into plug-in maps get their counter machinery
(LoopEntryGuard, decrement responsibility) spliced into the correct
map. Each UCM `OrFork` / `OrJoin` / `LoopFork` / `LoopJoin` carries a
stable id linking it back to the tree node it came from, so
correlation survives arbitrary decomposition boundaries.

### Learning path

- [`demo/scenario_synthesis_tutorial.ipynb`](demo/scenario_synthesis_tutorial.ipynb)
  — pedagogical walkthrough with a small synthetic example per section.
- [`demo/scenario_synthesis.ipynb`](demo/scenario_synthesis.ipynb)
  — empirical demonstration on `ClaimsPaymentLog` (24 variants,
  compression 0.146) in both encodings.
- The Scenarios tab in
  [`web/streamlit_app_v2.py`](web/streamlit_app_v2.py) — no code needed.

## Module layout

```
pm4py_ucm/
├── api.py                                 # high-level read_/write_/discover_/view_
├── objects/ucm/
│   ├── obj.py                             # UCM object model (URN metamodel)
│   ├── conversion/from_process_tree.py    # PM4Py process tree → UCM
│   ├── conversion/decomposition.py        # hierarchical decomposition rules + presets
│   ├── exporter/variants/jucm.py          # UCM → jUCMNav .jucm (XMI 2.0)
│   ├── importer/variants/jucm.py          # jUCMNav .jucm → UCM
│   └── layout/layouter.py                 # auto-layout for jUCMNav graphical view
├── algo/discovery/
│   ├── ucm/
│   │   ├── algorithm.py                   # discovery dispatcher (mirrors BPMN)
│   │   └── variants/inductive.py          # inductive-miner-based discovery
│   ├── variants/                          # concurrency-aware variant clustering
│   │   ├── choice_signature.py            # replay algorithm + signature canonicalisation
│   │   └── clustering.py                  # per-variant clustering + fitness / compression
│   └── scenarios/                         # scenario synthesis on top of a UCM + clustering
│       ├── synthesis.py                   # variables, ScenarioDefs, LoopEntryGuard, conditions
│       ├── decision_mining.py             # data-driven strategy: sklearn tree → jUCMNav expr
│       ├── expression_minimizer.py        # boolean simplifier for mined expressions
│       └── reports.py                     # variants.csv / case_variant_map.csv / condition_mining.csv
└── visualization/ucm/
    ├── visualizer.py                      # apply / view / save (mirrors BPMN)
    └── variants/classic.py                # graphviz-based renderer
```

The object model in `objects/ucm/obj.py` mirrors the [jUCMNav EMF
metamodel][jucmnav-meta] (`urn`, `urncore`, `ucm.map`) closely enough to
emit XMI files that load directly in jUCMNav.

[jucmnav-meta]: https://github.com/JUCMNAV/projetseg-update/tree/master/seg.jUCMNav/src/seg/jUCMNav/emf

## Definitions vs references

For the full Python object model in one picture, see
[`docs/ucm_class_diagram.svg`](docs/ucm_class_diagram.svg) (vector,
paper-ready) or the [PNG preview](docs/ucm_class_diagram.png). The
[PlantUML source](docs/ucm_class_diagram.puml) can be re-rendered or
extended for figures in academic papers.

UCM keeps a sharp distinction between a *definition* (a reusable named
concept declared once at the URN level) and each visual *reference* to it
on a map. The object model surfaces this distinction explicitly:

| Definition (one) | Reference (many) | Where reference lives |
|---|---|---|
| `UCM.Responsibility` (an activity) | `UCM.RespRef` (the "✕" symbol) | inside a map's `nodes` list |
| `UCM.ComponentElement` (an actor / team / role / system) | `UCM.ComponentRef` (the labelled rectangle) | inside a map's `cont_refs` list |

A definition lives on the URN container (`ucm.responsibilities`,
`ucm.components`). Each *visual occurrence* on a diagram is a separate
reference object that points back to the definition via `resp_def` (for
`RespRef`) or `cont_def` (for `ComponentRef`). Many references may share
the same definition — that is exactly what lets the same activity or the
same actor appear in multiple places without being declared twice.

A third link — `Responsibility.performer` — runs *between definitions*:
it expresses the semantic fact that a given activity is performed by a
given actor/team. This is a logical binding, independent of layout. The
visual binding (`RespRef.cont_ref → ComponentRef`) is derived from it
whenever a map is built or `UCM.bind_performers()` is called.

```text
─ URN level ──────────────────────────────────────────
 Responsibility "Login" ──performer──> ComponentElement "AuthService"
            ▲                                  ▲
            │ resp_def                         │ cont_def
─ Map level ──────────────────────────────────────────
   RespRef #5  ─────cont_ref─────────>  ComponentRef #12
   (drawn as ✕)                         (drawn as a rectangle)
```

Build them through the helpers:

```python
from pm4py_ucm import UCM

ucm = UCM(name="Example")
m   = ucm.add_map(name="MainMap")

# --- DEFINITIONS (one per concept, declared on the URN container) ---
login_def = ucm.get_or_add_responsibility("Login")          # Responsibility
actor_def = ucm.get_or_add_component(                       # ComponentElement
    "User", kind=UCM.ComponentElement.Kind.ACTOR)

# --- REFERENCES (many per definition, drawn on the map) -------------
login_node = m.add_node(UCM.RespRef(resp_def=login_def))    # RespRef
actor_box  = m.add_component_ref(actor_def, width=200, height=120)

# A path node may declare which component reference visually contains it:
login_node.cont_ref = actor_box
```

In the exported `.jucm` you'll see this distinction reflected as the
bidirectional links jUCMNav uses internally:

```xml
<responsibilities name="Login" id="35" respRefs="36"/>
<components       name="User"  id="45" contRefs="46" kind="Actor"/>
…
<nodes   xsi:type="ucm.map:RespRef" id="36" respDef="35" contRef="46" …/>
<contRefs xsi:type="ucm.map:ComponentRef" id="46" contDef="45" nodes="36" …/>
```

The `respRefs`/`contRefs` attributes on a definition list back-references
to every occurrence of it; `respDef`/`contDef` on the reference point
forward to the definition. The exporter computes the back-references
automatically — you only need to set the forward links.

## Compatibility with jUCMNav

The exporter produces files in the modern jUCMNav format (matches output
of jUCMNav 5.5 and later):

* Root element `<urn:URNspec>` declares four namespaces — `xmi`, `xsi`,
  `urn` (`http:///urn.ecore`), and `ucm.map` (`http:///ucm/map.ecore`).
  The `urncore` and `grl` packages do not need declarations because
  their concepts use unqualified element names inside the URN
  containment tree.
* URN-level metadata (`urnVersion="1.27"`, `specVersion="4"`, `name`,
  `author`, `created`, `modified`, `nextGlobalID`) lives on the root
  as **attributes**, not child elements.
* Children of `<urn:URNspec>` appear in the canonical order
  **ucmspec → grlspec → urndef**. Children of `<urndef>` appear in the
  order **responsibilities → specDiagrams → components**. Children of a
  `<specDiagrams>` UCMmap appear as **nodes → contRefs → connections**.
* Connections are **anonymous** — `<connections>` carries no `id`
  attribute. Endpoints use integer node IDs (`source="18" target="19"`).
* Nodes refer to their connections via XPath fragments
  (`succ="//@urndef/@specDiagrams.0/@connections.7"`), the only style
  available because connections lack IDs.
* `<condition>` elements distinguish a human-readable `label` (e.g.
  `"TrueBranch"`) from the logical `expression` (default `"true"`).
* `nextGlobalID` is exactly `max(all IDs) + 1` — the integer ID jUCMNav
  would assign to the next newly-created element.
* An auto-layouter places nodes on a layered left-to-right grid before
  export so the diagram is immediately readable in jUCMNav's editor.

## Testing

```bash
pip install -e .[dev]
python -m unittest discover -s tests -v
```

The default test suite does **not** require PM4Py to be installed: the
process-tree → UCM converter accepts duck-typed trees (`operator.value`,
`children`, `label`), which the tests use to exercise every operator in
isolation. The `tests/test_export_import.py` suite verifies that round-trip
through the jUCMNav XMI back-end is byte-deterministic.

## License

GPL-3.0-or-later, matching the upstream [PM4Py][pm4py-gpl] license.
The jUCMNav metamodel reproduced (in spirit) here is itself distributed
under EPL-2.0; this package only re-implements the metamodel in Python and
does not bundle any jUCMNav source.

[pm4py-gpl]: https://github.com/process-intelligence-solutions/pm4py/blob/release/LICENSE
