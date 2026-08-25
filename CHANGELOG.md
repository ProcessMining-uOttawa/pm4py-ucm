# Changelog

All notable changes to **pm4py-ucm** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.7.11] — 2026-08-17

The theme is knowing what a log will cost before spending time on it, and
being honest about which reported numbers are measurements and which are
time limits.

### Added

- `pm4py_ucm.algo.complexity`: `profile_log()`, `screen_mining()` and
  `estimate_replay()`. Mining cost can be ranked but not timed, so the
  screen returns a reason rather than a duration; replay is close to linear
  in cases, so it is measured with a time-boxed probe and extrapolated
  (within 0.79x–1.39x on every log tested).
- `discovery_parameters` on `discover_ucm_inductive()`, forwarded to
  `pm4py.discover_process_tree_inductive`. This exposes `noise_threshold`,
  previously unreachable — every caller mined the tree itself and injected
  it — and the `activity_key` / `timestamp_key` / `case_id_key` overrides
  for logs that are not XES-named.
- `max_replay_states` on `clustering.cluster()`, and `stats` on
  `choice_signature.replay()`. A truncated search and a trace the tree
  cannot produce both return `NOFIT`; they are now distinguishable, and the
  truncated ones surface as `ClusteringResult.budget_exhausted_case_ids`.
- Web front-end **V6**: screens a log's cost before mining, offers one-click
  reductions (keep the 2000 most frequent variants / the 50 most frequent
  activities), and gates the replay-based metrics behind a measured
  estimate. **Not deployed** — `streamlit_app.py` still shims V5.
- `docs/miner_performance.md`: the measurements across twelve logs behind
  all of the above.

### Changed

- `scikit-learn` is now a runtime dependency. The data-driven encoding has
  always imported it but it was declared nowhere, so an install from an
  index produced a package whose data-driven path raised, and CI silently
  skipped the ten tests guarded by `importorskip`.

### Fixed

- V6's two one-click reductions from the cost screen now hold. Both follow
  one rule: **a one-click reduction records what it selected, and never lives
  in a widget.** They travel as `variant_cap` (the *cases* the variant
  reduction selected, on the log as it stood when clicked) and `activity_cap`
  (the activity *names*). Each shows in the sidebar's **Log filters** with its
  own *Remove* button, rides through a saved project, and is applied
  identically by an exported script.

  That rule replaces two encodings that each lost a reduction silently:

  - A **rank range is relative to a population**, and any other filter moves
    it. Applying the variant reduction first and the activity one second
    re-read "keep the top 2,000" against a log that now had fewer than 2,000
    distinct sequences, selecting all of them — the screen then passed and the
    model and replay-based metrics were computed over every case, 7,233
    instead of the 2,010 chosen.
  - **Streamlit owns widget state** and may discard it on a rerun that changes
    nothing. Answering the replay prompt — even just dropping the metrics —
    reset the activity slider to its full range, putting the whole alphabet
    back: on a 274-activity log the model reverted from 50 activities to 224
    and the cost screen reappeared.
- The jUCMNav stub-binding reference fixture is checked in. Its path pointed
  at a sandbox upload that never existed in this checkout, so three tests
  had skipped since they were written; the suite now reports no skips.

### Notes

- No miner setting is a fast path: `disable_fallthroughs=True` and PM4Py's
  DFG-based IMd return structurally identical flower models on every log
  measured, and `multi_processing=True` exhausted system memory on a
  274-activity log. V6 therefore offers log reduction and never a flag.

## [0.7.10] — 2026-08-01

Two changes to the data-driven path, one about speed and reproducibility
and one about what the miner can see at all. Together they mean a log
whose decisive attribute is a number written as text — BPI Challenge
2012's `case:AMOUNT_REQ` is the canonical case — now yields a model
instead of a warning, and mining the same log twice yields the same
model.

**If you have saved projects or exported scripts**, one migration note
applies, and only if your log has a case attribute that is numeric but
serialised as a string *and* has few enough distinct values to have been
an enumeration before. Such a column is now an `integer`, so a family
axis over it is binned into ranges rather than listing discrete values,
and the `include_values` picks stored in a saved project (or emitted as
`FAMILY_INCLUDE_VALUES` into an exported script or notebook) name labels
that no longer exist on that axis. The consequence is a partition
narrower than the one you saved, or — when none of the stored picks
match — a `ValueError` listing the values now available. Re-pick the
values in the Family view and save again. No column in any log we test
against is affected; `numeric_coercion_threshold` above `1.0` restores
the previous typing wholesale if you need the old behaviour while you
migrate.

### Fixed

- **A numeric case attribute serialised as a string is now typed as a
  number, not discarded as a high-cardinality string.** Attribute typing
  in `extract_case_features` consulted the column's *dtype*, so a log
  that wrote its amounts as text fell through to the string branch and,
  if it carried more distinct values than `max_enum_cardinality`, was
  dropped. On `BPI_Challenge_2012.xes` that column is
  `case:AMOUNT_REQ` — perfectly case-constant, 631 distinct values, and
  exactly the variable one would expect to govern a loan application.
  Dropping it left the log with no usable attribute at all, and
  data-driven OR-fork mining abandoned it with "No case-constant
  attributes met the type / cardinality filters".

  Object columns are now coerced with `pandas.to_numeric` before the
  enumeration branch is considered, and typed as numerics when at least
  `numeric_coercion_threshold` of their non-null values parse (default
  `0.99`, exposed on `extract_case_features`, `detect_case_attributes`,
  and `partition_log`). Values that do not parse become `NaN`, which the
  feature encoder already tolerates, so a mostly-numeric column with a
  few `"N/A"` markers stays usable instead of being dropped whole.

  The threshold is strict because the case it guards against is a
  *structured* non-numeric minority rather than a stray marker. A
  clinical log we type records treatment as `"1"`/`"2"`/`"3"` with
  combination therapies written `"2,3"` and `"1,2,3"` — 97.7% numeric.
  Coercing it reclassified six patients' combination therapy as missing
  data, which in a family partition files them under `Unknown` instead
  of giving them their own cell. Columns whose only non-numeric value is
  a single `"na"` or `"?"` sit at 99.6% and stay numeric, as intended.

  Columns naming an *entity* rather than a quantity are never coerced,
  however numeric they look: `org:resource`, `org:group`, `org:role`,
  and `concept:instance` (see `IDENTIFIER_COLUMNS`, matched after
  stripping any `case:` prefix, case-insensitively). Their digits are
  labels, so ordering them or splitting on `org_resource <= 250` is
  meaningless; on `RoadTraffic.xes`, coercion turned `org:resource` into
  a four-range family axis. They remain enumerations when their
  cardinality is low enough. The exclusion governs only the coercion
  path, so a natively-numeric column of the same name types as it always
  did.

  Typing therefore follows the values rather than the declared type, and
  **cardinality no longer overrides it**: a string column of three
  distinct numbers is an `integer`, exactly as an `int64` column of the
  same three values already was. A column that used to classify as a
  low-cardinality enumeration *and* is cleanly numeric now classifies as
  an integer — which changes its guards from `attr == V` equalities to
  `attr <= T` / `attr > T` comparisons, and, in `partition_log`, changes
  such an axis from discrete values to binned ranges when it holds more
  distinct values than the requested `bins`. A saved Web-tool family
  configuration that filtered on the old enumeration labels will no
  longer match: `include_values` naming values that are now range labels
  silently narrows the partition, or raises if it selects none of them.
  Pass `numeric_coercion_threshold` above `1.0` to restore dtype-only
  typing. Genuinely non-numeric strings are unaffected.

  Across the seven logs of our evaluation the net effect is now purely
  additive: `case:AMOUNT_REQ` on BPI 2012, and `Household factor score`
  and `Smoking History` on a clinical log — each previously dropped
  whole, the latter two over a single `"na"` / `"?"`. No column on any
  of the seven changes away from the type it already had.

