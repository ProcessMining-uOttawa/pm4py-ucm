# Dashboards (the semantic contract)

This document specifies the user-defined dashboard feature: the data
contract shipped to the browser
([`algo/dashboards/contract.py`](../pm4py_ucm/algo/dashboards/contract.py)),
the metric catalog
([`algo/dashboards/catalog.py`](../pm4py_ucm/algo/dashboards/catalog.py)),
and the computation engine
([`algo/dashboards/engine.py`](../pm4py_ucm/algo/dashboards/engine.py)).

Like [`metrics.md`](metrics.md), it is a *contract*: every rule below is
pinned by a test in
[`tests/test_dashboards.py`](../tests/test_dashboards.py) with
hand-computed numbers. Changing a definition here changes a test — on
purpose.

---

## 1. Architecture: one engine, two implementations

Dashboards compute **in the browser**. Filtering, segmentation,
aggregation and target scoring all run client-side over a compact
snapshot of the log.

That is not an optimisation; it is what makes the exported HTML report
work. A reader opening the export offline can change the filter bar and
watch every widget, the targets strip and the scorecard recompute — with
no server — because the export carries the engine and the same fact table
the app used.

**The app's Dashboards view and the exported report are the same
artifact**, built by
[`view.dashboard_html`](../pm4py_ucm/algo/dashboards/view.py). The app
embeds it in a `streamlit.components.v1.html` iframe; the export writes
it to a file. The two outputs are byte-identical apart from the
`readOnly` flag and the storage key — this is checked, not asserted by
hand.

The engine therefore exists twice:

| | |
|---|---|
| [`algo/dashboards/engine.py`](../pm4py_ucm/algo/dashboards/engine.py) | the reference. Computes exports and previews server-side, and is what the test suite pins. |
| [`assets/dash-engine.js`](../pm4py_ucm/algo/dashboards/assets/dash-engine.js) | the browser half. Must return what the Python half returns. |

Rendering exists **once**, in
[`assets/dash-ui.js`](../pm4py_ucm/algo/dashboards/assets/dash-ui.js).
The engine returns *data* — values, states, labels, counts — and the UI
turns it into pixels. A widget looks the same in the app and the export
because they read one computation through one renderer.

The assets live inside the package, not under `web/`: the export is built
by `pm4py_ucm` and has to be self-contained wherever it is installed.
`MANIFEST.in` ships them.

Two implementations of one set of semantics will drift unless something
holds them together. `TestJsParity` is that something: it runs both
engines over every metric, every visualisation, both target modes and
stacked filters, and compares the full widget structure — values, states,
labels, counts and formatted text.

**If you change one engine, change the other.** The parity test will tell
you if you forgot.

---

## 2. The fact table

The payload is **columnar**: activity names and resources are
dictionary-encoded to integer codes, event timestamps are delta-encoded
against their case's start, and per-case event lists are flattened into
one array addressed by CSR-style offsets. Buffers are typed
little-endian arrays, base64-encoded; the JS side decodes them straight
into `TypedArray` views.

The obvious encoding (an array of per-case objects) was measured and
rejected:

| log | cases / events | array-of-objects | columnar binary + base64 |
|---|---|---|---|
| ClaimsPaymentLog | 5 600 / 78 k | 2.17 MB / 4.5 s | **0.69 MB / 0.1 s** |
| IssueTracker | 11 284 / 100 k | 2.94 MB / 7.9 s | **0.92 MB / 0.1 s** |

The encoding costs ~6 bytes per event plus ~8 bytes per case before
base64's 4/3 expansion, so a 561 k-event log lands near 6 MB — comparable
to the PNGs the family report already embeds. No sampling is needed for
realistic logs; a byte budget (`max_payload_bytes`, default 24 MB) falls
back to a deterministic case sample beyond it and records the true
population in `sampled_from`, which the UI must surface.

### Why a fact table rather than pre-aggregated series

A widget's value depends on the dashboard filter, the widget filter and
the segmentation axes — all of which the reader can change *after*
export. Pre-aggregating would freeze those choices. Per-case facts keep
every catalog metric computable in the reader, including
`time_between(a, b)` for activity pairs nobody picked at export time.

### Time resolution

Timestamps are epoch **seconds, rounded to nearest**. Real logs carry
sub-second components (ClaimsPaymentLog has milliseconds), so a duration
here can differ from the raw log by up to **1 second** across its two
endpoints.

