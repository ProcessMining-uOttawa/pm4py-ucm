# pm4py-ucm web front-end

A [Streamlit](https://streamlit.io) front-end for the `pm4py-ucm` package.
Upload an event log (XES or CSV), tune the inductive miner / decomposition /
performer settings interactively, and download the resulting `.jucm` model
plus a high-quality PNG rendering in either UCM (Z.151 / jUCMNav) or BPMN
notation.

![Overview of the PM4Py-UCM web interface](WebInterfaceOverview.png)

## Run locally

Requires Python 3.9+ and the [Graphviz](https://graphviz.org/download/) `dot`
binary on `PATH` (the layouter shells out to it).

From the repo root:

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS/Linux

pip install -r web/requirements.txt
streamlit run web/streamlit_app.py
```

Streamlit opens `http://localhost:8501`.

The web-specific deps (`streamlit`, `pm4py`) live in `web/requirements.txt`
so they stay out of the package's own dev workflow (`pip install -e ".[dev]"`).

## Using the app

### 1 · Upload a log

The file uploader at the top of the page accepts:

- **`.xes`** / **`.xes.gz`** — standard XES files are mined directly; the
  inductive miner runs as soon as the file is uploaded.
- **`.csv`** — a column-mapping section appears after upload (see step 2).

Once a file is uploaded its bytes are cached for the rest of the session.
Changing notation / decomposition / performer settings re-uses the same log,
so you never have to re-upload to try a different option.

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
3. Set **Main file path** to `web/streamlit_app.py`. Streamlit Cloud picks up
   `web/requirements.txt` and `web/packages.txt` automatically because they
   sit next to the main file. If a build ever fails to find them, point at
   them explicitly under Advanced settings.
4. Deploy. `packages.txt` installs the `graphviz` apt package, which provides
   the `dot` binary the layouter shells out to.

Updates are a `git push` to the tracked branch.

## Layout

```
web/
  streamlit_app.py             # entry point
  requirements.txt             # streamlit + pm4py + `-e .` (the package itself)
  packages.txt                 # apt: graphviz
  WebInterfaceOverview.png     # screenshot used in this README
  README.md                    # this file
```

`-e .` is a relative path resolved against pip's working directory, which is
the repo root both locally (`pip install -r web/requirements.txt` run from
the repo root) and on Streamlit Cloud. Either way it points at the
`pm4py-ucm` project, so any change to the library is picked up on the next
Streamlit rerun (locally) or next deploy (on the Cloud).
