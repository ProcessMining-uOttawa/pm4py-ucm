---
name: release
description: Cut a pm4py-ucm release end to end — check readiness, bump the three version sites, date the changelog, open and merge the PR, tag, publish to PyPI, and refresh HANDOFF. Use when asked to cut, ship, or publish a release, or to prepare one.
---

# Cutting a pm4py-ucm release

Perform the steps. Stop at the two gates that need a human: **the `pypi`
environment approval**, and **anything a check reports**. Never work around a
failing check — the checks exist because each of these went wrong once.

The mechanical facts are guarded by tests, not by this file. If a guard and
this document disagree, the guard is right and this document is stale.

## 1. Decide the number, and say so

Patch for a fix, minor for a feature. If the release contains a **behaviour
change**, say so plainly to the maintainer before cutting, and if it still goes
out at patch level, put an upgrade note at the very top of the changelog
section naming what changed and how to opt out. 0.8.1 shipped this way: the
validation gates began raising where they used to return.

## 2. Readiness

```bash
python tools/release_check.py <version>
```

It reports the version sites, the changelog state, the branch and tree, whether
the tag is free (**by version order** — a lexical sort puts `v0.7.10` before
`v0.7.5` and will tell you a taken tag is free), and whether
`.github/workflows/publish.yml` is present at HEAD. It changes nothing.

Then the suite, which carries the drift guards:

```bash
python -m pytest tests/ -q
```

`tests/test_release_consistency.py` is the one that matters here: it fails if a
superseded app is still called deployed, if a retired deployment is linked, if
a local link is broken, or if the app the shim runs is not the one the READMEs
name. Fix what it reports — do not skip it.

## 3. Bump, on a release branch

`git checkout -b release/<version>`, then **three** sites, kept in lock-step by
a test:

- `pyproject.toml` → `version`
- `pm4py_ucm/__init__.py` → `__version__`
- `web/sessions/codegen.py` → `GENERATOR_VERSION`

Retitle `## [Unreleased]` to `## [<version>] — <date>` and open a fresh empty
`## [Unreleased]` above it.

## 4. What the release also has to keep true

The guards cover the mechanical claims. These need judgement, so check them by
hand and only touch what the release actually changed:

- **HANDOFF.md** — rewrite the state-on-arrival block and what the release
  added. Carry forward any trap the release taught; that file is where the next
  session starts.
- **The READMEs** (root, `web/`, `demo/`, `tests/`) — only if the release
  changed what they describe. The guards catch a *wrong* claim, not a *missing*
  one.
- **Save/resume** — a new session parameter needs a `Param` in
  `web/sessions/registry.py`, a gather and a restore in the app, and a mention
  in `docs/sessions.md` if it is worth an example. Tests enforce the first
  three; §5 of that document is an example list, not a catalogue, so not every
  parameter belongs there.
- **The notebooks in `demo/`** — only when a release changes an API they
  demonstrate. Version numbers inside them are usually historical ("As of
  pm4py-ucm 0.3.1"), and are correct as they stand.
- **Screenshots in `web/*.png`** — the maintainer captures these by hand. If
  the release changed the UI visibly, *say so and ask*; do not generate them.

## 5. PR, and merge

Open the PR, wait for CI on 3.9–3.12, merge. Two notes:

- The `pull_request` trigger has been running minutes late on this repo. If no
  run appears, `gh workflow run tests.yml --ref <branch>` dispatches one.
- Two PRs that both edit `CHANGELOG.md` under `[Unreleased]` **conflict**,
  however unrelated their code. Merge one, then rebase the other.

## 6. Tag and publish

```bash
gh release create v<version> --target main --title "..." --notes-file -
```

`--target main`, not a sha. Write notes that lead with anything a reader must
know before upgrading, then what changed.

Creating the release triggers `publish.yml`. The build job runs, then the
upload **waits at the `pypi` environment gate** — a required-reviewer
checkpoint.

> **Gate.** Approving it publishes to PyPI, which is public and effectively
> irreversible. Only approve when the maintainer has asked for the release; if
> there is any doubt, stop and say the run is waiting.

## 7. Verify it is actually live

```bash
pip index versions pm4py-ucm
pip install "pm4py-ucm==<version>" --dry-run --ignore-installed --no-cache-dir
```

**`--no-cache-dir` matters**, and expect a lag: the upload succeeds and the
simple index shows the files while pip's resolver still reports "no matching
distribution" for a minute or two. A stale local index has produced that error
for a release that was already live — twice. Poll until it resolves rather than
reporting the release on the strength of the upload alone.

## 8. Afterwards

Streamlit Community Cloud redeploys from `main` on its own; the maintainer
handles deployments and does not want reminders. Delete the merged branches.
