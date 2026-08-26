"""What a set of scenarios covers, and how two scenarios differ.

Built on :mod:`pm4py_ucm.algo.scenario_traversal`, which records the
elements each scenario actually walked
(:attr:`~pm4py_ucm.algo.scenario_traversal.ScenarioTraversalResult.visited`).
This module turns those records into the two things a reader wants:

* **coverage** — what fraction of the model a set of scenarios exercises,
  and which elements they never touched;
* **comparison** — for two scenarios, what only A did, what only B did,
  and what both did.

The denominator is the **whole model**: every path node and every
connection, across every map. On a decomposed model that includes the
plug-ins, which is the point — a scenario that stops at a stub has not
covered what the stub stands for. It does mean a single scenario reads
low, because one scenario walks one path through a model that contains
every path. That is the honest number, not a fault.

Coverage is a **set**, not a sum: an element a loop entered nine times is
covered once. The hit counts are kept alongside for reporting, since "how
hard did this element work" is a different and also useful question.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..objects.ucm.obj import UCM

#: An element's identity in a coverage set: ``(type name, model id)``. The
#: same key :class:`ScenarioTraversalResult.visits` uses — stable across
#: runs and serialisable, unlike ``id(element)``.
Key = Tuple[str, int]

#: The key type name used for a path segment. Everything else is a node,
#: which is what makes the elements/paths split a one-line predicate.
PATH_KIND = "NodeConnection"


def model_elements(ucm: UCM) -> Dict[Key, Any]:
    """Every path node and connection in the model, keyed for coverage.

    Elements without an ``id`` are skipped: they cannot appear in a
    traversal record either, so counting them in the denominator would
    make full coverage unreachable.
    """
    out: Dict[Key, Any] = {}
    for m in ucm.maps:
        for coll in (getattr(m, "nodes", []), getattr(m, "connections", [])):
            for el in coll:
                ident = getattr(el, "id", None)
                if ident is None:
                    continue
                out[(type(el).__name__, int(ident))] = el
    return out


@dataclass
class Coverage:
    """What a set of scenarios walked, against the whole model."""

    #: Keys walked by at least one of the scenarios.
    covered: Set[Key] = field(default_factory=set)
    #: Every key in the model — the denominator.
    total: Set[Key] = field(default_factory=set)
    #: Summed hit count per covered key, across the scenarios considered.
    hits: Dict[Key, int] = field(default_factory=dict)
    #: Label per key, for tooltips and reports.
    labels: Dict[Key, str] = field(default_factory=dict)
    #: Names of the scenarios this covers.
    scenarios: List[str] = field(default_factory=list)

    @property
    def uncovered(self) -> Set[Key]:
        return self.total - self.covered

    @property
    def fraction(self) -> float:
        return len(self.covered) / len(self.total) if self.total else 0.0

    @property
    def elements(self) -> Tuple[int, int]:
        """``(covered, total)`` over path **nodes**.

        Start and end points, responsibilities, forks and joins, stubs —
        the things drawn on the map. Split out from the paths because a
        run can walk every node and still miss segments, and the two
        readings answer different questions.
        """
        return self._split(nodes=True)

    @property
    def paths(self) -> Tuple[int, int]:
        """``(covered, total)`` over path **segments** (the connections).

        Usually the harder number: a model has more segments than nodes,
        and an alternative that carries no responsibility shows up here
        and nowhere else.
        """
        return self._split(nodes=False)

    def _split(self, nodes: bool) -> Tuple[int, int]:
        def want(key: Key) -> bool:
            is_path = key[0] == PATH_KIND
            return is_path != nodes
        return (sum(1 for k in self.covered if want(k)),
                sum(1 for k in self.total if want(k)))

    @property
    def element_fraction(self) -> float:
        c, t = self.elements
        return c / t if t else 0.0

    @property
    def path_fraction(self) -> float:
        c, t = self.paths
        return c / t if t else 0.0

    def by_kind(self) -> Dict[str, Tuple[int, int]]:
        """``{element type: (covered, total)}``.

        A single percentage hides that a scenario may walk every
        responsibility while leaving most connections untouched, which is
        usually the more interesting reading.
        """
        out: Dict[str, List[int]] = {}
        for kind, _i in self.total:
            out.setdefault(kind, [0, 0])[1] += 1
        for kind, _i in self.covered:
            out.setdefault(kind, [0, 0])[0] += 1
        return {k: (v[0], v[1]) for k, v in sorted(out.items())}


@dataclass
class Comparison:
    """Two scenarios, partitioned into A-only, B-only and both."""

    a_name: str = ""
    b_name: str = ""
    a_only: Set[Key] = field(default_factory=set)
    b_only: Set[Key] = field(default_factory=set)
    both: Set[Key] = field(default_factory=set)
    total: Set[Key] = field(default_factory=set)
    labels: Dict[Key, str] = field(default_factory=dict)

    @property
    def union(self) -> Set[Key]:
        return self.a_only | self.b_only | self.both

    @property
    def neither(self) -> Set[Key]:
        return self.total - self.union

    @property
    def agreement(self) -> float:
        """Jaccard: how much of what either scenario did, both did.

        1.0 means the two walked identical paths, 0.0 means they shared
        nothing at all — which cannot happen for scenarios sharing a start
        point, so a very low number usually means they diverge immediately.
        """
        u = self.union
        return len(self.both) / len(u) if u else 0.0


def coverage(ucm: UCM, results: Iterable[Any]) -> Coverage:
    """Coverage of ``results`` (traversal results) over the whole model."""
    total = set(model_elements(ucm))
    cov = Coverage(total=total)
    for r in results:
        cov.scenarios.append(getattr(r, "scenario", ""))
        visited = getattr(r, "visited", set())
        cov.covered |= set(visited)
        for k in visited:
            cov.hits[k] = cov.hits.get(k, 0) + r.visits.get(k, 0)
            if k not in cov.labels:
                label = r.visit_labels.get(k, "")
                if label:
                    cov.labels[k] = label
    # A scenario can only walk what the model contains; anything else means
    # the results came from a different model than the one being measured.
    stray = cov.covered - total
    if stray:
        raise ValueError(
            f"{len(stray)} visited element(s) are not in this model — the "
            "traversal results and the UCM do not match")
    return cov


def compare(ucm: UCM, result_a: Any, result_b: Any) -> Comparison:
    """Partition two scenarios' coverage into A-only, B-only and both."""
    a = set(getattr(result_a, "visited", set()))
    b = set(getattr(result_b, "visited", set()))
    labels: Dict[Key, str] = {}
    for r in (result_a, result_b):
        for k, lab in getattr(r, "visit_labels", {}).items():
            labels.setdefault(k, lab)
    return Comparison(
        a_name=getattr(result_a, "scenario", "A"),
        b_name=getattr(result_b, "scenario", "B"),
        a_only=a - b, b_only=b - a, both=a & b,
        total=set(model_elements(ucm)), labels=labels,
    )


