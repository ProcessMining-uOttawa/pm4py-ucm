# Metric definitions (the semantic contract)

This document is the precise specification of every quantitative metric
`pm4py-ucm` computes from an event log: the activity, edge, process and
choice statistics shown in the performance overlays
([`algo/performance.py`](../pm4py_ucm/algo/performance.py)) and the
family statistics reports
([`algo/discovery/families/stats.py`](../pm4py_ucm/algo/discovery/families/stats.py)).

It is the *contract* the validation suite
([`tests/test_metric_validation.py`](../tests/test_metric_validation.py))
enforces: every definition below is pinned by a test with hand-computed
numbers and, where an external authority exists, reconciled against
pm4py. If you change a definition here, a test will change with it — on
purpose.

Notation: `n` is a count; a *case* is one process instance (trace); an
*event* is one activity execution; a *directly-follows pair* `(a, b)` is
an adjacency where `b` is the next event after `a` in the same case.

---

## 1. The log model

A log is a table with, per event:

| Column (default key)        | Meaning                                            |
|-----------------------------|----------------------------------------------------|
| `case:concept:name`         | case (trace) identifier                            |
| `concept:name`              | activity name                                      |
| `time:timestamp`            | **completion** timestamp of the event              |
| `start_timestamp` *(opt.)*  | **start** timestamp of the event                   |

Two log kinds follow from whether `start_timestamp` is present:

* **Interval log** — both `start_timestamp` and `time:timestamp` are
  present. Every event has a duration, so activity *service times* are
  defined and edge waiting is measured completion→**start**.
* **Single-timestamp log** — only `time:timestamp`. Events are
  instantaneous; service times are undefined (and omitted, never
  faked), and edge waiting is measured completion→**completion**.

