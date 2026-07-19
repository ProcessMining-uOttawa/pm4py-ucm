// The session report — a multi-section self-contained document.
//
// One artifact serves two shapes: the same bundle boots as a Dashboard
// (the app's Dashboards view / a single-dashboard export) or as a Report
// (this), chosen by cfg.mode in the page bootstrap. The report stitches
// together a left table of contents, a reader filter bar that recomputes
// everything, a scorecard landing, the dashboard(s) — rendered by the
// same headless Dashboard the app uses, so a widget looks identical here
// — and a model section with both notations as inline SVG, zoomable and
// pannable offline.
//
// Built client-side (buildSessionReport, below), so it carries the user's
// actual widgets: components.html is one-way, so a server-built report
// would only ever know the seed dashboard. The model SVGs the report
// embeds are the one thing the browser cannot produce, so Python puts
// them in the page config for the report to lift.

import * as E from "./dash-engine.js";
import {
  Dashboard, h, describeFilter, axisLabel, modal, toast, STATE_LABEL,
  scriptJson,
} from "./dash-ui.js";

export class Report {
  constructor(root, cfg) {
    this.root = root;
    this.cfg = cfg;
    this.table = E.decodePayload(cfg.payload);
    this.catalog = cfg.catalog;
    this.catalogMap = Object.fromEntries(cfg.catalog.map((m) => [m.id, m]));
    // One or more named dashboards. A single-dashboard page reports its
    // one dashboard; the shape is a list so nothing needs special-casing.
    this.dashboards = (cfg.dashboards && cfg.dashboards.length)
      ? cfg.dashboards
      : [{ name: cfg.name || "Dashboard", specs: cfg.specs || [] }];
    this.filters = (cfg.filters || []).slice();
    this.modelSvg = cfg.modelSvg || {};
    this.renders = cfg.renders || {};
    // The full family statistics report (a self-contained HTML document
    // from families/report.py), embedded as an <iframe> — the browser
    // cannot compute the family stats, so the backend hands the finished
    // report over the same way it does the model SVG.
    this.familyReport = cfg.familyReport || null;
    this.title = cfg.reportTitle || cfg.name || "Session report";
    this.children = [];      // the headless Dashboard per section

    root.classList.add("pm-dash", "pm-report");
    this._initTheme(cfg.theme);
    this.render();
  }

