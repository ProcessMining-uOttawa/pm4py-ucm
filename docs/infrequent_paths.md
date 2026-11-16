# Removing infrequent paths

*Design note. Built in 0.9.4: `pm4py_ucm.prune_infrequent_paths`, and
the web app's **Infrequent paths** controls.*

Log filters, decomposition and the split model all reduce what a model has
to show. Many models stay hard to read anyway, and the reason is often the
same: branches that almost no case takes. A rare alternative after an
OR-fork, an optional activity inside an AND-fork that is skipped 98% of
the time, a loop that repeats in a handful of cases.

On a directly-follows graph the fix is trivial: delete the edges below a
threshold. A UCM or a BPMN model has connection nodes, so an edge cannot
simply be deleted. The whole region between a fork and its join has to go,
and the fork itself must disappear when one branch is left.

## 1. Where the pruning happens

**On the process tree, not on the UCM.** Three reasons:

1. A process tree is block-structured, so removing a child of an operator
   always leaves a valid tree. On the UCM graph the same operation means
   finding the join that matches a fork, deleting the region between them
   and repairing the connections, which is re-deriving the structure the
   tree already has.
2. The frequencies needed are already keyed on the tree: the replay-based
   traversal counts of [`metrics.md`](metrics.md) say how many cases walk
   each tree node, and they conserve (the branches of a choice sum to the
   choice).
3. Everything downstream is built from the tree: the conversion, the
   decomposition, the traversal counts, scenario synthesis, the behavioural
   variants. Pruning the tree and converting as usual keeps them all
   consistent with no change to any of them.

```python
import pm4py, pm4py_ucm

tree = pm4py.discover_process_tree_inductive(log, noise_threshold=0.0)
result = pm4py_ucm.prune_infrequent_paths(tree, log, min_share=0.05)
ucm = pm4py_ucm.convert_to_ucm(result.tree, decomposition="auto")

for b in result.removed:
    print(b.summary, b.cases, f"{b.share:.1%}", b.reason)
```

To also leave out the cases that took a removed path (§4), ask for it and
go on with `result.log`:

```python
result = pm4py_ucm.prune_infrequent_paths(
    tree, log, min_share=0.05, drop_cases=True)
ucm = pm4py_ucm.convert_to_ucm(result.tree)
pm4py_ucm.annotate_performance(ucm, result.log, ...)   # the remaining cases
```

A family or a split model prunes every one of its trees the same way,
each from its own cases (§5):

```python
family = pm4py_ucm.discover_ucm_family(
    log, ["region"], prune={"min_share": 0.05, "drop_cases": True})
ucm = pm4py_ucm.discover_ucm_by_attribute(
    log, "service", prune={"min_share": 0.05})
```

## 2. What is removed

Only two places in a tree are decision points, and only those are judged:

| Decision point | What is judged | Its share |
|---|---|---|
| A choice (`X`, or the inclusive `O`) | each branch, a silent "skip" branch included | the branch's traversals over the choice's |
| A loop | the redo part | the redo's traversals over the body's, i.e. the fraction of body executions followed by a repeat rather than the exit |

A branch is removed when it fails **either** active test:

- `min_cases`: fewer than this many distinct cases walk it. `1` removes
  only what is never walked.
- `min_share`: its share is below this fraction (`0.05` is 5%).

The absolute test counts **cases** and the relative one counts
**traversals**, on purpose. One case that goes round a loop ten times is
ten traversals of the redo and one case: the share describes the decision
point, the case count describes how much of the log is behind it.

**The branches of a parallel block are never judged directly.** Each one
carries the fork's full inflow, so none is locally infrequent. What reads
as an infrequent path after an AND-fork is an *optional* branch,
`+(A, X(B, tau))` with `B` rare. Removing `B` leaves a silent branch, and
the clean-up below takes it out of the fork.

**The most frequent branch of a choice is always kept**, so a high
percentage can never empty a block.

## 3. The clean-up

After the removals, the tree is reduced bottom-up. This is the recursive
part: one removal can make its parent disappear, and that one its own
parent.

- A choice left with one branch is replaced by that branch.
- Several silent branches of a choice merge into one.
- Silent children of a sequence or of a concurrent block are dropped. A
  block left with one child is replaced by it; with none, it becomes
  silent itself.
- A loop whose redo was removed is replaced by its body.

So in `S, +(A, X(B, tau)), E` with `B` in 3% of cases and a 5% threshold:
`B` goes, the choice is silent, the AND-fork has one branch left, and the
model is the plain sequence `S, A, E`.

## 4. Decisions

**Nested choices of the same kind are judged as one decision.**
`X(a, X(b, c))` is treated as `X(a, b, c)`. Otherwise a branch's share
would depend on how the miner happened to nest it: `c` could be half of
the inner choice and 5% of the real one. The pruned tree comes back
flattened in this respect even when nothing is removed.

**Thresholds are evaluated once, on the original counts.** The surviving
branches' percentages are not recomputed and judged again. The result is
therefore monotone: raising a threshold only removes more. Re-thresholding
after each removal can snowball (removing the 5% branch of an 80/15/5
choice makes 15 read as 16, and so on) and makes a slider unpredictable.

**Keeping or dropping the affected cases is the user's choice.** The
cases concerned are those whose path through the *original* tree walks a
removed branch (for a case that did not fit, its repaired path).
`PruneResult.affected_cases` counts them exactly, because the traversal
counts record which tree nodes each distinct activity sequence walks.

