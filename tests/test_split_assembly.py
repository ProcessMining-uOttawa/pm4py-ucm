"""One model split along a case attribute (#152).

A log that mixes processes mines into maps that follow no real boundary:
step names shared between unrelated processes tie them together. The split
assembly mines one model per value of the attribute that names the process
and puts them under a root map with one stub per value. Nothing is shared
between values, so each map's overlay comes from its own cases alone.
"""
import pandas as pd
import pytest

import pm4py_ucm
from pm4py_ucm.objects.ucm.obj import UCM

# Two services that share the step names "login" and "pay" but nothing
# else, and a third that behaves exactly like the second.
_TRACES = {
    "permit": [["login", "apply", "review", "pay", "issue"]] * 6
              + [["login", "apply", "reject"]] * 2,
    "refund": [["login", "claim", "pay"]] * 5,
    "rebate": [["login", "claim", "pay"]] * 3,
}


def _log():
    rows, t, n = [], pd.Timestamp("2024-01-01", tz="UTC"), 0
    for service, traces in _TRACES.items():
        for trace in traces:
            n += 1
            for step in trace:
                t += pd.Timedelta(minutes=7)
                rows.append({"case:concept:name": f"c{n:02d}",
                             "concept:name": step, "time:timestamp": t,
                             "service": service})
    return pd.DataFrame(rows)


def _meta(element):
    return {m.name: m.value for m in element._metadata}


def _stubs(ucm_map):
    return [n for n in ucm_map.nodes if isinstance(n, UCM.Stub)]


@pytest.fixture(scope="module")
def split():
    fit = {}
    ucm, family = pm4py_ucm.discover_ucm_by_attribute(
        _log(), "service",
        node_metrics=["traversal_frequency"],
        edge_metrics=["traversal_frequency", "traversal_percentage"],
        return_family=True, fit=fit)
    return ucm, family, fit


def test_root_has_one_static_stub_per_value(split):
    ucm, family, _ = split
    root = ucm.maps[0]
    assert root.name == "Overview"
    stubs = _stubs(root)
    assert sorted(s.name for s in stubs) == ["permit", "rebate", "refund"]
    assert not any(s.dynamic for s in stubs)
    assert not any(isinstance(n, UCM.RespRef) for n in root.nodes)
    assert len([n for n in root.nodes if isinstance(n, UCM.OrFork)]) == 1


def test_no_plugin_is_shared_even_between_identical_values(split):
    """refund and rebate behave identically. The umbrella would merge them
    into one plug-in; here each keeps its own map, so its counts are its
    own."""
    ucm, _, _ = split
    plugins = {s.name: s.bindings[0].plugin for s in _stubs(ucm.maps[0])}
    assert all(len(s.bindings) == 1 for s in _stubs(ucm.maps[0]))
    assert len({id(p) for p in plugins.values()}) == 3
    assert plugins["refund"] is not plugins["rebate"]
    for name, n_cases in (("refund", 5), ("rebate", 3)):
        counts = {r.resp_def.name: _meta(r).get("perf_traversal_frequency")
                  for r in plugins[name].nodes if isinstance(r, UCM.RespRef)}
        assert counts == {"login": str(n_cases), "claim": str(n_cases),
                          "pay": str(n_cases)}


def test_a_value_model_holds_only_its_own_activities(split):
    ucm, _, _ = split
    plugins = {s.name: s.bindings[0].plugin for s in _stubs(ucm.maps[0])}
    acts = {r.resp_def.name for r in plugins["permit"].nodes
            if isinstance(r, UCM.RespRef)}
    assert acts == {"login", "apply", "review", "pay", "issue", "reject"}
    # "pay" in permit counts permit's cases, not every case that pays.
    pay = [r for r in plugins["permit"].nodes
           if isinstance(r, UCM.RespRef) and r.resp_def.name == "pay"][0]
    assert _meta(pay)["perf_traversal_frequency"] == "6"


def test_root_counts_are_the_cases_per_value(split):
    ucm, _, _ = split
    root = ucm.maps[0]
    by_stub = {s.name: _meta(s)["perf_branch0_traversal_frequency"]
               for s in _stubs(root)}
    assert by_stub == {"permit": "8", "refund": "5", "rebate": "3"}
    fork = [n for n in root.nodes if isinstance(n, UCM.OrFork)][0]
    shares = sorted(v for k, v in _meta(fork).items()
                    if k.endswith("_traversal_percentage"))
    assert shares == ["19% of 16", "31% of 16", "50% of 16"]


