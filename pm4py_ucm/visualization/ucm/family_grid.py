"""Grid rendering for model families.

Renders a :class:`~pm4py_ucm.algo.discovery.families.family.ModelFamily`
as a single PNG:

* one partition attribute → a vertical **stack** (one row per value);
* two attributes → a **matrix** (rows = first attribute's values,
  columns = second attribute's values) with column header strips and a
  left row-label gutter.

Every cell panel carries the cell's caption (``n=123 (14.2%)``) —
essential context when comparing models mined from very differently
sized sub-logs. Observed-but-skipped combinations (below ``min_cases``)
render as grayed placeholders with their case count; combinations never
observed render as ``no cases``. Cells whose model has several maps
(decomposed cells) are themselves stacked vertically inside their panel
via :mod:`pm4py_ucm.visualization.ucm.stacked`.

**Resolution.** Text readability in the export is a function of the
raster DPI, not of the composite's overall pixel count — so the grid
never crushes individual panels to fit a fixed width (that is what
made text unreadable in early versions). Instead it picks the
rendering DPI adaptively, in two stages:

1. *Heuristic choice before rendering* — aim for ``target_dpi``
   (default 192, twice graphviz's 96: crisp on screen and in print)
   and back off toward 96 when a probe render of the largest cell
   projects the composite past ``max_total_pixels``.
2. *Exact enforcement after rendering* — grid slots are sized by
   per-column max width × per-row max height, which can exceed any
   single panel's area when panels vary in shape, so the projection
   can undershoot. If the measured layout still exceeds the budget,
   every panel is downscaled uniformly (supersampled from the higher
   rendering DPI, so text stays sharp) until the composite fits —
   but never below the 96-dpi-equivalent floor: a very large family
   is allowed to exceed the budget rather than become unreadable.

All grid chrome (headers, row labels, captions, padding) scales with
the effective DPI so proportions are resolution-independent.
"""

from __future__ import annotations

import math
import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from . import stacked as _stacked
from .variants import classic as _classic


_BG = (255, 255, 255)
_HEADER_COLOR = (32, 32, 32)
_CAPTION_COLOR = (96, 96, 96)
_PLACEHOLDER_BG = (243, 243, 243)
_PLACEHOLDER_FG = (150, 150, 150)
_GRID_LINE = (200, 200, 200)
#: Separator between FAMILY MEMBERS. Much heavier and darker than the
#: thin light rule :mod:`.stacked` draws between a decomposed model's
#: own maps — the reader must see at a glance where one member's
#: root+plug-ins end and the next member begins.
_MEMBER_LINE = (70, 70, 70)

# Base (96-dpi) chrome metrics — multiplied by the DPI scale factor.
_PAD = 14                 # inner padding of each grid cell
_HEADER_FONT_SIZE = 20
#: Row (member) labels are drawn rotated 90° in the left gutter at
#: this size — larger than the old inline labels, and the rotation
#: gives them the whole panel height to run along.
_LABEL_FONT_SIZE = 30
_CAPTION_FONT_SIZE = 14
_TITLE_FONT_SIZE = 22
_MEMBER_LINE_W = 5        # member-separator thickness
_MIN_PANEL_W = 260        # placeholder / empty-cell panel width
_MIN_PANEL_H = 120

#: Graphviz's default raster resolution — layout is DPI-independent,
#: so panel pixel dimensions scale linearly with (dpi / 96).
_BASE_DPI = 96

#: Default DPI the adaptive chooser aims for. Twice the graphviz
#: default: every glyph gets 2× the pixels, which is what makes the
#: export readable when zooming into a large grid.
DEFAULT_TARGET_DPI = 192

#: Default ceiling on the composite's total pixel count. Chosen to
#: stay well under Pillow's default decompression-bomb threshold
#: (~178M px) so the exported PNG opens in ordinary viewers, while
#: keeping peak compositing memory in the hundreds of MB.
DEFAULT_MAX_TOTAL_PIXELS = 150_000_000


def _render_cell_png(ucm, tmpdir: str, index: int,
                     parameters: Dict[str, Any]) -> str:
    """Render one cell's UCM to a PNG (stacked when multi-map)."""
    path = os.path.join(tmpdir, f"cell_{index:03d}.png")
    if len(ucm.maps) > 1:
        return _stacked.render(ucm, path, parameters=dict(parameters))
    params = dict(parameters)
    params["format"] = "png"
    gviz = _classic.apply(ucm, parameters=params)
    gviz.format = "png"
    rendered = gviz.render(
        filename=os.path.join(tmpdir, f"cell_{index:03d}"), cleanup=True,
    )
    return rendered


def _text_size(draw, text: str, font) -> Tuple[int, int]:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    except AttributeError:  # very old Pillow
        return draw.textsize(text, font=font)  # type: ignore


