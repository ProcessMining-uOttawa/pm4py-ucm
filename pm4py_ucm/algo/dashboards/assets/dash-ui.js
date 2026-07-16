// Dashboard view — rendering, composer, interactions.
//
// Renders from what dash-engine.js computes. The engine returns data
// (values, states, labels, counts); everything visual happens here. That
// split is why the app and the export look identical: they share one
// computation and one renderer, not two of each.
//
// Loaded by the Streamlit island and by the self-contained HTML export.
// The export sets `readOnly` so the reader can filter and drill down but
// not restructure someone else's dashboard.
//
// State lives in the browser. `components.html` is one-way, so specs
// persist to localStorage keyed by the log, and travel in and out as
// JSON. See view.py.

import * as E from "./dash-engine.js";

const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
};

const STATE_LABEL = { met: "MET", risk: "AT RISK", missed: "MISSED" };

//: Most bars a categorical axis draws before the renderer keeps only the
//: largest and says so. Sized so each bar stays wide enough to hover in
//: a 2-column card.
const BAR_CAP = 24;

export class Dashboard {
  /**
   * @param {HTMLElement} root
   * @param {object} opts  {payload, catalog, specs, name, readOnly,
   *                        storageKey, renders}
   */
  constructor(root, opts) {
    this.root = root;
    this.opts = opts;
    this.table = E.decodePayload(opts.payload);
    this.catalog = Object.fromEntries(opts.catalog.map((m) => [m.id, m]));
    this.catalogList = opts.catalog;
    this.name = opts.name || "Dashboard";
    this.readOnly = !!opts.readOnly;
    this.renders = opts.renders || {};

    this.specs = this._load(opts.specs);
    // An export carries the filters it was taken under, so it opens on
    // the question it was sent to answer.
    this.filters = (opts.filters || []).slice();
    this.notation = Object.keys(this.renders)[0] || "ucm";

    root.classList.add("pm-dash");
    this._initTheme(opts.theme);
    this._applyPendingPin(opts.pendingPin);
    this.render();
  }

  /**
   * Add a widget the host asked for — "Pin to dashboard" on the Model
   * view.
   *
   * The host cannot reach in and do this itself: `components.html` is
   * one-way, and the widgets live in this browser. So the host puts the
   * request in the config and reruns, and the island applies it here.
   *
   * That makes the request arrive on EVERY rerun, not once, so it has to
   * be idempotent — the applied pin ids are remembered, and the host
   * mints a fresh id per click. Without that, one pin would breed a new
   * widget on every interaction with the page.
   */
  _applyPendingPin(pin) {
    if (!pin || !pin.id || this.readOnly) return;
    const key = `${this._key()}:pins`;
    let applied = [];
    try {
      applied = JSON.parse(localStorage.getItem(key) || "[]");
    } catch (e) { /* a corrupt store just means we might re-pin once */ }
    if (applied.includes(pin.id)) return;

    this.specs.push(pin.spec);
    this._save();
    try {
      // Keep only the recent ones: this list is unbounded otherwise, and
      // only the last few pins can still be in flight.
      localStorage.setItem(key, JSON.stringify(applied.concat(pin.id).slice(-20)));
    } catch (e) {
      console.warn("dashboard: could not record the pin", e);
    }
    this._pinned = pin.spec.title;
  }

  // -- theme ---------------------------------------------------------