- **Mining the same log twice now mines the same conditions.** It did
  not: two runs of identical code on `ClaimsPaymentLog` guarded a branch
  with `Broker == Citadel_Insurance` in one and
  `Broker == Greenline_Insurance_Services` in the other, at identical
  accuracy, and ordered the scenarios' `<initializations>` differently
  too. `pm4py.read_xes` does not fix the frame's column order between
  runs, and `extract_case_features` walked it — so the URN variables
  were created in that order, and so were the one-hot feature columns
  the decision tree sees, which is what settles a tie between two
  equally-informative splits. The candidate columns are now ordered by
  name, and the training rows by case ID rather than by whatever order
  the caller collected them in. The mined model is a function of the
  log's content and nothing else. Where a tie previously fell one way or
  the other by luck, it now falls consistently — so a re-mined model may
  differ from a particular past run, and will not differ again.

### Changed

- **The data-driven condition strategy walks the log once, not once per
  case.** Deciding which branch a case took at a given XOR is a question
  about its activity sequence alone, so every case carrying the same
  sequence has the same answer. Two things followed from taking that
  seriously:

  - the labelling pass behind `condition_strategy="data-driven"` replays
    each **distinct sequence** once and fans the result out over the
    cases sharing it — on `RoadTraffic`, 231 replays where there were
    150 370;
  - it reads the cases from the `ParseTable` clustering already built
    instead of re-deriving them with its own `groupby`. Turning that
    561 000-row frame into cases costs ~27 s, more than replaying its
    distinct sequences does, and it had been happening twice.

  `discover_scenarios` therefore builds one table and gives it to both
  consumers; `synthesize_scenarios` accepts it via a new `parses=`
  argument, and the V5 app hands over its session-wide one. As a
  side-effect the labelling and the clustering can no longer disagree
  about a case's sequence: both read the same normalised view, where
  re-grouping the frame took events in row order and clustering took
  them in timestamp order.

  End-to-end `discover_scenarios(..., condition_strategy="data-driven")`,
  measured against 0.7.9 with the log's column order pinned so the two
  runs are comparable:

  | log | noise | before | after |
  | --- | --- | --- | --- |
  | `ClaimsPaymentLog` | 0.0 | 6.2 s | 5.4 s |
  | `ClaimsPaymentLog` | 0.2 | 4.4 s | 2.3 s |
  | `RoadTraffic` | 0.0 | 113.4 s | 44.4 s |
  | `RoadTraffic` | 0.2 | 99.3 s | 41.7 s |

  Output is unchanged, and checked to be: on both logs at both noise
  thresholds the case-to-branch mapping handed to the decision miner is
  identical case for case (1.18 M labels on `RoadTraffic` alone), as are
  every mined tree, every arc condition, and the `.jucm` itself.

## [0.7.9] — 2026-08-01

Executable scenarios you can trust, and frequency counts that conserve —
at a cost you can see and control. Every scenario `pm4py-ucm` writes now
runs to completion in jUCMNav, and together they walk every path of the
model the variants cover. Along the way a replay bug turned up that had
been **inflating reported fitness**, so some numbers legitimately change.

### Added


- **Scenarios are now planned for path coverage.** The goal: if the
  mined variants cover the log's cases, the generated scenario
  definitions should collectively walk every path of the UCM. Two things
  stood in the way, and both are now decided up front by a planner
  rather than improvised per fork:
  - **loops are sized from the tree's structure, not just the log.** A
    body containing a 4-way choice cannot demonstrate all four branches
    in 2 iterations, whatever the conditions say. New
    `suggest_loop_iterations(tree)` computes the minimum from the shape
    alone — a choice needs the *sum* of its branches (one iteration goes
    down one branch), a sequence needs the *max* (its children share an
    iteration), a nested loop needs 1 (its own counter covers what is
    inside it), and a choice on the *redo* path needs one more than in
    the body (redo runs once fewer than the body). Each scenario's
    counter is the larger of what its variant observed — still capped by
    `max_loop_iterations` — and what coverage requires, so the cap trims
    a loop that merely ran often but never one that has to run to stay
    honest;
  - **branches get an explicit schedule.** Each choice hands every
    branch a contiguous, non-empty slice of its variant's iterations,
    sized from that branch's own subtree first and only then shared out
    by observed frequency. Proportional rounding could collapse a rare
    branch to an empty range; and a nested choice — evaluated only on
    the iterations where its parent branch is taken — now has its ranges
    carved out of *those* iterations rather than the whole run.
  Multi-way choices inside loops are handled like any other, removing
  the old `true / false / false / …` fallback that made every branch but
  the first unreachable.

  Measured over the three bundled samples and a real clinical log, at
  two noise thresholds, decomposed and flat: every scenario runs to
  completion, and the scenarios collectively reach **every activity that
  appears in a case the variants cover** — on the clinical log (was 6 of
  10), `ClaimsPaymentLog` (was 24 of 25), `IssueTrackerSyntheticLog`,
  and `devlog`, whose tree has 17 loops nested 15 deep. On `devlog` the
  four activities still unreached occur *only* in cases that do not fit
  the model, so no variant represents them — that is the honest boundary
  of the guarantee, whose premise is "if the variants cover the cases".

  The remaining unreached *arcs* are loop-**bypass** paths — the
  0-iteration case — which no case in these logs exhibits.

  `pm4py_ucm.suggest_loop_iterations(tree)` is public, so a caller can
  see how long the generated scenarios will be to step through before
  generating them. Walked through in
  `demo/scenario_synthesis_tutorial.ipynb` §5.1.

### Changed


