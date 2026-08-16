"""V6's one-click reductions record what they selected, and hold.

The cost screen offers two — keep the 2,000 most frequent variants, keep the
50 most frequent activities — and says both apply. Two encodings each lost one
silently:

* a **rank range is relative to a population**, and any other filter moves it.
  Applying the variant reduction first and the activity one second re-read
  "keep the top 2,000" against a log that now had fewer than 2,000 distinct
  sequences and selected all of them — every case the reduction had dropped
  came back, and the metrics were computed over the full case set;
* **Streamlit owns widget state** and may discard it on a rerun that changes
  nothing, so a reduction parked in a slider evaporated when the user merely
  answered the replay prompt.

Both now travel in the filter spec as what they selected — ``variant_cap``
names the cases, ``activity_cap`` names the activities. These tests pin the
first failure mode directly; the second is structural (nothing here touches
Streamlit) and is pinned by the encoding itself.

The fixture is shaped like the log that exposed it, where projecting onto the
top activities collapses the variant count below the cap.

The pipeline under test is the ``apply_log_filters`` the code exporter emits
into every generated script — sliced out of ``codegen._HELPERS`` and exec'd,
since it is a template string rather than a module function. That mirrors
``_apply_log_filters`` in the Streamlit script, which cannot be imported
without a running Streamlit, and has the bonus of testing the source users
actually run from an exported project.
"""
import random
import sys
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "web"))

pm4py = pytest.importorskip("pm4py")

from sessions import codegen  # noqa: E402


def _emitted_filter_pipeline():
    """``apply_rename`` + ``apply_log_filters`` as the exporter emits them."""
    src = codegen._HELPERS
    start = src.index("def apply_rename(")
    end = src.index("def resource_params(")
    ns = {"pd": pd, "pm4py": pm4py}
    exec(compile(src[start:end], "<codegen._HELPERS>", "exec"), ns)
    return ns["apply_log_filters"]


apply_log_filters = _emitted_filter_pipeline()

N_CORE = 8
N_NOISE = 6
CAP = 40