def test_root_counts_need_no_replay():
    """The event-based fallbacks are exact on the root, so a caller that
    skipped the replay still sees the cases per value there."""
    ucm = pm4py_ucm.discover_ucm_by_attribute(
        _log(), "service", node_metrics=["frequency"],
        edge_metrics=["frequency", "percentage"])
    by_stub = {s.name: _meta(s).get("perf_branch0_traversal_frequency")
               for s in _stubs(ucm.maps[0])}
    assert by_stub == {"permit": "8", "refund": "5", "rebate": "3"}


def test_branches_are_guarded_by_the_attribute(split):
    ucm, _, _ = split
    root = ucm.maps[0]
    guards = {}
    for c in root.connections:
        if c.condition is not None and c.condition.expression:
            guards[c.condition.label] = c.condition.expression
    assert guards == {"permit": "service == permit",
                      "refund": "service == refund",
                      "rebate": "service == rebate"}
    names = [s.name for g in ucm.scenario_groups for s in g.scenarios]
    assert sorted(names) == ["permit", "rebate", "refund"]


def test_fit_is_summed_over_the_values(split):
    _, _, fit = split
    assert fit["total_cases"] == 16
    assert fit["fitting_cases"] == 16


def test_every_value_is_kept_and_none_is_merged():
    """No 'Other' bucket and no minimum size: a rare service is still a
    process of its own."""
    log = _log()
    extra = []
    for k in range(20):
        extra.append({"case:concept:name": f"x{k}", "concept:name": "solo",
                      "time:timestamp": pd.Timestamp("2024-02-01", tz="UTC"),
                      "service": f"svc{k:02d}"})
    log = pd.concat([log, pd.DataFrame(extra)], ignore_index=True)
    ucm, family = pm4py_ucm.discover_ucm_by_attribute(
        log, "service", return_family=True)
    assert len(family.cells) == 23
    assert len(_stubs(ucm.maps[0])) == 23
    assert not any("Other" in s.name for s in _stubs(ucm.maps[0]))


def test_decomposition_applies_within_each_value():
    flat = pm4py_ucm.discover_ucm_by_attribute(_log(), "service")
    cut = pm4py_ucm.discover_ucm_by_attribute(
        _log(), "service",
        decomposition={"on_root_sequence": True, "on_parallel": True,
                       "on_alternative": True, "on_loop": True,
                       "max_leaves_per_map": 3,
                       "min_leaves_to_decompose": 2, "balance_ratio": 0.0})
    assert len(flat.maps) == 4
    assert len(cut.maps) > 4
    assert len(_stubs(cut.maps[0])) == 3


def test_split_model_survives_the_jucm_round_trip(tmp_path, split):
    ucm, _, _ = split
    path = tmp_path / "split.jucm"
    pm4py_ucm.write_ucm(ucm, str(path))
    back = pm4py_ucm.read_ucm(str(path))
    assert [m.name for m in back.maps] == [m.name for m in ucm.maps]
    assert len(_stubs(back.maps[0])) == 3


def test_split_is_a_mode_of_the_family_assembly():
    family = pm4py_ucm.discover_ucm_family(_log(), ["service"], min_cases=1)
    ucm = pm4py_ucm.assemble_ucm_family(family, mode="split")
    assert len(_stubs(ucm.maps[0])) == 3
    with pytest.raises(ValueError, match="'split'"):
        pm4py_ucm.assemble_ucm_family(family, mode="nope")


# -- Families: a wide attribute can be chosen, and is then cut down ---------

def _wide_log(n_values):
    rows, t = [], pd.Timestamp("2024-01-01", tz="UTC")
    for v in range(n_values):
        for k in range(v % 3 + 1):          # unequal sizes: a clear top few
            for step in ("a", "b"):
                t += pd.Timedelta(minutes=1)
                rows.append({"case:concept:name": f"c{v}_{k}",
                             "concept:name": step, "time:timestamp": t,
                             "office": f"office{v:02d}"})
    return pd.DataFrame(rows)


def test_family_accepts_an_attribute_with_60_values_but_not_61():
    from pm4py_ucm.algo.discovery.families import detect_case_attributes
    specs, _ = detect_case_attributes(_wide_log(60))
    assert "office" in {s.source_name for s in specs.values()}
    specs, _ = detect_case_attributes(_wide_log(61))
    assert "office" not in {s.source_name for s in specs.values()}


def test_a_wide_family_axis_is_still_capped_with_an_other_bucket():
    family = pm4py_ucm.discover_ucm_family(
        _wide_log(60), ["office"], min_cases=1, max_values_per_attribute=20)
    labels = [c.label for c in family.cells]
    assert len(labels) == 20
    assert "Other" in labels
