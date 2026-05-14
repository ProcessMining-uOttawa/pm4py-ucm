"""
Vertically stacked multi-map UCM rendering.

When a UCM contains more than one map (e.g. produced by the hierarchical
decomposition in
:mod:`pm4py_ucm.objects.ucm.conversion.decomposition`), a single
graphviz drawing rapidly becomes unreadable: graphviz packs the
cluster subgraphs side-by-side rather than top-to-bottom, and there is
no clean way to insert title strips or separators between them.

This module renders each map to its own PNG via the standard
:mod:`pm4py_ucm.visualization.ucm.variants.classic` pipeline, then
composites the images vertically using Pillow with a title strip
above each map and a thin horizontal separator between adjacent maps.
The deterministic order is the order maps appear in ``UCM.maps`` —
which, for decomposition-produced UCMs, is pre-order DFS through the
stub tree (root first, then plug-ins in left-to-right stub order).
"""
from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from ...objects.ucm.obj import UCM
from .variants import classic as _classic


#: Spacing constants — picked to look acceptable across typical zoom
#: levels (50%–200%). They scale linearly with the rendered DPI graphviz
#: chose for each map; if the user requests an unusual ``dpi``, the
#: composite still produces a clean image.
_TITLE_PAD_TOP = 18
_TITLE_PAD_BOTTOM = 10
_TITLE_FONT_SIZE = 18  # slightly larger than node labels (default 11pt)
_SEPARATOR_THICKNESS = 2
_SEPARATOR_MARGIN = 12
_BG_COLOR = (255, 255, 255)
_TITLE_COLOR = (32, 32, 32)
_SEPARATOR_COLOR = (160, 160, 160)


def _has_pillow() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def _select_maps(
    ucm: UCM, only: Optional[str]
) -> List[Tuple[int, UCM.UCMmap]]:
    """Return the list of ``(index, map)`` pairs to render.

    ``only`` filters to a single map by name; ``None`` selects all.
    Unknown names raise :class:`ValueError` — silent fallback hides typos.
    """
    if only is None:
        return list(enumerate(ucm.maps))
    matching = [(i, m) for i, m in enumerate(ucm.maps) if m.name == only]
    if not matching:
        raise ValueError(
            f"No map named {only!r} in UCM "
            f"(available: {[m.name for m in ucm.maps]})"
        )
    return [matching[0]]


def _render_each(
    ucm: UCM,
    maps: List[Tuple[int, UCM.UCMmap]],
    parameters: Dict[str, Any],
) -> List[Tuple[str, str]]:
    """Render every selected map to a PNG; return ``[(name, png_path), …]``.

    The temp directory is the caller's responsibility — we drop every
    PNG into the same temp dir for easy cleanup."""
    out: List[Tuple[str, str]] = []
    tmpdir = tempfile.mkdtemp(prefix="pm4py_ucm_stacked_")
    for idx, ucm_map in maps:
        per_map_params = dict(parameters)
        per_map_params["map_index"] = idx
        per_map_params["format"] = "png"
        # Discard any "map_name" filter — we've already resolved selection.
        per_map_params.pop("map_name", None)
        gviz = _classic.apply(ucm, parameters=per_map_params)
        gviz.format = "png"
        base = os.path.join(tmpdir, f"map_{idx:03d}")
        rendered = gviz.render(filename=base, cleanup=True)
        out.append((ucm_map.name or f"Map{idx}", rendered))
    return out