@pytest.fixture(scope="module")
def log():
    """A log whose rare activities carry most of the sequence distinctions.

    Dropping them collapses the variant count below ``CAP``, which is exactly
    the shape that made the cap evaporate.
    """
    rng = random.Random(4242)
    core = [f"C{i}" for i in range(N_CORE)]
    noise = [f"N{i}" for i in range(N_NOISE)]
    core_seqs = sorted({tuple(rng.choices(core, k=rng.randint(3, 6)))
                        for _ in range(30)})
    rows, case = [], 0
    for i, seq in enumerate(core_seqs):
        for _ in range(max(2, 12 - i // 3)):
            case += 1
            trace = list(seq)
            for _ in range(rng.randint(1, 2)):
                trace.insert(rng.randint(0, len(trace)), rng.choice(noise))
            for j, act in enumerate(trace):
                rows.append({
                    "case:concept:name": f"K{case:05d}",
                    "concept:name": act,
                    "time:timestamp": pd.Timestamp("2025-01-01", tz="UTC")
                    + pd.Timedelta(hours=j + case),
                })
    return pd.DataFrame(rows)


def _profile(df):
    return (df["concept:name"].nunique(),
            df["case:concept:name"].nunique(),
            len(pm4py.get_variants(df)))


def _top_activities(df, n):
    return list(df["concept:name"].value_counts().index[:n])


def test_the_shape_that_exposed_the_bug(log):
    """Projecting onto the core activities must drop the variant count below
    the cap — otherwise these tests prove nothing."""
    acts_full, _, vars_full = _profile(log)
    assert vars_full > CAP, "the cap must actually bind on the full log"
    projected = apply_log_filters(
        log, (("activity_ranks", (1, N_CORE)),))
    _, _, vars_proj = _profile(projected)
    assert vars_proj < CAP, (
        "the projection must collapse variants below the cap, which is what "
        "made a rank-based cap silently widen back out")


def test_cap_alone_reduces_cases(log):
    _, cases_full, _ = _profile(log)
    capped = apply_log_filters(log, (("variant_cap", (1, CAP, ())),))
    acts, cases, variants = _profile(capped)
    assert variants == CAP
    assert cases < cases_full


def test_cap_survives_an_activity_filter_applied_afterwards(log):
    """The regression. Cases kept by the cap must not come back."""
    capped = apply_log_filters(log, (("variant_cap", (1, CAP, ())),))
    _, cases_capped, _ = _profile(capped)

    both = apply_log_filters(log, (
        ("activity_ranks", (1, N_CORE)),
        ("variant_cap", (1, CAP, ())),
    ))
    acts, cases, _ = _profile(both)
    assert acts == N_CORE, "the activity reduction still applies"
    assert cases == cases_capped, (
        "the variant reduction must keep exactly the cases it selected; "
        "widening back to the full log is the bug this pins")


def test_a_rank_range_would_have_evaporated(log):
    """Contrast: the old ``variant_ranks`` encoding is a no-op here.

    Not a complaint about ``variant_ranks`` — it is correct for a slider the
    user drags against the current population. It is the wrong encoding for a
    reduction chosen against an earlier one, which is why the cap moved.
    """
    _, cases_full, _ = _profile(log)
    both = apply_log_filters(log, (
        ("activity_ranks", (1, N_CORE)),
        ("variant_ranks", (1, CAP)),
    ))
    _, cases, _ = _profile(both)
    assert cases == cases_full


def test_cap_composes_with_a_base_spec(log):
    """A cap clicked while another filter was already in force selects its
    cases on that filtered log, and still cannot be widened afterwards."""
    base = (("activity_ranks", (1, N_CORE + 2)),)
    spec_at_click = base + (("variant_cap", (1, CAP, base)),)
    at_click = apply_log_filters(log, spec_at_click)
    _, cases_at_click, _ = _profile(at_click)

    narrowed = apply_log_filters(log, (
        ("activity_ranks", (1, N_CORE)),
        ("variant_cap", (1, CAP, base)),
    ))
    _, cases_after, _ = _profile(narrowed)
    assert cases_after == cases_at_click


def test_activity_cap_names_its_activities(log):
    """``activity_cap`` keeps exactly the activities it names."""
    keep = tuple(f"C{i}" for i in range(4))
    out = apply_log_filters(log, (("activity_cap", keep),))
    assert set(out["concept:name"].unique()) == set(keep)


def test_activity_cap_does_not_re_rank_when_the_log_changes(log):
    """The regression this pins is the mirror of the variant one.

    A rank range would re-rank against whatever population is left. Naming
    the activities means the same alphabet survives, whatever else moves.
    """
    ranked = list(log["concept:name"].value_counts().index)
    keep = tuple(ranked[:6])

    alone = apply_log_filters(log, (("activity_cap", keep),))
    with_cases_dropped = apply_log_filters(log, (
        ("activity_cap", keep),
        ("variant_cap", (1, CAP, ())),
    ))
    assert set(alone["concept:name"].unique()) == set(keep)
    assert set(with_cases_dropped["concept:name"].unique()) <= set(keep)
    # The alphabet is pinned by name, so dropping cases cannot add one back.
    assert not (set(with_cases_dropped["concept:name"].unique()) - set(keep))


def test_both_caps_compose(log):
    """Both one-click reductions in force at once, in either spec order."""
    ranked = list(log["concept:name"].value_counts().index)
    keep = tuple(ranked[:6])
    a = apply_log_filters(log, (
        ("activity_cap", keep), ("variant_cap", (1, CAP, ()))))
    b = apply_log_filters(log, (
        ("variant_cap", (1, CAP, ())), ("activity_cap", keep)))
    assert _profile(a) == _profile(b)
    # And the case selection is still the variant cap's, not the full log.
    _, cases_full, _ = _profile(log)
    assert _profile(a)[1] < cases_full


def test_cap_accepts_the_lists_a_project_round_trip_returns(log):
    """A saved project stores the spec as JSON, so the nested base comes back
    as lists rather than tuples."""
    as_tuples = apply_log_filters(log, (
        ("activity_ranks", (1, N_CORE)),
        ("variant_cap", (1, CAP, (("activity_ranks", (1, N_CORE + 2)),))),
    ))
    as_lists = apply_log_filters(log, (
        ("activity_ranks", (1, N_CORE)),
        ("variant_cap", (1, CAP, [["activity_ranks", [1, N_CORE + 2]]])),
    ))
    assert _profile(as_tuples) == _profile(as_lists)
