"""Tests for attribute-based model families.

Covers: log partitioning (enumeration / boolean / binned-integer axes,
Other/Unknown buckets, min_cases skipping), per-cell family discovery,
zip/directory export, combined single-URN assembly (shared
definitions), the dynamic-stub umbrella (preconditions, variables,
strategies, dedup), round-trips, and byte-determinism.

Most tests inject a deterministic toy tree miner so they run without
pm4py; one end-to-end test exercises the real inductive miner."""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile

import pytest

pd = pytest.importorskip("pandas")

import pm4py_ucm
from pm4py_ucm import UCM
from pm4py_ucm.algo.discovery.families import (
    assemble_combined,
    assemble_umbrella,
    discover,
    partition_log,
    write_family,
)
from pm4py_ucm.objects.ucm.exporter.variants.jucm import serialize_to_string
from pm4py_ucm.objects.ucm.importer.variants.jucm import parse_string


_GRAPHVIZ = shutil.which("dot") is not None


def _strip_timestamps(xml: str) -> str:
    """Drop the created/modified export timestamps for determinism
    comparisons (same convention as test_decomposition)."""
    return re.sub(r' (created|modified)="[^"]*"', '', xml)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class T:
    """Duck-typed process-tree node (same shape as the converter expects)."""

    def __init__(self, operator=None, label=None, children=None):
        self.operator = operator
        self.label = label
        self.children = children or []


def _toy_miner(df):
    """Deterministic stand-in for the inductive miner: a sequence tree
    of the modal activity sequence of the sub-log."""
    seqs = df.groupby("case:concept:name", sort=True)["concept:name"].apply(
        tuple,
    )
    counts = {}
    for s in seqs:
        counts[s] = counts.get(s, 0) + 1
    modal = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return T(operator="->", children=[T(label=a) for a in modal])


def _make_log(
    n_breast_er=6, n_breast_planned=5, n_lung_er=4, n_lung_planned=7,
):
    """Synthetic cancer log: two cancer types with distinct activity
    sequences; ``emergency`` does NOT change the lung pathway (so the
    two lung cells are behaviourally identical — the dedup case)."""
    rows = []
    ts = pd.Timestamp("2026-01-01")

    def add_case(cid, activities, ctype, emergency, age):
        nonlocal ts
        for act in activities:
            rows.append({
                "case:concept:name": cid,
                "concept:name": act,
                "time:timestamp": ts,
                "case:cancer_type": ctype,
                "case:emergency": emergency,
                "case:age": age,
            })
            ts += pd.Timedelta(minutes=1)

    breast_er = ["Register", "Triage", "Biopsy", "Surgery", "Discharge"]
    breast_pl = ["Register", "Biopsy", "Surgery", "Discharge"]
    lung = ["Register", "Scan", "Chemo", "Discharge"]

    i = 0
    for _ in range(n_breast_er):
        add_case(f"c{i:03d}", breast_er, "Breast", True, 40 + i % 10); i += 1
    for _ in range(n_breast_planned):
        add_case(f"c{i:03d}", breast_pl, "Breast", False, 55 + i % 10); i += 1
    for _ in range(n_lung_er):
        add_case(f"c{i:03d}", lung, "Lung", True, 62 + i % 10); i += 1
    for _ in range(n_lung_planned):
        add_case(f"c{i:03d}", lung, "Lung", False, 68 + i % 10); i += 1
    return pd.DataFrame(rows)


def _discover(df, attributes, **kwargs):
    kwargs.setdefault("min_cases", 1)
    params = dict(kwargs.pop("parameters", None) or {})
    params.setdefault("tree_miner", _toy_miner)
    params.setdefault("resource_attribute", False)
    return discover(df, attributes, parameters=params, **kwargs)


# ---------------------------------------------------------------------------
# Partitioning
# ---------------------------------------------------------------------------

