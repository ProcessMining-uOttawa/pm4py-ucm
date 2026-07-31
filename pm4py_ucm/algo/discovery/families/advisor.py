"""Partition advisor — rank case attributes by *discriminative power*.

Before mining a :class:`~.family.ModelFamily`, the hardest question is *which*
case attribute yields genuinely different processes rather than noise. This
module answers it **deterministically** — no LLM — by scoring every candidate
case attribute on how much the process actually changes across its values. The
web app surfaces the ranking to guide the First/Second attribute pickers; an
optional LLM sense-check (see ``docs/ai_insights.md`` §4.1b) is a separate,
opt-in layer on top of this — the ranking is the product.

Three signals, all from material the Family view already computes:

* **control-flow divergence** — normalised mutual information ``I(A; variant) /
  min(H(A), H(variant))``: how cleanly the attribute's values map onto
  behavioural variants. 0 = the attribute tells you nothing about the trace, 1 =
  one perfectly determines the other. (The *min*-entropy denominator keeps it
  fair to a low-cardinality attribute even when the log has many variants.)
* **duration effect size** — ``eta^2`` of case duration across the attribute's
  values (share of duration variance between groups): does the attribute segment
  *performance*.
* **sanity** — cardinality (a near-unique attribute is an identifier, not a
  determinant), coverage/balance and missingness, surfaced as flags and used to
  discount the score.

Cardinality bias — a near-unique attribute would trivially "explain" the variant
— is controlled by **bucketing every attribute to the same bounded axes the
family would actually partition on** (top ``max_values`` + ``"Other"`` for
categoricals, ``bins`` quantiles for numerics) *before* measuring divergence, and
by an explicit high-cardinality flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from .partition import detect_case_attributes

#: Behavioural variants beyond this (by frequency) collapse into one "other"
#: bucket, bounding H(variant) and de-noising the long tail of rare traces.
_MAX_VARIANTS = 30
#: Categorical axis cap / numeric quantile-bin count — mirrors the family's own
#: ``max_values_per_attribute`` / ``bins`` so the score reflects the partition
#: the user would actually get.
_MAX_VALUES = 8
_BINS = 4


@dataclass
class AttributeScore:
    """One ranked candidate attribute. All scores are in ``[0, 1]``."""

    name: str            #: source column, e.g. ``"case:primary_intent"``
    label: str           #: display name (``name`` without a ``case:`` prefix)
    type: str            #: ``"enumeration"`` / ``"integer"`` / ``"boolean"``
    n_values: int        #: distinct non-null raw values
    coverage: float      #: fraction of cases with a non-null value
    balance: float       #: ``1 - largest-bucket share`` (0 = near-constant)
    divergence: float    #: control-flow: ``U(variant | attribute)``
    effect: float        #: duration effect size (``eta^2``)
    score: float         #: combined, sanity-discounted rank key
    flags: List[str] = field(default_factory=list)
    rationale: str = ""

    def to_row(self) -> dict:
        """A flat dict for a dataframe / report row."""
        return {
            "attribute": self.label,
            "type": self.type,
            "values": self.n_values,
            "coverage_%": round(self.coverage * 100, 1),
            "control_flow": round(self.divergence, 3),
            "duration_effect": round(self.effect, 3),
            "score": round(self.score, 3),
            "note": self.rationale,
        }


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _uncertainty_coefficient(attr, variant) -> float:
    """Normalised mutual information ``I(attr; variant) / min(H(attr),
    H(variant))`` — how cleanly the attribute's values map onto behavioural
    variants, in ``[0, 1]`` (1 = one perfectly determines the other). Dividing by
    the *min* entropy keeps it fair to a low-cardinality attribute even when the
    log has many variants (whose large ``H(variant)`` would otherwise swamp it).
    NaNs dropped pairwise; base-invariant (natural log)."""
    import numpy as np
    import pandas as pd

    ct = pd.crosstab(pd.Series(attr).reset_index(drop=True),
                     pd.Series(variant).reset_index(drop=True))
    n = float(ct.to_numpy().sum())
    if n <= 0:
        return 0.0
    pxy = ct.to_numpy(dtype=float) / n
    px = pxy.sum(axis=1, keepdims=True)
    py = pxy.sum(axis=0, keepdims=True)
    with np.errstate(divide="ignore", invalid="ignore"):
        mi = np.where(pxy > 0, pxy * np.log(pxy / (px * py)), 0.0).sum()
        hx = -np.where(px > 0, px * np.log(px), 0.0).sum()
        hy = -np.where(py > 0, py * np.log(py), 0.0).sum()
    denom = min(hx, hy)
    if denom <= 0:
        return 0.0
    return float(min(1.0, max(0.0, mi / denom)))


def _eta_squared(values, groups) -> float:
    """One-way ``eta^2`` (share of variance between groups) of ``values`` across
    ``groups``, in ``[0, 1]``. 0 when either is degenerate."""
    import pandas as pd

    d = pd.DataFrame({"v": pd.to_numeric(values, errors="coerce"),
                      "g": pd.Series(groups).astype("object")}).dropna()
    if len(d) < 2 or d["g"].nunique() < 2 or d["v"].nunique() < 2:
        return 0.0
    grand = d["v"].mean()
    ss_total = float(((d["v"] - grand) ** 2).sum())
    if ss_total <= 0:
        return 0.0
    ss_between = float(d.groupby("g")["v"].apply(
        lambda s: len(s) * (s.mean() - grand) ** 2).sum())
    return float(min(1.0, max(0.0, ss_between / ss_total)))


def _bucket(series, attr_type: str):
    """Collapse a raw attribute series onto the bounded axis the family would
    partition on: top ``_MAX_VALUES`` + ``"Other"`` for categoricals, ``_BINS``
    quantiles for a genuinely continuous numeric (a low-cardinality numeric is
    kept discrete). Missing stays missing."""
    import pandas as pd

    if attr_type == "integer":
        num = pd.to_numeric(series, errors="coerce")
        uniq = num.dropna().unique()
        if len(uniq) <= _BINS or len(uniq) <= 10:
            return num.astype("object")
        try:
            return pd.qcut(num, q=_BINS, duplicates="drop").astype("object")
        except (ValueError, TypeError):
            return num.astype("object")
    s = series.astype("object").where(series.notna())
    top = set(s.value_counts().index[:_MAX_VALUES])
    return s.map(lambda v: v if (pd.isna(v) or v in top) else "Other")


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def _variant_codes(log_df, case_id_col, activity_col, timestamp_col):
    """Per-case behavioural-variant code (capped to the ``_MAX_VARIANTS`` most
    frequent + one ``other`` bucket), indexed by case id."""
    import pandas as pd

    df = log_df.sort_values([case_id_col, timestamp_col], kind="stable")
    # Cast the activity to plain str (object) first: an arrow-backed string
    # column makes groupby().agg(tuple) an arrow list whose value_counts has no
    # kernel — the same arrow gotcha the CSV loader coerces around.
    acts = df[activity_col].astype(str)
    sig = acts.groupby(df[case_id_col].to_numpy(), sort=False).agg(tuple)
    ranked = sig.value_counts().index
    code = {s: (i if i < _MAX_VARIANTS else _MAX_VARIANTS)
            for i, s in enumerate(ranked)}
    return sig.map(code)


def _case_durations(log_df, case_id_col, timestamp_col):
    """Per-case end-to-end duration in seconds, indexed by case id."""
    import pandas as pd

    ts = pd.to_datetime(log_df[timestamp_col], utc=True, errors="coerce")
    g = ts.groupby(log_df[case_id_col], sort=False)
    return (g.max() - g.min()).dt.total_seconds()


def rank_partition_attributes(
    log_df,
    *,
    case_id_col: str = "case:concept:name",
    activity_col: str = "concept:name",
    timestamp_col: str = "time:timestamp",
) -> List[AttributeScore]:
    """Rank ``log_df``'s case attributes by discriminative power (best first).

    Deterministic and LLM-free. Returns ``[]`` when the log has no usable
    case-constant attribute. See the module docstring for the scoring.
    """
    import warnings

    import pandas as pd

    with warnings.catch_warnings():
        # A log with no case-constant attribute is normal here (the advisor
        # simply returns []); the detector's "OR-fork mining abandoned" warning
        # is about a different use and would only confuse.
        warnings.filterwarnings("ignore", message=".*case-constant attributes.*")
        specs, per_case_raw = detect_case_attributes(
            log_df, case_id_col=case_id_col)
    if not specs or per_case_raw is None or per_case_raw.empty:
        return []

    variant = _variant_codes(log_df, case_id_col, activity_col, timestamp_col)
    cases = variant.index
    has_time = timestamp_col in log_df.columns
    dur = (_case_durations(log_df, case_id_col, timestamp_col).reindex(cases)
           if has_time else None)
    raw = per_case_raw.reindex(cases)
    n_cases = len(cases)

    scores: List[AttributeScore] = []
    for spec in specs.values():
        col = spec.source_name
        # Skip pm4py's internal bookkeeping columns (@@case_index, @@index):
        # they are per-event/per-case counters, not real case attributes.
        if col not in raw.columns or col.startswith("@@"):
            continue
        s = raw[col]
        n_values = int(s.dropna().nunique())
        coverage = float(s.notna().mean())
        bucket = _bucket(s, spec.type)
        counts = bucket.value_counts(dropna=True)
        largest = float(counts.iloc[0] / counts.sum()) if len(counts) else 1.0
        balance = 1.0 - largest

        divergence = _uncertainty_coefficient(bucket, variant.reindex(cases))
        effect = _eta_squared(dur, bucket) if dur is not None else 0.0

        flags: List[str] = []
        # A near-unique attribute is an identifier, not a determinant.
        if n_values > max(20, int(0.5 * n_cases)):
            flags.append("high-cardinality — likely an identifier")
        if balance < 0.05:
            flags.append("nearly constant")
        if coverage < 0.5:
            flags.append(f"only {coverage * 100:.0f}% of cases have a value")

        base = 0.6 * divergence + 0.4 * effect
        discounted = any(f.startswith(("high-cardinality", "nearly"))
                         for f in flags)
        score = base * (0.25 if discounted else 1.0)

        if discounted:
            rationale = flags[0]
        elif divergence >= 0.15 and effect >= 0.1:
            rationale = (f"routes behaviour (explains {divergence:.0%} of the "
                         f"variant variation) and segments duration")
        elif divergence >= 0.15:
            rationale = (f"routes behaviour — explains {divergence:.0%} of the "
                         "variant variation")
        elif effect >= 0.1:
            rationale = (f"segments duration — {effect:.0%} of its variance is "
                         "between values")
        else:
            rationale = "little effect on behaviour or duration"

        scores.append(AttributeScore(
            name=col,
            label=col[len("case:"):] if col.startswith("case:") else col,
            type=spec.type, n_values=n_values, coverage=coverage,
            balance=balance, divergence=divergence, effect=effect,
            score=round(score, 4), flags=flags, rationale=rationale))

    scores.sort(key=lambda a: a.score, reverse=True)
    return scores
