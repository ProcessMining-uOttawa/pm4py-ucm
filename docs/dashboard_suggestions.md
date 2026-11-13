# Dashboard suggestions — design

> **Status.** Tier 1 (§2) is **implemented**:
> [`algo/dashboards/suggest.py`](../pm4py_ucm/algo/dashboards/suggest.py),
> pinned by [`tests/test_dashboard_suggestions.py`](../tests/test_dashboard_suggestions.py).
> Tier 2 (§3) and tier 3 (§4) are **design proposals — not implemented**.

## 1. Problem and principle

The Dashboards view is underused because a useful dashboard costs many
composer round-trips: pick a metric, pick its activities, pick an axis, pick
a chart, repeat. Until now the view opened on a fixed six-widget starter that
names no activity and no attribute, so it looked the same on every log, and a
dashboard created with **+** opened empty.

A dashboard is already a plain JSON list of widget specs
([`dashboards.md`](dashboards.md) §8). So proposing one means *emitting
specs* — nothing in the engine, the renderer or the JS/Python parity changes.

The same principle as [`ai_insights.md`](ai_insights.md) applies: **the
deterministic layer is the product.** Three tiers, each usable without the
next:

| Tier | Input | LLM | Status |
|---|---|---|---|
| 1 | the log | none | implemented |
| 2 | the log + an informal description | external, the user's own | proposed |
| 3 | the log + a GRL goal model | none (optional for binding) | proposed |

## 2. Tier 1 — from the log alone

### 2.1 API

```python
from pm4py_ucm.algo.dashboards import build_fact_table, suggest_dashboards

table = build_fact_table(log_df, log_name="claims")
# ...or, to have behavioural variants proposed too (§2.2):
#   clustering = pm4py_ucm.algo.discovery.variants.clustering.cluster(log_df, tree)
#   table = build_fact_table(log_df, log_name="claims", behaviour=clustering)
for d in suggest_dashboards(table):
    print(d["name"], "—", d["why"])
    for spec in d["specs"]:
        print("   ", spec["title"], "—", spec["why"])
```

`suggest_dashboards(table, *, max_widgets=10)` returns a list of dashboards,
most general first:

```json
{"key": "bottlenecks", "name": "Bottlenecks",
 "why": "Assess Claim holds 41% of all elapsed time.",
 "specs": [ … widget specs, each with a "why" … ],
 "filters": []}
```

It reads **only the fact table** — the same snapshot the browser computes on —
so a suggestion can never name something a widget cannot bind, and a sampled
table yields suggestions consistent with the sampled numbers on screen.

`why` is the one field a suggested spec adds to the spec format. It is the
sentence that justifies the widget, with the number that triggered it. The
engines ignore it; the UI shows it in the gallery.

### 2.2 What is proposed, and when

Every rule is a property of the log, never a guess about its domain.

Each themed dashboard is built in the same order: a few KPIs that **name**
what matters most, then a chart or heatmap that shows **everything** — so
the reader sees the top of the ranking and what the ranking was taken
from. The wide charts use the **Activity axis** ([`dashboards.md`](dashboards.md)
§5): one bar, or one heatmap row, per activity.

**Overview** — always.

| Widget | Condition |
|---|---|
| median and P90 case duration (KPI) | always |
| rework rate, events per case (KPI) | always |
| case duration (histogram) | durations are not all equal |
| arrivals over time, WIP over time (line) | cases start in ≥ 3 calendar months |
| duration by *period* (line) | *period* has ≥ 3 values; see below |
| duration by weekday (bar) | no usable period (a short log) |
| share of cases by activity (bar, every activity) | always — the process's footprint |

The *period* axis is the coarsest of `month` / `quarter` / `year` that still
has between 3 and 24 segments, preferring the finest: a two-month log gets no
trend line at all rather than a two-point one.

**Bottlenecks** — when some activity holds time.

Activities are ranked by their **share of all elapsed time**: the sum, over
every execution, of the time since the case's previous event, divided by the
sum over all events. That is event-weighted on purpose — it ranks where the
log's time *goes* — while the widgets it produces report the catalog's
case-weighted `actSojourn`, and each `why` says which is which.