This is deliberate: it keeps the delta buffers in `uint32`, halving the
payload against `float64`, and it is orders of magnitude below the
resolution of any metric in the catalog — durations are reported in days
to one decimal, where a second is 1/8640th of the last displayed digit.

The timestamp *unit* is read from the dtype, never presumed:
`pm4py.read_xes` yields `datetime64[ns]`, pandas 3 infers
`datetime64[us]` for constructed timestamps, and a CSV parse can yield
either. Assuming one unit silently scales every duration by a power of
ten.

### Dropped events

Events whose timestamp cannot be parsed are **dropped**, and the count is
kept in `dropped_events`. They cannot be kept: an unparseable timestamp
has no position in a case's event order, and its `NaT` sentinel would
reach the delta arithmetic and silently corrupt that case's start rather
than fail.

---

## 3. The computation model

Every metric is **per case, then aggregated**. A widget computes one
value per case, filters cases, groups them by the segmentation axes, and
aggregates within each group.

That model is what makes reader-side filtering possible at all: a filter
is a case mask, and every number on the dashboard falls out of
re-aggregating the surviving cases.

### Missing is not zero

A per-case value of `NaN` means *this case has no value for this metric*
— the `to` activity never followed the `from`, the activity never
occurred, the log has no start timestamps. **Missing cases leave the
denominator; they are never counted as zero.** A case that never appealed
has no appeal-to-decision time, and averaging a zero in would fabricate a
number.

`share` is the exception by construction: it counts non-missing cases and
asks what fraction are non-zero.

The distinction is per metric, and it matters:

* `actPresence` — absence **is** the answer, so a case that skips the
  activity scores 0 and stays in the denominator;
* `actSojourn` — a case that skips the activity has no sojourn time for
  it, so it is missing;
* `edgeShare` — cases that never reach the source activity are not in the
  denominator at all, so the branch probability is *among cases that
  reached the fork*.

### Ordering is by event index, never by timestamp

The fact table stores whole seconds, so two genuinely ordered events can
share a timestamp. Deciding order by comparing timestamps would read a
real sub-second transition as "did not happen" and drop the case from the
metric entirely.

This is not hypothetical: on ClaimsPaymentLog, `Approve Assessment →
Verify Claim` is sub-second in 34 cases. Index order is exact, because
`build_fact_table` sorts events by time.

### Case-weighted vs event-weighted

The activity time metrics here are **case-weighted**: a case that ran
`Send Fine` three times contributes the mean of its three service times,
once. The performance **overlays** on the model
([`algo/performance.py`](../pm4py_ucm/algo/performance.py)) are
**event-weighted**: that same case contributes three times.