class TestPartition:

    def test_single_enumeration_attribute(self):
        df = _make_log()
        part = partition_log(df, ["cancer_type"])
        assert [a.display_name for a in part.attributes] == ["cancer_type"]
        assert [c.label for c in part.cells] == ["Breast", "Lung"]
        assert [c.n_cases for c in part.cells] == [11, 11]
        assert part.total_cases == 22
        assert part.covered_cases == 22
        # The cell's df slice only carries that cell's events.
        breast = part.cells[0]
        assert set(breast.df["case:cancer_type"]) == {"Breast"}

    def test_two_attributes_cross_product(self):
        df = _make_log()
        part = partition_log(df, ["cancer_type", "emergency"])
        labels = [c.labels for c in part.cells]
        assert labels == [
            ("Breast", "true"), ("Breast", "false"),
            ("Lung", "true"), ("Lung", "false"),
        ]
        assert [c.n_cases for c in part.cells] == [6, 5, 4, 7]
        grid = part.grid_counts()
        assert grid[("Lung", "false")] == 7

    def test_integer_binning_with_explicit_edges(self):
        df = _make_log()
        part = partition_log(
            df, ["age"], bin_edges={"age": [30, 60, 90]},
        )
        assert part.attributes[0].binned
        labels = [v.label for v in part.attributes[0].values]
        assert labels == ["30-60", "60-90"]
        tokens = [v.token for v in part.attributes[0].values]
        assert tokens == ["_30_60", "_60_90"]  # digit-leading → underscore
        assert sum(c.n_cases for c in part.cells) == 22

    @staticmethod
    def _priority_log(levels):
        """One-activity log with an integer ``case:priority`` column;
        ``levels`` = [(value, n_cases), ...]."""
        rows = []
        ts = pd.Timestamp("2026-01-01")
        i = 0
        for val, n in levels:
            for _ in range(n):
                rows.append({
                    "case:concept:name": f"c{i:04d}",
                    "concept:name": "Act",
                    "time:timestamp": ts,
                    "case:priority": val,
                })
                i += 1
        return pd.DataFrame(rows)

    def test_discrete_integer_one_bin_per_value(self):
        # Priority levels 1..5 with 5 bins requested: each level is its
        # own bin (labelled by the value), not a "1-2"/"2-3" range.
        df = self._priority_log([(1, 4), (2, 3), (3, 5), (4, 2), (5, 6)])
        part = partition_log(df, ["priority"], bins=5)
        assert part.attributes[0].binned
        values = part.attributes[0].values
        assert [v.label for v in values] == ["1", "2", "3", "4", "5"]
        assert [(v.lo, v.hi) for v in values] == [
            (1.0, 1.0), (2.0, 2.0), (3.0, 3.0), (4.0, 4.0), (5.0, 5.0)]
        assert [c.n_cases for c in part.cells] == [4, 3, 5, 2, 6]
        assert part.covered_cases == 20

    def test_discrete_integer_falls_back_to_ranges_when_bins_fewer(self):
        # Asking for fewer bins than distinct values keeps quantile
        # ranges — the value-per-bin path only fires when it can honour
        # every level.
        df = self._priority_log([(1, 4), (2, 3), (3, 5), (4, 2), (5, 6)])
        part = partition_log(df, ["priority"], bins=2)
        labels = [v.label for v in part.attributes[0].values]
        assert all("-" in l for l in labels)
        assert sum(c.n_cases for c in part.cells) == 20

    def test_other_bucket_merges_low_count_values(self):
        rows = []
        ts = pd.Timestamp("2026-01-01")
        # 3 dominant types + 3 rare ones.
        spec = [("A", 10), ("B", 8), ("C", 6), ("X", 1), ("Y", 1), ("Z", 1)]
        i = 0
        for val, n in spec:
            for _ in range(n):
                rows.append({
                    "case:concept:name": f"c{i:03d}",
                    "concept:name": "Act",
                    "time:timestamp": ts,
                    "case:t": val,
                })
                i += 1
        df = pd.DataFrame(rows)
        part = partition_log(df, ["t"], max_values_per_attribute=4)
        labels = [v.label for v in part.attributes[0].values]
        assert labels == ["A", "B", "C", "Other"]
        other_cell = part.cells[-1]
        assert other_cell.label == "Other"
        assert other_cell.n_cases == 3
        other_value = part.attributes[0].values[-1]
        assert other_value.kind == "other"
        assert other_value.raw_values == ("X", "Y", "Z")

    @staticmethod
    def _gender_log(spec):
        """One-activity log with a ``case:gender`` attribute;
        ``spec`` = [(raw_value, n_cases), ...]."""
        rows = []
        ts = pd.Timestamp("2026-01-01")
        i = 0
        for val, n in spec:
            for _ in range(n):
                rows.append({
                    "case:concept:name": f"c{i:03d}",
                    "concept:name": "Act",
                    "time:timestamp": ts,
                    "case:gender": val,
                })
                i += 1
        return pd.DataFrame(rows)

    def test_values_merge_case_insensitively(self):
        # F/f and M/m are the same category; the axis shows one value
        # per category, counts merged across spellings.
        df = self._gender_log([("F", 6), ("f", 2), ("M", 3), ("m", 1)])
        part = partition_log(df, ["gender"])
        values = part.attributes[0].values
        assert [v.label for v in values] == ["F", "M"]
        assert [c.n_cases for c in part.cells] == [8, 4]
        # Every merged spelling is recorded on the value.
        assert values[0].raw_values == ("F", "f")
        assert values[1].raw_values == ("M", "m")

    def test_case_merge_displays_most_frequent_spelling(self):
        df = self._gender_log([("female", 5), ("Female", 2), ("M", 3)])
        part = partition_log(df, ["gender"])
        assert [v.label for v in part.attributes[0].values] == \
            ["M", "female"]
        assert part.cells[-1].n_cases == 7

    def test_case_sensitive_opt_out(self):
        df = self._gender_log([("F", 6), ("f", 2), ("M", 3), ("m", 1)])
        part = partition_log(df, ["gender"], ignore_value_case=False)
        assert [v.label for v in part.attributes[0].values] == \
            ["F", "M", "f", "m"]
        assert [c.n_cases for c in part.cells] == [6, 3, 2, 1]

    def test_include_values_matches_case_insensitively(self):
        df = self._gender_log([("F", 6), ("f", 2), ("M", 3), ("m", 1)])
        part = partition_log(
            df, ["gender"], include_values={"gender": ["f"]},
        )
        assert [c.label for c in part.cells] == ["F"]
        assert part.cells[0].n_cases == 8
        assert part.dropped_cases == 4

    def test_case_merge_feeds_other_bucket(self):
        # After merging, A/a is one dominant value; the cap of 2 keeps
        # it and folds the two rare categories into Other.
        df = self._gender_log(
            [("A", 5), ("a", 4), ("x", 1), ("y", 1)],
        )
        part = partition_log(df, ["gender"], max_values_per_attribute=2)
        labels = [v.label for v in part.attributes[0].values]
        assert labels == ["A", "Other"]
        assert [c.n_cases for c in part.cells] == [9, 2]

    def test_unknown_bucket_and_drop(self):
        df = _make_log()
        df = df.copy()
        # Blank out one case's attribute entirely.
        df.loc[df["case:concept:name"] == "c000", "case:cancer_type"] = None
        part = partition_log(df, ["cancer_type"])
        assert [c.label for c in part.cells] == ["Breast", "Lung", "Unknown"]
        assert part.cells[-1].n_cases == 1
        part2 = partition_log(df, ["cancer_type"], unknown_bucket=False)
        assert [c.label for c in part2.cells] == ["Breast", "Lung"]
        assert part2.dropped_cases == 1

    def test_min_cases_skips_small_cells(self):
        df = _make_log()
        part = partition_log(
            df, ["cancer_type", "emergency"], min_cases=5,
        )
        assert [c.labels for c in part.cells] == [
            ("Breast", "true"), ("Breast", "false"), ("Lung", "false"),
        ]
        assert [(tuple(v.label for v in vs), n)
                for vs, n in part.skipped_cells] == [(("Lung", "true"), 4)]

    def test_unknown_attribute_raises(self):
        df = _make_log()
        with pytest.raises(ValueError) as exc:
            partition_log(df, ["no_such_attribute"])
        assert "cancer_type" in str(exc.value)

    def test_include_values_filters_cases(self):
        df = _make_log()
        part = partition_log(
            df, ["cancer_type"],
            include_values={"cancer_type": ["Breast"]},
        )
        assert [c.label for c in part.cells] == ["Breast"]
        assert [v.label for v in part.attributes[0].values] == ["Breast"]
        assert part.dropped_cases == 11  # the Lung cases
        # Two-attribute filtering composes.
        part2 = partition_log(
            df, ["cancer_type", "emergency"],
            include_values={"cancer_type": ["Breast", "Lung"],
                            "emergency": ["true"]},
        )
        assert [c.labels for c in part2.cells] == [
            ("Breast", "true"), ("Lung", "true"),
        ]

    def test_include_values_empty_selection_raises(self):
        df = _make_log()
        with pytest.raises(ValueError) as exc:
            partition_log(
                df, ["cancer_type"],
                include_values={"cancer_type": ["NoSuchValue"]},
            )
        assert "Breast" in str(exc.value)

    def test_attribute_count_bounds(self):
        df = _make_log()
        with pytest.raises(ValueError):
            partition_log(df, [])
        with pytest.raises(ValueError):
            partition_log(df, ["cancer_type", "emergency", "age"])


