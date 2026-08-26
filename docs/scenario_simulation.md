# Scenario simulation, coverage and A/B comparison

Design note for **0.8.0**. Written 2026-08-25 against `main` at v0.7.12,
before any code. It records what already exists, the gaps between that and
the feature, the decisions taken, and the order the work should happen in.

Nothing here is implemented yet. Where this note says "today", it describes
v0.7.12.

## The feature

Run a UCM's scenarios the way jUCMNav runs them, then use the result
visually:

* **Simulate** — execute selected scenarios and report what jUCMNav's
  Problems view would report.
* **Highlight + coverage** — colour the elements and connections a selected
  set of scenarios walked, and report what fraction of the model that is,
  with per-element statistics on hover.
* **A vs B** — pick two scenarios and colour what each covered plus what
  both did, again with coverage and hover statistics.

## What already exists

`pm4py_ucm/algo/scenario_traversal.py` (617 lines, 23 tests in
`tests/test_scenario_traversal.py`) already simulates jUCMNav's traversal: a
token from each enabled start point, an OR-fork picking the branch whose
guard holds, an AND-fork spawning a token per arm, an AND-join waiting for
every arm. It reports the same problem kinds jUCMNav does —
`blocked_and_join`, `infinite_loop`, `end_point_not_reached`,
`no_branch_enabled` / `multiple_branches_enabled`, `unsupported_node` — and
models jUCMNav's *maximum hit count* preference, with
`required_max_hit_count()` to tell a preference setting apart from a real
model fault.

Two things about its status shape the plan:

* **It is not exposed.** It is not exported from `pm4py_ucm/__init__.py` and
  there are zero references to it anywhere under `web/`. 0.8.0 is its debut
  in the product, not an extension of something users already use.
* **The name `traversal` is already taken, for something else.**
  `pm4py_ucm/algo/traversal.py` (`compute_traversal_stats`, which *is*
  exported) replays the **event log** over the model to produce the
  performance overlay's traversal counts. `scenario_traversal.py` executes
  **scenarios** against the model. Same word, unrelated inputs and outputs.

## Gaps

### 1. The result does not say what was visited

`ScenarioTraversalResult` carries `responsibilities`, `reached_end_points`,
`steps`, `peak_hit_count` and `peak_hit_element`. Highlighting and coverage
need the *set* of elements and connections entered, with per-element hit
counts.

The bookkeeping already exists internally — `_Traversal` counts hits per
`_elem_key(el)` — it is simply not surfaced. This is the cheapest item on
the list, and everything else depends on it.

### 2. Stubs are unsupported, so decomposed models cannot be simulated — **closed in Stage 2**

Today `_UNSUPPORTED` contains `Stub`, `WaitingPlace`, `Timer`, `Connect`,
`FailurePoint` and `Anything`. `Stub` being in that tuple means a decomposed
model — root map plus plug-ins — reports `unsupported_node` and stops.
Decomposition is a headline feature and jUCMNav traverses into plug-in maps,
so this is the largest single piece of work in 0.8.0 and the one most likely
to move the schedule.

The other four are **out of scope**, because the generators never produce
them. Checked against `objects/ucm/conversion/` and `algo/discovery/`:

| node kind | mentions in the generation path | in scope |
|---|---|---|
| `Stub` | 128 | **yes** |
| `WaitingPlace` | 0 | no |
| `Timer` | 0 | no |
| `FailurePoint` | 0 | no |
| `Connect` | 0 as a node kind — the 90 textual hits are the verb `_connect()` | no |

The importer *can* read all four from a hand-authored `.jucm`, so an
imported model may still contain one. That case is already handled the right
way: `unsupported_node` is **reported, not skipped**, so the simulator never
claims success on a model it did not really run. Keep that property.

### 3. No hover text, but the pipeline supports it

`visualization/ucm/svg.py` contains no `<title>` or tooltip handling today.
It does not need custom JavaScript: rendering goes `classic.apply` →
graphviz `Digraph` → `.pipe(format="svg")`, and graphviz's `tooltip=`
attribute emits an SVG `<title>`, which browsers render as a native hover
tooltip.

Colour is likewise an existing concept rather than a new renderer: the
performance heat-map already colours nodes *and* edges per element
(`heatmap_node` / `heatmap_edge` in `classic.apply`). Coverage colouring is a
second **value source** into that same pipeline.

## Decisions

| question | decision |
|---|---|
| Where does it live? | The **bottom of the Scenarios view**, not a new view |
| Relationship to the performance overlay | Both stay; **only one renders at a time** |
| Stub traversal | In scope |
| `Timer` / `WaitingPlace` / `Connect` / `FailurePoint` | Out of scope — never generated |
| Coverage denominator | **Whole model** |
| A/B unit | **Two individual scenarios** for now, not two scenario groups |
| Colours | A = dark green, B = dark orange, both = purple |

### Why the Scenarios view rather than a new one

Scenarios are *defined* there, so definition and execution stay adjacent.
"Compare" already exists and means comparing **family members**, so an A/B
*scenario* comparison in its own view would overload that word. And the view
has room — it is the second-smallest in the app:

