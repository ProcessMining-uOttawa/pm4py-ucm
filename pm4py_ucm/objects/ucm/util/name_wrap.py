"""
Shared helper for wrapping long element names across two or three lines so
that diagrams stay compact.

The same wrapped representation must be used by:

* the **jUCMNav exporter** — to emit names like
  ``"Send for&#xD;&#xA;Credit Collection"`` so jUCMNav itself draws the
  responsibility as a narrow, taller box;
* the **layouter** — to size component bounding boxes correctly (a
  three-line label is taller and narrower than a one-line one);
* the **visualizer** — to keep the graphviz PNG consistent with the
  ``.jucm`` rendering.

Wrapping a name returns the wrapped string (with embedded ``\\r\\n``
sequences) plus a ``(line_count, longest_line_length)`` tuple that the
layouter uses to estimate the on-screen footprint of the label.

The wrapping is purely heuristic and conservative: it never breaks a
word, never produces a single-character last line, and never wraps
names already short enough to fit on one line.
"""
from __future__ import annotations

from typing import List, Tuple

# CRLF — exactly what jUCMNav writes when it splits a name itself.
_LINE_BREAK = "\r\n"

#: Default width threshold; names longer than this many characters are
#: candidates for wrapping. Chosen to keep responsibility rectangles in
#: the ``.jucm`` close to square at the default font size (≈9–10 chars
#: per line fit comfortably in jUCMNav's default rectangle).
DEFAULT_MAX_WIDTH = 12

#: Hard ceiling on the number of lines we will produce. Beyond this the
#: wrap heuristic gives up and returns the name unchanged; long-tail
#: names like Java fully-qualified method paths are better left to the
#: renderer's own clipping behaviour.
DEFAULT_MAX_LINES = 3


def wrap_name(
    name: str,
    max_width: int = DEFAULT_MAX_WIDTH,
    max_lines: int = DEFAULT_MAX_LINES,
) -> str:
    """Wrap ``name`` across at most ``max_lines`` lines, breaking only on
    whitespace.

    Names already at or below ``max_width`` are returned unchanged. If the
    longest single word exceeds ``max_width`` we still let it through —
    splitting inside a word reads worse than overflowing a little.
    """
    if not name or "\r" in name or "\n" in name:
        # Either nothing to do, or the caller pre-wrapped — respect them.
        return name
    if len(name) <= max_width:
        return name
    words = name.split()
    if len(words) == 1:
        return name  # single word — can't wrap on whitespace

    # Greedy first-fit packing. Then check whether the result fits in
    # ``max_lines``; if not, widen the budget until it does.
    lines = _greedy_pack(words, max_width)

    if len(lines) > max_lines:
        # Too many lines — widen the budget until we land at ``max_lines``
        # or stop making progress. We avoid recursion (an unwrappable
        # name can otherwise loop forever) by capping the search by the
        # name length itself.
        budget = max_width
        cap = len(name) + 1
        while len(lines) > max_lines and budget < cap:
            budget = max(budget + 1, (sum(len(l) for l in lines)
                                       + max_lines - 1) // max_lines)
            lines = _greedy_pack(words, budget)
        if len(lines) > max_lines:
            # Genuinely unwrappable (one long token, or weird whitespace) —
            # leave the name alone so the renderer can clip it as it sees fit.
            return name

    return _LINE_BREAK.join(lines)


def _greedy_pack(words: List[str], max_width: int) -> List[str]:
    """Greedy first-fit line packing of ``words`` so that no line — when
    rendered as space-joined words — exceeds ``max_width`` characters
    (single overflows allowed when a word itself is wider)."""
    lines: List[str] = []
    current: List[str] = []
    current_len = 0
    for w in words:
        added = len(w) + (1 if current else 0)
        if current and current_len + added > max_width:
            lines.append(" ".join(current))
            current = [w]
            current_len = len(w)
        else:
            current.append(w)
            current_len += added
    if current:
        lines.append(" ".join(current))
    return lines


def label_dimensions(
    name: str,
    max_width: int = DEFAULT_MAX_WIDTH,
    max_lines: int = DEFAULT_MAX_LINES,
) -> Tuple[int, int]:
    """Return ``(n_lines, longest_line_chars)`` for the wrapped ``name``.

    Used by the layouter to estimate label dimensions in characters,
    which it then scales by an approximate per-character width to get a
    pixel footprint."""
    wrapped = wrap_name(name, max_width=max_width, max_lines=max_lines)
    if not wrapped:
        return 1, 0
    lines = wrapped.split(_LINE_BREAK)
    longest = max(len(l) for l in lines) if lines else 0
    return len(lines), longest
