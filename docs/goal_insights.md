# Goal-model synthesis & AI insights — design proposal

> **Status: design proposal — not yet implemented.** This document is the
> agreed shape for the "AI / LLM support for analysis and recommendations"
> feature. It is written to be built against (like `docs/sessions.md` was),
> not to describe shipped behaviour. Nothing here exists in the code yet.

## 1. Thesis

Every other process-mining tool bolts an LLM onto an **efficiency /
conformance dashboard**, and the LLM's job is to *narrate KPIs*. pm4py-ucm
mines to a **requirements notation with a variability metamodel** (UCM), and
it is part of the **User Requirements Notation** (URN, ITU-T Z.151) — the one
notation family that *also* carries a goal notation (**GRL**, the
Goal-oriented Requirements Language), **native traceability links**
(`URNlink`), and a **computable satisfaction semantics** (GRL *indicators* +
*evaluation strategies*).

That combination lets us do something no PM tool can: **synthesise a GRL goal
model — with indicators driven by the influential metrics, traceable back to
the mined UCM — and compute goal satisfaction per cohort.** The output is a
real `.grl`/URN artifact that opens in jUCMNav next to the mined UCM, not prose
about one.

The feature is therefore **two layers**:

| Layer | What it is | LLM? |
|---|---|---|
| **A. Goal-model synthesis** | Metrics → GRL indicators → softgoals → contribution links → per-cohort strategies, traceable to UCM | **No** — deterministic. LLM only *names* things. |
| **B. AI interpretation** | Rule critique, element naming in domain terms, labelled hypotheses about divergence | **Yes** — optional, grounded, bring-your-own-model. |

Layer A is the crown jewel and does **not** depend on an LLM. That is exactly
why it is not fluffy: the metric→indicator→contribution→strategy→traceability
chain is *computed*, not narrated.

## 2. Layer A — GRL goal-model synthesis (deterministic)

### 2.1 What we emit

From a mined UCM + its performance/family statistics, synthesise a GRL model
containing:

- **Indicators** — one per *influential* metric (see §2.3). A GRL `Indicator`
  is `(worstValue, thresholdValue, targetValue)` + a measured real-world value;
  jUCMNav interpolates a satisfaction in `[worst..target] → evalRange`. Our
  **dashboard targets are already proto-indicators** — lift them straight in;
  derive the rest from the data distribution.
- **Softgoals** — a small **quality catalog** (§2.4): *Timeliness*,
  *Efficiency*, *Quality / low rework*, *Conformance*, *Throughput*, and a
  *Cost* proxy. Indicators **contribute** to these.
- **Goals / Tasks** — operationalisations traceable to UCM responsibilities
  and pathways (a Task per key responsibility or per plug-in variant).
- **Contribution links** (Indicator → Softgoal) with a **sign and strength**
  derived from metric semantics (§2.5); optionally nuanced by the LLM.
- **`URNlink` traceability** — every indicator/task links back to the exact
  UCM element it measures. This is **exact and mechanical** because the metric
  already carries `perf_<metric>` metadata on that node/edge/fork.
- **Evaluation strategies** — **one per family cohort** (or per scenario):
  assign that cohort's measured indicator values, and jUCMNav propagates them
  to a **computed goal-satisfaction profile**. Comparing strategies *is* the
  quantified cohort-divergence answer, expressed as goal satisfaction rather
  than a table.

### 2.2 Why traceability is the moat

Because the UCM was *mined*, we know precisely which responsibility an activity
metric belongs to, which OR-fork a branch share belongs to, and which segment
an edge time belongs to. So the `URNlink` from a GRL indicator to its UCM
element is not a guess — it is the same binding the performance overlay already
uses. The goal model is **anchored to the mined behaviour**, and a reviewer can
click an indicator and land on the responsibility it scores.

### 2.3 Selecting the influential metrics (deterministic)

"Influential" is a statistical property, not an opinion:

- **Cross-cohort discrimination** — rank metrics by variance / effect size
  *across* family members. A metric that separates cohorts is influential.
- **Outcome correlation** — rank activity/edge metrics by correlation with a
  chosen outcome (case duration, rework, fitness).
- Cap to the top-*k* per quality dimension so the goal model stays legible.

### 2.4 The quality catalog (metric → softgoal, with sign)

A curated, extensible default map — deterministic, overridable:

