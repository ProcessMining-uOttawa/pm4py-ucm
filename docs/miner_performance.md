# Inductive miner and replay performance

Why some logs mine in milliseconds and others do not finish at all, which
knobs actually help (few), and how to give a user a trustworthy estimate
before they commit to a long run.

Measured 2026-08-15 against PM4Py 2.7.22.2 on twelve event logs: the seven
from the SAM 2026 evaluation plus five larger ones. Four of the five are
confidential clinical or administrative logs and appear here only as
aggregate counts under neutral labels — no institution, cohort or
diagnosis is identified, and no log content left the machine.

## Summary

| question | answer |
|---|---|
| What drives inductive-miner cost? | Distinct activity **sequences** (Spearman +0.93), not events or cases. Activity count sets the cost *per* fall-through (+0.61 on the per-variant residual). |
| Can mining time be predicted from log statistics? | **No.** Rank order, yes; a number, no. Two logs with the same activities×variants product differ by 2.8x. |
| Is there a fast path that keeps the model? | **No.** `disable_fallthroughs=True` and IMd produce structurally identical flower models. |
| Does `multi_processing=True` help? | **No — it is dangerous.** It exhausted system memory on a 274-activity log. |
| Can replay cost be predicted? | Not from structure. **Yes from a short sample**, within 0.79x–1.39x on every log tested. |

## The logs

| log | events | cases | acts | seq variants | var/cases | df density | repeat |
|---|---|---|---|---|---|---|---|
| Issue Tracker | 10 006 | 1 132 | 9 | 11 | 0.01 | 0.15 | 1.06 |
| Cancer Pathway | 1 832 | 258 | 10 | 111 | 0.43 | 0.53 | 1.00 |
| Road Traffic | 561 470 | 150 370 | 11 | 231 | 0.00 | 0.58 | 1.01 |
| Devlog | 8 739 | 285 | 10 | 267 | 0.94 | 0.71 | 6.02 |
| Claims Payment | 78 126 | 5 600 | 25 | 164 | 0.03 | 0.05 | 1.05 |
| Clinical A | 39 991 | 1 963 | 34 | 626 | 0.32 | 0.25 | 1.80 |
| Clinical B | 38 314 | 3 046 | 41 | 994 | 0.33 | 0.18 | 1.18 |
| Clinical C | 50 403 | 3 544 | 46 | 1 137 | 0.32 | 0.17 | 1.16 |
| Invenio SDLC | 23 998 | 2 455 | 30 | 1 745 | 0.71 | 0.39 | 1.64 |
| Clinical D | 160 590 | 4 572 | 37 | 2 797 | 0.61 | 0.45 | 1.42 |
| BPI 2012 | 262 200 | 13 087 | 24 | 4 366 | 0.33 | 0.22 | 2.10 |
| Registry A | 543 283 | 7 734 | **274** | 7 665 | 0.99 | 0.04 | 1.11 |

`df density` is distinct directly-follows pairs over |A|²; `repeat` is
events per distinct activity within a case.

## 1. Mining cost

All times: `discover_process_tree_inductive`, `noise_threshold=0.0`,
single process, three standard columns only.

| log | fall-throughs on | fall-throughs off | speedup |
|---|---|---|---|
| Issue Tracker | 0.03 s | 0.02 s | 2x |
| Cancer Pathway | 0.04 | 0.02 | 2x |
| Claims Payment | 0.10 | 0.10 | 1x |
| Devlog | 0.26 | 0.04 | 6x |
| Road Traffic | 0.61 | 0.61 | 1x |
| Invenio SDLC | 1.65 | 0.23 | 7x |
| Clinical B | 2.93 | 0.09 | 33x |
| Clinical C | 4.58 | 0.11 | 42x |
| Clinical A | 6.25 | 0.11 | 57x |
| Clinical D | 11.86 | 0.36 | 33x |
| BPI 2012 | 21.92 | 1.16 | 19x |
| Registry A | **>300 (killed)** | 4.87 | >62x |