- **The replay is optional, and says what it costs.** The traversal
  metrics are the only ones that conserve, but they are also the only
  ones that need the log replayed on the model — minutes on a log with
  thousands of distinct variants, and Streamlit cannot interrupt a mine
  once it starts. The Performance overlay therefore offers **Replay the
  log for traversal counts** (on by default), decided *before* the run.
  Switching it off falls back to the observed event and
  directly-follows counts, and the diagram caption says so plainly:
  cheap, but they do not add up where the model has parallel branches or
  silent skips.

  The counts are never estimated from a *partial* replay. A
  half-finished pass would bias them toward whichever variants happened
  to come first and would understate how much of the log the model
  explains — an error that looks like a modelling problem rather than a
  measurement one. Skipping is honest; guessing is not.

- **The log is replayed once per session, not once per view.** Variant
  clustering (Scenarios) and traversal counting (Model) need *exactly*
  the same parses, and replay is the most expensive step in the package
  — on BPI Challenge 2012 at `noise_threshold=0.0` it is **24 minutes**
  for 4 366 distinct sequences, against 39 seconds to mine the tree
  itself. They each replayed independently, so looking at both views
  paid it twice.

  New `algo.discovery.variants.parses` holds the single pass: replay
  each distinct activity sequence once, keep everything either consumer
  needs, and hand the same `ParseTable` to both. `cluster()` and
  `compute_traversal_stats()` accept it via a `parses=` argument and
  replay nothing themselves; the web app builds it once per
  log + noise + filters and shares it across the two views.

  The table also carries the **normalised log**, which turned out to
  matter as much: converting a DataFrame into per-case sequences costs
  1.6 s on the 5 600-case claims log — twenty times what replaying its
  164 distinct sequences costs — and every consumer needed it. Sharing
  both more than halves the work even on logs where replay is cheap
  (claims: 328 replays → 164, and 2.2× faster end to end).

### Changed — Web app (V5)


- **The Model view reports replay progress**, the way the Scenarios view
  already did. Computing the traversal metrics is the longest phase of a
  mine on a large log, and it showed only a static "Replaying the log on
  the model…" — with no count and no estimate there was no way to tell a
  slow run from a hung one. It now reads `Replaying variants — 250/4366 ·
  about 29 min left`, and the alignment stage names its own budget
  (`Aligning unfitted variants (≤10s)`) so a phase that stops early
  doesn't look like one that stalled.

### Fixed


- **Alignment repair could take hours; it is now bounded.** Computing the
  traversal metrics aligns each non-fitting case to its nearest path
  through the model, and that alignment's cost turns out to be not merely
  high but *unpredictable* — it follows the model's loops and choices, not
  the trace's length. On a 2 455-case GitHub-SDLC log the same model
  aligned a 12-event sequence in 0.05 s and a 10-event one in **26 s**, so
  no length threshold makes it safe; its 893 non-fitting sequences
  projected to roughly **ten hours**, which made the default overlay
  unusable on that log.

  The repair phase now has a wall-clock budget — `max_repair_seconds`
  overall (default 10 s) and `max_repair_seconds_per_sequence` for any one
  sequence (default 1 s) — enforced by `pm4py-ucm` rather than delegated,
  since pm4py's own whole-log time parameter does not reliably stop a
  batch. Sequences are attempted in order of how many **cases** carry
  them, so a budget that cannot cover everything is spent where it buys
  the most coverage, and anything left unaligned is reported in
  `unexplained_cases` rather than silently attributed. Pass `None` to
  either limit for the previous unbounded behaviour, or `repair=False` to
  skip alignment.

  That log now completes in about 10 s with coverage rising from 51 %
  (fitting cases only) to 63 %. The bundled samples never reach the
  budget — their alignments are milliseconds — so their numbers are
  unchanged.


- **Synthesised scenarios deadlocked in jUCMNav when the model had a
  loop.** Two independent defects, both reported on a real clinical log
  where all four scenarios failed — as an infinite loop, a blocked
  AND-join, or a scenario that never reached its end point:
  - the **loop counter was decremented by a responsibility inside a
    choice**. The synthesizer attached the `counter = counter - 1`
    expression to the first responsibility reachable from the LoopJoin,
    assuming it "runs once per body iteration" — true only when nothing
    can bypass it. With a body like `X(A, →(…))` that responsibility sits
    in one branch, so iterations taking the other left the counter
    untouched, the redo condition stayed true, and the traversal spun
    forever. The site is now required to **dominate** the loop body;
    otherwise a dedicated decrement node is spliced directly after the
    LoopJoin, where nothing can bypass it (that fallback already
    existed for tau-only bodies).
  - a **branch inside a loop body could never fire**. The inside-loop
    XOR conditions partition the counter as `(lower, upper]` with
    `upper` starting at the counter's initial value, so they assume the
    counter still reads that value the first time a body choice is
    evaluated. Decrementing at the top of the body shifted every
    evaluation down by one and left the topmost range unreachable — the
    branch that should fire on the first iteration never fired, and its
    activity appeared in no scenario at all. The decrement now sits at
    the **end** of the body, immediately before the LoopFork: the one
    position that is both unavoidable and after the body's choices. It
    is always a dedicated node, so a modeller's own activity is no
    longer decorated with bookkeeping either.
  - the **loop-entry guard raised an AND-join's arity**. Its bypass arc
    was wired straight to the post-loop node; when that node is an
    AND-join — which fires only once *every* incoming arc has delivered
    — a 2-branch parallel suddenly needed 3 tokens and could never
    complete. The bypass and the loop exit are alternatives, so they now
    merge through an OrJoin, leaving the join's arity unchanged. (Only
    the loop's own incoming arc is re-routed; folding in the other
    predecessors would collapse the parallelism itself. The equivalent
    problem for a Stub target was already handled.)
- **Concurrency-aware replay could return a wrong parse.** A parallel
  block replays each child against a *projection* of the window — a
  fresh list whose positions are their own coordinate space — but the
  parse memo is keyed on `(subtree, start, end)` with no list identity
  in it. Sharing the enclosing trace's memo across that boundary let the
  parse of one window answer for a different one whenever two
  projections of the same subtree had the same length and different
  content. Observed on a real log: a choice reporting one branch taken
  twice and its sibling never, for a trace containing one event of each.
  Each projection now gets its own memo. This affected anything reading
  a replay's branch counts — scenario synthesis and, since 0.7.8, the
  traversal metrics.
- **Traversal counts are now exact under nested loops.** A loop entered
  more than once runs a different number of body iterations each time,
  and the replay recorded only the *maximum* per node — correct for
  sizing a scenario's loop counter, but an over-count when used as an
  execution count (an outer loop running twice around an inner one that
  ran 3 then 1 iterations reported 6 body executions instead of 4).
  `replay()` gained a `loop_total_counts` output carrying the total over
  all visits, alongside the existing max, and `traversal_frequency` uses
  it. With both fixes, every activity's traversal count now equals its
  observed event count across the fitting cases of both bundled sample
  logs, at every noise threshold.
