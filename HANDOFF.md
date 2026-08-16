# Handoff — release 0.7.11

Written 2026-08-16 at the close of the session that produced the work. The
SAM 2026 paper is done and published; this repo's next job is shipping
0.7.11. Everything the release needs is listed here, including the traps
that cost real time last round.

## State on arrival

- **11 commits sit unpushed on `main`** (`e2d9650` … `f81e487`). Nothing is
  in flight and the tree is clean apart from an untracked `.mutmut-cache`
  that belongs to another session.
- `pyproject.toml` still says **0.7.10**.
- `CHANGELOG.md` has a filled-in `[Unreleased]` section — that is the
  0.7.11 body, ready to be dated and retitled.
- Everything else described below is committed and pushed.

## What 0.7.11 contains

See `CHANGELOG.md [Unreleased]`. In one line: the cost screening
(`algo.complexity`), the parameter passthrough that finally exposes
`noise_threshold`, the gave-up/doesn't-fit split in replay, the V6 web app,
`scikit-learn` declared as a real dependency, and `docs/miner_performance.md`.

**Careful about the baseline.** PyPI's 0.7.10 artifact was built from
`main@64a0437`, while the git tag `v0.7.10` points at `e328b25`, six commits
earlier — the two are not the same source, and that discrepancy was left in
place deliberately. So "since 0.7.10" is ambiguous: use the PyPI artifact
commit (`64a0437`) as the baseline, which makes 0.7.11 = the 11 unpushed
commits plus `92e1c63`, `59daa44` and `2066c1d`.

## Release checklist

1. Push the 11 commits (`git push origin main`) — they have never left this
   machine, and this checkout is shared with other sessions.
2. Bump `version` in `pyproject.toml` to `0.7.11`.
3. In `CHANGELOG.md`, retitle `[Unreleased]` to `## [0.7.11] — <date>` and
   open a fresh empty `[Unreleased]`.
4. Run the suite: `python -m pytest -q`. Expect **1026 passed, 0 skipped**.
5. Commit, then create the release: `gh release create v0.7.11 --generate-notes`.
6. Approve the deployment when the `pypi` environment asks — the build job
   runs first and the upload waits on that approval.
7. Verify: `pip install pm4py-ucm==0.7.11 --dry-run --ignore-installed`.

### Traps that will bite otherwise

- **Tags here are pre-created.** `v0.7.11` may already exist, pointing at an
  older commit. Check with `git tag --sort=version:refname`, never
  `git tag -l | tail` — lexical order puts `v0.7.10` *before* `v0.7.5`, and
  that is exactly how the 0.7.10 baseline discrepancy happened.
- **A release-triggered workflow resolves its file at the tag's commit.** If
  the tag predates `.github/workflows/publish.yml`, no run is created at all
  — silently, not as a failure. That is why 0.7.10 had to be dispatched by
  hand. Either tag a commit that contains the workflow, or fall back to
  `gh workflow run publish.yml -f target=pypi`.
- **The dispatch path skips the tag-vs-version guard** (it is gated on
  `github.event_name == 'release'`), so if you dispatch, check by hand that
  `project.version` matches the tag. PyPI never lets a version be re-used.

## Open items, roughly by value

- **Issue #97** — the four sites that pre-mine a tree to set a noise
  threshold are now redundant. V2/V3/V5 are deliberately left alone; new
  code should use `discovery_parameters`.
- **V6 is not deployed.** `streamlit_app.py` still shims V5. Repointing it is
  a deliberate decision, not part of this release unless you want it to be.
- **`activities > 50` in `screen_mining` is untested** in the region that
  matters. It fires only on logs the variant clause already catches, and no
  log measured sits between 46 and 274 activities. A wide-but-regular log
  would tell you whether the clause earns its place.
- **A clinical log fits zero cases at the default noise threshold.** One
  160k-event log mines a tree with 0.0% fitness at 0.20 and 100% at 0.0.
  Worth deciding whether 0.20 is the right default to ship.
- **`_mine_family` honours the replay decision but cannot share the parse
  table** — each cell mines its own tree, and a parse is specific to
  `(tree, sequence)`.

## Testing the web app

Read `docs/miner_performance.md` for the measurement harness, and note three
mechanics that cost hours last round:

- `form_input` does not drive Streamlit selectboxes; click the combobox open,
  then click the `[role="option"]`.
- Never read the DOM mid-render — a partial page once looked exactly like a
  missing sidebar and sent me diagnosing a regression that did not exist.
- **Log to a file, and read the server log.** A rerun clears the UI, so a
  Streamlit exception that aborts a rerun is invisible to UI-based
  instrumentation. Three fix attempts missed the real cause for that reason;
  the server traceback found it in one pass.

A synthetic log with 2,700 variants over 80 activities (~15k events) trips
both screening thresholds and cycles in seconds, where the real logs take ten
minutes a round. Build one rather than testing against a confidential log.

## Confidential logs

Several logs used in the measurements are confidential and must not enter
the repo. If one is needed for a UI test, the sample picker reads
`web/samples/` with no path override, so: add `web/samples/_tmp_*` to
`.git/info/exclude` **before** copying anything in — that file is local and
untracked, so no `git add -A` from a parallel session can stage it — then
delete both the file and the rule afterwards and confirm with `git status`.
