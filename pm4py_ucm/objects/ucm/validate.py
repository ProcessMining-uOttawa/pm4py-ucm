"""Structural well-formedness of a UCM.

A model can be perfectly serialisable and still be nonsense: jUCMNav's
metamodel constrains how many path segments may enter and leave each kind
of path node, and nothing in this library enforced it. A responsibility
with two incoming segments is not a UCM — but it exports, it renders, and
it even traverses, so the error surfaces only when a human opens the
``.jucm`` and notices the picture is wrong.

The rules are the arity ones, which is where the generators can plausibly
go astray:

===============  =========  ==========  ====================================
node             in         out         notes
===============  =========  ==========  ====================================
StartPoint       0          1           the path begins here
EndPoint         1          0           and ends here
RespRef          1          1           a responsibility is a point on ONE path
EmptyPoint       1          1           a routing bend
DirectionArrow   1          1           a layout marker on one path
OrFork           1          >= 2        one in, a choice of ways out
AndFork          1          >= 2        one in, every way out
OrJoin           >= 2       1           alternatives merge
AndJoin          >= 2       1           parallel branches synchronise
Stub             >= 1       >= 1        a call site; bindings do the rest
WaitingPlace     1          1           (also Timer, its subclass)
Connect          >= 1       >= 1        an asynchronous meeting point
FailurePoint     1          1
===============  =========  ==========  ====================================

``Anything`` is unconstrained: it is the aspect-oriented wildcard, and its
whole point is to match whatever is there.

This is deliberately **not** a semantic check. It says nothing about
whether guards select exactly one branch, whether the model deadlocks, or
whether an end point is reachable — that is what
:mod:`pm4py_ucm.algo.scenario_traversal` is for, and it can only run on a
model that is structurally sound in the first place.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .obj import UCM

#: ``node class -> (expected in-degree, expected out-degree)``. ``None``
#: means "two or more"; a number means exactly that many.
ARITY: Dict[type, Tuple[Optional[int], Optional[int]]] = {
    UCM.StartPoint: (0, 1),
    UCM.EndPoint: (1, 0),
    UCM.RespRef: (1, 1),
    UCM.EmptyPoint: (1, 1),
    UCM.DirectionArrow: (1, 1),
    UCM.OrFork: (1, None),
    UCM.AndFork: (1, None),
    UCM.OrJoin: (None, 1),
    UCM.AndJoin: (None, 1),
    UCM.WaitingPlace: (1, 1),
    UCM.FailurePoint: (1, 1),
}

#: Kinds where "at least one" is the only rule. A stub's real constraints
#: live in its plug-in bindings, and a Connect is an asynchronous meeting
#: point whose arity depends on what meets there.
AT_LEAST_ONE = (UCM.Stub, UCM.Connect)


@dataclass
class StructuralProblem:
    """One node whose path-segment arity is wrong."""

    map_name: str
    node_type: str
    node_id: Optional[int]
    node_name: str
    detail: str

    def __str__(self) -> str:
        where = f"{self.node_type}"
        if self.node_name:
            where += f" {self.node_name!r}"
        if self.node_id is not None:
            where += f" (id {self.node_id})"
        return f"[{self.map_name}] {where}: {self.detail}"


def _describe(expected: Optional[int]) -> str:
    return "at least 2" if expected is None else str(expected)


def validate_ucm(ucm: UCM) -> List[StructuralProblem]:
    """Report every path node whose in/out arity breaks the metamodel.

    Returns an empty list for a well-formed model. Reports rather than
    raises: a caller checking a mined model wants the whole picture, not
    the first fault.
    """
    problems: List[StructuralProblem] = []
    for m in ucm.maps:
        indeg: Dict[int, int] = {}
        outdeg: Dict[int, int] = {}
        for c in getattr(m, "connections", []):
            outdeg[id(c.source)] = outdeg.get(id(c.source), 0) + 1
            indeg[id(c.target)] = indeg.get(id(c.target), 0) + 1
        map_name = getattr(m, "name", "") or "?"
        for node in getattr(m, "nodes", []):
            if isinstance(node, UCM.Anything):
                continue
            i = indeg.get(id(node), 0)
            o = outdeg.get(id(node), 0)
            if isinstance(node, AT_LEAST_ONE):
                bad = []
                if i < 1:
                    bad.append(f"{i} incoming (expected at least 1)")
                if o < 1:
                    bad.append(f"{o} outgoing (expected at least 1)")
            else:
                # Exact class match, not isinstance: Timer subclasses
                # WaitingPlace and RespRef/others share PathNode, so an
                # isinstance walk would pick the wrong rule.
                rule = ARITY.get(type(node))
                if rule is None:
                    continue
                exp_in, exp_out = rule
                bad = []
                if exp_in is None:
                    if i < 2:
                        bad.append(f"{i} incoming (expected at least 2)")
                elif i != exp_in:
                    bad.append(f"{i} incoming (expected {exp_in})")
                if exp_out is None:
                    if o < 2:
                        bad.append(f"{o} outgoing (expected at least 2)")
                elif o != exp_out:
                    bad.append(f"{o} outgoing (expected {exp_out})")
            if bad:
                problems.append(StructuralProblem(
                    map_name=map_name,
                    node_type=type(node).__name__,
                    node_id=getattr(node, "id", None),
                    node_name=getattr(node, "name", "") or "",
                    detail="; ".join(bad),
                ))
    return problems


def check_ucm(ucm: UCM) -> None:
    """Raise :class:`ValueError` if the model is not well-formed.

    For callers that would rather fail than inspect — a test, or a
    pipeline step that must not pass a malformed model downstream.
    """
    problems = validate_ucm(ucm)
    if problems:
        joined = "\n  ".join(str(p) for p in problems)
        raise ValueError(
            f"{len(problems)} structural problem(s) in this UCM:\n  {joined}")


def check_generated(ucm: UCM, produced_by: str) -> UCM:
    """Refuse to hand back a structurally invalid model this library just built.

    The counterpart to :func:`check_ucm`, for the *generators* rather than the
    exporters, and the difference is whose fault a failure is. An exporter may
    legitimately be handed a malformed model — jUCMNav accepts files this check
    rejects, so round-tripping one imported from elsewhere has to stay
    possible. A generator built the model here, from a process tree, so no
    input of the caller's can be at fault: the message says so and asks for a
    report, and ``produced_by`` names the step to report.

    Internal to the library's generation paths (conversion, discovery,
    synthesis, family mining and assembly); callers use the ``validate=``
    keyword on those functions rather than this directly.
    """
    problems = validate_ucm(ucm)
    if not problems:
        return ucm
    joined = "\n  ".join(str(x) for x in problems)
    raise ValueError(
        f"{produced_by} produced a structurally invalid UCM "
        f"({len(problems)} problem(s) against jUCMNav's metamodel). This is a "
        f"bug in pm4py-ucm rather than a problem with your log — please "
        f"report it, with the settings used. Pass validate=False to receive "
        f"the model anyway.\n  {joined}"
    )
