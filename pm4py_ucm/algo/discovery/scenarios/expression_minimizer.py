"""Small boolean simplifier for jUCMNav OR-fork conditions.

Decision-tree-generated expressions can be highly verbose because
every leaf contributes its own AND-clause to the per-branch OR:

    (Broker != X && Claim_Value > 1000 && Claim_Value <= 5000)
    || (Broker != X && Claim_Value > 1000 && Claim_Value > 5000)
    || (Broker != X && Claim_Value <= 1000)
    || ...

A few of these clauses always collapse:

* **Complement absorption** — ``(P && Y) || (P && !Y)`` -> ``P`` when
  ``Y`` and ``!Y`` are syntactic complements. ``B > 5000`` is the
  complement of ``B <= 5000``; ``A == X`` is the complement of
  ``A != X``.
* **Clause subsumption** — ``A || (A && B)`` -> ``A``.
* **Duplicate clause** — ``A || A`` -> ``A``.
* **Implied-comparison subsumption** — ``B > 100 && B > 50`` ->
  ``B > 100`` (the stricter bound wins); analogous for ``<=``.

We iterate these rules to a fixpoint. The grammar handled is
exactly what the synthesizer emits: a top-level
``OR`` of optionally-parenthesised ``AND`` clauses, with literals
of the form ``var == VAL`` / ``var != VAL`` / ``var > N`` /
``var <= N`` / ``var < N`` / ``var >= N``. Anything more
complicated is returned unchanged.

The minimizer never *over*-simplifies: every transformation is a
boolean tautology, so the resulting expression matches exactly
the same set of variable assignments as the original. In
particular, if the original expression simplifies to a literal
``true`` or ``false`` we return that as-is so callers can detect
trivial-branch forks and decide whether to restructure.
"""
from __future__ import annotations

import re
from typing import FrozenSet, List, Optional, Tuple


#: One literal: ``(var, op, value)``. ``value`` is a string so the
#: same triple can represent ``Claim_Value > 1000`` and
#: ``Channel == Email`` without juggling types — we never need to
#: compare across different value kinds for the simplification rules.
Literal = Tuple[str, str, str]


_OPS = ("<=", ">=", "==", "!=", "<", ">")  # longest-first for matching


def _complement(lit: Literal) -> Literal:
    """Return the syntactic complement of ``lit`` (for collapse rules).

    ``(B > N)`` <-> ``(B <= N)`` and ``(B < N)`` <-> ``(B >= N)``
    are mutually exclusive over integers. ``A == V`` <-> ``A != V``
    flips the comparator. Other operators have no syntactic
    complement at this layer and are returned with a sentinel op
    that never matches anything real."""
    var, op, val = lit
    flip = {
        ">": "<=", "<=": ">",
        "<": ">=", ">=": "<",
        "==": "!=", "!=": "==",
    }
    return (var, flip.get(op, "?"), val)


def _parse_literal(token: str) -> Optional[Literal]:
    """Parse ``"Broker != Spot_Health"`` -> ``("Broker", "!=", "Spot_Health")``.

    Returns ``None`` if the token doesn't look like a comparison —
    the caller treats that as "expression too complex to simplify"
    and bails the whole simplification."""
    t = token.strip()
    for op in _OPS:
        idx = t.find(op)
        if idx <= 0:
            continue
        # Cheap disambiguation: ``==`` vs ``=`` (we only use ``==``),
        # ``!=`` vs ``!``. We've already iterated longest-first so
        # ``<=`` matches before ``<``.
        left = t[:idx].strip()
        right = t[idx + len(op):].strip()
        if not left:
            continue
        return (left, op, right)
    return None


def _split_top_level(text: str, sep: str) -> List[str]:
    """Split on ``sep`` only at parenthesis depth 0. Used to break a
    DNF expression into clauses without breaking sub-expressions."""
    parts: List[str] = []
    depth = 0
    last = 0
    i = 0
    while i < len(text):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and text.startswith(sep, i):
            parts.append(text[last:i])
            last = i + len(sep)
            i = last
            continue
        i += 1
    parts.append(text[last:])
    return [p.strip() for p in parts]


def _strip_outer_parens(text: str) -> str:
    """Strip exactly one matched outer pair of parentheses if they
    enclose the whole expression."""
    t = text.strip()
    if not (t.startswith("(") and t.endswith(")")):
        return t
    # Verify the outer parens match each other (not e.g. ``(a) && (b)``).
    depth = 0
    for i, c in enumerate(t):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if depth == 0 and i < len(t) - 1:
            return t
    return t[1:-1].strip()


def _parse_clause(clause_text: str) -> Optional[FrozenSet[Literal]]:
    """Parse ``"A == X && B > 5"`` -> ``frozenset({("A","==","X"), ("B",">","5")})``."""
    text = _strip_outer_parens(clause_text)
    literals = _split_top_level(text, "&&")
    parsed: List[Literal] = []
    for lit_text in literals:
        lit = _parse_literal(lit_text)
        if lit is None:
            return None  # signal "give up"
        parsed.append(lit)
    return frozenset(parsed)


def _parse_dnf(expression: str) -> Optional[List[FrozenSet[Literal]]]:
    """Parse a top-level OR-of-AND expression into a list of clauses.

    Returns ``None`` if the structure doesn't fit (e.g. nested
    parentheses we don't support, or operators we don't recognise)
    — the caller treats it as "leave the original alone"."""
    if not expression or expression.strip() in ("true", "false"):
        return None
    text = expression.strip()
    clauses = _split_top_level(text, "||")
    out: List[FrozenSet[Literal]] = []
    for clause_text in clauses:
        clause = _parse_clause(clause_text)
        if clause is None:
            return None
        out.append(clause)
    return out


