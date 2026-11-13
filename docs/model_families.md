# Model families and performance overlays

A user guide to comparing one process across case attributes. It
describes the library as released; the exact definition of every number
mentioned here is in [`metrics.md`](metrics.md). Three feature sets:

1. **Model families** — partition an event log by the values of one or
   two case-level attributes (e.g. *cancer type* × *age group*), mine
   one UCM per combination, and assemble the results: as separate
   models, as one combined multi-map model, or as a single
   **overarching model** whose dynamic stubs select variant sub-maps
   through conditions over the chosen attributes.
2. **Performance overlays** — frequencies and times computed from the
   log and displayed on activities and edges (PNG) and exported as
   jUCMNav metadata (`.jucm`).
3. **Family statistics reports** — comparative statistics for every
   family member (process, activity, and choice level) and a
   self-contained interactive HTML report for ranking the combinations
   and comparing any two side by side.

Everything here is also available point-and-click in the web app
([`web/`](../web/README.md)): the **Family** and **Compare** views, and
the performance-overlay controls of the Model view.

---

## 1. Model families

### Motivation

Many logs mix cases that follow *different processes*: a cancer-care
log contains distinct pathways per cancer type; a claims log may route
work differently per country or product. Mining one model over such a
log yields an unreadable union; mining fully separate models loses
what the pathways share. Model families give you both views — and the
overarching *umbrella* model expresses the family in native URN terms:
**dynamic stubs are variation points, plug-in maps are variants, and
scenario strategies are configurations**.

### Quick start

```python
import pm4py
import pm4py_ucm

log = pm4py.read_xes("cancer_log.xes")

family = pm4py_ucm.discover_ucm_family(
    log,
    ["cancer_type", "age"],       # 1 or 2 case-level attributes
    decomposition="auto",         # per-cell, same as discover_ucm_inductive
    min_cases=20,                 # skip combinations with fewer cases (default 10)
    bins=4,                       # numeric attributes → quantile ranges (default)
)

# One .jucm per combination + family_summary.csv (zip or directory)
pm4py_ucm.write_ucm_family(family, "family.zip")

# All combinations rendered side by side: a stack for one attribute,
# a matrix (rows × columns) for two, with n / % captions per cell.
pm4py_ucm.save_vis_ucm_family(family, "family_grid.png")   # or .svg

# One overarching model: shared skeleton + dynamic stubs at the
# points of divergence, one strategy per combination.
umbrella = pm4py_ucm.assemble_ucm_family(family, mode="umbrella")
pm4py_ucm.write_ucm(umbrella, "family_umbrella.jucm")

# Or: every combination as an independent root map in one file,
# with shared responsibility/component definitions.
combined = pm4py_ucm.assemble_ucm_family(family, mode="combined")
pm4py_ucm.write_ucm(combined, "family_combined.jucm")
```

`write_bpmn_family(family, "family.zip", assembled=umbrella)` writes the
same family as BPMN 2.0 — see [`bpmn_export.md`](bpmn_export.md).

### Choosing the attributes

Which attribute yields genuinely different processes, rather than
noise? `rank_partition_attributes` answers from the log, with no model
mined and no LLM:

```python
for score in pm4py_ucm.rank_partition_attributes(log)[:3]:
    print(score.label, score.score, score.rationale)
```

It returns one `AttributeScore` per case attribute, best first. Each
attribute is first bucketed onto a bounded axis (its 8 most frequent
values plus *Other*; quantile ranges for a numeric with more than 10
distinct values), then scored on two signals in `[0, 1]`:

- `divergence` — normalised mutual information between the attribute and
  the case's activity sequence, `I(A; variant) / min(H(A), H(variant))`,
  with the sequences beyond the 30 most frequent pooled: how cleanly the
  attribute's values map onto behaviour;
- `effect` — η² of case duration (first to last event) across the
  attribute's values: the share of duration variance that lies between
  them.

`score = 0.6 · divergence + 0.4 · effect`, multiplied by 0.25 when the
attribute is flagged *high-cardinality* (more distinct values than
`max(20, half the cases)` — an identifier, not a determinant) or *nearly
constant* (its largest bucket holds over 95% of cases). `flags` also
notes an attribute present in under half the cases; `rationale` is the
one-line reading; `to_row()` gives a flat dict for a table. The ranking
advises — `discover_ucm_family` accepts any detected attribute.