Within a case, events are ordered by `time:timestamp` (a stable sort;
see [§7](#7-semantics-decisions)). All metrics are invariant to input
row order — the computation sorts internally.

Durations are reported in **seconds** throughout.

---

## 2. Activity metrics

Computed per distinct activity name.

| Metric                | Definition |
|-----------------------|------------|
| `frequency`           | number of events of this activity (executions). |
| `relative_frequency`  | `frequency / n_events` — share of all events (0..1). Sums to 1 across activities. |
| `case_coverage`       | number of *distinct cases* containing at least one such event. |
| `repeat_frequency`    | `frequency − case_coverage` = repeat executions (events beyond the first per case) — a **rework** indicator. |
| `mean/median/min/max/total/std/p90/p95_time` | **service time** = `completion − start`, aggregated over the activity's events. **Interval logs only**; omitted entirely on single-timestamp logs. |
| `sojourn_mean/median/min/max/total/std/p90/p95_time` | **sojourn time** = `completion − previous event's completion` in the same case, aggregated over the activity's events. Defined on **any** timestamped log. |
| `traversal_frequency` | replay-based: how many times the *model* walks this activity — see [§9](#9-traversal-metrics-replay-on-the-model). Differs from `frequency` wherever the model over- or under-generalises. |

`std` is the **sample** standard deviation (`ddof = 1`) and is omitted
when the metric has fewer than two samples; `p90` / `p95` are the 90th /
95th percentiles by **linear interpolation** (numpy/pandas default).
These conventions apply to every time-aggregate in this document.

**First-event exclusion (sojourn).** A case's first event has no
predecessor, so it contributes no sojourn value. Consequently the number
of sojourn samples for an activity is
`frequency − (number of cases whose first event is that activity)`. This
identity is an enforced invariant.

Service time ≈ processing; sojourn ≈ waiting + processing attributed to
the activity (the time the case spent since its previous step). On a
single-timestamp log, sojourn is the only activity-level time available
and is the honest stand-in for "how long this step took".

The diagram-overlay layer
([`NODE_METRICS`](../pm4py_ucm/algo/performance.py)) exposes only
`frequency`, `case_coverage`, and the `*_time` / `sojourn_*_time` means,
medians and totals. Every other entry (`relative_frequency`,
`repeat_frequency`, `min`/`max`/`std`/`p90`/`p95`) is computed and
carried in `PerformanceStats` / `FamilyStats` for the comparison reports
but is **not** an overlay metric — so the `.jucm` overlay output is
byte-unchanged by their presence.

---

## 3. Edge metrics (directly-follows pairs)

Computed per directly-follows pair `(a, b)` observed in the log.

> **These counts measure event *adjacency*, not flow.** On a model with
> concurrency or silent skips they do not conserve — an activity's
> outgoing edge can read a small fraction of the activity's own count,
> because the event that actually follows it belongs to a parallel
> branch. Use the **traversal metrics of [§9](#9-traversal-metrics-replay-on-the-model)**
> when you need numbers that add up.

| Metric                | Definition |
|-----------------------|------------|
| `frequency`           | number of times `b` directly follows `a` (across all cases). |
| `case_frequency`      | number of *distinct cases* in which `b` directly follows `a` at least once (`≤ frequency`). The gap `frequency − case_frequency` is rework on the handover. |
| `relative_frequency`  | `frequency / (n_events − n_cases)` — share of all directly-follows traversals. Sums to 1 across pairs. |
| `mean/median/min/max/total/std/p90/p95_time` | **waiting time** between the two events, aggregated over all occurrences of the pair. |
| `percentage`          | *(overlay only, on OR-fork branches)* this branch's share of its fork's outgoing traversals — see [§3.3](#33-branch-percentage). |
| `traversal_frequency`, `traversal_percentage` | replay-based — see [§9](#9-traversal-metrics-replay-on-the-model). |

**Waiting time depends on the log kind:**

* Interval log: `waiting = start(b) − completion(a)` — the idle gap
  between finishing `a` and starting `b`. **Excludes** `b`'s service
  time.
* Single-timestamp log: `waiting = completion(b) − completion(a)` —
  which necessarily **includes** `b`'s (unmeasurable) service time. This
  equals `b`'s sojourn value for that occurrence.

The frequency identity `Σ pair.frequency = n_events − n_cases` holds on
any log (every event except a case's first closes exactly one pair).

### 3.1 Segment attribution (how an edge maps to activity pairs)

On a UCM, an arc rarely connects two activities directly — it may pass
through routing bends, empty points, OR/AND forks and joins, and static
stubs into plug-in maps. Edge statistics are attributed by resolving
each arc to the **set** of activities reachable immediately *before* it
(walking backward through joins — all predecessor branches) and
immediately *after* it (walking forward through forks — all outgoing
branches), transparently following **static single-binding stubs** into
and out of their plug-in maps. Dynamic or multi-binding stubs stop the
walk (the mapping would be ambiguous).

The edge's statistics are then the aggregate of the directly-follows
pairs over `prevs × nexts`. This is what lets an OR-fork sitting right
after a join — the common decomposed case — still receive branch
statistics: its predecessor is the *set* of activities feeding the join.

### 3.2 Aggregating a segment over several pairs

When a segment spans more than one `(prev, next)` pair, the pair
statistics combine as:

* `frequency` — **summed** across the constituent pairs;
* `total_time` — **summed**;
* `mean_time` — **frequency-weighted** mean of the pair means
  (`Σ meanᵢ·freqᵢ / Σ freqᵢ`), *not* the arithmetic mean of the means;
* `median_time` — **only kept when the segment is a single pair**;
  medians do not combine from summary statistics, so a multi-pair
  segment reports no median rather than a wrong one.

### 3.3 Branch percentage

On an arc leaving an OR-fork, `percentage` is the branch segment's
frequency divided by the sum of all the fork's branch segment
frequencies. Branch percentages of a fork sum to 1 (100%). Percentage is
undefined (and omitted) on arcs that do not leave an OR-fork.

---

## 4. Process metrics (per family cell / sub-log)

Computed per family cell in
[`compute_family_stats`](../pm4py_ucm/algo/discovery/families/stats.py);
the same definitions apply to a whole log viewed as one cell.

| Metric                     | Definition |
|----------------------------|------------|
| `n_cases`                  | distinct cases in the cell. |
| `coverage`                 | `n_cases / total cases in the family` (0..1). |
| `n_events`                 | events in the cell. |
| `n_activities`             | distinct activity names in the cell. |
| `events_per_case` (mean/median/min/max) | distribution of trace lengths. |
| `duration` (mean/median/min/max/**total**/std/p90/p95) | **case duration**, see below. |
| `rework` (`case_fraction`, `mean_repeats_per_case`) | share of cases with at least one repeated activity, and the mean per case of `Σ(count − 1)` repeat executions. |
| `start_activities` / `end_activities` | `{activity: n_cases}` — cases beginning / ending with each activity; values sum to `n_cases`. Ordered by timestamp when present, else by row order within the case. Match pm4py's `get_start_activities` / `get_end_activities`. |
| `variants`                 | behavioural variant counts + fitness, see [§5](#5-replay-metrics-variants-fitness-choices). |

**Case duration** = `latest completion − earliest start` within the
case:

* Interval log: `max(completion) − min(start_timestamp)` — the full
  wall-clock span including the first event's lead-in.
* Single-timestamp log: `max(completion) − min(completion)` (start
  defaults to completion), i.e. first-event → last-event span.

`duration.total` is the **sum** of case durations across the cell (added
because it is a headline figure for effort/throughput comparisons). Case
duration is **absent** (empty dict) when the log has no timestamps.

The single-timestamp case duration matches pm4py's
`get_all_case_durations` exactly (validated on the claims log, all
cases).

---

## 5. Replay metrics (variants, fitness, choices)

These come from replaying each cell's sub-log on the cell's configured
process tree (the family skeleton with the cell's variants substituted),
via the concurrency-aware clustering
([`algo/discovery/variants/`](../pm4py_ucm/algo/discovery/variants/)).

| Metric                  | Definition |
|-------------------------|------------|
| `n_variants`            | number of **concurrency-aware** behavioural variants — traces differing only in the interleaving order inside a parallel block share a variant. |
| `n_sequence_variants`   | number of distinct activity **sequences** (the naive trace-variant count). Always `≥ n_variants`. |
| `fitness`               | fraction of cases in `[0, 1]` that replayed cleanly on the tree. `1 − fitness` is the noise share. |

**Choice metrics** (`FamilyChoice`) align OR-fork branch counts *across
cells*. The per-cell trees are anti-unified into the family skeleton
(control-flow only — resource variation does **not** split a fork), and
each cell's replay lands branch-traversal counts on the skeleton fork
identity, so "the choice after *Close Assessment*" is one comparable row
for every cell.

For each choice and each cell, `counts` is the per-branch traversal
count, or `None` when the cell never reaches the choice (distinct from a
zero — "not reached" ≠ "reached, never taken").

* **Outside-loop** fork: each conforming case picks exactly one branch,
  so the branch counts sum to the number of conforming cases reaching
  the fork (`≤ n_cases`).
* **Inside-loop** fork (`inside_loop = True`): the choice is evaluated
  once per loop iteration, so summed counts can **exceed** the case
  count.
* **Noise exclusion:** a non-conforming case (one that does not replay
  on the tree) contributes to **no** branch and lowers `fitness`; it
  never inflates a choice count.

`shared = True` marks a fork in the shared skeleton every cell can
reach; `shared = False` marks a fork living inside one variation-point
variant (only the covering cells have counts).

All variant/fitness/choice numbers are recoverable exactly from a log
generated by a known tree with fixed branch choices — this is how they
are validated ([§8](#8-validation)).

---

## 6. Aggregation-order guarantees

For any activity or pair time metric on any log:

```
min ≤ median ≤ max      and      min ≤ mean ≤ max
min ≤ p90 ≤ p95 ≤ max    and      std ≥ 0
total = mean × frequency   (to floating-point tolerance)
```

Frequency identities: `Σ activity.relative_frequency = 1`,
`Σ pair.relative_frequency = 1`, `repeat_frequency = frequency −
case_coverage`, and `case_frequency ≤ frequency`.

These are enforced invariants, not incidental.

---

## 7. Semantics decisions

Deliberate choices at the edges of the definitions. Each is pinned by a
test so a change is conscious. **These are the definitions most worth a
second opinion** — if any disagrees with how you (or a reviewer) expects
the metric to behave, change it here first and let the test follow.

| Situation | Decision | Rationale |
|-----------|----------|-----------|
| **Working calendars are not applied** (weekends, public holidays, shift/business hours) | All service, sojourn, waiting and case-duration times are **raw wall-clock** elapsed seconds — no non-working periods are subtracted. A gap spanning a weekend counts the full 48h+. | Calendars are organisation- and locale-specific; applying one would bake an assumption into every number. Consumers needing business-time can post-process with their own calendar. Documented so the wall-clock nature is explicit, not implied precision. |
| **Overlapping activities on an interval log** (successor starts before predecessor completes) | Waiting time is reported **negative**, not clamped to 0. | The negative value is real information (resources worked in parallel / out of order); clamping hides it. Consumers that want non-negative waiting can clamp downstream. |
| **Simultaneous (tied) timestamps** | Zero-duration edge; the pair is still counted. | A real adjacency with no measurable gap. |
| **Single-event case** | Contributes to activity `frequency` and `case_coverage` but to no directly-follows pair. | It has no adjacency. |
| **Timezone-aware timestamps** | Supported; differences are computed in true elapsed seconds. | — |
| **First event of a case** | Excluded from sojourn (no predecessor). | Sojourn is defined relative to the previous event. |
| **Multi-pair segment median** | Dropped (reported absent). | Medians cannot be combined from per-pair summaries without the raw samples. |
| **Mixed int/float attribute values (partitioning)** | pandas dtype coercion may render `7` as `7.0`; this is a property of the frame, consistent across the vectorised and event-log paths. | Documented, not "fixed". |

---

## 8. Validation

The metrics are validated by four independent oracle strategies
([`tests/test_metric_validation.py`](../tests/test_metric_validation.py)):

1. **Hardened distinct-value oracle** — a hand-computed fixture where
   every aggregate (min, median, mean, max, total) is a *different*
   number, asserted to the exact second. This catches mean↔median and
   min↔max swaps that the older uniform-timing fixtures could not.
2. **Invariants** — the algebraic identities of §2, §3 and §6 checked
   on random logs.
3. **Metamorphic transforms** — duplicating cases scales counts/totals
   but not means/medians; shifting all timestamps is invariant; scaling
   time scales all durations; permuting rows and relabelling cases are
   invariant; concatenating disjoint logs is additive.
4. **Simulation ground truth** — logs generated from a known tree with
   fixed branch choices, loop counts and injected noise, so variant
   counts, fitness and per-branch choice counts are known by
   construction.

Where an external authority exists, the numbers are reconciled against
**pm4py**: directly-follows frequencies, performance-DFG waiting times
(mean/median/min/max/sum/**stdev**), start/end-activity distributions,
and case durations (including P90/P95/std) match `pm4py.discover_dfg`,
`pm4py.discovery.discover_performance_dfg`,
`pm4py.get_start_activities` / `get_end_activities` and
`pm4py.get_all_case_durations` (percentiles via `numpy.percentile`)
exactly — on the interval claims log
(`demo/ClaimsPaymentLog.xes`, 78k events) and on a large private
single-timestamp clinical log (617k events, 334 edges), once the
interval-vs-completion waiting semantics of §3 are accounted for. The
suite ships a pm4py differential test on a synthetic single-timestamp
log (skipped when pm4py is absent), so the reconciliation runs in CI
without any private data.

---

## 9. Traversal metrics (replay on the model)

Everything above counts **the log**: events, and pairs of events that
were adjacent in a trace. Those are the right definitions for what they
measure, but neither answers the question a reader actually asks of a
diagram — *how many cases go this way?* — and on a model with
concurrency the difference is not small.

### 9.1 Why the log-based counts don't conserve

A directly-follows pair `(a, b)` exists only when `b` immediately follows
`a` **in the trace**. Two structures break that:

* **Concurrency.** On a parallel block the activity that actually follows
  `a` is usually one from a *sibling* branch — a transition the model has
  no edge for. The traversal is counted nowhere, so `a`'s outgoing edge
  reads far less than `a` itself.
* **Silent skips.** A `tau` branch produces no event, so a choice whose
  alternative is "do nothing" has no pair to count on that side: the
  observable branch reports **100 %** no matter how rare it is.

Both were observed on a 258-case clinical log: an activity with 257
executions whose only outgoing edge read `40`; a fork with 197 inflow
whose choice split `25`/`4`; and a death branch reported at `100 %` where
the true figure is `25 %` (64 of 258).

### 9.2 Definition

Traversal metrics count how often the log **walks the model**, by
replaying each case on the process tree the model was built from. A
process tree is block-structured, so a replay gives an exact count per
tree node, and the counts conserve by construction:

| Metric | Definition |
|--------|------------|
| `traversal_frequency` *(activities and edges)* | number of times the counted cases walk this element. A node inside a loop body counts once per iteration, including under **nested** loops — a loop entered `V` times whose body ran `D` times in total executes its `do` branch `D` times and its `redo` branch `D − V`. |
| `traversal_percentage` *(OR-fork branches)* | the branch's share of its fork's own traversals, rendered **with the base it divides** (`25% of 258`). |

Identities that hold by construction — each is asserted in
[`tests/test_traversal.py`](../tests/test_traversal.py):

* an activity's count equals the count on its incoming and on its
  outgoing edge;
* every branch of a **parallel** fork carries the fork's inflow (each
  case runs all of them);
* the branches of a **choice** sum to the fork's own count, so
  `traversal_percentage` over one fork sums to 100 %;
* a silent branch is counted like any other.

On a log whose cases all fit the tree, these counts are not merely
self-consistent: each activity's `traversal_frequency` equals the number
of times it was **actually observed**. That equality is checked against
both bundled sample logs and holds at every noise threshold.

Counts are attributed to model elements through provenance: every node
and connection the tree→UCM converter emits records the `id()` of the
subtree it came from. The counts are keyed by that subtree, so they
survive decomposition into plug-in maps (a stub carries the count of the
subtree it stands for).

### 9.3 Coverage — the number to report alongside

A trace only has an exact parse if it **fits** the tree, and a model
mined with a noise threshold deliberately discards infrequent behaviour.
Fit is therefore a property of *how much you simplified*, not of the
counting. On the clinical log above:

| `noise_threshold` | cases fitting exactly |
|---|---|
| 0.0 | 258 / 258 (100 %) |
| 0.1 | 191 / 258 (74 %) |
| 0.2 | 144 / 258 (56 %) |

Two strategies, selected by `repair`:

* **`repair=False`** — count only the cases that fit. Every number is a
  real observed case, but they describe the fitting sub-log alone and
  systematically under-report.
* **`repair=True`** *(default)* — additionally align each non-fitting
  case to its nearest path through the model and count that path.
  Coverage reaches the whole log. The trade-off is that a *repaired*
  path is counted rather than an observed one: for an activity the model
  treats as mandatory, alignment inserts the missing execution, so
  `traversal_frequency` can exceed the observed `frequency`. That gap is
  meaningful — it is how much the model over-claims.

`TraversalStats` reports `fitting_cases`, `repaired_cases`,
`unexplained_cases`, `coverage` and `fitting_ratio` so a consumer can
state what the numbers cover. **`fitting_ratio` is the one to show a
reader**; the web app puts it under the model's metrics row and, below
70 %, points at the noise threshold as the knob that changes it.

### 9.4 Cost

Traversal counts are a function of the *parse*, not of the trace, so the
work is deduplicated twice: once per distinct activity sequence (which is
what saves the replay itself) and again per concurrency-aware signature
(which is what the counts are keyed on). On the clinical log, 258 cases
reduce to 111 distinct sequences and 14 signatures. Alignment repair
likewise runs once per distinct non-fitting sequence.
