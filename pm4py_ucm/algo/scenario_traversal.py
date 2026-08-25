"""Simulate jUCMNav's scenario traversal over a UCM, offline.

Exporting a syntactically valid ``.jucm`` is not the same as exporting a
model whose scenarios *run*. jUCMNav executes a scenario by pushing a
token from each enabled start point and following the path: an OR-fork
picks the branch whose guard holds, an AND-fork spawns a token per arm,
and an AND-join waits until every one of its incoming arms has delivered
one. A model can be perfectly well-formed as XML and still deadlock —
which is what a nested loop containing parallelism does when one arm's
completion depends on a loop whose counter is decremented on a path that
is itself waiting at a join.

That failure is invisible to a static check of the branch guards. Asking
"does exactly one branch hold at each fork, under this scenario's
initializations?" answers a different question: it says nothing about
whether the resulting token flow ever reaches the end point, and it
cannot be asked at all of forks *inside* a loop, whose guards read a
counter that only changes during traversal. This module closes that gap
by actually running the tokens, and reports the same four problem kinds
jUCMNav reports in its Problems view:

* ``blocked_and_join`` — an AND-join that received some but not all of
  its arms and never fired;
* ``infinite_loop`` — one element was entered as many times as
  jUCMNav's *maximum hit count* preference allows. That ceiling is a
  setting, not a property of the model, so see
  :func:`required_max_hit_count` before concluding a model is at fault;
* ``end_point_not_reached`` — a mandatory end point never got a token;
* ``no_branch_enabled`` / ``multiple_branches_enabled`` — an OR-fork
  whose guards do not select exactly one branch.

Unsupported node kinds are reported rather than skipped
(``unsupported_node``): a simulator that quietly ignores a construct it
cannot execute would report success on models it never really ran, which
is the exact failure this module exists to prevent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple
import re

from ..objects.ucm.obj import UCM


#: Hit count at which a *single* element is declared to be looping —
#: jUCMNav's ``ScenarioTraversalPreferences.DEFAULT_MAXHITCOUNT``. Note
#: this is per element, not a global step budget: a model can execute a
#: great many steps legitimately, and still be caught the moment one
#: node is entered for the thousandth time.
DEFAULT_MAX_HIT_COUNT = 1000

#: Global ceiling on scheduler iterations. Not a jUCMNav concept — a
#: backstop so a port bug cannot hang the caller.
DEFAULT_MAX_STEPS = 2_000_000


class ExpressionError(Exception):
    """A guard or action could not be evaluated."""


# ---------------------------------------------------------------------------
# The jUCMNav expression sub-language
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""(?P<ws>\s+)
      | (?P<num>\d+(?:\.\d+)?)
      | (?P<op>&&|\|\||==|!=|<=|>=|[<>+\-*/()!])
      | (?P<id>[A-Za-z_][A-Za-z_0-9]*)
    """,
    re.VERBOSE,
)

_OP_MAP = {"&&": " and ", "||": " or ", "!": " not "}

#: ``Loop_X = Loop_X - 1`` but not ``a == b`` / ``a != b`` / ``a <= b``.
_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z_0-9]*)\s*=(?![=])\s*(.+?)\s*$")


