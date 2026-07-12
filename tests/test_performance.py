"""Tests for the performance overlay (frequencies / times on
activities and edges): stats computation, segment-based annotation,
visualizer output, and .jucm round-trip of the metadata."""
from __future__ import annotations

import re

import pytest

pd = pytest.importorskip("pandas")

from pm4py_ucm import UCM, convert_to_ucm
from pm4py_ucm.algo.performance import (
    annotate_performance,
    compute_performance_stats,
)
from pm4py_ucm.objects.ucm.exporter.variants.jucm import serialize_to_string
from pm4py_ucm.objects.ucm.importer.variants.jucm import parse_string
from pm4py_ucm.visualization.ucm.variants import classic


class T:
    def __init__(self, operator=None, label=None, children=None):
        self.operator = operator
        self.label = label
        self.children = children or []


def _log(with_intervals=False):
    """6 cases A,B,D and 4 cases A,C,D; 10 minutes between events;
    optional 5-minute activity durations (interval log)."""
    rows = []
    ts = pd.Timestamp("2026-01-01")
    cid = 0
    for mid, n in (("B", 6), ("C", 4)):
        for _ in range(n):
            for act in ["A", mid, "D"]:
                row = {"case:concept:name": f"c{cid:02d}",
                       "concept:name": act,
                       "time:timestamp": ts}
                if with_intervals:
                    row["start_timestamp"] = ts - pd.Timedelta(minutes=5)
                rows.append(row)
                ts += pd.Timedelta(minutes=10)
            cid += 1
    return pd.DataFrame(rows)


def _ucm():
    tree = T(operator="->", children=[
        T(label="A"),
        T(operator="X", children=[T(label="B"), T(label="C")]),
        T(label="D"),
    ])
    return convert_to_ucm(tree)


class TestStats:

    def test_activity_frequencies_and_coverage(self):
        stats = compute_performance_stats(_log())
        assert stats.total_cases == 10
        assert stats.activity["A"] == {"frequency": 10,
                                       "case_coverage": 10}
        assert stats.activity["B"]["frequency"] == 6
        assert stats.activity["C"]["case_coverage"] == 4

    def test_pair_frequencies_and_waiting_time(self):
        stats = compute_performance_stats(_log())
        assert stats.pairs[("A", "B")]["frequency"] == 6
        assert stats.pairs[("A", "C")]["frequency"] == 4
        assert stats.pairs[("B", "D")]["frequency"] == 6
        # 10 minutes between consecutive events.
        assert stats.pairs[("A", "B")]["mean_time"] == 600.0
        assert stats.pairs[("A", "B")]["total_time"] == 3600.0

    def test_interval_log_activity_durations(self):
        stats = compute_performance_stats(_log(with_intervals=True))
        assert stats.activity["A"]["mean_time"] == 300.0
        assert stats.activity["A"]["total_time"] == 3000.0
        # Waiting time shrinks to 5 minutes: completion → next start.
        assert stats.pairs[("A", "B")]["mean_time"] == 300.0

    def test_single_timestamp_log_has_no_activity_durations(self):
        stats = compute_performance_stats(_log())
        assert "mean_time" not in stats.activity["A"]


