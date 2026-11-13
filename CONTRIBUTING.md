# Contributing to pm4py-ucm

How to set up, what to run before opening a pull request, and the rules the
suite enforces so you meet them on purpose rather than in CI.

## Set up

```bash
git clone https://github.com/ProcessMining-uOttawa/pm4py-ucm.git
cd pm4py-ucm
pip install -e ".[dev]"
```

Python 3.10 or later. The `dev` extra brings pytest, PM4Py, matplotlib,
Bandit and lxml. You also need the **Graphviz `dot` binary** on your `PATH`
(`apt install graphviz`, `brew install graphviz`, or the Windows installer):
the Python `graphviz` package is only a wrapper around it.

To run the web app locally:

```bash
pip install -r web/requirements.txt
streamlit run web/streamlit_app.py
```

## Where things live

| Directory | What it holds |
|---|---|
| `pm4py_ucm/` | The library. `objects/` is the UCM object model with its importers and exporters, `algo/` the discovery, scenario, family, performance and dashboard algorithms, `visualization/` the renderers, `api.py` the public functions. The [README's module layout](README.md#module-layout) has the full tree. |
| `web/` | The Streamlit app ([`web/README.md`](web/README.md)). `streamlit_app.py` is a shim that runs the current version, `streamlit_app_v6.py`; earlier versions are deprecated and receive no changes. |
| `tests/` | The suite ([`tests/README.md`](tests/README.md) maps every file to what it protects). |
| `docs/` | Specifications and design notes ([`docs/README.md`](docs/README.md) indexes them). |
| `demo/` | Tutorial notebooks and two sample logs ([`demo/README.md`](demo/README.md)). |
| `examples/` | Three short scripts: a UCM built by hand, inductive mining, scenario synthesis. |
| `tools/` | `release_check.py` (is the tree ready to release?) and `cache_audit.py` (is the app's caching sound?). |

## Before you open a pull request

```bash
python -m pytest tests/ -q                           # about three minutes
python tests/gen_readme.py                           # if you added or removed tests
python tools/cache_audit.py web/streamlit_app_v6.py  # if you touched the app
bandit -r pm4py_ucm web -ll                          # no medium or high finding
```

CI runs the suite on every Python version the package claims (the matrix in
[`.github/workflows/tests.yml`](.github/workflows/tests.yml)).

Then:

- **Add a changelog entry** under `## [Unreleased]` in
  [`CHANGELOG.md`](CHANGELOG.md), in the *Added* / *Changed* / *Fixed*
  section that fits. Say what a user will notice, and flag any change of
  behaviour as an upgrade note.
- **Add a test that fails without your change.** For a bug fix, check that it
  really does fail first.
- **Update the documentation the change makes wrong**, in the same pull
  request. The table below says where.

## Rules the suite enforces

Each of these exists because it went wrong once.

| If you… | then… | Enforced by |
|---|---|---|
| change a metric's definition | update [`docs/metrics.md`](docs/metrics.md); its hand-computed expectations are the contract | `tests/test_metric_validation.py` |
| change the dashboard engine | change **both** engines: `algo/dashboards/engine.py` and `assets/dash-engine.js` must agree on every widget | `TestJsParity` in `tests/test_dashboards.py` |
| add a setting to the app | follow the checklist in [`docs/sessions.md`](docs/sessions.md) §17 (a stable `key=`, a `Param` in `web/sessions/registry.py`, the gather, the restore), or the setting is lost from saved projects | `tests/test_sessions_registry.py` |
| add a parameter to an `@st.cache_data` function | make sure the hashed parameters determine the result; a `_`-prefixed parameter is left out of the cache key | `tools/cache_audit.py`, run by the suite |
| bump the version | change all three sites together: `pyproject.toml`, `pm4py_ucm/__init__.py`, `GENERATOR_VERSION` in `web/sessions/codegen.py` | `tests/test_release_consistency.py` |
| add, move or rename a document | keep every local link resolving, and list a new document in [`docs/README.md`](docs/README.md) | `tests/test_release_consistency.py` |
| link a repository file from the root `README.md` | use an absolute `https://github.com/…/blob/main/…` URL: PyPI renders that file and cannot follow relative links | `tests/test_release_consistency.py` |
| add or remove a test | regenerate `tests/README.md` with `python tests/gen_readme.py`; never edit it by hand | `python tests/gen_readme.py --check` |

Two rules no test can enforce:

- **Confidential event logs never enter the repository**, including as
  fixtures, notebook outputs or screenshots. Measurements on such logs are
  reported as aggregate counts under neutral labels.
- **The screenshots in `web/*.png` are captured by the maintainer.** If your
  change alters the interface visibly, say so in the pull request.

## Compatibility to preserve

- **`.jucm` files must open in jUCMNav.** The exporter's output is kept
  stable so that a file mined today loads in the editor; see
  [Compatibility with jUCMNav](README.md#compatibility-with-jucmnav).
- **The Python API in `pm4py_ucm.__all__` is public.** Changing the
  behaviour of an existing name needs an upgrade note and at least a minor
  version.
- **Saved projects keep loading.** A change to what a project stores needs a
  migration in `web/sessions/migrate.py`.

## Releasing

Releases are cut by the maintainer. The procedure is written down in
[`.claude/skills/release/SKILL.md`](.claude/skills/release/SKILL.md), and
`python tools/release_check.py <version>` reports whether the tree is ready.

## Licence

By contributing you agree that your contribution is licensed under the
project's licence, [GPL-3.0-or-later](LICENSE).