| Widget | Condition |
|---|---|
| avg sojourn of the top activities (KPI, up to 3) | each holds ≥ 5% of elapsed time |
| avg waiting vs service of the top activity (KPI ×2) | interval log |
| avg sojourn by activity (bar, every activity) | always |
| avg sojourn, activity × *period* (heatmap) | a period exists, ≤ 40 activities |
| avg time across the slowest transitions (KPI, up to 2) | taken by ≥ 5% of cases, and not leading into an activity listed above (that would restate its sojourn) |
| top activity's sojourn by *period* (line) | a period exists, > 40 activities (no heatmap) |
| top activity's sojourn by the best attribute (bar) | a *strong* attribute exists (below) |

**Rework and flow** — when cases repeat work or the flow branches.

| Widget | Condition |
|---|---|
| rework rate (KPI) | ≥ 5% of cases rework |
| rework rate of the most-repeated activities (KPI, up to 2) | repeated in ≥ 5% of all cases |
| rework rate by activity (bar, every activity) | ≥ 5% of cases rework |
| rework rate, activity × *period* (heatmap) | ≥ 5% of cases rework, a period exists, ≤ 40 activities |
| rework rate, activity × best attribute (heatmap) | as above but no period, and a strong attribute exists |
| share of cases reaching each end activity (KPI, up to 3) | ≥ 2 end activities each closing ≥ 5% of cases |
| branch probability at the main decision points (KPI, up to 4) | below |
| rework by *period* (line) | ≥ 5% of cases rework, a period exists, > 40 activities (no heatmap) |

The per-activity widgets use the catalog's `actRework` — the share of the
cases *performing* an activity that perform it more than once — not
`actRepeats`: an average of 0.3 repeats per case displays as "0", and a
widget that reads 0 next to a reason that says 31% is worse than none. Two
denominators are in play and each reason states both: an activity is
*selected* for a KPI by its repeats as a share of **all** cases (rework
nobody reaches is not worth a widget), and *reported* as a rate among the
cases that reach it (so a rare activity that is always redone stands out on
the bar and the heatmap).

A **decision point** is an activity that ≥ 10% of cases reach and that has at
least two directly-following activities each taken by ≥ 10% of the cases
reaching it. It is read from the log's directly-follows relation, not from
the mined model, because that is exactly what `edgeShare` measures: the
widget and the rule that proposed it then cannot disagree.

Directly-follows alone confuses concurrency with choice: two parallel
activities `a ∥ b` show `a → b` and `b → a`, and whatever precedes them
appears to "choose" between them. Two exclusions remove that:

* a successor that **interleaves** with the activity itself is not a branch;
* two successors that interleave with **each other** are the halves of a
  parallel split, and neither is a branch.

`a` and `b` interleave when both `a → b` and `b → a` occur but in *different*
cases (fewer than half the cases taking one also take the other). That is
what separates concurrency from a loop, which also runs both ways — but in
the same cases, since the case that returns is one that went.

This is a heuristic over the log. The mined UCM knows its OR-forks and
AND-forks exactly; anchoring these widgets to its forks (and showing them
beside the pinned model) is the follow-up, and needs the fork → activity
binding the performance overlay already has.

**Segments** — when something splits the cases.

Case attributes are scored exactly as the partition advisor scores them
([`advisor.py`](../pm4py_ucm/algo/discovery/families/advisor.py)): control-flow
divergence (normalised mutual information with the variant) and duration
effect size (η²), combined 0.6 / 0.4, on the fact table's own segment keys.
An attribute is **chartable** when it has 2–24 segments, covers ≥ 50% of
cases and is not nearly constant, and **strong** when it also scores ≥ 0.05.

| Widget | Condition |
|---|---|
| median duration by attribute (bar), the 2 best-scoring | chartable |
| rework by the best attribute (bar) | strong, and ≥ 5% of cases rework |
| share of cases, activity × best attribute (heatmap) | strong, ≤ 40 activities |
| avg duration, best attribute × *period* (table) | strong, a period exists |
| median duration by resource (bar) | 2–200 resources |
| median duration by sequence variant (bar) | ≥ 2 variants, and no behavioural variants (below) |

A chartable attribute that is not strong still gets its one bar, with a
reason that says it barely separates the cases: on a log where no attribute
matters, that is the finding, and hiding the attributes would leave the user
to build the same bars by hand to learn it. Nothing else is built on a weak
attribute.

**Behavioural variants** — when the fact table carries them.