### Partitioning

`discover_ucm_family` detects **case-constant attributes** with the
same machinery the data-driven scenario synthesis uses (event-level
columns constant per case are lifted automatically, as in real-world
XES exports). Per attribute type:

| Type          | Partitioning                                                                    |
|---------------|---------------------------------------------------------------------------------|
| enumeration   | one cell per value, **case-insensitively** — `F` and `f` are the same value, displayed as the log's most frequent spelling (every merged spelling stays on `PartitionValue.raw_values`); past `max_values_per_attribute` the least frequent values merge into an **Other** bucket |
| boolean       | `true` / `false` (any letter case)                                               |
| numeric       | **binned** into ranges — `bins` quantiles, or explicit `bin_edges={attr: [edges]}`. At most `bins` distinct whole numbers are kept as one bin per value instead, so levels `1..4` stay `1`, `2`, `3`, `4` |
| missing value | an **Unknown** bucket (`unknown_bucket=False` drops those cases)                 |

A column is numeric when its **values** are, not when its dtype says so:
XES exports routinely serialise numbers as strings, so an object column
is coerced first and treated as numeric when at least 99% of its
non-null values parse. Values that do not parse become missing, and land
in the **Unknown** bucket. The share is `numeric_coercion_threshold`:
pass `discover_ucm_family(..., numeric_coercion_threshold=1.01)` to keep
a column of numeric strings (codes, say) as an enumeration instead.
`max_enum_cardinality` (default 40) is the most distinct values a string
attribute may have and still be offered as an enumeration.

Two consequences worth knowing before you partition:

- A numeric-looking column is an axis of **ranges**, not of discrete
  values, once it holds more distinct values than `bins`. If you want
  the individual values, raise `bins` to at least that count, or pass
  explicit `bin_edges`.
- Columns naming an *entity* — `org:resource`, `org:group`, `org:role`,
  `concept:instance` — are never coerced however numeric they look,
  because their digits are labels and a range like `541-561` over them
  means nothing. They partition as enumerations.
- An enumeration with more than 40 distinct values is not offered as a
  partition attribute at all; naming one raises a `ValueError` listing
  the attributes that were detected.

The threshold is deliberately strict. A column of treatment codes
`"1"`/`"2"`/`"3"` that writes combinations as `"2,3"` is 97.7% numeric,
and coercing it would file every combination case under **Unknown**
rather than giving it a cell; a column whose only non-numeric value is a
single `"na"` sits at 99.6% and is genuinely numeric.

Additional policy knobs:

- `min_cases` (default 10) — observed combinations below the threshold
  are *skipped* (recorded on the family and shown grayed in the grid)
  rather than mined into overfitted micro-models; if no combination
  reaches it, `discover_ucm_family` raises a `ValueError` listing the
  combinations it saw;
- `max_values_per_attribute` (default 12) and `other_bucket` — the cap
  past which an enumeration's least frequent values merge into
  **Other**; with `other_bucket=False` those cases are dropped instead;
- `include_values={attribute: [labels]}` — restrict an attribute to the
  listed values (labels as they appear on the axis, including
  `Other` / `Unknown` and range labels like `"18-39"`, matched
  case-insensitively); other cases are dropped;
- `ignore_value_case=False` — opt out of the case-insensitive value
  merging for logs whose codes are genuinely case-significant;
- `noise_threshold` — forwarded to the inductive miner per cell;
- `decomposition` — applied per cell, so the family is flat or
  hierarchical exactly like a single model would be;
- `parameters` — converter and resource parameters applied to every cell
  (`resource_attribute`, `urn_name`, …);
- `validate` (default `True`) — check each cell's model as it is mined
  and raise a `ValueError` naming the cell if it is malformed;
- `progress_callback(stage, done, total)` — fired after each mined cell.

Axis values are ordered deterministically — sorted values, ranges
ascending, **Other** and **Unknown** last — so repeated runs give
identical partitions, map names and exports.

