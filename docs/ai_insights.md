# AI insights — analysis & recommendations — design proposal

> **Status: design proposal — not yet implemented.** The agreed shape for the
> LLM-powered *analysis and recommendation* capabilities of Feature 1. Its
> deterministic companion — synthesising a **GRL goal model** from the metrics —
> is specified in [`goal_insights.md`](goal_insights.md); this document is the
> **single home** for the LLM grounding contract and the provider seam, which
> that feature references.

## 1. Scope & thesis

This is the "analysis of results and recommendations" half of Feature 1: an LLM
acting as a **grounded analyst** over pm4py-ucm's *already-computed* structured
outputs. It never sources numbers — it interprets, critiques, names, and
recommends, always citing the deterministic layer.

The four capabilities span the analysis lifecycle:

| Stage | Capability | § |
|---|---|---|
| **Before mining** (setup) | Partition & decomposition advisor | 4.1 |
| **Per model** (annotation) | Variation-point & stub naming | 4.2 |
| **Per model** (critique) | Decision-rule critique & naming | 4.3 |
| **Across a family** (analysis) | Cohort-divergence explanation | 4.4 |

What makes these different from the me-too "narrate the KPIs" of other tools:
pm4py-ucm's outputs are **structured** (aligned family stats, per-fork mined
rules with accuracy, variability-metamodel elements), so the LLM operates on a
*rich, verifiable substrate* and its output can be **checked against** that
substrate — not free text over a chart.

## 2. The grounding contract (non-negotiable)

Applies to every capability here **and** to the LLM naming in
`goal_insights.md`:

1. **Numbers only from the deterministic layer.** Every figure originates in
   `FamilyStats`, `annotate_performance`, `condition_mining.csv`, the fact
   table, or the synthesised indicators — and is **cited** to its source. The
   LLM may not compute or restate a figure it wasn't given.
2. **Causal/interpretive claims are labelled hypotheses**, visibly distinct
   from measured facts.
3. **The model & the stats are ground truth.** The LLM edits names and
   descriptions and emits commentary; it never edits structure or metrics.
4. **`docs/metrics.md` is the semantic contract** the interpretation is checked
   against.
5. **Verifiability by construction.** Prompts are built from the structured
   frames (not raw logs); outputs carry back-references so a reader — and a test
   (§6) — can confirm each figure traces to an input.

## 3. Provider seam — agnostic + local (decided stance)

- A **provider-agnostic interface** with **no default wired in**. The user
  supplies provider + endpoint + key, or points at a **local model** (Ollama /
  llama.cpp / any OpenAI-compatible endpoint).
- The **pure library stays offline and zero-dependency**; AI is an **opt-in**
  layer (a web-app panel or an optional `pm4py_ucm.ai` extra), gated behind a
  configured provider. With none configured, every deterministic output still
  ships; the AI annotations are simply absent — never a hard dependency.
- **Local matters because it widens the audience.** The family / divergence use
  cases are healthcare, insurance, public sector — **confidential logs often
  barred from a cloud API**. Local models make these capabilities usable on
  exactly the logs where the analysis is most valuable, and they fit the
  project's offline / self-contained-report identity.
- **Degradation:** provider error/timeout → fall back to deterministic
  names/labels and omit the commentary.

## 4. Capabilities

### 4.1 Partition & decomposition advisor (pre-mining setup)

**What:** before mining a family, recommend *which* case attributes are worth
partitioning on — and suggest sensible decomposition boundaries.

**Why it's needed:** the hardest part of the family feature is knowing which
attribute yields genuinely different processes rather than noise. That expertise
barrier gates adoption.

**Deterministic core + LLM judgment (hybrid):**
- *Deterministic:* rank candidate attributes by **discriminative power** — how
  much the mined process (or its key metrics) actually changes across the
  attribute's values (cross-value variance / effect size), plus cardinality and
  coverage sanity (the coverage heatmap already exists pre-mining).
- *LLM:* a **domain sense-check** on the ranked shortlist — "`Broker` looks like
  a routing determinant; `case_id_prefix` is probably an artifact" — and a
  plain-language rationale. The ranking is computed; the LLM adds judgment.

**Business value:** onboarding — turns "which attribute?" from expert intuition
into a guided recommendation. **Originality:** ties AI to a *setup* decision,
not post-hoc narration.

### 4.2 Variation-point & stub naming (per-model annotation)

**What:** turn machine-generated labels — `cancer_type == Breast && age_group
== _40_59`, `loop_Test`, an anonymous dynamic stub — into a **domain name** plus
a one-line `Intent:` description.