  /**
   * Resolve light/dark.
   *
   * `opts.theme` is the host telling us — a Streamlit app can be forced
   * to dark on a light OS, so the host's answer has to beat the OS in
   * both directions. Absent a host (the standalone export), follow the
   * OS and keep following it: a reader who flips their system theme with
   * the file open should not be left staring at the wrong one.
   *
   * The CSS resolves the same way on its own; this only mirrors the
   * answer into `data-theme` (so the attribute can win over the media
   * query) and into `this.dark`, which the JS-computed heat ramp needs.
   */
  _initTheme(theme) {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      this.dark = theme ? theme === "dark" : mq.matches;
      this.root.setAttribute("data-theme", this.dark ? "dark" : "light");
    };
    apply();
    if (!theme) {
      const onChange = () => { apply(); this.render(); };
      // addEventListener on a MediaQueryList is unsupported on old
      // Safari; the export may well be opened there.
      if (mq.addEventListener) mq.addEventListener("change", onChange);
      else if (mq.addListener) mq.addListener(onChange);
    }
  }

  /** The resolved theme, for surfaces that render outside the root. */
  _theme() { return this.dark ? "dark" : "light"; }

  // -- persistence ---------------------------------------------------
  // localStorage, not the server: components.html cannot send state back
  // to Python, and the export has no server at all. Same code, so a
  // dashboard built in the app survives into the exported file via the
  // specs baked into view.py's payload.

  _key() { return `pm4py-ucm:dash:${this.opts.storageKey || this.table.logName}`; }

  _load(fallback) {
    if (this.readOnly) return (fallback || []).slice();
    try {
      const raw = localStorage.getItem(this._key());
      if (raw) return JSON.parse(raw);
    } catch (e) {
      // A corrupt or blocked store must not take the whole view down.
      console.warn("dashboard: could not read saved widgets", e);
    }
    return (fallback || []).slice();
  }

  _save() {
    if (this.readOnly) return;
    try {
      localStorage.setItem(this._key(), JSON.stringify(this.specs));
    } catch (e) {
      console.warn("dashboard: could not save widgets", e);
    }
  }

  exportSpecs() { return JSON.parse(JSON.stringify(this.specs)); }

  /**
   * This page, as a standalone read-only export.
   *
   * The page serialises *itself* rather than asking the server to build
   * an export. That is not a trick — it falls out of the architecture.
   * The document already carries the engine, the renderer and the whole
   * fact table with no external reference, so a copy of it with the
   * config swapped IS the export. It also fixes a problem the server
   * cannot solve: the widgets being exported live in this browser, and
   * `components.html` is one-way, so Python does not know what the user
   * built. An export produced server-side would confidently ship the
   * *default* dashboard instead of theirs.
   */
  exportHtml() {
    const doc = document.documentElement.cloneNode(true);

    // #pm-root re-renders from the config on load, and transient UI
    // (an open modal, a fading toast) must not be frozen into the file.
    const root = doc.querySelector("#pm-root");
    if (root) root.replaceChildren();
    doc.querySelectorAll(".pm-toast, .pm-scrim").forEach((e) => e.remove());

    const cfg = JSON.parse(
      document.getElementById("pm-data").textContent);
    cfg.specs = this.specs;
    cfg.readOnly = true;
    // Ship the reader the view as filtered, so an export sent to answer
    // a specific question opens on that question rather than on the
    // unfiltered log.
    cfg.filters = this.filters;
    const data = doc.querySelector("#pm-data");
    // Same escape the Python side applies: a `</script>` inside the data
    // would end the element and truncate the file.
    data.textContent = JSON.stringify(cfg)
      .replace(/<\//g, "<\\/").replace(/<!--/g, "<\\!--");

    return "<!DOCTYPE html>\n" + doc.outerHTML;
  }

  downloadExport() {
    const blob = new Blob([this.exportHtml()], { type: "text/html" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${(this.name || "dashboard").replace(/[^\w.-]+/g, "_")}.html`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast("Exported — interactive, offline, no server needed", this._theme());
  }

  // -- computation ---------------------------------------------------

  _compute(spec) {
    try {
      return E.computeWidget(spec, this.table, this.catalog, this.filters);
    } catch (err) {
      return { id: spec.id, title: spec.title, viz: spec.viz,
               error: String(err && err.message || err) };
    }
  }

  _liveCount() {
    const m = E.applyFilters(this.table, this.filters);
    let n = 0;
    for (let i = 0; i < m.length; i++) if (m[i]) n++;
    return n;
  }

  // -- render --------------------------------------------------------

  render() {
    const computed = this.specs.map((s) => this._compute(s));
    this.root.replaceChildren(
      this._header(),
      this._targets(computed),
      this._grid(computed),
    );
    if (this._pinned) {
      toast(`Pinned “${this._pinned}”`, this._theme());
      this._pinned = null;
    }
  }

  _header() {
    const n = this._liveCount();
    const total = this.table.nCases;
    const kids = [
      h("span", { class: "pm-title" }, this.name),
      ...this.filters.map((f, i) =>
        h("span", { class: "pm-chip" },
          describeFilter(f, this.table),
          h("button", {
            title: "Remove filter",
            onclick: () => { this.filters.splice(i, 1); this.render(); },
          }, "✕"))),
      // Filtering stays available to a reader: recomputing every widget
      // against their own question is the whole point of shipping the
      // engine with the export. read-only means "cannot restructure
      // someone else's dashboard", not "cannot explore it" — and the
      // drill-down on table cells filters regardless, so hiding this
      // would only make the same capability harder to find.
      h("button", {
        class: "pm-btn pm-btn--ghost",
        onclick: () => this._openFilter(),
      }, "+ filter"),
    ];

    kids.push(h("span", { class: "pm-count" },
      `${n.toLocaleString("en-US")}${n === total ? "" : " of " +
        total.toLocaleString("en-US")} cases in`));

    if (this.table.sampled) {
      kids.push(h("span", {
        class: "pm-sampled",
        title: `The log was too large to send to the browser whole, so ` +
               `these numbers are estimated from a random sample of ` +
               `${this.table.nCases.toLocaleString("en-US")} of ` +
               `${this.table.sampledFrom.toLocaleString("en-US")} cases.`,
      }, `sample of ${this.table.sampledFrom.toLocaleString("en-US")}`));
    }
    if (this.table.droppedEvents) {
      kids.push(h("span", {
        class: "pm-sampled",
        title: "Events whose timestamp could not be parsed were excluded.",
      }, `${this.table.droppedEvents.toLocaleString("en-US")} events dropped`));
    }

    if (Object.keys(this.renders).length > 1) {
      kids.push(h("select", {
        class: "pm-select",
        onchange: (e) => { this.notation = e.target.value; this.render(); },
      }, ...Object.keys(this.renders).map((k) =>
        h("option", { value: k, selected: k === this.notation },
          k.toUpperCase()))));
    }
    if (!this.readOnly) {
      kids.push(h("button", {
        class: "pm-btn pm-btn--ghost",
        title: "Save this dashboard as a standalone interactive HTML "
             + "file — it works offline, with no server.",
        onclick: () => this.downloadExport(),
      }, "⬇ Export"));
      kids.push(h("button", {
        class: "pm-btn", onclick: () => this._openComposer(),
      }, "+ Add widget"));
    }
    return h("div", { class: "pm-head" }, ...kids);
  }

  _targets(computed) {
    const targeted = computed.filter((w, i) =>
      this.specs[i].target && this.specs[i].target.on && !w.error);
    if (!targeted.length) return document.createComment("no targets");

    const count = (s) => targeted.filter((w) => w.state === s).length;
    // Name the worst offender: a strip that only counts states makes the
    // reader hunt for the one that matters.
    const worst = targeted.find((w) => w.state === "missed")
      || targeted.find((w) => w.state === "risk");

    return h("div", { class: "pm-targets" },
      h("span", { class: "pm-targets__label" }, "Targets"),
      h("span", { class: "pm-pill pm-pill--met" }, `${count("met")} met`),
      h("span", { class: "pm-pill pm-pill--risk" }, `${count("risk")} at risk`),
      h("span", { class: "pm-pill pm-pill--missed" }, `${count("missed")} missed`),
      worst && h("span", { class: "pm-targets__worst" },
        `worst: ${worst.title} (${worst.text})`),
      h("button", { class: "pm-link", onclick: () => this._openScorecard() },
        "open scorecard →"),
    );
  }

  _grid(computed) {
    if (!this.specs.length) {
      return h("div", { class: "pm-grid" },
        h("div", { class: "pm-empty" },
          this.readOnly ? "This dashboard has no widgets."
            : "No widgets yet — use “+ Add widget” to build one."));
    }
    return h("div", { class: "pm-grid" },
      ...computed.map((w, i) => this._widget(w, this.specs[i], i)));
  }

  _widget(w, spec, i) {
    const wide = w.viz === "table" && (w.cols || []).length > 5;
    const card = h("div", {
      class: `pm-w pm-w--${w.viz}${wide ? " pm-w--wide" : ""}`,
    });

    const tools = h("div", { class: "pm-w__tools" });
    if (w.viz === "table" && !w.error) {
      tools.append(h("button", {
        class: "pm-w__tool",
        title: spec.statusColors ? "Colour by value" : "Colour by target status",
        onclick: () => {
          spec.statusColors = !spec.statusColors;
          this._save(); this.render();
        },
      }, spec.statusColors ? "status" : "value"));
      tools.append(h("button", {
        class: "pm-w__tool", title: "Swap axes",
        onclick: () => {
          const s = spec.segment || {};
          spec.segment = { rows: s.cols, cols: s.rows };
          this._save(); this.render();
        },
      }, "⇄"));
      tools.append(h("button", {
        class: "pm-w__tool", title: "Download CSV",
        onclick: () => downloadCsv(w),
      }, "⬇"));
    }
    if (!this.readOnly) {
      tools.append(h("button", {
        class: "pm-w__tool", title: "Remove widget",
        onclick: () => {
          this.specs.splice(i, 1); this._save(); this.render();
        },
      }, "✕"));
    }

    // A widget with its own filter is measuring a different population
    // from the one the header advertises; say so on the card, or its
    // lower numbers read as a bug.
    const own = (spec.filter || []).length;
    card.append(h("div", { class: "pm-w__head" },
      h("div", { class: "pm-w__title", title: w.title }, w.title),
      own > 0 && h("span", {
        class: "pm-w__badge pm-w__badge--filter",
        title: "This widget has its own filter:\n"
             + spec.filter.map((f) => "· " + describeFilter(f, this.table))
                 .join("\n"),
      }, own === 1 ? "FILTERED" : `${own} FILTERS`),
      w.state && h("span", { class: `pm-w__badge pm-w__badge--${w.state}` },
        STATE_LABEL[w.state]),
      tools));

    const body = h("div", { class: "pm-w__body" });
    if (w.error) {
      body.append(h("div", { class: "pm-preview__err" }, w.error));
    } else if (w.viz === "model") {
      body.append(this._modelBody(w));
    } else if (w.viz === "table") {
      body.append(this._tableBody(w, spec));
    } else if (w.series) {
      body.append(...this._barBody(w));
    } else {
      body.append(...this._kpiBody(w, spec));
    }
    card.append(body);
    return card;
  }

  _kpiBody(w, spec) {
    const out = [
      h("div", { class: "pm-kpi__value" }, w.text),
      h("div", { class: "pm-kpi__sub" }, w.sub || ""),
    ];
    if (w.distribution) {
      const d = w.distribution;
      out.push(h("div", { class: "pm-kpi__dist", title:
        `met ${d.met.toFixed(1)}% · at risk ${d.risk.toFixed(1)}% · ` +
        `missed ${d.missed.toFixed(1)}%` },
        h("i", { class: "met", style: `width:${d.met}%` }),
        h("i", { class: "risk", style: `width:${d.risk}%` }),
        h("i", { class: "missed", style: `width:${d.missed}%` })));
    } else if (spec.target && spec.target.on && w.value != null) {
      // Progress toward the goal, clamped: an 800%-of-target bar tells
      // the reader nothing a full bar does not.
      const goal = +spec.target.value;
      const frac = spec.target.dir === ">="
        ? (goal ? w.value / goal : 0)
        : (w.value ? goal / w.value : 1);
      const pct = Math.max(0, Math.min(1, frac)) * 100;
      const colour = w.state === "met" ? "var(--positive)"
        : w.state === "risk" ? "var(--warn-bar)" : "var(--garnet)";
      out.push(h("div", { class: "pm-kpi__bar" },
        h("i", { style: `width:${pct}%;background:${colour}` })));
    }
    return out;
  }

  _barBody(w) {
    const all = w.series;
    if (!all.length) return [h("div", { class: "pm-kpi__sub" }, "No data.")];

    // A categorical axis can have hundreds of segments — ClaimsPaymentLog
    // has 164 variants — and 164 two-pixel bars in a 350px card say
    // nothing. Show the largest and SAY how many were left out; silently
    // truncating would read as "this is all of them".
    //
    // Time axes are exempt: their order carries the meaning, so a dense
    // series is legible where a dense bar chart is not, and reordering by
    // value would destroy it.
    const isTime = E.TIME_AXES.includes(w.axis);
    let pts = all, dropped = 0;
    if (!isTime && all.length > BAR_CAP) {
      pts = all.slice()
        .sort((a, b) => (b.value ?? -Infinity) - (a.value ?? -Infinity))
        .slice(0, BAR_CAP);
      dropped = all.length - pts.length;
    }

    const vals = pts.map((p) => p.value).filter((v) => v != null);
    const max = Math.max(1, ...vals);
    const bars = h("div", { class: "pm-bars" }, ...pts.map((p) => {
      const u = p.value == null ? 0 : p.value / max;
      const bg = p.state ? `var(--${p.state}-fg)` : E.heat(u, this.dark).bg;
      return h("i", {
        style: `height:${Math.round(8 + u * 92)}%;background:${bg}`,
        title: `${p.label}: ${p.text || E.fmt(p.value, w.unit)}` +
               (p.nCases != null ? ` (${p.nCases.toLocaleString("en-US")} cases)` : ""),
      });
    }));
    const mid = dropped
      ? `top ${pts.length} of ${all.length} ${axisLabel(w.axis)} by value`
      : `${pts.length} × ${axisLabel(w.axis) || "points"} · hover for values`;
    const axis = h("div", { class: "pm-axis" },
      h("span", {}, pts[0].label),
      pts.length > 2 && h("span", { class: "pm-axis__mid", title: dropped
        ? `${all.length} segments in total; the ${dropped} smallest are `
          + `not drawn. The widget's target state still considers all of them.`
        : "" }, mid),
      pts.length > 1 && h("span", {}, pts[pts.length - 1].label));
    return [bars, axis];
  }

  _tableBody(w, spec) {
    const flat = w.cells.flat().map((c) => c.value).filter((v) => v != null);
    const lo = Math.min(...flat), hi = Math.max(...flat);
    const grid = h("div", {
      class: "pm-table",
      style: `grid-template-columns: max-content repeat(${w.cols.length}, minmax(62px, 1fr))`,
    });

    grid.append(h("div", { class: "pm-table__cell pm-table__cell--head" },
      axisLabel(w.rowsAxis)));
    w.cols.forEach((c) => grid.append(
      h("div", {
        class: "pm-table__cell pm-table__cell--head",
        title: `Filter to ${c}`,
        onclick: () => this._drill(w.colsAxis, c),
      }, c)));

    w.rows.forEach((r, ri) => {
      grid.append(h("div", {
        class: "pm-table__cell pm-table__cell--row",
        title: `Filter to ${r}`,
        onclick: () => this._drill(w.rowsAxis, r),
      }, r));
      w.cells[ri].forEach((cell, ci) => {
        if (cell.value == null) {
          grid.append(h("div", {
            class: "pm-table__cell pm-table__cell--empty",
            title: "No cases in this segment",
          }, "—"));
          return;
        }
        let style = "";
        let text = cell.text;
        if (spec.statusColors && cell.state) {
          // Status mode carries a glyph as well as a colour: colour alone
          // is not a signal everyone can read.
          const suffix = { met: " ✓", risk: " !", missed: " ✕" }[cell.state];
          text += suffix;
          style = `background:var(--${cell.state}-bg);color:var(--${cell.state}-fg)`;
        } else {
          const u = hi > lo ? (cell.value - lo) / (hi - lo) : 0;
          const c = E.heat(u * 0.85, this.dark);
          style = `background:${c.bg};color:${c.fg}`;
        }
        grid.append(h("div", {
          class: "pm-table__cell pm-table__cell--data",
          style,
          title: `${w.rows[ri]} · ${w.cols[ci]}: ${cell.text}` +
                 ` (${cell.nCases.toLocaleString("en-US")} cases)` +
                 `\nClick to filter to this segment`,
          onclick: () => {
            this._drill(w.rowsAxis, w.rows[ri]);
            this._drill(w.colsAxis, w.cols[ci], true);
          },
        }, text));
      });
    });
    return h("div", { class: "pm-tablewrap" }, grid);
  }

  _modelBody(w) {
    const src = this.renders[this.notation];
    const box = h("div", { class: "pm-model" });
    if (src) box.append(h("img", { src, alt: `Mined ${this.notation} model` }));
    else box.append(h("span", { class: "pm-model__cap" },
      `${this.notation.toUpperCase()} render not available`));
    return box;
  }

  // -- interactions ---------------------------------------------------

  _drill(axis, label, quiet) {
    if (!axis || axis === "none") return;
    const exists = this.filters.some((f) =>
      f.field === "segment" && f.value[0] === axis && f.value[1] === label);
    if (!exists) {
      this.filters.push({ field: "segment", op: "is", value: [axis, label] });
    }
    if (!quiet) { this.render(); toast(`Filtered to ${label}`, this._theme()); }
    else this.render();
  }

  /**
   * An axis/value picker, shared by the dashboard filter bar and the
   * composer's per-widget FILTER row.
   *
   * Both produce the same `segment` filter the engine already
   * understands, and the same one a table-cell drill-down produces — so
   * a filter arrived at by clicking and a filter built by hand are the
   * same object, and neither needs its own code path.
   */
  _filterPicker() {
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
    return {
      axisSel, valSel,
      get: () => ({ field: "segment", op: "is",
                    value: [axisSel.value, valSel.value] }),
    };
  }

  _openFilter() {
    const p = this._filterPicker();
    const body = h("div", {}, h("div", { class: "pm-row" },
      h("span", { class: "pm-row__label" }, "Where"), p.axisSel,
      h("span", { class: "pm-row__hint" }, "is"), p.valSel));

    modal({
      theme: this._theme(),
      title: "Add filter", sub: `→ ${this.name}`, body,
      confirm: "Apply filter",
      onConfirm: () => {
        this.filters.push(p.get());
        this.render();
        toast("Filter applied", this._theme());
        return true;
      },
    });
  }

  _openScorecard() {
    const rows = E.scorecard(this.specs, this.table, this.catalog, this.filters);
    const body = h("div", {});
    if (!rows.length) {
      body.append(h("div", { class: "pm-empty" },
        "No widget on this dashboard has a target."));
    } else {
      const t = h("table", { class: "pm-score" },
        h("thead", {}, h("tr", {},
          ...["Target", "Goal", "Actual", "Achievement", "Cases", "State"]
            .map((x) => h("th", {}, x)))),
        h("tbody", {}, ...rows.map((r) => {
          const spec = this.specs.find((s) => s.id === r.id) || {};
          // In per_case mode the value is a share, so the bar must
          // measure against the share goal, not the per-case threshold.
          const goal = E.targetGoalValue(spec);
          const perCase = spec.target && spec.target.mode === "per_case";
          const dir = perCase ? ">=" : (spec.target || {}).dir;
          let pct = 0;
          if (r.value != null && goal) {
            pct = dir === ">=" ? r.value / goal : goal / r.value;
          }
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
            h("td", {}, h("span", {
              class: `pm-pill pm-pill--${r.state || "met"}`,
            }, STATE_LABEL[r.state] || "—")));
        })));
      body.append(t);
    }
    modal({ theme: this._theme(), title: "Scorecard", sub: `→ ${this.name}`, body,
            confirm: null, cancel: "Close" });
  }

  // -- composer -------------------------------------------------------

  _openComposer() {
    const spec = {
      id: `w${Date.now().toString(36)}`,
      metric: "duration", params: {}, agg: "avg",
      filter: [], segment: {}, viz: "kpi", statusColors: false,
      target: { on: false, dir: "<=", value: 14, warn: 18,
                mode: "aggregate" },
    };

    const body = h("div", {});
    const preview = h("div", { class: "pm-preview" });
    const rows = {};

    const metricSel = h("select", { class: "pm-select pm-metric" });
    for (const level of ["process", "activity", "edge"]) {
      const group = h("optgroup", { label: level.toUpperCase() });
      for (const m of this.catalogList.filter((x) => x.level === level)) {
        group.append(h("option", {
          value: m.id, disabled: !m.available,
          title: m.available ? m.help : m.unavailableReason,
        }, `${m.label}${m.available ? "" : " — unavailable"}`));
      }
      metricSel.append(group);
    }

    const aggSel = h("select", { class: "pm-select" });
    const paramsBox = h("div", {
      style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap;flex:1",
    });
    const helpNote = h("div", { class: "pm-row__note" });

    // Per-widget filter. The dashboard's own filters stack ON TOP of
    // these at compute time, so this row narrows one widget without
    // touching the rest of the board — "everything, but this card is
    // just the appeals".
    const fPick = this._filterPicker();
    const fChips = h("div", {
      style: "display:flex;gap:6px;flex-wrap:wrap;align-items:center",
    });
    const fAdd = h("button", {
      class: "pm-btn pm-btn--ghost", type: "button",
      onclick: () => {
        spec.filter.push(fPick.get());
        drawChips();
        refresh();
      },
    }, "+ add");
    const drawChips = () => {
      fChips.replaceChildren(...spec.filter.map((f, i) =>
        h("span", { class: "pm-chip" }, describeFilter(f, this.table),
          h("button", {
            type: "button", title: "Remove",
            onclick: () => { spec.filter.splice(i, 1); drawChips(); refresh(); },
          }, "✕"))));
    };

    const rowsSel = h("select", { class: "pm-select" });
    const colsSel = h("select", { class: "pm-select" });
    const axes = E.segmentAxes(this.table);
    for (const sel of [rowsSel, colsSel]) {
      sel.append(h("option", { value: "" }, "(none)"));
      for (const a of axes) sel.append(h("option", { value: a.id }, a.label));
    }
    const vizNote = h("span", { class: "pm-row__hint" });

    const targetToggle = h("button", {
      class: "pm-toggle", "aria-pressed": "false", type: "button",
    });
    const dirSel = h("select", { class: "pm-select" },
      h("option", { value: "<=" }, "≤ at most"),
      h("option", { value: ">=" }, "≥ at least"));
    const valInput = h("input", {
      class: "pm-input", type: "number", value: "14",
      style: "width:80px",
    });
    const warnInput = h("input", {
      class: "pm-input", type: "number", value: "18",
      style: "width:80px",
    });
    const modeSel = h("select", { class: "pm-select" },
      h("option", { value: "aggregate" }, "aggregate"),
      h("option", { value: "per_case" }, "per case"));
    const shareInput = h("input", {
      class: "pm-input", type: "number", value: "90",
      style: "width:70px", title: "Goal: share of cases that must meet it",
    });
    const targetNote = h("span", { class: "pm-row__hint" });

    // -- wiring ------------------------------------------------------

    const metric = () => this.catalog[metricSel.value];

    const rebuildParams = () => {
      const m = metric();
      paramsBox.replaceChildren();
      spec.params = {};
      // Seed from the log's dominant start/end activities, so the
      // composer opens on a real measurement instead of an empty one.
      const ends = E.commonEndpoints(this.table);
      for (const p of m.params) {
        const sel = h("select", {
          class: "pm-select",
          onchange: (e) => { spec.params[p.name] = e.target.value; refresh(); },
        });
        const options = p.kind === "activity" ? this.table.activities : [];
        options.forEach((o) => sel.append(h("option", { value: o }, o)));
        const preferred = p.name === "to" ? ends.to : ends.from;
        const value = options.includes(preferred) ? preferred : options[0];
        sel.value = value;
        spec.params[p.name] = value;
        paramsBox.append(h("span", { class: "pm-row__hint" }, p.label), sel);
      }
      if (!m.params.length) {
        paramsBox.append(h("span", { class: "pm-row__hint" },
          "This metric takes no parameters."));
      }
      rows.params.style.display = m.params.length ? "" : "none";
    };

    const rebuildAggs = () => {
      const m = metric();
      aggSel.replaceChildren(...m.aggs.map((a) =>
        h("option", { value: a, selected: a === m.defaultAgg }, a)));
      aggSel.disabled = m.aggs.length < 2;
      spec.agg = m.defaultAgg;
    };

    const syncViz = () => {
      spec.segment = { rows: rowsSel.value || undefined,
                       cols: colsSel.value || undefined };
      const n = (rowsSel.value ? 1 : 0) + (colsSel.value ? 1 : 0);
      spec.viz = n === 0 ? "kpi" : n === 1 ? "bar" : "table";
      spec.statusColors = n === 2 && spec.target.on;
      vizNote.textContent = n === 0
        ? "no axes → KPI card"
        : n === 1 ? "one axis → bar chart"
          : "two axes → heatmap table";
    };

    const syncTarget = () => {
      const on = spec.target.on;
      targetToggle.setAttribute("aria-pressed", String(on));
      for (const el of [dirSel, valInput, warnInput, modeSel]) el.disabled = !on;
      const perCase = modeSel.value === "per_case";
      shareInput.style.display = perCase && on ? "" : "none";
      shareLabel.style.display = perCase && on ? "" : "none";
      spec.target.mode = modeSel.value;
      spec.target.dir = dirSel.value;
      spec.target.value = Number(valInput.value);
      spec.target.warn = Number(warnInput.value);
      spec.target.shareGoal = perCase ? Number(shareInput.value) : undefined;
      const segmented = rowsSel.value || colsSel.value;
      targetNote.textContent = !on ? ""
        : perCase
          ? "each case scored; the widget reports the share that met"
          : segmented ? "every segment scored; the worst state rolls up"
            : "the aggregate value is scored";
    };

    const refresh = () => {
      syncViz();
      syncTarget();
      spec.metric = metricSel.value;
      spec.agg = aggSel.value;
      spec.title = defaultTitle(spec, metric());

      // Live preview computes the real widget over the real cases — the
      // log is already in the browser, so there is nothing to sample and
      // no reason to show an approximation.
      preview.replaceChildren(
        h("span", { class: "pm-preview__label" }, "Live preview"));
      const w = this._compute(spec);
      if (w.error) {
        preview.append(h("span", { class: "pm-preview__err" }, w.error));
        return;
      }
      if (w.viz === "kpi") {
        preview.append(h("span", { class: "pm-preview__value" }, w.text));
      }
      const shape = w.viz === "kpi" ? "a KPI card"
        : w.viz === "bar" ? `a bar chart of ${(w.series || []).length} segments`
          : `a ${w.rows.length} × ${w.cols.length} heatmap table`;
      preview.append(h("span", { class: "pm-preview__note" },
        `Adds as ${shape} over ${w.nCases.toLocaleString("en-US")} ` +
        `filtered case${w.nCases === 1 ? "" : "s"}.` +
        (w.state ? ` Currently ${STATE_LABEL[w.state]}.` : "")));
      const m = metric();
      helpNote.textContent = [m.help, m.weightingNote].filter(Boolean).join(" ");
    };

    metricSel.addEventListener("change", () => {
      rebuildParams(); rebuildAggs(); refresh();
    });
    aggSel.addEventListener("change", refresh);
    rowsSel.addEventListener("change", refresh);
    colsSel.addEventListener("change", refresh);
    targetToggle.addEventListener("click", () => {
      spec.target.on = !spec.target.on; refresh();
    });
    for (const el of [dirSel, valInput, warnInput, modeSel, shareInput]) {
      el.addEventListener("input", refresh);
      el.addEventListener("change", refresh);
    }

    const shareLabel = h("span", { class: "pm-row__hint" }, "goal ≥");

    rows.metric = h("div", { class: "pm-row" },
      h("span", { class: "pm-row__label" }, "Metric"), metricSel, aggSel);
    rows.params = h("div", { class: "pm-row" },
      h("span", { class: "pm-row__label" }, "Params"), paramsBox);
    rows.filter = h("div", { class: "pm-row" },
      h("span", { class: "pm-row__label" }, "Filter"),
      fPick.axisSel, h("span", { class: "pm-row__hint" }, "is"),
      fPick.valSel, fAdd, fChips,
      h("span", { class: "pm-row__note" },
        "Applies to this widget only. The dashboard's filters stack on "
        + "top of it."));
    rows.segment = h("div", { class: "pm-row" },
      h("span", { class: "pm-row__label" }, "Segment"),
      h("span", { class: "pm-row__hint" }, "rows"), rowsSel,
      h("span", { class: "pm-row__hint" }, "cols"), colsSel, vizNote);
    rows.target = h("div", { class: "pm-row" },
      h("span", { class: "pm-row__label" }, "Target"),
      targetToggle, dirSel, valInput,
      h("span", { class: "pm-row__hint" }, "warn at"), warnInput,
      modeSel, shareLabel, shareInput, targetNote);

    body.append(rows.metric, rows.params, rows.filter, rows.segment,
                rows.target, helpNote, preview);

    rebuildParams(); rebuildAggs(); drawChips(); refresh();

    modal({
      theme: this._theme(),
      title: "New widget", sub: `→ ${this.name}`, body,
      confirm: "Add to dashboard",
      onConfirm: () => {
        if (!spec.target.on) delete spec.target;
        this.specs.push(JSON.parse(JSON.stringify(spec)));
        this._save();
        this.render();
        toast("Widget added", this._theme());
        return true;
      },
    });
  }
}