  // Theme: a report is a downloaded standalone file, so it follows the OS
  // (or a pinned theme) — there is no embedding host to read.
  _initTheme(theme) {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      this.dark = theme ? theme === "dark" : mq.matches;
      const t = this.dark ? "dark" : "light";
      this.root.setAttribute("data-theme", t);
      document.documentElement.setAttribute("data-theme", t);
    };
    apply();
    if (!theme) {
      const on = () => { apply(); this.render(); };
      if (mq.addEventListener) mq.addEventListener("change", on);
      else if (mq.addListener) mq.addListener(on);
    }
  }

  _theme() { return this.dark ? "dark" : "light"; }

  _sections() {
    const secs = [{ id: "scorecard", label: "Scorecard" }];
    this.dashboards.forEach((d, i) =>
      secs.push({ id: `dash-${i}`, label: d.name }));
    if (Object.keys(this.modelSvg).length || Object.keys(this.renders).length) {
      secs.push({ id: "model", label: "Process model" });
    }
    if (this.familyReport) secs.push({ id: "family", label: "Family" });
    return secs;
  }

  render() {
    this.children = [];
    const secs = this._sections();
    this.root.replaceChildren(
      this._toc(secs),
      h("div", { class: "pm-report__main" },
        this._header(),
        this._scorecardSection(),
        ...this.dashboards.map((d, i) => this._dashboardSection(d, i)),
        this._modelSection(),
        this._familySection()),
    );
  }

  _toc(secs) {
    return h("nav", { class: "pm-report__toc" },
      h("div", { class: "pm-report__brand" }, this.title),
      ...secs.map((s) => h("a", {
        class: "pm-report__tocitem", href: `#${s.id}`,
        onclick: (e) => {
          e.preventDefault();
          this.root.querySelector(`#${s.id}`)
            .scrollIntoView({ behavior: "smooth", block: "start" });
        },
      }, s.label)));
  }

  _header() {
    const n = this._liveCount();
    const total = this.table.nCases;
    return h("div", { class: "pm-report__head" },
      h("div", { class: "pm-report__title" }, this.title),
      h("div", { class: "pm-head", style: "border:0;padding:0;position:static" },
        ...this.filters.map((f, i) => h("span", { class: "pm-chip" },
          describeFilter(f, this.table),
          h("button", {
            title: "Remove filter",
            onclick: () => { this.filters.splice(i, 1); this.render(); },
          }, "✕"))),
        h("button", {
          class: "pm-btn pm-btn--ghost", onclick: () => this._openFilter(),
        }, "+ filter"),
        h("span", { class: "pm-count" },
          `${n.toLocaleString("en-US")}${n === total ? "" : " of "
            + total.toLocaleString("en-US")} cases`)));
  }

  _liveCount() {
    const m = E.applyFilters(this.table, this.filters);
    let n = 0;
    for (let i = 0; i < m.length; i++) if (m[i]) n++;
    return n;
  }

  // -- sections ------------------------------------------------------

  _scorecardSection() {
    const specs = this.dashboards.flatMap((d) => d.specs);
    const rows = E.scorecard(specs, this.table, this.catalogMap, this.filters);
    const body = rows.length
      ? this._scorecardTable(rows, specs)
      : h("div", { class: "pm-empty" },
        "No widget across these dashboards has a target.");
    return this._section("scorecard", "Scorecard", body);
  }

  _scorecardTable(rows, specs) {
    return h("table", { class: "pm-score" },
      h("thead", {}, h("tr", {},
        ...["Target", "Goal", "Actual", "Achievement", "Cases", "State"]
          .map((x) => h("th", {}, x)))),
      h("tbody", {}, ...rows.map((r) => {
        const spec = specs.find((s) => s.id === r.id) || {};
        const goal = E.targetGoalValue(spec);
        const perCase = spec.target && spec.target.mode === "per_case";
        const dir = perCase ? ">=" : (spec.target || {}).dir;
        let pct = 0;
        if (r.value != null && goal) pct = dir === ">=" ? r.value / goal : goal / r.value;
        pct = Math.max(0, Math.min(1, pct)) * 100;
        const colour = r.state === "met" ? "var(--positive)"
          : r.state === "risk" ? "var(--warn-bar)" : "var(--garnet)";
        return h("tr", {},
          h("td", {}, r.title),
          h("td", {}, r.goal),
          h("td", {}, r.actual),
          h("td", {}, h("div", { class: "pm-score__ach" },
            h("i", { style: `width:${pct}%;background:${colour}` }))),
          h("td", {}, r.nCases.toLocaleString("en-US")),
          h("td", {}, h("span", { class: `pm-pill pm-pill--${r.state || "met"}` },
            STATE_LABEL[r.state] || "—")));
      })));
  }

  _dashboardSection(d, i) {
    const mount = h("div", {});
    const sec = this._section(`dash-${i}`, d.name, mount);
    // The dashboard is drawn by the very Dashboard the app uses, headless
    // and read-only, sharing the report's filters and bubbling a
    // cell-drill up to the report so it narrows every section at once.
    const child = new Dashboard(mount, {
      table: this.table, payload: this.cfg.payload, catalog: this.catalog,
      specs: d.specs, name: d.name, readOnly: true, headless: true,
      dark: this.dark, renders: this.renders, modelSvg: this.modelSvg,
      filters: this.filters,
      onDrill: (axis, label) => this._drill(axis, label),
    });
    this.children.push(child);
    return sec;
  }

  _drill(axis, label) {
    if (!this.filters.some((f) => f.field === "segment"
        && f.value[0] === axis && f.value[1] === label)) {
      this.filters.push({ field: "segment", op: "is", value: [axis, label] });
    }
    this.render();
  }

  _modelSection() {
    const notations = Object.keys(this.modelSvg).length
      ? Object.keys(this.modelSvg) : Object.keys(this.renders);
    if (!notations.length) return document.createComment("no model");
    let current = notations[0];

    const stage = h("div", { class: "pm-model__stage" });
    const draw = () => {
      stage.replaceChildren();
      const svg = this.modelSvg[current];
      if (svg) {
        const box = h("div", { class: "pm-model__pan" });
        box.innerHTML = svg;
        stage.append(box);
        panZoom(box);
      } else if (this.renders[current]) {
        const box = h("div", { class: "pm-model__pan" },
          h("img", { src: this.renders[current], alt: `${current} model` }));
        stage.append(box);
        panZoom(box);
      }
    };

    const toggle = h("div", { class: "pm-seg" },
      ...notations.map((nn) => h("button", {
        class: "pm-seg__btn" + (nn === current ? " pm-seg__btn--on" : ""),
        onclick: (e) => {
          current = nn;
          toggle.querySelectorAll(".pm-seg__btn").forEach((b) =>
            b.classList.toggle("pm-seg__btn--on", b.textContent === nn.toUpperCase()));
          draw();
        },
      }, nn.toUpperCase())));

    const head = h("div", { class: "pm-model__bar" }, toggle,
      h("span", { class: "pm-model__hint" },
        "scroll to zoom · drag to pan · both notations embedded"));
    const wrap = h("div", {}, head, stage);
    draw();
    return this._section("model", "Process model", wrap);
  }

  _familySection() {
    if (!this.familyReport) return document.createComment("no family");
    // The family report is a complete, self-contained HTML document with
    // its own tables, heatmaps, pairwise comparison and per-cell model
    // SVGs. Rather than re-implement all of that here, embed it whole in a
    // sandboxed <iframe>: full fidelity, and it stays self-contained in
    // the downloaded report. allow-scripts runs its sorting / lightbox;
    // allow-popups keeps "open image in a new tab" working.
    const frame = h("iframe", {
      class: "pm-family__frame",
      // First-party content (our own generated report). allow-same-origin
      // lets its sorting / lightbox work fully; allow-popups keeps "open
      // image in a new tab" working.
      sandbox: "allow-scripts allow-same-origin allow-popups "
        + "allow-downloads allow-popups-to-escape-sandbox",
      title: "Model-family statistics report",
    });
    frame.style.cssText = "width:100%;height:82vh;border:1px solid "
      + "var(--line,#d9d4ca);border-radius:8px;background:#fff;display:block";
    // Set as a property, not an attribute, so the whole HTML document does
    // not have to be attribute-escaped.
    frame.srcdoc = this.familyReport;
    const wrap = h("div", {},
      h("div", { class: "pm-model__hint", style: "margin-bottom:8px" },
        "The full model-family statistics report — sortable tables, "
        + "heatmaps, pairwise process comparison, and per-cell models."),
      frame);
    return this._section("family", "Family", wrap);
  }

  _section(id, label, body) {
    return h("section", { class: "pm-report__section", id },
      h("h2", { class: "pm-report__h2" }, label), body);
  }

  // -- reader filter -------------------------------------------------

  _openFilter() {
    const axes = E.segmentAxes(this.table);
    const axisSel = h("select", { class: "pm-select" },
      ...axes.map((a) => h("option", { value: a.id }, a.label)));
    const valSel = h("select", { class: "pm-select" });
    const fill = () => {
      const { labels } = E.segmentKeys(this.table, axisSel.value);
      valSel.replaceChildren(...labels.map((l) => h("option", { value: l }, l)));
    };
    axisSel.addEventListener("change", fill);
    fill();
    modal({
      theme: this._theme(), title: "Add filter", sub: `→ ${this.title}`,
      body: h("div", {}, h("div", { class: "pm-row" },
        h("span", { class: "pm-row__label" }, "Where"), axisSel,
        h("span", { class: "pm-row__hint" }, "is"), valSel)),
      confirm: "Apply filter",
      onConfirm: () => {
        this.filters.push({ field: "segment", op: "is",
          value: [axisSel.value, valSel.value] });
        this.render();
        return true;
      },
    });
  }
}