| view | lines (v0.7.12) |
|---|---|
| Dashboards | 132 |
| **Scenarios** | **246** |
| Model | 270 |
| Compare | 399 |
| Family | 527 |

One collapsible **Simulation** section at the bottom, with a mode toggle
(*Coverage* / *Compare A vs B*), since both modes share almost all of their
machinery.

### Why the overlay stays, and why only one shows at a time

They answer different questions. The performance overlay is **log-derived** —
what actually happened across every case: frequency, sojourn, waiting time.
The simulator is **model-derived** — what this model does under one
scenario's initializations. Traversal cannot produce a median sojourn time,
and the log cannot say which branch a hypothetical scenario would take.

They are mutually exclusive *at display time* because they compete for the
same channel: the heat-map already paints nodes and edges on a red ramp,
which would collide with red-for-A. One colour source at a time, chosen by
the user.

Replacing the overlay is a **non-goal**, worth recording because the blast
radius would be large: 63 references in `streamlit_app_v6.py`, plus
`web/sessions/codegen.py` (exported scripts), `web/sessions/registry.py`
(`overlay_nodes`, `overlay_edges`, `overlay_replay`, `overlay_heatmap*` —
**persisted in every saved project**, so removing them would break resume),
`families/assembly.py`, and the `.jucm` export where every metric becomes a
jUCMNav properties line.

## Plan

### Stage 1 — surface the simulator — **done**

* `ScenarioTraversalResult.visits` — the net hit count per element entered,
  covering connections as well as nodes, keyed by `(type name, model id)`
  rather than object identity so the record can be compared, serialised and
  matched against a rendered diagram. `.visited` is the subset that actually
  executed, and `.visit_labels` carries a label per key for tooltips.
* Exported from `pm4py_ucm/__init__.py`.
* **Name collision: resolved by disambiguation, not renaming.** The plan
  originally assumed renaming was free because nothing imported the module.
  That was wrong — the root README documents
  `from pm4py_ucm.algo.scenario_traversal import required_max_hit_count`
  and `check_traversal`, so those names are already public in practice.
  They keep their names; the exports are grouped under a comment stating
  plainly that the neighbouring `compute_traversal_stats` is unrelated.
  Later stages should give *new* entry points unambiguous names rather than
  rename these.

Measured on `ClaimsPaymentLog` (24 synthesised scenarios, all clean, 210
model elements) once this landed:

| | |
|---|---|
| one scenario | 110/210 = **52.4%** of the whole model |
| union of five | 178/210 = **84.8%** |
| A vs B | A-only 7, B-only 23, both 103 |

which is the three-way partition the A/B colours need, and confirms the
"whole-model coverage reads low" risk is real but not absurd on a
single-map model.

### Stage 2 — jUCMNav fidelity: stubs — **done**

* `Stub` is out of `_UNSUPPORTED`. A token entering a stub is handed to the
  bound plug-in's start point; a plug-in end point is treated as a *return*
  and the path resumes on the stub's out-arc, so coverage spans every map.
* **Static** stub: exactly one plug-in, and several bound is reported
  (`multiple_plugins_enabled`) rather than silently picked.
* **Dynamic** stub: every binding whose precondition holds is entered, a
  binding without a precondition counting as true; none holding is reported
  (`no_plugin_selected`). Recorded as an assumption, not a validated match —
  the generators emit only static stubs, so there was nothing to check it
  against.
* Two ambiguities are reported rather than guessed, both impossible in a
  generated model: `ambiguous_plugin_return` (one end point bound by several
  stubs — the scheduler carries nodes rather than tokens, so which caller to
  return to cannot be recovered) and `ambiguous_plugin_entry`.

Measured on `ClaimsPaymentLog` mined with `decomposition="auto"` — 6 maps,
247 elements, 24 scenarios — by restoring the old `_UNSUPPORTED` tuple to
get the "before" numbers:

| | clean | responsibilities run by scenario 1 | problems |
|---|---|---|---|
| before | 0/24 | 1 | 24 x `unsupported_node`, 24 x `end_point_not_reached` |
| after | **24/24** | **16** | none |

Coverage on the decomposed model: 55.9% for one scenario, 98.8% for the
union of all 24, with a single scenario touching 5 of the 6 maps.

### Stage 3 — coverage and rendering — **done**

* `pm4py_ucm.algo.scenario_coverage`: `coverage(ucm, results)` and
  `compare(ucm, a, b)`, exported as `coverage` / `compare` / `Coverage` /
  `Comparison`. Coverage carries `.fraction`, `.uncovered`, per-key hit
  counts, and `.by_kind()` — a single percentage hides that a scenario can
  walk every responsibility while leaving most connections untouched.
  Comparison carries the three-way partition plus `.neither` and
  `.agreement` (Jaccard).
* `coverage_render()` / `comparison_render()` translate a coverage result
  into `{"colors": …, "tooltips": …}` keyed by `id(element)`. The keying
  boundary lives here: coverage works in model ids so it can leave the
  process, the renderer works in object identity because that is what
  graphviz names are built from.