// ---------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------

function defaultTitle(spec, metric) {
  const p = spec.params || {};
  const parts = [];
  if (metric.aggs.length > 1 && spec.agg !== "share") parts.push(spec.agg);
  if (p.from && p.to) parts.push(`${p.from} → ${p.to}`);
  else if (p.activity) parts.push(p.activity);
  else parts.push(metric.label.toLowerCase());
  if (parts.length === 1) parts[0] = metric.label;
  return parts.join(" ");
}

function axisLabel(axis) {
  if (!axis) return "";
  if (axis.startsWith("attr:")) return axis.slice(5).replace(/^case:/, "");
  return axis[0].toUpperCase() + axis.slice(1);
}

function describeFilter(f, table) {
  if (f.field === "segment") return `${axisLabel(f.value[0])} = ${f.value[1]}`;
  if (f.field === "contains") {
    return `${f.op === "not" ? "excludes" : "contains"} ${f.value}`;
  }
  if (f.field === "date") return `${f.value[0] || "…"} → ${f.value[1] || "…"}`;
  if (f.field === "resource") return `resource = ${f.value}`;
  if (f.field.startsWith("attr:")) {
    return `${f.field.slice(5).replace(/^case:/, "")} ${f.op} ${f.value}`;
  }
  return f.field;
}

