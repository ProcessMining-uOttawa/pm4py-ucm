# Model families and performance overlays

This document covers the family feature sets added in v0.4.0–v0.5.0:

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

Everything here is also available point-and-click in the V3 web app
(`web/streamlit_app_v3.py`: the **Family** tab, the **Compare** tab,
and the **Performance overlay** sidebar section).

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
    min_cases=20,                 # skip combinations with fewer cases
    bins=4,                       # numeric attributes → quantile ranges
)

# One .jucm per combination + family_summary.csv (zip or directory)
pm4py_ucm.write_ucm_family(family, "family.zip")

# All combinations rendered side by side: a stack for one attribute,
# a matrix (rows × columns) for two, with n / % captions per cell.
pm4py_ucm.save_vis_ucm_family(family, "family_grid.png")

# One overarching model: shared skeleton + dynamic stubs at the
# points of divergence, one strategy per combination.
umbrella = pm4py_ucm.assemble_ucm_family(family, mode="umbrella")
pm4py_ucm.write_ucm(umbrella, "family_umbrella.jucm")

# Or: every combination as an independent root map in one file,
# with shared responsibility/component definitions.
combined = pm4py_ucm.assemble_ucm_family(family, mode="combined")
pm4py_ucm.write_ucm(combined, "family_combined.jucm")
```

### Partitioning

`discover_ucm_family` detects **case-constant attributes** with the
same machinery the data-driven scenario synthesis uses (event-level
columns constant per case are lifted automatically, as in real-world
XES exports). Per attribute type:

| Type          | Partitioning                                                                    |
|---------------|---------------------------------------------------------------------------------|
| enumeration   | one cell per value, **case-insensitively** — `F` and `f` are the same value, displayed as the log's most frequent spelling (every merged spelling stays on `PartitionValue.raw_values`); past `max_values_per_attribute` the least frequent values merge into an **Other** bucket |
| boolean       | `true` / `false` (any letter case)                                               |
| numeric       | **binned** into ranges — `bins` quantiles, or explicit `bin_edges={attr: [edges]}` |
| missing value | an **Unknown** bucket (`unknown_bucket=False` drops those cases)                 |

Additional policy knobs:

- `min_cases` — observed combinations below the threshold are *skipped*
  (recorded on the family and shown grayed in the grid) rather than
  mined into overfitted micro-models;
- `include_values={attribute: [labels]}` — restrict an attribute to the
  listed values (labels as they appear on the axis, including
  `Other` / `Unknown` and range labels like `"18-39"`, matched
  case-insensitively); other cases are dropped;
- `ignore_value_case=False` — opt out of the case-insensitive value
  merging for logs whose codes are genuinely case-significant;
- `noise_threshold` — forwarded to the inductive miner per cell;
- `decomposition` — applied per cell, so the family is flat or
  hierarchical exactly like a single model would be.

The result is a `ModelFamily`: one standalone `UCM` per cell plus the
partition axes, per-cell case lists, coverage bookkeeping, and each
cell's mined `{activity: performer}` mapping.

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
(combination × behavioural variant, capped by
`max_variants_per_cell`). Each scenario initialises:

- the attribute variables → plug-in selection at every dynamic stub;
- a `family_variant` enumeration value → branch selection at every
  outside-loop OR-fork (`family_variant == AUS_v2` disjunctions);
- per-loop iteration counters (capped by `max_loop_iterations`) →
  deterministic loop traversal via the same entry-guard / decrement
  scaffolding as `discover_scenarios`.

Inside-loop two-way XORs get combined `family_variant` +
counter-range conditions (branches distributed across iterations by
the observed per-variant proportions); XORs with more than two
branches inside loops fall back to a deterministic split — the same
behaviour as the single-model synthesizer. Uncovered low-frequency
variants are noted on the scenario group. `path_scenarios=False`
restores one plain configuration strategy per combination.

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
is recorded in the PNG metadata (`pm4py_ucm_dpi`).

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

### Metrics

| Layer      | Metric          | Meaning                                                            |
|------------|-----------------|---------------------------------------------------------------------|
| activities | `frequency`     | number of executions (events) — `n` exceeding `case_coverage` instantly shows loop rework |
| activities | `case_coverage` | number of cases containing the activity                             |
| activities | `mean/median/total_time` | activity **service time** — requires an *interval* log (a `start_timestamp` column); silently omitted otherwise |
| edges      | `frequency`     | directly-follows traversals of the edge's segment                   |
| edges      | `percentage`    | an OR-fork branch's share of the fork's traversals                  |
| edges      | `mean/median/total_time` | **waiting time** between the segment's activities (completion → next start for interval logs, completion → completion otherwise) |

### Segment attribution

A UCM edge is not a DFG edge, so statistics are attributed via
**segments**: every arc is walked backward and forward — through
routing bends, empty points, joins (backward: *all* predecessor
branches), forks (forward: *all* outgoing branches), and static
single-binding stubs (into and out of the plug-in, so decomposed
models get edge statistics across stub boundaries). The edge's
statistics are the aggregate of the directly-follows pairs over the
two activity sets: frequencies and totals add, means are
frequency-weighted, and the median is kept only for single-pair
segments. One annotation lands on each segment's first arc. Dynamic /
multi-binding stubs stop the walk, honestly leaving those arcs
unannotated.

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

The overlay composes with families throughout: per-cell models (grid
and zip) are annotated from each cell's own sub-log, and the
assemblies accept the metric selections too —
`assemble_ucm_family(family, mode="umbrella", node_metrics=[...],
edge_metrics=[...])` annotates the shared skeleton from the **whole**
family log and each variant plug-in from its **covering cells'**
sub-log.

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

`compute_family_stats` computes, per family member, three levels of
statistics designed for *comparison across cells*:

- **Process level** — case count and share of the log, event count,
  events per case (min/mean/median/max), **case duration
  min/mean/median/max and the TOTAL across the cell's cases**,
  distinct activities, concurrency-aware behavioural variant counts,
  and replay fitness.
- **Activity level** — execution frequency, case coverage,
  **sojourn time** (time since the case's previous event ≈ waiting +
  service — derivable from *any* timestamped log, so single-timestamp
  logs get activity-level time statistics too), and (for interval
  logs carrying a `start_timestamp` column) service-time
  **min/mean/median/max/total** per activity. Metrics the log cannot
  support are omitted, never fabricated.
- **Edge level** — directly-follows activity pairs (`Register Claim →
  Quick Assessment`) with traversal frequency and waiting-time
  min/mean/median/max/total, ordered by family-wide frequency.
  Waiting is completion→start on interval logs; on single-timestamp
  logs it is completion→completion (which includes the successor's
  own duration — the report says so rather than pretending
  otherwise).
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
`process_frame()`, `activity_frame(metric)`,
`choice_count_frame(choice)` / `choice_share_frame(choice)`; JSON via
`to_dict()`.

### The interactive HTML report

`write_family_report` emits **one self-contained HTML file** — the
statistics embedded as JSON, the per-cell model images embedded as
base64 PNGs, and vanilla JavaScript for the interactivity. No CDN, no
external assets: it opens offline in any browser and can be archived
as supplementary material for a paper. Five views:

- **Overview** — sortable process-level table (click a header to
  rank) with per-column heat-mapping; every duration column includes
  the per-case aggregate *and* the cell total.
- **Compare** — pick any two family members: headline delta cards
  (absolute and percent change), the two model images side by side
  (click to zoom, or open in a separate browser tab at full
  resolution), an activity delta table (Δ and ratio, diverging
  color scale, optional per-case normalisation), and the two members'
  choice bars aligned. "Multiple windows" = open the file in several
  browser tabs, one pair each.
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
`style="bpmn"` switches the embedded images to BPMN notation.

In the V3 web app, the **Compare** tab offers the same ranking table,
pair pickers, side-by-side models, activity deltas and aligned choice
tables directly in Streamlit, plus a download button for the HTML
report (also available next to the other downloads on the Family
tab).

---

## Known limitations

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
- Variation inside commutative operators (e.g. one cell having an
  extra XOR alternative) surfaces as a whole-node variant rather than
  a finer-grained per-branch stub.
