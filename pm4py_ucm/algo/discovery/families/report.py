"""Self-contained interactive HTML report for a model family.

:func:`write_family_report` produces **one HTML file with zero external
dependencies** — statistics embedded as JSON, per-cell model images
embedded as base64 PNGs, and a few hundred lines of vanilla JavaScript
for the interactivity. The file opens in any browser, works offline,
can be archived as supplementary material for a paper, and several
copies can be opened side by side ("multiple windows" = multiple tabs,
each with a different pair selected).

Five views:

* **Overview** — sortable process-level table (cases, events, events
  per case, case-duration min/median/mean/max/**total**, variants,
  fitness) with per-column heat-mapping;
* **Compare** — pick any two cells: headline delta cards, the two
  model images side by side, an activity delta table (Δ and ratio,
  diverging color scale, optional per-case normalisation), and
  aligned choice bars;
* **Activities** — activities × cells matrix for any metric,
  row/column/global heat-mapping;
* **Choices** — every OR-fork aligned across the family, one 100%
  stacked bar per cell with counts;
* **Models** — the per-cell images as a browsable gallery.

Every percentage and time is displayed next to its ``n`` — the cells
of a family routinely differ in size by an order of magnitude, and a
share without its sample size misleads. Metrics that the log cannot
support (service times without a ``start_timestamp`` column) are
omitted, never faked.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Dict, List, Optional

from .family import ModelFamily
from .stats import FamilyStats, compute_family_stats


#: Cell images wider than this are downscaled before embedding — keeps
#: a 36-cell report in the tens of MB at most while staying readable
#: when zoomed (the lightbox and the open-in-new-tab view show the
#: embedded image at its native size). Downscaling never goes below
#: the 96-dpi readability floor (half the rendering DPI): a wide
#: decomposed model keeps legible text even when that exceeds the
#: width cap — same discipline as the family grid.
DEFAULT_IMAGE_MAX_WIDTH = 2600

#: Raster DPI for the embedded cell images — twice graphviz's default,
#: same as the family grid's target: crisp text when zoomed in.
IMAGE_DPI = 192

#: Readability floor for the embedded images (see above).
_IMAGE_FLOOR_DPI = 96

#: Project home, linked from the report's header and footer.
GITHUB_URL = "https://github.com/ProcessMining-uOttawa/pm4py-ucm"

#: Tool branding shown in the report chrome. V3 = the PM4Py-UCM web
#: tool generation that introduced model families and these reports.
TOOL_BRAND = "PM4Py-UCM V3"


# ---------------------------------------------------------------------------
# Per-cell images
# ---------------------------------------------------------------------------

def _render_cell_images(
    family: ModelFamily,
    style: str,
) -> Optional[List[Optional[str]]]:
    """One base64 SVG ``data:`` URI per cell (``None`` entries for cells
    that failed to render); ``None`` altogether when rendering is
    unavailable (no graphviz binary).

    SVG rather than PNG: the embedded images stay crisp at any zoom, their
    text is selectable, and the report is a fraction of the size — no
    raster DPI, width cap or Pillow palette-quantisation needed, since a
    vector has no resolution to trade off. A decomposed cell's maps are
    stacked exactly as the PNG path composited them. The images are
    embedded (``<img src>``), so stub links are omitted (``navigable=False``)
    — they cannot be followed from an image anyway."""
    try:
        from ....visualization.ucm import svg as _svg
    except ImportError:  # pragma: no cover - visualization always ships
        return None

    uris: List[Optional[str]] = []
    for cell in family.cells:
        try:
            svg = _svg.model_to_svg(cell.ucm, style, navigable=False)
            uris.append(
                "data:image/svg+xml;base64,"
                + base64.b64encode(svg.encode("utf-8")).decode("ascii")
            )
        except Exception:
            # One unrenderable cell must not sink the report; the first
            # failure usually means graphviz is absent, so give up on the
            # remaining cells too.
            uris.append(None)
            if all(u is None for u in uris):
                return None
    return uris


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def family_report_html(
    family: ModelFamily,
    *,
    stats: Optional[FamilyStats] = None,
    title: Optional[str] = None,
    images: bool = True,
    style: str = "ucm",
    image_max_width: int = DEFAULT_IMAGE_MAX_WIDTH,
) -> str:
    """Build the report and return it as an HTML string.

    ``stats`` defaults to :func:`~.stats.compute_family_stats` on the
    family (which then needs ``family.log_df``); pass a precomputed
    :class:`~.stats.FamilyStats` when the log has been dropped.
    ``images=False`` (or an environment without the graphviz binary)
    omits the embedded per-cell model images; the statistics views are
    unaffected. The images are now vector SVG, so ``image_max_width`` is
    accepted for backward compatibility but ignored (a vector has no
    resolution to cap). The output is deterministic for a given family —
    no timestamps are embedded."""
    if stats is None:
        stats = compute_family_stats(family)

    data: Dict[str, Any] = stats.to_dict()
    data["title"] = title or (
        "Model family — by " + " × ".join(stats.attribute_names)
    )
    data["style"] = style

    image_uris = (
        _render_cell_images(family, style)
        if images else None
    )
    if image_uris is not None:
        for cell_dict, uri in zip(data["cells"], image_uris):
            cell_dict["image"] = uri

    payload = json.dumps(
        data, ensure_ascii=False, separators=(",", ":"),
    ).replace("</", "<\\/")
    from .... import __version__  # lazy: pm4py_ucm is fully imported
    return (
        _TEMPLATE
        .replace("__TITLE__", _html_escape(data["title"]))
        .replace("__BRAND__", _html_escape(TOOL_BRAND))
        .replace("__VERSION__", _html_escape(__version__))
        .replace("__GITHUB__", GITHUB_URL)
        .replace("__DATA__", payload)
    )


def write_family_report(
    family: ModelFamily,
    path: str,
    *,
    stats: Optional[FamilyStats] = None,
    title: Optional[str] = None,
    images: bool = True,
    style: str = "ucm",
    image_max_width: int = DEFAULT_IMAGE_MAX_WIDTH,
) -> str:
    """Write the interactive family report to ``path`` (see
    :func:`family_report_html`). Returns ``path``."""
    html = family_report_html(
        family, stats=stats, title=title, images=images,
        style=style, image_max_width=image_max_width,
    )
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    return path


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------
#
# One template, two placeholders (__TITLE__, __DATA__). Everything else
# is client-side: the tables, heatmaps, bars and comparisons are built
# in vanilla JS from the embedded DATA object. Colors are
# colorblind-safe (blue sequential ramp, blue-orange diverging scale,
# Okabe-Ito categorical palette for choice branches).

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  --accent: #2166ac;
  --ink: #1a2733;
  --muted: #5b6b7a;
  --line: #dde5ec;
  --bg: #f4f7fa;
  --card: #ffffff;
  --pos: #2166ac;
  --neg: #b35806;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: "Segoe UI", system-ui, -apple-system, Roboto, Arial, sans-serif;
  background: var(--bg); color: var(--ink); font-size: 14px;
}
header {
  background: linear-gradient(135deg, #123c63, #2166ac);
  color: #fff; padding: 22px 28px 14px;
}
header h1 { margin: 0 0 4px; font-size: 21px; font-weight: 600; }
header .sub { opacity: .85; font-size: 13px; }
header .sub a { color: #fff; font-weight: 600; }
footer a { color: var(--accent); font-weight: 600; }
.chips { margin-top: 12px; display: flex; flex-wrap: wrap; gap: 8px; }
.chip {
  background: rgba(255,255,255,.14); border-radius: 20px;
  padding: 4px 12px; font-size: 12.5px;
}
.chip b { font-weight: 600; }
nav {
  position: sticky; top: 0; z-index: 30; background: var(--card);
  border-bottom: 1px solid var(--line); padding: 0 20px;
  display: flex; gap: 4px; box-shadow: 0 1px 4px rgba(16,42,67,.06);
}
nav button {
  border: none; background: none; padding: 12px 16px; cursor: pointer;
  font: inherit; font-weight: 600; color: var(--muted);
  border-bottom: 3px solid transparent;
}
nav button.active { color: var(--accent); border-bottom-color: var(--accent); }
nav button:hover { color: var(--accent); }
main { padding: 20px; max-width: 1500px; margin: 0 auto; }
section.view { display: none; }
section.view.active { display: block; }
.card {
  background: var(--card); border: 1px solid var(--line);
  border-radius: 10px; padding: 16px 18px; margin-bottom: 18px;
  box-shadow: 0 1px 3px rgba(16,42,67,.05);
}
.card h2 { margin: 0 0 4px; font-size: 16px; }
.card .hint { color: var(--muted); font-size: 12.5px; margin: 0 0 12px; }
.controls {
  display: flex; flex-wrap: wrap; gap: 14px; align-items: center;
  margin-bottom: 12px; font-size: 13px;
}
.controls label { color: var(--muted); font-weight: 600; }
select {
  font: inherit; padding: 5px 8px; border: 1px solid var(--line);
  border-radius: 6px; background: #fff; color: var(--ink); max-width: 340px;
}
.toggle { display: inline-flex; align-items: center; gap: 5px; cursor: pointer; }
button.small {
  font: inherit; font-size: 12.5px; padding: 5px 10px; cursor: pointer;
  border: 1px solid var(--line); background: #fff; border-radius: 6px;
}
button.small:hover { border-color: var(--accent); color: var(--accent); }
.tbl-wrap { overflow-x: auto; }
table.data { border-collapse: collapse; width: 100%; font-size: 13px; }
table.data th, table.data td {
  padding: 6px 10px; border-bottom: 1px solid var(--line);
  text-align: right; white-space: nowrap;
}
table.data th {
  cursor: pointer; user-select: none; background: #eef3f8;
  color: #33475b; font-weight: 600;
}
table.data th .arr { opacity: .5; font-size: 10px; margin-left: 3px; }
table.data th:first-child, table.data td:first-child {
  text-align: left; position: sticky; left: 0; z-index: 1;
  background: var(--card);
  font-weight: 600; max-width: 260px; overflow: hidden;
  text-overflow: ellipsis;
}
table.data th:first-child { background: #eef3f8; z-index: 2; }
table.data tr:hover td { filter: brightness(.97); }
td .n { color: var(--muted); font-weight: 400; font-size: 11px; margin-left: 4px; }
td.na { color: #b3bfca; }
.cards-row { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 14px; }
.mcard {
  flex: 1 1 150px; min-width: 150px; border: 1px solid var(--line);
  border-radius: 8px; padding: 10px 12px; background: #fbfcfe;
}
.mcard .t { color: var(--muted); font-size: 11.5px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .4px; }
.mcard .v { font-size: 15px; margin-top: 3px; }
.mcard .v b { font-size: 16px; }
.mcard .d { font-size: 12px; margin-top: 3px; font-weight: 600; }
.d.up { color: var(--neg); } .d.down { color: var(--pos); } .d.flat { color: var(--muted); }
.imgs { display: flex; flex-wrap: wrap; gap: 14px; }
.imgbox { flex: 1 1 420px; min-width: 300px; }
.imgbox .cap {
  font-size: 12.5px; color: var(--muted); margin-bottom: 6px;
}
.imgbox .cap b { color: var(--ink); }
.imgbox img {
  width: 100%; height: auto; border: 1px solid var(--line);
  border-radius: 8px; background: #fff; cursor: zoom-in;
}
.noimg {
  border: 1px dashed var(--line); border-radius: 8px; padding: 30px;
  text-align: center; color: var(--muted); background: #fbfcfe;
}
.gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; }
#lightbox {
  display: none; position: fixed; inset: 0; z-index: 100;
  background: rgba(12,24,38,.82); cursor: zoom-out; overflow: auto;
  padding: 24px; text-align: center;
}
#lightbox img { max-width: none; background: #fff; border-radius: 6px; }
#lightbox .lbcap { color: #fff; margin-bottom: 10px; font-size: 14px; }
.choice { margin-bottom: 22px; }
.choice h3 { margin: 0 0 2px; font-size: 14px; }
.choice .meta { color: var(--muted); font-size: 12px; margin-bottom: 8px; }
.legend { display: flex; flex-wrap: wrap; gap: 12px; font-size: 12px;
  margin-bottom: 8px; color: var(--muted); }
.legend .sw { display: inline-block; width: 11px; height: 11px;
  border-radius: 3px; margin-right: 5px; vertical-align: -1px; }
.bar-row { display: flex; align-items: center; margin-bottom: 5px; gap: 10px; }
.bar-row .lab {
  width: 220px; min-width: 130px; font-size: 12.5px; text-align: right;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.bar-row .lab .n { display: block; color: var(--muted); font-size: 11px; }
.bar { flex: 1; display: flex; height: 22px; border-radius: 5px;
  overflow: hidden; background: #edf1f5; }
.bar .seg { height: 100%; display: flex; align-items: center;
  justify-content: center; color: #fff; font-size: 11px; font-weight: 600;
  overflow: hidden; white-space: nowrap; }
.bar .seg.dark { color: #1a2733; }
.bar-row.unreached .bar { background: repeating-linear-gradient(
  45deg, #eef1f5, #eef1f5 6px, #e3e8ee 6px, #e3e8ee 12px); }
.bar-row.unreached .lab { color: #b3bfca; }
.note { font-size: 12px; color: var(--muted); margin-top: 8px; }
footer { text-align: center; color: var(--muted); font-size: 12px;
  padding: 18px; }
@media (max-width: 800px) {
  .bar-row .lab { width: 120px; }
}
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <div class="sub">Interactive statistics report · generated by
  <a href="__GITHUB__" target="_blank" rel="noopener">__BRAND__</a>
  (pm4py-ucm __VERSION__) · self-contained (works offline)</div>
  <div class="chips" id="chips"></div>
</header>
<nav id="nav"></nav>
<main id="main"></main>
<div id="lightbox" onclick="this.style.display='none'"></div>
<footer>__BRAND__ family report — sortable columns (click a header),
heat-mapped values, and a pair comparison under <b>Compare</b>. Open
the file several times to compare more than one pair side by side.
· <a href="__GITHUB__" target="_blank" rel="noopener">pm4py-ucm on
GitHub</a></footer>
<script>
const DATA = __DATA__;

// ---------- palettes (colorblind-safe) ------------------------------------
const CAT = ["#0072B2","#E69F00","#009E73","#CC79A7","#56B4E9",
             "#D55E00","#F0E442","#999999"];
function lerp(a, b, t) { return a + (b - a) * t; }
function mix(c1, c2, t) {
  return [0,1,2].map(i => Math.round(lerp(c1[i], c2[i], t)));
}
const SEQ_LO = [247,251,255], SEQ_HI = [33,102,172];      // white -> blue
const DIV_NEG = [179,88,6], DIV_MID = [247,247,247], DIV_POS = [33,102,172];
function seqRgb(t) { return mix(SEQ_LO, SEQ_HI, Math.max(0, Math.min(1, t))); }
function divRgb(t) { // t in [-1, 1]
  t = Math.max(-1, Math.min(1, t));
  return t < 0 ? mix(DIV_MID, DIV_NEG, -t) : mix(DIV_MID, DIV_POS, t);
}
// Background + a text color that stays readable on it — white only
// when the background's relative luminance is genuinely dark.
function heatStyle(rgb) {
  const lum = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2];
  return "background:rgb(" + rgb.join(",") + ");color:" +
    (lum < 140 ? "#fff" : "#1a2733");
}

// ---------- formatting -----------------------------------------------------
function esc(s) {
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
function fmtDur(s) {
  if (s == null || isNaN(s)) return "—";
  const sign = s < 0 ? "-" : ""; s = Math.abs(s);
  if (s < 60) return sign + s.toFixed(0) + "s";
  if (s < 3600) return sign + (s/60).toFixed(1) + "m";
  if (s < 86400) return sign + (s/3600).toFixed(1) + "h";
  const d = s / 86400;
  if (d >= 500) return sign + (d/365.25).toFixed(1) + "y";
  return sign + d.toFixed(d >= 100 ? 0 : 1) + "d";
}
function fmtNum(x, dec) {
  if (x == null || isNaN(x)) return "—";
  if (dec === undefined) dec = (Math.abs(x) < 10 && x % 1 !== 0) ? 1 : 0;
  return x.toLocaleString("en-US",
    {minimumFractionDigits: 0, maximumFractionDigits: dec});
}
function fmtPct(x, dec) {
  if (x == null || isNaN(x)) return "—";
  return x.toFixed(dec === undefined ? 1 : dec) + "%";
}

// ---------- metric registry ------------------------------------------------
const hasT = DATA.hasTimestamps, hasI = DATA.hasIntervals;
// Ratio (0..1) formatted as a percentage.
function fmtRatio(x) {
  return (x == null || isNaN(x)) ? "—" : (x * 100).toFixed(1) + "%";
}
const ACT_METRICS = [
  {key: "frequency", label: "frequency (executions)", fmt: fmtNum,
   perCase: true},
  {key: "relative_frequency", label: "relative frequency (share of events)",
   fmt: fmtRatio, perCase: false},
  {key: "case_coverage", label: "case coverage (cases)", fmt: fmtNum,
   perCase: true},
  {key: "repeat_frequency", label: "repeat frequency (rework executions)",
   fmt: fmtNum, perCase: true},
];
if (hasI) {
  for (const [k, lab] of [["mean_time","mean service time"],
      ["median_time","median service time"], ["min_time","min service time"],
      ["max_time","max service time"], ["std_time","std service time"],
      ["p90_time","P90 service time"], ["p95_time","P95 service time"],
      ["total_time","total service time"]])
    ACT_METRICS.push({key: k, label: lab, fmt: fmtDur, perCase: false});
}
if (hasT) {
  // Sojourn = time since the case's PREVIOUS event (~ waiting +
  // service attributed to the activity) — derivable from completion
  // timestamps alone, so single-timestamp logs get activity-level
  // time statistics too.
  for (const [k, lab] of [
      ["sojourn_mean_time","mean sojourn time (since previous event)"],
      ["sojourn_median_time","median sojourn time"],
      ["sojourn_min_time","min sojourn time"],
      ["sojourn_max_time","max sojourn time"],
      ["sojourn_std_time","std sojourn time"],
      ["sojourn_p90_time","P90 sojourn time"],
      ["sojourn_p95_time","P95 sojourn time"],
      ["sojourn_total_time","total sojourn time"]])
    ACT_METRICS.push({key: k, label: lab, fmt: fmtDur, perCase: false});
}
const EDGE_METRICS = [
  {key: "frequency", label: "frequency (traversals)", fmt: fmtNum,
   perCase: true},
  {key: "case_frequency", label: "case frequency (distinct cases)",
   fmt: fmtNum, perCase: true},
  {key: "relative_frequency", label: "relative frequency (share of traversals)",
   fmt: fmtRatio, perCase: false},
];
if (hasT) {
  const w = hasI ? "waiting time" : "waiting time (compl.→compl.)";
  for (const [k, lab] of [["mean_time","mean " + w],
      ["median_time","median " + w], ["min_time","min " + w],
      ["max_time","max " + w], ["std_time","std " + w],
      ["p90_time","P90 " + w], ["p95_time","P95 " + w],
      ["total_time","total " + w]])
    EDGE_METRICS.push({key: k, label: lab, fmt: fmtDur, perCase: false});
}
const PROC_COLS = (() => {
  const c = [
    {key: "cases", label: "cases", get: x => x.cases, fmt: fmtNum},
    {key: "coverage", label: "share", get: x => x.coverage * 100,
     fmt: v => fmtPct(v)},
    {key: "events", label: "events", get: x => x.events, fmt: fmtNum},
    {key: "epc", label: "events/case (mean)",
     get: x => x.eventsPerCase.mean, fmt: v => fmtNum(v, 1)},
    {key: "acts", label: "activities", get: x => x.activities, fmt: fmtNum},
  ];
  if (hasT) {
    for (const [k, lab] of [["min","min"],["median","median"],
        ["mean","mean"],["max","max"],["p90","P90"],["p95","P95"],
        ["std","std"],["total","TOTAL"]])
      c.push({key: "dur_" + k, label: "duration " + lab,
              get: x => x.duration[k], fmt: fmtDur});
  }
  c.push({key: "rework", label: "rework (cases w/ repeat)",
          get: x => x.rework ? x.rework.case_fraction : null,
          fmt: fmtRatio});
  c.push({key: "variants", label: "variants",
          get: x => x.variants.n_variants, fmt: fmtNum});
  c.push({key: "seqvar", label: "seq. variants",
          get: x => x.variants.n_sequence_variants, fmt: fmtNum});
  c.push({key: "fitness", label: "fitness",
          get: x => x.variants.fitness == null ? null
                    : x.variants.fitness * 100, fmt: v => fmtPct(v)});
  return c;
})();

// ---------- generic sortable heat-mapped table ------------------------------
// rows: [{label, cells: [{v, text, n}] }]; state kept per table id.
const tableState = {};
function renderTable(el, id, cols, rows, opts) {
  opts = opts || {};
  const st = tableState[id] || (tableState[id] = {sort: null, asc: false});
  let sorted = rows.slice();
  if (st.sort !== null) {
    sorted.sort((a, b) => {
      if (st.sort === -1)
        return st.asc ? a.label.localeCompare(b.label)
                      : b.label.localeCompare(a.label);
      const av = a.cells[st.sort].v, bv = b.cells[st.sort].v;
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      return st.asc ? av - bv : bv - av;
    });
  }
  // per-column ranges for the heatmap
  const ranges = cols.map((c, j) => {
    let lo = Infinity, hi = -Infinity;
    for (const r of rows) {
      const v = r.cells[j].v;
      if (v != null && !isNaN(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    }
    return [lo, hi];
  });
  let h = '<div class="tbl-wrap"><table class="data"><thead><tr>';
  const arrow = j => st.sort === j ? (st.asc ? " ▲" : " ▼") : " ⇅";
  h += '<th data-j="-1">' + esc(opts.rowHeader || "") +
       '<span class="arr">' + arrow(-1) + "</span></th>";
  cols.forEach((c, j) => {
    h += '<th data-j="' + j + '" title="click to sort">' + esc(c.label) +
         '<span class="arr">' + arrow(j) + "</span></th>";
  });
  h += "</tr></thead><tbody>";
  for (const r of sorted) {
    h += "<tr><td title=\"" + esc(r.label) + "\">" + esc(r.label) + "</td>";
    r.cells.forEach((cell, j) => {
      const v = cell.v;
      let stylec = "", cls = "";
      if (v == null || isNaN(v)) cls = "na";
      else if (opts.heat) {
        const [lo, hi] = ranges[j];
        if (opts.diverging && opts.diverging[j]) {
          const m = Math.max(Math.abs(lo), Math.abs(hi));
          if (m > 0) stylec = heatStyle(divRgb(v / m));
        } else if (hi > lo) {
          stylec = heatStyle(seqRgb((v - lo) / (hi - lo)));
        }
      }
      h += '<td class="' + cls + '" style="' + stylec + '">' +
        (cell.text != null ? cell.text : "—") +
        (cell.n != null ? '<span class="n">' + cell.n + "</span>" : "") +
        "</td>";
    });
    h += "</tr>";
  }
  h += "</tbody></table></div>";
  el.innerHTML = h;
  el.querySelectorAll("th").forEach(th => th.onclick = () => {
    const j = parseInt(th.dataset.j, 10);
    if (st.sort === j) st.asc = !st.asc; else { st.sort = j; st.asc = j === -1; }
    renderTable(el, id, cols, rows, opts);
  });
}

// ---------- header chips + nav ----------------------------------------------
(function () {
  const chips = [
    "<b>" + DATA.cells.length + "</b> processes",
    "<b>" + fmtNum(DATA.coveredCases) + "</b> / " +
      fmtNum(DATA.totalCases) + " cases covered",
    "<b>" + DATA.activityNames.length + "</b> activities",
    "<b>" + DATA.edgeNames.length + "</b> activity pairs",
    "<b>" + DATA.choices.length + "</b> choice points",
  ];
  if (DATA.droppedCases) chips.push(fmtNum(DATA.droppedCases) + " cases dropped");
  if (DATA.skipped.length) chips.push(DATA.skipped.length + " small cells skipped");
  chips.push(hasI ? "interval log — service times available"
    : (hasT ? "single timestamps — no service times" : "no timestamps"));
  document.getElementById("chips").innerHTML =
    chips.map(c => '<span class="chip">' + c + "</span>").join("");
})();

const VIEWS = [["overview","Overview"],["compare","Compare"],
  ["activities","Activities"],["edges","Edges"],
  ["choices","Choices"],["models","Models"]];
const nav = document.getElementById("nav");
const main = document.getElementById("main");
for (const [id, label] of VIEWS) {
  const b = document.createElement("button");
  b.textContent = label; b.dataset.v = id;
  b.onclick = () => showView(id);
  nav.appendChild(b);
  const s = document.createElement("section");
  s.className = "view"; s.id = "view-" + id;
  main.appendChild(s);
}
function showView(id) {
  nav.querySelectorAll("button").forEach(b =>
    b.classList.toggle("active", b.dataset.v === id));
  main.querySelectorAll("section.view").forEach(s =>
    s.classList.toggle("active", s.id === "view-" + id));
  if (location.hash !== "#" + id) history.replaceState(null, "", "#" + id);
}

// ---------- Overview ---------------------------------------------------------
(function () {
  const el = document.getElementById("view-overview");
  let heat = true;
  el.innerHTML =
    '<div class="card"><h2>Process comparison — one row per family member</h2>' +
    '<p class="hint">Click a column header to rank the processes. ' +
    'Colors are scaled per column (darker = larger). Durations are per ' +
    'case; <b>duration TOTAL</b> is the sum over all of the cell’s ' +
    'cases.</p>' +
    '<div class="controls"><label class="toggle">' +
    '<input type="checkbox" id="ov-heat" checked> heatmap</label></div>' +
    '<div id="ov-table"></div>' +
    '<div class="note" id="ov-skipped"></div></div>';
  const rows = DATA.cells.map(c => ({
    label: c.label,
    cells: PROC_COLS.map(col => {
      const v = col.get(c);
      return {v: v, text: col.fmt(v)};
    }),
  }));
  function draw() {
    renderTable(document.getElementById("ov-table"), "ov", PROC_COLS, rows,
      {heat: heat, rowHeader: DATA.attributes.join(" / ")});
  }
  document.getElementById("ov-heat").onchange = e => {
    heat = e.target.checked; draw();
  };
  if (DATA.skipped.length) {
    document.getElementById("ov-skipped").textContent =
      "Skipped (below min cases): " + DATA.skipped.map(s =>
        s.labels.join(" / ") + " (n=" + s.cases + ")").join(", ") + ".";
  }
  draw();
})();

// ---------- Compare ----------------------------------------------------------
(function () {
  const el = document.getElementById("view-compare");
  const opts = DATA.cells.map((c, i) =>
    '<option value="' + i + '">' + esc(c.label) +
    " (n=" + c.cases + ")</option>").join("");
  el.innerHTML =
    '<div class="card"><h2>Compare two processes</h2>' +
    '<p class="hint">Pick any two family members. Deltas are B relative ' +
    'to A. Open this file in another browser tab to compare a second ' +
    'pair side by side.</p>' +
    '<div class="controls">' +
    '<label>A</label><select id="selA">' + opts + "</select>" +
    '<button class="small" id="swap" title="swap A and B">⇄ swap</button>' +
    '<label>B</label><select id="selB">' + opts + "</select>" +
    "</div>" +
    '<div class="cards-row" id="cmp-cards"></div></div>' +
    '<div class="card"><h2>Models side by side</h2>' +
    '<p class="hint">Click an image to zoom in place, or open it in ' +
    "its own browser tab at full resolution.</p>" +
    '<div class="imgs" id="cmp-imgs"></div></div>' +
    '<div class="card"><h2>Activity comparison</h2>' +
    '<p class="hint">Δ and ratio are B vs A; the Δ column is ' +
    'color-scaled around zero (blue = larger in B, orange = larger in A).</p>' +
    '<div class="controls">' +
    '<label>metric</label><select id="cmp-metric"></select>' +
    '<label class="toggle" id="cmp-pc-wrap">' +
    '<input type="checkbox" id="cmp-percase"> per case ' +
    '(divide by the cell’s case count)</label></div>' +
    '<div id="cmp-table"></div></div>' +
    (DATA.edgeNames.length ?
      '<div class="card"><h2>Edge comparison</h2>' +
      '<p class="hint">Directly-follows handovers of A and B — ' +
      'traversal counts and waiting times' + (hasI ? "" :
      " (completion-to-completion on this single-timestamp log)") +
      ".</p>" +
      '<div class="controls">' +
      '<label>metric</label><select id="cmp-emetric"></select>' +
      '<label class="toggle" id="cmp-epc-wrap">' +
      '<input type="checkbox" id="cmp-epercase"> per case</label></div>' +
      '<div id="cmp-etable"></div></div>' : "") +
    '<div class="card"><h2>Choices</h2>' +
    '<p class="hint">Branch shares of each OR-fork, A above B. ' +
    'n = branch traversals.</p><div id="cmp-choices"></div></div>';

  const selA = document.getElementById("selA");
  const selB = document.getElementById("selB");
  selB.value = String(Math.min(1, DATA.cells.length - 1));
  const mSel = document.getElementById("cmp-metric");
  mSel.innerHTML = ACT_METRICS.map((m, i) =>
    '<option value="' + i + '">' + esc(m.label) + "</option>").join("");
  const eSel = document.getElementById("cmp-emetric");
  if (eSel) eSel.innerHTML = EDGE_METRICS.map((m, i) =>
    '<option value="' + i + '">' + esc(m.label) + "</option>").join("");

  // A/B delta table over named entries (shared by the activity and
  // edge comparisons).
  function deltaTable(el, tableId, names, entryOf, m, perCase, A, B,
                      rowHeader) {
    const fmt = perCase ? (v => fmtNum(v, 2)) : m.fmt;
    const rows = names.map(name => {
      const ea = entryOf(A, name), eb = entryOf(B, name);
      let va = ea ? ea[m.key] : null, vb = eb ? eb[m.key] : null;
      if (va === undefined) va = null;
      if (vb === undefined) vb = null;
      if (perCase) {
        va = va == null ? null : va / A.cases;
        vb = vb == null ? null : vb / B.cases;
      }
      const d = (va != null && vb != null) ? vb - va : null;
      const r = (va != null && vb != null && va !== 0) ? vb / va : null;
      return {label: name, cells: [
        {v: va, text: fmt(va)},
        {v: vb, text: fmt(vb)},
        {v: d, text: d == null ? "—" : (d > 0 ? "+" : "") + fmt(d)},
        {v: r, text: r == null ? "—" : r.toFixed(2) + "×"},
      ]};
    }).filter(r => r.cells[0].v != null || r.cells[1].v != null);
    renderTable(el, tableId,
      [{label: "A — " + A.label}, {label: "B — " + B.label},
       {label: "Δ (B−A)"}, {label: "ratio B/A"}],
      rows, {heat: true, diverging: [false, false, true, false],
             rowHeader: rowHeader});
  }

  function deltaBadge(a, b, fmt, invert) {
    if (a == null || b == null || isNaN(a) || isNaN(b))
      return '<div class="d flat">—</div>';
    const d = b - a;
    const pct = a !== 0 ? (d / Math.abs(a)) * 100 : null;
    const cls = d === 0 ? "flat" : (d > 0 ? "up" : "down");
    const sign = d > 0 ? "+" : "";
    return '<div class="d ' + cls + '">' + sign + fmt(d) +
      (pct != null ? " (" + sign + pct.toFixed(0) + "%)" : "") + "</div>";
  }

  function draw() {
    const ia = +selA.value, ib = +selB.value;
    const A = DATA.cells[ia], B = DATA.cells[ib];
    // headline cards
    const defs = [
      ["cases", c => c.cases, fmtNum],
      ["events/case (mean)", c => c.eventsPerCase.mean, v => fmtNum(v, 1)],
    ];
    if (hasT) {
      defs.push(["mean duration", c => c.duration.mean, fmtDur]);
      defs.push(["median duration", c => c.duration.median, fmtDur]);
      defs.push(["P90 duration", c => c.duration.p90, fmtDur]);
      defs.push(["TOTAL duration", c => c.duration.total, fmtDur]);
    }
    defs.push(["rework rate", c => c.rework ? c.rework.case_fraction : null,
               fmtRatio]);
    defs.push(["variants", c => c.variants.n_variants, fmtNum]);
    document.getElementById("cmp-cards").innerHTML = defs.map(d => {
      const [t, get, fmt] = d, a = get(A), b = get(B);
      return '<div class="mcard"><div class="t">' + esc(t) + "</div>" +
        '<div class="v"><b>' + fmt(a) + "</b> → <b>" + fmt(b) +
        "</b></div>" + deltaBadge(a, b, fmt) + "</div>";
    }).join("");

    // images
    const imgs = document.getElementById("cmp-imgs");
    imgs.innerHTML = [["A", A], ["B", B]].map(p => {
      const [tag, c] = p;
      const cap = '<div class="cap"><b>' + tag + " — " + esc(c.label) +
        "</b> · n=" + c.cases + " (" + fmtPct(c.coverage * 100) +
        ' of the log) · <button class="small ot">open in new tab ⧉' +
        "</button></div>";
      if (!c.image)
        return '<div class="imgbox"><div class="cap"><b>' + tag +
          " — " + esc(c.label) + "</b></div>" +
          '<div class="noimg">no embedded image</div></div>';
      return '<div class="imgbox">' + cap +
        '<img src="' + c.image + '" alt="' + esc(c.label) +
        '" loading="lazy" data-cap="' + esc(c.label) + '"></div>';
    }).join("");
    wireImages(imgs);

    // activity table
    const m = ACT_METRICS[+mSel.value];
    const perCase = m.perCase && document.getElementById("cmp-percase").checked;
    document.getElementById("cmp-pc-wrap").style.display =
      m.perCase ? "" : "none";
    deltaTable(document.getElementById("cmp-table"), "cmp",
      DATA.activityNames, (c, name) => c.activity[name],
      m, perCase, A, B, "activity");

    // edge table (only when the log carries timestamps)
    if (eSel) {
      const em = EDGE_METRICS[+eSel.value];
      const ePerCase = em.perCase &&
        document.getElementById("cmp-epercase").checked;
      document.getElementById("cmp-epc-wrap").style.display =
        em.perCase ? "" : "none";
      deltaTable(document.getElementById("cmp-etable"), "cmpE",
        DATA.edgeNames, (c, name) => c.edges[name],
        em, ePerCase, A, B, "edge");
    }

    // choices
    document.getElementById("cmp-choices").innerHTML =
      DATA.choices.map(ch => {
        const rows = [[ "A — " + A.label, ch.counts[ia]],
                      ["B — " + B.label, ch.counts[ib]]];
        return choiceBlock(ch, rows);
      }).join("") ||
      '<p class="hint">The family’s processes contain no OR-fork ' +
      "choices.</p>";
  }
  selA.onchange = selB.onchange = mSel.onchange = draw;
  document.getElementById("cmp-percase").onchange = draw;
  if (eSel) {
    eSel.onchange = draw;
    document.getElementById("cmp-epercase").onchange = draw;
  }
  document.getElementById("swap").onclick = () => {
    const t = selA.value; selA.value = selB.value; selB.value = t; draw();
  };
  draw();
})();

// ---------- choice rendering (shared by Compare / Choices views) --------------
function choiceBlock(ch, rows) {
  const legend = '<div class="legend">' + ch.branches.map((b, i) =>
    '<span><span class="sw" style="background:' + CAT[i % CAT.length] +
    '"></span>' + esc(b) + "</span>").join("") + "</div>";
  const bars = rows.map(r => {
    const [label, counts] = r;
    if (!counts) {
      return '<div class="bar-row unreached"><div class="lab">' +
        esc(label) + '<span class="n">not reached</span></div>' +
        '<div class="bar"></div></div>';
    }
    const total = counts.reduce((s, x) => s + x, 0);
    const segs = counts.map((c, i) => {
      if (!c) return "";
      const pct = total ? (c / total) * 100 : 0;
      const lab = pct >= 12 ? pct.toFixed(0) + "%" : "";
      return '<div class="seg" style="width:' + pct + "%;background:" +
        CAT[i % CAT.length] + '" title="' + esc(ch.branches[i]) + ": " +
        pct.toFixed(1) + "% (n=" + c + ')">' + lab + "</div>";
    }).join("");
    return '<div class="bar-row"><div class="lab">' + esc(label) +
      '<span class="n">n=' + total + "</span></div>" +
      '<div class="bar">' + segs + "</div></div>";
  }).join("");
  const meta = [];
  meta.push(ch.shared ? "shared skeleton fork"
                      : "variation-point fork (not in every process)");
  if (ch.insideLoop)
    meta.push("inside a loop — counts are evaluations across " +
              "iterations and may exceed case counts");
  return '<div class="choice"><h3>' + esc(ch.fullName) + "</h3>" +
    '<div class="meta">' + meta.join(" · ") + "</div>" +
    legend + bars + "</div>";
}

// ---------- generic metric matrix (Activities / Edges views) -----------------
function matrixView(cfg) {
  const el = document.getElementById("view-" + cfg.id);
  if (!cfg.names.length) {
    el.innerHTML = '<div class="card"><h2>' + cfg.title + "</h2>" +
      '<p class="hint">' + cfg.empty + "</p></div>";
    return;
  }
  el.innerHTML =
    '<div class="card"><h2>' + cfg.title + "</h2>" +
    '<p class="hint">' + cfg.hint + "</p>" +
    '<div class="controls">' +
    '<label>metric</label><select id="' + cfg.id + '-metric"></select>' +
    '<label class="toggle" id="' + cfg.id + '-pc-wrap">' +
    '<input type="checkbox" id="' + cfg.id + '-percase"> per case</label>' +
    '<label class="toggle"><input type="checkbox" id="' + cfg.id +
    '-heat" checked> heatmap</label></div>' +
    '<div id="' + cfg.id + '-table"></div>' +
    '<p class="note">' + cfg.note + "</p></div>";
  const mSel = document.getElementById(cfg.id + "-metric");
  mSel.innerHTML = cfg.metrics.map((m, i) =>
    '<option value="' + i + '">' + esc(m.label) + "</option>").join("");
  function draw() {
    const m = cfg.metrics[+mSel.value];
    const perCase = m.perCase &&
      document.getElementById(cfg.id + "-percase").checked;
    document.getElementById(cfg.id + "-pc-wrap").style.display =
      m.perCase ? "" : "none";
    const heat = document.getElementById(cfg.id + "-heat").checked;
    const fmt = perCase ? (v => fmtNum(v, 2)) : m.fmt;
    const rows = cfg.names.map(name => ({
      label: name,
      cells: DATA.cells.map(c => {
        const e = cfg.entry(c, name);
        let v = e ? e[m.key] : null;
        if (v === undefined) v = null;
        if (perCase && v != null) v = v / c.cases;
        return {v: v, text: fmt(v)};
      }),
    }));
    renderTable(document.getElementById(cfg.id + "-table"), cfg.id,
      DATA.cells.map(c => ({label: c.label})), rows,
      {heat: heat, rowHeader: cfg.rowHeader});
  }
  mSel.onchange = draw;
  document.getElementById(cfg.id + "-percase").onchange = draw;
  document.getElementById(cfg.id + "-heat").onchange = draw;
  draw();
}

matrixView({
  id: "activities",
  title: "Activities × processes",
  hint: "Every activity across the whole family; columns are the " +
    "family members. Click headers to rank.",
  note: "Heatmap scaling is per column (per process); — marks " +
    "activities the process never executes.",
  empty: "No activities.",
  rowHeader: "activity",
  names: DATA.activityNames,
  metrics: ACT_METRICS,
  entry: (c, name) => c.activity[name],
});
matrixView({
  id: "edges",
  title: "Edges — directly-follows activity pairs × processes",
  hint: "Handovers between two consecutive activities: traversal " +
    "frequency and waiting time" + (hasI ? "" :
    " — measured completion-to-completion on this single-timestamp " +
    "log, so it includes the successor's own duration") +
    ". Ordered by family-wide frequency; click headers to rank.",
  note: "Heatmap scaling is per column (per process); — marks pairs " +
    "the process never traverses.",
  empty: "This log carries no timestamps — event order and " +
    "directly-follows statistics are not derivable.",
  rowHeader: "edge",
  names: DATA.edgeNames,
  metrics: EDGE_METRICS,
  entry: (c, name) => c.edges[name],
});

// ---------- Choices --------------------------------------------------------------
(function () {
  const el = document.getElementById("view-choices");
  if (!DATA.choices.length) {
    el.innerHTML = '<div class="card"><h2>Choices</h2>' +
      '<p class="hint">The family’s processes contain no OR-fork ' +
      "choices to compare.</p></div>";
    return;
  }
  el.innerHTML = '<div class="card"><h2>Choice points across the family</h2>' +
    '<p class="hint">Each OR-fork of the (aligned) family processes, ' +
    'one 100% stacked bar per family member. Hover a segment for the ' +
    'exact share and count.</p><div id="ch-all"></div></div>';
  document.getElementById("ch-all").innerHTML = DATA.choices.map(ch =>
    choiceBlock(ch, DATA.cells.map((c, i) => [c.label, ch.counts[i]]))
  ).join("");
})();

// ---------- Models -----------------------------------------------------------------
(function () {
  const el = document.getElementById("view-models");
  const any = DATA.cells.some(c => c.image);
  el.innerHTML = '<div class="card"><h2>The family’s models</h2>' +
    '<p class="hint">One ' + esc(DATA.style).toUpperCase() +
    "-notation model per attribute combination. Click to zoom, or " +
    "open in a browser tab at full resolution.</p>" +
    (any ? '<div class="gallery" id="gal"></div>'
         : '<div class="noimg">This report was generated without ' +
           "embedded model images.</div>") + "</div>";
  if (!any) return;
  document.getElementById("gal").innerHTML = DATA.cells.map((c, i) => {
    const cap = '<div class="cap"><b>' + esc(c.label) + "</b> · n=" +
      c.cases + " (" + fmtPct(c.coverage * 100) +
      ') · <button class="small ot">open in new tab ⧉</button></div>';
    if (!c.image)
      return '<div class="imgbox"><div class="cap"><b>' + esc(c.label) +
        "</b></div>" + '<div class="noimg">not rendered</div></div>';
    return '<div class="imgbox">' + cap + '<img loading="lazy" src="' +
      c.image + '" data-cap="' + esc(c.label) + '" alt="' +
      esc(c.label) + '"></div>';
  }).join("");
  wireImages(document.getElementById("gal"));
})();

// Wire every image box in `root`: click-to-zoom on the image, and the
// caption's "open in new tab" button (full embedded resolution).
function wireImages(root) {
  root.querySelectorAll(".imgbox").forEach(box => {
    const im = box.querySelector("img");
    if (!im) return;
    im.onclick = () => lightbox(im.src, im.dataset.cap);
    const bt = box.querySelector("button.ot");
    if (bt) bt.onclick = () => openTab(im.src);
  });
}

function openTab(src) {
  // A data: URI cannot be opened as a top-level tab — convert the
  // embedded base64 image to a Blob URL first (works offline). The MIME
  // type is read from the URI so SVG and PNG both open correctly.
  const mime = (src.match(/^data:([^;,]+)/) || [, "image/png"])[1];
  const bin = atob(src.split(",")[1]);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  window.open(URL.createObjectURL(new Blob([bytes], {type: mime})),
              "_blank");
}
function lightbox(src, cap) {
  const lb = document.getElementById("lightbox");
  lb.innerHTML = '<div class="lbcap">' + esc(cap || "") +
    ' — click anywhere to close · <button class="small" id="lb-open">' +
    "open in new tab ⧉</button></div><img src=\"" + src + '">';
  lb.style.display = "block";
  document.getElementById("lb-open").onclick = e => {
    e.stopPropagation();
    openTab(src);
  };
}
document.addEventListener("keydown", e => {
  if (e.key === "Escape")
    document.getElementById("lightbox").style.display = "none";
});

showView((location.hash || "#overview").slice(1) in
  {overview:1, compare:1, activities:1, edges:1, choices:1, models:1}
  ? (location.hash || "#overview").slice(1) : "overview");
</script>
</body>
</html>
"""