The mechanism is `ActivityConcurrentUVCL._get_candidate` in
`pm4py/algo/discovery/inductive/fall_through/activity_concurrent.py`: at
every node where no cut is found it rebuilds the entire variant log once
per candidate activity and runs full cut detection on each. Cost is
therefore (how often cuts fail) x (alphabet size).

### What predicts it

Spearman rank correlation against fall-through mining time, n=12:

```
seq_variants  +0.930      len_max       +0.664      repeat_ratio  +0.476
A x V         +0.909      events        +0.636      var_ratio     +0.406
df_pairs      +0.832      len_median    +0.552      df_density    -0.203
activities    +0.762      cases         +0.545
```

Events and cases are weak: Road Traffic has 27x BPI's cases and mines 32x
faster. Activities alone are weak too: Claims (25 activities) and BPI (24)
differ by 220x.

**Multiplying activities by variants makes the ranking worse, not better**
(+0.909 vs +0.930). Invenio and Clinical C have near-identical products
(52 350, 52 302) and differ 2.8x in time; Clinical A has the lowest product
of the mid group and the highest time.

Activity count does matter, but only in the per-variant residual:

```
Cancer Pathway   10 acts    0.36 ms/variant
Invenio          30 acts    0.95
BPI 2012         24 acts    5.02
Clinical A       34 acts    9.98
Registry A      274 acts   39.14
```

Spearman(ms per variant, activities) = +0.61. **Caveat:** eleven of twelve
logs sit in a 9–46 activity band and the only log outside it (274) also has
the most variants. This sample cannot separate the two effects, so no
fitted composite from it should be trusted.

### Practical consequence

Sequence-variant count is a sound *screen* (rank order holds across five
orders of magnitude) but not an *estimator*. Under 2 000 variants every log
here mined in under 7 s; above 2 800, between 12 s and never. Within that
upper band the spread is 25x, so no ETA is defensible.

The adopted screen is therefore:

```
warn and offer log reduction when   seq_variants > 2000  OR  activities > 50
```

The variant clause is what the measurements support: it separates
{Clinical D, BPI 2012, Registry A} (11.9 s to never) from everything else
(<= 6.25 s), with a clean gap between 1 745 and 2 797 variants.

The activity clause is a **precaution, not a finding**. On these twelve logs
it fires only on Registry A, which the variant clause already catches, and
no log sits between 46 and 274 activities. It is included because the
mechanism is linear in alphabet size — a wide log with few variants would
be expensive for a reason the variant clause cannot see — but nothing here
demonstrates that, and the threshold of 50 is a judgement call.

## 2. There is no fast path that preserves the model

`disable_fallthroughs=True` suppresses only the *searching* fall-throughs.
Flower remains as the terminating last resort, so wherever a cut fails the
miner drops straight to `loop(xor(every activity), tau)`.

| log | nodes on→off | parallel | XOR | loops | max branching |
|---|---|---|---|---|---|
| Issue Tracker | 12→12 (identical) | 1→1 | 0→0 | 1→1 | 0→0 |
| Claims Payment | 45→34 | 1→0 | 6→3 | 3→3 | 2→16 |
| Road Traffic | 45→15 | 5→0 | 12→1 | 1→1 | 2→10 |
| Devlog | 66→13 | 6→0 | 15→1 | 10→1 | 2→10 |
| Invenio SDLC | 187→37 | 16→0 | 46→3 | 21→1 | 2→28 |
| Clinical B | 111→44 | 5→0 | 22→1 | 5→1 | 7→41 |
| Clinical A | 127→37 | 9→0 | 29→1 | 10→1 | 5→34 |
| Clinical D | 90→40 | 5→0 | 16→1 | 7→1 | 13→37 |
| Clinical C | 156→49 | 12→0 | 39→1 | 5→1 | 10→46 |
| BPI 2012 | 99→28 | 6→0 | 23→1 | 7→1 | 4→22 |