The result is a `ModelFamily`: one standalone `UCM` per cell
(`family.cells`, each with its `labels`, `case_ids`, `n_cases`,
`coverage`, `tree` and mined `{activity: performer}` mapping) plus the
partition axes (`attributes`) and the coverage bookkeeping
(`total_cases`, `covered_cases`, `dropped_cases`, `skipped_cells`).
`family.cell("Breast", "40-59")` looks a cell up by its labels. The
family keeps the log (`log_df`), which the assemblers and the statistics
need.

`write_ucm_family` names each file after its combination
(`cancer_type=Breast_age=40-59.jucm`); `family_summary.csv` lists every
combination with its case count, coverage and file, skipped ones
included.

### The umbrella (overarching model)

`assemble_ucm_family(family, mode="umbrella")` anti-unifies the
per-cell process trees into a **shared skeleton**:

- identical subtrees (canonical signature) are shared verbatim on the
  root map;
- sequences share their longest common prefix and suffix of children;
  equal-length remainders merge position-wise (several *localized*
  variation points), unequal-length remainders become one variation
  point — a cell whose remainder is empty gets a pass-through **skip**
  plug-in;
- loops merge on (do, redo) separately;
- anything else that differs becomes a variation point wholesale
  (no partial alignment of commutative operators — wrong pairings
  would produce misleading skeletons).

Each variation point materialises as a **dynamic stub** whose plug-ins
are the distinct variant sub-maps, guarded by preconditions over
enumeration/boolean scenario variables derived from the attributes
(`cancer_type == Breast && age_group == _40_59`). Plug-in names carry
the values they cover (`Register Claim [AUS | NZL]`).

**Resource variation counts as variation** (default,
`resource_variation=False` disables): an activity performed by
different actors in different cells becomes a variation point even
under identical control flow, and each variant plug-in draws the
activity inside its cells' actor. The binding is *visual*
(`RespRef.cont_ref`); the shared `Responsibility.performer` definition
is set only for activities the whole family agrees on.

**Deduplication** (default): behaviourally identical variants share a
single plug-in whose selection condition is the OR of the member
cells' clauses, *factored over the attribute domains* — when a value's
variants cover every value of the second attribute, that attribute
drops out of the condition entirely. The shared plug-ins show which
sub-populations follow the same process.

When nothing is shared at the root — or with `skeleton=False` — the
umbrella degenerates to a single `start → dynamic stub → end` root map
with whole cell models as plug-ins. A family whose cells are identical
in both control flow and performers emits a warning instead of
silently producing a stub-less model.

### Path scenarios

By default (`path_scenarios=True`) the umbrella's strategies are
**executable path scenarios**: each cell's sub-log is replayed
(concurrency-aware variant clustering) on the cell's *configured tree*
— the skeleton with each variation point substituted by the cell's
variant subtree — and one `ScenarioDef` is emitted per
(combination × behavioural variant, the most frequent first, capped by
`max_variants_per_cell`, default 5). Each scenario initialises:

- the attribute variables → plug-in selection at every dynamic stub;
- a `family_variant` enumeration value → branch selection at every
  outside-loop OR-fork (`family_variant == AUS_v2` disjunctions);
- per-loop iteration counters (capped by `max_loop_iterations`,
  default 2) →
  deterministic loop traversal via the same entry-guard / decrement
  scaffolding as `discover_scenarios`.

Inside-loop two-way XORs get combined `family_variant` +
counter-range conditions (branches distributed across iterations by
the observed per-variant proportions); XORs with more than two
branches inside loops fall back to a deterministic split — the same
behaviour as the single-model synthesizer. Uncovered low-frequency
variants are noted on the scenario group. `path_scenarios=False`
restores one plain configuration strategy per combination, which is also
the fallback when the family's log has been dropped; `strategies=False`
emits none.

Both assembly modes check the finished model (`validate=True`) and raise
a `ValueError` if it is not structurally well-formed.

### Grid rendering and resolution

`save_vis_ucm_family` renders a vertical **stack** (one attribute) or
a rows × columns **matrix** (two attributes) with per-cell
`n=… (…%)` captions and grayed placeholders for skipped/empty
combinations. Resolution adapts so exported text stays readable:
rendering aims for `target_dpi` (default 192) and backs off toward the
96-dpi floor only when the composite would exceed `max_total_pixels`
(default 150M, enforced exactly after rendering by supersampled
downscaling); the floor wins over the budget with a warning. Pass
`parameters={"dpi": …}` to pin an exact resolution. The effective DPI
is recorded in the PNG metadata (`pm4py_ucm_dpi`). PNG output needs
Pillow.