def _vertical_label(text: str, font, probe, max_len_px: int):
    """The row label as a transparent image rotated 90° (reads
    bottom-to-top), elided with an ellipsis when longer than the row
    is tall. Rotation lets the member name use the panel's full
    height, so it can be drawn much larger than an inline label."""
    from PIL import Image, ImageDraw

    label = text
    w, h = _text_size(probe, label, font)
    if w > max_len_px:
        while len(label) > 1:
            label = label[:-1]
            w, h = _text_size(probe, label + "…", font)
            if w <= max_len_px:
                label += "…"
                break
    margin = 4
    im = Image.new("RGBA", (w + 2 * margin, h * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.text((margin, h // 2), label, font=font, fill=_HEADER_COLOR)
    box = im.getbbox()
    if box:
        im = im.crop(box)
    return im.rotate(90, expand=True)


def _choose_dpi(
    family,
    parameters: Dict[str, Any],
    tmpdir: str,
    target_dpi: int,
    max_total_pixels: int,
) -> int:
    """Pick the highest DPI ≤ ``target_dpi`` whose projected composite
    stays within ``max_total_pixels``; never below the 96-dpi floor.

    The projection renders the *largest* mined cell (by node count)
    once at 96 dpi and assumes every mined cell is that big — a safe
    overestimate, so the chosen DPI errs toward the budget, not past
    it. Panel area scales with (dpi/96)²."""
    from PIL import Image

    cells = getattr(family, "cells", [])
    if not cells:
        return target_dpi

    probe_cell = max(
        cells, key=lambda c: sum(len(m.nodes) for m in c.ucm.maps),
    )
    probe_params = dict(parameters)
    probe_params["dpi"] = _BASE_DPI
    probe_png = _render_cell_png(
        probe_cell.ucm, tmpdir, 999, probe_params,
    )
    with Image.open(probe_png) as im:
        panel_area = im.width * im.height
    try:
        os.remove(probe_png)
    except OSError:
        pass

    projected_at_base = max(1, panel_area) * len(cells)
    allowed_scale = math.sqrt(max_total_pixels / projected_at_base)
    dpi = int(_BASE_DPI * allowed_scale)
    return max(_BASE_DPI, min(int(target_dpi), dpi))


def render(
    family,
    output_path: str,
    parameters: Optional[Dict[str, Any]] = None,
) -> str:
    """Render ``family`` to a single grid PNG at ``output_path``.

    Parameters
    ----------
    family
        A :class:`ModelFamily`.
    output_path
        Destination PNG path.
    parameters
        Forwarded to the per-cell renderer (``style``, …).
        Additionally recognised here:

        ``dpi``
            Explicit raster resolution for every panel. When set, both
            adaptive stages (heuristic choice and post-render budget
            enforcement) are bypassed — the value is honoured exactly.
        ``target_dpi``
            Resolution the adaptive chooser aims for (default
            :data:`DEFAULT_TARGET_DPI` = 192). The actual DPI backs
            off toward 96 only when the projected composite would
            exceed the pixel budget.
        ``max_total_pixels``
            Pixel budget for the composite (default
            :data:`DEFAULT_MAX_TOTAL_PIXELS`). The 96-dpi floor wins
            over the budget for very large families — the export may
            get big, but never unreadable.
        ``max_panel_width``
            Optional hard cap (in pixels) above which a single panel
            is downscaled. **Off by default** — downscaling destroys
            text legibility; enable only when one outlier cell must
            not dominate the grid.

    Returns the chosen path. The effective DPI is recorded as a
    ``pm4py_ucm_dpi`` entry in the PNG's text metadata.
    """
    if not _stacked._has_pillow():
        raise ImportError(
            "Family grid rendering requires Pillow. Install with "
            "`pip install Pillow`."
        )
    from PIL import Image, ImageDraw
    from PIL.PngImagePlugin import PngInfo

    parameters = dict(parameters or {})
    explicit_dpi = parameters.pop("dpi", None)
    target_dpi = int(parameters.pop("target_dpi", DEFAULT_TARGET_DPI))
    max_total_pixels = int(
        parameters.pop("max_total_pixels", DEFAULT_MAX_TOTAL_PIXELS)
    )
    max_panel_w = parameters.pop("max_panel_width", None)

    rows = family.row_values
    cols = family.col_values  # empty list for 1-attribute families
    two_d = bool(cols)
    n_rows = len(rows)
    n_cols = len(cols) if two_d else 1

    tmpdir = tempfile.mkdtemp(prefix="pm4py_ucm_family_")
    try:
        # --------------------------------------------------------------
        # Resolution: explicit override, or adaptive within the budget.
        # --------------------------------------------------------------
        if explicit_dpi is not None:
            dpi = int(explicit_dpi)
        else:
            dpi = _choose_dpi(
                family, parameters, tmpdir, target_dpi, max_total_pixels,
            )
        scale = dpi / _BASE_DPI
        cell_params = dict(parameters)
        cell_params["dpi"] = dpi

        # --------------------------------------------------------------
        # Render each mined cell to its own PNG.
        # --------------------------------------------------------------
        panels: Dict[Tuple[int, int], Image.Image] = {}
        captions: Dict[Tuple[int, int], str] = {}
        placeholders: Dict[Tuple[int, int], str] = {}

        grid = family.grid()
        skipped = {
            tuple(v.label for v in values): n
            for values, n in family.skipped_cells
        }
        idx = 0
        for r, rv in enumerate(rows):
            for c in range(n_cols):
                labels = (rv.label, cols[c].label) if two_d else (rv.label,)
                cell = grid.get(labels)
                if cell is not None:
                    png = _render_cell_png(
                        cell.ucm, tmpdir, idx, cell_params,
                    )
                    idx += 1
                    im = Image.open(png).convert("RGBA")
                    if max_panel_w and im.width > int(max_panel_w):
                        f = int(max_panel_w) / im.width
                        im = im.resize(
                            (int(max_panel_w), max(1, int(im.height * f))),
                            Image.LANCZOS,
                        )
                    panels[(r, c)] = im
                    captions[(r, c)] = cell.caption
                elif labels in skipped:
                    placeholders[(r, c)] = (
                        f"n={skipped[labels]} (below min_cases)"
                    )
                else:
                    placeholders[(r, c)] = "no cases"

        # --------------------------------------------------------------
        # Measure — and enforce the pixel budget exactly. The probe
        # projection can undershoot when panels vary in shape (slots
        # are per-column max width × per-row max height), so if the
        # measured layout exceeds the budget, downscale every panel
        # uniformly. Panels were rendered at ``dpi``, so shrinking by
        # ``s`` yields an effective DPI of ``dpi * s`` — supersampled,
        # hence still sharp — floored at 96-dpi equivalence.
        # --------------------------------------------------------------
        if two_d:
            title = (f"{family.attributes[0].display_name} × "
                     f"{family.attributes[1].display_name}")
        else:
            title = f"by {family.attributes[0].display_name}"

        probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        effective_dpi = float(dpi)

        for _attempt in (0, 1):
            scale = effective_dpi / _BASE_DPI
            pad = round(_PAD * scale)
            header_fs = max(1, round(_HEADER_FONT_SIZE * scale))
            label_fs = max(1, round(_LABEL_FONT_SIZE * scale))
            caption_fs = max(1, round(_CAPTION_FONT_SIZE * scale))
            title_fs = max(1, round(_TITLE_FONT_SIZE * scale))
            min_panel_w = round(_MIN_PANEL_W * scale)
            min_panel_h = round(_MIN_PANEL_H * scale)

            header_font = _stacked._load_font(header_fs)
            label_font = _stacked._load_font(label_fs)
            caption_font = _stacked._load_font(caption_fs)
            title_font = _stacked._load_font(title_fs)

            caption_h = caption_fs + round(10 * scale)

            col_w = [0] * n_cols
            row_h = [0] * n_rows
            for r in range(n_rows):
                for c in range(n_cols):
                    im = panels.get((r, c))
                    w = im.width if im is not None else min_panel_w
                    h = im.height if im is not None else min_panel_h
                    col_w[c] = max(col_w[c], w + 2 * pad)
                    row_h[r] = max(row_h[r], h + caption_h + 2 * pad)

            # Row-label gutter (always), column headers (matrix only).
            # Labels are drawn rotated 90°, so the gutter width is
            # governed by the text HEIGHT (one line of the label
            # font), not by the longest label.
            gutter_w = 0
            for rv in rows:
                _, h = _text_size(probe, rv.label, label_font)
                gutter_w = max(gutter_w, h)
            gutter_w += 2 * pad

            header_h = (header_fs + 2 * round(10 * scale)) if two_d else 0
            title_h = title_fs + 2 * round(12 * scale)

            total_w = gutter_w + sum(col_w)
            total_h = title_h + header_h + sum(row_h)

            if (_attempt == 1 or explicit_dpi is not None
                    or total_w * total_h <= max_total_pixels):
                # An explicit ``dpi`` is honoured exactly — the caller
                # opted out of both adaptive stages.
                break
            # Over budget: shrink panels toward the 96-dpi floor. The
            # 0.97 margin absorbs chrome that scales sublinearly
            # (text-derived gutter widths, rounded paddings) so the
            # re-measured layout lands under the budget, not on it.
            shrink = math.sqrt(max_total_pixels / (total_w * total_h)) * 0.97
            shrink = max(shrink, _BASE_DPI / effective_dpi)
            if shrink >= 1.0:
                # Already at the 96-dpi floor: readability wins over
                # the budget, but tell the caller — Pillow-based
                # consumers may need MAX_IMAGE_PIXELS raised to open
                # the result.
                import warnings
                warnings.warn(
                    f"Family grid is {total_w}x{total_h} px "
                    f"({total_w * total_h / 1e6:.0f}M pixels) at the "
                    f"96-dpi readability floor, exceeding "
                    f"max_total_pixels={max_total_pixels}. Consider "
                    "raising min_cases or partitioning on fewer "
                    "values; Pillow consumers may need "
                    "Image.MAX_IMAGE_PIXELS raised to open the file."
                )
                break
            for key, im in list(panels.items()):
                panels[key] = im.resize(
                    (max(1, round(im.width * shrink)),
                     max(1, round(im.height * shrink))),
                    Image.LANCZOS,
                )
            effective_dpi = max(_BASE_DPI, effective_dpi * shrink)

        # --------------------------------------------------------------
        # Composite.
        # --------------------------------------------------------------
        out = Image.new("RGB", (total_w, total_h), color=_BG)
        draw = ImageDraw.Draw(out)

        tw, _ = _text_size(draw, title, title_font)
        draw.text((max(pad, (total_w - tw) // 2), round(12 * scale)),
                  title, font=title_font, fill=_HEADER_COLOR)

        y0 = title_h
        if two_d:
            x = gutter_w
            for c, cv in enumerate(cols):
                w, _ = _text_size(draw, cv.label, header_font)
                draw.text(
                    (x + max(0, (col_w[c] - w) // 2), y0 + round(10 * scale)),
                    cv.label, font=header_font, fill=_HEADER_COLOR,
                )
                x += col_w[c]
            y0 += header_h

        y = y0
        for r in range(n_rows):
            # Row (member) label — rotated 90°, centred in the row,
            # elided if the row is shorter than the text.
            lab_im = _vertical_label(
                rows[r].label, label_font, probe,
                max_len_px=max(min_panel_h, row_h[r] - 2 * pad),
            )
            out.paste(
                lab_im,
                (max(0, (gutter_w - lab_im.width) // 2),
                 y + max(0, (row_h[r] - lab_im.height) // 2)),
                lab_im,
            )
            x = gutter_w
            for c in range(n_cols):
                im = panels.get((r, c))
                if im is not None:
                    x_off = x + max(0, (col_w[c] - im.width) // 2)
                    draw_y = y + pad
                    out.paste(im, (x_off, draw_y), mask=im)
                    cap = captions[(r, c)]
                    cw, _ = _text_size(draw, cap, caption_font)
                    draw.text(
                        (x + max(0, (col_w[c] - cw) // 2),
                         draw_y + im.height + round(6 * scale)),
                        cap, font=caption_font, fill=_CAPTION_COLOR,
                    )
                else:
                    text = placeholders[(r, c)]
                    box = (x + pad, y + pad,
                           x + col_w[c] - pad, y + row_h[r] - pad)
                    draw.rectangle(box, fill=_PLACEHOLDER_BG,
                                   outline=_GRID_LINE)
                    tw2, th2 = _text_size(draw, text, caption_font)
                    draw.text(
                        (x + max(0, (col_w[c] - tw2) // 2),
                         y + (row_h[r] - th2) // 2),
                        text, font=caption_font, fill=_PLACEHOLDER_FG,
                    )
                x += col_w[c]

            # Separator under the row (not after the last): the
            # boundary between FAMILY MEMBERS — thick and dark, so it
            # clearly outranks the thin light rules the stacked
            # renderer draws between a decomposed member's own maps.
            if r != n_rows - 1:
                draw.line(
                    [(0, y + row_h[r]), (total_w, y + row_h[r])],
                    fill=_MEMBER_LINE,
                    width=max(2, round(_MEMBER_LINE_W * scale)),
                )
            y += row_h[r]

        # Vertical separators between columns (matrix only) — also
        # member boundaries, same weight as the row separators.
        if two_d:
            x = gutter_w
            for c in range(n_cols - 1):
                x += col_w[c]
                draw.line([(x, y0), (x, total_h)],
                          fill=_MEMBER_LINE,
                          width=max(2, round(_MEMBER_LINE_W * scale)))

        # Record the effective DPI in the PNG metadata (both as the
        # standard physical-dimension chunk and as a readable text
        # entry) so downstream tools can honour the intended print
        # size and callers can verify what the adaptive chooser did.
        dpi_out = round(effective_dpi)
        meta = PngInfo()
        meta.add_text("pm4py_ucm_dpi", str(dpi_out))
        out.save(output_path, dpi=(dpi_out, dpi_out), pnginfo=meta)
        return output_path
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
