"""The ƒ custom-metric grammar — a small, closed expression language.

A custom metric is a per-case expression, optionally narrowed by a
``where`` clause, which :mod:`~pm4py_ucm.algo.dashboards.engine` then
aggregates like any catalog metric. It exists so a user can measure
something the catalog does not name — "share of high-value claims that
were appealed", "days from registration to payment over €500" — without
anyone shipping code.

Closed, and no ``eval``
-----------------------
The grammar is deliberately tiny and is parsed to an explicit AST, never
handed to a language ``eval``. Nothing a user types can reach the host:
the only operations are the ones enumerated here, over the only data the
fact table exposes. This is the same reason the rest of the dashboard
computes client-side over a snapshot rather than shipping a query to a
server.

One value type
--------------
Every expression evaluates, per case, to a **number or null** — there are
no strings at runtime. Strings appear only as the *names* inside a call
(``contains("Payment")``), fixed when the formula is written. That single
type is what keeps this grammar agreeing between the Python evaluator
here and the JS one in ``dash-engine.js``: there is no string/number
coercion to get subtly wrong in two languages. A comparison yields
``1``/``0``; ``null`` (a missing time, an absent attribute) propagates
through arithmetic and comparison and is dropped by aggregation, exactly
as everywhere else in the engine.

Grammar
-------
::

    formula     := orExpr [ "where" orExpr ]
    orExpr      := andExpr ( "or" andExpr )*
    andExpr     := notExpr ( "and" notExpr )*
    notExpr     := "not" notExpr | comparison
    comparison  := additive ( ("=="|"!="|">"|">="|"<"|"<=") additive )?
    additive    := term ( ("+"|"-") term )*
    term        := unary ( ("*"|"/") unary )*
    unary       := "-" unary | primary
    primary     := number | call | "(" orExpr ")"
    call        := IDENT "(" [ arg ( "," arg )* ] ")"

Functions (all per case):

======================  ===========================================
``duration()``          case length, days
``contains(act)``       1 if the case has ``act``, else 0
``count(act)``          occurrences of ``act`` in the case
``time_between(a, b)``  days, first ``a`` to the first ``b`` after it;
                        null when that never happens
``timestamp(act)``      epoch seconds of the first ``act``, else null
``attr(name)``          a **numeric** case attribute; null if absent or
                        non-numeric. Categorical attributes are filtered
                        with the widget's Filter row, not here — keeping
                        this grammar single-typed.
======================  ===========================================

Result type
-----------
Inferred from the AST and used exactly as a catalog metric's is — to pick
the unit and the aggregations offered. A formula whose top operation is a
comparison or a logical is a 0/1 indicator → ``percent`` (aggregate with
``share``). One that mentions any time function → ``time``. Otherwise →
``count``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

#: Function name → exact argument count. Arity is checked at parse time;
#: whether a named activity/attribute exists is a soft check against the
#: log (see :func:`unknown_names`), not a parse error.
FUNCTIONS: Dict[str, int] = {
    "duration": 0,
    "contains": 1,
    "count": 1,
    "time_between": 2,
    "timestamp": 1,
    "attr": 1,
}

#: Functions that make the whole formula time-valued.
_TIME_FUNCS = {"duration", "time_between", "timestamp"}

_KEYWORDS = {"where", "and", "or", "not"}
_COMPARISONS = {"==", "!=", ">", ">=", "<", "<="}


class FormulaError(ValueError):
    """A formula that does not parse. The message is written for the
    editor to show verbatim, so it names the problem, not the internals."""


# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------

@dataclass
class _Tok:
    kind: str            # num str ident op lparen rparen comma kw
    value: Any
    pos: int


def _tokenize(text: str) -> List[_Tok]:
    toks: List[_Tok] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c in "()":
            toks.append(_Tok("lparen" if c == "(" else "rparen", c, i))
            i += 1
            continue
        if c == ",":
            toks.append(_Tok("comma", c, i))
            i += 1
            continue
        if c in "\"'":
            j = i + 1
            while j < n and text[j] != c:
                j += 1
            if j >= n:
                raise FormulaError(f"Unclosed string starting at position {i}.")
            toks.append(_Tok("str", text[i + 1:j], i))
            i = j + 1
            continue
        if c.isdigit() or (c == "." and i + 1 < n and text[i + 1].isdigit()):
            j = i
            while j < n and (text[j].isdigit() or text[j] == "."):
                j += 1
            try:
                toks.append(_Tok("num", float(text[i:j]), i))
            except ValueError:
                raise FormulaError(f"Bad number {text[i:j]!r}.")
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            toks.append(_Tok("kw" if word in _KEYWORDS else "ident", word, i))
            i = j
            continue
        # operators, longest first so >= is not read as > then =
        two = text[i:i + 2]
        if two in ("==", "!=", ">=", "<="):
            toks.append(_Tok("op", two, i))
            i += 2
            continue
        if c in "+-*/><":
            toks.append(_Tok("op", c, i))
            i += 1
            continue
        if c == "=":
            raise FormulaError(
                f"Use == for comparison, not = (position {i}).")
        raise FormulaError(f"Unexpected character {c!r} at position {i}.")
    toks.append(_Tok("end", None, len(text)))
    return toks


# ---------------------------------------------------------------------------
# Parser (recursive descent)
# ---------------------------------------------------------------------------

class _Parser:
    def __init__(self, toks: List[_Tok]):
        self.toks = toks
        self.i = 0

    def _peek(self) -> _Tok:
        return self.toks[self.i]

    def _next(self) -> _Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def _expect(self, kind: str, what: str) -> _Tok:
        t = self._peek()
        if t.kind != kind:
            raise FormulaError(
                f"Expected {what} but found "
                f"{t.value if t.value is not None else 'end of formula'}.")
        return self._next()

    def parse(self) -> Dict[str, Any]:
        node = self._or()
        if self._peek().kind == "kw" and self._peek().value == "where":
            self._next()
            cond = self._or()
            node = {"t": "where", "e": node, "cond": cond}
        if self._peek().kind != "end":
            t = self._peek()
            raise FormulaError(f"Unexpected {t.value!r} after the formula.")
        return node

    def _or(self):
        node = self._and()
        while self._peek().kind == "kw" and self._peek().value == "or":
            self._next()
            node = {"t": "logic", "op": "or", "l": node, "r": self._and()}
        return node

    def _and(self):
        node = self._not()
        while self._peek().kind == "kw" and self._peek().value == "and":
            self._next()
            node = {"t": "logic", "op": "and", "l": node, "r": self._not()}
        return node

    def _not(self):
        if self._peek().kind == "kw" and self._peek().value == "not":
            self._next()
            return {"t": "not", "e": self._not()}
        return self._comparison()

    def _comparison(self):
        node = self._additive()
        t = self._peek()
        if t.kind == "op" and t.value in _COMPARISONS:
            self._next()
            return {"t": "cmp", "op": t.value, "l": node,
                    "r": self._additive()}
        return node

    def _additive(self):
        node = self._term()
        while self._peek().kind == "op" and self._peek().value in ("+", "-"):
            op = self._next().value
            node = {"t": "bin", "op": op, "l": node, "r": self._term()}
        return node

    def _term(self):
        node = self._unary()
        while self._peek().kind == "op" and self._peek().value in ("*", "/"):
            op = self._next().value
            node = {"t": "bin", "op": op, "l": node, "r": self._unary()}
        return node

    def _unary(self):
        if self._peek().kind == "op" and self._peek().value == "-":
            self._next()
            return {"t": "neg", "e": self._unary()}
        return self._primary()

    def _primary(self):
        t = self._peek()
        if t.kind == "num":
            self._next()
            return {"t": "num", "v": t.value}
        if t.kind == "lparen":
            self._next()
            node = self._or()
            self._expect("rparen", "a closing )")
            return node
        if t.kind == "ident":
            return self._call()
        if t.kind == "str":
            raise FormulaError(
                f"A bare string {t.value!r} is not a value — strings are "
                "only names inside a function like contains(\"…\").")
        raise FormulaError(
            f"Expected a number or a function, found "
            f"{t.value if t.value is not None else 'end of formula'}.")

    def _call(self):
        name = self._next().value
        if name not in FUNCTIONS:
            raise FormulaError(
                f"Unknown function {name!r}. Available: "
                f"{', '.join(sorted(FUNCTIONS))}.")
        self._expect("lparen", f"( after {name}")
        args: List[Dict[str, Any]] = []
        if self._peek().kind != "rparen":
            args.append(self._arg())
            while self._peek().kind == "comma":
                self._next()
                args.append(self._arg())
        self._expect("rparen", f"a closing ) for {name}(")
        want = FUNCTIONS[name]
        if len(args) != want:
            raise FormulaError(
                f"{name}() takes {want} argument"
                f"{'' if want == 1 else 's'}, got {len(args)}.")
        return {"t": "call", "fn": name, "args": args}

    def _arg(self):
        t = self._peek()
        if t.kind == "str":
            self._next()
            return {"t": "str", "v": t.value}
        raise FormulaError(
            "A function argument must be a quoted name like \"Payment\".")


def parse(text: str) -> Dict[str, Any]:
    """Parse a formula to its AST, or raise :class:`FormulaError`."""
    if not text or not text.strip():
        raise FormulaError("The formula is empty.")
    return _Parser(_tokenize(text)).parse()


# ---------------------------------------------------------------------------
# Static analysis
# ---------------------------------------------------------------------------

def result_type(ast: Dict[str, Any]) -> str:
    """``"time"`` / ``"percent"`` / ``"count"`` — see the module docstring."""
    top = ast["e"] if ast["t"] == "where" else ast
    # A 0/1 indicator aggregates into a percentage: a comparison, a
    # logical, or a bare contains() (which is itself 0/1).
    if top["t"] in ("cmp", "logic", "not"):
        return "percent"
    if top["t"] == "call" and top["fn"] == "contains":
        return "percent"
    if _references_time(ast):
        return "time"
    return "count"


def _references_time(node: Dict[str, Any]) -> bool:
    if node["t"] == "call":
        return node["fn"] in _TIME_FUNCS
    return any(_references_time(v) for k, v in node.items()
              if isinstance(v, dict)) or any(
        _references_time(a) for a in node.get("args", []))


def _string_args(node: Dict[str, Any], fn: str) -> List[str]:
    out: List[str] = []
    if node.get("t") == "call" and node["fn"] == fn:
        out += [a["v"] for a in node["args"] if a["t"] == "str"]
    for k, v in node.items():
        if isinstance(v, dict):
            out += _string_args(v, fn)
    for a in node.get("args", []):
        out += _string_args(a, fn)
    return out


def referenced_activities(ast: Dict[str, Any]) -> List[str]:
    names: set = set()
    for fn in ("contains", "count", "timestamp"):
        names.update(_string_args(ast, fn))
    for a, b in _pairs(ast):
        names.update((a, b))
    return sorted(names)


def _pairs(node: Dict[str, Any]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    if node.get("t") == "call" and node["fn"] == "time_between":
        a, b = node["args"]
        if a["t"] == "str" and b["t"] == "str":
            out.append((a["v"], b["v"]))
    for k, v in node.items():
        if isinstance(v, dict):
            out += _pairs(v)
    for a in node.get("args", []):
        out += _pairs(a)
    return out


def referenced_attributes(ast: Dict[str, Any]) -> List[str]:
    return sorted(set(_string_args(ast, "attr")))


def unknown_names(ast: Dict[str, Any], activities, attributes) -> List[str]:
    """Names referenced by the formula that the log does not have.

    A soft check for the editor — a likely typo, surfaced as a warning
    rather than a parse error, because the evaluator handles an unknown
    activity gracefully (its count is 0) and a formula may legitimately
    be written before its target exists.
    """
    acts = set(activities)
    attrs = set(attributes)
    missing = [a for a in referenced_activities(ast) if a not in acts]
    missing += [f"attr:{a}" for a in referenced_attributes(ast)
                if a not in attrs]
    return missing


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate(ast: Dict[str, Any], base: Dict[str, Any], n_cases: int):
    """``[n_cases]`` float array of the formula's per-case value.

    ``base`` maps each function name to a callable returning a per-case
    array — supplied by the engine, so this module needs no knowledge of
    the fact table and cannot drift from the catalog metrics that share
    those primitives. ``NaN`` is null; it propagates and is dropped by
    aggregation.
    """
    import numpy as np

    def ev(node):
        t = node["t"]
        if t == "num":
            return np.full(n_cases, node["v"], dtype=np.float64)
        if t == "call":
            fn = base[node["fn"]]
            args = [a["v"] for a in node["args"]]
            return np.asarray(fn(*args), dtype=np.float64)
        if t == "neg":
            return -ev(node["e"])
        if t == "bin":
            l, r = ev(node["l"]), ev(node["r"])
            if node["op"] == "+":
                return l + r
            if node["op"] == "-":
                return l - r
            if node["op"] == "*":
                return l * r
            # divide, with /0 → null rather than inf poisoning the mean
            with np.errstate(divide="ignore", invalid="ignore"):
                out = l / r
            out[~np.isfinite(out)] = np.nan
            # but keep a genuine nan-from-input as nan (already is)
            return out
        if t == "cmp":
            l, r = ev(node["l"]), ev(node["r"])
            op = node["op"]
            if op == "==":
                res = (l == r)
            elif op == "!=":
                res = (l != r)
            elif op == ">":
                res = (l > r)
            elif op == ">=":
                res = (l >= r)
            elif op == "<":
                res = (l < r)
            else:
                res = (l <= r)
            out = res.astype(np.float64)
            out[np.isnan(l) | np.isnan(r)] = np.nan
            return out
        if t == "logic":
            l, r = ev(node["l"]), ev(node["r"])
            lb, rb = (l != 0), (r != 0)
            if node["op"] == "and":
                out = (lb & rb).astype(np.float64)
            else:
                out = (lb | rb).astype(np.float64)
            out[np.isnan(l) | np.isnan(r)] = np.nan
            return out
        if t == "not":
            e = ev(node["e"])
            out = (e == 0).astype(np.float64)
            out[np.isnan(e)] = np.nan
            return out
        if t == "where":
            val = ev(node["e"])
            cond = ev(node["cond"])
            keep = np.isfinite(cond) & (cond != 0)
            return np.where(keep, val, np.nan)
        raise FormulaError(f"Cannot evaluate node {t!r}.")

    return ev(ast)


# ---------------------------------------------------------------------------
# One-call compile, for the editor and the widget
# ---------------------------------------------------------------------------

def compile_formula(text: str, *, activities=(), attributes=()) -> Dict[str, Any]:
    """Parse and analyse a formula, catching the error for display.

    Returns ``{"ok", "ast", "resultType", "error", "unknown"}``. ``ok`` is
    False with ``error`` set on a parse failure; ``unknown`` lists names
    not in the log (a warning, not a failure).
    """
    try:
        ast = parse(text)
    except FormulaError as exc:
        return {"ok": False, "ast": None, "resultType": None,
                "error": str(exc), "unknown": []}
    return {
        "ok": True,
        "ast": ast,
        "resultType": result_type(ast),
        "error": None,
        "unknown": unknown_names(ast, activities, attributes),
    }
