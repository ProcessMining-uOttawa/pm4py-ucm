# Handoff — after 0.8.0

**0.8.0 is released.** Scenario simulation, coverage and A/B comparison are
complete: stages 1–5 of [`docs/scenario_simulation.md`](docs/scenario_simulation.md)
are all on `main`, and that plan is now history rather than a to-do.

## State on arrival

- `main` carries 0.8.0, tagged `v0.8.0`, published to PyPI.
- All three version sites read **0.8.0** and are kept in lock-step by a test:
  `pyproject.toml`, `pm4py_ucm/__init__.py`, `web/sessions/codegen.py`
  (`GENERATOR_VERSION`).
- `CHANGELOG.md` has a fresh empty `[Unreleased]`.
- Suite: **1133 passed, 0 failed, 0 skipped** (~4½ min locally); CI green on
  Python 3.9–3.12.
- **One deployment.** https://pm4py-ucm.streamlit.app/ serves V6 through the
  `streamlit_app.py` shim. The second deployment
  (`pm4py-ucm-scenarios`, which served the frozen V2) is **retired** — the SAM
  2026 paper that required it was accepted and its final version points at the
  main app.

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

## Release checklist (for the next one)

1. Bump **three** sites, kept in lock-step by a test: `pyproject.toml`,
   `pm4py_ucm/__init__.py` (`__version__`), `web/sessions/codegen.py`
   (`GENERATOR_VERSION`).
2. Retitle `[Unreleased]` and open a fresh empty one.
3. Full suite, then PR → merge.
4. `gh release create vX.Y.Z --target main` — the tag's commit must contain
   `.github/workflows/publish.yml`, or the release-triggered run is created
   **silently not at all**.
5. Check the tag does not already exist, with a **version sort**:
   `git ls-remote --tags origin | sed 's#.*refs/tags/##' | sort -V | tail`.
   Lexical order puts `v0.7.10` before `v0.7.5`.
6. Approve the `pypi` environment gate. It is a required-reviewer
   checkpoint; the build job passes first and the upload waits.
7. Verify with `pip install pm4py-ucm==X.Y.Z --dry-run --ignore-installed
   --no-cache-dir`. **Use `--no-cache-dir`** — a stale local pip index
   reported "no matching distribution" for a release that was already live.

## Traps, and what they cost

Carried forward because each one bit during development:

- **Persisted option values are identifiers, not labels.** `_VIEWS` entries and
  `simulation_mode` store `"Model"` / `"coverage"`; the icons and words come
  from each widget's `format_func`. A label in a saved file breaks the day the
  wording changes. This rule has now been learned three times.
- **Selections are recorded by name, not rank or index.** A rank is relative to
  a population and any other setting moves that population.
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

1. **Stale `.jucm` artifacts.** Any `.jucm` exported before #108 is likely
   malformed; `family_umbrella.jucm` carried the defect across 14 maps.
   Anything archived is worth regenerating.
2. **The default noise threshold of 0.20.** A clinical log fits zero cases at
   it. Still unresolved, and still the oldest open question here.
3. **`demo/model_families_tutorial.ipynb` writes ~2 MB of embedded SVG.** Fine,
   but the notebook grew from 909 KB when it moved to vector output.

Struck from this list at 0.8.0: V5's "Notation (Model tab)" label. V5 is
deprecated now, so cosmetic work on it would contradict the no-new-work rule.
