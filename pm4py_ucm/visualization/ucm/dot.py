"""Running Graphviz on a model, with a fallback for layouts ``dot`` rejects.

Components are drawn as Graphviz *clusters*. ``dot`` ranks a cluster as a
unit, and on a large map whose path keeps leaving and re-entering the same
components it can fail to rank them at all: it prints ``Error: trouble in
init_rank``, exits non-zero, and what it has written by then is a garbled
picture (overlapping, empty component boxes). The graph itself is fine: no
node sits in two clusters and there is no cycle. Graphviz 8.0.3 and 15.1.0
both do it. It showed up on a 497-node root map with 19 components, mined
at noise threshold 0 from a log mixing 55 processes.

Without the clusters the same map lays out normally. So :func:`pipe` and
:func:`render` try the graph as built and, if ``dot`` fails, run it again
with the component clusters turned into plain subgraphs: the component
boxes are lost on that picture, everything else is kept, and a note in the
picture says so. A failure that is not about the layout (no Graphviz on
the PATH) is not retried and propagates unchanged.
"""
from __future__ import annotations

import re
import subprocess
import warnings

import graphviz

#: Component clusters are named ``cluster_<prefix>c<id>``; the stacked
#: renderer's per-map clusters are ``cluster_<index>`` and must stay, or the
#: maps would lose their frames.
_COMPONENT_CLUSTER = re.compile(r"subgraph cluster_(?!\d+\b)")

FALLBACK_NOTE = ("Component boxes are omitted here: Graphviz could not lay "
                 "this map out with them.")


def without_component_clusters(gviz) -> "graphviz.Source":
    """``gviz`` with its component clusters demoted to plain subgraphs, and
    a note at the bottom of the picture."""
    src = _COMPONENT_CLUSTER.sub("subgraph flat_", gviz.source)
    note = (f'\tgraph [label="{FALLBACK_NOTE}" labelloc=b labeljust=l '
            'fontsize=10 fontcolor="#8a4b00"]\n')
    head, brace, rest = src.partition("{\n")
    return graphviz.Source(head + brace + note + rest,
                           engine=getattr(gviz, "engine", None),
                           format=getattr(gviz, "format", None))


def _retry(gviz, run):
    try:
        return run(gviz)
    except subprocess.CalledProcessError as failure:
        if "cluster_" not in gviz.source:
            raise
        try:
            out = run(without_component_clusters(gviz))
        except subprocess.CalledProcessError:
            raise failure from None
        warnings.warn(
            "Graphviz could not lay a map out with its component boxes "
            "(dot exited with an error); it was drawn without them.",
            stacklevel=3)
        return out


def pipe(gviz, format: str) -> bytes:
    """``gviz.pipe(format=...)``, retried without component clusters when
    ``dot`` fails on the layout."""
    return _retry(gviz, lambda g: g.pipe(format=format))


def render(gviz, **kwargs) -> str:
    """``gviz.render(**kwargs)``, retried the same way."""
    return _retry(gviz, lambda g: g.render(**kwargs))
