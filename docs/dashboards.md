# Dashboards (the semantic contract)

This document specifies the user-defined dashboard feature: the data
contract shipped to the browser
([`algo/dashboards/contract.py`](../pm4py_ucm/algo/dashboards/contract.py)),
the metric catalog
([`algo/dashboards/catalog.py`](../pm4py_ucm/algo/dashboards/catalog.py)),
the computation engine
([`algo/dashboards/engine.py`](../pm4py_ucm/algo/dashboards/engine.py))
and the page that carries them
([`algo/dashboards/view.py`](../pm4py_ucm/algo/dashboards/view.py)).
[§10](#10-python-api) lists the Python entry points.

Dashboards *proposed* from the log — which widgets, and why — are specified
separately in [`dashboard_suggestions.md`](dashboard_suggestions.md); they are
ordinary widget specs and add nothing to this contract.

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
embeds it in a sandboxed same-origin iframe (`st.iframe`); the export is
that same document with its config swapped
([§9](#the-page-exports-itself)).

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
base64's 4/3 expansion, so a 561 k-event log lands near 6 MB. No sampling
is needed for realistic logs. A byte budget (`max_payload_bytes`, default
24 MB; `0` disables it) bounds the pathological case: when the estimated
payload exceeds it *and* the log has more than `sample_cases` cases
(default 40 000), that many cases are drawn with a fixed seed — so an
export is reproducible — and the true population is recorded in
`sampled_from`, which the UI must surface.

The buffers, each declaring its own `dtype` so integer widths can widen
without the decoder caring:

| buffer | per | holds |
|---|---|---|
| `off` | case (+1) | CSR offsets: case *i* owns events `[off[i], off[i+1])` |
| `start` | case | the case start, epoch seconds: the earliest start timestamp of its events on an interval log, else its first event's time (see below) |
| `act` | event | index into `activities` |
| `dt` | event | completion time, seconds since the case's `start` |
| `res` | event | index into `resources` (absent when the log has no resource column) |
| `sdt` | event | start timestamp, seconds since the case's `start` (interval logs only) |
| `attr<i>`, `behaviour` | case | one column per case attribute: a dictionary index, or the raw number for a numeric attribute |

Integer columns reserve their dtype's maximum as the *missing* sentinel;
float columns use `NaN`. Both engines read either as "no value". The
payload carries `version` (`CONTRACT_VERSION`), which the JS decoder
checks.

### Case attributes

The attributes offered as filters and axes are the ones the Family view
can partition on — the same detection and the same boolean / integer /
enumeration typing ([`model_families.md` §1](model_families.md#partitioning)).
An enumeration or boolean ships as a dictionary plus per-case codes, in
the log's own spelling. A numeric attribute ships its raw per-case value
plus bin edges, and the client assigns the bins: `bins` quantile bins
(default 4), or — when the column holds fewer than 10 distinct whole
numbers — one bin per value, so a 1..5 rating segments as its five
levels whatever `bins` says.

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
payload against `float64`, and it is at or below the resolution of
anything displayed — a duration of a day or more is shown in days to one
decimal, where a second is 1/8640th of the last digit, and the finest
unit a duration is ever shown in is the whole second.

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

On an interval log, a *start* timestamp that does not parse, or that lies
after its own event's completion, is marked missing rather than clamped
to a zero-length service time that would drag every mean down. The event
is kept; it simply has no service or waiting time.

**When a case starts.** On an interval log a case starts at the
earliest moment any of its events *started*; on a single-timestamp log,
at its first event. An event whose start is missing, unparseable or after
its own completion counts from its completion. This is the definition of
[`pm4py_ucm.util.timing`](../pm4py_ucm/util/timing.py), shared with the
family statistics and the partition advisor ([`metrics.md`](metrics.md)
§4). `duration`, the time axes, the date filter and the series metrics
all read this one case start, and every offset counts from it, so no
valid start is ever negative.

Up to 0.9.0 the case start was the first *completion* on every log. Two
things followed on interval logs: `duration` left out the first
activity's own duration, and a start earlier than that first completion
— the first event of nearly every case — was marked missing, which
removed it from `actService`.

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
* `actRework` — a case that skips the activity has nothing to redo, so
  it is missing: the rate is *among the cases performing the activity*,
  and a rare activity that is always redone reads 100% rather than its
  share of the log;
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
execution of this activity take").

**Decided: the dashboard activity-time metrics stay case-weighted.** One
number per case is the natural unit for a case-level dashboard, and it is
what composes with the per-case targets, segmentation, and scorecard
built on top. The model overlays stay event-weighted, by design. The
divergence is intentional and documented — not a discrepancy to
reconcile — and the composer surfaces it on the affected metrics.

---

## 4. Metric catalog

| id | level | result | definition |
|---|---|---|---|
| `duration` | process | time | earliest start to latest completion of the case. On a log without start timestamps that is its first event to its last. |
| `timeBetween` | process | time | first `from` to the first `to` **that follows it**. No value when `to` never follows `from`. |
| `wip` | process | rate | cases open at each month boundary — started at or before it, last event after it (a stock). |
| `arrivalRate` / `completionRate` | process | rate | cases starting / ending within each month (flows). |
| `eventCount` | process | count | events in the case. |
| `rework` | process | percent | 1 when any activity occurs more than once in the case. |
| `actFreq` | activity | count | occurrences of the activity within the case. |
| `actPresence` | activity | percent | 1 when the case contains the activity. |
| `actRepeats` | activity | count | occurrences beyond the first; 0 when it runs once *and* when it never runs. |
| `actRework` | activity | percent | 1 when the case runs the activity more than once, **among cases that run it at all**. |
| `actSojourn` | activity | time | time since the case's previous event (waiting + service). |
| `actService` | activity | time | start to completion, for every event with a valid start timestamp — a case's first event included. **Interval logs only.** |
| `actWaiting` | activity | time | previous event's completion to this activity's start. **Interval logs only.** Negative values are kept — they mean real concurrency, not an error. |
| `edgeFreq` | edge | count | directly-follows traversals of `from → to` within the case. |
| `edgeTime` | edge | time | elapsed time across the `from → to` step, completion to completion. |
| `edgeShare` | edge | percent | share of cases taking `from → to` **among those reaching `from`**. |

`actRepeats` and `actRework` both describe rework and differ in what they
count and over whom — see [`metrics.md`](metrics.md) §2, which also sets
them against the model overlay's `repeat_frequency`. As a count,
`actRepeats` displays as a whole number, so an average of 0.3 reads "0";
`actRework` is the one to chart.

Result types, which fix the unit and the aggregations offered:

| result | per-case value | unit | aggregations |
|---|---|---|---|
| `time` | a duration in **days** | `d` | `avg` `median` `p90` `sum` `min` `max` |
| `count` | a cardinality | `n` | `avg` `median` `p90` `sum` `min` `max` |
| `percent` | a 0/1 indicator | `%` | `share` only — any other `agg` in a spec is overridden |
| `rate` | none: a monthly series | `n` | `avg` `median` `p90` `max`, over the months |

The three `rate` metrics are **series metrics**. They are not per-case
values but one point per calendar month from the first case start to the
last case end, computed over the cases surviving the filters. The
widget's `value` aggregates those monthly points, and its segment axes
are ignored — the month is the axis. A case whose last event is not a
real ending is still "ended" at that event: the log cannot tell *still
running* from *ended silently*.

Where a definition here and one in [`metrics.md`](metrics.md) share a
name, they agree on what is measured: `duration` is the family report's
case duration (`metrics.md` §4), up to the rounding of two timestamps to
the second. One difference is deliberate: the activity time metrics here
are case-weighted ([§3](#case-weighted-vs-event-weighted)).

A metric naming an activity the log does not have is not an error:
counts and presence are 0, times are missing.

Interval-only metrics stay **visible** in the composer on a
single-timestamp log, marked unavailable with a reason, rather than
leaving a hole where a user expects service time to be.

### ƒ Custom formulas

A custom metric is a per-case expression in a small closed grammar,
optionally narrowed by a `where` clause, then aggregated like any other
metric. It exists so a user can measure something the catalog does not
name without anyone shipping code.

The grammar is parsed to an explicit AST and **never** handed to a
language `eval` — the only operations are the ones enumerated, over the
only data the fact table exposes. Full grammar and semantics are in the
module docstring of
[`formula.py`](../pm4py_ucm/algo/dashboards/formula.py); the short form:

* functions: `duration()` (days), `contains(act)` (0/1), `count(act)`,
  `time_between(a, b)` (days; null when `b` never follows `a`),
  `timestamp(act)` (epoch seconds of the first `act`), `attr(name)`;
* `+ - * /`, the comparisons `== != > >= < <=`, `and` / `or` / `not`,
  parentheses;
* a trailing `where <predicate>` that drops non-matching cases to null.

In a widget spec a formula is `"metric": "custom"` with
`"params": {"formula": "…"}`. A formula that does not parse evaluates to
null for every case, so a widget carrying one renders `—` rather than
taking the dashboard down; the composer is where it is validated.

**One value type.** Every expression evaluates, per case, to a number or
null — there are no strings at runtime. That single type is what keeps
the grammar agreeing between the Python evaluator and the JS one, with
no string/number coercion to get subtly wrong in two languages. `null`
(a missing time, an absent attribute) propagates and is dropped by
aggregation, exactly as everywhere else.

**Attributes.** `attr(name)` reads a *numeric* case attribute, for
arithmetic and inequalities. Numeric here means the *values* are
numbers, not the column's dtype — an amount the log serialises as text
counts. Identifier columns (`org:resource` and friends) never do,
however numeric they look. A categorical attribute is read through the
one place a string may appear outside a call: as the other side of an
equality, `attr("Channel") == "Web"` (or `!=`). The string is matched
against the attribute's value dictionary and the comparison yields
1 / 0 / null, so nothing string-valued exists at runtime; a bare string
anywhere else is an error.

**Result type** is inferred from the AST — a comparison, a logical, or a
bare `contains()` is a 0/1 indicator → `percent`; anything mentioning a
time function → `time`; else → `count` — and drives the unit and the
aggregations offered, exactly as a catalog metric's does.

The evaluator exists twice, like the rest of the engine, and the parity
suite feeds the same formulas to both. The composer's editor validates
through the *same* `compile_formula`, so what its chip calls valid is
exactly what will run.

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

What is printed (`text`), by unit: a percentage keeps one decimal; a
count is a whole number with thousands separators; a duration — held in
days — is shown in the largest unit that keeps it legible: days (one
decimal, none from 100 d up), else hours, else minutes, else whole
seconds. A group with no value prints `—`.

---

## 5. Segmentation

Axes: `year`, `quarter`, `month`, `weekday`, `resource` (when the log
has a resource column), `variant`, and one per detected case attribute
(`attr:<name>`). A spec's `segment` holds up to two, as `rows` and
`cols`.

* **Time axes read the case's start** — on an interval log the earliest
  start timestamp of its events, else its first event ([§2](#2-the-data-contract)). Labels are
  `2011`, `2011-Q1`, `2011-01`, `Mon`.
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
  on both ends — the family partitioner's convention. The two agree on
  the edges when both use quantile bins with the same `bins`; they can
  differ on *when* a column is kept as discrete levels (here: fewer than
  10 distinct whole numbers; the partitioner: at most `bins` of them —
  [§2](#case-attributes)).
* **A case with no value on an axis is in no segment.** A missing
  attribute, or a case with no resource, leaves the widget rather than
  forming a bucket of its own.
* **Empty segments are omitted, not zeroed.** A segment with no surviving
  case is not a zero bar. In a two-axis table, a row or column with no
  case is dropped and an empty cell inside the grid renders `—`.

### The Activity axis

`activity` is the one axis that does **not** partition the cases. It varies
an activity-level metric's own `activity` parameter over every activity in
the log, so one widget shows *sojourn by activity* instead of the sojourn of
the one activity its spec names — and, with a second (case) axis, an
activity × month or activity × attribute heatmap.

* It is valid only for metrics measured per activity (`actFreq`,
  `actPresence`, `actRepeats`, `actRework`, `actSojourn`, `actService`,
  `actWaiting`);
  the composer disables it for the rest, and the engines raise otherwise.
  The `activity` in `params` is ignored.
* The result has the shape of any segmented widget (`series`, or
  `rows`/`cols`/`cells`), so every renderer, the bar cap, CSV and axis swap
  work unchanged. There is no overall `value`: the activities do not share
  one.
* An activity with no value in any surviving case is omitted, like any
  empty segment — so a time metric drops activities that never occur under
  the filter, while `actPresence` keeps them at 0%, which is the answer.
* Clicking a bar or a row drills down to **the cases containing that
  activity** (a `segment` filter on `activity` is the `contains` filter).
* It is not listed by `segment_axes`, which the filter picker enumerates:
  activities are filtered with *contains*, not as a segment.

Cost: the metric runs once per activity, so a widget on this axis costs
as many passes over the events as the log has activities.

### Behavioural variants

`build_fact_table(log_df, behaviour=clustering)` adds each case's
**behavioural variant** — its concurrency-aware variant, from
[`variants/clustering.py`](../pm4py_ucm/algo/discovery/variants/clustering.py)
— as a case attribute named `behavioural variant` (`BEHAVIOUR_ATTRIBUTE`).
From there it is an ordinary enumeration: the axis
`attr:behavioural variant`, a filter field, and readable by the ƒ grammar,
in both engines, with no engine code of its own.

* Values keep the clustering's ids and order (`v1` is the most frequent),
  so a variant here is the variant of that name in the scenario reports.
  They are **not** the `v1`, `v2`… of the built-in `variant` axis, which
  ranks activity sequences.
* A case that did not replay on the model belongs to no variant and is
  *missing* — it leaves the denominator, as everywhere else.
* The attribute carries `descriptions` (`{id: partial-order expression}`),
  which the UI appends to bar, pie and table-header tooltips. The engines
  never read it.
* The variants are relative to the tree the clustering replayed on. A
  dashboard that uses them means something only alongside that model: the
  app keys the fact table on the miner's noise threshold for this reason,
  and an exported script rebuilds the clustering at the same threshold.

Segmentation arity is the first thing that decides the visualisation — see
the next section.

### Filtering by date

The `date` filter — `{field: "date", value: [from, to]}`, ISO dates,
either end nullable for an open range, the final period inclusive — is
matched against the case **start**. The filter picker's **Date range**
option produces it, its inputs seeded from the log's first and last
case-start dates (`dateSpan` in the browser engine).

---

## 6. Visualisations and their guards

`spec.viz` says how a widget's figure is **drawn**. Most shapes are a
rendering choice over a figure the engine already returns — only `hist`
and `box` add data — so adding one costs a renderer, not a metric.

The composer offers a shape only when it would say something **true**
about this widget. Those guards are not decoration: an unguarded pie of
averages, or a line over an alphabetical axis, states a relationship the
data does not have.

| `viz` | Offered when | Reads |
| --- | --- | --- |
| `kpi` | no segment axis | `value`, `text` |
| `gauge` | no axis **and the widget has a target** | `value` + the target |
| `hist` | no axis, non-series metric | `hist` |
| `box` | no axis, non-series metric | `box` |
| `bar` | one axis — or any series metric | `series` |
| `line` | one **time** axis — or any series metric | `series` |
| `pie` | one axis **and `agg` is `sum`** | `series` |
| `table` | two axes | `rows`/`cols`/`cells` |
| `model` | pinned from the Model view; not composed | `renders` |

The reasons, one guard at a time:

* **A gauge needs a target.** A dial reads a value *against* a threshold;
  with no target there is nothing to read it against and the needle is
  decoration. The dial spans value and target with headroom, so the arc
  never pins to an end and the target tick always lands on the scale.
* **A line needs an order.** A line says "this moved from here to there",
  which is true along time and false along an alphabetical list of
  countries. So `line` is offered on a time axis (and for series metrics,
  which are ordered by construction) and never on a categorical one.
* **A pie needs a whole.** Slices only mean anything when they add up:
  that is a `sum`, not an average — "average duration by country" sums to
  nothing. Hence `agg == "sum"`. `sum` is offered for times as well as
  counts (a total duration is a real quantity), so "case-days by country"
  is pie-able but "average days by country" is not.
* **A distribution needs per-case values.** `hist` and `box` bin the bag
  of per-case numbers, which a **series** metric (WIP, arrival rate) does
  not have — it is a run of ordered points. So they are offered only for
  non-series metrics, unsegmented (a segmented distribution would be one
  chart per segment, which is what the bar already is).

These guards are contextual — they depend on the segmentation, the
aggregation, the axis kind and whether a target is set — none of which is
a property of the metric. So they live in the composer (`syncViz`), and
there is deliberately no per-metric or per-result-type viz list to drift
out of step with them.

### The value axis

Bars, lines and histograms draw a **value axis**: a few round ticks from
zero to just past the largest value, with faint gridlines. Without it a
chart shows only shape — which month is highest, not how high — and every
number costs a hover.

* Ticks are chosen for roundness (steps of 1, 2, 2.5 or 5 × a power of
  ten), about three intervals to a chart.
* A duration is labelled in the unit that keeps its ticks round — a chart
  peaking at 45 minutes reads `0 m … 60 m`, not fractions of a day — the
  same largest-legible-unit rule a single value follows.
* Counts tick in whole numbers; a share that reaches 100% gets no headroom
  beyond it.
* The axis always starts at **zero**, and a bar's height is its value over
  the axis top. So bars are comparable by length, and a chart of sub-day
  durations no longer flattens against a scale of one day.

It is rendering only: `axisTicks` lives in `dash-ui.js`, reads the figures
the engine already returns, and adds nothing to the contract. Pie, gauge
and box plot already state their values (slices, the dial's scale,
min / median / max) and are unchanged.

### What the extra-data shapes return

* **Histogram** — `histogram(values)` returns `{bins: [{lo, hi, count}],
  min, max, n, integer}`. A whole-number metric spanning no more than 40
  gets one bar per value; everything else gets 20 equal-width bins. The
  bin index is a `floor` of plain IEEE-double arithmetic, computed the
  same way in Python and JS so the two never disagree on a boundary.
* **Box plot** — `box_stats(values)` returns the five-number summary plus
  1.5·IQR Tukey fences, the whisker ends (the extreme values *inside* the
  fences) and the outliers beyond them (capped at 100, with a full count).
  Quartiles reuse the same linear-interpolation `percentile` the
  aggregations do.

### Caps, and saying so

A chart that silently drops data lies about being complete, so both caps
are announced:

* **Bars** keep the largest `BAR_CAP` (24) of a categorical axis and say
  "top 24 of 164"; the target state still considers every segment. **Time
  axes are exempt** — their order carries the meaning, and reordering by
  value would destroy it.
* **Pie** draws at most `PIE_CAP` (8) slices: past that it keeps the
  largest seven and folds the tail into one *Other* slice, labelled with
  how many it swallowed. A pie stops being readable long before a bar
  does. Only positive values are drawn.

## 7. Targets

A target is `{on, dir, value, warn, mode}`, with `dir` either `"<="`
(less is better) or `">="`. `warn` is the threshold between *at risk*
and *missed*, on the far side of `value`: a `<=` target with value 14
and warn 18 reads "meet 14, tolerate 18, miss beyond". It defaults to
`value`, which leaves no *at risk* band. `value` and `warn` are in the
metric's unit — **days** for a time metric, percentage points for a
`percent` one. A target with `on: false` scores nothing; a group with no
value has no state.

Two modes:

* **`aggregate`** (default) — score the group's aggregated value: *is our
  average within 14 days?*
* **`per_case`** — score every case, then report the **share that met** as
  the group's value: *are 90% of cases within 14 days?* The distribution
  across met/risk/missed feeds the tri-colour bar. A share needs its own
  threshold to have a state at all, so it is scored (as a `>=` target)
  against `shareGoal` / `shareWarn`, two further fields of the target;
  without a `shareGoal` the share is reported but has no state.
  **Every value the widget reports is then a share**, whatever the metric
  measures: its `unit` is `%`, and so are its headline, each bar and each
  cell — a widget segmented by country reads "the share of each
  country's cases within 14 days". The mode takes effect only while the
  target is on.

**Scoring is per segment, and a widget rolls up to its worst segment.** A
heatmap of 40 segments with one breach is a breach; reporting its average
state would hide exactly the cell the reader needs.

The **scorecard** is one row per widget whose target is on — title, goal,
actual, state and case count — computed under the same dashboard filters
as the widgets. In `per_case` mode its goal is the share goal ("≥ 90% of
cases"), not the per-case threshold.

---

## 8. Widget specs

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

| field | meaning |
|---|---|
| `id` | unique within the dashboard; reorder and edit key off it. |
| `title` | the card's heading; defaults to the metric's label. |
| `metric` | a catalog id ([§4](#4-metric-catalog)), or `custom` for a ƒ formula. |
| `params` | the metric's parameters: `activity`, or `from` / `to`; `formula` for `custom`. |
| `agg` | one of the aggregations the metric's result type offers; defaults to the metric's own. |
| `filter` | a list of filters, below. |
| `segment` | `{rows, cols}`, each an axis of [§5](#5-segmentation) or absent. |
| `viz` | a shape of [§6](#6-visualisations-and-their-guards); defaults to `kpi`. |
| `target` | [§7](#7-targets); optional. |
| `size` | `{w, h}` grid span, set when the user resizes the card; overrides the shape's default ([Layout](#layout)). |
| `statusColors` | tables only: colour cells by target state instead of by value. |
| `why` | set on suggested widgets only ([`dashboard_suggestions.md`](dashboard_suggestions.md) §2.1); the engines ignore it. |

Dashboard filters stack **on top of** each widget's own filter, so a
reader-side filter narrows every widget without editing any of them. All
filters AND together. Each is `{field, op, value}`:

| `field` | `op` | `value` |
|---|---|---|
| `contains` | `is`, `not` | an activity name — the case contains it (or not). |
| `date` | `between` | `[from, to]` ISO dates against the case start; either may be null; the final period is included whole. |
| `resource` | `is`, `in`, `not` | a resource or a list, against the case's first-event resource. |
| `attr:<name>` (enumeration, boolean) | `is`, `in`, `not` | a value or a list. |
| `attr:<name>` (numeric) | `is`, `not`, `>`, `>=`, `<`, `<=`, `between` | a number, or `[lo, hi]` (inclusive) for `between`. A case with no value never matches, `not` included. |
| `segment` | `is`, `not` | `[axis, label]` — the drill-down a bar or table-cell click produces. |

A filter on a resource or attribute the log does not have passes every
case rather than none, so a definition loaded on another log degrades to
"unfiltered"; the loader reports the unbound name
([§9](#save-and-load-a-definition)).

`compute_widget` returns **data, not styling**: values, states, labels
and counts. The Streamlit view and the exported HTML render from the same
structure, so a widget looks the same in both because they read one
computation, not two. Every result carries `id`, `title`, `viz`, `unit`,
`resultType`, `agg`, `nCases` (cases surviving the filters) and
`sampled`, then by shape:

* no axis — `value`, `text`, `state`, `sub` (the caption), plus
  `distribution` in `per_case` mode and `hist` / `box` for those shapes.
  A `hist` or `box` result also carries `valueUnit`, the unit of its bins
  and quartiles: the metric's own, which under a `per_case` target is not
  the widget's `unit` (a share in `%`);
* one axis — `axis` and `series`: `[{label, value, text, state, nCases}]`,
  with the unsegmented `value` / `text` alongside (in `per_case` mode, the
  share over all the widget's cases);
* two axes — `rowsAxis`, `colsAxis`, `rows`, `cols` and `cells[r][c]` =
  `{value, text, state, nCases}`;
* a series metric — `series`: `[{label, value}]` per month, and the
  aggregated `value`.

`state` on a segmented widget is its worst segment's
([§7](#7-targets)).

---

## 9. The view

### Where state lives

In the browser. An embedded HTML document is one-way — it cannot send
state back to Python — and the export has no server at all, so widget
specs persist to `localStorage`, namespaced per log (`storage_key`,
default the log name) so two logs never share a dashboard.
`window.pmDashboard.exportSpecs()` reads the current widgets back out.

An editable page holds a **set of named dashboards**, not one: the header
switches between them, adds, renames and deletes, and each keeps its own
widgets and filters. The `specs` passed to `dashboard_html` only seed the
first dashboard of a log that has none saved; after that the browser's
set wins.

The host has two ways in, both through the page config, because it
cannot reach the widgets directly:

* `pending_pin` — `{id, spec}`, a widget to add, as *Pin to dashboard*
  on the Model view does. The config is re-sent on every rerun, so `id`
  must be fresh per request; the page remembers the ids it has applied.
* `suggestions` — dashboards to offer behind **✦ Suggest**
  ([`dashboard_suggestions.md`](dashboard_suggestions.md) §2.4).

The web app's saved projects read the dashboard set out of the browser,
and write it back on resume, through a separate component —
[`sessions.md` §11](sessions.md#11-dashboards--the-bridge). Nothing in
this package depends on it.

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

The practical one: the embed is one-way, so Python does not know
which widgets the user built — they live in this browser. An export
produced server-side would confidently ship the **default** dashboard
instead of theirs.

The export carries the current widgets *and the current filters*, so a
dashboard sent to answer a specific question opens on that question. An
*export all* writes every named dashboard into one read-only page whose
header still switches between them (`dashboards` / `active` in the
config).

### Save and load a definition

⬇ Export ships the *data* — the whole fact table, so the file runs
offline. ⬇ Save ships only the *recipe*: a few-kilobyte JSON file holding
the dashboard's name, its widgets, its filters, and the name of the log
it was built on.

```json
{
  "pm4pyUcmDashboard": 1,
  "name": "Ops overview",
  "log": "ClaimsPaymentLog",
  "specs": [ … widget specs … ],
  "filters": [ … dashboard filters … ]
}
```

⬆ Load reads such a file and *replaces* the current widgets (it does not
merge). Widget ids are re-minted on load, so a file with blank or
colliding ids — or the same file loaded twice — never breaks reorder or
edit, which key off the id.

A definition names activities and attributes, so it is portable across
logs that share them. When one is loaded against a log that lacks a name
a widget uses, that widget cannot bind — the engine renders it as a bare
`0` or *No data.*, which reads like a real measurement. So the loader
runs `unboundRefs(spec, table)` over every widget first — a static walk
of the metric params, the segment axes, the custom formula, and the
widget's own filters — and reports which widgets could not bind and what
each is missing, rather than letting them masquerade as answers. Filters
are exploration state tied to the loaded log's values, so they are
carried only when the definition came from that same log; otherwise they
are dropped and noted.

Save/Load lives in the island, next to Export, for the same reason Export
does: the widgets live in the browser and only the browser can serialise
them.

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

### The session report

The single-dashboard export above is one shape; the **session report** is
the other — a multi-section document with a left table of contents, a
reader filter bar that recomputes everything, a scorecard landing, the
dashboard(s), a process-model section with both notations as inline
SVG (zoom with the wheel, pan by dragging, offline) and, when a family
has been mined, its statistics report
([`model_families.md` §3](model_families.md#3-family-statistics-reports))
in a frame of its own (`family_report`). One artifact serves
both shapes: the page bootstrap boots a `Report` when `cfg.mode ===
"report"` and a `Dashboard` otherwise.

It is built client-side, like the export, so it carries the user's real
widgets rather than the seed dashboard. The one thing the browser cannot
produce is the model render, so Python embeds both notations as SVG in
the page config (`model_svg`) for the report to lift; a multi-map
(decomposed) model has no single SVG, so it falls back to the PNG the
model widgets already use.

Each dashboard section is drawn by the very same `Dashboard`, `headless`
(no per-section header) and read-only, sharing the report's filters — so
a widget looks identical in the app, the export and the report, because
there is one renderer. A cell drill-down bubbles up to the report's
shared filter, narrowing every section at once.

### Inlining

`dash-ui.js` imports `dash-engine.js`, and `dash-report.js` imports both;
nothing can resolve those relative specifiers in a `file://` or `srcdoc`
page. So all three are stitched into one module script: the engine is
wrapped in an IIFE returning its exports as the `E` namespace, and the UI
and report are concatenated after it with their imports and `export`
keywords stripped, so the report references `Dashboard` / `h` / `E` as
bundle-scope names. The export list is derived from the source, and a
test checks that every `E.<name>` the UI calls is actually exported — a
mismatch there is a runtime `TypeError` no Python test would otherwise
see.

Concatenating at one scope has a hazard the single-file checks miss: a
name declared top-level in two modules (a duplicate `STATE_LABEL` did
this) is a `SyntaxError` that fails the whole script and renders nothing.
A test therefore `node --check`s the **assembled** bundle, not just each
file.

### Escaping JSON into a script

The config is JSON inside a `<script>`, and a graphviz SVG carries a
`<!-- Generated -->` comment. Escaping `<!--` to `<\!--` (as an early
version did) breaks `JSON.parse` — `\!` is not a legal JSON escape. The
config instead escapes every literal `<` to `<`, which neutralises
both a closing script tag and the comment-open at once and stays valid
JSON. The client-side exports (`exportHtml`, `buildSessionReport`) apply
the identical `scriptJson`.

### Layout

CSS grid, 4 columns, 118px rows, `grid-auto-flow: dense`. KPI 1×1;
every other shape 2×2; a table with more than five data columns and the
model widget take the full width. Below 900px it folds to 2 columns.
Wide content scrolls inside its own card — the page never scrolls
sideways.

On an editable page a card can be dragged by its grip to reorder, and
resized from its corner, up to 4 × 4 cells; the span is stored on the
spec (`size`), so it survives a save, an export and a definition file.

### Honesty in the UI

The header states the live case count against the total, and says so
when the numbers are not the whole truth: a sampled fact table shows
`sample of N`, and dropped events show `N events dropped`. Status-mode
table cells carry `✓ / ! / ✕` as well as a colour, because colour alone
is not a signal every reader can see. An empty segment renders `—`, never
a zero.

---

## 10. Python API

Everything is importable from `pm4py_ucm.algo.dashboards`; none of it is
re-exported at the package root.

```python
import pm4py
from pm4py_ucm.algo.dashboards import (
    build_fact_table, compute_widget, scorecard, write_dashboard,
)

log = pm4py.read_xes("demo/ClaimsPaymentLog.xes")
table = build_fact_table(log, log_name="ClaimsPaymentLog")

spec = {"id": "w1", "metric": "duration", "agg": "median",
        "segment": {"rows": "attr:Country"}, "viz": "bar",
        "target": {"on": True, "dir": "<=", "value": 14, "warn": 18}}
widget = compute_widget(spec, table)          # data, not styling (§8)
rows = scorecard([spec], table)               # one row per targeted widget

write_dashboard("claims.html", table, specs=[spec], read_only=True)
```

| | |
|---|---|
| `build_fact_table(log_df, *, log_name, bins, max_payload_bytes, sample_cases, behaviour, …_col)` | the fact table of [§2](#2-the-fact-table); `FactTable.to_payload()` is what ships. |
| `CATALOG`, `BY_ID`, `catalog_json(interval_log=…)` | the metric catalog of [§4](#4-metric-catalog). |
| `per_case_values`, `series_values`, `aggregate`, `apply_filters`, `segment_axes`, `segment_keys`, `target_state`, `worst_state`, `fmt` | the engine's building blocks. |
| `compute_widget(spec, table, *, dashboard_filters=None)`, `scorecard(specs, table, *, dashboard_filters=None)` | one widget, and the targets table. |
| `compile_formula(text)`, `parse_formula`, `result_type`, `FUNCTIONS`, `FormulaError` | the ƒ grammar. |
| `predicate_case_ids(log_df, formula)` | the case ids for which a ƒ predicate is true, over the whole (never sampled) log — a log filter. Raises `FormulaError` on a bad formula. |
| `dashboard_html(table, *, specs, filters, name, read_only, renders, storage_key, title, theme, …)`, `write_dashboard(path, table, **kwargs)` | the self-contained page of [§9](#9-the-view). |
| `suggest_dashboards(table, *, max_widgets=10)` | [`dashboard_suggestions.md`](dashboard_suggestions.md). |
