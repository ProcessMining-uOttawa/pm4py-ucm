"""Selecting an event log down to chosen behavioural variants.

The Scenarios view already has the user pick which variants to highlight;
those same picks name a set of cases, because clustering recorded which case
fell into which variant. Exporting that sub-log is how a noisy log gets
cleaned — and the cases that replayed as *noise* belong to no variant, so
they drop out by construction rather than by a threshold.
"""
from __future__ import annotations

import pytest

pd = pytest.importorskip("pandas")

from pm4py_ucm.algo.discovery.variants.clustering import (  # noqa: E402
    ClusteringResult, Variant, filter_log_by_variants, resolve_variant_names,
)


def _clustering():
    """Three variants over six cases, plus two that replayed as noise."""
    return ClusteringResult(
        variants=[
            Variant(variant_id="v1", signature=(), case_ids=["c1", "c2"],
                    frequency=2, partial_order_expression="A",
                    linearization_count=1, sequence_variants=1),
            Variant(variant_id="v2", signature=(), case_ids=["c3", "c4"],
                    frequency=2, partial_order_expression="B",
                    linearization_count=1, sequence_variants=1),
            Variant(variant_id="v3", signature=(), case_ids=["c5", "c6"],
                    frequency=2, partial_order_expression="C",
                    linearization_count=1, sequence_variants=1),
        ],
        noise_case_ids=["n1", "n2"],
        sequence_variant_count=3,
        total_cases=8,
    )


def _log(case_ids=("c1", "c2", "c3", "c4", "c5", "c6", "n1", "n2")):
    return pd.DataFrame({
        "case:concept:name": [c for c in case_ids for _ in range(2)],
        "concept:name": ["start", "end"] * len(case_ids),
        "extra": range(2 * len(case_ids)),
    })


class TestResolvingNames:

    def test_variant_ids_and_scenario_names_are_interchangeable(self):
        """The synthesizer names each scenario ``<variant_id>_<suffix>``, so
        a caller holding a scenario selection and one holding variant ids are
        asking the same question."""
        c = _clustering()
        by_id = resolve_variant_names(c, ["v1", "v3"])
        by_scenario = resolve_variant_names(
            c, ["v1_QuickAssessment", "v3_Escalate"])
        assert [v.variant_id for v in by_id] == ["v1", "v3"]
        assert by_id == by_scenario

    def test_the_clustering_order_is_kept(self):
        """Variants are ordered by descending frequency; a selection given in
        any order comes back in the clustering's, so downstream output does
        not depend on click order."""
        c = _clustering()
        assert [v.variant_id for v in resolve_variant_names(c, ["v3", "v1"])] \
            == ["v1", "v3"]

    def test_duplicates_collapse(self):
        c = _clustering()
        got = resolve_variant_names(c, ["v1", "v1_Something", "v1"])
        assert [v.variant_id for v in got] == ["v1"]

    def test_an_unknown_name_raises_rather_than_being_skipped(self):
        """Skipping it would silently shrink the exported log, and a log
        quietly missing cases is worse than no log."""
        with pytest.raises(ValueError, match="v9"):
            resolve_variant_names(_clustering(), ["v1", "v9"])


class TestFiltering:

    def test_only_the_selected_variants_cases_survive(self):
        got = filter_log_by_variants(_log(), _clustering(), ["v1", "v2"])
        assert sorted(got["case:concept:name"].unique()) == \
            ["c1", "c2", "c3", "c4"]

    def test_noise_cases_are_excluded_by_construction(self):
        """Not by a threshold: a noise case belongs to no variant, so
        selecting every variant already drops it. This is the cleaning."""
        got = filter_log_by_variants(
            _log(), _clustering(), ["v1", "v2", "v3"])
        assert set(got["case:concept:name"]) == {f"c{i}" for i in range(1, 7)}
        assert "n1" not in set(got["case:concept:name"])

    def test_columns_and_row_order_are_untouched(self):
        """The point is a log that re-mines exactly like the one it came
        from, so nothing may be reordered, renamed or dropped."""
        log = _log()
        got = filter_log_by_variants(log, _clustering(), ["v2"])
        assert list(got.columns) == list(log.columns)
        assert got["extra"].tolist() == sorted(got["extra"].tolist())
        assert got.equals(log[log["case:concept:name"].isin(["c3", "c4"])])

    def test_integer_case_ids_still_match(self):
        """A CSV import can carry integer case ids while the clustering's are
        strings; comparing without coercion would silently select nothing."""
        log = pd.DataFrame({
            "case:concept:name": [1, 1, 2, 2],
            "concept:name": ["a", "b", "a", "b"],
        })
        clustering = ClusteringResult(
            variants=[Variant(variant_id="v1", signature=(),
                              case_ids=["1"], frequency=1,
                              partial_order_expression="A",
                              linearization_count=1, sequence_variants=1)],
            noise_case_ids=[], sequence_variant_count=1, total_cases=2)
        got = filter_log_by_variants(log, clustering, ["v1"])
        assert len(got) == 2
        assert set(got["case:concept:name"]) == {1}

    def test_an_empty_selection_raises(self):
        """An empty log is not a useful artifact, and asking for one is more
        likely a mistake than an intent."""
        with pytest.raises(ValueError, match="empty log"):
            filter_log_by_variants(_log(), _clustering(), [])

    def test_a_missing_case_column_is_named(self):
        log = _log().rename(columns={"case:concept:name": "case_id"})
        with pytest.raises(ValueError, match="case_id_col"):
            filter_log_by_variants(log, _clustering(), ["v1"])
        got = filter_log_by_variants(
            log, _clustering(), ["v1"], case_id_col="case_id")
        assert set(got["case_id"]) == {"c1", "c2"}


class TestOnARealLog:

    def test_selecting_every_variant_drops_exactly_the_noise(self):
        """The headline property, on a mined log rather than a fixture: the
        cases removed are precisely the ones that did not replay."""
        pm4py = pytest.importorskip("pm4py")
        import pm4py_ucm
        from pathlib import Path
        import zipfile

        root = Path(__file__).resolve().parent.parent
        xes = root / "demo" / "ClaimsPaymentLog.xes"
        if not xes.exists():
            zf = root / "demo" / "ClaimsPaymentLog.zip"
            if not zf.exists():
                pytest.skip("bundled log unavailable")
            with zipfile.ZipFile(zf) as z:
                z.extractall(root / "demo")
        log = pm4py.read_xes(str(xes))
        tree = pm4py.discover_process_tree_inductive(log, noise_threshold=0.2)
        ucm, clustering = pm4py_ucm.discover_scenarios(
            log, parameters={"process_tree": tree})
        assert clustering.noise_case_ids, "sanity: this log has noise"

        names = [s.name for g in ucm.scenario_groups for s in g.scenarios]
        kept = pm4py_ucm.filter_log_by_variants(log, clustering, names)
        before = log["case:concept:name"].nunique()
        after = kept["case:concept:name"].nunique()
        assert before - after == len(clustering.noise_case_ids)
