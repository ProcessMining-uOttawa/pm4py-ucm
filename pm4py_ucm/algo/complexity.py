"""Cost screening: how expensive will discovery and replay be on this log?

Two questions, answered differently because the evidence says they have to
be. See ``docs/miner_performance.md`` for the measurements behind both.

**Mining** cannot be estimated. Across twelve logs the cost tracks the
number of distinct activity sequences well enough to *rank* them (Spearman
+0.93) but not to time them: two logs with the same activities x variants
product differed 2.8x, and the slowest never finished at all. So
:func:`screen_mining` returns a risk level and a reason to show the user,
never a predicted duration.

**Replay** can be estimated, because it is close to linear in cases. What
varies is the per-case constant -- over a 500x range -- so it has to be
measured rather than predicted. :func:`estimate_replay` measures it on a
small batch and extrapolates, which landed within 0.79x-1.39x on every log
tested, including predicting an 18-minute run before committing to it.

The probe is time-boxed rather than fixed-size on purpose: a fixed
500-case sample cost 0.08s on one log and 46.8s on another, i.e. it is
most expensive exactly where the warning matters most. Under a time box,
logs cheap enough to finish inside the budget simply finish -- for most
logs the probe *is* the computation.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = [
    "LogProfile",
    "MiningRisk",
    "ReplayEstimate",
    "profile_log",
    "screen_mining",
    "estimate_replay",
    "DEFAULT_VARIANT_LIMIT",
    "DEFAULT_ACTIVITY_LIMIT",
]

CASE_KEY = "case:concept:name"
ACTIVITY_KEY = "concept:name"
TIMESTAMP_KEY = "time:timestamp"

#: Above this many distinct activity sequences, mining is worth warning
#: about. Measured: every log below 2000 variants mined in under 7s; the
#: three above 2797 took 11.9s, 21.9s and "did not finish in 900s".
DEFAULT_VARIANT_LIMIT = 2000

#: Above this many distinct activities, warn as well. This is a
#: precaution rather than a measured threshold -- PM4Py's fall-through
#: cost is linear in alphabet size, so a wide log with few variants would
#: be expensive for a reason the variant count cannot see, but no log in
#: the study sat between 46 and 274 activities to demonstrate it.
DEFAULT_ACTIVITY_LIMIT = 50


@dataclass
class LogProfile:
    """Cheap per-log statistics: one pass, no mining."""

    events: int
    cases: int
    activities: int
    seq_variants: int
    trace_len_median: float
    trace_len_max: int

    @property
    def variant_ratio(self) -> float:
        """Distinct sequences per case. Approaching 1.0 means almost every
        case is unique, which is the shape that makes mining expensive."""
        return self.seq_variants / self.cases if self.cases else 0.0

    def as_dict(self) -> Dict[str, Any]:
        d = {
            "events": self.events,
            "cases": self.cases,
            "activities": self.activities,
            "seq_variants": self.seq_variants,
            "trace_len_median": self.trace_len_median,
            "trace_len_max": self.trace_len_max,
            "variant_ratio": round(self.variant_ratio, 4),
        }
        return d


@dataclass
class MiningRisk:
    """Whether to warn before mining, and what to tell the user.

    Deliberately carries no predicted duration: the statistics that rank
    logs correctly cannot time them.
    """

    high: bool
    reason: str
    triggers: List[str] = field(default_factory=list)


@dataclass
class ReplayEstimate:
    """Result of a time-boxed replay probe."""

    total_cases: int
    sampled_cases: int
    elapsed_s: float
    estimated_total_s: float
    complete: bool
    noise_rate: float

    @property
    def clustering(self):
        """The clustering computed during the probe, when :attr:`complete`
        is true -- i.e. the probe replayed every case and there is nothing
        left to run. ``None`` otherwise."""
        return self._clustering

    _clustering: Any = None


def profile_log(
    df,
    *,
    case_id_key: str = CASE_KEY,
    activity_key: str = ACTIVITY_KEY,
    timestamp_key: Optional[str] = TIMESTAMP_KEY,
) -> LogProfile:
    """Compute the cheap statistics used to screen a log.

    Everything here is a single pass over ``df``: safe to run at upload
    time, before the user has committed to anything. ``timestamp_key`` is
    used only to order events within a case; pass ``None`` if ``df`` is
    already in order.
    """
    work = df
    if timestamp_key is not None and timestamp_key in df.columns:
        work = df.sort_values([case_id_key, timestamp_key], kind="stable")

    seqs = work.groupby(case_id_key, sort=False)[activity_key].apply(tuple)
    lens = seqs.map(len)
    return LogProfile(
        events=int(len(work)),
        cases=int(len(seqs)),
        activities=int(work[activity_key].nunique()),
        seq_variants=int(seqs.nunique()),
        trace_len_median=float(lens.median()) if len(lens) else 0.0,
        trace_len_max=int(lens.max()) if len(lens) else 0,
    )


def screen_mining(
    profile: LogProfile,
    *,
    variant_limit: int = DEFAULT_VARIANT_LIMIT,
    activity_limit: int = DEFAULT_ACTIVITY_LIMIT,
) -> MiningRisk:
    """Decide whether to warn the user before mining this log.

    Returns a risk flag and a sentence explaining *why*, phrased in terms
    of the log rather than a duration. Users act correctly on a reason and
    mis-plan on a wrong number, and on this evidence any number would
    sometimes be wrong by two orders of magnitude.

    When :attr:`MiningRisk.high` is set, the caller should offer to reduce
    the log -- dropping infrequent activities or infrequent variants --
    and re-profile, rather than offering any miner flag. No PM4Py setting
    makes this faster while keeping a usable model.
    """
    triggers: List[str] = []
    if profile.seq_variants > variant_limit:
        triggers.append(
            f"{profile.seq_variants:,} distinct activity sequences across "
            f"{profile.cases:,} cases"
        )
    if profile.activities > activity_limit:
        triggers.append(f"{profile.activities:,} distinct activities")

    if not triggers:
        return MiningRisk(
            high=False,
            reason=(
                f"{profile.seq_variants:,} distinct sequences over "
                f"{profile.activities:,} activities -- mining should be quick."
            ),
        )

    detail = " and ".join(triggers)
    unique = ""
    if profile.variant_ratio >= 0.9:
        unique = (
            " Nearly every case is unique, which is the shape that makes"
            " the miner's fall-through search expensive."
        )
    return MiningRisk(
        high=True,
        reason=(
            f"This log has {detail}. Mining may take a long time, and on the"
            f" most extreme logs it may not finish at all.{unique}"
        ),
        triggers=triggers,
    )


def estimate_replay(
    df,
    tree,
    *,
    time_budget: float = 5.0,
    probe_cases: int = 50,
    case_id_key: str = CASE_KEY,
    coarsen_loops: bool = True,
    random_state: int = 0,
) -> ReplayEstimate:
    """Estimate the cost of concurrency-aware replay, cheaply.

    Replays a small batch of cases to measure the per-case cost, then, if
    the whole log looks like it fits inside ``time_budget``, replays all of
    it -- so a log that is cheap enough simply gets computed and
    :attr:`ReplayEstimate.complete` comes back true with the real
    clustering attached. Otherwise the estimate is extrapolated linearly
    and no further work is done.

    ``tree`` must be the process tree the caller intends to use; the
    estimate is only meaningful against the tree that will actually be
    replayed.
    """
    from .discovery.variants import clustering as _clustering

    ids = df[case_id_key].drop_duplicates()
    total = int(len(ids))
    if total == 0:
        return ReplayEstimate(0, 0, 0.0, 0.0, True, 0.0)

    first = min(probe_cases, total)
    sample_ids = set(ids.sample(n=first, random_state=random_state))
    batch = df[df[case_id_key].isin(sample_ids)]

    started = time.perf_counter()
    clus = _clustering.cluster(batch, tree, coarsen_loops=coarsen_loops)
    elapsed = time.perf_counter() - started

    noise_rate = len(clus.noise_case_ids) / first if first else 0.0
    if first == total:
        return ReplayEstimate(total, first, elapsed, elapsed, True,
                              noise_rate, clus)

    per_case = elapsed / first
    projected = per_case * total

    # Cheap enough to just do it: the probe becomes the computation.
    if projected <= time_budget:
        started = time.perf_counter()
        clus = _clustering.cluster(df, tree, coarsen_loops=coarsen_loops)
        full = time.perf_counter() - started
        return ReplayEstimate(
            total, total, elapsed + full, full, True,
            len(clus.noise_case_ids) / total, clus)

    return ReplayEstimate(total, first, elapsed, projected, False, noise_rate)