def _compile(expr: str, var_names: Iterable[str]):
    """Compile a jUCMNav expression to a callable over a value map.

    An identifier naming a declared variable becomes a lookup; every
    other identifier is an enumeration literal standing for itself, so
    ``variant_id == v1`` compares the variable against the string
    ``"v1"``. This mirrors how jUCMNav resolves bare identifiers, and is
    why no quoting appears anywhere in a generated condition.
    """
    names = set(var_names)
    text = (expr or "").strip()
    if not text:
        return lambda values: True
    out: List[str] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise ExpressionError(f"cannot tokenise at {text[pos:pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup
        tok = m.group()
        if kind == "ws":
            continue
        if kind == "op":
            out.append(_OP_MAP.get(tok, tok))
        elif kind == "num":
            out.append(tok)
        else:
            low = tok.lower()
            if low in ("true", "false"):
                out.append(low.capitalize())
            elif tok in names:
                out.append(f"V[{tok!r}]")
            else:
                out.append(repr(tok))
    try:
        code = compile("".join(out), "<ucm-expr>", "eval")
    except SyntaxError as exc:
        raise ExpressionError(f"{expr!r}: {exc}") from exc

    def _run(values, _code=code):
        try:
            return eval(_code, {"__builtins__": {}}, {"V": values})
        except KeyError as exc:
            raise ExpressionError(f"uninitialised variable {exc}") from exc
        except TypeError as exc:
            raise ExpressionError(f"type mismatch in {expr!r}: {exc}") from exc
        except Exception as exc:  # pragma: no cover - defensive
            raise ExpressionError(f"{expr!r}: {exc}") from exc

    return _run


def _coerce(raw: str):
    """Turn an initialization's string value into a comparable Python one."""
    if isinstance(raw, bool) or not isinstance(raw, str):
        return raw
    s = raw.strip()
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _default_value(variable) -> Any:
    """The value a declared variable holds before any initialization.

    Mirrors ``UcmEnvironment.refresh``, which registers every declared
    variable with a default the moment the environment is built:
    ``registerBoolean(name)`` → ``false``, ``registerInteger(name)`` →
    ``0``, and ``registerEnumerationInstance(type, name)`` → the
    enumeration's *first* value. Consequently a guard may reference an
    attribute no scenario initializes and still evaluate — which is why
    such a guard is not, by itself, a defect.
    """
    kind = (getattr(variable, "type", "") or "").strip().lower()
    if kind == "integer":
        return 0
    if kind == "enumeration":
        et = getattr(variable, "enumeration_type", None)
        values = list(getattr(et, "values", []) or [])
        return values[0] if values else ""
    return False


def _run_actions(expression: str, values: Dict[str, Any],
                 var_names: Iterable[str]) -> None:
    """Apply a responsibility's action expression to ``values``.

    Actions are ``;``-separated assignments — the loop-counter decrements
    scenario synthesis emits (``Loop_X = Loop_X - 1;``). A statement that
    is not an assignment is ignored: jUCMNav tolerates descriptive
    expressions on responsibilities, and treating one as an error would
    fail models that run perfectly well.
    """
    for stmt in (expression or "").split(";"):
        if not stmt.strip():
            continue
        m = _ASSIGN_RE.match(stmt)
        if not m:
            continue
        target, rhs = m.group(1), m.group(2)
        values[target] = _compile(rhs, var_names)(values)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class TraversalProblem:
    """One reason a scenario did not execute cleanly."""

    kind: str
    scenario: str
    node_id: Optional[int] = None
    node_name: str = ""
    node_type: str = ""
    detail: str = ""

    def __str__(self) -> str:
        where = ""
        if self.node_type or self.node_id is not None:
            label = f" (name: {self.node_name})" if self.node_name else ""
            where = f" on {self.node_type}{label} (id: {self.node_id})"
        tail = f" — {self.detail}" if self.detail else ""
        return f"[{self.scenario}] {self.kind}{where}{tail}"


@dataclass
class ScenarioTraversalResult:
    """Outcome of running one scenario."""

    scenario: str
    problems: List[TraversalProblem] = field(default_factory=list)
    responsibilities: List[str] = field(default_factory=list)
    reached_end_points: List[str] = field(default_factory=list)
    steps: int = 0
    #: Highest number of times any single element was entered, and what
    #: that element was. jUCMNav declares an infinite loop once one
    #: element reaches its *maximum hit count* preference, so this is the
    #: number to compare that setting against — see
    #: :func:`required_max_hit_count`.
    peak_hit_count: int = 0
    peak_hit_element: str = ""
    #: Net hit count per element the traversal entered, keyed by
    #: ``(type name, model id)``. "Net" because a node that blocks is
    #: un-counted when it re-enters the waiting list, exactly as jUCMNav's
    #: ``decrementHitCount`` does — so this is the number of times the
    #: element actually executed, not the number of times it was tried.
    #: Covers connections as well as nodes, since a path is both.
    #:
    #: Keyed by the model id rather than object identity so the record can
    #: be compared, serialised and matched against a rendered diagram. Ids
    #: are unique per ``(type, id)`` pair within a model; the type is part
    #: of the key because nodes and connections share one id counter.
    visits: Dict[Tuple[str, int], int] = field(default_factory=dict)
    #: Human-readable label per key in :attr:`visits`, for tooltips and
    #: problem reports.
    visit_labels: Dict[Tuple[str, int], str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def visited(self) -> "set":
        """The keys in :attr:`visits` that actually executed.

        A key can sit at zero: blocking decrements, so an element that was
        entered and then evicted without ever completing is recorded but
        did not run. Coverage must not count it.
        """
        return {k for k, c in self.visits.items() if c > 0}


# ---------------------------------------------------------------------------
# The traversal
# ---------------------------------------------------------------------------

_PASS_THROUGH = (UCM.EmptyPoint, UCM.DirectionArrow, UCM.OrJoin)
_UNSUPPORTED = (UCM.Stub, UCM.WaitingPlace, UCM.Timer, UCM.Connect,
                UCM.FailurePoint, UCM.Anything)

def _elem_key(el) -> Tuple[str, int]:
    """Identity for hit-count bookkeeping. Connections and nodes share the
    counter namespace in jUCMNav, so keep them distinguishable."""
    return (type(el).__name__, id(el))


class _InfiniteLoop(Exception):
    """One element was entered ``max_hit_count`` times."""

    def __init__(self, element) -> None:
        super().__init__("infinite loop")
        self.element = element


class _Traversal:
    """A port of jUCMNav's ``DefaultScenarioTraversal`` scheduler.

    Faithfulness matters more than elegance: the point is to agree with
    jUCMNav, so the structure mirrors the Java — a LIFO ``_to_visit``
    stack, a FIFO ``_wait_list`` holding at most one entry per path node,
    per-element hit counts over *both* nodes and connections, and the
    ``_consecutive_reblocks`` counter that decides when a stalled
    traversal is declared blocked.

    The behaviour a hand-rolled token model misses: when every waiting
    node has been retried without progress, jUCMNav does **not** stop. It
    reports "Traversal blocked on <node>", forcibly evicts that node from
    the waiting list, and carries on — which is how one stuck AND-join
    cascades into further blocks and, eventually, into an element
    reaching the loop ceiling.
    """

    def __init__(self, ucm: UCM, scenario, max_hit_count: int,
                 max_steps: int, patient_on_preconditions: bool = True) -> None:
        self.ucm = ucm
        self.patient_on_preconditions = patient_on_preconditions
        self.scenario = scenario
        self.max_hit_count = max_hit_count
        self.max_steps = max_steps
        self.var_names = {v.name for v in ucm.variables}
        # Every *declared* variable holds its type default before the
        # scenario's own initializations are applied, exactly as
        # jUCMNav's UcmEnvironment.refresh() registers each variable up
        # front (boolean -> false, integer -> 0, enumeration -> the first
        # value of its type). Without this, a guard naming an attribute
        # the scenario never initializes looks like an error, when
        # jUCMNav evaluates it perfectly happily against the default —
        # the difference between reporting a model broken and reporting
        # it fine.
        self.values: Dict[str, Any] = {}
        for var in ucm.variables:
            self.values[var.name] = _default_value(var)
        for init in scenario.initializations:
            self.values[init.variable.name] = _coerce(init.value)
        self.hits: Dict[Tuple[str, int], int] = {}
        #: Human-readable label per counted element, for reporting which
        #: one is closest to jUCMNav's ceiling.
        self.hit_labels: Dict[Tuple[str, int], str] = {}
        #: The element behind each counted key. ``hits`` is keyed by object
        #: identity (a memory address, which is right for counting but
        #: meaningless outside this process), so keep the object itself in
        #: order to publish a model-id-keyed record at the end.
        self.hit_elements: Dict[Tuple[str, int], Any] = {}
        self.to_visit: List[Any] = []        # stack of PathNode
        self.wait_list: List[Any] = []       # queue of PathNode
        self.consecutive_reblocks = 0
        self.last_popped: Optional[Any] = None
        self.reached: List[Any] = []
        self.result = ScenarioTraversalResult(scenario=scenario.name)
        self._cache: Dict[str, Any] = {}

    # -- bookkeeping ----------------------------------------------------

    def hit(self, el) -> int:
        return self.hits.get(_elem_key(el), 0)

    def track(self, el) -> None:
        key = _elem_key(el)
        self.hit_elements.setdefault(key, el)
        if key not in self.hit_labels:
            name = getattr(el, "name", "") or ""
            ident = getattr(el, "id", None)
            self.hit_labels[key] = (
                f"{type(el).__name__}"
                + (f" {name!r}" if name else "")
                + (f" (id {ident})" if ident is not None else "")
            )
        self.hits[key] = self.hits.get(key, 0) + 1
        if self.hits[key] >= self.max_hit_count:
            raise _InfiniteLoop(el)

    def untrack(self, el) -> None:
        key = _elem_key(el)
        if key in self.hits:
            self.hits[key] -= 1

    def problem(self, kind, node=None, detail="") -> None:
        self.result.problems.append(TraversalProblem(
            kind=kind, scenario=self.scenario.name,
            node_id=getattr(node, "id", None),
            node_name=getattr(node, "name", "") or "",
            node_type=type(node).__name__ if node is not None else "",
            detail=detail,
        ))

    def guard(self, expr: str):
        if expr not in self._cache:
            self._cache[expr] = _compile(expr, self.var_names)
        return self._cache[expr]

    # -- scheduling -----------------------------------------------------

    def add_to_waiting_list(self, node) -> None:
        # Blocking undoes the visit, so a retry does not inflate the hit
        # count (jUCMNav's ``decrementHitCount`` inside addToWaitingList).
        self.untrack(node)
        if node not in self.wait_list:
            self.wait_list.append(node)
        if self.last_popped is not None and node is self.last_popped:
            self.consecutive_reblocks += 1

    def visit_connection(self, conn) -> None:
        self.track(conn)
        self.to_visit.append(conn.target)

    def next_visit(self):
        """``(node, blocked_node)`` — jUCMNav's ``getNextVisit``.

        ``blocked_node`` is set when the traversal deadlocked: every
        waiting node has been retried without progress.
        """
        if not self.to_visit and not self.wait_list:
            self.last_popped = None
            self.consecutive_reblocks = 0
            return None, None
        if self.to_visit:
            self.last_popped = self.to_visit.pop()
            if self.last_popped in self.wait_list:
                self.wait_list.remove(self.last_popped)
            return self.last_popped, None
        if self.consecutive_reblocks >= len(self.wait_list):
            return None, self.wait_list[0]
        self.last_popped = self.wait_list.pop(0)
        return self.last_popped, None

    # -- node semantics -------------------------------------------------

    def process(self, node) -> None:
        self.track(node)

        if isinstance(node, _UNSUPPORTED):
            self.problem("unsupported_node", node,
                         f"{type(node).__name__} is not simulated; this "
                         f"scenario's verdict would be unsound")
            return

        if isinstance(node, UCM.EndPoint):
            if node not in self.reached:
                self.reached.append(node)
                self.result.reached_end_points.append(node.name)
            return

        if isinstance(node, UCM.RespRef):
            rd = getattr(node, "resp_def", None)
            self.result.responsibilities.append(
                getattr(rd, "name", None) or node.name)
            if rd is not None and getattr(rd, "expression", ""):
                try:
                    _run_actions(rd.expression, self.values, self.var_names)
                except ExpressionError as exc:
                    self.problem("expression_error", node, str(exc))

        if isinstance(node, UCM.AndJoin):
            succ = node.succ_connections
            if len(succ) != 1:
                self.problem("malformed_and_join", node,
                             f"{len(succ)} outgoing connections, expected 1")
                return
            out = succ[0]
            for inc in node.pred_connections:
                if self.hit(out) + 1 > self.hit(inc):
                    self.add_to_waiting_list(node)
                    return
            self.visit_connection(out)
            return

        if isinstance(node, UCM.OrFork):
            enabled = []
            for c in node.succ_connections:
                expr = getattr(getattr(c, "condition", None), "expression", "")
                try:
                    if bool(self.guard(expr)(self.values)):
                        enabled.append(c)
                except ExpressionError as exc:
                    self.problem("expression_error", node, str(exc))
            if enabled:
                if len(enabled) > 1:
                    self.problem(
                        "multiple_branches_enabled", node,
                        f"{len(enabled)} guards hold; taking the first to "
                        f"stay deterministic")
                self.visit_connection(enabled[0])
            elif self.patient_on_preconditions:
                # jUCMNav's default (``DEFAULT_ISPATIENTONPRECONDITIONS``)
                # is to *wait*, not to fail: a guard that is false now may
                # be true once another branch has run and updated a
                # variable. The token parks instead of dying, which is why
                # a fork with no true branch shows up later as a blocked
                # traversal rather than as an error at the fork itself.
                self.add_to_waiting_list(node)
            else:
                self.problem("no_branch_enabled", node,
                             "no outgoing guard evaluates to true")
            return

        # Start points, responsibilities, OR-joins and pass-throughs
        # continue along every outgoing connection — one each, except at
        # an AND-fork, where the parallel branches are spawned here.
        succ = node.succ_connections
        if not succ:
            self.problem("dead_end", node, "node has no outgoing connection")
            return
        for c in succ:
            self.visit_connection(c)

    # -- driver ---------------------------------------------------------

    def run(self) -> ScenarioTraversalResult:
        started = False
        for sp in self.scenario.start_points:
            if getattr(sp, "enabled", True):
                self.to_visit.append(sp.start_point)
                started = True
        if not started:
            self.problem("no_start_point", None,
                         "scenario enables no start point")
            return self.result

        steps = 0
        while True:
            steps += 1
            if steps > self.max_steps:
                self.problem("step_budget_exhausted", None,
                             f"scheduler exceeded {self.max_steps} iterations")
                break
            node, blocked = self.next_visit()
            if blocked is not None:
                self.problem(
                    "blocked_and_join" if isinstance(blocked, UCM.AndJoin)
                    else "traversal_blocked", blocked,
                    "every waiting node was retried without progress",
                )
                self.wait_list.remove(blocked)
                self.consecutive_reblocks = 0
                continue
            if node is None:
                break
            try:
                self.process(node)
            except _InfiniteLoop as loop:
                self.problem("infinite_loop", loop.element,
                             f"entered {self.max_hit_count} times")
                break

        self.result.steps = steps
        if self.hits:
            key, count = max(self.hits.items(), key=lambda kv: kv[1])
            self.result.peak_hit_count = count
            self.result.peak_hit_element = self.hit_labels.get(key, "")
        self._publish_visits()
        self._verify_end_points()
        return self.result

    def _publish_visits(self) -> None:
        """Re-key the internal hit counts by model id for the caller.

        An element whose ``id`` is unset cannot be addressed from outside
        this process, so it is dropped rather than published under a key
        that would collide with every other id-less element of its type.
        Mined and imported models set every id; this guards hand-built
        ones.
        """
        for key, count in self.hits.items():
            el = self.hit_elements.get(key)
            ident = getattr(el, "id", None)
            if ident is None:
                continue
            pub = (type(el).__name__, int(ident))
            self.result.visits[pub] = self.result.visits.get(pub, 0) + count
            self.result.visit_labels[pub] = self.hit_labels.get(key, "")

    def _verify_end_points(self) -> None:
        for ep in self.scenario.end_points:
            if not getattr(ep, "enabled", True):
                continue
            if not getattr(ep, "mandatory", True):
                continue
            if ep.end_point not in self.reached:
                self.problem("end_point_not_reached", ep.end_point,
                             "scenario should have reached this end point")


def traverse_scenario(
    ucm: UCM,
    scenario: "UCM.ScenarioDef",
    max_hit_count: int = DEFAULT_MAX_HIT_COUNT,
    max_steps: int = DEFAULT_MAX_STEPS,
    patient_on_preconditions: bool = True,
) -> ScenarioTraversalResult:
    """Run one scenario the way jUCMNav would, and report what it reports."""
    return _Traversal(ucm, scenario, max_hit_count, max_steps,
                      patient_on_preconditions).run()


class NoScenariosError(Exception):
    """The model carries no scenario, so nothing could be traversed.

    Raised rather than returning "no problems found": a caller asking
    whether a model executes cleanly must never be told *yes* on the
    strength of having run nothing. This fires, for instance, when a
    model is read back with :func:`pm4py_ucm.read_ucm` from a build that
    does not import scenario definitions.
    """


def traverse_all(
    ucm: UCM, max_hit_count: int = DEFAULT_MAX_HIT_COUNT,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> List[ScenarioTraversalResult]:
    """Run every scenario of every scenario group in ``ucm``.

    Raises :class:`NoScenariosError` when there is nothing to run.
    """
    out: List[ScenarioTraversalResult] = []
    for group in ucm.scenario_groups:
        for scenario in group.scenarios:
            out.append(traverse_scenario(
                ucm, scenario, max_hit_count=max_hit_count,
                max_steps=max_steps))
    if not out:
        raise NoScenariosError(
            "model declares no scenarios; traversal verified nothing"
        )
    return out


def required_max_hit_count(
    ucm: UCM, max_hit_count: int = DEFAULT_MAX_HIT_COUNT,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> Tuple[int, str, str]:
    """``(minimum setting, scenario, element)`` this model needs.

    jUCMNav declares an infinite loop as soon as *one* element is entered
    ``maximum hit count`` times — a **preference**, not a property of the
    model. Set it below what a model legitimately needs and jUCMNav
    reports a deadlock that is not there: the offending visit is
    abandoned, its AND-joins then starve, and the scenario never reaches
    its end point. The symptom is a Problems view full of blocked joins,
    which reads exactly like a malformed model.

    A model with parallel branches inside nested loops enters some
    elements once per iteration of every enclosing loop, so it can
    legitimately need a much higher ceiling than a flat one. This
    returns the smallest setting at which the model traverses cleanly,
    so it can be reported alongside the model instead of being
    discovered from errors that do not exist.
    """
    worst = (0, "", "")
    for result in traverse_all(ucm, max_hit_count=max_hit_count,
                               max_steps=max_steps):
        if result.peak_hit_count > worst[0]:
            worst = (result.peak_hit_count, result.scenario,
                     result.peak_hit_element)
    return (worst[0] + 1, worst[1], worst[2])


def check_traversal(
    ucm: UCM, max_hit_count: int = DEFAULT_MAX_HIT_COUNT,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> List[TraversalProblem]:
    """Every problem across every scenario — empty when the model runs.

    The intended use is as a gate before a model is published or
    measured: if this returns anything, jUCMNav will report it too,
    *provided its maximum-hit-count preference is at least*
    :func:`required_max_hit_count`. Raises :class:`NoScenariosError` if
    the model has no scenarios, so an empty list always means "ran, and
    found nothing".
    """
    return [p for r in traverse_all(ucm, max_hit_count=max_hit_count,
                                    max_steps=max_steps)
            for p in r.problems]