- **A saved project no longer loses the metrics you picked when the replay
  is switched off.** Turning off *Replay the log for traversal counts*
  swaps each traversal metric for its event-based counterpart so the
  diagram still says something — a render-time substitution, with the
  pickers deliberately left alone so the toggle stays reversible. But the
  project gather read the substituted lists rather than the picks, so
  saving wrote `frequency` where you had chosen `traversal_frequency`, and
  the choice was gone for good: resuming and switching the replay back on
  returned counts that do not conserve. The picks are now stored as
  picked, and the opt-out is stored beside them as a new `overlay_replay`
  setting, so a resumed project restores both. The exported Python script
  carries the pair too — an `OVERLAY_REPLAY` constant resolving the same
  fallback at run time — which means the script reproduces exactly what
  the session showed, and flipping that one constant back on recovers the
  conserving counts without re-picking any metric.
- **Exported notebooks are valid at the format version they declare.** The
  generated `.ipynb` announced nbformat 4.5, which requires every cell to
  carry an `id`, and supplied none. Jupyter has been quietly patching them
  in while warning that it will become a hard error — and
  `nbformat.validate()` only warns, which is why the notebook's own
  validity test never objected. Cells now carry positional ids, so the
  notebook is well-formed and two exports of the same project remain
  byte-identical.

## [0.7.8] — 2026-07-31

Frequency numbers that **conserve**. Counting events and counting
directly-follows pairs answer two different questions, and on a model
with concurrency or silent skips they contradict each other — an activity
with 257 executions could show an outgoing edge of 40, and a branch whose
alternative was a silent skip reported 100 %. The overlay now counts how
often the log *walks the model*, and says how much of the log that
covers.

### Added

- **Replay-based traversal counts — frequency numbers that conserve.** A
  frequency overlay used to read two different things off the log: an
  activity's number was its *event count*, an edge's number a
  *directly-follows count*. On a model with concurrency those disagree
  badly, because a directly-follows pair only exists when two events are
  **adjacent in the trace** — on a parallel block the event that actually
  follows an activity usually belongs to a sibling branch, so the
  traversal is counted nowhere. A silent skip is worse: it produces no
  event, so a choice whose alternative is "do nothing" reported **100 %**
  for the branch that happens to be observable. On a 258-case clinical
  log that meant an activity with 257 executions whose only outgoing edge
  read `40`, a fork with 197 inflow whose choice split `25`/`4`, and a
  death branch at `100 %` where the true figure is `25 %`.

  New `pm4py_ucm.compute_traversal_stats(tree, log)` counts how often the
  log **walks the model** instead, by replaying it on the process tree
  the model was built from. The counts conserve by construction: an
  activity's count equals the count on its own edges, every branch of a
  parallel fork carries the fork's inflow, and the branches of a choice
  sum to it. New `traversal_frequency` (activities and edges) and
  `traversal_percentage` (fork branches) overlay metrics, passed to
  `annotate_performance(..., traversal=..., tree=...)`. The existing
  event-count and directly-follows metrics are unchanged and still
  available. See the new §9 of [`docs/metrics.md`](docs/metrics.md).

  Non-fitting cases are aligned to their nearest path through the model
  so the counts cover the whole log (`repair=False` counts only exact
  fits). Either way `TraversalStats` reports coverage — a model mined
  with a noise threshold explains only part of its log, and the numbers
  say so rather than quietly describing a sub-log.

- **Partition advisor (Family view) — deterministic, no LLM.** A new
  **💡 Suggested attributes** table ranks the log's case attributes by
  *discriminative power* — control-flow divergence (normalised mutual
  information between the attribute and the trace variant) plus case-duration
  effect size, discounting identifiers and near-constant fields — so choosing
  *which* attribute to build a family on is a guided recommendation rather than a
  guess. When nothing scores high it says so. New public
  `pm4py_ucm.rank_partition_attributes(log)` returning ranked `AttributeScore`s.
  (This is the deterministic half of the AI-insights §4.1 "partition &
  decomposition advisor"; the optional LLM sense-check is a separate later
  layer. See `docs/ai_insights.md`.)
- **`devlog` sample log** — a real developer-activity CSV (285 cases, rich case
  attributes) bundled alongside the two synthetic samples; CSV files are now
  accepted as sample logs.

### Fixed

- **AND-fork edge frequencies.** On a parallel (`+`) fork, every case that
  reaches the fork runs *every* branch, so a branch's traversal frequency is the
  fork's inflow — not the interleaving-dependent directly-follows count, which
  split the inflow across branches (e.g. `8` + `197` = `205`) and contradicted
  the branch activities' own frequencies. Each AND-fork branch now reads the
  inflow, consistent with its activity. (An XOR whose alternative is a silent
  *skip to the end of the process* still can't show the skipped cases — those
  produce no directly-follows pair — a separate, deeper limitation.)

### Changed

- **Concurrency-aware variant clustering replays once per distinct
  activity sequence** instead of once per case. A trace's parse depends
  only on its sequence, so cases sharing one were doing identical work —
  and the waste was worst on non-fitting traces, which only give up after
  exhausting the backtracking budget. On the bundled claims log this cuts
  5,600 replays to 164 (34× fewer, ~1.8× faster end to end); on a
  258-case clinical log, 258 to 111. Output is unchanged — same variants,
  same case ordering, same noise bucket.

### Changed — Web app (V5)