- **Keep** (the default). The log is not filtered. Those cases no longer
  fit the pruned model; with the default repair, the traversal counts
  place them on their nearest remaining path, and the fitting ratio
  reports the loss. This is the DFG behaviour, where filtering an edge
  does not drop cases.
- **Drop** (`drop_cases=True`, `PruneResult.filter_log`). Those cases are
  left out, as a log filter would do. Every case that fitted the original
  tree and remains still fits the pruned one: the removal makes no
  remaining case non-fitting, and percentages add up over those cases.
  The price is that the model, its overlays, the scenarios and the
  dashboards describe a sub-log, which the app states where it reports the
  pruning.

Cases that could not be counted at all on the original tree (no fit, no
repair within the budget) walk nothing as far as the counts know, so they
are never dropped.

**This is not the miner's noise threshold.** The inductive miner's
threshold filters directly-follows pairs *while* it looks for structure,
so it changes which structure is found, and its effect on a given branch
is hard to predict. Pruning acts on the finished tree with measured
frequencies: the structure of what remains does not change, and what was
removed is listed. The two combine well: mine at a low noise threshold for
a faithful structure, then prune.

## 5. Families and split models

`discover_ucm_family(..., prune={...})` and, through it,
`discover_ucm_by_attribute(..., prune={...})` prune every cell's tree
right after it is mined. The dict takes `min_cases`, `min_share`,
`prune_loops` and `drop_cases`.

- **Each cell is judged on its own cases.** A branch that is 5% of the
  whole log may be half of one cell and absent from another; it is removed
  only where it is infrequent. That is the point of a family, and it means
  cells can differ after pruning where they did not before.
- **`min_cases` is a number of cases per cell**, so the same value bites
  harder on a small cell. `min_share` is the scale-free one.
- **With `drop_cases`**, the dropped cases leave their cell (`case_ids`,
  `n_cases`, `coverage`) and the family's log, so the statistics, the
  overlays and the assemblies describe the remaining cases.
  `cell.pruned_cases` and `family.pruned_cases` count them;
  `family.total_cases` stays the log's. A cell is never emptied: if every
  one of its cases walked a removed path, they all stay.
- `cell.prune` is that cell's `PruneResult`.

## 6. Caveats

- **Pruning needs a replay of the unpruned tree**, which is the costly
  step on a large log (see [`miner_performance.md`](miner_performance.md)).
  Pass `stats=` to reuse traversal counts you already have for that tree.
  The costly part of that replay is aligning the cases that do not fit.
  Those alignments are kept (`PruneResult.repair_hints`) and reused when
  the log is replayed on the pruned tree: a nearest path that avoided the
  removed branches is still a nearest path. Only the sequences whose path
  walked a removed branch, and those the first pass ran out of time for,
  are aligned again.
- **The same settings can remove a slightly different set of paths from
  one run to the next** on a log that fits poorly, because the alignment
  is bounded by wall-clock time and what it does not reach is not counted.
  Seen on a 10,004-case log split 11 ways: 166 paths in one run, 169 in
  the next. It only happens where the budget is exhausted. Families and
  split models therefore scale the budget to each model's size (one
  second per 50 distinct sequences, 10 s to 60 s), which makes it rarer:
  165 and 164 on that log, with about 93 cases left uncounted instead of
  about 470.
- **Counts on a poorly fitting tree are partly repaired counts.** On a
  model mined with a high noise threshold many cases do not fit, and their
  traversals are those of the nearest path. The pruning decisions are only
  as good as that fit; `repair=False` restricts them to the fitting cases.
- **The replay parses an inclusive choice one branch at a time**, so the
  share of an `O` branch is the same quantity as for an `X`.
- **An activity is only removed with a branch.** An activity that is rare
  because its whole block is rarely *reached* is caught by `min_cases`, not
  by `min_share`, since locally it may be 100% of its decision.

## 7. In the web app

The sidebar's **Infrequent paths** checkbox, under the miner settings,
works like *Log filters*: ticking it opens the section and switches the
removal on, unticking it closes the section and switches it off. Inside
are *Minimum branch share (%)*, *Minimum cases per branch* and the
checkbox *Drop the cases on removed paths*; both thresholds at 0 removes
nothing. Closing the section remembers its settings for the session.

- **Every view uses it.** The Model, Scenarios and Dashboards views use
  the pruned tree, so scenarios, behavioural variants and the fitness
  figure agree with the model shown. A split model (*Split by attribute*)
  and the Family and Compare views prune each of their models from its own
  cases (§5).
- **With the checkbox on**, the same views work from the log without the
  dropped cases: the case and event counts, the overlays, the scenarios,
  the dashboards and the Model view's event-log export.
- The Family view's coverage preview is computed before mining, so it
  cannot anticipate the dropped cases; it says so when pruning is on.
- The Model, Family and Compare views say what was removed, in how many
  models, and how many cases were kept as non-fitting or dropped.
- The unpruned tree's replay is shared and cached: moving a control
  re-prunes without replaying it again. Traversal metrics then need a
  replay of the pruned tree, as for any model.
- Pruning goes through the same replay gate as the traversal metrics. If
  the replay is declined, nothing is pruned and the Model view says so.
- A saved project stores `prune_min_cases`, `prune_min_share` (the share
  as a fraction) and `prune_drop_cases`, and the exported script
  reproduces the pruning through its `prune_tree` and `prune_arg` helpers.
  Saved while the section is closed, the thresholds are stored as 0; a
  project opens the section exactly when it has a threshold.

## 8. Not built

- A preview that marks what a threshold *would* remove before removing it.
- A report of the removed branches as a download.