class TestAnnotate:

    @staticmethod
    def _perf(element):
        for md in element.metadata:
            if md.name == "_perf":
                return md.value
        return None

    def test_activity_annotations(self):
        ucm = _ucm()
        annotate_performance(
            ucm, _log(),
            node_metrics=["frequency", "case_coverage"],
            edge_metrics=[],
        )
        texts = {
            n.resp_def.name: self._perf(n)
            for m in ucm.maps for n in m.nodes
            if isinstance(n, UCM.RespRef)
        }
        assert texts["A"] == "n=10 · 10 cases"
        assert texts["B"] == "n=6 · 6 cases"

    @staticmethod
    def _branch_perf(node, index):
        for md in node.metadata:
            if md.name == f"_perf_branch{index}":
                return md.value
        return None

    def test_branch_annotations_with_percentage(self):
        ucm = _ucm()
        annotate_performance(
            ucm, _log(),
            node_metrics=[],
            edge_metrics=["frequency", "percentage"],
        )
        # Edge overlays live on the SOURCE node, branch-indexed
        # (jUCMNav has no metadata feature on connections).
        fork = next(
            n for m in ucm.maps for n in m.nodes
            if isinstance(n, UCM.OrFork)
        )
        branch_texts = {
            self._branch_perf(fork, i)
            for i in range(len(fork.succ_connections))
        }
        assert branch_texts == {"6 · 60%", "4 · 40%"}
        # Sequential segments get plain frequencies (no percentage
        # outside forks), stored on the segment's first node.
        seq_texts = [
            self._branch_perf(n, 0)
            for m in ucm.maps for n in m.nodes
            if isinstance(n, UCM.RespRef) and self._branch_perf(n, 0)
        ]
        assert "6" in seq_texts and "4" in seq_texts  # B→D and C→D

    def test_fork_after_join_gets_branch_stats(self):
        # ->(A, X(B, C), X(E, F)): the second OR-fork sits right
        # after the first XOR's join — its "previous activity" is the
        # SET {B, C}, aggregated over the directly-follows pairs.
        rows = []
        ts = pd.Timestamp("2026-01-01")
        combos = [("B", "E", 4), ("C", "E", 2), ("B", "F", 3),
                  ("C", "F", 1)]
        cid = 0
        for mid, tail, n in combos:
            for _ in range(n):
                for act in ["A", mid, tail]:
                    rows.append({"case:concept:name": f"c{cid:02d}",
                                 "concept:name": act,
                                 "time:timestamp": ts})
                    ts += pd.Timedelta(minutes=10)
                cid += 1
        df = pd.DataFrame(rows)
        tree = T(operator="->", children=[
            T(label="A"),
            T(operator="X", children=[T(label="B"), T(label="C")]),
            T(operator="X", children=[T(label="E"), T(label="F")]),
        ])
        ucm = convert_to_ucm(tree)
        annotate_performance(
            ucm, df, node_metrics=[],
            edge_metrics=["frequency", "percentage"],
        )
        # The E/F fork: E taken 6 of 10 ((B,E)+(C,E)), F 4 of 10.
        forks = [n for m in ucm.maps for n in m.nodes
                 if isinstance(n, UCM.OrFork)]
        all_branch_texts = {
            self._branch_perf(f, i)
            for f in forks for i in range(len(f.succ_connections))
        }
        assert "6 · 60%" in all_branch_texts  # E branch, through join
        assert "4 · 40%" in all_branch_texts  # F branch

    def test_reannotation_replaces(self):
        ucm = _ucm()
        annotate_performance(ucm, _log(),
                             node_metrics=["frequency"], edge_metrics=[])
        annotate_performance(ucm, _log(),
                             node_metrics=["case_coverage"],
                             edge_metrics=[])
        node = next(
            n for m in ucm.maps for n in m.nodes
            if isinstance(n, UCM.RespRef) and n.resp_def.name == "A"
        )
        perfs = [md.value for md in node.metadata if md.name == "_perf"]
        assert perfs == ["10 cases"]

    def test_unknown_metric_raises(self):
        with pytest.raises(ValueError):
            annotate_performance(_ucm(), _log(),
                                 node_metrics=["bogus"], edge_metrics=[])