- **The performance overlay now leads with the traversal counts, and says
  what they cover.** The diagram caption names the measure ("Counts =
  cases walking this path, replayed on the model"), a note under the
  metrics row reports how much of the log the model explains ("4,990 of
  5,600 cases (89 %) fit this model exactly") and what happened to the
  rest, and branch shares carry the base they divide (`25% of 258`) so a
  percentage can't be read against the wrong denominator. When fit drops
  below 70 % the note becomes a warning pointing at the noise threshold —
  the setting that actually changes it. Selecting the directly-follows
  metric instead is still supported and says so in the caption.
- **Sub-maps are navigable both ways.** A stub / composite activity already links
  *down* to its plug-in map; now each plug-in map's **end point links back up to
  its parent map** (UCM and BPMN), so a decomposed model is no longer one-way.
- **Sample logs carry a one-line description** in the picker.
- **Useful defaults out of the box.** The Model tab now opens in **BPMN**
  notation — the notation most readers already know — with UCM one click away
  (the library's own `style=` default is unchanged); decomposition now defaults
  to **auto** (shape-fitted) instead of off; the activity overlay leads with the **time**
  metric (median service/sojourn time) then frequency; and the first time an
  overlay is active the **heat-map emphasis** turns on automatically with the
  **Per family member** scale. Each remains freely changeable and the choice
  sticks.

## [0.7.7] — 2026-07-31

Shape-fitted decomposition: `"auto"` now scales its map-size parameters to the
process-tree shape instead of fixed magic numbers, and the Model-view control is
reworked (off / auto / two pre-sets / Custom).

### Changed

- **Decomposition `"auto"` now fits its parameters to the process-tree shape**
  instead of fixed magic numbers. New `pm4py_ucm.suggest_decomposition(tree)`
  scales `max_leaves_per_map` (≈ 1.5·√N) and `min_leaves_to_decompose` (≈ 0.15·N)
  with the tree's activity-leaf count N, so a small model stays flat and a large
  one splits into readable, not-too-many maps. A decomposition dict may carry the
  `"auto"` sentinel for either size dimension; it is resolved against the tree in
  `apply()`, so the app, the exported pipeline, and every family cell decompose
  identically.

### Changed — Web app (V5)

- **Decomposition control reworked.** The dropdown is now **off / auto /
  Pre-set: Max=8/Min=4 / Pre-set: Max=6/Min=3 / Custom** — `auto` fits the map
  size to the tree shape, the pre-sets pin fixed dimensions, and **Custom** sets
  `max_leaves_per_map` / `min_leaves_to_decompose` / `balance_ratio` by hand. All
  modes decompose on every operator kind; the four `on_*` toggles (all on) live
  under **Advanced — boundary rules & sizes**, where the size inputs are editable
  under Custom and shown as a caption for `auto` / the pre-sets. All of these —
  sizes and toggles — round-trip through save/resume and the exported pipeline.

## [0.7.6] — 2026-07-30

An attribute-based log filter written in the ƒ metric language (which gained
categorical equality to support it), and a collapsible log-source section.

### Added

- **The ƒ custom-formula language gained categorical equality.** Alongside its
  numeric/temporal expressions, a formula can now compare a categorical case
  attribute to a value: `attr("Channel") == "Web"` (and `!=`), matched against
  the attribute's value dictionary and yielding a per-case `1`/`0`/null. A
  quoted value is legal only as one side of such an `==`/`!=`; anywhere else it
  is an error. Mirrored in both the Python evaluator and `dash-engine.js`
  (pinned by the parity test), so dashboard metrics get it too.

### Added — Web app (V5)

- **Attribute-based log filter.** A new **Attribute filter (ƒ)** box under **Log
  filters** keeps the cases whose per-case ƒ predicate is true — the same
  grammar as a custom dashboard metric, e.g. `attr("Channel") == "Web"`,
  `attr("amount") > 500 and duration() < 30`. It rides in `filter_spec` as
  `attr_expr`, so it round-trips through save/resume and the exported Python
  pipeline like every other filter. (New public helper
  `pm4py_ucm.algo.dashboards.predicate_case_ids`.)

### Changed — Web app (V5)

- **The log-source picker and CSV column mapping are now collapsible.** They
  live in one **📁 Log source & columns** expander that auto-opens while there
  is no usable log (or a CSV whose columns still need Apply) and collapses once
  mining can proceed, reclaiming the vertical space they used to take on every
  view.

## [0.7.5] — 2026-07-28

Two bug fixes: model families no longer hang while computing statistics on logs
with long traces, and a CSV whose role/resource column is named `concept:name`
now mines.

### Fixed

- **Model families no longer hang while computing statistics on logs with long
  traces.** The concurrency-aware replay behind the per-cell variant/fitness
  statistics memoises its sub-parses, but memo hits skipped the
  `max_replay_states` budget — so the un-memoised sequence/loop backtracking
  could revisit the same sub-problems exponentially often and spin forever on a
  200+-event trace (the Family tab froze partway through "Computing family
  statistics"). Every replay entry now charges the budget, so a trace that
  can't be parsed within it is reported as noise, exactly as a budget-exhausted
  parse already was.

### Fixed — Web app (V5)

- **A CSV whose role/resource column is literally named `concept:name` now
  mines.** The chosen role/resource columns are mapped to `org:role` /
  `org:resource` *before* `pm4py.format_dataframe` instead of after: because
  `format_dataframe` writes the canonical `concept:name` activity column by
  dropping any same-named column, a role column named `concept:name` was being
  overwritten by the activity copy and then renamed away — leaving no activity
  column, so mining failed with "the specified activity column is not contained
  in the dataframe". The same fix is applied to the exported Python pipeline's
  `read_log`.

## [0.7.4] — 2026-07-22

Export the whole session — model, scenarios, model-family **and** dashboards — as
a runnable `.py` + tutorial `.ipynb` in one download, with vector `.svg` beside
every `.png` and the performance heat-map carried into the exported artifacts;
plus fixes so the exported scenario variant count matches the app and the Family
view's settings survive save/resume.

### Added — Web app (V5)

- **The Python export can now include the dashboards.** Alongside the model,
  scenarios and family, the *Export as Python* control emits a `run_dashboards`
  step that rebuilds the fact table over the same filtered log and renders **all
  saved dashboards into one** self-contained interactive HTML file — a read-only
  header switcher moves between them (via `build_fact_table` +
  `write_dashboard`, which gained `dashboards=` / `active=` for this). A
  **pinned-model** widget is populated: when — and only when — a dashboard pins
  the model, the mined model is embedded as SVG for both notations (with the
  heat-map), so it renders instead of showing a grey box.
- **The pipeline exports vector `.svg` alongside every `.png`.** `run_model`
  writes `model.svg` and `run_family` writes `family_grid.svg`, both carrying the
  heat-map like their PNGs (`save_vis_ucm_family` now forwards the heat-map to
  its `.svg` export too).

### Fixed — Web app (V5)

- **The exported pipeline's scenarios now match the app's variant count.** The
  generated `run_scenarios` let `discover_scenarios` re-mine its own process
  tree at `noise_threshold=0` (a perfect fit), so every trace the app had
  treated as noise resurfaced as an extra variant (e.g. 11 instead of the app's
  9 on ClaimsPaymentLog). It now pins the **same noise-thresholded tree** the
  Model view and the app's Scenarios view cluster on.
- **Resuming a project no longer drops the Family view's First/Second attribute
  or Min/Max/Bins.** Those are main-area widgets, whose `session_state` Streamlit
  discards while another view is active — so restoring into the raw widget key
  was garbage-collected before the Family tab was ever opened (attributes reset
  to the first one), and saving from another view captured the widget defaults
  instead of the user's sizes. Both sides now go through the durable sticky
  mirror (`_sticky_seed` on restore, `_sticky_get` on save), so the family
  configuration round-trips — and the exported Python code sees the real values.

### Changed — Web app (V5)

- **The exported Jupyter notebook is now an interactive tutorial.** Instead of
  defining every function and calling a single `run()` in the last cell, the
  `.ipynb` defines each stage and **immediately runs it**, showing the
  intermediate result inline — the loaded log, the case counts before/after
  filtering, the mined model image, the variants table, the family grid and the
  dashboard files — so it reads like a personalised walkthrough. The `.py` script
  keeps its function-plus-`run()` structure for automation. The model and family
  previews now show the exported **SVG** (crisp and scalable) when it exists,
  falling back to the PNG.
- **One export button.** *Export as Python* is now a single **⬇ Export Python
  (.py + .ipynb)** download that bundles both flavours in a zip, with the
  scenario / family / dashboards options all pre-selected. (The `web/sessions`
  API keeps the two separate `generate_script` / `generate_notebook` entry
  points.)

## [0.7.3] — 2026-07-21

Export a session as a runnable Python pipeline, a cycle-time log filter, the
performance heat-map extended across the Family/Compare views and every exported
artifact, and a batch of dashboard and save/resume fixes.

### Fixed — Web app (V5)

- **Low-cardinality numeric case attributes are offered as discrete levels in
  dashboards, not quantile ranges.** A whole-number attribute with fewer than
  10 distinct values (e.g. a 1–5 rating) now gets one bin per level — so
  dashboard **filters and segments** offer the individual levels (`1`, `2`, `3`,
  `4`, `5`) instead of merged ranges like `1–2 / 2–3 / 4–5` — *regardless of the
  requested bin count*. The attribute stays numeric, so the ƒ-formula `attr(…)`
  still reads its value. Columns with ≥ 10 distinct values are still quantile-
  binned.
- **Saving/resuming a project no longer drops the family de-dup setting**, and
  a CI guard now keeps the *restore* side of save/resume from drifting. The
  "merge behaviourally identical plug-ins" checkbox is keyed by the family
  fingerprint (unknown until the family is mined), so a loaded project only
  *noted* it instead of restoring it; it now hands the value off to the
  checkbox when it renders. More importantly, two static drift guards were
  added: one asserts every registry parameter is referenced where a loaded
  project is applied (`_apply_project_config`), and one asserts the reverse-map
  restores every `filter_spec` key the transform reads (so a new pre-mining
  filter like the cycle-time band can't be saved-but-not-restored). The
  existing guard already covered the *save* side; the restore side was
  unguarded.
- **A model pinned to a dashboard keeps its performance heat-map.** The
  Dashboards pinned-model widget rendered its SVG without the overlay's
  heat-map settings, so the colour/thickness emphasis vanished on the pinned
  copy (the metric sub-lines, which ride in the `.jucm`, were unaffected). It
  now renders with the same heat-map kwargs as the Model view (shared
  `_model_heat_kwargs()` helper), so a pinned model matches what you pinned.
- **The performance heat-map now applies to the Family and Compare views**, not
  only the Model view. Selecting *Heat-map emphasis* colours and thickens the
  activities/edges of every family cell and of the two compared members, using
  the same first-of-each-layer overlay metric. It **survives changing the
  compared processes** (each Compare cell re-renders with the heat-map), and now
  also reaches the **family grid PNG download, the Compare-cell PNG, and the
  interactive HTML report** — so every rendering of a family carries it.

### Changed — Web app (V5)

- **Heat-map scale is now three-way**, each with an inline caption and a `?`
  explaining it:
  - **Local (per map)** — every map on its own min/max (each sub-map of a
    decomposed model highlights its own hotspots);
  - **Per family member (across its maps)** — each Family/Compare member scaled
    to its own range, pooled over all of its (decomposed) maps, so a colour is
    comparable *within* a member (the whole model in the single-model Model
    view);
  - **Global (across family members)** — every member against **one shared
    range**, so a colour means the same thing in every member and the cells are
    directly comparable.

  The persisted setting is `overlay_heatmap_scope` (`"local"` / `"global"` /
  `"family"` — `"global"` = per-member, `"family"` = across-members); the old
  boolean `overlay_heatmap_global` is migrated on load. New:
  `classic.heat_span(models, …)` computes a shared cross-member span,
  `model_to_svg` / `family_grid.render_svg` accept explicit `node_span` /
  `edge_span` overrides, and `write_family_report` gained a `heat=` kwarg that
  carries the heat-map onto the report's embedded per-cell images.

### Added — Web app (V5)

- **Cycle-time filter.** A new two-handled **Cycle-time percentile (case
  duration)** slider in the sidebar's **Log filters** keeps cases by their
  end-to-end cycle time (last − first event): `0` = fastest, `100` = slowest.
  Drag the left handle in to drop the fastest cases, the right handle in to drop
  the slowest, or pick a middle band (e.g. `0–10` keeps the fastest 10%,
  `90–100` the slowest 10%). Like every log filter it is global — every view and
  export mines the filtered log — and it round-trips through a saved project.
- **Export the analysis as a runnable Python pipeline.** A new **Export as
  Python** control in the sidebar's **Project** group emits a plain-Python
  script (`.py`) — or a Jupyter notebook (`.ipynb`) — that reproduces the
  current session over the public `pm4py_ucm` API: log loading, the pre-mining
  rename + filters, inductive mining with the session's parameters,
  decomposition, performers, the performance overlay, and the model export.
  Optional check-boxes add the **scenario-synthesis** and **model-family**
  pipelines. Because a project stores only *inputs* and every artifact
  recomputes, the emitted script is a **faithful, deterministic replay** —
  running it reproduces the same `.jucm`. The generated pipeline also **carries
  the pre-mining cycle-time filter** and **reproduces the performance heat-map**
  in its exported artifacts — the model PNG, the family grid PNG, and the
  interactive HTML report's embedded images — matching what the user saw. Turns
  a GUI exploration into an automatable, version-controllable pipeline and
  doubles as a personalised tutorial. Deterministic (no LLM) — a template
  emitter over the session parameter registry. See
  [`docs/code_export.md`](docs/code_export.md).

### Internal

- New Streamlit-free `web/sessions/codegen.py` (`generate_script` /
  `generate_notebook`) with a golden test asserting the emitted script's `.jucm`
  is byte-identical (modulo the exporter's wall-clock timestamp) to a direct
  public-API pipeline, plus a drift guard that every registry parameter is
  handled or intentionally ignored.

## [0.7.2] — 2026-07-19

**A Model-view performance heat-map**, plus fixes to the save/resume and
dashboard views. The heat-map colours and thickens activities and edges by a
chosen metric so bottlenecks read at a glance; its on/off and scale settings
travel with a saved project like every other overlay setting.

### Added — Web app (V5)

- **Performance heat-map** — an optional Model-view emphasis (a checkbox in the
  Performance-overlay group) that colours and thickens activities and edges by
  the value of the **first** selected metric of each layer. A time metric drives
  a **red** ramp, any other a **blue** one; lighter/thinner = lower,
  darker/thicker = higher. A **scale** control chooses **local** (each diagram
  on its own min/max — every sub-map highlights its own hotspots) or **global**
  (every map against the whole model's min/max, so a value reads the same
  everywhere); they coincide when the model isn't decomposed. In **BPMN** the
  activity box is tinted (a pale value-scaled wash) under a stronger, thickened
  contour; in **UCM** the responsibility marker itself colours and **grows**
  with the value (a small marker was hard to spot), and the already-thick UCM
  paths use a lower thickness ceiling. A path keeps one colour and thickness
  across its routing points (empty points) up to the next real node, so a
  segment reads as a single line. It is a render-time overlay of the existing
  `perf_<metric>` metadata — the on-screen SVG and the SVG / PNG downloads all
  agree, and the `.jucm` is unchanged. No metric on a layer → that layer is
  unchanged.
- **Overlay metrics apply on a button.** Picking overlay metrics re-annotates
  the model, so the activity / edge selections now stage behind an **Apply
  metric changes** button rather than re-mining after each single pick.

### Changed — Web app (V5)

- **BPMN decomposed activities (sub-processes) are now pastel green** with a
  green contour (was pink), reading as a distinct, calmer sub-process colour.

### Fixed — Web app (V5)

- **The Family and Compare view settings no longer reset when you navigate
  away and back.** Their config widgets live in the main area, and Streamlit
  discards the state of any widget not rendered on a run (sidebar widgets are
  immune because they always render), so leaving the Family tab reset the
  attribute, the max-values / min-cases / bins, and the value filters to their
  defaults. Those widgets are now made *sticky* (mirrored into a persistent
  key), so a selection survives view switches — independent of save/resume.
- **Pinned process-model widgets on a dashboard now have scrollbars.** The
  model widget (and the session report's model section) is sized in real
  pixels inside a scrollable box, so zooming grows native scrollbars that
  track the diagram — a model larger than the card can now be scrolled, not
  only dragged.

## [0.7.1] — 2026-07-19

**Save, share and resume a whole analysis session.** A configured session —
log reference, CSV mapping, renaming, filters, performers, overlays,
decomposition, family and scenario settings, the open view, **and the
Dashboards** — round-trips through a project file, so an analysis can be put
down and picked back up, or handed to a colleague.

### Added — Web app (V5)

- **Save a project** from the sidebar's **Project** group, as either a small
  **settings file** (`<log>.ucmproj.json`, configuration only — email-able, no
  event data) or a self-contained **project bundle** (`<log>.ucmproj.zip`,
  configuration + the event log). (#63)
- **Resume a project** from the **log-source** area: a bundle brings its own
  log; a settings file re-uses the current log or re-requests it, warning on a
  hash mismatch. Every restored setting re-seeds its widget, and the caches
  recompute the model, scenarios, family and reports from the restored
  configuration. (#64)
- **Dashboards travel with a project.** A small, versioned, build-free
  bidirectional component bridges the browser-island dashboards back to the
  host on save and restores them on resume, so a saved project carries every
  dashboard you built (widgets, targets, segments, the pinned model). (#65)

### Fixed — Web app (V5)

- **The Family and Compare tabs now reproduce on resume.** Unlike the Model and
  Dashboards views, the Family view mines only on demand, so a resumed project
  used to show its restored attributes but no mined family — leaving Compare
  (which reads the family) empty. Opening the Family tab after a load now
  **re-mines the family automatically once**, and Compare's process pair is
  restored best-effort. (#66)

### Internal

- A **Session Parameter Registry** (`web/sessions/`) is the single source of
  truth for what a project persists, with a CI drift guard that fails if a keyed
  configuration widget is neither registered nor explicitly ignored — so
  persistence keeps working as the app grows. Streamlit-free and unit-tested.
  (#62, #63)
- Replaced the deprecated `use_container_width` with `width="stretch"` for
  Streamlit 1.57. (#60)

See [`docs/sessions.md`](docs/sessions.md) for the full design.

## [0.7.0] — 2026-07-18

**Global log filtering and activity renaming across the whole web app**, a
downloadable filtered log, and project branding. The Streamlit front-end is
now **V5** (`web/streamlit_app_v5.py`); the deployment shim
(`web/streamlit_app.py`) runs it, so https://pm4py-ucm.streamlit.app/ serves
V5. V4/V3 are strict subsets and remain in git history.

### Added — Web app (V5)

- **A pre-mining log filter**, global to every view and export. Keep
  **activities** or **trace-variants** by frequency rank with two-handled
  range sliders (most / least / a middle band), **exclude** named activities
  (listed alphabetically), and restrict the **date window** (cases
  *intersecting* or *fully inside*). The Model view's Activities / Cases /
  Events metrics read *selected / total* while a filter is active.
  (#47, #50, #51, #52)
- **A "top variants by case coverage (%)" box** kept in sync with the variant
  rank slider — type a percentage and the slider snaps so the top variants
  cover at least that share of cases, and vice-versa. Recomputed on the
  activity/date-filtered log. (#52, #53)
- **The filtered event log is downloadable** as its own asset in **XES** and
  **CSV**, from the Model view; the filename carries the active filter
  description. (#52, #53)
- **Activity renaming** — a real *pre-mining* transform that relabels (and can
  **merge**) activities before mining, so the new names flow to **every view
  and every export**, including the exported log and the `.jucm`. Edited in a
  modal dialog (with an **Apply** button, so edits re-mine only when you ask),
  seeded from a **CSV/JSON** map upload, and **exportable as JSON** in the same
  format the loader accepts. (#55, #57, #58)
- **The global filter now also drives the Family, Compare and Dashboards
  views** (and their exports), not just Model and Scenarios. (#54)
- **Project logo** — the app's sidebar brand and browser-tab favicon, the
  README header, and the generated API-docs site. (#56, #58)
- **Resizable model viewers** — a diagram-height slider on the Model, Family
  and Compare SVG viewers; a pinned model's default name carries the active
  filter. (#48)

### Changed

- **The sidebar is decluttered** — the less-used groups (performers,
  performance overlay, log filters) collapse into expanders. (#50)
- The family **"merge behaviourally identical plug-ins"** option defaults off
  and moved to the Prepare-downloads section (it only shapes the umbrella
  `.jucm`). (#49, #51)
- The Streamlit app is now **V5**; `streamlit run web/streamlit_app_v5.py`.

### Fixed

- **CSV logs no longer crash the filter** with `ArrowNotImplementedError:
  Function 'unique' has no kernel matching input types (list<item: string>)`.
  `pm4py.format_dataframe` gave a CSV import's activity columns the arrow-backed
  `string` dtype, which broke the variant aggregation; the log is now coerced
  to plain `object` strings so CSV behaves like XES everywhere. (#53)
- **Family re-mine no longer raises `CacheReplayClosureError`** (a cached miner
  drew a progress bar as a child of the caller's `st.status` block). (#49)
- **The rename dialog reliably captures an in-progress cell edit** on Apply
  (the editor now lives in an `st.form`), and a mapping upload now reports a
  clear error for a malformed file and a warning for names that don't match any
  activity in the log (case-sensitive), instead of failing silently. (#58)

### Docs

- The end-to-end tutorial notebook gains **filtering** and **renaming**
  sections (the code equivalent of the web app's pre-mining transforms).
- READMEs (main, `web/`, `tests/`, `demo/`) refreshed for **0.7.0 / V5**.

## [0.6.5] — 2026-07-18

Model-view viewer and overlay refinements.

### Fixed

- **The inline model viewer's scrollbars now track the zoom and the diagram
  size.** It previously zoomed with a CSS transform inside a clipped stage,
  so the scrollbars reflected neither; the diagram is now sized in real
  pixels inside a scrolling stage. The wheel zooms toward the cursor,
  dragging pans, and a stub click scrolls to its plug-in. Applies to the
  Model, Family, and Compare viewers.
- **No more double scrollbars.** The host iframe's own scrollbars are
  suppressed, so only the viewer's inner pair shows (the outer pair used to
  overlap and hide the useful inner one).

### Added

- **The performance overlay pre-selects sensible metrics.** Activities open
  on `frequency` plus a time metric — service time (`median_time`) when the
  log has two timestamps, otherwise sojourn time (`sojourn_median_time`) —
  and edges on `percentage` + `frequency`. Chosen per log; change any of them.

## [0.6.4] — 2026-07-17

Refinements to the web front-end: a zoomable pinned model, faster mining
when settings change, and download files built only when you ask for them.

### Added

- **A pinned model widget is now zoomable.** A model pinned to a dashboard
  used to be a flat image; it now behaves like the Model view and the
  session report — scroll to zoom, drag to pan, click a stub to jump to its
  sub-map — preferring the crisp inline SVG and framing the whole model to
  the card on open.
- **One "Prepare downloads" button per view.** The Family and Scenarios
  views no longer build their download files up front. The per-cell zip,
  the combined and umbrella `.jucm`, the grid PNG and the interactive
  report (Family), and the synthesized `.jucm` (Scenarios) are built only
  when you ask, on a single button — so mining or synthesizing just to look
  at the result stays fast.

### Changed

- **Changing the decomposition no longer re-mines the process tree.** The
  Model and Scenarios views share one cache for the parsed log and the
  inductive tree, keyed on the log and the noise threshold alone — so
  toggling the decomposition (or, for scenarios, the strategy / loop
  settings) reuses the tree and rebuilds only the tree→UCM conversion. This
  dominates the cost on complex logs.
- **Mining a model family is markedly faster.** The download-only umbrella
  and combined assemblies — the bulk of a family mine — are deferred to the
  new "Prepare downloads" button, so browsing the grid no longer pays for
  artifacts it may never use. The umbrella's variation-point counts moved
  next to its download, where the umbrella that produces them is built.

## [0.6.3] — 2026-07-17

Three more ways to draw a widget. Each is a new *rendering* of a figure
the engine already computes, so none of them changes a number.

### Added

- **Line charts** — the ordered series a bar draws, as a line with
  hoverable points. Offered for a series metric (WIP, arrival rate) and
  for a one-axis segmentation **over a time axis**: a line says the order
  means something, which it does not on a categorical axis.
- **Gauges** — a KPI against its target as a dial: a value arc coloured by
  target state and a tick at the goal. Offered only when the widget has a
  target — without one there is nothing to read the value against.
- **Pie / donut charts** — each segment's share of the total, drawn as a
  donut with the total in the hole and a legend. Offered only when the
  aggregation is `sum`, the one case where slices add to a whole; the tail
  past eight slices folds into one *Other* slice and says so.
- **`sum` is offered for time metrics**, not just counts — a total
  duration ("28,928 case-days") is a real quantity, and it is what a pie
  needs. Still off `rate`, where a sum of rates means nothing.

### Changed

- The composer's **Chart picker is context-aware**: it offers only the
  visualisations that say something true about the current metric and
  segmentation, and disappears when there is no choice to make.

## [0.6.2] — 2026-07-17

Finishes the dashboards UI: the composer picks a visualisation by
thumbnail, a breached target says which segments broke it, and a log can
hold several named dashboards — exportable together as one file.

### Added

- **Multiple named dashboards** — a log can hold several, switched from a
  header dropdown with **New / Rename / Delete**. Each keeps its own
  widgets and filters; the whole set persists per log in the browser. (The
  switcher lives in the Dashboards view rather than the app rail because
  the embedded island owns its own state — the host cannot read it back.)
- **Export all dashboards in one file** — ⬇ Export all bundles every
  dashboard into a single self-contained, offline HTML file whose header
  switcher moves between them, read-only.
- **Scorecard breach drill-down** — a segmented target's scorecard row
  expands to the segments that broke it (worst first); clicking one filters
  the whole dashboard to that segment.
- **Viz thumbnail picker** — the composer's Chart row is now clickable
  tiles, each a small glyph of the visualisation, instead of a dropdown.

## [0.6.1] — 2026-07-16

A dashboards-refinement release on top of 0.6.0: new distribution and
save/load features, direct-manipulation of the widget grid, the migration
off Streamlit's deprecated HTML-embedding API, and a browsable API
reference.

### Added

- **Histogram and box-plot widgets** — an unsegmented per-case metric can
  now show its whole distribution instead of a single aggregate. New
  `histogram` / `box_stats` engine functions (mirrored Python↔JS): a
  histogram of equal-width bins, or one bar per value for a small
  whole-number metric; and a Tukey box plot (five-number summary,
  1.5·IQR whiskers, outliers). Chosen in the composer's **Chart** row,
  which also previews the live distribution.
- **Date-range filter** — the filter picker gains a "Date range" option
  with two date inputs seeded from the log's span, producing the engine's
  existing `date` filter.
- **Save / Load dashboard definitions** — download a dashboard's recipe
  (name, widgets, filters) as a small JSON file and reload it, on the same
  or another log. A cross-log **binding report** names any widget that
  references activities or attributes the target log lacks.
- **Drag to reorder and drag to resize widgets** — a grip drags a widget
  to a new position; a corner handle resizes its grid span, persisted per
  widget so it travels with a saved / exported dashboard. Replaces the
  ◄/► step buttons.

### Changed

- **Island embedding migrated from the deprecated `st.components.v1.html`
  to `st.iframe`** in the V4 app (Dashboards island, SVG model viewer,
  open-image-in-tab) — behaviourally identical (same `srcdoc` sandbox).
  `web/requirements.txt` now pins `streamlit>=1.57,<2`.

### Fixed

- Histogram / box-plot widgets are no longer **clipped** on the dashboard:
  they span like the other charts and the chart fills its card, so the
  baseline and axis labels stay visible.

### Documentation

- **API reference** built with [pdoc](https://pdoc.dev) and published to
  **GitHub Pages** (<https://processmining-uottawa.github.io/pm4py-ucm/>)
  on each push to `main`, via a new `docs` workflow.
- **`tests/README.md`** — a per-file map of the test suite, regenerable
  with `tests/gen_readme.py`.
- READMEs: corrected the deployed-app references from V3 to **V4**, and
  added a full description of the **ƒ custom-formula grammar** to the web
  README.

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