# ---------------------------------------------------------------------------
# Family discovery
# ---------------------------------------------------------------------------

class TestDiscoverFamily:

    def test_basic_two_cells(self):
        df = _make_log()
        family = _discover(df, ["cancer_type"])
        assert [c.label for c in family.cells] == ["Breast", "Lung"]
        breast = family.cells[0]
        assert breast.ucm.maps[0].name == "Breast"
        resp_names = {r.name for r in breast.ucm.responsibilities}
        assert "Biopsy" in resp_names and "Chemo" not in resp_names
        assert abs(sum(c.coverage for c in family.cells) - 1.0) < 1e-9

    def test_min_cases_recorded_on_family(self):
        df = _make_log()
        family = _discover(df, ["cancer_type", "emergency"], min_cases=5)
        assert len(family.cells) == 3
        assert len(family.skipped_cells) == 1
        assert family.covered_cases == 18

    def test_no_cells_raises(self):
        df = _make_log()
        with pytest.raises(ValueError) as exc:
            _discover(df, ["cancer_type"], min_cases=1000)
        assert "min_cases" in str(exc.value)

    def test_zip_export(self):
        df = _make_log()
        family = _discover(df, ["cancer_type", "emergency"])
        tmp = tempfile.mkdtemp(prefix="pm4py_ucm_famtest_")
        try:
            path = os.path.join(tmp, "family.zip")
            write_family(family, path)
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                assert "cancer_type=Breast_emergency=true.jucm" in names
                assert "family_summary.csv" in names
                summary = zf.read("family_summary.csv").decode("utf-8")
                assert "cancer_type,emergency,n_cases,coverage_pct,file" in summary
                assert "Lung,false,7," in summary
                jucm = zf.read(
                    "cancer_type=Breast_emergency=true.jucm"
                ).decode("utf-8")
                assert "Biopsy" in jucm
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_directory_export(self):
        df = _make_log()
        family = _discover(df, ["cancer_type"])
        tmp = tempfile.mkdtemp(prefix="pm4py_ucm_famtest_")
        try:
            out = os.path.join(tmp, "out")
            write_family(family, out)
            files = sorted(os.listdir(out))
            assert files == [
                "cancer_type=Breast.jucm",
                "cancer_type=Lung.jucm",
                "family_summary.csv",
            ]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Combined assembly
# ---------------------------------------------------------------------------

class TestCombinedAssembly:

    def test_shared_definitions_single_container(self):
        df = _make_log()
        family = _discover(df, ["cancer_type"])
        combined = assemble_combined(family)
        assert [m.name for m in combined.maps] == ["Breast", "Lung"]
        # "Register" appears in both cells but is ONE definition.
        registers = [
            r for r in combined.responsibilities if r.name == "Register"
        ]
        assert len(registers) == 1
        # Both maps' RespRefs point at the same definition object.
        refs = combined.find_resp_refs(registers[0])
        assert {r.diagram.name for r in refs} == {"Breast", "Lung"}

    def test_byte_determinism_and_roundtrip(self):
        df = _make_log()
        f1 = _discover(df, ["cancer_type", "emergency"])
        f2 = _discover(df, ["cancer_type", "emergency"])
        s1 = serialize_to_string(assemble_combined(f1))
        s2 = serialize_to_string(assemble_combined(f2))
        assert _strip_timestamps(s1) == _strip_timestamps(s2)
        reread = parse_string(s1)
        assert len(reread.maps) == 4