| Metric family | Softgoal | Sign (higher metric →) |
|---|---|---|
| service / sojourn / waiting time | Timeliness, Efficiency | hurts |
| rework / repeat frequency | Quality (low rework) | hurts |
| replay fitness | Conformance | helps |
| case / event throughput | Throughput | helps |
| branch share on a costly path | Cost (proxy) | hurts |

Sign + strength give the `Contribution` link; the LLM (Layer B) may refine
wording and nuance but never invents the numbers.

### 2.5 Indicator thresholds (deterministic, with a target hook)

`worst / threshold / target` come from the data distribution
(e.g. P95 / median / P10 for a *time-is-bad* metric), **or** from an explicit
**dashboard target** the user already set — reusing the existing targets/
scorecard machinery so the goal model inherits the analyst's intent.

### 2.6 Scope: extend the GRL metamodel

Today `_emit_grlspec` writes only an **empty** `grlspec`. Building Layer A
means implementing the GRL side of the metamodel, mirroring what already exists
for UCM:

- `obj.py` — `IntentionalElement` (Softgoal/Goal/Task/Resource), `Indicator`
  (worst/threshold/target/unit), `ElementLink` subtypes (`Contribution`,
  `Decomposition`, `Dependency`), `Actor`, `EvaluationStrategy` + `Evaluation`,
  and `URNlink` for traceability.
- exporter — `_emit_grlspec` populates actors / intentional elements / links /
  strategies; a `GRLGraph` diagram with auto-layout (reuse the UCM layouter
  approach) so the model is visible/editable in jUCMNav.
- importer + a **byte-stable round-trip** test, matching the UCM discipline.
- **Propagation is deferred to jUCMNav** in v1 (emit strategies, let jUCMNav
  evaluate). A pure-Python propagation for the web app is a later phase.

## 3. Layer B — AI interpretation (optional)

The LLM annotates the deterministic artifacts; it never sources numbers. This
layer — its capabilities, the **grounding contract**, the **agnostic + local
provider seam**, and its failure modes — is specified in full in
[`ai_insights.md`](ai_insights.md). Two instances touch the goal model directly:

- **Naming** softgoals / goals / indicators / variation points in domain terms
  (the "variation-point & stub naming" capability of `ai_insights.md` §4.2),
  written into the model so jUCMNav shows it.
- **Contribution-sign nuance** — refining the quality-catalog defaults (§2.4)
  where a metric's effect on a softgoal is domain-specific.

Layer A ships the goal model with or without a configured provider; Layer B is
never a hard dependency. The grounding contract in `ai_insights.md` §2 applies
verbatim to any figure the LLM touches here.

## 4. Phasing

| Phase | Deliverable |
|---|---|
| **P0** | GRL metamodel in `obj.py` + exporter/importer, byte-stable round-trip; synthesise **indicators + softgoals + contributions + `URNlink` traceability** from a single mined UCM's overlay metrics; emit a jUCMNav-loadable `.grl` view. |
| **P1** | **Per-cohort evaluation strategies** from family stats; the strategy-comparison as the quantified divergence view; influential-metric selection (§2.3). |
| **P2** | **Layer B** — provider seam (agnostic + local), element naming, rule critique, labelled hypotheses, all under the grounding contract. |
| **P3** | Pure-Python GRL propagation for in-app satisfaction display; goal-model export wired into sessions + code export. |

## 5. Testing

- Deterministic synthesis is **unit-testable**: fixture UCM + metrics →
  expected indicators / links / thresholds / traceability.
- GRL export is **byte-stable** and **jUCMNav-loadable** (same bar as UCM).
- Layer B is tested for the **grounding contract**: assert every emitted figure
  traces to a provided value; a red-team fixture checks no fabricated numbers.

## 6. Research framing

**Process mining → goal model is essentially unexplored.** URN is uniquely
suited because it holds the behaviour notation (UCM), the goal notation (GRL),
native traceability, and a satisfaction semantics under one metamodel. Mining a
GRL model with indicators + strategies, traceable to a mined UCM, is a genuine
MoDRE/RE contribution — and it stays within URN even though it reaches past the
paper's current UCM-only scope.

## 7. Open questions

- Softgoal decomposition depth: flat (indicators → softgoals) vs a shallow goal
  hierarchy per actor/component.
- Whether Tasks map to responsibilities, to plug-in variants, or both.
- Outcome selection for the correlation ranking (duration by default?).
- Where the quality catalog lives and how users override it.
- `.grl` as a separate file vs one URN spec carrying both UCM and GRL.