**Inputs:** the umbrella's variation points and their guard conditions, the
plug-in variants, the attributes in play, and each variant's partial-order
expression.

**Output that persists:** names/intents are **written into the `.jucm`**, so
jUCMNav shows them. The AI improves a **standard, interoperable modeling
artifact**, not an ephemeral dashboard.

**Business value:** a model a domain expert can *read* is a model that gets
*validated*. **Originality:** lands in the modeling tool. This is also the
concrete instance of the "LLM naming" referenced by `goal_insights.md`
(softgoals/goals/indicators are named the same way).

*Lowest-risk, highest-polish — the natural first slice of the LLM layer.*

### 4.3 Decision-rule critique & naming (per-model critique)

**What:** for data-driven scenarios, (a) restate each mined per-fork rule as a
**business-policy sentence**, (b) **flag suspicious rules**, and (c) name the
fork.

**Inputs (already structured):** `condition_mining.csv` — per `(OR-fork,
branch)` accuracy, sample size, feature set, `skipped_reason`, and the emitted
expression.

**The skepticism that matters:** a 95%-accurate rule can be a genuine policy
*or* an artifact — **target leakage** (an attribute set *after* the decision), a
**proxy** attribute, or a **threshold artifact**. The LLM surfaces these as
labelled concerns for the analyst to confirm; it does not overrule the miner.

**Business value:** analysts distrust auto-mined rules precisely because they
can't quickly separate policy from artifact — automated skepticism builds that
trust. **Originality:** we already expose accuracy + features + `skipped_reason`,
a rich structured critique input most tools don't surface at all.

### 4.4 Cohort-divergence explanation (cross-family analysis)

**What:** explain *why* family members differ — the single highest-value
question in comparative process analysis ("why is region X slower? why does
product line Y rework more?").

**The moat is the alignment.** Through the shared skeleton, "the same decision
point" is genuinely the same row for every cohort, so the LLM compares
apples-to-apples over the aligned choice shares, activity Δ/ratio, and edge
deltas — not a loose diff of two DFGs. Grounded in that plus the distinguishing
attribute values, it produces **labelled hypotheses**: *"Cohort A skips
Assessment 80% vs B's 12%; the distinguishing attribute is
`Broker == Spot_Health` — likely an auto-approval agreement."*

**Relationship to the goal model (`goal_insights.md`):** the GRL per-cohort
**strategies** give the *quantified* comparison (computed goal satisfaction per
cohort); this capability adds the *causal narrative* on top of that + the
aligned stats. Deterministic quantification and grounded explanation are
complementary, not competing.

**Business value:** this is what enterprise PM suites charge for — delivered on
a structurally more reliable, aligned substrate. **Originality:** the alignment
makes the comparison verifiable; the hypotheses are tied to specific figures.

## 5. Phasing

| Phase | Deliverable |
|---|---|
| **B0** | Provider seam (agnostic + local), grounding contract enforced in code, opt-in gating. **Variation-point & stub naming (4.2)** — lowest risk, writes into `.jucm`. |
| **B1** | **Decision-rule critique (4.3)** over `condition_mining.csv`. |
| **B2** | **Cohort-divergence explanation (4.4)** over the aligned family stats (and, once available, the GRL strategy profiles). |
| **B3** | **Partition & decomposition advisor (4.1)** — deterministic ranking + LLM sense-check. |

(The `Bn` phases are independent of and can interleave with the `Pn` phases in
`goal_insights.md`; 4.2 depends only on an existing umbrella, 4.3 on data-driven
scenarios, 4.4 on a mined family.)

## 6. Testing — enforce the contract

- **No-fabrication test:** feed a fixture whose structured frames contain known
  figures; assert every number in the AI output traces back to an input value
  (back-reference check), and a red-team fixture confirms the model can't smuggle
  in an un-provided number without it being flagged.
- **Determinism-independent:** with the provider seam pointed at a stub/echo
  backend, the pipeline (prompt assembly, parsing, `.jucm` write-back for 4.2)
  is unit-tested without any real model call.
- **Round-trip:** names written by 4.2 survive the byte-stable `.jucm`
  export/import.

## 7. Open questions

- How prominently to surface hypotheses vs facts in the UI (a visual "measured
  vs inferred" split).
- Whether rule-critique flags (4.3) should gate the scenario export or only
  annotate it.
- How much of the advisor (4.1) ranking is worth shipping deterministically even
  with no provider configured (it is useful on its own).
- Prompt-size strategy for large families (summarise from frames, never dump raw
  stats).