/**
 * Wheel-zoom + drag-pan a diagram box, offline and dependency-free.
 *
 * `opts.fit` frames the whole diagram to the box on first layout (scaled
 * down to fit, never up, and centred). The report's big model stage does
 * not need it — a model roughly fills it at 100% — but a small dashboard
 * widget would otherwise open on a corner.
 */
function panZoom(box, opts) {
  const inner = box.firstElementChild;
  if (!inner) return;
  // Size the diagram in REAL pixels inside a scrollable box (not a CSS
  // transform in a clipped box): native scrollbars then appear and track the
  // zoom and the diagram's real extent, so a model taller or wider than the
  // card can be scrolled — the same model the Model view's viewer shows.
  box.style.overflow = "auto";
  inner.style.transform = "none";
  inner.style.transformOrigin = "0 0";
  inner.style.maxWidth = "none";
  inner.style.display = "block";

  // Natural aspect (width / height) from the SVG viewBox or the image, so the
  // px height can be derived from the px width at any zoom.
  let aspect = 0;
  const svg0 = inner.tagName.toLowerCase() === "svg"
    ? inner : (inner.querySelector ? inner.querySelector("svg") : null);
  if (svg0 && svg0.viewBox && svg0.viewBox.baseVal && svg0.viewBox.baseVal.height)
    aspect = svg0.viewBox.baseVal.width / svg0.viewBox.baseVal.height;
  else if (inner.tagName === "IMG" && inner.naturalHeight)
    aspect = inner.naturalWidth / inner.naturalHeight;

  let zoom = 1;
  // At zoom 1 the diagram spans the box width (fit-to-width); the wheel scales
  // that width. A model taller than the box then grows a vertical scrollbar.
  const size = () => {
    const bw = box.clientWidth;
    if (!bw) return false;
    const w = bw * zoom;
    inner.style.width = w + "px";
    inner.style.height = aspect ? (w / aspect) + "px" : "auto";
    return true;
  };

  box.addEventListener("wheel", (e) => {
    e.preventDefault();
    const f = e.deltaY < 0 ? 1.1 : 1 / 1.1;
    const z0 = zoom;
    zoom = Math.min(8, Math.max(0.2, zoom * f));
    // Keep the point under the cursor fixed while the px size changes.
    const r = box.getBoundingClientRect();
    const cx = e.clientX - r.left, cy = e.clientY - r.top;
    const ox = box.scrollLeft + cx, oy = box.scrollTop + cy;
    if (size()) {
      const k = zoom / z0;
      box.scrollLeft = ox * k - cx;
      box.scrollTop = oy * k - cy;
    }
  }, { passive: false });

  // Drag to pan — via native scroll, so it composes with the scrollbars.
  let dragging = false, px = 0, py = 0, moved = 0;
  box.addEventListener("pointerdown", (e) => {
    dragging = true; moved = 0; px = e.clientX; py = e.clientY;
    box.setPointerCapture(e.pointerId); box.style.cursor = "grabbing";
  });
  box.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const dx = e.clientX - px, dy = e.clientY - py;
    moved += Math.abs(dx) + Math.abs(dy);
    box.scrollLeft -= dx; box.scrollTop -= dy;
    px = e.clientX; py = e.clientY;
  });
  const stop = () => { dragging = false; box.style.cursor = "grab"; };
  box.addEventListener("pointerup", stop);
  box.addEventListener("pointerleave", stop);
  // Click a stub to scroll its plug-in panel into view — the decomposed model
  // is stacked in one SVG, so navigating is scrolling that panel to the top.
  // setPointerCapture (drag-start) retargets the click to the box, so resolve
  // the clicked node by coordinates, not e.target.
  box.addEventListener("click", (e) => {
    if (moved > 6) return;
    const hit = document.elementFromPoint(e.clientX, e.clientY) || e.target;
    const a = hit && hit.closest ? hit.closest("a") : null;
    if (!a) return;
    const href = a.getAttribute("xlink:href") || a.getAttribute("href") || "";
    if (!href.startsWith("#pm-map-")) return;
    e.preventDefault();
    const svg = box.querySelector("svg");
    const target = svg && svg.querySelector(href);
    if (!target) return;
    const tr = target.getBoundingClientRect(), br = box.getBoundingClientRect();
    box.scrollTop += (tr.top - br.top) - 10;
  });
  box.style.cursor = "grab";

  // The box is built detached (0 width) and attached by the caller, so size on
  // the next macrotask, with a bounded retry for a late layout pass. setTimeout
  // is used over rAF / ResizeObserver deliberately: those are paused in a
  // backgrounded tab (the model would stay unsized until focus), while
  // setTimeout still fires and layout is computed even while hidden. A second
  // pass 40 ms later re-measures clientWidth after a tall model has grown a
  // vertical scrollbar, so no phantom horizontal bar remains.
  const settle = () => setTimeout(size, 40);
  if (size()) settle();
  else {
    let tries = 0;
    const retry = () => {
      if (size()) settle();
      else if (++tries < 20) setTimeout(retry, 30);
    };
    setTimeout(retry, 0);
  }
  if (inner.tagName === "IMG" && !inner.complete)
    inner.addEventListener("load", () => { size(); settle(); }, { once: true });
}

/**
 * Assemble the session report from the live dashboard, client-side.
 *
 * Same idea as Dashboard.exportHtml: the current page already carries the
 * engine, the renderer, the report code and the fact table, so a copy of
 * it with the config swapped for report mode IS the report — and it
 * carries the user's real widgets, which a server build could not know.
 */
export function buildSessionReport(dash) {
  const doc = document.documentElement.cloneNode(true);
  const root = doc.querySelector("#pm-root");
  if (root) root.replaceChildren();
  doc.querySelectorAll(".pm-toast, .pm-scrim").forEach((e) => e.remove());

  const cfg = JSON.parse(document.getElementById("pm-data").textContent);
  cfg.mode = "report";
  cfg.readOnly = true;
  cfg.dashboards = [{ name: dash.name, specs: dash.specs }];
  cfg.filters = dash.filters;
  cfg.reportTitle = `${dash.name} — session report`;
  const data = doc.querySelector("#pm-data");
  data.textContent = scriptJson(cfg);
  return "<!DOCTYPE html>\n" + doc.outerHTML;
}