# ---------------------------------------------------------------------------
# Umbrella assembly
# ---------------------------------------------------------------------------

class TestUmbrellaAssembly:

    def test_structure_single_attribute(self):
        df = _make_log()
        family = _discover(df, ["cancer_type"])
        ucm = assemble_umbrella(family)
        root = ucm.maps[0]
        assert root.name == "Overview"
        # The shared skeleton stays on the root map: both cancer types
        # Register first and Discharge last.
        root_resps = {
            n.resp_def.name for n in root.nodes
            if isinstance(n, UCM.RespRef)
        }
        assert root_resps == {"Register", "Discharge"}
        # One dynamic stub at the (single) point of divergence.
        stubs = [n for n in root.nodes if isinstance(n, UCM.Stub)]
        assert len(stubs) == 1
        stub = stubs[0]
        assert stub.dynamic is True
        assert len(stub.bindings) == 2
        exprs = {b.precondition.expression for b in stub.bindings}
        assert exprs == {"cancer_type == Breast", "cancer_type == Lung"}
        # Bindings wire the stub's arcs to each variant's start/end,
        # and the variant plug-ins carry ONLY the divergent activities.
        variant_resps = set()
        for b in stub.bindings:
            assert b.in_bindings and b.out_bindings
            assert b.plugin in ucm.maps
            variant_resps.update(
                n.resp_def.name for n in b.plugin.nodes
                if isinstance(n, UCM.RespRef)
            )
        assert variant_resps == {
            "Triage", "Biopsy", "Surgery", "Scan", "Chemo",
        }

    def test_skeleton_localizes_multiple_variation_points(self):
        # A,X?,C,Y?,B per type: the skeleton keeps A, C, B on the root
        # map and produces TWO dynamic stubs (X1|X2 and Y1|Y2).
        rows = []
        ts = pd.Timestamp("2026-01-01")
        for t, x, y in (("T1", "X1", "Y1"), ("T2", "X2", "Y2")):
            for i in range(4):
                for act in ["A", x, "C", y, "B"]:
                    rows.append({
                        "case:concept:name": f"{t}_{i}",
                        "concept:name": act,
                        "time:timestamp": ts,
                        "case:t": t,
                    })
                    ts += pd.Timedelta(minutes=1)
        df = pd.DataFrame(rows)
        family = _discover(df, ["t"])
        ucm = assemble_umbrella(family)
        root = ucm.maps[0]
        root_resps = {
            n.resp_def.name for n in root.nodes
            if isinstance(n, UCM.RespRef)
        }
        assert root_resps == {"A", "C", "B"}
        stubs = [n for n in root.nodes if isinstance(n, UCM.Stub)]
        assert len(stubs) == 2
        for stub in stubs:
            assert stub.dynamic
            assert len(stub.bindings) == 2
            assert {b.precondition.expression for b in stub.bindings} \
                == {"t == T1", "t == T2"}

    def test_skip_plugin_when_cell_lacks_a_phase(self):
        # Breast has an extra Triage step nowhere in Lung; with only
        # emergency=false cases the middles are [Triage]/[] — the Lung
        # side gets a pass-through "skip" plug-in.
        rows = []
        ts = pd.Timestamp("2026-01-01")
        for t, acts in (("WithStep", ["A", "S", "B"]),
                        ("WithoutStep", ["A", "B"])):
            for i in range(4):
                for act in acts:
                    rows.append({
                        "case:concept:name": f"{t}_{i}",
                        "concept:name": act,
                        "time:timestamp": ts,
                        "case:t": t,
                    })
                    ts += pd.Timedelta(minutes=1)
        df = pd.DataFrame(rows)
        family = _discover(df, ["t"])
        ucm = assemble_umbrella(family)
        root = ucm.maps[0]
        stubs = [n for n in root.nodes if isinstance(n, UCM.Stub)]
        assert len(stubs) == 1
        # Plug-in names carry the covered attribute values.
        names = {b.plugin.name for b in stubs[0].bindings}
        assert names == {"S [WithStep]", "skip [WithoutStep]"}
        skip_map = next(b.plugin for b in stubs[0].bindings
                        if b.plugin.name.startswith("skip"))
        # Pass-through plug-in: start and end, no activities.
        assert not [n for n in skip_map.nodes
                    if isinstance(n, UCM.RespRef)]
        assert skip_map.start_points and skip_map.end_points

    def test_degenerate_merge_falls_back_to_whole_model_stub(self):
        # Different root operators → nothing shared → single dynamic
        # stub with whole cell models as plug-ins (named by cell).
        df = _make_log()

        def split_miner(cell_df):
            if cell_df["case:cancer_type"].iloc[0] == "Breast":
                return _toy_miner(cell_df)
            seq = _toy_miner(cell_df)
            return T(operator="X", children=seq.children)

        family = _discover(
            df, ["cancer_type"],
            parameters={"tree_miner": split_miner,
                        "resource_attribute": False},
        )
        ucm = assemble_umbrella(family)
        root = ucm.maps[0]
        assert not [n for n in root.nodes if isinstance(n, UCM.RespRef)]
        stubs = [n for n in root.nodes if isinstance(n, UCM.Stub)]
        assert len(stubs) == 1
        assert stubs[0].name == "by cancer_type"
        assert {b.plugin.name for b in stubs[0].bindings} \
            == {"Breast", "Lung"}

    def _resource_log(self, same_flow=True):
        """Two types with IDENTICAL control flow; activity B is
        performed by a different actor per type, A and C by the same
        actor everywhere."""
        rows = []
        ts = pd.Timestamp("2026-01-01")
        roles = {"T1": "Alice", "T2": "Bob"}
        for t in ("T1", "T2"):
            for i in range(4):
                for act in ["A", "B", "C"]:
                    rows.append({
                        "case:concept:name": f"{t}_{i}",
                        "concept:name": act,
                        "time:timestamp": ts,
                        "case:t": t,
                        "org:role": (
                            roles[t] if act == "B" else "Carol"
                        ),
                    })
                    ts += pd.Timedelta(minutes=1)
        return pd.DataFrame(rows)

    def test_resource_variation_creates_variation_point(self):
        df = self._resource_log()
        family = discover(
            df, ["t"], min_cases=1,
            parameters={"tree_miner": _toy_miner,
                        "resource_attribute": "org:role"},
        )
        assert family.cells[0].performers["B"] == "Alice"
        assert family.cells[1].performers["B"] == "Bob"
        ucm = assemble_umbrella(family)
        root = ucm.maps[0]
        # A and C shared on the root map; B — same activity, different
        # actor — is the variation point.
        root_resps = {
            n.resp_def.name for n in root.nodes
            if isinstance(n, UCM.RespRef)
        }
        assert root_resps == {"A", "C"}
        stubs = [n for n in root.nodes if isinstance(n, UCM.Stub)]
        assert len(stubs) == 1 and stubs[0].dynamic
        assert len(stubs[0].bindings) == 2
        # Each variant plug-in draws B inside its own actor.
        actors = {}
        for b in stubs[0].bindings:
            refs = [n for n in b.plugin.nodes
                    if isinstance(n, UCM.RespRef)]
            assert [r.resp_def.name for r in refs] == ["B"]
            assert refs[0].cont_ref is not None
            actors[b.precondition.expression] = \
                refs[0].cont_ref.cont_def.name
        assert actors == {"t == T1": "Alice", "t == T2": "Bob"}
        # The SHARED definition of B carries no semantic performer
        # (the cells disagree); unanimous activities keep theirs.
        b_def = next(r for r in ucm.responsibilities if r.name == "B")
        assert b_def.performer is None
        a_def = next(r for r in ucm.responsibilities if r.name == "A")
        assert a_def.performer is not None
        assert a_def.performer.name == "Carol"

    def test_resource_variation_can_be_disabled(self):
        df = self._resource_log()
        family = discover(
            df, ["t"], min_cases=1,
            parameters={"tree_miner": _toy_miner,
                        "resource_attribute": "org:role"},
        )
        with pytest.warns(UserWarning, match="no variation points"):
            ucm = assemble_umbrella(family, resource_variation=False)
        root = ucm.maps[0]
        assert not [n for n in root.nodes if isinstance(n, UCM.Stub)]

    def test_identical_cells_warn_about_missing_variation(self):
        # Same flow AND same performers everywhere → honest warning.
        df = self._resource_log()
        df = df.copy()
        df["org:role"] = "Carol"
        family = discover(
            df, ["t"], min_cases=1,
            parameters={"tree_miner": _toy_miner,
                        "resource_attribute": "org:role"},
        )
        with pytest.warns(UserWarning, match="no variation points"):
            ucm = assemble_umbrella(family)
        assert len(ucm.maps) == 1  # single shared model, no plug-ins

    def test_path_scenarios_cover_within_cell_variants(self):
        # T1 cases split between A,B,D and A,C,D; T2 always A,E,D.
        # Path scenarios must yield one executable scenario per
        # (cell × variant) with family_variant branch conditions on
        # the XOR inside T1's variant plug-in.
        rows = []
        ts = pd.Timestamp("2026-01-01")

        def case(cid, acts, t):
            nonlocal ts
            for a in acts:
                rows.append({"case:concept:name": cid,
                             "concept:name": a,
                             "time:timestamp": ts, "case:t": t})
                ts += pd.Timedelta(minutes=1)

        for i in range(6):
            case(f"t1b_{i}", ["A", "B", "D"], "T1")
        for i in range(4):
            case(f"t1c_{i}", ["A", "C", "D"], "T1")
        for i in range(5):
            case(f"t2_{i}", ["A", "E", "D"], "T2")
        df = pd.DataFrame(rows)

        def xor_miner(cell_df):
            seqs = sorted({
                tuple(s) for s in cell_df.groupby(
                    "case:concept:name", sort=True,
                )["concept:name"].apply(tuple)
            })
            if len(seqs) == 1:
                return T(operator="->",
                         children=[T(label=a) for a in seqs[0]])
            # A, X(B|C), D for the two-sequence cell.
            return T(operator="->", children=[
                T(label="A"),
                T(operator="X",
                  children=[T(label=s[1]) for s in seqs]),
                T(label="D"),
            ])

        family = discover(
            df, ["t"], min_cases=1,
            parameters={"tree_miner": xor_miner,
                        "resource_attribute": False},
        )
        ucm = assemble_umbrella(family)

        # family_variant enumeration covers every (cell, variant).
        fv = next(v for v in ucm.variables
                  if v.name == "family_variant")
        assert set(fv.enumeration_type.values) == {
            "T1_v1", "T1_v2", "T2_v1",
        }
        scenarios = ucm.scenario_groups[0].scenarios
        assert [s.name for s in scenarios] == [
            "T1 v1", "T1 v2", "T2 v1",
        ]
        s1 = scenarios[0]
        inits = {i.variable.name: i.value for i in s1.initializations}
        assert inits == {"t": "T1", "family_variant": "T1_v1"}

        # The XOR inside T1's plug-in carries family_variant branch
        # conditions — v1 (B, modal) on one branch, v2 (C) on the
        # other — on the arc DIRECTLY leaving the fork (the only arc
        # jUCMNav's traversal engine evaluates).
        forks = [
            n for m in ucm.maps for n in m.nodes
            if isinstance(n, UCM.OrFork)
        ]
        assert len(forks) == 1
        exprs = {
            arc.condition.expression
            for arc in forks[0].succ_connections
        }
        assert exprs == {
            "family_variant == T1_v1", "family_variant == T1_v2",
        }

    def test_path_scenarios_condition_inside_loop_xors(self):
        # A loop whose body chooses between B and C per iteration:
        # cases S,B,E (one pass) and S,B,C,E / S,C,B,E (two passes).
        # The inside-loop XOR must get combined family_variant +
        # loop-counter conditions, not stay at "true".
        rows = []
        ts = pd.Timestamp("2026-01-01")

        def case(cid, acts):
            nonlocal ts
            for a in acts:
                rows.append({"case:concept:name": cid,
                             "concept:name": a,
                             "time:timestamp": ts, "case:t": "T1"})
                ts += pd.Timedelta(minutes=1)

        for i in range(5):
            case(f"one_{i}", ["S", "B", "E"])
        for i in range(4):
            case(f"two_{i}", ["S", "B", "C", "E"])
        # Second dummy type so a family (2 cells) exists at all.
        for i in range(3):
            rows_len = len(rows)
            case(f"z_{i}", ["S", "B", "E"])
            for r in rows[rows_len:]:
                r["case:t"] = "T2"
        df = pd.DataFrame(rows)

        def loop_miner(cell_df):
            # ->( S, *( X(B, C), tau ), E )
            return T(operator="->", children=[
                T(label="S"),
                T(operator="*", children=[
                    T(operator="X",
                      children=[T(label="B"), T(label="C")]),
                    T(),  # tau redo
                ]),
                T(label="E"),
            ])

        family = discover(
            df, ["t"], min_cases=1,
            parameters={"tree_miner": loop_miner,
                        "resource_attribute": False},
        )
        ucm = assemble_umbrella(family)

        # Loop counter variable exists and scenarios initialise it.
        int_vars = [v for v in ucm.variables if v.type == "integer"]
        assert int_vars, "expected a loop counter variable"
        scenarios = ucm.scenario_groups[0].scenarios
        assert any(
            any(i.variable.type == "integer" for i in s.initializations)
            for s in scenarios
        )

        # The inside-loop XOR (both branches lead into the loop body;
        # it is the OrFork whose _tree_python_id maps to the X node)
        # carries non-trivial conditions referencing family_variant.
        conditioned = []
        for m in ucm.maps:
            for n in m.nodes:
                if not isinstance(n, UCM.OrFork):
                    continue
                for arc in n.succ_connections:
                    if (arc.condition is not None
                            and "family_variant"
                            in (arc.condition.expression or "")):
                        conditioned.append(arc.condition.expression)
        assert conditioned, "inside-loop XOR should carry conditions"
        # At least one clause combines the variant with a counter
        # range (the two-iteration variant distributes B and C over
        # counter values).
        assert any(
            ("&&" in e and ("<=" in e or ">" in e))
            for e in conditioned
        ), conditioned

    def test_path_scenarios_disabled_gives_plain_strategies(self):
        df = _make_log()
        family = _discover(df, ["cancer_type"])
        ucm = assemble_umbrella(family, path_scenarios=False)
        scenarios = ucm.scenario_groups[0].scenarios
        assert [s.name for s in scenarios] == ["Breast", "Lung"]
        assert not any(v.name == "family_variant"
                       for v in ucm.variables)

    def test_skeleton_false_gives_trivial_umbrella(self):
        df = _make_log()
        family = _discover(df, ["cancer_type"])
        ucm = assemble_umbrella(family, skeleton=False)
        root = ucm.maps[0]
        assert not [n for n in root.nodes if isinstance(n, UCM.RespRef)]
        stubs = [n for n in root.nodes if isinstance(n, UCM.Stub)]
        assert len(stubs) == 1
        assert stubs[0].name == "by cancer_type"
        assert len(stubs[0].bindings) == 2
        # Whole cell models as plug-ins, named by cell label.
        assert {b.plugin.name for b in stubs[0].bindings} \
            == {"Breast", "Lung"}

    def test_variables_and_strategies(self):
        # Plain configuration strategies (path_scenarios=False) — the
        # per-variant path scenarios have their own test below.
        df = _make_log()
        family = _discover(df, ["cancer_type", "emergency"])
        ucm = assemble_umbrella(family, path_scenarios=False)
        var_names = {v.name: v.type for v in ucm.variables}
        assert var_names == {
            "cancer_type": "enumeration", "emergency": "boolean",
        }
        ets = {e.name: e.values for e in ucm.enumeration_types}
        assert ets == {"CancerType": ["Breast", "Lung"]}
        assert len(ucm.scenario_groups) == 1
        scenarios = ucm.scenario_groups[0].scenarios
        assert [s.name for s in scenarios] == [
            "Breast / true", "Breast / false",
            "Lung / true", "Lung / false",
        ]
        first = scenarios[0]
        inits = {i.variable.name: i.value for i in first.initializations}
        assert inits == {"cancer_type": "Breast", "emergency": "true"}
        assert first.start_points and first.end_points
        assert first.end_points[0].mandatory

    def test_dedup_merges_identical_cells(self):
        df = _make_log()
        family = _discover(df, ["cancer_type", "emergency"])
        ucm = assemble_umbrella(family)
        root = ucm.maps[0]
        stub = [n for n in root.nodes if isinstance(n, UCM.Stub)][0]
        # Breast/true and Breast/false differ (Triage); Lung/true and
        # Lung/false are identical → merged: 3 plug-ins, not 4.
        assert len(stub.bindings) == 3
        merged = [
            b for b in stub.bindings
            if "Lung" in (b.precondition.label or "")
        ]
        assert len(merged) == 1
        # The OR over both emergency values minimises away entirely:
        # (Lung && true) || (Lung && false) → Lung.
        assert merged[0].precondition.expression == "cancer_type == Lung"
        # But every cell still gets its own strategy.
        assert len(ucm.scenario_groups[0].scenarios) == 4

    def test_dedup_disabled(self):
        df = _make_log()
        family = _discover(df, ["cancer_type", "emergency"])
        ucm = assemble_umbrella(family, dedup=False)
        root = ucm.maps[0]
        stub = [n for n in root.nodes if isinstance(n, UCM.Stub)][0]
        assert len(stub.bindings) == 4

    def test_export_and_roundtrip(self):
        df = _make_log()
        family = _discover(df, ["cancer_type"])
        ucm = assemble_umbrella(family)
        text = serialize_to_string(ucm)
        assert 'dynamic="true"' in text
        assert "<precondition" in text
        assert "cancer_type == Breast" in text
        # Every <bindings> must reference its plug-in map by id, and
        # the SHARED entry/exit arcs of the dynamic stub must
        # back-reference EVERY binding's <in>/<out> (space-separated
        # XPaths) — jUCMNav cannot wire bindings to plug-ins from a
        # partial list (regression: the lookup used to keep only the
        # last binding).
        bindings_tags = re.findall(r"<bindings\b[^>]*", text)
        assert len(bindings_tags) == 2
        assert all("plugin=" in b for b in bindings_tags)
        in_refs = re.search(r'inBindings="([^"]+)"', text).group(1)
        out_refs = re.search(r'outBindings="([^"]+)"', text).group(1)
        assert "@bindings.0/@in.0" in in_refs
        assert "@bindings.1/@in.0" in in_refs
        assert "@bindings.0/@out.0" in out_refs
        assert "@bindings.1/@out.0" in out_refs
        # Determinism.
        family2 = _discover(df, ["cancer_type"])
        assert _strip_timestamps(text) == _strip_timestamps(
            serialize_to_string(assemble_umbrella(family2))
        )
        # Round-trip: dynamic flag and preconditions survive.
        reread = parse_string(text)
        stubs = [
            n for m in reread.maps for n in m.nodes
            if isinstance(n, UCM.Stub)
        ]
        dyn = [s for s in stubs if s.dynamic]
        assert len(dyn) == 1
        exprs = {
            b.precondition.expression
            for b in dyn[0].bindings if b.precondition is not None
        }
        assert exprs == {"cancer_type == Breast", "cancer_type == Lung"}

    def test_umbrella_with_decomposition(self):
        # A variant subtree with nested sub-sequences: the plug-in's
        # conversion applies the family's decomposition, so static
        # stubs appear INSIDE the variant plug-in, below the dynamic
        # stub at the variation point.
        df = _make_log()

        def nested_miner(cell_df):
            if cell_df["case:cancer_type"].iloc[0] == "Breast":
                mid = T(operator="->", children=[
                    T(operator="->",
                      children=[T(label="B1"), T(label="B2")]),
                    T(operator="->",
                      children=[T(label="B3"), T(label="B4")]),
                ])
            else:
                mid = T(label="Scan")
            return T(operator="->", children=[
                T(label="Register"), mid, T(label="Discharge"),
            ])

        family = _discover(
            df, ["cancer_type"],
            decomposition={
                "on_root_sequence": True, "on_parallel": False,
                "on_loop": False, "max_leaves_per_map": 2,
                "min_leaves_to_decompose": 2, "balance_ratio": 0.0,
            },
            parameters={
                "tree_miner": nested_miner, "resource_attribute": False,
            },
        )
        ucm = assemble_umbrella(family)
        root = ucm.maps[0]
        stub = [n for n in root.nodes if isinstance(n, UCM.Stub)][0]
        assert stub.dynamic
        # Register / Discharge shared on the root map.
        root_resps = {
            n.resp_def.name for n in root.nodes
            if isinstance(n, UCM.RespRef)
        }
        assert root_resps == {"Register", "Discharge"}
        # Plug-ins themselves decomposed → static stubs below the
        # dynamic one.
        static = [
            n for m in ucm.maps[1:] for n in m.nodes
            if isinstance(n, UCM.Stub) and not n.dynamic
        ]
        assert static, "expected static stubs inside decomposed plug-ins"