* `classic.apply` takes `coverage_colors` / `coverage_tooltips` and
  **drops the heat-map when they are present** — the two compete for the
  same channel.
* `model_to_svg(..., coverage=…)` passes it through, on both the
  single-map and the stacked multi-map path.

**A correction to this note.** It claimed graphviz's `tooltip=` "emits an
SVG `<title>`, which browsers render as a native hover tooltip". That is
wrong: graphviz honours `tooltip` only when the element *also* carries a
`URL`, because the tooltip becomes the generated anchor's `xlink:title`.
An element without a link gets nothing.

What graphviz *does* always emit is a `<title>` holding its internal object
name — which embeds a memory address and is shown on hover on every diagram
the project renders. The hover text is therefore injected by rewriting
those titles, which is both how it arrives and a small improvement on what
was there: after a coverage render, no element exposes an address, and the
uncovered ones say "not covered" rather than nothing.

### Stage 4 — the Scenarios-view section — **done**

A **Simulation** section at the bottom of the Scenarios view, appearing
once scenarios have been synthesised:

* every scenario is run, with a run/clean/problems summary and a table of
  the problems jUCMNav would list;
* **Coverage** mode — multi-select scenarios, then percentage of the whole
  model, covered/total, never-walked count, and a by-element-kind
  breakdown;
* **Compare A vs B** mode — two selectors, the A-only / B-only / both
  counts, the agreement, and how many elements neither walked;
* the highlighted model rendered inline, every element hoverable;
* a notice when the performance overlay's heat-map is suspended. That
  notice earns its place: the heat-map defaults **on** whenever overlay
  metrics are picked, so without it the user would watch their heat-map
  vanish with no explanation.

Verified in the running app on `ClaimsPaymentLog`: 18 scenarios, all clean;
coverage mode 54.7% (128/234); A/B `v1_QuickAssessment` vs
`v2_AnalyzeClaim` giving 7 / 23 / 121 at 80% agreement with 83 walked by
neither; and inside the viewer, an SVG carrying all three colours and 254
hover titles.

### Stage 5 — persistence and export — **done**

* Four registry parameters — `simulation_mode`, `simulation_scenarios`,
  `simulation_a`, `simulation_b` — so a saved project resumes into the same
  simulation. The rule held: the mode is stored as an **identifier**, with the
  radio's words supplied by `format_func` from `_SIM_MODES`, the same shape
  `active_view` uses. The scenarios are stored by **name**; a re-mine that
  drops one degrades (A/B fall back to their defaults, Coverage filters the
  pick list) rather than highlighting the wrong scenario.
* All four are main-area widgets in a view that is usually not the active one
  when a project loads, so they take the Family route: `_sticky_get` on save,
  `_sticky_seed` on restore. The one deviation is the Coverage multiselect,
  which is clamped by hand instead of through `_sticky`'s `options=` — that
  treats an empty list as "restore the default", which would undo a
  deliberately cleared selection on the next rerun.
* `codegen` emits `run_simulation(ucm_s)`, chained onto `run_scenarios`, which
  replays the scenarios and writes `simulation.svg` plus a summary CSV
  (`simulation_coverage.csv` by kind, or `simulation_compare.csv` with the
  A-only / B-only / both / neither partition). The notebook gets the same as a
  cell, previewed inline.

Verified in the running app on `ClaimsPaymentLog`: Compare mode with
`v1_QuickAssessment` vs `v5_QuickAssessment` (40 / 30 / 90 at 56% agreement)
saved from the **Model** view — with the simulation widgets garbage collected
— wrote `"simulation_mode": "compare"` and both names; resuming that file into
a fresh session and re-synthesizing came back on the same pair with the same
numbers.

**The drift guards were parsing the wrong app.** `test_sessions_registry.py`
and one guard in `test_sessions_codegen.py` still read `streamlit_app_v5.py`,
which V6 superseded, so the gather/restore contract was being enforced against
a file nobody deploys. They now read V6; V5 keeps a gather of the registry
defaults for the new ids (and a test of its own), because `collect` refuses a
gather missing a registered id and V5's Save would otherwise raise.

## Risks

* **Stub traversal is the schedule risk.** Everything else is additive to
  code that already works; this one changes the traversal's core loop.
* **The colours changed once they were seen in use.** Red/blue read badly
  beside the heat-map's red ramp, so A/B became dark green and dark
  orange. Making both "a bit dark" nearly matched their lightness (luma
  95.5 vs 101.4), which would have merged them on a greyscale printout and
  under strong colour-vision deficiency, so the green was deepened to open
  a 44-point luma gap: the pair now separates by lightness as well as hue.
  Purple stays for the intersection and sits between the two in lightness,
  so if it ever proves hard to read, add a second channel (dash or
  thickness) rather than re-hueing.
* **Whole-model coverage will report low percentages** on decomposed models,
  because one scenario walks one path through a model that contains every
  path. That is the honest number and is the decision taken; it needs
  presenting so it does not read as a fault.
