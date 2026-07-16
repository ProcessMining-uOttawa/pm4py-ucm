# Changelog

All notable changes to **pm4py-ucm** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.0] — 2026-07-16

The dashboards release: user-defined interactive dashboards over a log,
first-class SVG model rendering with navigable stubs, and the redesigned
**V4** workspace app — now the deployed default.

### Added — Dashboards

- **User-defined dashboards** (`pm4py_ucm.algo.dashboards`): a per-case
  **fact table** (`build_fact_table`), a **metric catalog**, and
  `compute_widget` — KPIs, one-axis bars, and two-axis tables — with
  dashboard- and widget-level **filters**, **segmentation** axes,
  **targets**, and a **scorecard**. The compute engine exists twice
  (Python here, JS in `assets/dash-engine.js`) and is held byte-for-byte
  in step by a parity test.
- **The ƒ custom-formula language** (`compile_formula`) — a tiny, closed,
  no-`eval` per-case expression grammar (`duration()`, `contains(act)`,
  `count(act)`, `time_between(a, b)`, `timestamp(act)`, `attr(name)`,
  arithmetic / comparison / `and`/`or`/`not`, optional `where` clause)
  for metrics the catalog does not name.
- **Self-contained interactive HTML export** (`dashboard_html` /
  `write_dashboard`) — the same artifact the app's Dashboards view
  renders, so app and export cannot drift. Includes a multi-section
  **session report** (scorecard + dashboards + the process model as SVG +
  a **Family** section embedding the family statistics report), a reader
  filter bar that recomputes everything, and a "Pin to dashboard" path
  from the Model view.
- **Dashboards tutorial** (`demo/dashboards_tutorial.ipynb`) and the
  semantic contract [`docs/dashboards.md`](docs/dashboards.md).

### Added — SVG rendering

- **First-class SVG export**: `save_vis_ucm(ucm, "x.svg")` and
  `save_vis_ucm_family(family, "grid.svg")` now render vector SVG
  (single-map or the full stacked / 2-D-grid composite), not just PNG.
- **Navigable stub hyperlinks**: a stub / decomposed sub-process links to
  its plug-in map — a single-plug-in stub jumps straight there, a dynamic
  (multi-binding) stub opens a **picker** listing each plug-in with its
  precondition. Panel/menu ids are namespaced per member, so a link never
  jumps across a family.
- SVG is the default on-screen render in the V4 app (Model, Compare,
  Family); the family **grid** and the family **HTML report** cell models
  are SVG (crisp, selectable, smaller).

### Added — Web app (V4) and deployment