# ---------------------------------------------------------------------------
# Grid rendering
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _GRAPHVIZ, reason="graphviz 'dot' not on PATH")
class TestGridRendering:

    def _render(self, family, parameters=None):
        from pm4py_ucm.visualization.ucm import family_grid
        tmp = tempfile.mkdtemp(prefix="pm4py_ucm_famgrid_")
        path = os.path.join(tmp, "grid.png")
        try:
            family_grid.render(family, path, parameters=parameters)
            from PIL import Image
            with Image.open(path) as im:
                return im.size, dict(im.text or {})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_single_attribute_stack(self):
        df = _make_log()
        family = _discover(df, ["cancer_type"])
        (w, h), _ = self._render(family)
        assert w > 100 and h > 100

    def test_two_attribute_matrix_with_skipped_cell(self):
        df = _make_log()
        family = _discover(
            df, ["cancer_type", "emergency"], min_cases=5,
        )
        (w, h), _ = self._render(family)
        assert w > 200 and h > 200

    def test_explicit_dpi_scales_output(self):
        df = _make_log()
        family = _discover(df, ["cancer_type"])
        (w96, h96), meta96 = self._render(family, {"dpi": 96})
        (w192, h192), meta192 = self._render(family, {"dpi": 192})
        assert meta96["pm4py_ucm_dpi"] == "96"
        assert meta192["pm4py_ucm_dpi"] == "192"
        # Panels scale linearly with DPI (chrome scales too, so the
        # composite lands close to 2× — allow generous tolerance).
        assert 1.7 < w192 / w96 < 2.3
        assert 1.7 < h192 / h96 < 2.3

    def test_adaptive_dpi_hits_target_for_small_family(self):
        # Two tiny cells: the projected composite is far below the
        # pixel budget, so the adaptive chooser goes to target_dpi.
        df = _make_log()
        family = _discover(df, ["cancer_type"])
        _, meta = self._render(family)
        assert int(meta["pm4py_ucm_dpi"]) == 192

    def test_adaptive_dpi_respects_pixel_budget(self):
        # A deliberately tiny budget forces the chooser down to the
        # 96-dpi readability floor — never below.
        df = _make_log()
        family = _discover(df, ["cancer_type"])
        _, meta = self._render(family, {"max_total_pixels": 10_000})
        assert int(meta["pm4py_ucm_dpi"]) == 96

    def test_budget_enforced_exactly_after_rendering(self):
        # Pick a budget between the 96-dpi size and the 192-dpi size:
        # the heuristic renders high, the post-measure pass shrinks
        # panels so the FINAL composite honours the budget (this is
        # the guarantee the probe projection alone cannot give when
        # panel shapes vary).
        df = _make_log()
        family = _discover(df, ["cancer_type"])
        (w96, h96), _ = self._render(family, {"dpi": 96})
        budget = int(w96 * h96 * 2.2)  # < the 4x of a full 192 render
        (w, h), meta = self._render(
            family, {"max_total_pixels": budget},
        )
        assert w * h <= budget
        assert 96 <= int(meta["pm4py_ucm_dpi"]) < 192