The built-in `variant` axis distinguishes every activity *sequence*, so two
orders of the same parallel work are two variants: ClaimsPaymentLog has 164.
pm4py-ucm's concurrency-aware clustering
([`variants/clustering.py`](../pm4py_ucm/algo/discovery/variants/clustering.py))
replays each case on the mined tree and groups cases by the *choices* they
made, folding the interleavings — the same log has 18 such variants. Handing
that clustering to `build_fact_table(behaviour=…)` makes it a segment axis
([`dashboards.md`](dashboards.md) §5), and then:

| Widget | Condition |
|---|---|
| cases by behavioural variant (pie) | ≥ 2 variants |
| median duration by behavioural variant (bar) | ≥ 2 variants |
| rework rate by behavioural variant (bar) | ≥ 5% of cases rework |
| share of cases, activity × behavioural variant (heatmap) | ≤ 40 activities |
| cases, behavioural variant × *period* (table) | a period exists |
| cases, behavioural variant × best attribute (table) | a strong attribute exists |

The activity × variant heatmap is the one that says what each variant *is*:
a cell is 100% or 0% (a variant contains an activity or does not), and a
column's tooltip gives its partial-order expression. The tables are narrowed
to the 8 most frequent variants by a **filter on the widget** — visible on
the card and removable — rather than by silently dropping columns; the bars
and the pie are not narrowed, since they announce their own tail.

There is no catalog metric that counts cases, so the count widgets use the
ƒ formula `1` summed, which is also what makes them pie-able.

The dashboard's reason states how far the sequences fold and, when not every
case replays on the model, what share does: a case that fits no variant has
no value on this axis and is in none of these widgets.

A dashboard with fewer than three widgets is not proposed, and a chart is
kept only if it draws (two bars, three points on a line, a 2 × 2 table) — an
axis chosen from the log's shape can still be nearly empty for one widget's
metric. `max_widgets` truncates each dashboard in the order above.

### 2.3 Targets: prefilled, switched off

A suggested KPI on a "less is better" metric (durations, sojourn, transition
time, rework, repeats) carries a target that is **off**:

```json
{"on": false, "dir": "<=", "value": 12.4, "warn": 14.9, "mode": "aggregate"}
```

`value` is the widget's current value — a baseline — and `warn` is 20% beyond
it. A target is a statement of intent, and the data cannot supply intent: a
target derived from the distribution marks a fixed share of cases *at risk*
by construction. So the suggestion saves the typing, and the analyst turns the
target on (and usually moves it) in the composer. Nothing suggested appears in
the scorecard until then. (The composer drops a target that is still off
when a widget is saved, as it does for any widget; the prefill is an offer
made once, on the first edit.)

### 2.4 Surfacing

`dashboard_html(..., suggestions=…)` carries the list in the page config; the
island never computes a suggestion, so there is no second implementation to
keep in parity.

* **Seed.** The app passes the Overview's specs as the seed of a log's first
  dashboard, replacing the fixed starter. A dashboard set already saved in the
  browser, or restored from a project, wins as before.
* **Gallery.** A **✦ Suggest** button in the header, and in the empty state of
  a new dashboard, opens the list: each dashboard with its reason and its
  widgets with theirs, and **Add**. Add fills the current dashboard when it
  is empty (the user made a blank one and asked what to put in it) and
  otherwise creates a *new* named dashboard and switches to it. It never
  replaces or merges into a dashboard that has widgets; a name already taken
  gets a numeric suffix.
* **Exports.** Read-only pages cannot restructure a dashboard, so the
  suggestions are stripped from an export rather than shipped as dead weight.

## 3. Tier 2 — an external LLM writes the definition (proposed)

The provider seam in `ai_insights.md` §3 puts an LLM *inside* the tool. For
dashboards a lighter route exists, because the import path already does:
the tool **exports a brief**, the user gives it and an informal description
to any LLM they are allowed to use, and **⬆ Load** imports what comes back.

No key, no provider code, no network call from the tool — and a confidential
log's owner sees exactly what leaves, since it is a text file they paste.

### 3.1 The brief

`describe_log(table) -> str`, offered as **⬇ Brief for an AI assistant**. One
Markdown file, four parts:

1. **The log** — name, case/event counts, date span, interval or not; every
   activity with its case share, mean repeats and share of elapsed time; the
   main transitions and decision points; every case attribute with its type,
   its values (or bins) and their case shares; resources (count, top few).
   Aggregates only — no case ids, no rows. This is the tier 1 profile, printed.