A target ending in `.svg` writes the same grid as one vector document
instead — crisp at any zoom, with selectable text and none of the DPI
machinery. `style="bpmn"` draws either in BPMN notation.
`view_ucm_family` renders to a temporary PNG and opens it (Windows).

---

## 2. Performance overlays

### Quick start

```python
ucm = pm4py_ucm.discover_ucm_inductive(log)
pm4py_ucm.annotate_performance(
    ucm, log,
    node_metrics=["frequency", "median_time"],   # up to 2 recommended
    edge_metrics=["percentage", "mean_time"],
)
pm4py_ucm.save_vis_ucm(ucm, "annotated.png")     # gray overlay text
pm4py_ucm.write_ucm(ucm, "annotated.jucm")       # metadata in jUCMNav
```

The default selection is `frequency` on both layers. Re-annotating
replaces the previous overlay; an empty sequence switches a layer's
display off. `parameters` may rename the log columns (`case_id_col`,
`activity_col`, `timestamp_col`, `start_timestamp_col`).

### Metrics

Each metric is defined in [`metrics.md`](metrics.md) (§2 activities, §3
edges, §9 traversal); this is the list of names
`annotate_performance` accepts.

| Layer | Names | Needs |
|---|---|---|
| activities | `frequency`, `case_coverage`, `relative_frequency`, `repeat_frequency` | any log |
| activities | `sojourn_<agg>_time` — time since the case's previous event | timestamps |
| activities | `<agg>_time` — **service time** | an *interval* log (a `start_timestamp` column); silently omitted otherwise |
| edges | `frequency`, `case_frequency`, `relative_frequency` — directly-follows counts of the edge's segment | any log |
| edges | `percentage` — an OR-fork branch's share of the fork's directly-follows traversals | an OR-fork branch |
| edges | `<agg>_time` — **waiting time** between the segment's activities | timestamps |
| both | `traversal_frequency`; edges also `traversal_percentage` | a replay, below |

`<agg>` is one of `mean`, `median`, `min`, `max`, `std`, `p90`, `p95`,
`total`. The full tuples are `NODE_METRICS` and `EDGE_METRICS` in
[`algo/performance.py`](../pm4py_ucm/algo/performance.py); an unknown
name raises a `ValueError`.

### Counts that add up: the traversal metrics