# ---------------------------------------------------------------------------
# End-to-end with the real inductive miner
# ---------------------------------------------------------------------------

def test_end_to_end_with_pm4py():
    pytest.importorskip("pm4py")
    df = _make_log()
    family = pm4py_ucm.discover_ucm_family(
        df, ["cancer_type"], min_cases=1,
        parameters={"resource_attribute": False},
    )
    assert [c.label for c in family.cells] == ["Breast", "Lung"]
    for cell in family.cells:
        assert cell.ucm.maps, "each cell must carry a mined model"
    umbrella = pm4py_ucm.assemble_ucm_family(family, mode="umbrella")
    text = serialize_to_string(umbrella)
    assert 'dynamic="true"' in text
    combined = pm4py_ucm.assemble_ucm_family(family, mode="combined")
    assert len(combined.maps) >= 2


# ---------------------------------------------------------------------------
# Family grid heat-map (the grid SVG the Family view shows honours the overlay)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _GRAPHVIZ, reason="graphviz 'dot' absent")
def test_family_grid_svg_honours_heatmap_and_family_scale():
    from pm4py_ucm.visualization.ucm.family_grid import render_svg
    from pm4py_ucm.visualization.ucm.variants import classic as _classic

    family = _discover(_make_log(), ["cancer_type"])
    # The toy miner doesn't annotate performance, so stamp each cell's
    # activities with distinct frequencies — and give the two cells different
    # ranges, so a family-wide scale differs from each cell's local scale.
    v = 1
    for cell in family.cells:
        for m in cell.ucm.maps:
            for n in m.nodes:
                if isinstance(n, UCM.RespRef):
                    n.add_metadata("perf_frequency", str(v))
                    v += 9

    off = render_svg(family, "bpmn")
    on = render_svg(family, "bpmn", heatmap=True, node_metric="frequency")
    assert on != off, "heat-map did not reach the family grid"

    # Family-wide span (shared across all cells) rescales vs each cell's own
    # local range — so the two are different renders.
    ns, es = _classic.heat_span([c.ucm for c in family.cells],
                                node_metric="frequency")
    assert ns is not None
    fam_scaled = render_svg(family, "bpmn", heatmap=True,
                            node_metric="frequency",
                            node_span=ns, edge_span=es)
    assert fam_scaled != on, "family-wide scale did not differ from local"