Clinical B's "off" tree is 41 visible leaves + 1 tau + 1 XOR + 1 loop = 44
nodes, exactly its total: a textbook flower. Clinical C, Clinical A,
Clinical D and Devlog are identical in form; the rest are within two nodes.

Every log keeps all its activities (visible leaves = activity count in all
22 trees), so a "did we lose activities?" check passes while all control
flow is gone. **Road Traffic is the clearest warning: no speedup at all
(0.61 s either way) and a flower.**

### IMd is the same tree

Passing a `DirectlyFollowsGraph` routes to IMd, which never projects logs
and so cannot reach the fall-through. It is fast — Registry A goes from
>300 s to 1.40 s total (0.86 s DFG build + 0.54 s mine), a >214x speedup —
and the DFG build is linear in events while the mine depends only on the
alphabet.

But IMd's trees are **structurally identical to the fall-throughs-disabled
trees on all ten measured fields, for all eleven logs**: same node counts,
same XOR/parallel/loop counts, same depth. Parallel nodes go to zero
everywhere except Issue Tracker.

For this library that is disqualifying rather than merely lossy: zero
parallel nodes leaves concurrency-aware variant clustering nothing to
canonicalise, and a single XOR over the whole alphabet leaves scenario
synthesis nothing to encode.

(Measured at `noise_threshold=0.0` only. Structural equality is over ten
counts, not tree isomorphism.)

## 3. Do not use `multi_processing=True`

PM4Py queues one `apply_async` per candidate activity, each carrying the
whole log. On a spawn platform every worker re-imports numpy/OpenBLAS. At
274 activities this exhausted system memory — `OpenBLAS error: Memory
allocation still failed after 10 retries` and workers unable to import
pandas — rather than finishing faster. A wall-clock timeout does not
protect against this; the failure is memory, not time.

`constants.ENABLE_MULTIPROCESSING_DEFAULT` is `False`, and
`MULTI_PROCESSING_LOWER_BOUND = 20` means the pool is only considered above
20 activities — i.e. exactly where it is most likely to hurt.

## 4. Replay cost

Concurrency-aware choice-signature clustering,
`clustering.cluster(df, tree, coarsen_loops=True)` on the
fall-throughs-enabled tree at `noise_threshold=0.0`.

| log | cases | nested loops | depth | replay | ms/case | fitness |
|---|---|---|---|---|---|---|
| BPI 2012 | 13 087 | 6 | 3 | 1 066.0 s | **81.45** | 96.7% |
| Clinical D | 4 572 | 6 | 4 | 33.0 | 7.21 | 100% |
| Clinical A | 1 963 | 7 | 2 | 12.0 | 6.13 | 100% |
| Invenio SDLC | 2 455 | **0** | 1 | 3.8 | 1.55 | 100% |
| Devlog | 285 | **0** | 1 | 0.4 | 1.53 | 100% |
| Clinical C | 3 544 | 2 | 2 | 4.4 | 1.24 | 100% |
| Clinical B | 3 046 | 2 | 3 | 3.7 | 1.22 | 100% |
| Cancer Pathway | 258 | 0 | 1 | 0.1 | 0.32 | 100% |
| Claims Payment | 5 600 | 0 | 1 | 1.1 | 0.19 | 100% |
| Road Traffic | 150 370 | 0 | 1 | 25.7 | 0.17 | 100% |
| Issue Tracker | 1 132 | 0 | 1 | 0.2 | 0.16 | 100% |

Replay is close to linear in cases; what varies is the per-case constant,
over a 500x range. Two structural predictors were tested and **both fail**:

- **Nested loops.** Clinical C and Clinical B have them and are cheaper per
  case (1.24, 1.22) than Invenio and Devlog, which have none (1.55, 1.53).
  Only `nested >= 6` separates, which is a threshold fitted on eleven points
  with three positives.