The two agree whenever an activity occurs at most once per case, and
diverge on rework. Neither is wrong — they answer different questions
("how long does this activity take *for a case*" vs "how long does an
execution of this activity take"). The composer surfaces this on the
affected metrics.

---

## 4. Metric catalog

| id | level | result | definition |
|---|---|---|---|
| `duration` | process | time | first to last event of the case. |
| `timeBetween` | process | time | first `from` to the first `to` **that follows it**. No value when `to` never follows `from`. |
| `wip` | process | rate | cases open at each month boundary (a stock). |
| `arrivalRate` / `completionRate` | process | rate | cases starting / ending within each month (flows). |
| `eventCount` | process | count | events in the case. |
| `rework` | process | percent | 1 when any activity occurs more than once in the case. |
| `actFreq` | activity | count | occurrences of the activity within the case. |
| `actPresence` | activity | percent | 1 when the case contains the activity. |
| `actRepeats` | activity | count | occurrences beyond the first; 0 when it runs once *and* when it never runs. |
| `actSojourn` | activity | time | time since the case's previous event (waiting + service). |
| `actService` | activity | time | start to completion. **Interval logs only.** |
| `actWaiting` | activity | time | previous event's completion to this activity's start. **Interval logs only.** Negative values are kept — they mean real concurrency, not an error. |
| `edgeFreq` | edge | count | directly-follows traversals of `from → to` within the case. |
| `edgeTime` | edge | time | elapsed time across the `from → to` step. |
| `edgeShare` | edge | percent | share of cases taking `from → to` **among those reaching `from`**. |

Interval-only metrics stay **visible** in the composer on a
single-timestamp log, marked unavailable with a reason, rather than
leaving a hole where a user expects service time to be.

### Aggregations

`avg`, `median`, `p90`, `sum`, `min`, `max`, `share`. Which are offered
is driven by the metric's result type, from one definition serialised
into the client payload — not from parallel Python and JS lists.

`median` and `p90` use **linear interpolation** — numpy's and pandas'
default, and the convention [`metrics.md`](metrics.md) pins for the whole
package.

> This deliberately differs from the design prototype's `pm-engine.js`,
> which used nearest-rank (`v[floor(n*0.9)]`). Matching the package
> matters more than matching the prototype: a dashboard median that
> disagreed with the same log's model overlay and family report would be
> a bug report, not a design choice.

### Rounding

Display rounding is **half away from zero** (5.25 → 5.3), implemented
explicitly in both engines.

Neither language's default would do: Python's `round`/`%.1f` round half
to *even* (5.25 → 5.2), JS's `toFixed` rounds half *up*. Left to their
defaults the two engines print different strings for the same value, and
a KPI reading 5.2 in the app and 5.3 in the export is indistinguishable
from a bug.

---

## 5. Segmentation

Axes: `year`, `quarter`, `month`, `weekday`, `resource`, `variant`, and
one per detected case attribute (`attr:<name>`).

* **Weekday is Monday-first.** (Epoch day 0 was a Thursday = index 3.)
* **`resource` is the case's first event's resource.** A case has many
  resources; an axis needs one. Whoever opened the case is the
  deterministic, explicable choice — hence the axis is labelled
  "Resource (first event)". Attributing a case to the resource of the
  *metric's* activity would be more faithful for activity-scoped metrics,
  but would make the axis mean different things in different widgets.
* **Variants are ranked by frequency**, so `v1` is always the most common
  path — and means the same variant in both engines.
* **Binned numeric attributes** use `[lo, hi)` bins with the last closed
  on both ends — the family partitioner's convention, so a dashboard
  segmented by an attribute agrees with a family grid partitioned on it.
* **Empty segments are omitted, not zeroed.** A segment with no surviving
  case is not a zero bar.

Segmentation arity picks the visualisation: 0 axes → KPI, 1 → bar/line,
2 → heatmap table.

---

## 6. Targets

A target is `{on, dir, value, warn, mode}`. `warn` is the threshold
between *at risk* and *missed*, on the far side of `value`: a `<=` target
with value 14 and warn 18 reads "meet 14, tolerate 18, miss beyond".

Two modes:

* **`aggregate`** (default) — score the group's aggregated value: *is our
  average within 14 days?*
* **`per_case`** — score every case, then report the **share that met** as
  the group's value: *are 90% of cases within 14 days?* The distribution
  across met/risk/missed feeds the tri-colour bar. A share needs its own
  threshold to have a state at all, so it is scored against `shareGoal` /
  `shareWarn`.

**Scoring is per segment, and a widget rolls up to its worst segment.** A
heatmap of 40 segments with one breach is a breach; reporting its average
state would hide exactly the cell the reader needs.

---

## 7. Widget specs

The core artifact, serialised as JSON. A dashboard is an ordered list of
these plus a dashboard-level filter list.

```json
{
  "id": "w1", "title": "avg Send Fine → Payment",
  "metric": "timeBetween",
  "params": {"from": "Send Fine", "to": "Payment"},
  "agg": "avg",
  "filter": [{"field": "attr:amount", "op": ">", "value": 100}],
  "segment": {"rows": "resource", "cols": "quarter"},
  "viz": "table",
  "target": {"on": true, "dir": "<=", "value": 14, "warn": 18,
             "mode": "aggregate"}
}
```

Dashboard filters stack **on top of** each widget's own filter, so a
reader-side filter narrows every widget without editing any of them.

Filter fields: `contains` (activity), `date` (case start, inclusive of
the whole final period), `resource`, `attr:<name>`, and `segment` — the
drill-down a table cell click produces.

`compute_widget` returns **data, not styling**: values, states, labels
and counts. The Streamlit view and the exported HTML render from the same
structure, so a widget looks the same in both because they read one
computation, not two.

---

## 8. The view

### Where state lives

In the browser. `components.html` is one-way — it cannot send state back
to Python — and the export has no server at all, so widget specs persist
to `localStorage`, namespaced per log so two logs never share a
dashboard. `window.pmDashboard.exportSpecs()` reads them back out; that
is the only channel out of the iframe.

### Read-only

`read_only=True` is what the export sets. A reader keeps every way of
*interrogating* the dashboard — filters, drill-down, axis swap, CSV,
scorecard — and loses only the ways of *restructuring* it (adding and
removing widgets). Filtering is the point of shipping the engine with the
export, so it is never taken away. A read-only page also leaves the
saved widgets alone, so opening an export cannot clobber a dashboard the
same person built in the app.

### The page exports itself

⬇ Export is handled **in the page**, by `Dashboard.exportHtml()` — not by
the server. Two reasons, one architectural and one practical.

The architectural one: the document already carries the engine, the
renderer and the whole fact table with no external reference. A copy of
it with the config swapped *is* the export. Nothing needs building.

The practical one: `components.html` is one-way, so Python does not know
which widgets the user built — they live in this browser. An export
produced server-side would confidently ship the **default** dashboard
instead of theirs.

The export carries the current widgets *and the current filters*, so a
dashboard sent to answer a specific question opens on that question.

### Light and dark

The handoff specifies only a light palette, so the dark one is derived
rather than invented: paper becomes ink, the surfaces keep their ordering
(rail behind card behind desk), and every value is a token so a theme is
~25 numbers rather than a rewrite.

Two things needed care:

* **Garnet does two jobs.** As an *accent* (text, borders, outline
  buttons) it must lift to `#ff6b81` on dark — uOttawa's `#8f001a` there
  is 1.6:1. As a *fill* behind white text it stays `#8f001a`, because a
  fill carries its own contrast regardless of the page. Hence
  `--garnet` and `--garnet-fill`.
* **The heat ramp is a function, not a swatch**, so CSS tokens cannot
  reach it and both themes are spelled out in `heat(u, dark)`. The dark
  ramp starts just above the card rather than at near-white: a grid of
  pale cells on a dark page is a glare panel, and it would invert the
  meaning by making cold values shout.

The theme resolves as: `data-theme` on the root (the host telling us,
winning in **both** directions — a Streamlit app can be dark on a light
OS) → `prefers-color-scheme` (the standalone export, which has no host to
ask, and which keeps tracking it live).

Surfaces that mount on `<body>` — the toast and the modal scrim — are
outside the dashboard root and inherit no `data-theme`, so they are told
it explicitly. The toast additionally *inverts* the page, so it reads
from `--invert-bg` rather than `--ink`, which flips with the theme.

> The bug this replaced: the Streamlit shell hard-coded the light paper
> background while Streamlit kept its near-white dark-theme text — white
> on white, 1.01:1. Any surface whose text belongs to someone else must
> move with the theme that owns that text.

**Known, unfixed:** the handoff's `--faint: #a09b91` is 2.77:1 on white
and 2.63:1 on paper, below the 4.5:1 AA floor for the 9.5–10.5px mono it
carries. It is the designer's specified token, so it has been left alone
pending a decision. The dark palette's `--faint` was set to `#9a948c`
(5.24:1) rather than inheriting that problem.

### Embedding JS in HTML

The HTML parser does not know JavaScript: it ends a `<script>` at the
first `</script` in the raw text — inside a string, a regex, or a
*comment*. `view._script_body` escapes it to `<\/script`, which is
identical for JS and only text in a comment.

This is not hypothetical. A comment in `dash-ui.js` *explaining this very
escaping* mentioned `</script>` in prose and truncated the bundle at
exactly that word, dumping the engine into the page body. The test that
pins it counts script tags rather than searching for a substring, because
the invariant is "the document is not truncated".

### Inlining

`dash-ui.js` imports `dash-engine.js`, and nothing can resolve that
relative specifier in a `file://` or `srcdoc` page. So the two are
stitched into one module script: the engine is wrapped in an IIFE
returning its exports as the `E` namespace the UI expects, and the UI's
import is dropped. The export list is derived from the source rather than
hand-maintained, and a test checks that every `E.<name>` the UI calls is
actually exported — a mismatch there is a runtime `TypeError` in the
browser that no Python test would otherwise see.

### Layout

CSS grid, 4 columns, 118px rows, `grid-auto-flow: dense`. KPI 1×1;
bar/line/table 2×2; a table with more than five data columns and the
model widget take the full width. Below 900px it folds to 2 columns.
Wide content scrolls inside its own card — the page never scrolls
sideways.

### Honesty in the UI

The header states the live case count against the total, and says so
when the numbers are not the whole truth: a sampled fact table shows
`sample of N`, and dropped events show `N events dropped`. Status-mode
table cells carry `✓ / ! / ✕` as well as a colour, because colour alone
is not a signal every reader can see. An empty segment renders `—`, never
a zero.
