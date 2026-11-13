# Handoff — after 0.9.0

**0.9.0 is released.** It makes the Dashboards view start itself:
**dashboards suggested from the log**, an **Activity axis**, a per-activity
**rework rate**, the concurrency-aware **behavioural variants** as a segment
axis, and **value axes** on the charts (PRs #139–#141). It is a minor
release because it **drops Python 3.9** and claims 3.13 and 3.14. Design and
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

## State on arrival

- `main` carries 0.9.0, tagged `v0.9.0`, published to PyPI on 2026-11-13
  and **verified installable** (`pip install "pm4py-ucm==0.9.0" --dry-run
  --no-cache-dir`). The maintainer confirmed the README images render on
  PyPI and that the deployed app runs 0.9.0.
- Since `v0.9.0` only documentation has changed: the `docs/` index (PR
  #143) and a **documentation audit** of every prose file against the code
  (see below). No behaviour has changed.
- All three version sites read **0.9.0** and are kept in lock-step by a test:
  `pyproject.toml`, `pm4py_ucm/__init__.py`, `web/sessions/codegen.py`
  (`GENERATOR_VERSION`).
- `CHANGELOG.md`'s `[Unreleased]` holds the `docs/` index and the audit.
  Documentation only: it does not call for a release on its own, though the
  root README is the PyPI page and only refreshes there with a release.
- **[`docs/README.md`](docs/README.md) indexes the documents** (PR #143):
  each by the question it answers, its kind (contract, design note,
  measurements, proposal), whether it is built, and the test that holds it
  to the code. A new document gets a status line under its title and a row
  there. `sessions.md` and `scenario_simulation.md` had kept "not yet
  implemented" headers since before they shipped; a design note's status
  line is prose, so nothing guards it — update it in the PR that ships the
  feature.
- Suite: **1476 passed, 0 failed, 0 skipped** (~3 min locally); CI
  green on Python 3.10–3.14. `requires-python` is `>=3.10`.
- **One deployment.** https://pm4py-ucm.streamlit.app/ serves V6 through the
  `streamlit_app.py` shim. The second deployment
  (`pm4py-ucm-scenarios`, which served the frozen V2) has been **deleted** —
  the SAM 2026 paper that required it was accepted and its final version
  points at the main app. That URL now 404s; do not link it.

## The documentation audit (after 0.9.0)

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

**Everything older than V6 is deprecated** — V5, V3 and V2 alike. They stay
runnable so older results reproduce, each renders a notice on start-up, and
they receive no new features. The line is drawn at persistence: they still
*save* every session parameter (`collect()` refuses a gather missing one, so
their Save buttons work), but only V6 *restores* newly-added ones. New work
goes in V6.

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

A. **Code defects the documentation audit found** — reported, not fixed;
   the documents describe the intended behaviour and say nothing false
   about these. Each needs a test first.
   - **`actService` loses the first event of almost every case.**
     `build_fact_table` discards a start timestamp earlier than the case
     start, but the case start is the first *completion*, so any first
     event with a duration is dropped. On the claims sample the audit
     measured 5,597 of 5,600 first events missing. The suggested "avg
     service" KPI inherits it.
   - **A `per_case` target on a segmented widget is formatted with the
     metric's unit**: a share of 44.2% prints as "44.2 d". Only the
     unsegmented KPI switches to `%`.
   - **`discover_scenarios` ignores `discovery_parameters`** (it mines with
     a bare `pm4py.discover_process_tree_inductive(log)`), so a noise
     threshold can only be set by passing a pre-mined `process_tree`. The
     README documents that workaround.
   - **`discover_ucm_family` does not forward `numeric_coercion_threshold`
     or `max_enum_cardinality`**, although `partition_log`'s docstring tells
     users to pass the former; `api.compute_family_stats` does not expose
     `coarsen_loops`; `assembly._cell_sub_log` hard-codes
     `"case:concept:name"`.
   - **Case duration has two definitions on interval logs**: dashboards and
     the partition advisor use first-to-last completion, the family
     statistics earliest start to latest completion (14.92 d against
     15.07 d on the claims sample). Both contracts now say so; confirm it
     is intended.
   - **`web/sessions/migrate.py` sets `_newer_schema` and nothing reads
     it**; the key is written back into the saved file.
   - **The app's Decomposition help text says the Scenarios view "always
     runs flat"**, but scenarios are attached to the model built with the
     sidebar's decomposition. Check in the app before rewording.
   - **`.devcontainer/devcontainer.json` installs from a root
     `requirements.txt` that does not exist** (it is `web/requirements.txt`),
     so a Codespace gets Streamlit and nothing else.
   - Comments in `dash-ui.js` still name `components.html`; the app embeds
     through `st.iframe`. Cosmetic; left because the file is a shipped
     asset.

B. **One decision is the maintainer's.** `docs/ai_insights.md` §3 specifies
   an *in-tool* LLM provider seam; the stance settled for dashboards is an
   *external* LLM fed an exported brief (`dashboard_suggestions.md` §3).
   The two documents now flag the conflict instead of hiding it.

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
