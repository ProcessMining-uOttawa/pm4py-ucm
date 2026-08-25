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
| Colours | A = red, B = blue, both = purple |

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

### Stage 3 — coverage and rendering

* Coverage over a set of scenarios: elements and connections walked, as a
  fraction of the whole model.
* A coverage **colour source** in `classic.apply`, alongside the existing
  metric-driven one and mutually exclusive with it.
* Per-element `tooltip=` carrying the hover statistics.
* A/B: red / blue / purple, from the two scenarios' visited sets.

### Stage 4 — the Scenarios-view section

* Multi-select scenarios → highlight + coverage summary.
* A/B mode → two selectors, three colours, coverage for each and for the
  intersection.
* Heat-map / coverage mutual exclusion in the sidebar.

### Stage 5 — persistence and export

* Registry parameters for the selection and mode, so a saved project resumes
  into the same simulation. Mind the existing rule: persisted option values
  are **identifiers, not labels** — see `docs/sessions.md`.
* `codegen` emits the equivalent calls, so an exported script reproduces the
  simulation the app showed.

## Risks

* **Stub traversal is the schedule risk.** Everything else is additive to
  code that already works; this one changes the traversal's core loop.
* **Purple as the union colour** is the weakest part of the colour scheme
  under deuteranopia — red and blue separate well, but purple can read as
  either. Kept as specified; if it proves hard to read, add a second channel
  (dash pattern or thickness) rather than changing the hues.
* **Whole-model coverage will report low percentages** on decomposed models,
  because one scenario walks one path through a model that contains every
  path. That is the honest number and is the decision taken; it needs
  presenting so it does not read as a fault.