class TestRenderAndExport:

    def test_visualizer_includes_overlay(self):
        ucm = _ucm()
        annotate_performance(
            ucm, _log(),
            node_metrics=["frequency"],
            edge_metrics=["percentage"],
        )
        source = classic.apply(ucm, parameters={"style": "ucm"}).source
        assert 'POINT-SIZE="9"' in source
        assert "n=10" in source
        assert "60%" in source

    def test_export_and_reimport_preserve_metadata(self):
        ucm = _ucm()
        annotate_performance(ucm, _log(),
                             node_metrics=["frequency"], edge_metrics=[])
        text = serialize_to_string(ucm)
        assert re.search(r'<metadata name="_perf" value="n=10', text)
        reread = parse_string(text)
        values = [
            md.value
            for m in reread.maps for n in m.nodes
            for md in n.metadata if md.name == "_perf"
        ]
        assert "n=10 · 10 cases" not in values  # single-metric overlay
        assert "n=10" in values

    def test_all_metrics_exported_one_per_line(self):
        # Every available metric becomes its own perf_<metric>
        # metadata entry (activity metrics on the RespRef, edge
        # metrics branch-indexed on the arc's source node) —
        # regardless of the display selection.
        ucm = _ucm()
        annotate_performance(ucm, _log(),
                             node_metrics=["frequency"],
                             edge_metrics=["percentage"])
        text = serialize_to_string(ucm)
        assert '<metadata name="perf_frequency" value="10"/>' in text
        assert '<metadata name="perf_case_coverage" value="10"/>' in text
        assert ('<metadata name="perf_branch0_percentage" value="60%"/>'
                in text)
        assert ('<metadata name="perf_branch0_mean_time" value="10.0m"/>'
                in text)
        # REGRESSION (jUCMNav FeatureNotFoundException): connections
        # must never carry <metadata> children.
        assert not re.search(r"<connections [^>]*>\s*<metadata", text)

    def test_edge_overlay_survives_export_reimport_render(self):
        # Regression: the app renders from re-imported .jucm bytes;
        # edge overlays must survive that round trip into the PNG.
        ucm = _ucm()
        annotate_performance(ucm, _log(),
                             node_metrics=["frequency"],
                             edge_metrics=["frequency", "percentage"])
        reread = parse_string(serialize_to_string(ucm))
        for style in ("ucm", "bpmn"):
            source = classic.apply(
                reread, parameters={"style": style}).source
            assert "60%" in source, f"edge overlay lost in {style} PNG"
            assert "n=10" in source

    def test_family_assemblies_carry_overlays(self):
        # Combined and umbrella assemblies annotate per map: cell maps
        # (combined) / variant plug-ins (umbrella) from the covering
        # cells' sub-log, the umbrella's shared skeleton from the
        # whole family log.
        from pm4py_ucm.algo.discovery.families import (
            assemble_combined, assemble_umbrella, discover,
        )
        rows = []
        ts = pd.Timestamp("2026-01-01")
        for t, mid, n in (("T1", "B", 6), ("T2", "C", 4)):
            for i in range(n):
                for act in ["A", mid, "D"]:
                    rows.append({"case:concept:name": f"{t}_{i}",
                                 "concept:name": act,
                                 "time:timestamp": ts, "case:t": t})
                    ts += pd.Timedelta(minutes=10)
        df = pd.DataFrame(rows)

        def miner(cell_df):
            seq = sorted({
                tuple(s) for s in cell_df.groupby(
                    "case:concept:name", sort=True,
                )["concept:name"].apply(tuple)
            })[0]
            return T(operator="->",
                     children=[T(label=a) for a in seq])

        family = discover(
            df, ["t"], min_cases=1,
            parameters={"tree_miner": miner,
                        "resource_attribute": False},
        )

        def perf_of(ucm, map_name, resp_name):
            m = next(m for m in ucm.maps if m.name == map_name)
            n = next(n for n in m.nodes
                     if isinstance(n, UCM.RespRef)
                     and n.resp_def.name == resp_name)
            return next((md.value for md in n.metadata
                         if md.name == "_perf"), None)

        combined = assemble_combined(
            family, node_metrics=["frequency"], edge_metrics=[],
        )
        # Per-cell statistics: A appears 6 times in T1, 4 in T2.
        assert perf_of(combined, "T1", "A") == "n=6"
        assert perf_of(combined, "T2", "A") == "n=4"

        umbrella = assemble_umbrella(
            family, node_metrics=["frequency"], edge_metrics=[],
            path_scenarios=False,
        )
        # Shared skeleton: whole-log statistics (A and D shared).
        root = umbrella.maps[0]
        a_node = next(n for n in root.nodes
                      if isinstance(n, UCM.RespRef)
                      and n.resp_def.name == "A")
        assert next(md.value for md in a_node.metadata
                    if md.name == "_perf") == "n=10"
        # Variant plug-ins: covering cells' statistics.
        stub = next(n for n in root.nodes if isinstance(n, UCM.Stub))
        by_expr = {
            b.precondition.expression: b.plugin
            for b in stub.bindings
        }
        b_node = next(n for n in by_expr["t == T1"].nodes
                      if isinstance(n, UCM.RespRef))
        assert next(md.value for md in b_node.metadata
                    if md.name == "_perf") == "n=6"

    def test_segments_cross_static_stubs(self):
        # Decomposed model: A | stub(B1..B4) | D — edge statistics
        # must resolve through the static stub into the plug-in.
        rows = []
        ts = pd.Timestamp("2026-01-01")
        for cid in range(5):
            for act in ["A", "B1", "B2", "B3", "B4", "D"]:
                rows.append({"case:concept:name": f"c{cid}",
                             "concept:name": act,
                             "time:timestamp": ts})
                ts += pd.Timedelta(minutes=10)
        df = pd.DataFrame(rows)

        tree = T(operator="->", children=[
            T(label="A"),
            T(operator="->", children=[
                T(label=f"B{i}") for i in range(1, 5)
            ]),
            T(label="D"),
        ])
        ucm = convert_to_ucm(tree, decomposition={
            "on_root_sequence": True, "on_parallel": False,
            "on_loop": False, "max_leaves_per_map": 100,
            "min_leaves_to_decompose": 3, "balance_ratio": 0.0,
        })
        assert len(ucm.maps) > 1  # decomposition produced a plug-in
        annotate_performance(ucm, df,
                             node_metrics=[], edge_metrics=["frequency"])
        root = ucm.maps[0]
        # The arc leaving A crosses the stub into the plug-in: its
        # segment is (A, B1) with frequency 5 — stored branch-indexed
        # on A itself.
        a_node = next(n for n in root.nodes
                      if isinstance(n, UCM.RespRef)
                      and n.resp_def.name == "A")
        texts = [md.value for md in a_node.metadata
                 if md.name == "_perf_branch0"]
        assert texts == ["5"]