- **`do`/`redo` alphabet overlap** — the condition the SAM 2026 paper blames
  for backtracking — occurs in **zero of eleven trees** at threshold 0.0.
  Most loops have a tau redo, making the overlap trivially empty.

BPI 2012, the one pathological log, is also the only one with fitness below
100%. Traces that do not fit exhaust the search budget before returning
`nofit`, so they cost full price. That is consistent with the 81 ms/case
but is a single data point.

### Sampling predicts it well

Mine on the full log, then replay a random sample of cases and extrapolate
linearly:

| log | cases | sample | sample s | predicted | actual | ratio |
|---|---|---|---|---|---|---|
| BPI 2012 | 13 087 | 500 | 46.8 | 1 224.8 s | 1 066.0 s | 1.15x |
| Clinical D | 4 572 | 500 | 5.0 | 45.7 | 33.0 | 1.39x |
| Road Traffic | 150 370 | 500 | 0.09 | 28.3 | 25.7 | 1.10x |
| Clinical A | 1 963 | 500 | 2.4 | 9.5 | 12.0 | 0.79x |
| Clinical C | 3 544 | 500 | 0.69 | 4.9 | 4.4 | 1.12x |
| Invenio SDLC | 2 455 | 500 | 0.77 | 3.8 | 3.8 | 1.00x |
| Clinical B | 3 046 | 500 | 0.64 | 3.9 | 3.7 | 1.05x |
| Claims Payment | 5 600 | 500 | 0.10 | 1.1 | 1.1 | 1.04x |
| Devlog | 285 | all | 0.47 | 0.5 | 0.4 | 1.14x |
| Issue Tracker | 1 132 | 500 | 0.08 | 0.2 | 0.2 | 1.14x |
| Cancer Pathway | 258 | all | 0.08 | 0.1 | 0.1 | 1.21x |

Every estimate lands within 0.79x–1.39x across four orders of magnitude,
and the sample's noise count predicts fitness for free (24/500 = 4.8% on
BPI against 3.3% globally).

**Use a time box, not a fixed sample size.** A 500-case sample costs 0.08 s
on Issue Tracker and 46.8 s on BPI — 585x — so a fixed sample is most
expensive exactly where the warning matters. Replaying for a fixed few
seconds and extrapolating from however many cases finished gives constant
probe cost and the same accuracy; if only a handful of cases complete in
the box, that is itself the answer.

## 5. Recommendations

**Do not** offer `disable_fallthroughs` or IMd as user-facing speed
options. Both return a model this library cannot use, and the honest label
("40x faster, no control flow") is one no user would knowingly choose.

**Do**, in this order:

1. **Profile at load.** Events, cases, activities and sequence variants are
   all one pass. Show them before mining.
2. **Screen on `seq_variants > 2000 OR activities > 50`** to decide whether
   to warn. Give the reason ("7 665 distinct sequences over 274
   activities"), never a predicted time — the same statistics that rank
   logs correctly cannot estimate one.
3. **Offer log reduction** — drop rare activities, drop rare variants, or
   sample cases — and re-profile after. This is the only lever that reduces
   cost without destroying the model, and the user can see exactly what was
   removed. Always report the proportion of cases and activities retained.
4. **Time-box the replay probe** after mining and extrapolate. For most logs
   the probe completes the whole replay anyway.
5. **Let the user cancel.** For the worst logs mining does not finish, and
   no estimate substitutes for a stop button.

## Reproducing

The measurement harness is not committed; it lives outside the repo because
it reads confidential logs by absolute path. It ran one log and one phase
per process (`profile`, `mine_ff`, `mine_noff`, `compare`, `imd`, `replay`,
`sample`, `ambig`), each under an external timeout, appending one JSON line
per result so an interrupted batch loses at most one measurement. Reusing
that shape is recommended for any follow-up: several of these runs are slow
enough, and one was memory-hungry enough, that losing a whole batch to a
single bad log is a real cost.
