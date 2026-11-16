# Handoff — after 0.9.3

**0.9.3 is released.** It adds the **split model** (one model per value of
a case attribute, under a root map with one stub per value, issue #152),
the **CSV start timestamp** (a CSV can be an interval log, #153), a
**Graphviz fallback** for maps `dot` cannot lay out, *Add all* in the
suggestion gallery (PR #151) and the SVG viewer's scroll/zoom change
(PR #150). It ships at patch level at the maintainer's request although it
adds a public function and changes two behaviours; the changelog section
opens with the upgrade notes.

Before it, 0.9.2 was a samples release (PR #148): the bundled
`devlog` sample now covers **two development periods** (410 cases, with a
`case:period` attribute), and two CSV samples are added, **Procure-to-Pay**
and **RefundProcess**. No library or app behaviour changes.

Before it, 0.9.1 was a correction release: a **documentation
audit** of every prose file against the code (PR #145), and the **fixes
for the code defects that audit found** (PR #146). The one with reach is
that **case duration now has a single definition**, earliest start to
latest completion, in the dashboards, the partition advisor, the family
statistics and the app's cycle-time filter. It ships at patch level at the
maintainer's request although it changes numbers users see; the changelog
section opens with the upgrade notes.

Before it, 0.9.0 made the Dashboards view start itself: **dashboards
suggested from the log**, an **Activity axis**, a per-activity **rework
rate**, the concurrency-aware **behavioural variants** as a segment axis,
and **value axes** on the charts (PRs #139–#141). It was a minor release
because it **dropped Python 3.9** and claimed 3.13 and 3.14. Design and
rules in [`docs/dashboard_suggestions.md`](docs/dashboard_suggestions.md).

Before it, 0.8.5 added the standard **BPMN 2.0 export**: design in
[`docs/bpmn_export.md`](docs/bpmn_export.md) (PR #133), all five stages of
its plan in one PR (#134), shipped at patch level at the maintainer's
request because nothing that existed changes behaviour. Before it, 0.8.4 was
a file-loading hardening patch (PRs #127, #130, #131: two upload crashes from
the deployed app, five more from auditing the loaders, and a guarded first
parse), and 0.8.0–0.8.3 delivered scenario simulation, coverage and A/B comparison
(stages 1–5 of [`docs/scenario_simulation.md`](docs/scenario_simulation.md)),
the validation gates, the variant-filtered log export, and the release
tooling described below.

## In progress: removing infrequent paths (unreleased)

On the branch `feature/prune-infrequent-paths`, in a pull request. Built
in one session (2026-11-16) from the maintainer's request and two rounds
of his decisions; the code has not been reviewed beyond that.

- `pm4py_ucm/algo/pruning.py` and `pm4py_ucm.prune_infrequent_paths`
  prune the **process tree** from the traversal counts; the conversion is
  untouched. Design, decisions and the not-built list are in
  [`docs/infrequent_paths.md`](docs/infrequent_paths.md); tests in
  `tests/test_pruning.py`.
- The V6 app has the sidebar's **Infrequent paths** controls. The pruned
  tree comes from `_model_tree`, which every single-tree consumer now
  calls instead of `_log_and_tree` (Model, parse table, Scenarios,
  behavioural variants). `_pruned_tree` is the cached step; it replays the
  *unpruned* tree through the shared `_parse_table(…, prune_spec=())`.
  `_model_tree` is deliberately uncached so the log is not stored once per
  slider position.
- **Keep or drop is an option** (the maintainer's decision, 2026-11-16):
  `drop_cases` in the library, the *Drop the cases on removed paths*
  checkbox in the app. `prune_spec` is `()` or `(min_cases, min_share,
  drop)`. With drop on, `_model_tree` returns the log without the dropped
  cases (their ids are the third element of `_pruned_tree`'s result), and
  `_fact_table` filters the dashboards' frame the same way.
- **Split models and families are pruned too** (same decision): the
  family miner takes `prune=`, each cell judged on its own cases, and
  `_mine_split` / `_mine_family` pass the sidebar's settings. With drop
  on, the cells and `family.log_df` lose the dropped cases; the app's
  download-only assemblies re-attach the log through
  `_attach_family_log`, which removes them again.
- The sidebar section is a disclosure checkbox (`cfg_prune_on`), like
  *Log filters*, at the maintainer's request: its three widgets render
  only while it is open, so they use the `_sticky` mirror, and a project
  restore writes both the widget keys and the mirror.
  Checked in the running app by saving and loading real project files:
  saved open, the file holds the thresholds; saved closed, it holds 0;
  loading the first into a closed section opens it with its values, and
  loading the second into an open section closes it.
- The unpruned tree's parse table is pre-built with progress **once per
  log + filter + noise threshold in a session** (`_prune_warm_fp`), not on
  every rerun: as a cache hit it still unpickles the whole table. Measured
  on the claims sample a rerun costs about 0.6 s with or without pruning,
  so this is a precaution for large logs.
- **The maintainer's slow refresh after changing the diagram height**
  (his journey log: 10,004 cases, 4,474 variants, split on an 11-value
  attribute, 2% and 5 cases) was measured with temporary timing markers.
  A height change costs about 0.4 s server-side while the mine is cached,
  eight times out of eight, with identical mining arguments. The split
  mine itself costs about 150 s with pruning and 92 s without. So a slow
  refresh there is a re-mine, and nothing in a height change triggers
  one; the likely trigger is the cache being invalidated by the code
  edits and server restarts of this session (Streamlit re-runs a cached
  function whose source changed). Not proven.
- **Pruning with repaired counts is not deterministic.** On that log the
  same settings removed 173 paths in one mine and 172 in the next,
  because the alignment repair is bounded by wall-clock time and the
  repaired counts feed the thresholds. Open: prune from fitting cases
  only, or give the repair a budget that does not depend on the clock.
  (After the aligner change below: 166 and 169. Fewer cells exhaust the
  budget, so it is rarer, not gone.)
- **Where a split's time goes, and the aligner change.** The maintainer
  asked why "Assembling the split model" was slow. Profiled on that log:
  mining the 11 trees takes 3 s; the rest was alignment repair, about
  64 s in the pruning pass and about 57 s again in the assembly's replay.
  The pruning decision itself costs nothing measurable. The Petri-net A*
  aligned 2 to 18 sequences per cell before its 10 s budget ran out (each
  hit the 1 s per-sequence cap). `_aligned_paths` now searches the tree
  with PM4Py's `search_graph_pt` (30 to 95 ms per sequence, all aligned
  in most cells), keeps the Petri search as `_aligned_paths_petri` for
  non-PM4Py trees, and bounds a single sequence through a deadline-aware
  stand-in for `heapq` in PM4Py's module (`_HeapqWithDeadline`), since
  that search has no time limit of its own. The pruning pass's alignments
  are reused in the next replay (`repair_hints`). Result on that log with
  pruning: about 146 s to about 110 s, uncounted cases about 1,520 to
  about 470. **This changes traversal numbers on any poorly fitting
  model**, pruning or not; the changelog says so. What is left is real
  search time: two cells still exhaust the 10 s budget. Going further
  means parallel alignment across processes, not tried here.
- **The budget scales with each model** in families and splits (the
  maintainer's choice over a fixed 15 s, 2026-11-17):
  `scaled_repair_budget`, one second per 50 distinct sequences, 10 s to
  120 s, the formula the app already used for a single model. Measured on
  that log with pruning, two runs each:

  | Budget per model | Total | Uncounted | Paths removed |
  |---|---|---|---|
  | 10 s fixed | about 110 s | about 470 | 166, 169 |
  | 15 s fixed | 118 s | 304, 321 | 162, 162 |
  | 30 s fixed | about 155 s | 21, 42 | 158, 158 |
  | scaled | about 134 s | 92, 95 | 165, 164 |

  The scaled one still leaves 4 of 22 replays short: cells of 400 to 800
  sequences stay at the 10 s floor and need about 15 s, because a
  non-fitting sequence costs 30 to 95 ms here, not the 20 ms the slope
  assumes. `REPAIR_SEQUENCES_PER_SECOND = 25` would cover them; not
  changed without the maintainer's say, since it lengthens every large
  mine.
- Three registry parameters (`prune_min_cases`, `prune_min_share`,
  `prune_drop_cases`), restored in `_apply_project_config`, emitted by the
  code export (`prune_tree`, `prune_arg`).
- Checked in the running app on the claims sample at 10%: two rare repeats
  go; kept, the fit note moves from 89% to 83% with 341 cases affected;
  dropped, the log card reads 5,259 cases and the fit is back to 89%;
  split by Channel, 6 paths go in 3 models; a Broker family loses 17 paths
  in 8 models and 386 cases, and Compare shows the same note. In drop
  mode the Scenarios view reads 88.6% fitness with 598 non-conforming
  cases, the dashboards compute over 5,259 cases, and the event-log export
  holds 5,259 cases.
- The Model view's event-log export leaves the dropped cases out
  (`mined["dropped_case_ids"]`). The Family view's coverage preview is
  computed before mining and only says that pruning will change it. The
  replay prompts name what the replay is for (`_replay_what`), so they no
  longer say "metrics" when only pruning asked for it; **those prompts
  were not seen in the app**, since they only appear on a log whose replay
  is slow.

## State on arrival

- `main` carries 0.9.3. **Check the tag and PyPI
  before assuming it is live**: `git tag --sort=-v:refname | head -1` and
  `pip index versions pm4py-ucm`. The upload waits at the `pypi`
  environment gate for the maintainer's approval, so `main` can read
  0.9.3 while PyPI still serves 0.9.2.
- 0.9.3 holds PRs #150, #151 and #154 (see "What 0.9.3 did").
- 0.9.2 holds the sample logs (PR #148) and a `.gitattributes` that pins
  `tests/fixtures/*.jucm` to LF.
- 0.9.1 holds the `docs/` index (PR #143), the documentation audit
  (PR #145) and the fixes for the code defects it reported (PR #146).
- All three version sites read **0.9.3** and are kept in lock-step by a test:
  `pyproject.toml`, `pm4py_ucm/__init__.py`, `web/sessions/codegen.py`
  (`GENERATOR_VERSION`).
- `CHANGELOG.md`'s `[Unreleased]` is empty.
- **The fixes were checked in the running app** on the claims sample
  before the release: average case duration 15.1 d, service time of
  *Register Claim* 3.5 h, a histogram and a box plot under a per-case
  target with their axes in days, a segmented per-case widget in `%`, the
  reworded Decomposition help, and the cycle-time filter (band 0–10 keeps
  560 of 5,600 cases). **Not checked there**: the load note for a project
  saved by a newer app, which needs such a file to be uploaded.
- **[`docs/README.md`](docs/README.md) indexes the documents** (PR #143):
  each by the question it answers, its kind (contract, design note,
  measurements, proposal), whether it is built, and the test that holds it
  to the code. A new document gets a status line under its title and a row
  there. `sessions.md` and `scenario_simulation.md` had kept "not yet
  implemented" headers since before they shipped; a design note's status
  line is prose, so nothing guards it — update it in the PR that ships the
  feature.
- Suite: **1612 passed, 0 failed** in the main checkout (~3 min; one
  test needs the untracked `demo/ClaimsPaymentLog.xes` and is skipped in a
  fresh worktree); CI green on Python 3.10–3.14. `requires-python` is
  `>=3.10`.
- **Checked in the running app before the release**, on a 55-service
  mixed log (77,551 events, 431 activities): the split by `service` (55
  stubs with their case counts, 176 maps at threshold 0.20); saving a
  split project and resuming it in a fresh session; the highlighted model
  in the Scenarios view at threshold 0, drawn through the Graphviz
  fallback; the Family view offering the 55-value attribute; the
  late-start warning; *Add all* in the gallery. **Not checked there**: the
  PNG and BPMN downloads of a split model.
- `demo/sessions_tutorial.ipynb` and `demo/model_families_tutorial.ipynb`
  were **re-executed** for 0.9.3 (the second gained section 6b, the split
  model). The other three notebooks were not re-run.
- **One deployment.** https://pm4py-ucm.streamlit.app/ serves V6 through the
  `streamlit_app.py` shim. The second deployment
  (`pm4py-ucm-scenarios`, which served the frozen V2) has been **deleted** —
  the SAM 2026 paper that required it was accepted and its final version
  points at the main app. That URL now 404s; do not link it.

## What 0.9.3 did (PRs #150, #151, #154)

- **The split model.** `discover_ucm_by_attribute(log, attribute)` =
  `discover_ucm_family` on that attribute (every value kept: no `Other`,
  `min_cases=1`, up to 200 values) + `assemble_split`
  (`pm4py_ucm/algo/discovery/families/assembly.py`,
  `assemble_ucm_family(mode="split")`). The root map is converted from a
  *synthetic* choice over the cells' own trees, each intercepted as a
  static stub, so it gets the ordinary provenance tags, simplification
  and routing. Its counts come from the partition, handed to the overlay
  pass as the traversal statistics of that synthetic choice; the
  event-based `frequency` / `percentage` are mapped to their traversal
  twins there, because on the root they are the same number.
- **Why it exists** (issue #152, measured on the mixed log): at threshold
  0 the single tree's root is a 28-way choice, not 55, because 72 step
  names are shared across services; per service the models have 3 to 31
  activities. Decomposition cuts along the mined tree and cannot recover
  the service boundary.
- **Decisions the maintainer made**: the split lives in the Model view,
  not the Family view; **no plug-in is shared across stubs** (precise
  overlays matter more than compactness), so there is no dedup even
  between identical values; the Scenarios view keeps working on the
  single whole-log model ("not now" to per-value scenarios); families
  accept an attribute with up to **60** values
  (`DEFAULT_MAX_ENUM_CARDINALITY`) while the app's *Max values per
  attribute* stays capped at 20.
- **App.** *Split by attribute* is rendered into a sidebar slot left
  under the decomposition controls and filled after the log filters are
  known (its choices are the filtered log's case attributes), through
  `_sticky`. `_mine_split` replaces `_mine` when it is set; the shared
  parse table and the replay estimate are skipped, since each case is
  replayed on its own value's model. Registry param `split_attribute`.
- **CSV start timestamp.** `csv_columns` gained an optional **seventh**
  element (the start column; the sixth, day-first, is then always
  present). Build the tuple with `_csv_mapping_tuple`, read the start
  with `_csv_start_col`. `_format_csv_df` parses the start before
  `format_dataframe` (so it travels with its rows), puts it on the
  completions' clock afterwards, and **drops an unmapped
  `start_timestamp` column**: the mapping is authoritative. The exported
  script's `read_log` mirrors all of it.
- **The overlay default follows the mapping.** A CSV becomes an interval
  log only when its mapping is applied, after the overlay defaults were
  seeded; `overlay_seeded_interval::<hash>` records what they were seeded
  under and swaps `sojourn_median_time` / `median_time` only while the
  selection is still the untouched default.
- **Graphviz fallback** (`pm4py_ucm/visualization/ucm/dot.py`). `dot`
  fails with `Error: trouble in init_rank` on a large map whose path
  keeps re-entering the same component clusters (8.0.3 and 15.1.0 alike;
  the graph is valid). It still writes a complete-looking picture, which
  is garbage: overlapping, empty component boxes. `pipe` / `render` retry
  with the component clusters as plain subgraphs and a note in the
  picture. Every pipe and render in the package and the app goes through
  them.
- **V2, V3, V5 are untested and unmaintained** (see "Web app versions").

Traps this release taught:

- **Editing `streamlit_app_v6.py` under a running app breaks it** with a
  `TokenError` from `inspect.getsource` inside `st.cache_data`: the server
  runs the bytecode it compiled, with the old line numbers, and reads the
  function source from the file on disk. Restart the app; it is not a bug.
- **A non-zero exit from `dot` does not mean no output.** The SVG was
  complete and wrong. Do not salvage stdout from a failed layout.
- **The warning inside *Log source & columns* is invisible once the
  mapping is applied**, because the expander collapses. Anything the user
  must keep seeing belongs in the main area too; the late-start count is
  repeated in the "Read from" caption for that reason.
- **The cost screen still judges the whole log when a split is on**: 431
  activities trips it although no value has more than 31. Unfixed.
- **`@@case_index` and a numeric id column are offered as family
  attributes** (binned into ranges). Pre-existing, visible on any CSV.
- **A stash pop after a fast-forward leaves the files staged** and the
  stash in place. `git restore --staged .`, then drop the stash by hand.
- **Driving the app with a hidden window**: `find` + a ref click still
  works for selectboxes; a slider needs synthetic `ArrowLeft` keydowns on
  its thumb; a download can be captured by patching
  `HTMLAnchorElement.prototype.click` and fetching the `/media/` URL; a
  file upload by assigning a `DataTransfer` to the uploader's input.

## What 0.9.2 did: sample logs (PR #148)

- **`web/samples/devlog.csv` is now the two-period log** from
  pm4py-ucm-devmining (`results_period2/devlog_all.csv`): 410 cases,
  12,053 events, `case:period` 1 or 2. The Devlog rows in
  [`docs/miner_performance.md`](docs/miner_performance.md) were measured on
  the earlier 285-case file and were **not re-run**; a note there says so.
- **A CSV sample must auto-detect its columns.** *Load sample* seeds the
  mapping from `_CSV_AUTOPICK`, by exact header name, then by **position**
  (case, activity, timestamp = columns 0, 1, 2). RefundProcess's original
  headers (`Order Nr., Status, Employee, Complete Timestamp`) matched
  nothing, so the timestamp landed on the employee column. The bundled copy
  has standard headers and ISO dates. `tests/test_bundled_csv_samples.py`
  pins the mapping of each CSV sample and fails if a new one is not
  registered there.
- **Byte-compared fixtures need LF.** A fresh Windows worktree checked
  `tests/fixtures/*.jucm` out as CRLF and two `RegressionGuardTests`
  failed; the long-lived main checkout never showed it. `.gitattributes`
  now pins them. A new byte-compared fixture of another type needs a line
  there too.
- **Not checked:** the tutorial notebooks' stored outputs against the new
  devlog sample, and the new samples in the running app.

## What 0.9.1 did: the documentation audit (PR #145)

Every prose file was checked against the code, corrected, and filled in:
the root README, `web/`, `demo/` and `tests/` READMEs, all of `docs/`, the
docstrings the audit found stale, and a new
[`CONTRIBUTING.md`](CONTRIBUTING.md). What it taught:

- **Design notes had kept describing the design, not the code.**
  `sessions.md` documented a `Param` with `get`/`apply` callables, a
  `PERSIST_IGNORE` list and a `pm4py_ucm/algo/sessions/` package, none of
  which were ever built; `code_export.md` claimed byte-identity with the
  GUI. Each built feature's document now describes it as built, and opens
  with a status line. Section numbers were kept, because tests, code
  comments and other documents cite them.
- **The README stated defaults the library does not have.** The inductive
  miner's `noise_threshold` is PM4Py's `0.0` (the *app* starts at `0.2`),
  and performer mining is **on** by default. Several of the README's
  numbers (18 scenarios, the coverage figures) only reproduce on a tree
  mined at `0.2`; the snippets now say so.
- **PM4Py is distributed under AGPL-3.0**, not GPL; the README no longer
  says the licences match. The maintainer should confirm the wording.
- The sample log has **78,126 events**, not "~90 K".
- `CONTRIBUTING.md` is now in the link guard
  (`tests/test_release_consistency.py`).

## What 0.9.1 did: the audit's code defects, fixed (PR #146)

The audit listed defects it deliberately left alone. PR #146 fixed them,
each with a test that failed first. What is worth knowing:

- **One definition of case duration: earliest start to latest
  completion.** The maintainer settled the audit's open question
  ("Dashboard should also use earliest start to latest completion", then
  the same for the partition advisor). It lives in
  `pm4py_ucm/util/timing.py`: `case_durations` serves the family
  statistics and the advisor; `build_fact_table` applies the same rule to
  its integer seconds, because the case start is also the origin of the
  delta encoding. `TestOneCaseDuration` holds the three together, on a
  hand-computed log and on the claims sample (15.07 d from all three; the
  dashboards and the advisor used to give 14.92 d).
- **`actService` was the same defect.** With the case start at the first
  *completion*, a first event's start lay "before the case" and was
  marked missing. Moving the case start to the earliest start fixes both;
  the engines did not change, since `duration` is the case's last offset
  and everything else is a difference of offsets. The time axes, the date
  filter and the series metrics read the case start, so on an interval
  log they now place a case by its earliest start.
- **A broken start counts from its completion** (missing, unparseable, or
  after its own completion) in all three callers. The family statistics
  used to take `min(start)` as it came; on a clean log nothing changes.
- **The app's cycle-time filter follows too** (the maintainer's "yes,
  align it"). `_apply_log_filters` in `streamlit_app_v6.py` and its copy
  in `web/sessions/codegen.py` call `case_durations`; the two must stay in
  step, and `test_cycle_time_filter_measures_…` runs both. The sort is
  now stable, so tied cases keep their order in the log. The ƒ predicate's
  `duration()` already agreed: it goes through the dashboards' fact table.
- **`per_case` is one decision, taken once.** `_share_mode(target)` /
  `shareMode(target)` (target on *and* mode `per_case`) is asked in
  `compute_widget`, which sets the widget's `unit` to `%` for every
  shape. Before, only `_kpi` switched, and it did so even with the target
  off. A segmented widget's own `value` is now the overall share, which
  is what its scorecard row compares with the share goal.
- **`discover_scenarios` consumes `discovery_parameters` itself**, because
  it pins the tree before calling `discover_ucm_inductive`; left in
  `params` they reach that function beside a `process_tree` and are
  reported as ignored.
- **A distribution has its own unit.** A `hist` or `box` result carries
  `valueUnit` beside `unit`, because under a per-case target the headline
  is a share while the bins are still the metric's values; `dash-ui.js`
  reads it through `distUnit(w)`. The UI has no DOM test: the engines'
  field is checked through parity, the UI's use of it only by a source
  assertion.
- **A custom case-id column is only partly supported in families.**
  Mining, statistics and the combined assembly's frequency/time overlay
  now follow `ModelFamily.case_id_col`. The default tree miner, the
  replay-based traversal counts and the umbrella's path scenarios still
  assume `case:concept:name`. Nobody has asked for more; rename the
  column first.
- **`TestJsParity` did not catch either dashboard defect**, because both
  engines were wrong in the same way and its fixture is not an interval
  log. `TestJsParityOnAnIntervalLog` now covers service and waiting time.
  Parity proves agreement, not correctness: a metric also needs a
  hand-computed expectation.
- **A fresh worktree on Windows fails two tests that are not broken.**
  With `core.autocrlf=true`, `tests/fixtures/regression_no_decomp.jucm`
  is checked out with CRLF and `RegressionGuardTests` compares bytes.
  Strip the carriage returns in the working copy (the index is LF, so
  nothing is committed) or the suite reports 2 failures.

## What 0.9.0 added

All in `pm4py_ucm/algo/dashboards/` and the app's Dashboards view; mining,
scenarios, families and the exporters are untouched.

- **`suggest.py` — `suggest_dashboards(table)`.** Profiles the fact table
  and returns whole dashboards (Overview, Bottlenecks, Rework and flow,
  Segments, Behavioural variants), each widget with a `why`. Deterministic,
  no LLM. Each dashboard opens with KPIs that *name* what matters and
  follows with a bar or heatmap of *everything*. A suggestion is a list of
  ordinary widget specs, computed in Python and shipped in the page config
  (`dashboard_html(suggestions=…)`): the island only shows it, so there is
  no JS twin and nothing to keep in parity.
- **App.** A log's first dashboard is seeded from the Overview
  (`_dashboard_suggestions`, cached on `_fact_table`'s own arguments); the
  green **✦ Suggest** button opens the gallery; *Add* creates a new named
  dashboard or fills an empty one, and never replaces a built one.
- **Activity axis** (`ACTIVITY_AXIS = "activity"`, both engines). Varies a
  per-activity metric's own `activity` parameter instead of partitioning
  cases; returns the same shape as any segmented widget. Not in
  `segment_axes` (the filter picker enumerates that); the composer offers
  it, disabled for metrics that are not per activity.
- **`actRework`** — share of the cases *performing* an activity that
  perform it more than once. `docs/metrics.md` §2 sets it against
  `actRepeats` and the overlay's `repeat_frequency`.
- **Behavioural variants.** `build_fact_table(behaviour=clustering)` adds
  the case attribute `behavioural variant` (`BEHAVIOUR_ATTRIBUTE`), so it
  segments and filters with **no engine code of its own**. Ids are the
  clustering's (`v3` here is `v3` in the scenario reports). The app supplies
  it from the shared replay table (`_behaviour_clustering`), only when the
  replay was not skipped; the fact-table cache is keyed on the noise
  threshold for that reason. An exported script rebuilds it when a
  dashboard names the axis.
- **Value axes** (`axisTicks` in `dash-ui.js`, rendering only) on bars,
  lines and histograms; bars are now proportional from zero.
- **Tests**: `tests/test_dashboard_suggestions.py` (every rule, plus "every
  suggestion computes, binds and satisfies the viz guards" in both
  engines); Activity-axis, `actRework`, behavioural-variant and value-axis
  classes in `tests/test_dashboards.py`, with parity specs.
- **Python**: 3.9 dropped (`ubuntu-latest` moved to 26.04, which has no 3.9
  build); 3.13 and 3.14 claimed.

Decisions the maintainer made or accepted, so they are not re-litigated:
`actRework`'s denominator is the cases performing the activity; suggested
targets are prefilled at today's value and switched **off**; suggestions
hold up to 10 widgets; tier 2 goes through an **external** LLM and an
exported brief, not an in-tool provider.

Traps this release taught:

- **The composer's formula validator rewrites `spec.params` for every
  metric.** Reading an edited widget's params after it restored nothing, so
  every parameterised widget reopened on the log's default activities and
  saved them back. Found only by opening a suggested widget in the editor.
  The same pass restored only `hist`/`box` as a chart shape. Both fixed;
  both would have been invisible to the suite, which has no DOM harness.
- **Directly-follows cannot tell concurrency from choice.** `a ∥ b` shows
  `a → b` and `b → a`, and whatever precedes them appears to choose. The
  suggestion rule separates the two by *cases*: a loop's two directions
  share their cases, concurrency's barely overlap (`_interleaved`). It is
  still a heuristic; the mined model knows its forks exactly.
- **An axis chosen from the log's shape can be empty for one widget.** The
  strongest attribute gave "sojourn of Check by channel" one bar, because
  only one channel performs Check. Every suggested chart is computed once
  and kept only if it draws (`_draws`).
- **A count displays as a whole number**, so an average of 0.3 repeats
  reads "0". That is why `actRework` exists and why case counts use the ƒ
  formula `1` summed.
- **A case-constant `org:resource` is detected as a case attribute too**,
  duplicating the `resource` axis. The proposer skips it.
- **Bars were scaled to `max(1, largest value)`**, so any bar chart of
  sub-day durations was flat. Nobody had noticed without an axis to read.
- **The app caches the dashboard HTML** (`st.cache_data`), and the bundle
  is read inside that cached function: after editing `dash-ui.js` or the
  CSS, restart the server, or the page keeps the old assets.
- **A hidden app window stops the browser pane from drawing**: screenshots
  and coordinate clicks time out. DOM reads and clicks through
  `javascript_tool` still work, including inside the island's iframe.
- **Inline Python in a Bash heredoc loses a level of backslashes** — a
  `\\n` written into a test file arrived as a real newline and broke the
  file. Write patch scripts with the Write tool and run them.
- **`ubuntu-latest` is not a fixed image.** It moved to 26.04 between two
  PRs and took Python 3.9 with it. To trial a new Python, use the
  non-blocking `experimental` job described in a comment in `tests.yml`.

## What 0.8.5 added

The BPMN 2.0 export, library and app. The library API gained `write_bpmn`,
`convert_to_bpmn_xml` and `write_bpmn_family`; nothing else changed.

- **`pm4py_ucm/objects/ucm/exporter/variants/bpmn.py`** is its own XML
  writer (pm4py's BPMN object has no lanes, call activities or extension
  elements). One pool; **components are lanes**, nested through
  `childLaneSet`, tiling the pool from the swim-lane layout; unbound nodes
  join the nearest lane. Node centres transfer 1:1; routing points become
  waypoints; `routing="orthogonal"` elbows them.
- **Stubs**: default `stubs="call"` (static stub → `callActivity` to the
  plug-in's own process, one process and diagram per map; dynamic stub →
  collapsed sub-process with **inclusive** gateways over the plug-ins,
  binding preconditions as guards). `stubs="inline"` nests each plug-in as
  a collapsed sub-process with its own plane; only root maps become
  processes; a plug-in bound twice is written twice with distinct ids.
- **Metrics** as `<ucm:metric>` extension elements (`urn:pm4py-ucm:bpmn:1.0`),
  overlay text as flow labels, heat-map as BPMN-in-Color + `bioc`
  attributes reusing the classic renderer's ramps; `sizes="canonical"`
  scales the layout uniformly to standard shape sizes; `data="objects"`
  for scenario variables and definitions.
- **App**: Model view *Prepare BPMN… / Download BPMN*, Family view *BPMN 2.0
  (.zip)*, both through `st.cache_data` wrappers that re-read the cached
  `.jucm`, silence the re-layout warning, and pass `_APP_BPMN_OPTIONS =
  {"stubs": "inline"}` (pinned by `AppDownloadTests`).
- **Tests**: `tests/test_bpmn_export.py` (60), every export validated
  against the OMG XSD vendored under `tests/data/bpmn-xsd` (`lxml` dev
  dependency; skipped without it); `tests/README.md` has a "BPMN 2.0 export"
  area. Docs: README section, web/demo READMEs, tutorial §7.1,
  `demo/mine_and_export.py --bpmn`.

Traps this release taught:

- **bpmn.io cannot follow a call activity**, and its demo modeler has no
  diagram switcher: with the library default every composite activity was a
  dead end there. Verifying "in bpmn-js" through the API (`viewer.open(id)`)
  hid that; check what a user can click. Hence the app's inline mode.
- **pm4py's `read_bpmn` crashes on `callActivity`** (`'str' object has no
  attribute 'add_in_arc'`). Round-trip tests use stub-free or inline models.
- **Graphviz clusters are not lane-compatible.** The graphviz layouter puts
  sibling components side by side; lanes must stack. The exporter detects
  that and re-lays out with the swim-lane layouter (`UserWarning`), which
  every app export triggers, so the app silences it and its help text says
  the picture may differ.
- **Graphviz 15.1 on Windows spends ~0.85 s per `dot` call enumerating fonts
  through Pango** before the first lookup; 8.0.3 takes 0.06 s. With one call
  per map for layout and one for the picture, the local app felt many times
  slower than Streamlit Cloud (Debian Graphviz 2.42). The maintainer's
  machine now lists `Graphviz8\bin` before `Graphviz\bin` in the machine
  PATH. If a local run is mysteriously slow, time `dot -Tdot` on a two-node
  graph first.

## What 0.8.4 added

All of it is in the web app's upload path; the library is untouched.

- **The CSV import owns its timestamp.** `pm4py.format_dataframe` converts
  text columns inside a silent `try/except: pass` and never converts numeric
  ones, so a timestamp it could not read reached the miner as strings and
  died deep in pm4py ("the dataframe should (at least) contain a column of
  type date") — redacted on Streamlit Cloud. `_format_csv_df` now parses the
  mapped column itself (ISO-8601 strictly, then `mixed`, then integer epochs)
  and names the column in a `ValueError` when nothing reads. The mapping panel
  runs that formatter on the first 5000 rows on **Apply** and refuses an
  unminable mapping with the reason shown.
- **Every XES read goes through `_plain_xes_bytes`**, which unwraps zip and
  gzip by magic bytes. The temp file pm4py reads is always called `log.xes`,
  and pm4py picks its reader by extension, so a `.xes.gz` used to go to the
  XML parser (`NoTopLevelLog`). Finder's `__MACOSX/._x` twins are skipped.
- **Every CSV read goes through `_read_csv_bytes`**, which sniffs encoding
  (BOM, UTF-8, UTF-16, cp1252) and delimiter (`,` `;` tab `|`). A `.csv.gz`
  is inflated in `_accept_log_bytes` and judged by content.
- **Day-first dates** are a switch in the mapping panel, carried as an
  optional **sixth element of `csv_columns`** (only present when on, so
  five-element projects compare equal). Unpack sites use `csv_columns[:5]`.
  The exported script honours it.
- **The cost screen's `_screen_log` call is guarded.** It is the first full
  parse of any upload; a reader failure is now a *Could not read …* message
  with a hint per file kind, then `st.stop()`.

Traps this release taught:

- **dateutil applies `dayfirst` to ISO dates too**: `2024-06-03` becomes 6
  March. Parse ISO8601 strictly first and only hand the remainder to `mixed`
  (`_parse_stamps`). The ambiguity detector compares the two readings on a
  sample and must never flag ISO logs.
- **pm4py's `check_pandas_dataframe_columns` has two different messages** for
  a bad timestamp. "the timestamp column should be of type datetime" means
  some *other* column parsed as a date; "should (at least) contain a column of
  type date" means none did. The second is the one a lone unparsed timestamp
  produces.
- **Code emitted into the exported script is a template**: `"\n"` and `b"\xef"`
  escapes inside `web/sessions/codegen.py`'s reader body become literal
  characters in the generated file and break it. Use `chr(10)` /
  `codecs.BOM_UTF8`; `tests/test_sessions_codegen.py` compiles the output.
- **The Cloud's pm4py could not be pinned down** from the traceback (its
  `pandas_utils.py` differs by one line from every 2.7.18–2.7.23.8 release
  tried); the fix covers the failure mode regardless of cause. "Manage app"
  logs would name the build if it ever matters.

## What 0.8.1 added

Structural validation is wired into **every** generation path — conversion,
discovery, synthesis, family mining and family assembly — not just the
exporters, so a malformed model can no longer be observed downstream. Each
takes `validate=True` by default and raises otherwise; see the upgrade note in
the changelog.

`filter_log_by_variants` cuts a log down to chosen behavioural variants, which
is how a noisy log is cleaned without a threshold: a case that did not replay
belongs to no variant, so selecting every variant already drops it. Exposed in
the app's Simulation section and in exported scripts.

Two fixes came from using the app rather than from tests. Coverage keys were
unstable across copies of a model, because element ids are allocated lazily —
and `compare()` silently returned a plausible wrong answer where `coverage()`
refused. And nine widgets both took a default and had their key restored, which
warned on every project load and hid a latent crash on a stale option value.

## What 0.8.0 delivered

The simulator runs a UCM's scenarios the way jUCMNav does and reports the same
problem kinds; coverage and A/B comparison turn the results into numbers and
colours; the web app's Scenarios view has a **Simulation** section with both
highlight modes; the selection is saved with a project and reproduced by an
exported script (`run_simulation()`).

Landed alongside: a synthesis bug that produced structurally invalid models
(#108), `validate_ucm()` / `check_ucm()` with the `.jucm` exporters gating on
them, the A/B colours, and the Elements/Paths coverage split (#109).

## Web app versions

**Everything older than V6 is deprecated, untested and unmaintained** — V5,
V3 and V2 alike (maintainer's decision, 2026-11-16). The files stay in the
repository and each renders a notice on start-up, but no test parses them
and nothing has to be kept working in them: a new registry `Param` is
gathered in V6 only, which breaks V5's Save button, and that is accepted.
New work goes in V6.

The frozen-V2 rule is **over**. Nothing needs to stay byte-stable for it any
more. (`.jucm` byte-stability for jUCMNav interop is a separate, still-live
goal — do not confuse the two.)

## Releasing

Use the **`release` skill** (`.claude/skills/release/SKILL.md`) — it performs
the cut and stops at the two gates that need a human, the `pypi` approval and
anything a check reports.

Start with `python tools/release_check.py <version>`: version sites, changelog
state, branch and tree, whether the tag is free **by version order** (a lexical
sort puts `v0.7.10` before `v0.7.5`), whether `publish.yml` is present at
HEAD — without it the release-triggered run is created *silently not at all* —
and the Bandit scan, which fails the gate on any medium/high finding (the scan
had not run between 2026-07-14 and 0.8.5 because nothing made it run).

The cross-cutting claims are guarded by `tests/test_release_consistency.py`
rather than by a checklist: a superseded app described as deployed, a retired
deployment left linked, a broken local link, an app the shim does not run. That
split is deliberate — an audit of this repo found that **everything with an
executable guard held, and everything that was prose drifted**. What still
needs judgement (HANDOFF, the notebooks, the screenshots the maintainer
captures by hand) is listed in the skill.

## Traps, and what they cost

Carried forward because each one bit during development:

- **Persisted option values are identifiers, not labels.** `_VIEWS` entries and
  `simulation_mode` store `"Model"` / `"coverage"`; the icons and words come
  from each widget's `format_func`. A label in a saved file breaks the day the
  wording changes. This rule has now been learned three times.
- **Selections are recorded by name, not rank or index.** A rank is relative to
  a population and any other setting moves that population.
- **A cache key must be determined by what is hashed.** A ``_``-prefixed
  parameter is excluded from the key; if the hashed ones do not determine it,
  the cache serves a result computed for different inputs, silently. Run
  `python tools/cache_audit.py web/streamlit_app_v6.py` — the suite does.
- **A cached function may not touch an element created outside it.** Streamlit
  records every element an `@st.cache_data` function writes so it can replay
  them on a hit; a child element created on an *external* block replays into a
  block that no longer exists and raises `CacheReplayClosureError`. The first
  call always works, so this only shows up on the second. `status.update(
  label=…)` mutates the status itself and is safe; `status.progress(…)` is not.
  Documented in `_ProgressUI`, broken anyway by the 0.8.0 family-grid progress,
  and now enforced by a test.
- **Streamlit owns widget state and may discard it on a rerun that changes
  nothing**, and drops the state of any keyed widget not rendered this run. A
  main-area widget that must survive leaving its view needs the `_sticky`
  mirror; anything that must survive at all belongs in a plain session entry
  the app owns.
- **graphviz ignores `tooltip=` unless the element also has a `URL`.** Hover
  text is injected by rewriting the `<title>` graphviz always emits.
- **Verify in the app, not only in tests.** After changing the A/B colours the
  suite was green while the selectors still read "A (red)" / "B (blue)".
- **Check a regression test actually fails without the fix.** Two tests were
  tautological on first draft — one ended in `or True`, another compared a set
  with itself.
- **Point drift guards at the app people actually run.** The "every registry
  parameter is saved and restored" tests parsed V5 long after V6 replaced it.
- **Merging two PRs that both edit `CHANGELOG.md` under `[Unreleased]`
  conflicts**, however disjoint the code is.
- Docs-only merges can skip CI with `[skip ci]` in the **merge commit** subject
  (`gh pr merge --subject`). It skips every workflow for that push, including
  the pdoc deploy.
- **This repository does not allow GitHub auto-merge** ("Auto merge is not
  allowed for this repository"). To merge once CI is green, chain it:
  `gh pr checks <n> --watch --fail-fast && gh pr merge <n> --merge
  --delete-branch`, run in the background.

## Testing the web app

Read `docs/miner_performance.md` for the measurement harness, and note the
mechanics that cost hours:

- `form_input` does not drive Streamlit selectboxes; click the combobox open,
  then click the `[role="option"]`. Synthetic `.click()` on a checkbox input is
  also ignored — use the element ref.
- **Never read the DOM mid-render.** A partial page looked exactly like a
  missing sidebar, and later showed a *previous* render's numbers.
- **Log to a file and read the server log.** A rerun clears the UI, so a
  Streamlit exception that aborts a rerun is invisible to UI-based
  instrumentation.
- The browser pane starts narrow, and Streamlit auto-collapses the sidebar
  below ~768px — the view radio then does not exist. Resize to ≥1280 wide.
- To read the rendered model, the SVG is inside the viewer iframe:
  `iframe.contentDocument`, not its `srcdoc`.

A synthetic log with ~2,700 variants over 80 activities trips both screening
thresholds and cycles in seconds. **Shape matters**: to reproduce the
variant-cap bug the rare activities had to carry the sequence distinctions.

## Confidential logs

Several logs used in the measurements are confidential and must not enter the
repo. The sample picker reads `web/samples/` with no path override, so: add
`web/samples/_tmp_*` to `.git/info/exclude` **before** copying anything in —
that file is local and untracked — then delete both the file and the rule
afterwards and confirm with `git status`. The IRCC log lives at
`C:\Users\jucmn\Downloads\ircc_uOttawa.csv` (274 activities, 7,734 cases, 543k
events; ~10 min to mine after reduction).

## Open items, by value

A. **The documentation audit's code findings are closed.** The defects
   are fixed (PR #146, see "the audit's code defects, fixed"); what the fixes
   turned up was put to the maintainer, who decided (2026-11-13):
   - **A project from a newer schema is re-saved with this app's
     `schema_version`** (`ProjectDoc.to_dict` always writes the current
     one). *Accepted as it is*: very few projects have been saved. Revisit
     only if the schema ever reaches version 2.
   - **A custom case-id column in families is partly supported**: mining,
     statistics and the combined assembly's overlay follow
     `ModelFamily.case_id_col`; the default tree miner, the traversal
     counts and the path scenarios assume `case:concept:name`. *Left like
     this by decision*; rename the column first.
   - The comments in `dash-ui.js` and `dash-report.js` that named
     `components.html` now say "the embedding iframe".
   - The histogram / box plot axis under a per-case target is fixed
     (`valueUnit`).

B. **One decision is the maintainer's.** `docs/ai_insights.md` §3 specifies
   an *in-tool* LLM provider seam; the stance settled for dashboards is an
   *external* LLM fed an exported brief (`dashboard_suggestions.md` §3).
   The two documents now flag the conflict instead of hiding it.

00. **Split model, next steps** (none built):
   - **Scenarios per value.** The Scenarios view and simulation assume one
     tree; on a mixed log they run on the unreadable whole-log model. The
     maintainer said "not now".
   - **The cost screen and the replay estimate ignore the split.**
   - **The decomposition cap is not hard.** `_enforce_cap` only cuts
     subtrees at or above `min_leaves_to_decompose`, and a wide operator
     with only leaf children has nothing to cut: on the mixed log "auto"
     (cap 31) leaves a root with 141 responsibilities. Relaxing the floor
     changes existing outputs; grouping a wide choice's children would
     split one tree choice across two forks, which traversal counts and
     scenario synthesis both key on. Left for the maintainer to decide.
   - **A mixture warning**: nothing tells the user a log looks like a
     mixture and that splitting would help.

0. **Dashboards, next steps** (all designed in
   `docs/dashboard_suggestions.md`, none built):
   - **Tier 2 — definitions from an external LLM.** Export a brief
     (`describe_log`), and make **⬆ Load** validate and repair what comes
     back. The validating loader is useful on its own and comes first.
   - **Tier 3 — dashboards from a GRL goal model.** Blocked on the GRL
     metamodel (`goal_insights.md` P0) and on binding an indicator to a
     metric.
   - **Anchor decision points to the mined model's OR-forks** instead of
     the directly-follows heuristic.
   - **Activity-axis cost on large logs is unmeasured.** The metric runs
     once per activity in the browser; IRCC (274 activities, 543k events)
     is the case to time.
   - **A duration of exactly one hour can read "1.0 h" in Python and
     "60.0 m" in the browser**: the engines sum floats in different orders
     and the unit boundary sits on the last bit. Cosmetic, pre-existing.

1. **The five README screenshots show a UI that no longer exists.**
   `web/PM4Py-UCM-*.png` were captured at **v0.5.2**: the tabbed layout, a
   "Decomposition (Model tab only)" heading, no left rail, no cost screen, no
   Simulation. They are three releases and a redesign out of date, and there is
   no shot of the Scenarios view's Simulation section at all. 0.9.0 made
   the Dashboards shot staler still: value axes on every chart, the green
   Suggest button, heatmaps.

   The maintainer captures these by hand, and that is the arrangement — but
   note **an agent cannot do it**: the browser pane caps a screenshot at
   ~800×525 (region crop is unsupported, and the emulated viewport is
   downscaled), against 2525×1277 for the current files, and it cannot write
   the captured image to disk at all. Driving the app into the right state for
   each shot is the part an agent *can* do.

2. **Stale `.jucm` artifacts.** Any `.jucm` exported before #108 is likely
   malformed; `family_umbrella.jucm` carried the defect across 14 maps.
   Anything archived is worth regenerating.
3. **The default noise threshold of 0.20.** A clinical log fits zero cases at
   it. Still unresolved, and still the oldest open question here.
4. **`demo/model_families_tutorial.ipynb` writes ~2 MB of embedded SVG.** Fine,
   but the notebook grew from 909 KB when it moved to vector output.

Struck from this list at 0.8.0: V5's "Notation (Model tab)" label. V5 is
deprecated now, so cosmetic work on it would contradict the no-new-work rule.