- **`streamlit_app_v4.py`** — a left-rail workspace shell over the full
  V3 capability, theme-aware (light/dark), plus the new **Dashboards**
  view. **`web/streamlit_app.py` (the deployment's main-file shim) now
  runs V4**, so https://pm4py-ucm.streamlit.app/ serves it. V3 and V1
  remain in git history; the frozen **V2** scenarios app is untouched.
- Pinned `streamlit>=1.32,<2` so a Cloud rebuild cannot pull a release
  that removes the (deprecated) `st.components.v1.html` the islands use
  before the `st.iframe` migration lands (tracked in #19).

### Added — tutorials

- The discovery tutorial gains an **SVG** section (render, navigate,
  export); the families tutorial gains **pairwise comparison** and the
  interactive HTML report; the scenario tutorial now covers both OR-fork
  encodings in depth — **variant-driven** and **data-driven /
  decision-mining** with the per-fork accuracy report — plus a real-log
  capstone (absorbing and replacing the former empirical companion).

### Changed

- **Discrete integer columns get one bin per value.** When an integer
  attribute has at most the requested number of distinct whole-number
  values (e.g. priority levels 1–5), each value is its own bin instead of
  quantile ranges that merged or split them. Fixed in both the family
  partitioner and the dashboards contract.
- **Adaptive time units**: dashboard widgets and the session report show a
  short duration in the largest legible unit (`2.4 h`, `43 m`, `9 s`)
  rather than `0.0 d`.
- Dashboard activity-time metrics are **case-weighted** (documented
  decision; the model performance overlays stay event-weighted).

### Fixed

- **Stub-click navigation** in the SVG viewers actually navigates —
  `setPointerCapture` was retargeting the click off the anchor; resolved
  by hit-testing the click coordinates. Applies to the Model / Compare /
  Family viewers, the session-report model section, and the family
  report's lightbox.
- V4 lost the app name and the Model view's explanation; both restored
  (rail brand → repo, version → release, author byline), and the dead
  top band of the main area reclaimed.

### Security

- **The `.jucm` importer refuses DTDs / `<!DOCTYPE>` before parsing**
  (`_forbid_dtd`), so untrusted `.jucm` input can no longer trigger XML
  entity-expansion ("billion laughs") denial-of-service through stdlib
  `ElementTree` — a zero-dependency alternative to `defusedxml`. (XES
  event-log parsing is delegated to PM4Py.)
- Marked the component-colour `hashlib.md5` as `usedforsecurity=False`
  (it only maps a name to a palette index — non-cryptographic).
- Added `bandit` to the `[dev]` extra for a local static scan
  (`bandit -r pm4py_ucm web -ll`); the medium/high baseline is clean.

### Fixed (web app)

- **The header caption shows the running `pm4py_ucm.__version__`**
  instead of the latest GitHub release, so it reflects the build that is
  actually executing (and surfaces any environment/code mismatch at a
  glance).
- **The deployed app now always imports the current checkout's
  `pm4py_ucm`.** Launched via its main-file shim, the app's
  `sys.path[0]` was the `web/` directory, so `import pm4py_ucm`
  resolved to a site-packages copy — which on Streamlit Cloud lags the
  git checkout (the app code is pulled on every push, but the venv is
  only rebuilt when `requirements.txt` changes). The app now prepends the
  repo root to `sys.path` before importing, so it uses the checkout's
  package code.

## [0.5.2] — 2026-07-14

### Added (web app)

- **Double-click a family model to open it in a new browser tab** — the
  same zoom-in behaviour the Model tab already offers, now on the
  Family tab's grid *and* the Compare tab's two side-by-side cell
  models (an atomic `data-opentab` opt-in plus the shared delegated
  double-click handler; the Family grid also gets an "Open grid in new
  tab" button as a fallback).

### Added (metrics — validation + new statistics)

- **Metric-validation suite** (`tests/test_metric_validation.py`) and a
  **semantic contract** ([`docs/metrics.md`](docs/metrics.md)) defining
  every activity/edge/process/choice metric precisely (units, timestamp
  semantics, aggregation rules, and the deliberate edge-case decisions —
  negative waiting on overlapping intervals, ties, single-event cases,
  and the wall-clock/no-working-calendar caveat). The suite validates
  against four independent oracles (hand-computed distinct-value
  fixtures, algebraic invariants, metamorphic transforms, simulation
  ground truth) and reconciles frequencies, waiting times
  (mean/median/min/max/**stdev**), start/end activities and case
  durations against pm4py exactly. Verdict: the existing metrics were
  already correct.
- **New comparative metrics**, all additive: **rework / repetition**
  (activity `repeat_frequency`, process rework rate + mean repeats),
  **relative frequency** (activity & edge), **start / end activity
  distributions**, **edge case-frequency** (distinct cases per
  handover), and **P90 / P95 percentiles + sample std** on activity
  service, sojourn, edge waiting and case duration.
- Every metric is now selectable in the **performance-overlay menus**
  and written to the **`.jucm` as `perf_<metric>` metadata**, and is
  surfaced in the **interactive HTML report** and the web app's
  **Compare** tab (new columns, cards, and metric selectors). The
  `.jucm` diagram overlay stays byte-stable (only the ≤2 selected
  metrics are drawn).

### Changed

- **Variant partial-order expressions read cleaner** (#11, display
  only): a loop that ran once renders `A^1`, and a single-token loop
  body drops its parentheses (`Test Fix^>=2` instead of
  `(Test Fix)^>=2`); a parallel of a single activity with a skipped
  branch (`A || tau ≡ A`) drops its wrapper, while multi-branch
  parallels and `[A]` choices keep their brackets. Variant clustering,
  counts and fitness are unchanged. A new README table explains the
  notation.

### Fixed

- **Case-insensitive boolean type detection** in data-driven decision
  mining (#6): a case-constant column of mixed-case boolean strings
  (`"True"` / `"FALSE"` / `"TRUE"`) now classifies as a jUCMNav
  **boolean** variable — emitting `x == true` / `x == false` and
  enabling the expression minimizer's complement rule — instead of a
  two-value enumeration. Clean lowercase / native-bool / `0-1` columns
  classify exactly as before (byte-stable exports).

### Changed (web deployments)

- The original model-only **V1 app is retired**: `web/streamlit_app.py`
  is now a shim that runs `streamlit_app_v3.py`, so the primary
  Streamlit Cloud deployment (https://pm4py-ucm.streamlit.app/) serves
  the full V3 app — Model, Scenarios, Family, and Compare tabs —
  without touching its main-file setting. V1's last version remains in
  git history (up to v0.5.1).
- **`web/streamlit_app_v2.py` is restored to the real, frozen V2 app**
  (model + scenarios, byte-for-byte from the last pre-model-family
  state, plus a freeze notice): it had been turned into a V3 shim at
  v0.5.0, but https://pm4py-ucm-scenarios.streamlit.app/ must keep
  serving V2 exactly as referenced by a paper under review. Do not
  modernise that file; new features go to `streamlit_app_v3.py`.

## [0.5.1] — 2026-07-14

### Fixed (performance — resource mining on DataFrames)

- **Resource mining is no longer the hidden cost of "converting" a
  large log.** The DataFrame path of the performer miner iterated the
  log **per row** (`iterrows`); on a 617k-event log that took ~84
  seconds — twice per mine (performer binding + component
  vocabulary), several minutes on Streamlit Cloud — *even when the
  log had no performer attribute at all*, and it ran under the web
  app's "Converting process tree to UCM" label (the actual tree→UCM
  conversion takes milliseconds). The DataFrame path is now fully
  vectorized with an O(1) short-circuit when no priority attribute
  exists as a column: the same 617k-event mine dropped from ~84 s to
  under a second. Semantics are equivalence-tested against the
  per-event path (strategies, priority fall-through, empty/NaN
  handling, bucket ordering — which drives exported component IDs).
  The family pipeline benefits everywhere it mines resources per
  cell.

### Added (progress reporting)

- **`progress_callback(stage, done, total)`** — every genuinely long
  pipeline loop now accepts an optional callback (see
  `pm4py_ucm.util.progress`): variant replay
  (`discover_scenarios` / `clustering.cluster`), per-cell family
  mining (`discover_ucm_family`), umbrella assembly (plug-in
  materialisation and per-cell path-scenario replay,
  `assemble_ucm_family`), and family statistics
  (`compute_family_stats`). Callbacks fire at stage start, completion,
  and throttled intervals (~200/stage), so a repainting UI cannot slow
  the work down; the default `None` costs nothing and output is
  unchanged.
- **Web app: real progress bars.** The Model, Scenarios, and Family
  mining runs now show a progress bar with counts and a
  remaining-time estimate ("Replaying cases — 41,200/84,187 · about
  40s left") inside the status box, driven by the callbacks above.
  Phase labels were made honest: the label that read "Converting
  process tree to UCM" while resource mining ran now says so.
- **Sojourn times as overlay metrics** — `sojourn_mean_time` /
  `sojourn_median_time` / `sojourn_total_time` (time since the case's
  previous event) join `NODE_METRICS`: selectable in the web app's
  performance-overlay sidebar, rendered as `soj avg 2.1d` under
  activity names, and exported as `perf_sojourn_*` metadata lines.
  They work on any timestamped log — the activity-level time overlay
  for single-timestamp logs, matching the Compare tab's statistics.
- **Web app header** — the title is simply *PM4Py-UCM*; the caption
  states the repository's **latest published release** (queried from
  the GitHub API, cached an hour, with an always-valid
  `releases/latest` fallback when offline) and the author. The Model
  tab gained an **"Open image in new tab"** button so complex models
  can be zoomed in a full browser tab (base64 → Blob URL behind a
  plain `target="_blank"` anchor — ordinary link navigation, immune
  to popup blockers). **Double-clicking the model image does the
  same** (zoom-in cursor + tooltip hint; a delegated listener in the
  page's own JS realm survives Streamlit reruns and always opens the
  image's current render).

### Fixed (web app)

- **Applying a decomposition change no longer resets the sidebar.**
  The "Apply changes" button called `st.rerun()`, which aborts the
  script before the widgets below it are instantiated — and Streamlit
  drops the state of widgets skipped in a run. The Notation radio
  silently flipped back to UCM (the diagram re-rendered as UCM while
  the user had selected BPMN), and the resource-attribute, min-support
  and overlay selections were reset the same way. Applying now
  updates the session value and lets the run continue — no rerun, no
  state loss. Reproduced and verified fixed with a headless
  `streamlit.testing.v1.AppTest` flow.

## [0.5.0] — 2026-07-12

Web tool generation **V3** (V2 was the app before the family
features).

### Added (family statistics reports)

- **`compute_family_stats`** — comparative statistics for every cell
  of a mined family, computed once at mine time (no DataFrames kept):
  **process** level (cases, events, events/case, case-duration
  min/mean/median/max and *total*, behavioural variant counts, replay
  fitness), **activity** level (frequency, case coverage, *sojourn*
  time since the case's previous event — available for
  single-timestamp logs — and service-time min/mean/median/max/total
  on interval logs), **edge** level (directly-follows pairs with
  traversal frequency and waiting-time min/mean/median/max/total;
  completion→start on interval logs, completion→completion otherwise
  — labeled as such), and **choice** level: OR-fork branch counts
  *aligned across cells* through the family's skeleton merge, with
  context naming, inside-loop flagging, and "not reached" distinct
  from zero. pandas helpers (`process_frame`, `activity_frame`,
  `edge_frame`, `choice_share_frame`) and a JSON-ready `to_dict`.
- **`write_family_report`** — a **self-contained interactive HTML
  report** (embedded JSON + base64 model images + vanilla JS, no
  external assets, deterministic output, GitHub-linked V3 branding):
  sortable heat-mapped Overview, pairwise Compare (delta cards,
  model images side by side with zoom and open-in-new-tab, activity
  and edge delta/ratio tables on a diverging scale, aligned choice
  bars), Activities and Edges matrices, Choices as 100% stacked bars
  with n everywhere, and a model gallery. Colorblind-safe palettes;
  embedded images render at 192 dpi, never downscale below a 96-dpi
  readability floor, and are palette-quantized to stay small.
- **Web app (V3): Compare tab** — heat-mapped family ranking table,
  A/B pickers with delta metric cards, side-by-side cell models,
  activity/edge delta tables, per-choice branch-share expanders, and
  the HTML report as a download (also offered on the Family tab).
  `web/streamlit_app_v2.py` became `web/streamlit_app_v3.py`; the old
  path remains as a shim for existing deployments.
- **Case-insensitive attribute-value categories** in partitioning:
  raw values differing only in letter case (`F`/`f`) are one value,
  displayed as the log's most frequent spelling (all spellings kept
  on `PartitionValue.raw_values`); the `include_values` filter
  matches case-insensitively and booleans classify in any case. Opt
  out with `ignore_value_case=False`.

### Changed

- `compute_performance_stats` gains min/max service times, per-pair
  min/max waiting times, and per-activity sojourn times — extra
  entries only; overlay/metadata output is byte-stable.
- Durations of 500 days and more display in years; heat-mapped table
  text color is chosen by background luminance (readable in dark
  themes); family-grid **member** separators are thick dark lines and
  member labels are drawn rotated at a larger size.

## [0.4.0] — 2026-07-11

### Added (model families — attribute-partitioned discovery)

- **`discover_ucm_family`** — partition an event log by the values of
  1–2 case-level attributes (e.g. cancer type × age group) and mine
  one UCM per combination. Enumeration attributes partition by value
  (low-count values merge into an *Other* bucket past a cardinality
  cap); booleans by true/false; numeric attributes are binned into
  quantile or user-supplied ranges; missing values go to an *Unknown*
  bucket; combinations below `min_cases` are skipped but recorded.
  The existing `decomposition` argument applies per cell, so families
  can be flat or decomposed. New package
  `pm4py_ucm.algo.discovery.families` (partition / family / algorithm
  / assembly modules).
- **`write_ucm_family`** — one `.jucm` per cell plus a
  `family_summary.csv`, as a zip archive or a directory.
- **`assemble_ucm_family(mode="combined")`** — every cell model in a
  single URN spec as independent root maps, built in **one shared
  container**: one ID counter and shared responsibility/component
  definitions (the same activity is one definition referenced from
  many maps), so repeated runs export byte-identically.
- **`assemble_ucm_family(mode="umbrella")`** — one overarching model
  whose root map is the **shared skeleton** of the cell processes,
  computed by anti-unifying the per-cell process trees: identical
  subtrees are shared verbatim; sequences share their longest common
  prefix/suffix of children (equal-length remainders merge
  position-wise into several localized variation points); loops merge
  on (do, redo); anything else that differs becomes a variation point
  wholesale. Each variation point is a **dynamic stub** whose
  plug-ins are the distinct variant sub-maps, guarded by
  preconditions over enumeration/boolean scenario variables derived
  from the partition attributes; a cell whose process skips a
  variation point gets a pass-through `skip` plug-in. Behaviourally
  identical variants share one plug-in whose selection condition is
  factored over the attribute domains (full cover drops an attribute
  entirely). One `ScenarioDef` (strategy) per combination initialises
  the variables so jUCMNav's traversal selects the matching plug-in
  at every stub. When nothing is shared at the root — or with
  `skeleton=False` — this degenerates to the plain
  `start → dynamic stub → end` umbrella with whole cell models as
  plug-ins. This is the first producer of `Stub.dynamic`,
  multi-binding stubs, and `PluginBinding.precondition` — machinery
  the exporter/importer already round-tripped.
- **Per-cell path scenarios in the umbrella** (default,
  `path_scenarios=True`). Each cell's sub-log is replayed on the
  cell's *configured tree* (the merged skeleton with each variation
  point substituted by the cell's variant subtree — assembled from
  the same node objects the maps were converted from, so replay
  results correlate back to the UCM's OR-forks). One executable
  scenario per (combination × behavioural variant, capped by
  `max_variants_per_cell`): it initialises the attribute variables
  (plug-in selection at dynamic stubs), a `family_variant`
  enumeration value (branch selection at outside-loop OR-forks), and
  per-loop iteration counters. Loop scaffolding (entry guards,
  decrements, exit conditions) is wired once per conversion unit.
  **Inside-loop two-way XORs** get combined `family_variant` +
  loop-counter range conditions (branches distributed across
  iterations by observed per-variant proportions — the single-model
  synthesizer's mechanism, parameterised by variable name and fed
  canonically re-keyed data); enclosing loops are detected on the
  *configured* trees so a loop in the shared skeleton still governs
  an XOR inside a variant plug-in. Conditions land on the arc
  **directly leaving** the fork (`_pull_condition_onto_direct_arc`)
  — the only arc jUCMNav's traversal evaluates (an earlier revision
  put them on the routing bend's outbound arc, where they were
  ignored). On the ClaimsPayment Country umbrella every OR-fork
  branch arc (92/92) now carries a real condition. Uncovered
  variants are noted on the scenario group; inside-loop XORs with
  more than two branches fall back to a deterministic split. New
  module `pm4py_ucm.algo.discovery.families.scenarios`.
- **Value filtering** — `discover_ucm_family(...,
  include_values={attribute: [labels]})` (and `partition_log`)
  restricts an attribute to the listed values; other cases are
  dropped. The web app's Family tab gains per-attribute value
  multiselects (options from the live partition axes, including
  `Other`/`Unknown`), with the coverage preview honouring the filter.
- **Evocative variant plug-in names** — variation-point plug-ins are
  named with the attribute values they cover
  (`Register Claim [AUS | NZL]`) instead of bare ` 2`/` 3` suffixes.
- **Resource variation counts as variation** (umbrella + combined).
  Each `FamilyCell` keeps its own mined `{activity: performer}`
  mapping, and the umbrella's merge keys include the performer of
  every activity in a subtree — so the same activity performed by
  different actors in different cells becomes a variation point even
  under identical control flow (disable with
  `resource_variation=False`). Variant plug-ins (and, in combined
  mode, each cell's maps) bind their cells' performers **visually**
  (`RespRef.cont_ref`); the shared `Responsibility.performer`
  definitions are set only for activities the whole family agrees on.
  A family whose cells are identical in both control flow and
  performers now emits a warning instead of silently producing a
  stub-less umbrella.
- **`save_vis_ucm_family` / `view_ucm_family`** — grid rendering: a
  vertical stack for one attribute, a rows × columns matrix for two,
  with per-cell `n (%)` captions and grayed placeholders for skipped
  combinations (`pm4py_ucm.visualization.ucm.family_grid`).
- **Converter `container` parameter** —
  `from_process_tree.apply` / `decomposition.apply` can now build into
  an existing `UCM` container (post-processing scoped to the new maps;
  derived plug-in names deduplicated against existing maps).
- **Web app (V2): Family tab** — 1–2 attribute pickers over the
  detected case-constant attributes, partition policy controls, a
  pre-mining case-coverage table, and downloads for the per-cell zip,
  combined `.jucm`, umbrella `.jucm`, and grid PNG.

### Added (performance overlays)

- **`annotate_performance(ucm, log, node_metrics=…, edge_metrics=…)`**
  — overlay frequencies and times on the model
  (`pm4py_ucm.algo.performance`). Activity metrics: `frequency`
  (executions), `case_coverage`, and `mean/median/total_time` service
  times for interval logs (`start_timestamp` column). Edge metrics:
  directly-follows `frequency`, `percentage` (an OR-fork branch's
  share of the fork's traversals), and `mean/median/total_time`
  waiting times. Edge statistics are attributed via
  activity-to-activity *segments* (walked through bends/joins; arcs
  crossing another fork or a stub are left unannotated rather than
  guessed), with one annotation on each segment's first arc. The
  overlay lives in two metadata layers: `perf_<metric>` entries —
  **every** available metric, one per line, on RespRefs **and
  connections**, independent of the display selection (jUCMNav lists
  them line by line in the properties view) — and `_perf`, the
  display string for the selected metrics, rendered by the classic
  visualizer as a small gray line under activity names and on edges
  (both UCM and BPMN styles). The exporter writes `<metadata>` on
  nodes and connections and the importer parses both (including
  jUCMNav's own `_hits`), so overlays survive the export→reimport
  path the web app renders through; metadata-free models export
  byte-identically to before. Segment resolution walks **through
  static single-binding stubs** (via the plug-in binding), so
  decomposed models get edge statistics across stub boundaries;
  dynamic/multi-binding stubs stop the walk. Re-annotation replaces
  the previous overlay.
- **Web app**: a "Performance overlay" sidebar section — pick up to
  two activity metrics and two edge metrics; applied to the Model
  tab and to every family cell (grid rendering + per-cell `.jucm`),
  each cell annotated from its own sub-log.
- **`demo/model_families_tutorial.ipynb`** — executed end-to-end on
  `ClaimsPaymentLog`: attribute detection, partition preview,
  per-cell mining, grid rendering, per-cell/combined/umbrella
  exports, path scenarios, and performance overlays (rendered,
  exported as metadata, and used programmatically).
- **Family assemblies annotated too**: `assemble_ucm_family(...,
  node_metrics=…, edge_metrics=…)` overlays the combined model (each
  cell's maps from that cell's sub-log) and the umbrella (shared
  skeleton from the whole family log, each variant plug-in from its
  covering cells' sub-log) — so the Family tab's combined and
  umbrella `.jucm` downloads carry the metadata as well.
  `annotate_performance` gained a `maps=` parameter to scope
  annotation to a subset of a model's maps.

### Added (rendering resolution)

- **`dpi` parameter on the classic graphviz renderer** — layout is
  computed in points, so a higher DPI scales the whole drawing (text
  included) proportionally. Omitted by default, keeping existing
  output byte-identical. The stacked composite's title strips now
  scale with the requested DPI too.
- **Adaptive family-grid resolution.** The grid renderer aims for
  ``target_dpi`` (default 192 — twice graphviz's 96, so exported text
  is actually readable) and enforces a ``max_total_pixels`` budget
  (default 150M) in two stages: a probe-based DPI choice before
  rendering, and exact post-render enforcement that uniformly
  downscales the supersampled panels when panel-shape variance makes
  the projection undershoot. 96 dpi is a hard readability floor — a
  very large family exceeds the budget (with a warning) rather than
  becoming unreadable. Explicit ``dpi`` bypasses both stages. The
  effective DPI is recorded in the PNG metadata
  (``pm4py_ucm_dpi`` text chunk + physical-dimension header). The
  destructive ``max_panel_width`` downscaling that previously crushed
  wide panels to 1600 px is now **off by default**.
- **Web app**: the Family tab embeds a downscaled preview of the grid
  (≤2200 px wide) and serves the full-resolution render through the
  Grid PNG download, so huge exports don't strain the browser.

### Fixed (performance overlays — jUCMNav validity and coverage)

- **`<metadata>` on connections made jUCMNav reject the file**
  (`FeatureNotFoundException: Feature 'metadata' not found`) —
  NodeConnection has no metadata feature in jUCMNav's metamodel.
  Edge annotations now live on the arc's **source node** under
  branch-indexed keys (`_perf_branch<i>` display,
  `perf_branch<i>_<metric>` per metric, for the node's i-th outgoing
  arc); the visualizer and the export/reimport round trip read them
  from there, and connections are emitted exactly as before. A
  regression test asserts connections never carry metadata children.
- **Most OR-fork branches had no edge statistics** because segment
  resolution refused to walk backward through joins (and most forks
  sit right after one). Resolution is now set-based: backward walks
  fan out through joins, forward walks through forks, and the edge's
  statistics are the aggregate of the directly-follows pairs over
  the two activity sets (frequencies/totals add, means are
  frequency-weighted; medians are kept only for single-pair
  segments). On the flat claims model, annotated segments went from
  30 to 47 — including arcs directly after joins.

### Fixed (exporter — multi-binding stubs)

- **Dynamic stubs with several plug-in bindings exported broken
  back-references.** The shared entry/exit arcs of a multi-binding
  stub must list *every* binding's ``<in>``/``<out>`` in their
  ``inBindings``/``outBindings`` attributes (space-separated XPaths,
  as jUCMNav writes them), but the exporter's lookup tables were
  single-valued and kept only the last binding — so jUCMNav could not
  wire the bindings to their plug-in maps. The tables
  (``connection_to_in``/``connection_to_out`` and the plug-in
  start/end companions) are now one-to-many. Single-binding output is
  byte-identical to before.

### Fixed (expression minimizer)

- `X == true` / `X == false` are now recognised as complementary
  literals (they only ever denote boolean variables in this package),
  so `(P && X == true) || (P && X == false)` collapses to `P`. The
  complement-pair merge also checks both directions, restoring
  symmetry for `X != true` vs `X == true`.

## [0.3.2] — 2026-05-20

### Changed (docs)

- **Demo notebook refreshed for the v0.3.x decomposition changes.**
  Section 6 (Hierarchical decomposition) now covers the four boundary
  rules (`on_root_sequence`, `on_parallel`, `on_alternative`,
  `on_loop`) with a runnable cell each on `ClaimsPaymentLog`. A new
  6.4 demonstrates `on_alternative` (six maps: root + five
  alternative plug-ins), and a new 6.8 demonstrates the root-loop
  wrap fix on a hand-built loop-root tree. The auto-preset wording
  is updated from "three rules" to "four rules"; the §4.2 footnote
  is refreshed to match the IssueTracker log actually used in
  sections 0-5; the §10 wrap-up bullets list `on_alternative` and
  the root-loop wrap. Notebook re-executed end-to-end so every cell
  carries fresh outputs.
- **README polished.** Fixed a malformed Streamlit-app badge (was
  missing the leading `[` of the image tag), and added a "Three
  ways to get started" block right after the quick-start snippet
  pointing readers at the tutorial notebook, the web app, and the
  reference docs below.

## [0.3.1] — 2026-05-20

### Fixed (resource mining — silent attribute override)

- **`resource_attribute="org:role"` and `resource_attribute="org:resource"`
  produced identical component vocabularies on logs that carried both
  XES attributes.** Root cause was an enum-aliasing collapse in
  `pm4py_ucm.algo.discovery.resources.algorithm.Variants`: both
  `ACTIVITY_ATTRIBUTE` and `ROLE_THEN_RESOURCE` had the same value
  (the `activity_attribute` module), so `enum.Enum` silently made the
  second an alias for the first. The guard
  `if variant is Variants.ROLE_THEN_RESOURCE` then fired on *every*
  call to `apply()` / `distinct_components()` and injected the
  role-first `attribute_priority = ["org:role", "org:resource",
  "org:group"]` list — which overrode the user's
  `attribute="org:resource"` because the priority list takes
  precedence in the underlying variant.

  Fix: give the two `Variants` members distinct string values and
  resolve to the backend module via a separate
  `_VARIANT_BACKENDS` lookup table. The variant identity check
  in `apply()` / `distinct_components()` now works correctly,
  so the role-first priority is only injected when the caller
  explicitly asks for the `ROLE_THEN_RESOURCE` variant.

  Three regression tests in `tests/test_resources.py` lock in the
  fix: same log, two attributes, distinct vocabularies; distinct
  activity bindings; and the enum members are now distinct
  identities. Visible effect: on `ClaimsPaymentLog.zip`,
  `resource_attribute="org:resource"` now produces 58 components
  vs `"org:role"`'s 10 (previously both produced 10).

## [0.3.0] — 2026-05-20

### Added (decomposition)

- **`on_alternative` boundary rule.** Each branch of an `×` (XOR) or
  `∨` (OR) operator becomes its own plug-in map (symmetric to
  `on_parallel`). The OR-fork / OR-join stays on the parent map; the
  alternative bodies move into per-branch plug-ins. Included in
  `AUTO_DEFAULTS` and `AGGRESSIVE_DEFAULTS` as `True`. Exposed in the
  web app's Decomposition - advanced expander as a checkbox.

### Changed (decomposition)

- **Cap-induced cuts under `×` / `∨` use the same first-to-last
  naming recipe as parallel branches** instead of the dull
  `"sub <first-label>"` fallback. Stub names now read e.g.
  `"alpha to delta"` rather than `"sub alpha"`.

### Fixed (decomposition)

- **Loop at the *root* of the tree is now extracted under `on_loop`.**
  When the outermost operator of the input tree is `*` and `on_loop`
  is enabled, the tree is wrapped in a synthetic single-child
  sequence so the loop becomes a cut candidate. The root map gets a
  single stub pointing to the loop plug-in (instead of having the
  full loop machinery — OrFork / OrJoin / body — drawn inline, which
  was the prior behaviour). When `on_loop` is off, behaviour is
  unchanged (the loop renders inline).

### Notes

- Bumped to a **minor version** (0.2.x → 0.3.0) because the
  decomposition `auto` / `aggressive` presets now extract XOR/OR
  branches by default — output of `discover_ucm_inductive(log,
  decomposition="auto")` on logs with alternative paths will differ
  from earlier releases (more, smaller plug-in maps). The `"off"`
  path is unchanged and byte-stable with all prior releases.

## [0.2.1] — 2026-05-17

### Added (web v1.5)

- **Bundled sample logs.** A new *Sample log* tab in the upload area
  lets users pick a pre-bundled XES (zipped) without having to find
  their own event log. Two ship out of the box
  (`IssueTrackerSyntheticLog.zip`, `ClaimsPaymentLog.zip`); drop more
  files into `web/samples/` to extend the list — they're picked up
  automatically on next start.
- **ZIP archives are first-class inputs.** The file uploader accepts
  `.zip` alongside `.xes` / `.xes.gz` / `.csv`. The miner extracts
  the first `.xes` / `.xes.gz` entry inside, with zip-slip protection
  (entries with `..` or absolute paths are rejected).
- **Per-phase mining progress.** A multi-step `st.status` panel
  reports the current phase (*Reading CSV* → *Formatting events* →
  *Discovering process tree* → *Converting to UCM* → *Writing
  .jucm*), so a 100k+ event log no longer looks hung during the
  multi-minute inductive-miner step.

### Changed (hardening for public deployment)

- **`.streamlit/config.toml`** caps `maxUploadSize` at 75 MB and turns
  off Streamlit's usage telemetry.
- **Pillow's decompression-bomb guard** is now raised to 1 billion
  pixels (was disabled) — keeps protection while still permitting
  realistic mined UCMs.
- **Download filenames are sanitised** to `[A-Za-z0-9._-]`.
- **Mining failures** surface a clean one-line error inline plus an
  expandable *Show technical details* panel, instead of dumping a
  raw traceback to the page.
- **CSV reads use `low_memory=False`** so columns with mid-file dtype
  changes no longer trigger `DtypeWarning` or downstream type
  confusion (root cause of an earlier "import hangs" report).

### Fixed (web bugs)

- **Notation switch no longer flashes "Mining UCM..."** The
  `st.status` panel was created unconditionally around the cached
  `_mine` call and rendered briefly even on instant cache hits.
  An arg-fingerprint check now detects guaranteed cache hits before
  the call and skips the status panel entirely; only genuine
  cache misses surface a status panel / spinner.
- **Role / Resource column selectors no longer reset unexpectedly.**
  The per-rerun "defensive" `_seed_csv_selectors(only_invalid=True)`
  pass was replaced by a strict per-file-hash seeding gate — the
  CSV section seeds the selectors exactly once per uploaded file
  and never overwrites them afterwards. A safety net reseeds an
  individual key only if its stored value is no longer a valid
  option for the current file.
- **Decomposition Apply no longer requires re-confirming the CSV
  column mapping.** Once a column mapping has been applied, pending
  edits to the selectors show a warning + remine button but do not
  block other settings from triggering a remine — mining continues
  against the last-applied column mapping.
- **"Apply column mapping" now reliably starts mining.** The bug
  where the post-click rerun looped back to the *Click Apply…*
  prompt (because `st.file_uploader` returns the same UploadedFile
  on every rerun, and the unconditional reset in the
  `if uploaded is not None:` block fired afterward) is fixed by
  hashing the bytes and only resetting state on a genuinely new
  file.

### Added (inductive miner)

- **Noise-threshold slider** in the sidebar exposes the IMf
  (Inductive Miner — infrequent) threshold. Default 0.2 (the common
  practical default in PM4Py tutorials), range 0.0 – 1.0. The web
  layer pre-mines the process tree with the chosen threshold and
  hands it to `discover_ucm_inductive` via `parameters["process_tree"]`
  — no changes to the package's public API.

### Changed (UX)

- **Updated app caption** to mention CSV alongside XES and the
  sample-log option: "Mine a Use Case Map model from an XES or CSV
  event log and export it to jUCMNav, or to PNG files with BPMN or
  UCM views. Choose an existing log or upload your own."

## [0.2.0] — 2026-05-17

### Added (web front-end)

- **Streamlit web interface** in `web/`. Upload an XES (`.xes` /
  `.xes.gz`) or CSV event log, tune the inductive miner / decomposition /
  performer settings interactively, preview the mined UCM in either UCM
  or BPMN notation, and download the rendered PNG plus the `.jucm` file.
  Mining and rendering are cached separately so toggling notation
  re-renders without re-mining; decomposition advanced overrides and CSV
  column mappings are buffered behind explicit "Apply" buttons so the
  user can stage multiple changes before triggering a remine. Ships with
  Streamlit Community Cloud deployment files (`web/requirements.txt`,
  `web/packages.txt`). See [`web/README.md`](web/README.md) for the full
  walkthrough.

### Changed (UCM-only PNG polish)

- **Thicker UCM paths and contours.** Path penwidth raised from
  graphviz's default `1.0` to `2.6` for the UCM style only — matches
  jUCMNav's heavier line weight and reads more clearly through the
  responsibility-marker squares. Component-cluster border raised to
  `3.0` pt for actors / `2.2` pt otherwise; stub diamond contour
  raised to `2.5` pt. BPMN style keeps the lighter defaults.
- **Bold responsibility and stub labels.** UCM-style `RespRef` and
  `Stub` names render with graphviz HTML `<B>…</B>` labels so they
  stand out from path-line decorations. BPMN style unchanged.
- **Component-reference label sizes tuned.** Bold (`Helvetica-Bold`)
  cluster labels in both styles, sized at `DEFAULT_FONT_SIZE + 3`
  for UCM and `+4` for BPMN.

- **Thicker UCM paths.** Graph-level `penwidth` raised from
  graphviz's default `1.0` to `1.8` for the UCM style only —
  matches jUCMNav's heavier line weight and reads more clearly
  through the responsibility-marker squares. BPMN keeps the
  default 1.0 pt line.
- **Larger, bolder component labels in UCM.** Cluster label
  fontsize bumped from `DEFAULT_FONT_SIZE + 1` to
  `DEFAULT_FONT_SIZE + 5` for the UCM style only; Helvetica-Bold
  unchanged. BPMN keeps the more restrained `+1` pt.

### Fixed (UCM RespRef marker — continuous path)

- The UCM `RespRef` marker is now a small filled **black square
  node** rather than a graphviz `box`-arrowhead glyph. The previous
  approach (invisible point + `arrowhead=box` on incoming edges)
  left a visible white-space gap between the line and the marker
  because `splines=spline` routes around the (still-present) node
  bbox and graphviz places arrowheads with a small offset from
  the target. With a real square node and `arrowhead=none` on the
  edges that touch it, adjacent path segments now meet at the
  square's bbox boundary and the line reads as uninterrupted
  through the marker. The activity name still floats as an
  `xlabel`; BPMN style unchanged.

### Fixed (PNG arrow direction + DirectionArrow rendering)

- **`splines=curved` reverted to `splines=spline`**. The curved
  variant routed rank-back edges (e.g. the redo branch of a loop)
  with their arrowhead at the wrong end — the UCM `box` marker and
  BPMN normal arrowhead would land on the empty point upstream of
  the OR-join instead of on the OR-join itself. `splines=spline`
  (graphviz's b-spline routing around nodes) keeps direction
  consistent.
- **`DirectionArrow` nodes now render as edge-coloured pixels in
  PNG**, identical to `EmptyPoint`. When a `.jucm` produced by
  this exporter is re-imported, the two empty points before a
  loop's OR-join come back as `DirectionArrow` objects (per the
  exporter's promotion rule). Rendering them as the previous
  `rarrow` shape produced large duplicate arrow glyphs on top of
  the path lines, which already carry their own arrowheads.
  Edges into DirectionArrows also drop their arrowhead (matching
  the EmptyPoint treatment), so re-imported models look the same
  as the originals.

### Changed (UCM × marker + label placement)

- **PNG**: the × glyph that used to float above the path as plaintext
  has been replaced by a graphviz `box` arrowhead drawn at the end
  of every edge entering a RespRef. The marker now sits *on* the
  path line instead of as separate text floating above it. The
  RespRef node itself is invisible (`shape=point` with transparent
  colours); the activity name floats as an `xlabel`, both for
  bound and unbound RespRefs. BPMN style is unchanged.
- **PNG**: wider graphviz spacing (`nodesep` 0.40 → 0.75, `ranksep`
  0.55 → 1.00) so external (`xlabel`) names have room to breathe
  instead of overlapping adjacent paths and component boxes.
- **`.jucm`** label-placement heuristic is now bidirectional and
  more aggressive. For every unbound `RespRef` / `Stub`, the
  exporter counts neighbours in a tight column directly above
  *and* below the symbol (within ±80 px x and ±80 px y, up from
  the previous ±35 / ±60). The label is then placed on the
  quieter side: `deltaY=-55` for below (was -20), or `deltaY=+40`
  to push it further above when below is more crowded. Default
  (no override) only fires when both sides are clear.

### Changed (UCM colour + line crossing + arrow visibility)

- **Per-component colours in `.jucm`.** Each `<components>` definition
  now carries `lineColor`, `fillColor` and `filled="true"` attributes
  with the same hashed pastel palette the PNG visualizer uses —
  RGB-triplet format (`r,g,b` decimal) matching jUCMNav's
  convention. Example:
  `<components name="Claims Administrator" id="145" lineColor="0,64,128" fillColor="160,255,255" filled="true" contRefs="42"/>`.
  Colour is attached at the URN-level definition (not per
  ComponentRef) so every reference to the same actor inherits the
  same colour. The hash function moved to
  `pm4py_ucm.objects.ucm.util.component_colors.component_color()` and
  is shared between the exporter and the visualizer.
- **Path lines visually cross the × glyph** in PNG. Edges with a
  UCM-style `RespRef` target now also set `headclip=false`; edges
  with a `RespRef` source set `tailclip=false`. Both segments extend
  to the bbox centroid, so the two halves meet at the × instead of
  a few pixels away. `_BPMN_STYLES` unchanged.
- **Visible arrows into OR-joins / OR-forks** in PNG. The graph-level
  `arrowsize` raised from `0.7` to `1.0` (graphviz default) so
  arrowheads at the tiny OR-join dots stay legible, and an explicit
  `dir=forward` ensures back-edges (e.g. the redo branch of a loop)
  always draw their arrowhead at the *target* side even when
  graphviz routes the curve "backwards" in rank space.

### Changed (UCM PNG polish)

- **Per-component pastel colours.** Component-cluster fill and border
  are now chosen deterministically from a 12-entry professional
  pastel palette via MD5 hash of the component's name, so the same
  team gets the same colour across every map in one render and
  across runs. Actors keep the bold outline that distinguishes them
  but draw from the same hashed palette.
- **Bold component names** in the PNG (cluster labels switched to
  `Helvetica-Bold`). The `.jucm` is untouched — jUCMNav's own font
  settings stay in charge there.
- **Continuous path line through the × glyph.** UCM-style edges that
  terminate at a `RespRef` now drop their arrowhead, so the path
  reads as an unbroken line crossing the × marker rather than
  arrowing into a box and arrowing out again. BPMN style is
  unchanged (its boxed activities are valid flow destinations).
- **Smoother edges** via `splines=curved` on the graphviz top-level
  graph (was `splines=spline`). Produces softer bezier-style
  routing.

### Changed (UCM label placement)

- **PNG (UCM style)**: unbound `RespRef` and `Stub` elements now use
  graphviz's external `xlabel` for their name (with `forcelabels=true`
  on the graph) so the activity label floats next to the symbol
  rather than sitting on the path line. RespRefs/Stubs *inside* a
  ComponentRef cluster keep the compact inline label — the cluster
  already gives them clear space.
- **PNG (UCM style)**: component-cluster labels are pinned to the
  top-left (`labeljust=l, labelloc=t`) so the name stays away from
  path lines crossing through the middle of the rectangle.
- **`.jucm` exporter**: unbound `RespRef` and `Stub` labels get a
  positive `deltaY` (placing them below the symbol) when the model
  has a sibling element directly above — typically a parallel branch
  one row up. Bound elements and elements whose "above" region is
  clear keep the default `<label/>` so jUCMNav renders the name in
  its standard position above the symbol. ComponentRef labels are
  untouched: jUCMNav's default already places them at the top-left.

### Added

- **Hierarchical decomposition.** A new `decomposition=` keyword argument
  on `discover_ucm_inductive(...)` and `convert_to_ucm(...)` splits the
  result into a *root* UCM map plus *plug-in* (sub-)maps connected by
  UCM `Stub` nodes via the existing `PluginBinding` machinery (no model
  changes required). Three combinable boundary rules drive the split:
  - `on_root_sequence` — each top-level `->` child becomes a plug-in.
  - `on_parallel` — each `+` branch becomes a plug-in.
  - `on_loop` — each `*` operator's entire expansion becomes a plug-in.

  Layered safety parameters: `max_leaves_per_map` (hard cap on any
  single map's leaf count, recursively enforced), `min_leaves_to_decompose`
  (floor — small subtrees stay inlined), and `balance_ratio` (sibling
  share threshold — prevents a tiny child of a dominant sibling from
  being pulled into its own near-empty plug-in).

  Accepts `None` / `"off"` (default — single map, byte-stable with
  pre-change exports), `"auto"`, `"aggressive"`, or a dict that merges
  with the `"auto"` defaults. Unknown keys raise `ValueError`.
- **Stacked PNG rendering.** When a UCM contains multiple maps,
  `view_ucm(ucm)` and `save_vis_ucm(ucm, ...)` render every map
  vertically stacked into a single PNG — root map at the top, plug-in
  maps below in pre-order DFS, each panel carrying a title strip with
  its name and separated by a thin horizontal rule. The composite is
  produced via Pillow.
- **Map filter for visualisation.** Both visualisation entry points
  accept a `map="name"` kwarg to render exactly one map's panel — the
  existing single-map UX, scoped to a named plug-in.
- **Stub captions.** Bound stubs gain a small `→ <plug-in name>`
  external label in both the UCM and BPMN styles so the reader can
  follow each stub to its plug-in map.

### Changed

- `Pillow>=10` is now a runtime dependency (used for the stacked PNG
  composite). It is small and pure-Python.
- The process-tree converter's internal `_attach` now writes into an
  explicit `UCMmap` argument rather than the URN container's default
  map. The visible single-map behaviour is unchanged and byte-stable
  against pre-change exports (verified by a pinned `.jucm` fixture).
- **PNG background defaults to white** instead of transparent.
  Multi-map composites previously produced black-on-white panels when
  Pillow converted transparent regions to RGB. Pass
  `parameters={"bgcolor": "transparent"}` to the visualizer to opt
  back in; the composite now alpha-blends correctly in that case too.
- **Edge branch-condition labels are hidden by default** in the PNG
  visualizer. Synthetic conditions emitted by the process-tree
  converter (`redo`, `exit`, `branch0`…) carry no domain information
  for the reader and clutter the diagram. The `.jucm` export still
  carries them — jUCMNav users can rename or delete them there. Pass
  `parameters={"show_conditions": True}` to the visualizer to render
  them in the PNG.

### Fixed

- Plug-in maps now receive auto-layout coordinates in the exported
  `.jucm`. The graphviz layouter was previously called with a fixed
  `map_index=0`, leaving every plug-in map's nodes at the origin.
- `discover_ucm_inductive` now accepts a string path to an XES file
  (already advertised by the docstring); the path is materialised via
  `pm4py.read_xes` before being passed to the inductive miner and the
  resource miner.
- **ComponentRef propagation through Stubs.** When decomposition
  pushed every RespRef into plug-in maps, parent maps ended up with
  only Stubs and no ComponentRefs — jUCMNav then drew the root with
  no actor context. Each bound stub now surfaces its plug-in's
  components on its parent map (transitively, through nested stubs);
  when the plug-in uses exactly one component, the parent-side stub
  is drawn inside that component's rectangle.
- **Resource discovery is now on by default** in
  `discover_ucm_inductive`. When the log carries any of the standard
  XES "who" attributes (`org:resource` / `org:role` / `org:group`),
  components are generated even when the caller doesn't pass
  `resource_attribute` explicitly. Pass `resource_attribute=False` to
  opt out, or pass a specific attribute name to override the fallback
  list.
- **Wrapped labels rendered with single line spacing** in the PNG.
  The shared name-wrap helper joins lines with `\r\n` to match
  jUCMNav's encoding, but graphviz interprets `\r` and `\n` as two
  line breaks, double-spacing every wrapped label. The visualizer
  now normalises to `\n` before rendering.

### Changed (visualization)

- **6-word cap** on derived plug-in / stub names. Long
  ``first to last`` sequences are truncated to the first 6
  whitespace-separated words so stub captions stay readable.
- **No more ``→ <plugin-name>`` caption** under bound Stubs. The
  stub's own name is sufficient context; the arrow caption added
  clutter, especially in stacked multi-map PNGs.
- **Bold, slightly larger font for map names.** The stacked-PNG
  title strips load a bold TrueType variant and use 18pt (was
  16pt); cluster labels in multi-map single-Digraph mode use
  `Helvetica-Bold` at +3pt over the node-label size.
- **BPMN end point redesigned.** The previous styling was a thick-
  bordered black filled circle, visually identical to the start
  point. The end point is now a thick-bordered *white-filled*
  circle with a small black bullet in the centre — the BPMN
  Terminate End Event look.
- **BPMN stub gains the ``⊞`` decomposition marker** below the
  name, matching the BPMN sub-process convention. The UCM style
  continues to rely on the diamond shape alone.

## [0.1.0] — 2026-05-13

Initial public release.

### Added

- **Object model** mirroring the jUCMNav metamodel — `UCM` container with
  `Responsibility`, `ComponentElement`, `UCMmap`, the full `PathNode`
  hierarchy (`StartPoint`, `EndPoint`, `RespRef`, `OrFork`, `OrJoin`,
  `AndFork`, `AndJoin`, `Stub`, `WaitingPlace`, `Timer`, `Connect`,
  `DirectionArrow`, `FailurePoint`, `Anything`, `EmptyPoint`),
  `NodeConnection`, `ComponentRef`, `PluginBinding` (with `InBinding` /
  `OutBinding`), and a `Condition` value-object.
- **Process-tree → UCM converter** producing standard UCM shapes for
  sequence, XOR, parallel, and loop operators, followed by an empty-point
  simplification pass.
- **jUCMNav `.jucm` exporter and importer** — XMI 2.0 emit/parse with
  byte-stable round-trip against jUCMNav's own output, including
  bidirectional `respRefs` / `contRefs` back-references, anonymous
  `NodeConnection` XPath fragments, and full plug-in binding fidelity
  (`parentStub`, `inBindings`, `outBindings`).
- **Inductive UCM discovery** — `discover_ucm_inductive(log)` mines a
  UCM from an event log end-to-end.
- **Resource mining** — discovers `{activity: performer}` from an event
  log's `org:resource` / `org:role` / `org:group` attributes; binds
  responsibilities to component definitions both semantically
  (`Responsibility.performer`) and visually (`RespRef.cont_ref →
  ComponentRef`). Configurable aggregation (`mode`, `first`, `unbound`,
  `all`) and attribute priority.
- **Component vocabulary discovery** — separately surfaces every actor
  seen in the log, so URN-level component definitions exist for actors
  with no per-activity majority binding.
- **Swim-lane layout** — each top-level component gets an exclusive
  horizontal Y-band; nested children inhabit sub-bands of their parent's
  band. Non-overlap and containment are geometric guarantees, not
  constraint-satisfaction problems.
- **Two layout engines** — a built-in Sugiyama-style layouter and a
  graphviz-based layouter that uses the same engine as the PNG
  rendering, so `.jucm` files lay out in jUCMNav the way they look in
  the PNG. Graphviz-based is the default; falls back transparently when
  the graphviz binary is unavailable.
- **Two PNG styles** — `style="ucm"` (filled circle starts, perpendicular
  bar ends, × responsibilities, bars for AND-fork/join, dots for
  OR-fork/join, diamonds reserved for stubs) and `style="bpmn"`
  (activity boxes, gateway diamonds with `X`/`+`, thick-border end
  events).
- **Multi-line label rendering** in `.jucm` exports via `wrap_name()`,
  preserved as unbroken logical names on import.
- **Routing empty points** inserted around forks and joins for smoother
  edges and more layout flexibility (idempotent).
- **High-level API** — `read_ucm`, `write_ucm`, `discover_ucm_inductive`,
  `convert_to_ucm`, `view_ucm`, `save_vis_ucm`, `discover_components`,
  `bind_performers`.
- **Demo** — synthetic 1000-event issue-tracker XES log plus a one-shot
  `mine_and_export.py` script that runs the full pipeline.
- **Test suite** — 108 tests across object model, conversion, export /
  import, visualization, layout, graphviz layout, resources, name
  wrapping, routing points, and stub bindings. Three tests skip
  gracefully when the graphviz binary is unavailable.

### Performance

- O(1) membership checks in `UCMmap` (parallel `set`s next to ordered
  lists) and a linear-pass `simplify_empty_points` eliminate quadratic
  hot spots; the full NASA Java-instrumentation pipeline (78,772 events
  / 2,616 distinct activities) drops from 6.8s to 1.0s end-to-end.

### Notes

This release was developed across four engineering sessions; see
`project-history.md` (if shipped separately) for the development arc
and design decisions in retrospect.

[0.3.2]: https://github.com/ProcessMining-uOttawa/pm4py-ucm/releases/tag/v0.3.2
[0.3.1]: https://github.com/ProcessMining-uOttawa/pm4py-ucm/releases/tag/v0.3.1
[0.3.0]: https://github.com/ProcessMining-uOttawa/pm4py-ucm/releases/tag/v0.3.0
[0.2.1]: https://github.com/ProcessMining-uOttawa/pm4py-ucm/releases/tag/v0.2.1
[0.2.0]: https://github.com/ProcessMining-uOttawa/pm4py-ucm/releases/tag/v0.2.0
[0.1.0]: https://github.com/ProcessMining-uOttawa/pm4py-ucm/releases/tag/v0.1.0