`frequency` on an edge counts event *adjacency*, which concurrency and
silent skips break — an activity with 257 executions can show `40` on
its only outgoing edge ([`metrics.md` §9](metrics.md#9-traversal-metrics-replay-on-the-model)).
The traversal metrics count how often the log walks the model instead,
and need the process tree the model was built from:

```python
tree = pm4py.discover_process_tree_inductive(log)
ucm = pm4py_ucm.convert_to_ucm(tree)
traversal = pm4py_ucm.compute_traversal_stats(tree, log)

pm4py_ucm.annotate_performance(
    ucm, log,
    node_metrics=["traversal_frequency", "median_time"],
    edge_metrics=["traversal_percentage", "mean_time"],
    traversal=traversal, tree=tree,
)
print(f"{traversal.fitting_ratio:.0%} of cases fit the model exactly")
```

Without `traversal` and `tree` those two metrics are simply absent.
`fitting_ratio` is the figure to report beside them: a model mined with
a noise threshold explains only part of its log.

### Reading the overlay

Activities: `n=5600` (frequency or traversal frequency), `5600 cases`
(coverage), `12%` (relative frequency), `rpt 31` (repeats), then times
as `avg`, `med`, `min`, `max`, `sd`, `p90`, `p95`, `sum`, prefixed `soj`
for sojourn. Edges: a bare count, `1742c` for distinct cases, a
percentage, `50% of 5600` for a traversal share with the base it
divides, and the same time prefixes. Durations use the largest fitting
unit (`s`, `m`, `h`, `d`, `y`), in the display string and in the
stored `perf_<metric>` values alike.

### Segment attribution

A UCM edge is not a DFG edge, so the directly-follows statistics are
attributed via **segments**: every arc is walked backward and forward to
the nearest activities — through routing bends, empty points, joins,
forks and static single-binding stubs, so decomposed models get edge
statistics across stub boundaries — and the edge carries the aggregate
of the pairs between the two activity sets. One annotation lands on each
segment's first arc. Dynamic / multi-binding stubs stop the walk,
honestly leaving those arcs unannotated. The aggregation rules, and what
a multi-pair segment cannot report, are in
[`metrics.md` §3.1–3.4](metrics.md#31-segment-attribution-how-an-edge-maps-to-activity-pairs).

### Storage: how the overlay lives in the model

Two metadata layers (re-annotating replaces both):

- `perf_<metric>` on RespRefs and — **branch-indexed** —
  `perf_branch<i>_<metric>` on the source node of each annotated arc:
  *every* available metric, one entry per line, independent of the
  display selection. jUCMNav lists them line by line in the properties
  view of the exported `.jucm`.
- `_perf` / `_perf_branch<i>`: the display strings for the *selected*
  metrics, rendered by the visualizer as a small gray line under
  activity names and on edges (UCM and BPMN styles alike).

> **Format note:** jUCMNav's metamodel has **no metadata feature on
> connections** — that is why edge annotations are stored on the arc's
> source node under branch-indexed keys (`branch<i>` = the node's
> *i*-th outgoing arc, stable across export/import). Emitting
> `<metadata>` under `<connections>` makes jUCMNav reject the file.

### Families

The assemblies accept the metric selections —
`assemble_ucm_family(family, mode="umbrella", node_metrics=[...],
edge_metrics=[...])` annotates the shared skeleton from **all** the
family's cells and each variant plug-in from its **covering cells'**
sub-log; `mode="combined"` annotates each cell's maps from that cell.
Traversal metrics appear only on maps that belong to a single cell: a
map merged from several cells has no one tree to replay on.

The standalone per-cell models (`cell.ucm` — what the grid and
`write_ucm_family` use) are *not* annotated by `discover_ucm_family`.
Annotate them before rendering or writing:

```python
case = family.log_df["case:concept:name"].astype(str)
for cell in family.cells:
    pm4py_ucm.annotate_performance(
        cell.ucm, family.log_df[case.isin(set(cell.case_ids))],
        node_metrics=["frequency"], edge_metrics=["percentage"],
    )
```

---

## 3. Family statistics reports

### Quick start

```python
family = pm4py_ucm.discover_ucm_family(log, ["Country"], min_cases=10)

stats = pm4py_ucm.compute_family_stats(family)   # needs family.log_df
print(stats.process_frame().to_string())         # one row per cell

pm4py_ucm.write_family_report(
    family, "family_report.html", stats=stats,
    title="Claims — family by Country",
)
```

### The statistics (`FamilyStats`)

`compute_family_stats` computes, per family member, four levels of
statistics designed for *comparison across cells* (definitions:
[`metrics.md`](metrics.md) §2–§5):

- **Process level** — case count and share of the log, event count,
  events per case (min/mean/median/max), **case duration
  min/mean/median/max, std, P90/P95 and the TOTAL across the cell's
  cases**, distinct activities, rework (share of cases repeating an
  activity, mean repeats per case), start and end activities, the
  concurrency-aware behavioural variant count beside the plain
  sequence-variant count, and replay fitness. Variants are counted with
  loop iterations coarsened to `{0, 1, ≥2}`;
  `compute_family_stats(family, coarsen_loops=False)` tells every
  iteration count apart.
- **Activity level** — execution frequency, relative frequency, case
  coverage, repeats, **sojourn time** (time since the case's previous
  event ≈ waiting + service — derivable from *any* timestamped log, so
  single-timestamp logs get activity-level time statistics too), and
  (for interval logs carrying a `start_timestamp` column) service
  time, each as min/mean/median/max/std/P90/P95/total. Metrics the log
  cannot support are omitted, never fabricated.
- **Edge level** — directly-follows activity pairs (`Register Claim →
  Quick Assessment`) with frequency, distinct-case frequency, relative
  frequency and the same waiting-time aggregates, ordered by
  family-wide frequency. Waiting is completion→start on interval
  logs; on single-timestamp logs it is completion→completion (which
  includes the successor's own duration — the report says so rather
  than pretending otherwise). Empty for a log with no timestamps.
- **Choice level** — OR-fork branch counts **aligned across cells**:
  the per-cell trees are anti-unified into the family skeleton (the
  same merge that builds the umbrella, control-flow only) and each
  cell's sub-log is replayed on its configured tree, so "the choice
  after *Close Assessment*" is one comparable row for every
  combination. Forks are named by their context (`after Close
  Assessment_Human: Amend Assessment vs Request Customer Info`),
  flagged when they sit inside a loop (counts are then evaluations
  across iterations), and distinguished as *shared skeleton forks*
  vs *variation-point forks* (present only in some members). Cells
  that never reach a fork report "not reached" — distinct from zero.

Call it **right after mining** — it needs `family.log_df`, but the
result carries no DataFrames, so it stays small and picklable after
the log is dropped. pandas views for notebooks and apps:
`process_frame()`, `activity_frame(metric)`, `edge_frame(metric)`,
`choice_count_frame(choice)` / `choice_share_frame(choice)` (for a
`choice` in `stats.choices`); JSON via `to_dict()`. `parameters` may
rename the log columns, as for the overlay; `progress_callback` fires
per cell.

### The interactive HTML report

`write_family_report` emits **one self-contained HTML file** — the
statistics embedded as JSON, the per-cell model images embedded as
SVG, and vanilla JavaScript for the interactivity. No CDN, no
external assets: it opens offline in any browser and can be archived
as supplementary material for a paper. Six views:

- **Overview** — sortable process-level table (click a header to
  rank) with per-column heat-mapping; the duration columns give the
  per-case aggregates *and* the cell total.
- **Compare** — pick any two family members: headline delta cards
  (absolute and percent change), the two model images side by side
  (click to zoom, or open in a separate browser tab), activity and
  edge delta tables (Δ and ratio, diverging color scale, optional
  per-case normalisation), and the two members' choice bars aligned.
  To keep several pairs in view, open the file in several browser
  tabs, one pair each.
- **Activities** — the activities × members matrix for any metric,
  heat-mapped, with per-case normalisation for frequency-like
  metrics (the cells of a family routinely differ in size by an
  order of magnitude — absolute counts mislead).
- **Edges** — the directly-follows pairs × members matrix (traversal
  frequency, waiting times), busiest handovers first.
- **Choices** — every aligned OR-fork as one 100% stacked bar per
  member, colorblind-safe categorical palette, exact shares and
  counts on hover, `n` printed next to every bar.
- **Models** — the per-cell images as a browsable gallery.

The report is deterministic for a given family (no embedded
timestamps). `images=False` — or a machine without the graphviz
binary — omits the model images and keeps every statistics view.
`style="bpmn"` switches the embedded images to BPMN notation, and
`heat={…}` forwards performance heat-map settings to them. `stats`
defaults to `compute_family_stats(family)`; pass a precomputed one when
the log has been dropped. `family_report_html` (in
`pm4py_ucm.algo.discovery.families`) returns the same document as a
string.

In the web app, the **Compare** view offers the same ranking table,
pair pickers, side-by-side models, activity deltas and aligned choice
tables directly in Streamlit, plus a download button for the HTML
report (also available next to the other downloads on the Family
view).

---

## Known limitations

- A family partitions on one or two attributes, not more.
- Numeric attributes partition via bins; umbrella preconditions use an
  enumeration variable over the bin labels rather than honest integer
  range comparisons.
- Inside-loop XORs with more than two branches keep a deterministic
  `true`/`false` split (as in `discover_scenarios`).
- Activity service times need an interval log (`start_timestamp`
  column); pairing separate `lifecycle:transition` start/complete
  events is not implemented.
- Edge segments through dynamic or multi-binding stubs are not
  attributed (ambiguous by construction).
- In an assembled family, traversal metrics are available only on maps
  that come from a single cell; the shared skeleton and shared plug-ins
  carry the directly-follows metrics alone.
- Variation inside commutative operators (e.g. one cell having an
  extra XOR alternative) surfaces as a whole-node variant rather than
  a finer-grained per-branch stub.