# ---------------------------------------------------------------------------
# Render bindings
#
# The renderer works in object identity (``id(element)``) because that is
# what graphviz node names are built from; coverage works in model ids so
# it can survive leaving the process. Translating happens here, at the
# boundary, so neither side has to know about the other's keying.
# ---------------------------------------------------------------------------

#: Default A / B / shared colours. Dark green and dark orange differ in
#: hue *and* in lightness, so they stay apart on a printout, on a
#: projector, and for most colour-vision deficiencies — the pairing does
#: not rely on the red/green axis the way the original red/blue did.
#: Purple keeps the intersection distinct from both.
COLOR_A = "#145a32"        # dark green
COLOR_B = "#c2680a"        # dark orange
COLOR_BOTH = "#7b3fa0"     # purple
COLOR_COVERED = "#145a32"


def _index(ucm: UCM) -> Dict[Key, Any]:
    return model_elements(ucm)


def _label_uncovered(index: Dict[Key, Any], keys: Iterable[Key],
                     tips: Dict[int, str], note: str) -> None:
    """Give the *un*covered elements hover text too.

    Not decoration: without it they keep graphviz's default ``<title>``,
    which is the internal object name and embeds a memory address.
    Saying "not covered" is both more useful and less leaky, and it makes
    the whole diagram answer the same question under the cursor.
    """
    for k in keys:
        el = index.get(k)
        if el is None:
            continue
        tips.setdefault(id(el), k[0] + " (id " + str(k[1]) + ")"
                        + chr(10) + note)


def coverage_render(ucm: UCM, cov: Coverage,
                    color: str = COLOR_COVERED) -> Dict[str, Dict[int, Any]]:
    """``{"colors": …, "tooltips": …}`` for a coverage highlight.

    Both are keyed by ``id(element)``, ready to hand to the classic
    renderer as ``coverage_colors`` / ``coverage_tooltips``.
    """
    index = _index(ucm)
    colors: Dict[int, str] = {}
    tips: Dict[int, str] = {}
    for k in cov.covered:
        el = index.get(k)
        if el is None:
            continue
        colors[id(el)] = color
        hits = cov.hits.get(k, 0)
        label = cov.labels.get(k) or k[0]
        n = len(cov.scenarios)
        tips[id(el)] = (
            f"{label}\ncovered — {hits} hit(s) across "
            f"{n} scenario{'s' if n != 1 else ''}")
    _label_uncovered(index, cov.uncovered, tips,
                     "not covered by these scenarios")
    return {"colors": colors, "tooltips": tips}


def comparison_render(ucm: UCM, cmp: Comparison,
                      color_a: str = COLOR_A, color_b: str = COLOR_B,
                      color_both: str = COLOR_BOTH,
                      ) -> Dict[str, Dict[int, Any]]:
    """``{"colors": …, "tooltips": …}`` for an A/B comparison."""
    index = _index(ucm)
    colors: Dict[int, str] = {}
    tips: Dict[int, str] = {}
    for keys, colour, who in ((cmp.a_only, color_a, cmp.a_name),
                              (cmp.b_only, color_b, cmp.b_name),
                              (cmp.both, color_both,
                               f"{cmp.a_name} + {cmp.b_name}")):
        for k in keys:
            el = index.get(k)
            if el is None:
                continue
            colors[id(el)] = colour
            tips[id(el)] = f"{cmp.labels.get(k) or k[0]}\n{who}"
    _label_uncovered(index, cmp.neither, tips,
                     "walked by neither scenario")
    return {"colors": colors, "tooltips": tips}
