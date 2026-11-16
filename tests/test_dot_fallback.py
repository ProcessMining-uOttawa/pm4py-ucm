"""A map Graphviz cannot lay out with its component boxes is drawn without
them, rather than not at all.

``dot`` ranks each component cluster as a unit and, on a large map whose
path keeps re-entering the same components, can fail with ``Error: trouble
in init_rank``. The graph is valid and lays out fine once the clusters are
plain subgraphs. Found on the root map of a 55-process log mined at noise
threshold 0, where the Scenarios view reported a missing Graphviz instead.
"""
import shutil
import subprocess
import warnings

import pytest

import pm4py_ucm
from pm4py_ucm.visualization.ucm import dot as _dot

_SRC = """digraph G {
\tsubgraph cluster_0 {
\t\tlabel="a map frame"
\t\tsubgraph cluster_c123 {
\t\t\tlabel=Clerk
\t\t\tn1
\t\t}
\t\tsubgraph cluster_m0_c456 {
\t\t\tlabel=Agent
\t\t\tn2
\t\t}
\t}
\tn1 -> n2
}
"""


class _Graph:
    """Stands in for a Digraph whose layout fails while it has component
    clusters, the way ``dot`` does on the real map."""

    engine = "dot"
    format = "svg"

    def __init__(self, source=_SRC, error=None):
        self.source = source
        self.error = error or subprocess.CalledProcessError(
            1, ["dot"], b"", b"Error: trouble in init_rank\n")


def _failing_run(seen):
    def run(g):
        seen.append(g.source)
        if "cluster_c" in g.source or "cluster_m0_c" in g.source:
            raise _Graph().error
        return "drawn"
    return run


def test_layout_failure_is_retried_without_component_clusters():
    seen = []
    with pytest.warns(UserWarning, match="without them"):
        assert _dot._retry(_Graph(), _failing_run(seen)) == "drawn"
    assert len(seen) == 2
    retried = seen[1]
    # Component clusters are demoted; a per-map frame is not one of them.
    assert "subgraph flat_c123" in retried
    assert "subgraph flat_m0_c456" in retried
    assert "subgraph cluster_0 {" in retried
    assert "n1 -> n2" in retried
    assert _dot.FALLBACK_NOTE in retried


def test_a_layout_that_works_is_left_alone():
    seen = []
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        out = _dot._retry(_Graph(), lambda g: seen.append(g.source) or "ok")
    assert out == "ok"
    assert seen == [_SRC]


def test_the_original_failure_is_raised_when_the_retry_fails_too():
    first = subprocess.CalledProcessError(1, ["dot"], b"", b"Error: first\n")
    calls = []

    def run(g):
        calls.append(g)
        raise first if len(calls) == 1 else subprocess.CalledProcessError(
            1, ["dot"], b"", b"Error: second\n")

    with pytest.raises(subprocess.CalledProcessError) as caught:
        _dot._retry(_Graph(), run)
    assert caught.value is first
    assert len(calls) == 2


def test_a_graph_without_clusters_is_not_retried():
    calls = []

    def run(g):
        calls.append(g)
        raise subprocess.CalledProcessError(1, ["dot"], b"", b"Error: x\n")

    with pytest.raises(subprocess.CalledProcessError):
        _dot._retry(_Graph("digraph G {\n\tn1 -> n2\n}\n"), run)
    assert len(calls) == 1


def test_a_missing_graphviz_is_not_a_layout_failure():
    """Not a CalledProcessError: nothing to retry, and the caller must see
    it for what it is."""
    import graphviz

    def run(g):
        raise graphviz.ExecutableNotFound(["dot"])

    with pytest.raises(graphviz.ExecutableNotFound):
        _dot._retry(_Graph(), run)


def _drawing(svg: bytes):
    """An SVG as the sorted list of its lines, with Graphviz's running
    element numbers blanked: equal for two renders of one drawing, even
    when the elements were emitted in a different order."""
    import re
    text = re.sub(r'id="(clust|node|edge)\d+"', r'id="\1"',
                  svg.decode("utf-8"))
    return sorted(text.splitlines())


@pytest.mark.skipif(shutil.which("dot") is None, reason="needs Graphviz")
def test_the_declustered_picture_of_a_real_model_still_draws():
    import pandas as pd
    from pm4py_ucm.visualization.ucm.variants import classic

    rows, t = [], pd.Timestamp("2024-01-01", tz="UTC")
    for n in range(6):
        for step, who in (("a", "Clerk"), ("b", "Agent"), ("c", "Clerk")):
            t += pd.Timedelta(minutes=3)
            rows.append({"case:concept:name": f"c{n}", "concept:name": step,
                         "time:timestamp": t, "org:role": who})
    ucm = pm4py_ucm.discover_ucm_inductive(
        pd.DataFrame(rows), parameters={"resource_attribute": "org:role"})
    gviz = classic.apply(ucm, parameters={"style": "ucm", "format": "svg"})
    assert "subgraph cluster_" in gviz.source          # components as clusters
    flat = _dot.without_component_clusters(gviz)
    assert "subgraph cluster_" not in flat.source
    svg = flat.pipe(format="svg").decode("utf-8")
    assert svg.rstrip().endswith("</svg>")
    assert "Component boxes are omitted" in svg
    # And the ordinary path is unchanged: the same drawing as a direct
    # pipe. Not the same bytes: Graphviz does not emit the clusters of one
    # source in a fixed order (8.0.3 on Windows gave two outputs for eight
    # renders), and their ``clustN`` ids follow that order. So compare the
    # elements, whatever order and numbering they came out in.
    assert (_drawing(_dot.pipe(gviz, "svg"))
            == _drawing(gviz.pipe(format="svg")))
