# Scenario simulation, coverage and A/B comparison

**Status:** design note, built (0.8.0, refined in 0.8.1;
`pm4py_ucm.algo.scenario_traversal`, `pm4py_ucm.algo.scenario_coverage`, and
the **Simulation** section of the app's Scenarios view). It describes the
feature as it stands, the decisions behind it, and the measurements taken
while it was built in five stages.

## The feature

Run a UCM's scenarios the way jUCMNav runs them, then use the result
visually:

* **Simulate** — execute the scenarios and report what jUCMNav's Problems
  view would report.
* **Highlight + coverage** — colour the elements and connections a selected
  set of scenarios walked, and report what fraction of the model that is,
  with per-element statistics on hover.
* **A vs B** — pick two scenarios and colour what each covered plus what
  both did, again with hover statistics.
* **Keep** — export the event log reduced to the cases of the selected
  variants.

## Two things called "traversal"

`pm4py_ucm/algo/traversal.py` (`compute_traversal_stats`) replays the **event
log** over the model to produce the performance overlay's traversal counts.
`pm4py_ucm/algo/scenario_traversal.py` executes **scenarios** against the
model and knows nothing about a log. Same word, unrelated inputs and outputs.
The collision was resolved by disambiguation, not renaming: the root README
already documented `required_max_hit_count` and `check_traversal`, so those
names were public in practice. They keep their names, and
`pm4py_ucm/__init__.py` groups the scenario exports under a comment saying
that the neighbouring `compute_traversal_stats` is unrelated.

## The simulator

A token leaves each enabled start point; an OR-fork picks the branch whose
guard holds; an AND-fork spawns a token per arm; an AND-join waits for every
arm. All of it is exported from `pm4py_ucm`:

| | |
|---|---|
| `traverse_scenario(ucm, scenario, max_hit_count=1000, max_steps=2_000_000)` | run one `ScenarioDef` → `ScenarioTraversalResult` |
| `traverse_all(ucm, …)` | run every scenario of every group; raises `NoScenariosError` when there is none, so an empty answer never means "ran nothing" |
| `check_traversal(ucm, …)` | every `TraversalProblem` across every scenario — empty when the model runs |
| `required_max_hit_count(ucm, …)` | `(minimum setting, scenario, element)` — the smallest jUCMNav *maximum hit count* preference at which the model traverses cleanly |

**Problems** are the kinds jUCMNav reports: `blocked_and_join`,
`infinite_loop`, `end_point_not_reached`, `no_branch_enabled` /
`multiple_branches_enabled`, `unsupported_node`, and for stubs
`multiple_plugins_enabled`, `no_plugin_selected`, `ambiguous_plugin_return`,
`ambiguous_plugin_entry`. `infinite_loop` is measured against jUCMNav's
maximum-hit-count preference, which is a setting rather than a property of
the model — `required_max_hit_count()` tells the two apart.

**The result says what was visited.** `ScenarioTraversalResult.visits` is the
net hit count per element entered, covering connections as well as nodes,
keyed by `(type name, model id)` rather than object identity so the record
can be compared, serialised and matched against a rendered diagram. "Net"
because a node that blocks is un-counted when it re-enters the waiting list,
as jUCMNav does; `.visited` is the subset that actually executed, and
`.visit_labels` carries a label per key for tooltips. The result also carries
`problems`, `responsibilities`, `reached_end_points`, `steps`,
`peak_hit_count` / `peak_hit_element`, and `.ok`.

**Stubs are traversed**, so a decomposed model — root map plus plug-ins — can
be simulated, as jUCMNav does:

* A token entering a stub is handed to the bound plug-in's start point; a
  plug-in end point is treated as a *return* and the path resumes on the
  stub's out-arc, so coverage spans every map.
* **Static** stub: exactly one plug-in; several bound is reported
  (`multiple_plugins_enabled`) rather than silently picked.
* **Dynamic** stub: every binding whose precondition holds is entered, a
  binding without a precondition counting as true; none holding is reported
  (`no_plugin_selected`). This is an assumption, not a validated match —
  the generators emit only static stubs, so there was nothing to check it
  against.
* Two ambiguities are reported rather than guessed, both impossible in a
  generated model: `ambiguous_plugin_return` (one end point bound by several
  stubs — the scheduler carries nodes rather than tokens, so which caller to
  return to cannot be recovered) and `ambiguous_plugin_entry`.

**Out of scope:** `WaitingPlace`, `Timer`, `Connect`, `FailurePoint` and
`Anything`, because the generators never produce them (checked across
`objects/ucm/conversion/` and `algo/discovery/`). The importer *can* read
them from a hand-authored `.jucm`, so an imported model may contain one. That
case is `unsupported_node`: **reported, not skipped**, so the simulator never
claims success on a model it did not really run.

## Coverage and comparison

`pm4py_ucm.algo.scenario_coverage`, exported as `coverage`, `compare`,
`Coverage` and `Comparison`:

* `coverage(ucm, results)` → `Coverage`: `.covered`, `.total`, `.uncovered`,
  `.fraction`, summed `.hits` per key, `.labels`, `.scenarios`.
  `.elements` / `.paths` (and `.element_fraction` / `.path_fraction`) split
  the count into path **nodes** and the **segments** between them, and
  `.by_kind()` gives `{element type: (covered, total)}`. One percentage hides
  that a run can walk every responsibility and still miss segments — an
  alternative carrying no responsibility is a segment and nothing else — so
  the split says whether what is left uncovered is behaviour or just empty
  paths.
* `compare(ucm, result_a, result_b)` → `Comparison`: `.a_only`, `.b_only`,
  `.both`, plus `.union`, `.neither` and `.agreement` (Jaccard: the share of
  everything either scenario walked that both walked).
* `coverage_render(ucm, cov)` and `comparison_render(ucm, cmp)` (in the
  module, not re-exported at the top level) translate a result into
  `{"colors": …, "tooltips": …}` keyed by `id(element)`. The keying boundary
  lives here: coverage works in model ids so it can leave the process, the
  renderer works in object identity because that is what graphviz names are
  built from.

**Both entry points refuse results that do not belong to the model.** Element
ids are allocated lazily, so two copies of one model — for instance either
side of Streamlit's cache, which pickles — could number the same element
differently, and a comparison across them still returned plausible numbers.
`UCM.assign_path_ids()` pins the allocation order and is called before any id
is read; a visited key the model has no element for raises `ValueError`
rather than swelling "walked by neither" (0.8.1).

## Rendering

Colour is an existing concept rather than a new renderer: the performance
heat-map already colours nodes and edges per element in `classic.apply`.
Coverage colouring is a second **value source** into that pipeline —
`classic.apply` takes `coverage_colors` / `coverage_tooltips` and **drops the
heat-map when they are present**. `model_to_svg(..., coverage=…)` passes the
render dict through, on both the single-map and the stacked multi-map path.

**Hover text is injected by rewriting graphviz's `<title>` elements.** The
original plan was graphviz's `tooltip=` attribute, but graphviz honours
`tooltip` only when the element *also* carries a `URL`, because the tooltip
becomes the generated anchor's `xlink:title`. What graphviz always emits is a
`<title>` holding its internal object name, which embeds a memory address and
was shown on hover on every diagram. Rewriting those titles is both how the
hover text arrives and a small improvement: after a coverage render the
elements carry labels, and the uncovered ones say "not covered" rather than
nothing. No custom JavaScript is involved.

## Decisions

| question | decision |
|---|---|
| Where does it live? | The **bottom of the Scenarios view**, not a new view |
| Relationship to the performance overlay | Both stay; **only one renders at a time** |
| Stub traversal | In scope |
| `Timer` / `WaitingPlace` / `Connect` / `FailurePoint` | Out of scope — never generated |
| Coverage denominator | **Whole model**, every map included |
| A/B unit | **Two individual scenarios**, not two scenario groups |
| Colours | A = dark green, B = dark orange, both = purple |

### Why the Scenarios view rather than a new one

Scenarios are *defined* there, so definition and execution stay adjacent.
"Compare" already exists and means comparing **family members**, so an A/B
*scenario* comparison in its own view would overload that word. One
**Simulation** section at the bottom, with a mode toggle (*Coverage* /
*Compare A vs B*), since both modes share almost all of their machinery.

### Why the overlay stays, and why only one shows at a time

They answer different questions. The performance overlay is **log-derived** —
what actually happened across every case: frequency, sojourn, waiting time.
The simulator is **model-derived** — what this model does under one
scenario's initializations. Traversal cannot produce a median sojourn time,
and the log cannot say which branch a hypothetical scenario would take.

They are mutually exclusive *at display time* because they compete for the
same channel: the heat-map already paints nodes and edges. One colour source
at a time, and the app says so when it suspends the heat-map — the heat-map
defaults **on** whenever overlay metrics are picked, so without the notice
the user would watch it vanish with no explanation.

Replacing the overlay was a **non-goal**: its settings are persisted in every
saved project, emitted by the code exporter, used by family assembly and
written to the `.jucm` as jUCMNav properties lines.

### Why whole-model coverage

One scenario walks one path through a model that contains every path, so a
single scenario reads low, and lower still on a decomposed model. That is the
honest number; the app captions it so that it does not read as a fault.

### The colours

Red/blue read badly beside the heat-map's red ramp, so A/B became dark green
and dark orange. Making both "a bit dark" nearly matched their lightness
(luma 95.5 vs 101.4), which would have merged them on a greyscale printout
and under strong colour-vision deficiency, so the green was deepened to open
a 44-point luma gap: the pair separates by lightness as well as hue. Purple
stays for the intersection and sits between the two in lightness; if it ever
proves hard to read, add a second channel (dash or thickness) rather than
re-hueing.

## In the app

The **Simulation** section appears once scenarios have been synthesised:

* every scenario is run, with a run/clean/problems summary and a table of
  the problems jUCMNav would list;
* **Coverage** mode — multi-select scenarios, then **Elements** and **Paths**
  percentages against the whole model and a by-element-kind breakdown;
* **Compare A vs B** mode — two selectors, the A-only / B-only / both
  counts, the agreement, and how many elements neither walked;
* the highlighted model rendered inline, every element hoverable;
* **Export the log for the selected variants** — the selection is a set of
  behavioural variants, and clustering already knows which cases fell into
  each, so the same picker that says "highlight these" also says "keep
  these" (`filter_log_by_variants`, 0.8.1). Cases that replayed as noise
  belong to no variant, so they drop out by construction rather than by a
  threshold.

### Persistence and export

* Four registry parameters — `simulation_mode`, `simulation_scenarios`,
  `simulation_a`, `simulation_b` — so a saved project resumes into the same
  simulation ([`sessions.md`](sessions.md) §5). The mode is stored as an
  **identifier**, with the radio's words supplied by `format_func` from
  `_SIM_MODES`, the same shape `active_view` uses. The scenarios are stored
  by **name**; a re-mine that drops one degrades (A/B fall back to their
  defaults, Coverage filters the pick list) rather than highlighting the
  wrong scenario.
* All four are main-area widgets in a view that is usually not the active one
  when a project loads, so they take the Family route: `_sticky_get` on save,
  `_sticky_seed` on restore. The one deviation is the Coverage multiselect,
  which is clamped by hand instead of through `_sticky`'s `options=` — that
  treats an empty list as "restore the default", which would undo a
  deliberately cleared selection on the next rerun.
* The code exporter emits `run_simulation(ucm_s, clustering, log)` after
  `run_scenarios`, which replays the scenarios and writes `simulation.svg`
  plus a summary CSV (`simulation_coverage.csv` by kind, or
  `simulation_compare.csv` with the A-only / B-only / both / neither
  partition), and the selected variants' sub-log as `simulation_log.csv` /
  `.xes`. The notebook gets the same as a cell, previewed inline
  ([`code_export.md`](code_export.md) §4).

## Measurements

Taken on the bundled `ClaimsPaymentLog` sample as each stage landed; the
scenario count differs between rows because the synthesis settings did.

**Flat model** (24 synthesised scenarios, all clean, 210 model elements):

| | |
|---|---|
| one scenario | 110/210 = **52.4%** of the whole model |
| union of five | 178/210 = **84.8%** |
| A vs B | A-only 7, B-only 23, both 103 |

**Decomposed model** (`decomposition="auto"`: 6 maps, 247 elements, 24
scenarios), before and after stub traversal:

| | clean | responsibilities run by scenario 1 | problems |
|---|---|---|---|
| before | 0/24 | 1 | 24 x `unsupported_node`, 24 x `end_point_not_reached` |
| after | **24/24** | **16** | none |

Coverage on the decomposed model: 55.9% for one scenario, 98.8% for the
union of all 24, with a single scenario touching 5 of the 6 maps. With the
elements/paths split and every scenario selected: elements 100%, paths 97.3%.

**In the running app** (18 scenarios, all clean): A/B `v1_QuickAssessment`
vs `v2_AnalyzeClaim` gave 7 / 23 / 121 at 80% agreement with 83 walked by
neither. A Compare selection saved from the **Model** view — with the
simulation widgets garbage collected — wrote `"simulation_mode": "compare"`
and both names, and resuming that file into a fresh session came back on the
same pair with the same numbers.

## How it was built

Five stages, all in 0.8.0: (1) surface the simulator's visit record and
export it; (2) traverse stubs; (3) coverage, comparison and rendering;
(4) the Scenarios-view section; (5) persistence and export. Stage 5 also
found that the session drift guards were still parsing the superseded V5
app; they now read the deployed one (see [`sessions.md`](sessions.md) §6).
