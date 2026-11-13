# AI insights — analysis & recommendations — design proposal

**Status:** proposal, partly built. The two deterministic parts have shipped
— the decomposition auto-tuner (`pm4py_ucm.suggest_decomposition`, 0.7.7) and
the partition advisor (`pm4py_ucm.rank_partition_attributes`, 0.7.8; §4.1b).
**No LLM capability is built**: there is no provider code in the package or
the app, and §3, §4.2–§4.4 and §6 describe intentions.

Related documents:

- [`goal_insights.md`](goal_insights.md) — the deterministic companion,
  synthesising a **GRL goal model** from the metrics (not built). It refers
  here for the grounding contract (§2) and for element naming (§4.2).
- [`dashboard_suggestions.md`](dashboard_suggestions.md) §3–§4 — the newer
  proposal for LLM-written dashboard definitions and dashboards from a goal
  model. It takes a different route from §3 below: the tool **exports a
  brief**, the user takes it to an **external LLM** of their choosing, and
  the tool validates what comes back — no provider, key or network call in
  the tool. §3 predates that and has not been reconciled with it.

**Guiding principle (applies to every capability below): the deterministic
layer is the product; the LLM is an optional naming/explanation layer, OFF by
default.** Every feature ships and is useful with *zero* LLM configured.

## 1. Scope & thesis

The subject is LLM support for *analysing results and recommending next
steps*: an LLM acting as a **grounded analyst** over pm4py-ucm's *already-computed* structured
outputs. It never sources numbers — it interprets, critiques, names, and
recommends, always citing the deterministic layer.

The four capabilities span the analysis lifecycle:

| Stage | Capability | § | LLM | Status |
|---|---|---|---|---|
| **Before a family** (setup) | Partition advisor | 4.1b | optional sense-check | ranking built (0.7.8); sense-check not |
| **Per model** (annotation) | Variation-point & stub naming | 4.2 | core | not built |
| **Per model** (critique) | Decision-rule critique & naming | 4.3 | core | not built |
| **Across a family** (analysis) | Cohort-divergence explanation | 4.4 | core | not built |

**Why the numbering starts at 4.1b.** An earlier §4.1, "partition &
decomposition advisor", turned out to be two unrelated *setup* decisions in
two different views. The decomposition half (4.1a, Model view) is pure tree
geometry with no LLM in it: it shipped in 0.7.7 as
`suggest_decomposition(tree, level="auto")` behind the Model view's
decomposition control, and is not an AI capability. The partition half is
4.1b below.

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

## 3. Provider seam — agnostic + local

*Not built, and not reconciled with the external-LLM route of
[`dashboard_suggestions.md`](dashboard_suggestions.md) §3 (see the status
note above). That document expects an in-tool provider, if one is ever
built, to reuse its brief and its validator.*

- A **provider-agnostic interface** with **no default wired in**. The user
  supplies provider + endpoint + key, or points at a **local model** (Ollama /
  llama.cpp / any OpenAI-compatible endpoint).
- **One transport covers local *and* commercial, cheap *and* expensive.** The
  OpenAI `/v1/chat/completions` schema is the de-facto standard: local runners
  (**Ollama** `:11434/v1`, **LM Studio**, **llama.cpp server**, **vLLM**) and
  commercial providers (**OpenAI**, **Azure OpenAI**, **Google** Gemini's
  OpenAI-compat endpoint, **Anthropic** via a thin adapter) all speak it. So a
  single `OpenAICompatibleProvider(base_url, model, api_key_env)` is the whole
  seam; a `NullProvider` is the default. The interface is one method:

  ```python
  class LLMProvider(Protocol):
      def complete(self, system: str, user: str, *,
                   response_schema: dict | None = None) -> str: ...
  ```

- **Cheap vs expensive is just configuration** — the user picks the model + endpoint:
  local `llama3.1:8b` (free) → `gpt-4o-mini` / `gemini-flash` (cheap) →
  `gpt-4o` / `claude-opus` (expensive). The seam is indifferent to which.
- **Config surface:** a small *AI (optional)* expander — `base_url`, `model`,
  `api_key` (read from an **env var, never persisted in the `.ucmproj`**),
  `temperature`, `timeout`. Default off → AI toggles are greyed with "configure a
  provider to enable". Per-invocation opt-in (a button), with results cached on
  the input-context hash to bound cost.
