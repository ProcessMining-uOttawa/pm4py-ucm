# Handoff — after 0.8.3

**0.8.3 is released.** Scenario simulation, coverage and A/B comparison are
complete: stages 1–5 of [`docs/scenario_simulation.md`](docs/scenario_simulation.md)
are all on `main`, and that plan is now history rather than a to-do. 0.8.1
added the validation gates, the variant-filtered log export, and two fixes
found by using the app; 0.8.2 was a single-fix patch for a 0.8.0 regression that
broke the Family view on every second look; 0.8.3 stops every rendered diagram
leaking memory addresses, and adds the release tooling described below.

## State on arrival

- `main` carries 0.8.3, tagged `v0.8.3`, published to PyPI.
- All three version sites read **0.8.3** and are kept in lock-step by a test:
  `pyproject.toml`, `pm4py_ucm/__init__.py`, `web/sessions/codegen.py`
  (`GENERATOR_VERSION`).
- `CHANGELOG.md` has a fresh empty `[Unreleased]`.
- Suite: **1180 passed, 0 failed, 0 skipped** (~6 min locally); CI green on
  Python 3.9–3.12.
- **One deployment.** https://pm4py-ucm.streamlit.app/ serves V6 through the
  `streamlit_app.py` shim. The second deployment
  (`pm4py-ucm-scenarios`, which served the frozen V2) has been **deleted** —
  the SAM 2026 paper that required it was accepted and its final version
  points at the main app. That URL now 404s; do not link it.

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
sort puts `v0.7.10` before `v0.7.5`), and whether `publish.yml` is present at
HEAD — without it the release-triggered run is created *silently not at all*.

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

1. **The five README screenshots show a UI that no longer exists.**
   `web/PM4Py-UCM-*.png` were captured at **v0.5.2**: the tabbed layout, a
   "Decomposition (Model tab only)" heading, no left rail, no cost screen, no
   Simulation. They are three releases and a redesign out of date, and there is
   no shot of the Scenarios view's Simulation section at all.

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