# ---------------------------------------------------------------------------
# Simplification rules
# ---------------------------------------------------------------------------

def _dedup_clauses(
    clauses: List[FrozenSet[Literal]],
) -> List[FrozenSet[Literal]]:
    """Drop exact-duplicate clauses (preserves first-seen order)."""
    seen: set = set()
    out: List[FrozenSet[Literal]] = []
    for c in clauses:
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out


def _drop_subsumed_clauses(
    clauses: List[FrozenSet[Literal]],
) -> List[FrozenSet[Literal]]:
    """If clause A is a (proper or improper) subset of clause B, drop B.

    Reason: ``A`` already covers everything ``A && extra`` covers, so
    the larger clause is redundant in the disjunction. This is the
    standard absorption rule ``A || (A && B)`` -> ``A``."""
    survivors: List[FrozenSet[Literal]] = []
    for c in clauses:
        if any(other.issubset(c) and other != c for other in clauses):
            continue
        survivors.append(c)
    return survivors


def _collapse_strict_bounds_within_clause(
    clause: FrozenSet[Literal],
) -> FrozenSet[Literal]:
    """Within one AND-clause, keep only the tightest integer-comparison
    bound per (var, direction).

    Example: ``B > 100 && B > 50 && B <= 5000`` -> ``B > 100 && B <= 5000``.
    ``B > 100`` implies ``B > 50``, so the looser bound is redundant.
    Symmetric for ``<=`` / ``<`` / ``>=``."""
    by_dir: dict = {}  # (var, direction) -> (lit, numeric_value)
    keep: List[Literal] = []
    for lit in clause:
        var, op, val = lit
        if op not in (">", "<=", "<", ">="):
            keep.append(lit)
            continue
        try:
            n = int(val)
        except (ValueError, TypeError):
            keep.append(lit)
            continue
        direction = ">" if op in (">", ">=") else "<"
        key = (var, direction)
        prev = by_dir.get(key)
        if prev is None:
            by_dir[key] = (lit, n, op)
        else:
            prev_lit, prev_n, prev_op = prev
            # For ">" / ">=" pick the largest threshold (strictest).
            # For "<" / "<=" pick the smallest.
            if direction == ">":
                if n > prev_n or (n == prev_n and op == ">"):
                    by_dir[key] = (lit, n, op)
            else:
                if n < prev_n or (n == prev_n and op == "<"):
                    by_dir[key] = (lit, n, op)
    keep.extend(lit for (lit, _, _) in by_dir.values())
    return frozenset(keep)


def _merge_complement_pairs(
    clauses: List[FrozenSet[Literal]],
) -> Optional[List[FrozenSet[Literal]]]:
    """Look for two clauses that differ in **exactly one literal** that
    is the syntactic complement, and merge them by dropping that
    literal. Returns the new clause list if at least one merge
    happened, or ``None`` otherwise (caller signals "fixpoint reached
    for this rule")."""
    merged = list(clauses)
    for i in range(len(merged)):
        for j in range(i + 1, len(merged)):
            a, b = merged[i], merged[j]
            diff_a = a - b
            diff_b = b - a
            if len(diff_a) != 1 or len(diff_b) != 1:
                continue
            lit_a = next(iter(diff_a))
            lit_b = next(iter(diff_b))
            if _complement(lit_a) != lit_b:
                continue
            common = a & b
            # Drop both originals, add the merged clause once.
            new = [c for k, c in enumerate(merged)
                   if k != i and k != j]
            new.append(common)
            return new
    return None


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------

def minimize(expression: str) -> str:
    """Apply the simplification rules to ``expression`` until fixpoint
    and return the minimised string.

    Returns the original expression unchanged when the parser can't
    handle it — never crashes the caller. When the simplified DNF
    becomes empty (no clauses survive), returns ``"false"``; when a
    single empty clause survives, returns ``"true"``."""
    if not expression:
        return expression
    text = expression.strip()
    if text in ("true", "false"):
        return text

    parsed = _parse_dnf(text)
    if parsed is None:
        return expression

    clauses = [_collapse_strict_bounds_within_clause(c) for c in parsed]

    # Iterate to a fixpoint.
    for _ in range(64):  # bounded for safety
        prev = list(clauses)
        clauses = _dedup_clauses(clauses)
        clauses = _drop_subsumed_clauses(clauses)
        merged = _merge_complement_pairs(clauses)
        if merged is not None:
            clauses = [
                _collapse_strict_bounds_within_clause(c) for c in merged
            ]
        if clauses == prev:
            break

    if not clauses:
        return "false"
    # An empty clause (frozenset()) means the conjunction is the empty
    # AND, which is conventionally ``true``. If any clause is empty
    # the whole disjunction is true.
    if any(len(c) == 0 for c in clauses):
        return "true"

    return _format(clauses)


def _format(clauses: List[FrozenSet[Literal]]) -> str:
    """Serialise the simplified DNF back to jUCMNav-compatible text.
    Single literals come out unparenthesised; multi-literal clauses
    get one set of parens; multi-clause disjunctions are
    ``||``-joined."""
    clause_strs: List[str] = []
    for c in clauses:
        lits = sorted(c)  # stable output
        parts = [_format_literal(l) for l in lits]
        if len(parts) == 1:
            clause_strs.append(parts[0])
        else:
            clause_strs.append("(" + " && ".join(parts) + ")")
    if len(clause_strs) == 1:
        # Strip outer parens for a single clause — easier to read.
        s = clause_strs[0]
        if s.startswith("(") and s.endswith(")"):
            s = s[1:-1]
        return s
    return " || ".join(clause_strs)


def _format_literal(lit: Literal) -> str:
    var, op, val = lit
    return f"{var} {op} {val}"
