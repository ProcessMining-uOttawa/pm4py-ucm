# pm4py-ucm web front-end

A [Streamlit](https://streamlit.io) front-end for the `pm4py-ucm` package.
Upload an event log (XES or CSV) and, from a left-rail workspace, mine and
render **UCM models** (UCM or BPMN notation, PNG and navigable **SVG**),
synthesize executable **scenarios**, mine attribute **model families**,
**compare** family members, and build interactive **dashboards** —
downloading `.jucm`, CSV, SVG/PNG, and self-contained HTML at every step.

Since v0.6.0 the deployed app is **V4** (the workspace shell + Dashboards),
a strict superset of the earlier four-tab V3 app.

## Run locally

Requires Python 3.9+ and the [Graphviz](https://graphviz.org/download/) `dot`
binary on `PATH` (the layouter shells out to it).

From the repo root:

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS/Linux

pip install -r web/requirements.txt
streamlit run web/streamlit_app_v4.py     # V4 — the deployed app
# streamlit run web/streamlit_app_v3.py   # V3 — the earlier four-tab app
```

Streamlit opens `http://localhost:8501`.

The web-specific deps (`streamlit`, `pm4py`, `scikit-learn`) live in
`web/requirements.txt` so they stay out of the package's own dev workflow
(`pip install -e ".[dev]"`). `scikit-learn` is only needed by the
data-driven scenario condition mining (the option greys out when it is
missing).

## The app and its views

**`streamlit_app_v4.py`** (**V4**) is served at
https://pm4py-ucm.streamlit.app/ via the `streamlit_app.py` shim (that
deployment's main file). A left rail switches between five views over the
loaded log:

- **Model** — mine a UCM, preview it in UCM or BPMN notation as a zoom /
  pan **SVG** (click a stub to jump to its plug-in map), and download the
  SVG, a raster PNG, or the `.jucm`. Decomposition is honoured across all
  maps.

  ![Model view](PM4Py-UCM-Model.png)

- **Scenarios** — concurrency-aware variant clustering + one executable
  jUCMNav `ScenarioDef` per variant, with downloads for the `.jucm`
  (carrying the `<scenarioGroups>`), `variants.csv`,
  `case_variant_map.csv`, and (data-driven mode only)
  `condition_mining.csv`. Both variant-driven and data-driven OR-fork
  encodings are exposed.

  ![Scenarios view](PM4Py-UCM-Scenarios.png)

- **Family** — partition the log by 1–2 case attributes, mine one model
  per combination, and download the per-cell zip / combined `.jucm` /
  dynamic-stub umbrella `.jucm` / grid (SVG or PNG) / interactive HTML
  statistics report.

  ![Family view](PM4Py-UCM-Family.png)

- **Compare** — rank the family members on heat-mapped statistics tables
  (including total case durations) and compare any two side by side:
  models (SVG), per-activity/edge deltas, aligned OR-fork branch shares.

  ![Compare view](PM4Py-UCM-Compare.png)

- **Dashboards** — build widgets from a metric catalog (KPIs, segmented
  bars and tables), narrow them with filters, set targets and a
  scorecard, write custom metrics in the **ƒ formula language**, and
  export a self-contained interactive HTML dashboard (or a multi-section
  session report).

  ![Dashboards view](PM4Py-UCM-Dashboard.png)

The earlier four-tab **V3** app (`streamlit_app_v3.py`) is a strict subset
of V4 and stays in git history. **`streamlit_app_v2.py` is the FROZEN V2
app** (model + scenarios) — served at
https://pm4py-ucm-scenarios.streamlit.app/ for a paper under review; do
not modernise it. **V1** (model-only) was retired at v0.5.1.

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

The sidebar settings above (notation, miner, decomposition, performers)
apply to every view. Pick a view in the left rail:

### 4 · Model

Once mining completes, the **Model** view shows a metrics row (file name,
notation, decomposition mode, maps, nodes) and the diagram as a **vector
SVG** in a zoom / pan viewer — scroll to zoom, drag to pan, and for a
decomposed model **click a stub** to jump to its plug-in map (a dynamic
stub opens a picker of its preconditioned plug-ins). Downloads:

- **Download SVG** — the vector render (crisp at any zoom, text
  selectable).
- **Prepare PNG…** → **Download PNG** — a raster render, generated on
  demand.
- **Download .jucm** — the model in jUCMNav's native XMI format, ready to
  open in [jUCMNav](https://github.com/JUCMNAV/jUCMNavPlus).
- **Pin to dashboard** — adds the live model as a widget in the Dashboards
  view.

### 5 · Scenarios

Choose a **condition strategy** — *variant-driven* (lossless; every OR-fork
guarded by `variant_id == v_i`) or *data-driven* (a decision tree per
outside-loop fork turns case attributes into a business-readable rule;
needs `scikit-learn`) — and a scenario-group name. The view reports
headline metrics (variant count, sequence variants, compression, fitness,
and per-fork condition-mining accuracy in data-driven mode) and offers the
`.jucm` (with the synthesized `<scenarioGroups>`), `variants.csv`,
`case_variant_map.csv`, and (data-driven) `condition_mining.csv`. Works on
flat and decomposed models alike.

### 6 · Family

Pick **1–2 case attributes**; a **coverage heatmap** previews the cell
sizes *before* mining, with per-value filters, a `min_cases` floor, and
quantile `bins` for numeric attributes. Mine to get one model per
combination, shown as a **2-D SVG grid** (rows × columns). Downloads: the
per-cell `.zip`, the combined `.jucm` (shared definitions), the dynamic-
stub **umbrella** `.jucm` (one plug-in per variation point, with
executable strategies), the grid **SVG or PNG**, and the self-contained
**interactive HTML statistics report**.

### 7 · Compare

Rank the family members on a heat-mapped statistics table (per-cell cases,
durations incl. **total**, events/case, variants, rework), then pick any
two and see them **side by side**: SVG models, delta cards, per-activity
and per-edge Δ tables, and aligned OR-fork branch shares.

### 8 · Dashboards

An embedded, self-contained dashboard island over the log's per-case
**fact table**. Add **widgets** from the metric catalog (KPIs, one-axis
bars, two-axis tables), choose an **aggregation**, add per-widget or
dashboard-level **filters** and **segmentation** axes, set **targets** and
watch the **scorecard**, and write custom metrics in the **ƒ formula
language** (`duration() where attr("Claim_Value") > 500`, … — full grammar
below). **Export**
the whole dashboard as one self-contained interactive HTML file, or a
multi-section **session report** (scorecard + dashboards + the model as
SVG + a Family section). **Save** the dashboard's definition (its widgets
and filters) as a small reusable JSON file and **Load** it back later or
onto another log — widgets that name activities or attributes the target
log lacks are reported up front rather than shown as misleading zeros. The
exact metric definitions and the engine's rounding / weighting decisions
are documented in [`docs/dashboards.md`](../docs/dashboards.md); a runnable
walkthrough is
[`demo/dashboards_tutorial.ipynb`](../demo/dashboards_tutorial.ipynb).

#### The ƒ custom-formula language

When the catalog doesn't name what you want to measure, write it as a
**custom formula**: a per-case expression, optionally narrowed by a
`where` clause, that the dashboard then aggregates (avg / median / share /
…) exactly like any built-in metric. Choose **ƒ Custom formula…** in the
widget composer's metric list; a live chip validates as you type and shows
the inferred result type, and function chips insert the calls below.

**Functions** — each returns one number per case:

| Function | Returns |
| --- | --- |
| `duration()` | case length, in days |
| `contains("act")` | `1` if the case contains activity *act*, else `0` |
| `count("act")` | how many times *act* occurs in the case |
| `time_between("a", "b")` | days from the first *a* to the first *b* after it; null if that never happens |
| `timestamp("act")` | epoch seconds of the first *act*; null if absent |
| `attr("name")` | a **numeric** case attribute; null if absent or non-numeric |

**Operators**, from lowest to highest precedence: the trailing `where` <
`or` < `and` < `not` < comparisons (`==` `!=` `>` `>=` `<` `<=`) < `+` `-`
< `*` `/` < unary `-`. Group with `(` … `)`. A comparison or a logical
yields `1` / `0`, so it reads as a percentage after aggregation.

**`where`** — a trailing `where <predicate>` keeps only the cases the
predicate holds for and sets the rest to null (which aggregation drops), so
`duration() where contains("Appeal")` measures duration over appealed
cases only.

**Grammar** (EBNF):

```ebnf
formula    := orExpr [ "where" orExpr ]
orExpr     := andExpr ( "or" andExpr )*
andExpr    := notExpr ( "and" notExpr )*
notExpr    := "not" notExpr | comparison
comparison := additive ( ("=="|"!="|">"|">="|"<"|"<=") additive )?
additive   := term ( ("+"|"-") term )*
term       := unary ( ("*"|"/") unary )*
unary      := "-" unary | primary
primary    := number | call | "(" orExpr ")"
call       := IDENT "(" [ arg ( "," arg )* ] ")"
```

**Examples**:

| Formula | Measures |
| --- | --- |
| `duration() where attr("Claim_Value") > 500` | case duration, over claims above 500 |
| `contains("Appeal")` | share of cases that were appealed → *percent* |
| `count("Rework") / count("Assess")` | rework ratio per case |
| `time_between("Register", "Pay") where attr("Claim_Value") > 1000` | register → pay days, high-value claims only |

**One value type, and no `eval`.** Every expression evaluates, per case, to
a **number or null** — there are no strings at runtime; a string only ever
*names* an activity or attribute inside a call, fixed when the formula is
written. The text is parsed to an explicit syntax tree and never handed to
a language `eval`, so the only operations are the ones above, over the only
data the fact table holds — nothing you type can reach the host. `null` (a
missing time, an absent attribute) propagates through arithmetic and
comparison and is dropped by aggregation. Categorical attributes aren't
values here — filter them with the widget's **Filter** row; that is what
keeps the grammar single-typed and identical between the Python evaluator
and the in-browser one.

**Result type** is inferred from the expression and picks the unit and the
aggregations offered: a comparison, a logical, or a bare `contains()` is a
0/1 indicator → **percent** (aggregate with *share*); anything mentioning a
time function → **time**; otherwise → **count** — exactly as a catalog
metric's type does.

The canonical specification is the module docstring of
[`formula.py`](../pm4py_ucm/algo/dashboards/formula.py); see
[`docs/dashboards.md`](../docs/dashboards.md) for how it fits the rest of
the engine.

## Deploy to Streamlit Community Cloud

1. Push this repo to GitHub.
2. At <https://share.streamlit.io>, "New app" → pick the repo and branch.
3. Set **Main file path** to `web/streamlit_app_v4.py` for the latest
   app (or `web/streamlit_app_v2.py` for the frozen V2 scenarios
   app). Streamlit Cloud picks up `web/requirements.txt`
   automatically (sits next to the main file). The existing primary
   deployment points at `web/streamlit_app.py` — a shim that runs V4,
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