2. **The vocabulary** — the metric catalog with parameters and result types,
   the aggregations per result type, the segment axes this log supports, the
   visualisation guards of `dashboards.md` §6, the filter and target shapes,
   and the ƒ formula grammar. Generated from `catalog_json`, `segment_axes`
   and `FUNCTIONS`, never hand-written, so it cannot drift from the engine.
3. **The file format** — the `pm4pyUcmDashboard: 1` definition, its JSON
   Schema, and two worked examples taken from tier 1's own output for this
   log (so the examples are valid by construction and use real names).
4. **The rules** — use only names listed above, verbatim; output one JSON
   object and nothing else; set a target only when the description states one;
   prefer 6–8 widgets.

The user adds their own paragraph ("we care about time to first response for
priority-1 tickets; SLA is 4 hours").

### 3.2 Is this too fragile?

Not if the loader stops trusting the file. Today **⬆ Load** checks the
envelope and reports unbound activity and attribute names; everything else
passes through, and a wrong spec renders as an error card or, worse, as a
plausible number. An LLM will produce every one of these, so each needs a
check, and a hand-edited file benefits from the same checks:

| Failure | Handling |
|---|---|
| Prose or code fences around the JSON | extract the outermost `{…}` |
| Unknown metric, aggregation, axis, viz, filter field or op | reject the widget, say which field |
| Aggregation not offered for the metric's result type | reject (a `percent` metric is forced to `share` as now) |
| Viz that fails its guard (pie of averages, line on a categorical axis, gauge with no target) | **repair** to the shape the composer would pick, and say so |
| Missing or misspelt activity / attribute | case-insensitive and whitespace-insensitive match if unique; otherwise the existing unbound report |
| Invalid ƒ formula | reject with `compile_formula`'s own message |
| Interval-only metric on a single-timestamp log | reject with the catalog's reason |
| Time in the wrong unit (the catalog is in days) | cannot be detected; the brief states the unit, and a loaded target is shown in the report for the user to confirm |

The validator is `validate_definition(defn, table) -> (clean, report)` in
Python, mirrored in the island like the rest of the engine, with the report
shown in the existing "Dashboard loaded" dialog: *7 widgets loaded, 1
repaired, 1 rejected — here is why*. Rejections are copy-pasteable, so the
user can hand them back to the LLM for a second pass.

What stays fragile, and should be said in the UI: the LLM can pick a valid
metric that does not mean what the user asked. Validation proves a widget is
*well-formed and bound*, not that it is *the right question* — the same limit
a human-built widget has, which is why each loaded widget should keep the
LLM's one-line `why`.

The grounding contract of `ai_insights.md` §2 holds by construction: the LLM
chooses from a closed vocabulary and writes thresholds the user stated; every
displayed figure is computed by the engine.

### 3.3 Build order

1. Loader validation and repair (useful on its own, for hand-edited files).
2. `describe_log` and the header button.
3. Optional: a paste box in the Load dialog, so the user need not save a file.

The in-tool provider (phase B0 of `ai_insights.md`) can later call the same
brief and the same validator; nothing here is thrown away.

## 4. Tier 3 — from a goal model (proposed)

The inverse of [`goal_insights.md`](goal_insights.md) §2.1, which lifts
dashboard targets into GRL indicators:

* each GRL **indicator** → one widget, with `targetValue` / `thresholdValue`
  → `target.value` / `target.warn`, and the direction from which side
  `worstValue` lies on;
* each **softgoal** or **actor** → one dashboard holding the indicators that
  contribute to it;
* the scorecard then reads as goal satisfaction.

Two things are missing. The GRL half of the metamodel does not exist yet
(`_emit_grlspec` writes an empty `grlspec`; `goal_insights.md` phase P0 builds
it, importer included). And a hand-authored indicator — "Turnaround time" —
does not say which metric and which activities measure it. That binding needs
either metadata on the indicator (a `pm4py-ucm:metric` annotation carrying a
widget spec, which a model synthesised by `goal_insights.md` would have for
free) or the tier 2 route, with the goal model appended to the brief.

## 5. Testing

* Each rule in §2.2 has a test on a hand-computable log: the widget appears
  when its condition holds and is absent when it does not.
* Every suggested spec, on every fixture and on the shipped sample logs,
  computes without error, binds (`unboundRefs` is empty in JS), and satisfies
  the visualisation guards of `dashboards.md` §6.
* Suggestions are deterministic: two calls return equal lists.
* Tier 2, when built: a corpus of malformed definitions, one per row of the
  §3.2 table, each asserting its handling.