function downloadCsv(w) {
  const esc = (s) => `"${String(s).replace(/"/g, '""')}"`;
  const lines = [[axisLabel(w.rowsAxis), ...w.cols].map(esc).join(",")];
  w.rows.forEach((r, i) => {
    lines.push([esc(r), ...w.cells[i].map((c) =>
      c.value == null ? "" : c.value)].join(","));
  });
  const blob = new Blob([lines.join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${w.title.replace(/[^\w.-]+/g, "_")}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

let toastTimer = null;
export function toast(message, theme) {
  let el = document.querySelector(".pm-toast");
  if (!el) {
    // Also outside the dashboard root, so it needs the theme told to it
    // — and it is the one surface that must *invert* the page.
    el = h("div", { class: "pm-toast pm-dash" });
    document.body.append(el);
  }
  if (theme) el.setAttribute("data-theme", theme);
  el.textContent = message;
  el.classList.add("pm-toast--on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("pm-toast--on"), 2600);
}

export function modal({ title, sub, body, confirm, cancel, onConfirm, theme }) {
  const close = () => {
    scrim.remove();
    document.removeEventListener("keydown", onKey);
  };
  const onKey = (e) => { if (e.key === "Escape") close(); };

  const foot = h("div", { class: "pm-modal__foot" },
    h("button", { class: "pm-btn pm-btn--ghost", onclick: close },
      cancel || "Cancel"),
    confirm && h("button", {
      class: "pm-btn pm-btn--solid",
      onclick: () => { if (onConfirm() !== false) close(); },
    }, confirm));

  const panel = h("div", { class: "pm-modal" },
    h("div", { class: "pm-modal__head" },
      h("span", { class: "pm-modal__title" }, title),
      sub && h("span", { class: "pm-modal__sub" }, sub),
      h("button", { class: "pm-modal__close", onclick: close }, "✕")),
    h("div", { class: "pm-modal__body" }, body),
    foot);

  const scrim = h("div", {
    class: "pm-scrim pm-dash",
    // The scrim is appended to document.body, outside the dashboard
    // root, so it inherits none of the root's data-theme — a modal would
    // otherwise open light inside a dark dashboard.
    "data-theme": theme,
    onclick: (e) => { if (e.target === scrim) close(); },
  }, panel);

  document.body.append(scrim);
  document.addEventListener("keydown", onKey);
  return close;
}