- The **pure library stays offline and zero-dependency**; AI is an **opt-in**
  layer (a web-app panel or an optional `pm4py_ucm.ai` extra), gated behind a
  configured provider. With none configured, every deterministic output still
  ships; the AI annotations are simply absent — never a hard dependency.
- **Local matters because it widens the audience.** The family / divergence use
  cases are healthcare, insurance, public sector — **confidential logs often
  barred from a cloud API**. The app never sends raw event logs — only the
  already-computed structured context (rankings, aligned stats) a capability
  needs — and local models keep even that on-prem. Fits the project's offline /
  self-contained-report identity.
- **Degradation:** provider error/timeout → fall back to deterministic
  names/labels and omit the commentary.

## 4. Capabilities

### 4.1b Partition advisor (Family view) — deterministic-first

**What:** before mining a family, rank *which* case attributes are worth
partitioning on, so the analyst doesn't guess.

**Why it's needed:** the hardest part of the family feature is knowing which
attribute yields genuinely different processes rather than noise. That expertise
barrier gates adoption.

**Deterministic ranking — built** (`pm4py_ucm.rank_partition_attributes(log)`,
in `algo/discovery/families/advisor.py`; tested by
`tests/test_partition_advisor.py`). It returns one `AttributeScore` per
candidate case attribute (from `detect_case_attributes`), best first, scored
on three signals:

1. **Control-flow divergence** — normalised mutual information between the
   attribute and the trace variant, `I(A; variant) / min(H(A), H(variant))`:
   0 means the attribute says nothing about the trace, 1 means one determines
   the other. High ⇒ it *routes behaviour*.
2. **Duration effect size** — η² of case duration across the attribute's
   values. High ⇒ it *segments performance*.
3. **Sanity** — cardinality (near-unique ⇒ likely an identifier), coverage,
   balance and missingness, surfaced as flags and used to discount the score.

Cardinality bias is controlled by bucketing every attribute to the axes a
family would actually partition on (top values + "Other" for categoricals,
quantile bins for numerics) before measuring divergence. Each row carries a
one-line rationale. The Family view shows the ranking as a **Suggested
attributes** table above the *First / Second attribute* pickers, and says so
when nothing scores high.

What changed from the proposal: divergence is normalised mutual information
rather than Jensen–Shannon divergence, and the effect size is measured on
case duration only, not also on rework and event count.

**Optional LLM sense-check — not built:** a domain judgment over the *ranked
shortlist* — "`Broker` looks like a routing determinant; `case_id_prefix` is
probably an artifact" — grounded in the computed scores (per §2). The ranking
is the product; the LLM would add a labelled opinion.

**Business value:** onboarding — turns "which attribute?" from expert intuition
into a guided recommendation. **Originality:** ties the recommendation to a
*setup* decision on a verifiable, aligned substrate, not post-hoc narration.

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

| Phase | Deliverable | Needs an LLM? | Status |
|---|---|---|---|
| **A0** | Decomposition auto-tuner (old 4.1a) — `suggest_decomposition`. | no | built, 0.7.7 |
| **A1** | Partition advisor (4.1b) — deterministic ranking in the Family view. | no | built, 0.7.8 |
| **B0** | Provider seam (§3), grounding contract enforced in code, opt-in gating. **Variation-point & stub naming (4.2)** — lowest LLM risk, writes into `.jucm`. | yes | not built |
| **B0+** | The optional **LLM sense-check on 4.1b** (rides on B0). | yes | not built |
| **B1** | **Decision-rule critique (4.3)** over `condition_mining.csv`. | yes | not built |
| **B2** | **Cohort-divergence explanation (4.4)** over the aligned family stats (and, once available, the GRL strategy profiles). | yes | not built |

The `B*` phases can interleave with the `Pn` phases in `goal_insights.md`
(4.2 depends only on an existing umbrella, 4.3 on data-driven scenarios, 4.4
on a mined family).

## 6. Testing — enforce the contract

For the LLM capabilities, when built:

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
- Prompt-size strategy for large families (summarise from frames, never dump raw
  stats).