def _load_font(size: int):
    """Best-effort font lookup: prefer a bold TrueType for the map-name
    title strips (so root and plug-in map names read clearly), fall
    back to a regular TrueType, then to Pillow's default bitmap font
    on systems without any usable TTF on disk."""
    from PIL import ImageFont

    # Bold candidates first — map names should pop visually. We then
    # fall back to regular weights so the title is at least *present*
    # on systems missing a bold variant. Pillow's truetype() raises
    # OSError when the file is missing, so iterate.
    candidates = [
        # Bold variants (preferred for map-name titles)
        "arialbd.ttf",
        "DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "C:\\Windows\\Fonts\\arialbd.ttf",
        # Regular fallbacks
        "arial.ttf",
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:\\Windows\\Fonts\\arial.ttf",
    ]
    for name in candidates:
        try:
            return ImageFont.truetype(name, size=size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _composite(
    panels: List[Tuple[str, str]],
    output_path: str,
) -> str:
    """Composite ``panels`` (list of ``(title, png_path)``) into one
    vertical PNG with title strips and horizontal separators.

    Returns the path the composite was written to.
    """
    from PIL import Image, ImageDraw

    # Load as RGBA so a transparent PNG (if the caller overrides the
    # default ``bgcolor``) composites cleanly against the white
    # background below — converting straight to RGB maps fully
    # transparent pixels to black, which produced black map
    # backgrounds when this code first shipped.
    images = [(title, Image.open(p).convert("RGBA")) for title, p in panels]

    font = _load_font(_TITLE_FONT_SIZE)

    # Width of the composite = widest panel; pad narrower panels.
    width = max(im.width for _, im in images)

    # Compute total height: per panel = title strip + image + separator
    # (except the last panel which doesn't need a trailing separator).
    title_strip = _TITLE_PAD_TOP + _TITLE_FONT_SIZE + _TITLE_PAD_BOTTOM
    sep_total = _SEPARATOR_MARGIN * 2 + _SEPARATOR_THICKNESS
    total_h = 0
    for i, (_, im) in enumerate(images):
        total_h += title_strip + im.height
        if i != len(images) - 1:
            total_h += sep_total

    composite = Image.new("RGB", (width, total_h), color=_BG_COLOR)
    draw = ImageDraw.Draw(composite)

    y = 0
    for i, (title, im) in enumerate(images):
        # Title strip
        text = title
        # Pillow ≥ 10.0: prefer ``textbbox``; older versions used
        # ``textsize`` which is now removed. Both return a width so we
        # can horizontally centre.
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            text_w = bbox[2] - bbox[0]
        except AttributeError:  # very old Pillow
            text_w, _ = draw.textsize(text, font=font)  # type: ignore
        draw.text(
            (max(0, (width - text_w) // 2), y + _TITLE_PAD_TOP),
            text, font=font, fill=_TITLE_COLOR,
        )
        y += title_strip

        # Image — centre horizontally if narrower than the composite width.
        # Use the panel's alpha channel as the paste mask so transparent
        # regions show the composite's white background instead of black.
        x_off = (width - im.width) // 2
        composite.paste(im, (x_off, y), mask=im)
        y += im.height

        # Separator between panels (but not after the last one).
        if i != len(images) - 1:
            y += _SEPARATOR_MARGIN
            draw.rectangle(
                [(_SEPARATOR_MARGIN, y),
                 (width - _SEPARATOR_MARGIN, y + _SEPARATOR_THICKNESS - 1)],
                fill=_SEPARATOR_COLOR,
            )
            y += _SEPARATOR_THICKNESS + _SEPARATOR_MARGIN

    composite.save(output_path)

    # Clean up the per-panel PNGs (they share the same temp dir).
    if panels:
        tmpdir = os.path.dirname(panels[0][1])
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except OSError:
            pass

    return output_path


def render(
    ucm: UCM,
    output_path: str,
    parameters: Optional[Dict[str, Any]] = None,
    only: Optional[str] = None,
) -> str:
    """Render the UCM's maps stacked vertically into a single PNG.

    Parameters
    ----------
    ucm
        UCM model with one or more maps.
    output_path
        Destination PNG path.
    parameters
        Forwarded to the per-map renderer (see
        :func:`pm4py_ucm.visualization.ucm.variants.classic.apply`).
        ``map_index`` and ``map_name`` are stripped; ``only`` controls
        the selection here instead.
    only
        Optional map name; when set, only that map is rendered (single
        panel — the result is a normal vertical render but with the
        title strip on top).

    Raises
    ------
    ImportError
        When Pillow is not installed. The caller is expected to fall
        back to the cluster-subgraph rendering in
        :func:`pm4py_ucm.visualization.ucm.variants.classic.apply`.
    """
    if not _has_pillow():
        raise ImportError(
            "Stacked rendering requires Pillow. Install with "
            "`pip install Pillow`, or call view_ucm/save_vis_ucm with "
            "a single-map UCM."
        )

    parameters = dict(parameters or {})
    maps = _select_maps(ucm, only)
    if not maps:
        # Empty UCM — write an empty white image so callers don't
        # have to special-case it.
        from PIL import Image
        Image.new("RGB", (200, 60), color=_BG_COLOR).save(output_path)
        return output_path

    panels = _render_each(ucm, maps, parameters)
    return _composite(panels, output_path)
