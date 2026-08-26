# Handoff — version 0.8.0

Scenario simulation, coverage and A/B comparison. Stages 1–4 are on `main`;
**Stage 5 is what remains.** The plan is
[`docs/scenario_simulation.md`](docs/scenario_simulation.md) — read that
first, it carries the decisions and the reasons.

## State on arrival

- `main` at `690b02d`, working tree clean, **no open PRs**.
- Released: **0.7.12** on PyPI and tagged; V6 is the deployed app
  (`streamlit_app.py` shims it), so https://pm4py-ucm.streamlit.app/ serves
  the cost screen.
- `CHANGELOG.md` `[Unreleased]` already holds the 0.8.0 body so far
  (Added / Changed / Fixed). **Nothing is version-bumped**; `pyproject.toml`
  still says 0.7.12, which is correct — 0.8.0 has not been cut.
- Suite: **1121 passed, 0 failed, 0 skipped** (~4½ min locally).

## What is already done

**The simulator is public and complete enough to use.**
`pm4py_ucm.algo.scenario_traversal` runs a UCM's scenarios the way jUCMNav
does and reports the same problem kinds; `scenario_coverage` turns the
results into coverage and A/B comparisons; the web app's Scenarios view has
a **Simulation** section at the bottom with both highlight modes.

| stage | |
|---|---|
| 1 · visit record, simulator exported | done (#104) |
| 2 · stub traversal, so decomposed models simulate | done (#105) |
| 3 · coverage, A/B, colours + hover text | done (#106) |
| 4 · the Scenarios-view section | done (#107) |
| **5 · persistence + codegen** | **not started** |

Landed alongside, from a bug the maintainer found:

- **Synthesis was producing structurally invalid models** (#108). A
  `LoopEntryGuard` bypass arc landed directly on nodes that admit one
  incoming segment, so two responsibilities and an AND-fork each had two.
  Fixed by merging through an `OrJoin`.
- **`validate_ucm()` / `check_ucm()`** and the `.jucm` exporters now refuse
  to write a malformed model (`validate=False` to override).
- A/B colours, per-cell family-grid progress, and the Elements/Paths
  coverage split (#109).

## Stage 5 — what is left

From the design note:

- Registry parameters for the simulation selection and mode, so a saved
  project resumes into the same view. Add to `web/sessions/registry.py`,
  restore in `_apply_project_config` / `_apply_filter_spec_to_state`.
- `web/sessions/codegen.py` emits the equivalent calls, so an exported
  script reproduces the simulation the app showed.
- Then cut 0.8.0: bump the three version sites, date the changelog, tag.

**Mind the rule the session learned twice:** persisted option values are
**identifiers, not labels**. `_VIEWS` entries are compared directly, set via
`goto_view`, and stored as `active_view`; the view icons are applied through
the radio's `format_func` for exactly that reason. Whatever Stage 5 persists
must follow the same discipline.

## Release checklist (when 0.8.0 is cut)

1. Bump **three** sites, kept in lock-step by a test: `pyproject.toml`,
   `pm4py_ucm/__init__.py` (`__version__`), `web/sessions/codegen.py`
   (`GENERATOR_VERSION`).
2. Retitle `[Unreleased]` and open a fresh empty one.
3. Full suite, then PR → merge.
4. `gh release create v0.8.0 --target main` — the tag's commit must contain
   `.github/workflows/publish.yml`, or the release-triggered run is created
   **silently not at all**.
5. Check the tag does not already exist, with a **version sort**:
   `git ls-remote --tags origin | sed 's#.*refs/tags/##' | sort -V | tail`.
   Lexical order puts `v0.7.10` before `v0.7.5`.
6. Approve the `pypi` environment gate. It is a required-reviewer
   checkpoint; the build job passes first and the upload waits.
7. Verify with `pip install pm4py-ucm==0.8.0 --dry-run --ignore-installed
   --no-cache-dir`. **Use `--no-cache-dir`** — a stale local pip index
   reported "no matching distribution" for a release that was already live.

## Traps, and what they cost

Carried forward because each one bit during this session:

- **Streamlit owns widget state and may discard it on a rerun that changes
  nothing.** A reduction parked in a slider evaporated when the user merely
  answered a prompt. Anything that must survive belongs in a plain session
  entry the app owns, not a widget key.
- **A rank range is relative to a population.** "Keep the top 2,000
  variants" re-read after an activity filter selected *everything*. Record
  what was selected, not a rank.
- **graphviz ignores `tooltip=` unless the element also has a `URL`.** The
  design note asserted otherwise and was wrong. Hover text is injected by
  rewriting the `<title>` graphviz always emits — which also stopped every
  diagram exposing memory addresses on hover.
- **Verify in the app, not only in tests.** After changing the A/B colours
  the suite was green while the selectors still read "A (red)" / "B (blue)".
- **Check a regression test actually fails without the fix.** Two of this
  session's tests were tautological on first draft — one ended in
  `or True`, another compared a set with itself.
- **Merging two PRs that both edit `CHANGELOG.md` under `[Unreleased]`
  conflicts**, however disjoint the code is.
- Docs-only merges can skip CI with `[skip ci]` in the **merge commit**
  subject (`gh pr merge --subject`). It skips every workflow for that push,
  including the pdoc deploy.

## Testing the web app

Read `docs/miner_performance.md` for the measurement harness, and note the
mechanics that cost hours:

- `form_input` does not drive Streamlit selectboxes; click the combobox
  open, then click the `[role="option"]`. Synthetic `.click()` on a
  checkbox input is also ignored — use the element ref.
- **Never read the DOM mid-render.** A partial page looked exactly like a
  missing sidebar, and later showed a *previous* render's numbers, which
  nearly produced a wrong diagnosis. Wait until the server log stops
  growing.
- **Log to a file and read the server log.** A rerun clears the UI, so a
  Streamlit exception that aborts a rerun is invisible to UI-based
  instrumentation.
- The browser pane starts narrow, and Streamlit auto-collapses the sidebar
  below ~768px — the view radio then does not exist. Resize to ≥1280 wide.
- Screenshots need the pane displayed; `computer` clicks by **ref** work
  without one.
- To read the rendered model, the SVG is inside the viewer iframe:
  `iframe.contentDocument`, not its `srcdoc`.

A synthetic log with ~2,700 variants over 80 activities trips both screening
thresholds and cycles in seconds. **Shape matters**: to reproduce the
variant-cap bug the rare activities had to carry the sequence distinctions,
so projecting onto the top 50 collapsed the variant count *below* the cap.
The generator is worth rebuilding rather than guessing at the numbers.

## Confidential logs

Several logs used in the measurements are confidential and must not enter
the repo. The sample picker reads `web/samples/` with no path override, so:
add `web/samples/_tmp_*` to `.git/info/exclude` **before** copying anything
in — that file is local and untracked — then delete both the file and the
rule afterwards and confirm with `git status`. The IRCC log lives at
`C:\Users\jucmn\Downloads\ircc_uOttawa.csv` (274 activities, 7,734 cases,
543k events; ~10 min to mine after reduction).

## Open items, by value

1. **Stage 5**, above.
2. **`validate_ucm` is not called anywhere except the exporters.** Wiring it
   into conversion or synthesis would make the bug class impossible rather
   than merely detectable — a behaviour change worth deciding deliberately.
3. **Stale `.jucm` artifacts.** Any `.jucm` exported before #108 is likely
   malformed; `family_umbrella.jucm` carried the defect across 14 maps.
   Anything archived is worth regenerating.
4. **The default noise threshold of 0.20.** A clinical log fits zero cases
   at it. Still unresolved, and still the oldest open question here.
5. **`demo/model_families_tutorial.ipynb` writes ~2 MB of embedded SVG.**
   Fine, but the notebook grew from 909 KB when it moved to vector output.
6. `web/streamlit_app_v5.py` still says "Notation (Model tab)"; V6 dropped
   the parenthetical. Cosmetic, and v5 is no longer deployed.
