"""
Deterministic pastel colours for UCM component clusters.

The same component name always maps to the same colour, across maps
in one render *and* across runs — both the PNG visualizer and the
jUCMNav ``.jucm`` exporter consume this module so the same colour is
used in both outputs.

Implementation notes
--------------------

Hash function is MD5 for stability — Python's built-in ``hash`` is
randomised per process by default. MD5 has no cryptographic intent
here; we only need a deterministic byte to index into the palette.

The palette is twelve Material-Design pastels (light 100 fill paired
with strong 800 border). It's small enough that two unrelated
components occasionally land on the same colour (12 buckets), but
large enough that typical UCMs with ≤ 6 actors get distinct shades.
"""
from __future__ import annotations

import hashlib
from typing import Tuple


#: Twelve ``(fill, border)`` pairs from the Material-Design 100/800
#: range. Fill is light enough for dark labels to remain legible;
#: border is strong enough to define the cluster outline.
PASTEL_PALETTE: Tuple[Tuple[str, str], ...] = (
    ("#FFCDD2", "#C62828"),  # red
    ("#F8BBD0", "#C2185B"),  # pink
    ("#E1BEE7", "#7B1FA2"),  # purple
    ("#C5CAE9", "#283593"),  # indigo
    ("#BBDEFB", "#1565C0"),  # blue
    ("#B2EBF2", "#00838F"),  # cyan
    ("#B2DFDB", "#00695C"),  # teal
    ("#C8E6C9", "#2E7D32"),  # green
    ("#F0F4C3", "#9E9D24"),  # lime
    ("#FFECB3", "#FF8F00"),  # amber
    ("#FFE0B2", "#EF6C00"),  # orange
    ("#D7CCC8", "#4E342E"),  # brown
)


def component_color(name: str) -> Tuple[str, str]:
    """Return ``(fill, border)`` for a component named ``name``.

    Both values are uppercase ``"#RRGGBB"`` strings (the format
    graphviz accepts directly). Empty or ``None`` names map to the
    first palette entry deterministically rather than raising.
    """
    # Non-cryptographic: MD5 only maps a name to a stable palette index.
    # usedforsecurity=False documents that and silences SAST warnings.
    digest = hashlib.md5(
        (name or "").encode("utf-8"), usedforsecurity=False).digest()
    return PASTEL_PALETTE[digest[0] % len(PASTEL_PALETTE)]


def hex_to_rgb_triplet(hex_color: str) -> str:
    """Convert ``"#RRGGBB"`` to a ``"r,g,b"`` decimal triplet —
    the format jUCMNav (SWT under the hood) reads from metadata
    values for ``_FillColor`` / ``_LineColor``."""
    s = hex_color.lstrip("#")
    if len(s) != 6:
        return hex_color
    try:
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
    except ValueError:
        return hex_color
    return f"{r},{g},{b}"
