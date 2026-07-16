# pm4py-ucm web front-end

A [Streamlit](https://streamlit.io) front-end for the `pm4py-ucm` package.
Upload an event log (XES or CSV), tune the inductive miner / decomposition /
performer settings interactively, and download the resulting `.jucm` model
plus a high-quality PNG rendering in either UCM (Z.151 / jUCMNav) or BPMN
notation.

## Run locally

Requires Python 3.9+ and the [Graphviz](https://graphviz.org/download/) `dot`
binary on `PATH` (the layouter shells out to it).

From the repo root:

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS/Linux

pip install -r web/requirements.txt
streamlit run web/streamlit_app_v3.py
```

Streamlit opens `http://localhost:8501`.

The web-specific deps (`streamlit`, `pm4py`, `scikit-learn`) live in
`web/requirements.txt` so they stay out of the package's own dev workflow
(`pip install -e ".[dev]"`). `scikit-learn` is only needed by the
data-driven scenario condition mining (the option greys out when it is
missing).

## The V3 app

**`streamlit_app_v3.py`** — served at
https://pm4py-ucm.streamlit.app/ (via the `streamlit_app.py` shim,
that deployment's main file). Four tabs:

- **Model** — mine a UCM from a log, preview in UCM or BPMN notation,
  open the image in its own browser tab (button or double-click),
  download PNG + `.jucm`. Decomposition is honoured across all maps.

  ![Model tab](PM4Py-UCM-Model.png)

- **Scenarios** — concurrency-aware variant clustering + one
  executable jUCMNav `ScenarioDef` per variant, with downloads for the
  `.jucm` (carrying the `<scenarioGroups>`), `variants.csv`,
  `case_variant_map.csv`, and (data-driven mode only)
  `condition_mining.csv`. Both variant-driven and data-driven OR-fork
  encodings are exposed.

  ![Scenarios tab](PM4Py-UCM-Scenarios.png)

- **Family** — partition the log by 1–2 case attributes, mine one
  model per combination, download the per-cell zip / combined `.jucm`
  / dynamic-stub umbrella `.jucm` / grid PNG / interactive HTML
  statistics report.

  ![Family tab](PM4Py-UCM-Family.png)

- **Compare** — rank the family members on heat-mapped statistics
  tables — including total case durations — and compare any two side
  by side: models, per-activity/edge deltas, aligned OR-fork branch
  shares.

  ![Compare tab](PM4Py-UCM-Compare.png)

**`streamlit_app_v2.py` is the FROZEN V2 app** (model + scenarios,
the state before the model-family features) — served at
https://pm4py-ucm-scenarios.streamlit.app/ for a paper under review;
do not modernise it. **V1** (the model-only app that
`streamlit_app.py` used to contain) was retired at v0.5.1 and lives
in git history; its path is now a shim that runs V3.

## The V4 app (preview)

**`streamlit_app_v4.py`** reworks the same capability behind a left-rail
workspace and adds a fifth view — **Dashboards**: build widgets from a
metric catalog over your log (KPIs, segmented bars and tables), narrow
them with filters, set targets and a scorecard, write custom metrics in a
**ƒ formula language**, and export the whole thing as one self-contained
interactive HTML dashboard (or a multi-section session report). Not yet
the deployed default; run it locally with
`streamlit run web/streamlit_app_v4.py`.

![Dashboards view](PM4Py-UCM-Dashboard.png)

## Using the app

### 1 · Pick or upload a log

Two tabs at the top of the page:

- **Sample log** — pick one of the bundled XES logs and click **Load
  sample**. This is the quickest way to try the tool when you don't
  have an event log handy. Extending the list is just dropping more
  `.xes` / `.xes.gz` / `.zip` files into [`web/samples/`](samples) —
  the selector picks them up automatically on next deploy / restart.
- **Upload your own** — the file uploader accepts:
  - **`.xes`** / **`.xes.gz`** — standard XES files are mined directly.
  - **`.zip`** — searched for the first `.xes` / `.xes.gz` entry inside.
  - **`.csv`** — a column-mapping section appears after upload (see step 2).
  - Upload cap is **1 GB when running locally** and **75 MB on the
    public Community Cloud deployment**. The server layer allows 1 GB
    everywhere (`.streamlit/config.toml`); the app enforces the tighter
    75 MB when it detects it's running on Community Cloud (via the
    `HOSTNAME` / `/mount/src` fingerprints). Self-hosted deployments
    (Docker on your own VM, an on-prem server, …) get the local 1 GB
    ceiling by default.

Once a log is chosen (from a sample or an upload) its bytes are cached
for the rest of the session. Changing notation / decomposition /
performer settings re-uses the same log, so you never have to re-pick
or re-upload to try a different option.

### 2 · CSV column mapping (CSV uploads only)

When the uploaded file is CSV, five dropdowns appear under **CSV columns**:

| Field | Required? | Notes |
|---|---|---|
| Case id column | ✓ | Trace identifier |
| Activity column | ✓ | Event name |
| Timestamp column | ✓ | Parsed with pandas/`pm4py.format_dataframe` |
| Role column (optional) | — | Renamed to `org:role` for performer mining |
| Resource column (optional) | — | Renamed to `org:resource` for performer mining |

Sensible defaults are auto-detected from common column names
(`case:concept:name`, `concept:name`, `time:timestamp`, `org:role`, `Resource`,
`Assignee`, …). Review them, adjust if needed, then click **Apply column
mapping** to start mining. Mining is *blocked* until this button is clicked
on a fresh upload, so the miner never runs against a wrongly-guessed mapping.

Changing the dropdowns after the initial Apply shows a yellow banner and a
re-apply button — but mining keeps using the last-applied mapping in the
meantime, so other sidebar settings remain responsive.

### 3 · Sidebar — Options

#### Notation

- **UCM** (default) — Z.151 / jUCMNav notation. Filled circle for the start
  point, perpendicular bar for the end point, small black square + name for
  each responsibility, synchronisation bars for AND-forks/joins, dots for
  OR-forks/joins, diamond reserved for stubs.
- **BPMN** — Activity boxes for responsibilities, gateway diamonds with
  `X` / `+` markers, BPMN-canonical start / end events.

Switching notation re-renders the PNG but does **not** re-run the miner.

#### Inductive miner — Noise threshold

The IMf (*Inductive Miner — infrequent*) threshold. `0.0` keeps every observed
behaviour (classic Inductive Miner, perfect fitness, often noisy diagrams);
higher values filter out increasingly rare arcs and activities, producing
smaller and more abstract models. **0.2 is the default and a sensible
practical starting point**; useful range is roughly 0.1 – 0.4.

#### Decomposition

- **off** (default) — single flat map.
- **auto** — split into a root map plus plug-in maps when the model is large
  enough to benefit.
- **aggressive** — same boundary rules with a tighter cap, producing more /
  smaller plug-ins.

Switching the preset reveals an **Advanced** expander with six override
knobs (`on_root_sequence`, `on_parallel`, `on_loop`, `max_leaves_per_map`,
`min_leaves_to_decompose`, `balance_ratio`). The widget values are seeded
from the preset; tweak as many as you like and click **Apply changes** to
remine — single click, single remine. See the main `README.md`
[Hierarchical decomposition](../README.md#hierarchical-decomposition)
section for what each key does.

#### Performers

- **Resource attribute** — pick from `org:role` (default), `org:resource`,
  `Other…` (a custom event-attribute name, or a comma-separated fallback
  list like `org:role, org:resource, org:group`), or `(none)` to skip
  performer mining entirely.
- **Min support** — minimum fraction of events for an activity that must
  agree on a performer before the binding is kept. `0.0` (default) accepts
  the modal performer even when the resource pool is highly dispersed;
  raise this if you want only majority-owned activities to be bound.
  Disabled when performer mining is off.

### 4 · Results

Once mining completes, the page shows:

- A **metrics row** — file name, notation, decomposition mode, number of
  maps, number of nodes.
- The **rendered diagram** as an inline `<img>` element. The browser
  scales the preview to fit the column width; right-click → **Open image
  in new tab** to see the file at native resolution, or scroll-zoom the
  page to magnify in place.
- Two **download buttons**:
  - **Download PNG** — the rendered image at full resolution.
  - **Download .jucm** — the model in jUCMNav's native XMI format,
    ready to open in [jUCMNav](https://github.com/JUCMNAV/projetseg-update).

## Deploy to Streamlit Community Cloud

1. Push this repo to GitHub.
2. At <https://share.streamlit.io>, "New app" → pick the repo and branch.
3. Set **Main file path** to `web/streamlit_app_v3.py` for the latest
   app (or `web/streamlit_app_v2.py` for the frozen V2 scenarios
   app). Streamlit Cloud picks up `web/requirements.txt`
   automatically (sits next to the main file). The existing primary
   deployment points at `web/streamlit_app.py` — a shim that runs V3,
   so it keeps working without a settings change.
4. **`packages.txt` (apt packages) MUST be at the repo root** — Streamlit
   Cloud's apt-install phase only reads from the root, not from the main
   file's directory. The root `packages.txt` in this repo installs the
   `graphviz` apt package, which provides the `dot` binary the layouter
   shells out to.
5. Deploy.

Updates are a `git push` to the tracked branch.

## Adding more sample logs

The **Sample log** tab is populated by scanning
[`web/samples/`](samples) for `.xes`, `.xes.gz`, and `.zip` files.
Drop a new file in there and it shows up on the next app start — no
code change. Display names are derived from the file name (underscores
become spaces, archive/compression suffixes stripped).

## Robustness & security notes

The app is designed for a low-traffic public demo (Streamlit Community
Cloud or similar). A few hardening points worth knowing:

- Upload cap is **two-tier**. The Streamlit server layer
  ([`.streamlit/config.toml`](../.streamlit/config.toml)) allows
  **1 GB** everywhere, so the file uploader itself never blocks a
  local run. The app then re-checks the payload size and rejects
  anything above **75 MB** *only when it detects it is running on
  Streamlit Community Cloud* (via the `HOSTNAME` / `/mount/src`
  fingerprints). Result: 1 GB locally, 75 MB on the public demo,
  self-hosted deployments default to the local ceiling.
- The Pillow decompression-bomb guard is raised to **1 billion pixels**
  rather than disabled. Realistic mined UCMs are far below that.
- CSV reading uses `low_memory=False` so columns with mid-file dtype
  changes don't trigger `DtypeWarning` or downstream type confusion.
- ZIP archives (sample logs and user uploads) are scanned defensively —
  entries with `..` or absolute paths are skipped to prevent
  zip-slip-style path traversal.
- Downloaded file names are sanitised (only `[A-Za-z0-9._-]` survives).
- Mining failures show a one-line error inline plus an expandable
  technical-details panel — no raw Python traceback is displayed by
  default to casual visitors.
- Large logs (e.g. 100k+ events) can take a couple of minutes in the
  inductive miner; a multi-phase status panel reports the current step
  so the page never looks hung. The mining function is wrapped in
  `st.cache_data` keyed on the log bytes + settings, so toggling
  notation / re-mining identical inputs is instant.

If you deploy publicly and care deeply about XML safety, also review
PM4Py's XES parser — it relies on `lxml` under the hood; XXE / billion-
laughs hardening upstream is the right place for those concerns rather
than this Streamlit layer.

## Layout

```
packages.txt                   # apt: graphviz  (Streamlit Cloud reads this at repo root)
.streamlit/
  config.toml                  # upload-size cap, telemetry off
web/
  streamlit_app.py             # entry point
  requirements.txt             # streamlit + pm4py + `-e .` (the package itself)
  PM4Py-UCM-Model.png          # per-feature screenshots used in the READMEs
  PM4Py-UCM-Scenarios.png
  PM4Py-UCM-Family.png
  PM4Py-UCM-Compare.png
  PM4Py-UCM-Dashboard.png
  README.md                    # this file
  samples/
    *.zip / *.xes / *.xes.gz   # bundled sample logs; auto-listed in the UI
```

`-e .` is a relative path resolved against pip's working directory, which is
the repo root both locally (`pip install -r web/requirements.txt` run from
the repo root) and on Streamlit Cloud. Either way it points at the
`pm4py-ucm` project, so any change to the library is picked up on the next
Streamlit rerun (locally) or next deploy (on the Cloud).
