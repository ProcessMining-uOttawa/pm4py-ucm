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

/**
 * JSON safe to embed in a script element — mirror of view._script_json.
 *
 * Escapes every literal `<` to its `<` form, which neutralises a
 * closing script tag and the `<!--` comment open at once and stays valid
 * JSON (a graphviz SVG's `<!--` comment would otherwise make JSON.parse
 * throw on the way back in).
 */
export function scriptJson(value) {
  return JSON.stringify(value).replace(/</g, "\\u003c");
}

export const h = (tag, attrs = {}, ...kids) => {
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

//: SVG sibling of `h`: elements must be in the SVG namespace or the
//: browser renders an inert HTML `<rect>` that draws nothing.
const SVGNS = "http://www.w3.org/2000/svg";
export const svgEl = (tag, attrs = {}, ...kids) => {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.setAttribute("class", v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
};

export const STATE_LABEL = { met: "MET", risk: "AT RISK", missed: "MISSED" };

//: Most bars a categorical axis draws before the renderer keeps only the
//: largest and says so. Sized so each bar stays wide enough to hover in
//: a 2-column card.
const BAR_CAP = 24;

//: Sentinel axis id for the filter picker's "Date range" option — not a
//: real segment axis (it produces a `date` filter, not a `segment` one).
const DATE_AXIS = "__date";

//: Aggregations offered per result type — mirror of catalog.AGGS_BY_TYPE,
//: needed here because a ƒ custom metric has no catalog entry to read
//: them from.
const AGGS_BY_TYPE = {
  time: ["avg", "median", "p90", "min", "max"],
  count: ["avg", "median", "p90", "sum", "min", "max"],
  percent: ["share"],
  rate: ["avg", "median", "p90", "max"],
};
const CUSTOM_DEFAULT_AGG = { percent: "share", time: "avg", count: "avg" };

//: Insertable functions shown in the formula editor, grouped as the
//: handoff's rail is.
const FORMULA_HELP = [
  ["case", [
    ["contains(\"act\")", "1 if the case has the activity"],
    ["count(\"act\")", "how many times the activity occurs"],
    ["duration()", "case length, in days"],
    ["attr(\"name\")", "a numeric case attribute"],
  ]],
  ["time", [
    ["time_between(\"a\", \"b\")", "days from first a to the next b"],
    ["timestamp(\"act\")", "when the activity first occurs"],
  ]],
  ["combine", [
    ["where", "keep only cases matching a condition"],
    ["and", "both are true"], ["or", "either is true"], ["not", "negate"],
  ]],
];

export class Dashboard {
  /**
   * @param {HTMLElement} root
   * @param {object} opts  {payload, catalog, specs, name, readOnly,
   *                        storageKey, renders}
   */
  constructor(root, opts) {
    this.root = root;
    this.opts = opts;
    // A shared table may be passed in (the session report decodes the
    // payload once and hands the same table to every section) rather than
    // decoding it per dashboard.
    this.table = opts.table || E.decodePayload(opts.payload);
    this.catalog = Object.fromEntries(opts.catalog.map((m) => [m.id, m]));
    this.catalogList = opts.catalog;
    this.name = opts.name || "Dashboard";
    this.readOnly = !!opts.readOnly;
    // Headless: no own header. The session report embeds dashboard
    // sections under one shared reader filter bar, so each section drops
    // its own header and takes its filters from the report.
    this.headless = !!opts.headless;
    this.renders = opts.renders || {};

    // Multiple named dashboards persist in one registry entry per log; the
    // active one's name/specs/filters are the live fields the rest of the
    // class reads. A read-only / headless instance (an export, a report
    // section) is a single ephemeral dashboard from opts — no registry,
    // no switcher. An export carries the filters it was taken under, so it
    // opens on the question it was sent to answer.
    this._multi = !this.readOnly && !this.headless;
    this._loadRegistry(opts);
    this._activate();
    this.notation = Object.keys(this.renders)[0] || "ucm";

    root.classList.add("pm-dash");
    // A headless section inherits the report's theme rather than resolving
    // its own; the report owns the one <html> data-theme.
    if (this.headless) this.dark = !!opts.dark;
    else this._initTheme(opts.theme);
    this._applyPendingPin(opts.pendingPin);
    this.render();
  }

  /** Re-filter and re-render — the report calls this when its shared
   *  reader filter bar changes. */
  setFilters(filters) {
    this.filters = (filters || []).slice();
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
    const host = this._hostThemeSource();

    // Precedence: the live host theme when we are embedded and can read
    // it, else the theme the host baked into the page, else the OS. The
    // live read is what fixes the "partial refresh": Streamlit's own
    // theme-toggle regenerates this page, but the theme it bakes in can
    // lag a rerun, so trusting it left the island a step behind. Reading
    // the host directly is never stale.
    const resolve = () => host ? host.read()
      : theme ? theme === "dark" : mq.matches;

    const apply = () => {
      this.dark = resolve();
      const t = this.dark ? "dark" : "light";
      // Two places: the .pm-dash root (its token block keys off the
      // attribute) and <html> (the page-background CSS in the template
      // keys off that), so a dark dashboard has no light gutter.
      this.root.setAttribute("data-theme", t);
      document.documentElement.setAttribute("data-theme", t);
    };
    apply();

    const onChange = () => { const before = this.dark; apply();
      if (this.dark !== before) this.render(); };
    if (host) {
      host.observe(onChange);
    } else if (!theme) {
      // A standalone export follows the OS live. addEventListener on a
      // MediaQueryList is unsupported on old Safari, which may well open
      // the export.
      if (mq.addEventListener) mq.addEventListener("change", onChange);
      else if (mq.addListener) mq.addListener(onChange);
    }
  }

  /**
   * A reader for the embedding host's live theme, or ``null`` when there
   * is no reachable host (the standalone export, opened top-level).
   *
   * Streamlit renders the island in a same-origin ``srcdoc`` iframe with
   * ``allow-same-origin``, so the host document is readable. Its ``.stApp``
   * carries a CSS ``color-scheme`` that states the active theme — and the
   * shell overrides the app *background* but not that, so it stays a
   * clean signal. Background luminance is the fallback if a Streamlit
   * version ever drops it.
   */
  _hostThemeSource() {
    let doc;
    try {
      if (window.parent === window) return null;   // top-level: the export
      doc = window.parent.document;                 // throws if cross-origin
      if (!doc) return null;
    } catch (e) {
      return null;                                  // no allow-same-origin
    }
    const read = () => {
      const app = doc.querySelector(".stApp") || doc.body;
      if (!app) return !!this.dark;
      const cs = getComputedStyle(app);
      if (cs.colorScheme === "dark") return true;
      if (cs.colorScheme === "light") return false;
      return isDarkColor(cs.backgroundColor);
    };
    const observe = (cb) => {
      // A Streamlit theme toggle surfaces as an attribute change on the
      // app and/or a stylesheet swap in the head. Watch both; the read
      // is cheap, so reacting to a superfluous mutation costs nothing.
      const mo = new MutationObserver(cb);
      const app = doc.querySelector(".stApp");
      if (app) mo.observe(app, { attributes: true,
        attributeFilter: ["class", "style"] });
      if (doc.head) mo.observe(doc.head, { childList: true, subtree: true });
    };
    return { read, observe };
  }

  /** The resolved theme, for surfaces that render outside the root. */
  _theme() { return this.dark ? "dark" : "light"; }

  // -- persistence ---------------------------------------------------
  // localStorage, not the server: components.html cannot send state back
  // to Python, and the export has no server at all. Same code, so a
  // dashboard built in the app survives into the exported file via the
  // specs baked into view.py's payload.

  _key() { return `pm4py-ucm:dash:${this.opts.storageKey || this.table.logName}`; }
  _regKey() { return this._key() + ":set"; }
  _uid() {
    return "d" + Date.now().toString(36) +
      Math.random().toString(36).slice(2, 6);
  }

  /**
   * Load this log's dashboard registry (all named dashboards + the active
   * one), migrating a legacy single-dashboard store on first run. A
   * read-only / headless instance is a single ephemeral dashboard from
   * opts — no storage.
   */
  _loadRegistry(opts) {
    const seed = () => {
      const id = this._uid();
      return { active: id, dashboards: [{
        id, name: opts.name || "Dashboard",
        specs: (opts.specs || []).slice(),
        filters: (opts.filters || []).slice(),
      }] };
    };
    if (!this._multi) { this._reg = seed(); return; }

    let reg = null;
    try {
      const raw = localStorage.getItem(this._regKey());
      if (raw) reg = JSON.parse(raw);
    } catch (e) {
      console.warn("dashboard: could not read the dashboard set", e);
    }
    if (reg && Array.isArray(reg.dashboards) && reg.dashboards.length) {
      this._reg = reg;
    } else {
      this._reg = seed();
      // Migrate a legacy single-dashboard store into the first dashboard.
      try {
        const raw = localStorage.getItem(this._key());
        const legacy = raw ? JSON.parse(raw) : null;
        if (Array.isArray(legacy) && legacy.length) {
          this._reg.dashboards[0].specs = legacy;
        }
      } catch (e) { /* no legacy store */ }
    }
    if (!this._reg.dashboards.some((d) => d.id === this._reg.active)) {
      this._reg.active = this._reg.dashboards[0].id;
    }
  }

  /** Make the registry's active dashboard the live name/specs/filters. */
  _activate() {
    const d = this._reg.dashboards.find((x) => x.id === this._reg.active)
      || this._reg.dashboards[0];
    this.activeId = d.id;
    this.name = d.name || "Dashboard";
    this.specs = (d.specs || []).slice();
    this.filters = (d.filters || []).slice();
  }

  /** Write the live fields back into the active entry and persist. */
  _save() {
    if (!this._multi) return;
    const d = this._reg.dashboards.find((x) => x.id === this.activeId);
    if (d) { d.name = this.name; d.specs = this.specs; d.filters = this.filters; }
    try {
      localStorage.setItem(this._regKey(), JSON.stringify(this._reg));
    } catch (e) {
      console.warn("dashboard: could not save the dashboard set", e);
    }
  }

  // -- multiple named dashboards -------------------------------------

  _switchTo(id) {
    if (id === this.activeId) return;
    this._save();               // persist the one we are leaving
    this._reg.active = id;
    this._activate();
    this._save();
    this.render();
  }

  _newDashboard() {
    this._nameDialog("New dashboard", "", (name) => {
      this._save();
      const id = this._uid();
      this._reg.dashboards.push(
        { id, name: name || "Dashboard", specs: [], filters: [] });
      this._reg.active = id;
      this._activate();
      this._save();
      this.render();
      toast(`Created “${this.name}”`, this._theme());
    });
  }

  _renameDashboard() {
    this._nameDialog("Rename dashboard", this.name, (name) => {
      if (!name) return;
      this.name = name;
      this._save();
      this.render();
    });
  }

  _deleteDashboard() {
    const only = this._reg.dashboards.length <= 1;
    const gone = this.name;
    modal({
      theme: this._theme(),
      title: only ? "Clear dashboard" : "Delete dashboard",
      sub: `→ ${this.name}`,
      body: h("div", { class: "pm-empty" }, only
        ? `Remove all widgets from “${gone}”?`
        : `Delete “${gone}” and its widgets? This cannot be undone.`),
      confirm: only ? "Clear" : "Delete",
      onConfirm: () => {
        if (only) { this.specs = []; this.filters = []; }
        else {
          this._reg.dashboards =
            this._reg.dashboards.filter((d) => d.id !== this.activeId);
          this._reg.active = this._reg.dashboards[0].id;
          this._activate();
        }
        this._save();
        this.render();
        toast(only ? "Cleared the dashboard" : `Deleted “${gone}”`,
          this._theme());
        return true;
      },
    });
  }

  /** A one-field modal for naming / renaming a dashboard. */
  _nameDialog(title, initial, onOk) {
    const input = h("input", {
      class: "pm-input", type: "text", value: initial,
      placeholder: "Dashboard name", style: "width:100%",
    });
    const body = h("div", { class: "pm-row" },
      h("span", { class: "pm-row__label" }, "Name"), input);
    modal({
      theme: this._theme(), title, body, confirm: "OK",
      onConfirm: () => { onOk(input.value.trim()); return true; },
    });
    setTimeout(() => input.focus(), 0);
  }

  /** The dashboard switcher shown in the header (editable mode only). */
  _dashSwitcher() {
    const sel = h("select", {
      class: "pm-select pm-dashsel", title: "Switch dashboard",
      onchange: (e) => this._switchTo(e.target.value),
    }, ...this._reg.dashboards.map((d) =>
      h("option", { value: d.id, selected: d.id === this.activeId }, d.name)));
    const icon = (title, glyph, fn) => h("button", {
      class: "pm-btn pm-btn--ghost pm-btn--icon", title, onclick: fn,
    }, glyph);
    return h("span", { class: "pm-dashbar" }, sel,
      icon("New dashboard", "+", () => this._newDashboard()),
      icon("Rename dashboard", "✎", () => this._renameDashboard()),
      icon("Delete dashboard", "✕", () => this._deleteDashboard()));
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
    data.textContent = scriptJson(cfg);

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

  downloadReport() {
    // buildSessionReport is a bundle-scope function (dash-report.js is
    // concatenated after this file); it is always present in a rendered
    // page, which is the only place this runs.
    const blob = new Blob([buildSessionReport(this)], { type: "text/html" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${(this.name || "session").replace(/[^\w.-]+/g, "_")}-report.html`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast("Session report exported", this._theme());
  }

  // -- save / load a dashboard definition ----------------------------

  /**
   * The dashboard's *definition* — its widgets and filters — as a small
   * portable JSON file, distinct from the self-contained HTML export.
   *
   * The HTML export ships the whole fact table so it runs offline; this
   * ships only the recipe (a few KB), so it can be reloaded here or
   * opened on another log. A definition names activities and attributes,
   * so it is portable across logs that share them; `unboundRefs` reports
   * what a target log is missing on load.
   */
  downloadDefinition() {
    const def = {
      pm4pyUcmDashboard: 1,
      name: this.name,
      log: this.table.logName,
      specs: this.exportSpecs(),
      filters: JSON.parse(JSON.stringify(this.filters)),
    };
    const blob = new Blob([JSON.stringify(def, null, 2)],
      { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${(this.name || "dashboard")
      .replace(/[^\w.-]+/g, "_")}.dashboard.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast("Dashboard definition saved", this._theme());
  }

  /** Open the OS file picker and load the chosen definition. */
  _pickDefinition() {
    const input = h("input", {
      type: "file", accept: ".json,application/json",
      style: "display:none",
    });
    input.addEventListener("change", () => {
      const file = input.files && input.files[0];
      if (file) this.loadDefinition(file);
      input.remove();
    });
    document.body.append(input);
    input.click();
  }

  /** Read a definition file and apply it (async — FileReader). */
  loadDefinition(file) {
    const reader = new FileReader();
    reader.onload = () => {
      let def;
      try { def = JSON.parse(reader.result); }
      catch (e) {
        toast("Could not load — the file is not valid JSON", this._theme());
        return;
      }
      this._applyDefinition(def);
    };
    reader.onerror = () =>
      toast("Could not read that file", this._theme());
    reader.readAsText(file);
  }

  /**
   * Replace the current widgets with a loaded definition, reporting any
   * that cannot bind to this log.
   *
   * Widget ids are re-minted so a definition with blank or colliding ids
   * (or one loaded twice) never breaks reorder/edit, which key off id.
   * Filters are exploration state tied to the values of the log they were
   * taken on, so they are only carried when the definition came from this
   * same log; otherwise they are dropped and noted.
   */
  _applyDefinition(def) {
    if (!def || def.pm4pyUcmDashboard == null || !Array.isArray(def.specs)) {
      toast("Not a PM4Py-UCM dashboard file", this._theme());
      return;
    }

    const stamp = Date.now().toString(36);
    const specs = def.specs.map((s, i) => {
      const c = JSON.parse(JSON.stringify(s));
      c.id = `w${stamp}${i}`;
      return c;
    });

    const unbound = specs
      .map((s) => ({ spec: s, refs: E.unboundRefs(s, this.table) }))
      .filter((u) => u.refs.activities.length || u.refs.attributes.length);

    const sameLog = def.log && def.log === this.table.logName;
    const filters = sameLog ? (def.filters || []).slice() : [];
    const droppedFilters = sameLog ? 0 : (def.filters || []).length;

    if (typeof def.name === "string" && def.name.trim()) {
      this.name = def.name.trim();
    }
    this.specs = specs;
    this.filters = filters;
    this._save();
    this.render();

    this._reportLoad(specs.length, unbound, droppedFilters, def.log);
  }

  /** Tell the user what loaded, and what could not bind. */
  _reportLoad(total, unbound, droppedFilters, fromLog) {
    if (!unbound.length && !droppedFilters) {
      toast(`Loaded “${this.name}” — ${total} ` +
        `widget${total === 1 ? "" : "s"}`, this._theme());
      return;
    }

    const title = (s) => {
      try { return this._compute(s).title || s.title || s.metric; }
      catch (e) { return s.title || s.metric; }
    };
    const body = h("div", { class: "pm-load-report" });
    body.append(h("p", {},
      `Loaded ${total} widget${total === 1 ? "" : "s"} into ` +
      `“${this.name}”.`));

    if (unbound.length) {
      body.append(h("p", {},
        `${unbound.length} couldn't bind to this log ` +
        `(${this.table.logName}) and will show “—”:`));
      const list = h("ul", { class: "pm-load-report__list" });
      for (const u of unbound) {
        const miss = u.refs.activities
          .map((a) => `activity “${a}”`)
          .concat(u.refs.attributes.map((a) => `attribute “${a}”`))
          .join(", ");
        list.append(h("li", {},
          h("strong", {}, title(u.spec)),
          h("span", { class: "pm-load-report__miss" }, ` — missing ${miss}`)));
      }
      body.append(list);
    }

    if (droppedFilters) {
      body.append(h("p", { class: "pm-load-report__note" },
        `${droppedFilters} filter${droppedFilters === 1 ? "" : "s"} ` +
        `${droppedFilters === 1 ? "was" : "were"} not carried over` +
        (fromLog ? ` (built on ${fromLog}, not this log).` : ".")));
    }

    modal({
      theme: this._theme(),
      title: "Dashboard loaded",
      sub: `→ ${this.name}`,
      body,
      cancel: "Close",
    });
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
      this.headless ? document.createComment("headless") : this._header(),
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
      // The switcher (name dropdown + new/rename/delete) stands in for the
      // plain title when the dashboard is editable; a read-only export
      // shows just its name.
      this.readOnly ? h("span", { class: "pm-title" }, this.name)
        : this._dashSwitcher(),
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
        title: "This dashboard alone, as a standalone interactive HTML "
             + "file — works offline, no server.",
        onclick: () => this.downloadExport(),
      }, "⬇ Export"));
      kids.push(h("button", {
        class: "pm-btn pm-btn--ghost",
        title: "The full session report — scorecard, this dashboard, and "
             + "the process model in both notations — as one offline file.",
        onclick: () => this.downloadReport(),
      }, "⬇ Session report"));
      kids.push(h("button", {
        class: "pm-btn pm-btn--ghost",
        title: "Save this dashboard's definition (its widgets and filters) "
             + "as a small JSON file you can reload later, or open on "
             + "another log.",
        onclick: () => this.downloadDefinition(),
      }, "⬇ Save"));
      kids.push(h("button", {
        class: "pm-btn pm-btn--ghost",
        title: "Load a saved dashboard definition (.json), replacing the "
             + "current widgets. Widgets that name activities or attributes "
             + "this log lacks are reported and shown as “—”.",
        onclick: () => this._pickDefinition(),
      }, "⬆ Load"));
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
    // A resized widget carries its own grid span, overriding the per-viz
    // default (a KPI is 1×1, a chart 2×2, …). It rides on the spec, so it
    // persists to storage and travels with a saved/exported dashboard.
    if (spec.size && spec.size.w && spec.size.h) {
      card.style.gridColumn = `span ${spec.size.w}`;
      card.style.gridRow = `span ${spec.size.h}`;
    }

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
      // Reorder by dragging this grip; resize by dragging the corner
      // handle added below. Array order is the reading order (the grid is
      // grid-auto-flow:dense), so a reorder just moves the spec in the
      // array and re-renders — the drop reflows the rest.
      const grip = h("button", {
        class: "pm-w__tool pm-w__grip", title: "Drag to reorder",
        draggable: "true", "aria-label": "Drag to reorder widget",
      }, "⠿");
      this._wireReorder(grip, card, i);
      tools.append(grip);
      // A model widget is pinned, not composed — it has no metric,
      // segmentation or target to edit — so it gets reorder/resize/remove
      // but not the composer.
      if (w.viz !== "model") {
        tools.append(h("button", {
          class: "pm-w__tool", title: "Edit widget",
          onclick: () => this._openComposer(spec, i),
        }, "✎"));
      }
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
    } else if (w.viz === "hist" && w.hist) {
      body.append(...this._histBody(w));
    } else if (w.viz === "box" && w.box) {
      body.append(...this._boxBody(w));
    } else {
      body.append(...this._kpiBody(w, spec));
    }
    card.append(body);
    if (!this.readOnly) card.append(this._resizeHandle(card, spec));
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

  // -- distribution charts (histogram / box plot) --------------------

  /** The aggregate headline both distribution charts keep above them. */
  _distHead(w) {
    return [
      h("div", { class: "pm-kpi__value pm-kpi__value--sm" }, w.text),
      h("div", { class: "pm-kpi__sub" }, w.sub || ""),
    ];
  }

  _histBody(w) {
    const out = this._distHead(w);
    const hh = w.hist;
    // Wrap the chart so it *fills* the body's leftover height and scales
    // to fit (preserveAspectRatio) rather than deriving its height from
    // the card width — otherwise a tall chart overflows the card, which
    // clips its baseline and axis labels.
    out.push(hh && hh.bins.length
      ? h("div", { class: "pm-w__chart" }, this._histSvg(hh, w.unit))
      : h("div", { class: "pm-chart__empty" }, "No data to plot."));
    return out;
  }

  _histSvg(hh, unit) {
    const W = 300, H = 132, padL = 6, padR = 6, padT = 6, padB = 20;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const bins = hh.bins;
    const maxC = Math.max(1, ...bins.map((b) => b.count));
    const bw = plotW / bins.length;
    const bars = bins.map((b, i) => {
      const barH = (b.count / maxC) * plotH;
      const label = hh.integer && b.lo === b.hi
        ? String(b.lo)
        : `${E.fmt(b.lo, unit)}–${E.fmt(b.hi, unit)}`;
      return svgEl("rect", {
        x: padL + i * bw + 0.5, y: padT + plotH - barH,
        width: Math.max(0.5, bw - 1), height: barH, class: "pm-hist__bar",
      }, svgEl("title", {}, `${label}: ` +
        `${b.count.toLocaleString("en-US")} case` +
        `${b.count === 1 ? "" : "s"}`));
    });
    return svgEl("svg", {
      viewBox: `0 0 ${W} ${H}`, class: "pm-chart pm-hist",
      role: "img", "aria-label": `Histogram of ${hh.n} cases`,
    },
      svgEl("line", { x1: padL, y1: padT + plotH, x2: padL + plotW,
        y2: padT + plotH, class: "pm-chart__axis" }),
      ...bars,
      svgEl("text", { x: padL, y: H - 6, class: "pm-chart__lab" },
        E.fmt(hh.min, unit)),
      svgEl("text", { x: padL + plotW, y: H - 6, "text-anchor": "end",
        class: "pm-chart__lab" }, E.fmt(hh.max, unit)));
  }

  _boxBody(w) {
    const out = this._distHead(w);
    const bx = w.box;
    out.push(bx && bx.n
      ? h("div", { class: "pm-w__chart" }, this._boxSvg(bx, w.unit))
      : h("div", { class: "pm-chart__empty" }, "No data to plot."));
    return out;
  }

  _boxSvg(bx, unit) {
    const W = 300, H = 74, padL = 6, padR = 6, padT = 8, padB = 20;
    const plotW = W - padL - padR;
    const yMid = padT + (H - padT - padB) / 2;
    const bh = 22;
    const span = (bx.max - bx.min) || 1;
    const x = (v) => padL + ((v - bx.min) / span) * plotW;
    const els = [
      // whiskers, with end caps
      svgEl("line", { x1: x(bx.whiskerLo), y1: yMid, x2: x(bx.q1), y2: yMid,
        class: "pm-box__whisker" }),
      svgEl("line", { x1: x(bx.q3), y1: yMid, x2: x(bx.whiskerHi), y2: yMid,
        class: "pm-box__whisker" }),
      svgEl("line", { x1: x(bx.whiskerLo), y1: yMid - 6, x2: x(bx.whiskerLo),
        y2: yMid + 6, class: "pm-box__whisker" }),
      svgEl("line", { x1: x(bx.whiskerHi), y1: yMid - 6, x2: x(bx.whiskerHi),
        y2: yMid + 6, class: "pm-box__whisker" }),
      // the interquartile box + median
      svgEl("rect", { x: x(bx.q1), y: yMid - bh / 2,
        width: Math.max(1, x(bx.q3) - x(bx.q1)), height: bh,
        class: "pm-box__box" }),
      svgEl("line", { x1: x(bx.median), y1: yMid - bh / 2, x2: x(bx.median),
        y2: yMid + bh / 2, class: "pm-box__median" }),
    ];
    for (const o of bx.outliers) {
      els.push(svgEl("circle", { cx: x(o), cy: yMid, r: 2,
        class: "pm-box__outlier" }, svgEl("title", {}, E.fmt(o, unit))));
    }
    const summary =
      `min ${E.fmt(bx.min, unit)} · Q1 ${E.fmt(bx.q1, unit)} · ` +
      `median ${E.fmt(bx.median, unit)} · Q3 ${E.fmt(bx.q3, unit)} · ` +
      `max ${E.fmt(bx.max, unit)}` +
      (bx.nOutliers ? ` · ${bx.nOutliers} outlier` +
        `${bx.nOutliers === 1 ? "" : "s"}` : "");
    return svgEl("svg", {
      viewBox: `0 0 ${W} ${H}`, class: "pm-chart pm-box",
      role: "img", "aria-label": summary,
    },
      svgEl("title", {}, summary), ...els,
      svgEl("text", { x: padL, y: H - 6, class: "pm-chart__lab" },
        E.fmt(bx.min, unit)),
      svgEl("text", { x: x(bx.median), y: H - 6, "text-anchor": "middle",
        class: "pm-chart__lab" }, E.fmt(bx.median, unit)),
      svgEl("text", { x: padL + plotW, y: H - 6, "text-anchor": "end",
        class: "pm-chart__lab" }, E.fmt(bx.max, unit)));
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
    // In a report section the drill bubbles to the report's shared reader
    // filter, so a cell click narrows every section, not just this one.
    if (this.opts.onDrill) { this.opts.onDrill(axis, label); return; }
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
      ...axes.map((a) => h("option", { value: a.id }, a.label)),
      // A date range is not a segment axis (it is continuous, not a set of
      // labels), so it is its own option that swaps the value control for
      // two date inputs. The engine already understands the `date` filter
      // it produces.
      h("option", { value: DATE_AXIS }, "Date range"));

    const valSel = h("select", { class: "pm-select" });
    const [dmin, dmax] = E.dateSpan(this.table);
    const dateAttrs = { class: "pm-input pm-date", type: "date",
      style: "width:auto" };
    if (dmin) { dateAttrs.min = dmin; dateAttrs.max = dmax; }
    const fromIn = h("input", { ...dateAttrs, value: dmin || "" });
    const toIn = h("input", { ...dateAttrs, value: dmax || "" });
    const opWord = h("span", { class: "pm-row__hint" }, "is");
    const toWord = h("span", { class: "pm-row__hint" }, "to");

    const isDate = () => axisSel.value === DATE_AXIS;
    const fill = () => {
      const { labels } = E.segmentKeys(this.table, axisSel.value);
      valSel.replaceChildren(...labels.map((l) => h("option", { value: l }, l)));
    };
    const sync = () => {
      const d = isDate();
      valSel.style.display = d ? "none" : "";
      opWord.textContent = d ? "from" : "is";
      for (const el of [fromIn, toWord, toIn]) el.style.display = d ? "" : "none";
      if (!d) fill();
    };
    axisSel.addEventListener("change", sync);
    sync();

    const control = h("span", { class: "pm-filter-ctl" },
      axisSel, opWord, valSel, fromIn, toWord, toIn);

    return {
      axisSel, valSel, control,
      get: () => isDate()
        // An empty end is left open — "from March" and "up to March" are
        // both useful; the engine treats null as unbounded on that side.
        ? { field: "date", op: "is",
            value: [fromIn.value || null, toIn.value || null] }
        : { field: "segment", op: "is",
            value: [axisSel.value, valSel.value] },
    };
  }

  _openFilter() {
    const p = this._filterPicker();
    const body = h("div", {}, h("div", { class: "pm-row" },
      h("span", { class: "pm-row__label" }, "Where"), p.control));

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
    let close;   // assigned by modal() below; the drill handlers close it
    const body = h("div", {});
    if (!rows.length) {
      body.append(h("div", { class: "pm-empty" },
        "No widget on this dashboard has a target."));
    } else {
      const tbody = h("tbody", {});
      for (const r of rows) {
        const spec = this.specs.find((s) => s.id === r.id) || {};
        // In per_case mode the value is a share, so the bar must measure
        // against the share goal, not the per-case threshold.
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

        // Drill-down: a segmented target rolls up to a worst-state pill,
        // hiding *which* segments broke it. Expand the row to list the
        // breaching segments; clicking one filters the whole dashboard to
        // it (the same drill a table-cell click does).
        const breaches = this._breachingSegments(this._compute(spec));
        const drillable = breaches.length > 0;
        const caret = h("span", { class: "pm-score__caret" },
          drillable ? "▸" : "");
        const detail = h("tr", { class: "pm-score__detail" },
          h("td", { colspan: 6 }, drillable && h("div", { class: "pm-score__segs" },
            h("span", { class: "pm-score__segs-lab" },
              `${breaches.length} segment${breaches.length === 1 ? "" : "s"} ` +
              `breached — click to filter the dashboard:`),
            ...breaches.map((b) => h("button", {
              class: `pm-score__seg pm-score__seg--${b.state}`,
              title: `${STATE_LABEL[b.state]} · ${b.text}`,
              onclick: () => { this._drillBreach(b); if (close) close(); },
            }, `${b.label} · ${b.text}`)))));

        tbody.append(h("tr", {
          class: "pm-score__row" + (drillable ? " pm-score__row--drill" : ""),
          title: drillable ? "Show the segments that breached" : undefined,
          onclick: drillable ? () => {
            const open = detail.classList.toggle("pm-score__detail--open");
            caret.textContent = open ? "▾" : "▸";
          } : undefined,
        },
          h("td", {}, caret, " ", r.title),
          h("td", {}, r.goal),
          h("td", {}, r.actual),
          h("td", {}, h("div", { class: "pm-score__ach" },
            h("i", { style: `width:${pct}%;background:${colour}` }))),
          h("td", {}, r.nCases.toLocaleString("en-US")),
          h("td", {}, h("span", {
            class: `pm-pill pm-pill--${r.state || "met"}`,
          }, STATE_LABEL[r.state] || "—"))), detail);
      }
      body.append(h("table", { class: "pm-score" },
        h("thead", {}, h("tr", {},
          ...["Target", "Goal", "Actual", "Achievement", "Cases", "State"]
            .map((x) => h("th", {}, x)))),
        tbody));
    }
    close = modal({ theme: this._theme(), title: "Scorecard",
                    sub: `→ ${this.name}`, body, confirm: null, cancel: "Close" });
  }

  /**
   * The segments of a targeted, segmented widget that broke its target —
   * the bar points or table cells whose state is *at risk* or *missed*,
   * worst first, each carrying the axis/label drill it maps to.
   */
  _breachingSegments(w) {
    const bad = (s) => s === "missed" || s === "risk";
    const out = [];
    if (w.series && w.axis) {
      for (const p of w.series) {
        if (bad(p.state)) out.push({ label: p.label, text: p.text,
          state: p.state, drill: [[w.axis, p.label]] });
      }
    } else if (w.cells && w.rows && w.cols) {
      for (let ri = 0; ri < w.rows.length; ri++) {
        for (let ci = 0; ci < w.cols.length; ci++) {
          const c = w.cells[ri][ci];
          if (c && bad(c.state)) out.push({
            label: `${w.rows[ri]} · ${w.cols[ci]}`, text: c.text, state: c.state,
            drill: [[w.rowsAxis, w.rows[ri]], [w.colsAxis, w.cols[ci]]] });
        }
      }
    }
    out.sort((a, b) => (a.state === "missed" ? 0 : 1) - (b.state === "missed" ? 0 : 1));
    return out;
  }

  /** Filter the dashboard to a breaching segment (each of its axes). */
  _drillBreach(b) {
    b.drill.forEach(([axis, label], k) =>
      this._drill(axis, label, k < b.drill.length - 1));  // toast on the last only
  }

  // -- drag to reorder, drag to resize -------------------------------

  /**
   * Wire a widget's grip for drag-to-reorder.
   *
   * The grip is the only drag source (dragging the whole card would fight
   * the buttons and the resize handle), but every card is a drop target.
   * On drop the moved spec is spliced to the target's position and the
   * grid re-renders — the dense auto-flow reflows the rest, so there is no
   * placeholder to animate.
   */
  _wireReorder(grip, card, i) {
    grip.addEventListener("dragstart", (e) => {
      this._dragFrom = i;
      card.classList.add("pm-w--dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", String(i));  // Firefox needs a payload
      e.dataTransfer.setDragImage(card, 24, 16);         // drag the card, not the grip
    });
    grip.addEventListener("dragend", () => {
      card.classList.remove("pm-w--dragging");
      this._clearDropzones();
      this._dragFrom = null;
    });
    card.addEventListener("dragover", (e) => {
      if (this._dragFrom == null || this._dragFrom === i) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      this._clearDropzones();
      card.classList.add("pm-w--dropzone");
    });
    card.addEventListener("dragleave", (e) => {
      // Ignore leaves into a child element.
      if (!card.contains(e.relatedTarget)) card.classList.remove("pm-w--dropzone");
    });
    card.addEventListener("drop", (e) => {
      e.preventDefault();
      const from = this._dragFrom;
      this._clearDropzones();
      if (from == null || from === i) return;
      const moved = this.specs[from], target = this.specs[i];
      this.specs.splice(from, 1);
      let ti = this.specs.indexOf(target);
      if (from < i) ti += 1;   // dropping forward lands after the target
      this.specs.splice(ti, 0, moved);
      this._dragFrom = null;
      this._save();
      this.render();
    });
  }

  _clearDropzones() {
    this.root.querySelectorAll(".pm-w--dropzone")
      .forEach((el) => el.classList.remove("pm-w--dropzone"));
  }

  /**
   * The corner handle that resizes a widget's grid span, live.
   *
   * The pointer delta is converted to whole cell steps from the grid's own
   * measured column/row size (so it tracks the pointer at any zoom), the
   * card's span is set inline for immediate feedback, and on release the
   * final span is stored on `spec.size` and the grid re-rendered.
   */
  _resizeHandle(card, spec) {
    const MAX_W = 4, MAX_H = 4;
    const handle = h("div", { class: "pm-w__resize", title: "Drag to resize" });
    handle.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const grid = card.closest(".pm-grid");
      const gs = getComputedStyle(grid);
      const gap = parseFloat(gs.gap) || 0;
      const nCols = gs.gridTemplateColumns.split(" ").filter(Boolean).length || 4;
      const inner = grid.clientWidth - (parseFloat(gs.paddingLeft) || 0)
                                     - (parseFloat(gs.paddingRight) || 0);
      const colStep = (inner + gap) / nCols;
      const rowStep = (parseFloat(gs.gridAutoRows) || 118) + gap;
      const start = (spec.size && spec.size.w)
        ? { w: spec.size.w, h: spec.size.h } : this._defaultSpan(card);
      const startX = e.clientX, startY = e.clientY;
      const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
      let last = { ...start };
      // Capture keeps move/up flowing to the handle even when the pointer
      // leaves it; guarded because some environments reject a stray id.
      try { handle.setPointerCapture(e.pointerId); } catch (err) { /* fine */ }
      const move = (ev) => {
        last = {
          w: clamp(start.w + Math.round((ev.clientX - startX) / colStep),
                   1, Math.min(MAX_W, nCols)),
          h: clamp(start.h + Math.round((ev.clientY - startY) / rowStep), 1, MAX_H),
        };
        card.style.gridColumn = `span ${last.w}`;
        card.style.gridRow = `span ${last.h}`;
      };
      const up = () => {
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", up);
        spec.size = last;
        this._save();
        this.render();
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", up);
    });
    return handle;
  }

  /** The default grid span for a widget that has never been resized. */
  _defaultSpan(card) {
    const cl = card.classList;
    if (cl.contains("pm-w--model") || cl.contains("pm-w--wide")) return { w: 4, h: 2 };
    if (cl.contains("pm-w--kpi")) return { w: 1, h: 1 };
    return { w: 2, h: 2 };   // bar / line / table / hist / box
  }

  // -- composer viz thumbnail picker ---------------------------------

  /** A small SVG glyph of a visualisation, for the thumbnail picker. */
  _vizIcon(viz) {
    const bar = (x, y, w, hh, cls) =>
      svgEl("rect", { x, y, width: w, height: hh, rx: 1, class: cls });
    let marks;
    if (viz === "kpi") {
      marks = [bar(9, 7, 22, 6, "pm-vizpick__fill"),
               bar(9, 17, 13, 3, "pm-vizpick__mut")];
    } else if (viz === "hist") {
      marks = [9, 15, 21, 13, 8].map((hh, k) =>
        bar(7 + k * 6, 24 - hh, 4, hh, "pm-vizpick__fill"));
    } else { // box plot: whisker line, IQR box, median
      marks = [
        svgEl("line", { x1: 6, y1: 14, x2: 34, y2: 14, class: "pm-vizpick__stroke" }),
        bar(13, 8, 14, 12, "pm-vizpick__box"),
        svgEl("line", { x1: 20, y1: 8, x2: 20, y2: 20, class: "pm-vizpick__stroke" }),
      ];
    }
    return svgEl("svg", { viewBox: "0 0 40 28", class: "pm-vizpick__icon" },
      ...marks);
  }

  /**
   * A thumbnail picker for the unsegmented visualisation — clickable tiles
   * that show each shape (KPI / histogram / box plot), in place of a plain
   * dropdown so the choice reads at a glance. Returns the element plus a
   * `value` getter and a `select` setter, so it drops into the composer
   * where the old `<select>` was.
   */
  _chartPicker(onChange) {
    const opts = [["kpi", "KPI card"], ["hist", "Histogram"], ["box", "Box plot"]];
    let current = "kpi";
    const tiles = {};
    const paint = () => {
      for (const k of Object.keys(tiles))
        tiles[k].classList.toggle("pm-vizpick__tile--on", k === current);
    };
    const el = h("div", { class: "pm-vizpick", role: "radiogroup" });
    for (const [v, label] of opts) {
      const tile = h("button", {
        type: "button", class: "pm-vizpick__tile", title: label,
        "aria-label": label,
        onclick: () => { current = v; paint(); if (onChange) onChange(); },
      }, this._vizIcon(v), h("span", { class: "pm-vizpick__lab" }, label));
      tiles[v] = tile;
      el.append(tile);
    }
    paint();
    return {
      el,
      get value() { return current; },
      select(v) { current = v; paint(); },
    };
  }

  // -- composer -------------------------------------------------------

  /**
   * Build or edit a widget.
   *
   * With no argument it opens on a fresh spec ("+ Add widget"); passed an
   * existing spec and its index it opens on a deep copy of that spec (so
   * a cancelled edit leaves the original untouched) and replaces it on
   * save. One method serves both, so the two can never drift in what they
   * can express.
   */
  _openComposer(editSpec, editIndex = -1) {
    const isEdit = editSpec != null;
    const spec = isEdit
      ? JSON.parse(JSON.stringify(editSpec))
      : {
          id: `w${Date.now().toString(36)}`,
          metric: "duration", params: {}, agg: "avg",
          filter: [], segment: {}, viz: "kpi", statusColors: false,
        };
    // Normalise the shape a saved spec may not carry (target gets
    // deleted when off, filter/segment may be absent), so every control
    // below has a field to bind to.
    spec.params = spec.params || {};
    spec.filter = spec.filter || [];
    spec.segment = spec.segment || {};
    spec.target = spec.target
      || { on: false, dir: "<=", value: 14, warn: 18, mode: "aggregate" };
    // A user-set title survives edits; the auto-title only fills in until
    // the field is touched.
    let titleEdited = isEdit;

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
    metricSel.append(h("optgroup", { label: "ƒ" },
      h("option", { value: "custom",
        title: "Write your own metric over the log" },
        "ƒ Custom formula…")));

    const aggSel = h("select", { class: "pm-select" });
    const paramsBox = h("div", {
      style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap;flex:1",
    });
    const helpNote = h("div", { class: "pm-row__note" });

    // ƒ custom-formula editor: a textarea over the closed grammar, a
    // live validity chip and inferred result type, and insertable
    // functions. Validation and the result type come from the same
    // engine the metric computes with, so what the chip says is exactly
    // what will run.
    const fxArea = h("textarea", {
      class: "pm-fx", rows: "2", spellcheck: "false",
      placeholder: 'e.g. contains("Payment") where attr("amount") > 500',
    });
    const fxChip = h("span", { class: "pm-fx__chip" });
    const fxType = h("span", { class: "pm-tag" });
    const fxFns = h("div", { class: "pm-fx__fns" });
    for (const [group, fns] of FORMULA_HELP) {
      fxFns.append(h("span", { class: "pm-fx__group" }, group));
      for (const [text, tip] of fns) {
        fxFns.append(h("button", {
          class: "pm-fx__fn", type: "button", title: tip,
          onclick: () => {
            // Insert at the caret, then place the caret inside the first
            // quotes so the next thing typed is the name.
            const s = fxArea.selectionStart, e = fxArea.selectionEnd;
            const v = fxArea.value;
            fxArea.value = v.slice(0, s) + text + v.slice(e);
            const q = text.indexOf('"');
            const caret = s + (q >= 0 ? q + 1 : text.length);
            fxArea.focus();
            fxArea.setSelectionRange(caret, caret);
            fxInput();
          },
        }, text.replace(/\(.*/, text.includes("(") ? "()" : "")));
      }
    }
    const validateFormula = () => {
      const c = E.compileFormula(fxArea.value, this.table.activities,
        this.table.attributes.map((a) => a.name)
          .concat(this.table.attributes.map((a) => a.label)));
      spec.params = { formula: fxArea.value };
      if (!c.ok) {
        fxChip.textContent = "invalid";
        fxChip.className = "pm-fx__chip pm-fx__chip--bad";
        fxChip.title = c.error;
        fxType.textContent = "";
        fxArea.classList.add("pm-fx--bad");
        return c;
      }
      fxChip.textContent = c.unknown.length ? "check names" : "valid";
      fxChip.className = "pm-fx__chip "
        + (c.unknown.length ? "pm-fx__chip--warn" : "pm-fx__chip--ok");
      fxChip.title = c.unknown.length
        ? "Not found in this log: " + c.unknown.join(", ")
        : "";
      fxType.textContent = c.resultType;
      fxArea.classList.remove("pm-fx--bad");
      return c;
    };
    const fxInput = () => {
      const c = validateFormula();
      // Result type can change with the formula, so the aggregations on
      // offer are rebuilt from it before the preview recomputes.
      if (c.ok) rebuildAggs(spec.agg);
      refresh();
    };
    fxArea.addEventListener("input", fxInput);

    const titleInput = h("input", {
      class: "pm-input", type: "text", style: "flex:1;min-width:200px",
      placeholder: "auto",
      // Once the user types a title, stop overwriting it with the
      // auto-generated one on every field change.
      oninput: () => { titleEdited = true; spec.title = titleInput.value; },
    });

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
    // Without a segment axis a per-case metric is one number, but its
    // distribution can be shown instead — a histogram or a box plot.
    // (With an axis, the visualisation follows the axis count: bar/table.)
    // Thumbnail picker (KPI / histogram / box) instead of a dropdown, so
    // the choice reads at a glance. `refresh` is defined further down and
    // only called on click, so referencing it here is safe.
    const chartPick = this._chartPicker(() => refresh());
    const chartNote = h("span", { class: "pm-row__hint" });

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

    const isCustom = () => metricSel.value === "custom";
    // For a custom metric there is no catalog entry: synthesise one from
    // the formula's live result type so the rest of the composer (aggs,
    // viz, unit) works unchanged.
    const metric = () => {
      if (isCustom()) {
        const rt = validateFormula().resultType || "count";
        return { params: [], aggs: AGGS_BY_TYPE[rt],
          defaultAgg: CUSTOM_DEFAULT_AGG[rt], resultType: rt,
          label: "custom metric", help: "" };
      }
      return this.catalog[metricSel.value];
    };

    const rebuildParams = (keep) => {
      // The formula editor stands in for the Params row on a custom
      // metric; the two are never shown together.
      rows.formula.style.display = isCustom() ? "" : "none";
      if (isCustom()) { rows.params.style.display = "none"; return; }
      const m = metric();
      paramsBox.replaceChildren();
      const prev = keep || {};
      spec.params = {};
      // Seed from the log's dominant start/end activities, so the
      // composer opens on a real measurement instead of an empty one —
      // except when editing, where the widget's own params are restored.
      const ends = E.commonEndpoints(this.table);
      for (const p of m.params) {
        const sel = h("select", {
          class: "pm-select",
          onchange: (e) => { spec.params[p.name] = e.target.value; refresh(); },
        });
        const options = p.kind === "activity" ? this.table.activities : [];
        options.forEach((o) => sel.append(h("option", { value: o }, o)));
        const preferred = prev[p.name] != null ? prev[p.name]
          : (p.name === "to" ? ends.to : ends.from);
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

    const rebuildAggs = (keep) => {
      const m = metric();
      const chosen = m.aggs.includes(keep) ? keep : m.defaultAgg;
      aggSel.replaceChildren(...m.aggs.map((a) =>
        h("option", { value: a, selected: a === chosen }, a)));
      aggSel.disabled = m.aggs.length < 2;
      spec.agg = chosen;
    };

    const syncViz = () => {
      spec.segment = { rows: rowsSel.value || undefined,
                       cols: colsSel.value || undefined };
      const n = (rowsSel.value ? 1 : 0) + (colsSel.value ? 1 : 0);
      // A series metric (WIP, arrival rate) is a time series, not a bag of
      // per-case numbers, so a histogram/box of it would be meaningless —
      // it only ever gets the KPI headline. Others may pick their unsegmented
      // shape.
      const seriesMetric = !isCustom() &&
        E.SERIES_METRICS.includes(metricSel.value);
      const canShape = n === 0 && !seriesMetric;
      rows.chart.style.display = canShape ? "" : "none";
      if (n === 0) {
        spec.viz = seriesMetric ? "kpi" : chartPick.value;
      } else {
        spec.viz = n === 1 ? "bar" : "table";
      }
      spec.statusColors = n === 2 && spec.target.on;
      chartNote.textContent = spec.viz === "hist"
        ? "distribution as a histogram"
        : spec.viz === "box" ? "distribution as a box plot"
          : "a single headline number";
      vizNote.textContent = n === 0
        ? "no axes → single value or its distribution"
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
      // The auto-title tracks the fields until the user overrides it; the
      // placeholder then shows what auto would produce.
      const auto = defaultTitle(spec, metric());
      titleInput.placeholder = auto;
      if (!titleEdited) { spec.title = auto; titleInput.value = auto; }

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
      if (w.viz === "kpi" || w.viz === "hist" || w.viz === "box") {
        preview.append(h("span", { class: "pm-preview__value" }, w.text));
      }
      // Preview the actual distribution, not just the headline — otherwise
      // a histogram/box looks identical to a KPI here and its point is
      // lost. Uses the same renderers the card does.
      if (w.viz === "hist" && w.hist && w.hist.bins.length) {
        preview.append(h("div", { class: "pm-preview__chart" },
          this._histSvg(w.hist, w.unit)));
      } else if (w.viz === "box" && w.box && w.box.n) {
        preview.append(h("div", { class: "pm-preview__chart" },
          this._boxSvg(w.box, w.unit)));
      }
      const shape = w.viz === "kpi" ? "a KPI card"
        : w.viz === "hist" ? `a histogram of ${(w.hist || {}).n || 0} cases`
        : w.viz === "box" ? `a box plot of ${(w.box || {}).n || 0} cases`
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
      // Changing the metric abandons the old params/agg — they belonged
      // to a different measurement — so nothing is kept here. Switching
      // to custom seeds a real formula so the preview is not born broken.
      if (isCustom() && !fxArea.value.trim()) {
        fxArea.value = "duration()";
        validateFormula();
      }
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
    rows.formula = h("div", { class: "pm-row pm-row--fx" },
      h("span", { class: "pm-row__label" }, "ƒ"),
      h("div", { style: "flex:1;min-width:260px" },
        h("div", { style: "display:flex;gap:8px;align-items:center" },
          fxArea, fxChip, fxType),
        fxFns));
    rows.title = h("div", { class: "pm-row" },
      h("span", { class: "pm-row__label" }, "Title"), titleInput);
    rows.filter = h("div", { class: "pm-row" },
      h("span", { class: "pm-row__label" }, "Filter"),
      fPick.control, fAdd, fChips,
      h("span", { class: "pm-row__note" },
        "Applies to this widget only. The dashboard's filters stack on "
        + "top of it."));
    rows.segment = h("div", { class: "pm-row" },
      h("span", { class: "pm-row__label" }, "Segment"),
      h("span", { class: "pm-row__hint" }, "rows"), rowsSel,
      h("span", { class: "pm-row__hint" }, "cols"), colsSel, vizNote);
    rows.chart = h("div", { class: "pm-row" },
      h("span", { class: "pm-row__label" }, "Chart"), chartPick.el, chartNote);
    rows.target = h("div", { class: "pm-row" },
      h("span", { class: "pm-row__label" }, "Target"),
      targetToggle, dirSel, valInput,
      h("span", { class: "pm-row__hint" }, "warn at"), warnInput,
      modeSel, shareLabel, shareInput, targetNote);

    body.append(rows.metric, rows.title, rows.params, rows.formula,
                rows.filter, rows.segment, rows.chart, rows.target,
                helpNote, preview);

    // Set every control from the spec, then compute. For a fresh widget
    // the spec is the defaults; for an edit it is the saved widget, so
    // the composer opens showing exactly what is on the card.
    metricSel.value = spec.metric;
    if (spec.metric === "custom") fxArea.value = (spec.params || {}).formula || "";
    validateFormula();
    rebuildParams(isEdit ? spec.params : undefined);
    rebuildAggs(spec.agg);
    rowsSel.value = spec.segment.rows || "";
    colsSel.value = spec.segment.cols || "";
    // Restore the unsegmented shape when editing a hist/box widget; a
    // segmented one has no chart choice (the axes decide), so default kpi.
    chartPick.select((spec.viz === "hist" || spec.viz === "box")
      ? spec.viz : "kpi");
    dirSel.value = spec.target.dir;
    valInput.value = spec.target.value;
    warnInput.value = spec.target.warn;
    modeSel.value = spec.target.mode || "aggregate";
    if (spec.target.shareGoal != null) shareInput.value = spec.target.shareGoal;
    if (isEdit && spec.title) titleInput.value = spec.title;
    drawChips();
    refresh();

    modal({
      theme: this._theme(),
      title: isEdit ? "Edit widget" : "New widget",
      sub: `→ ${this.name}`, body,
      confirm: isEdit ? "Save changes" : "Add to dashboard",
      onConfirm: () => {
        if (!spec.target.on) delete spec.target;
        const clean = JSON.parse(JSON.stringify(spec));
        if (isEdit) this.specs[editIndex] = clean;
        else this.specs.push(clean);
        this._save();
        this.render();
        toast(isEdit ? "Widget updated" : "Widget added", this._theme());
        return true;
      },
    });
  }
}

// ---------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------

function defaultTitle(spec, metric) {
  if (spec.metric === "custom") {
    const f = (spec.params || {}).formula || "";
    return f ? "ƒ " + f : "custom metric";
  }
  const p = spec.params || {};
  const parts = [];
  if (metric.aggs.length > 1 && spec.agg !== "share") parts.push(spec.agg);
  if (p.from && p.to) parts.push(`${p.from} → ${p.to}`);
  else if (p.activity) parts.push(p.activity);
  else parts.push(metric.label.toLowerCase());
  if (parts.length === 1) parts[0] = metric.label;
  return parts.join(" ");
}

/** Is a CSS colour dark enough to want light text — the theme fallback. */
function isDarkColor(color) {
  const m = String(color).match(/[\d.]+/g);
  if (!m || m.length < 3) return false;
  const [r, g, b] = m.map(Number);
  // Rec. 601 luma; < 0.5 of 255 reads as a dark surface.
  return (0.299 * r + 0.587 * g + 0.114 * b) < 128;
}

export function axisLabel(axis) {
  if (!axis) return "";
  if (axis.startsWith("attr:")) return axis.slice(5).replace(/^case:/, "");
  return axis[0].toUpperCase() + axis.slice(1);
}

export function describeFilter(f, table) {
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
