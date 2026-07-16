// The dashboard metrics engine, browser side.
//
// This is the JS half of a two-implementation engine. The Python half —
// pm4py_ucm/algo/dashboards/engine.py — is the reference; this file must
// return the same numbers from the same contract payload, and
// tests/test_dashboards.py pins the pair against each other on a real
// log rather than trusting that they agree.
//
// It is loaded twice over: by the Dashboards view in the Streamlit app,
// and by the self-contained HTML export. That is the point of computing
// client-side at all — the reader's filter bar recomputes every widget
// offline, with no server, because the export carries this file and the
// same fact table the app used.
//
// Porting notes, i.e. the places where the obvious JS differs from the
// Python and must not:
//
//   * percentiles interpolate linearly (numpy/pandas default, and the
//     convention docs/metrics.md pins for the package). The design
//     prototype's pm-engine.js used nearest-rank; that is NOT what this
//     implements.
//   * ordering of two events is decided by event index, never by
//     comparing timestamps — the fact table stores whole seconds, so
//     genuinely ordered events can share one.
//   * weekday indices are Monday-first.
//   * a missing per-case value is NaN and leaves the denominator; it is
//     never coerced to 0.

const DAY = 86400;
const CONTRACT_VERSION = 1;

export const AGGS = ["avg", "median", "p90", "sum", "min", "max", "share"];

export const STATE_UI = {
  met: { label: "MET", bg: "#e6f4ec", fg: "#166b42" },
  risk: { label: "AT RISK", bg: "#fdf3d7", fg: "#8a6d00" },
  missed: { label: "MISSED", bg: "#fbeaec", fg: "#8f001a" },
};

const STATE_ORDER = ["missed", "risk", "met"];
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
export const TIME_AXES = ["year", "quarter", "month", "weekday"];
export const SERIES_METRICS = ["wip", "arrivalRate", "completionRate"];
const UNIT_BY_TYPE = { time: "d", percent: "%", count: "n", rate: "n" };

const ARRAY_OF = {
  uint8: Uint8Array, uint16: Uint16Array, uint32: Uint32Array,
  int32: Int32Array, float64: Float64Array,
};
const SENTINEL = {
  uint8: 0xff, uint16: 0xffff, uint32: 0xffffffff,
};

// ---------------------------------------------------------------------
// Decoding
// ---------------------------------------------------------------------

function b64ToBytes(b64) {
  if (typeof atob === "function") {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  return new Uint8Array(Buffer.from(b64, "base64")); // node, for the tests
}

/** Decode a contract payload into the table the rest of this module reads. */
export function decodePayload(payload) {
  if (payload.version !== CONTRACT_VERSION) {
    throw new Error(
      `Unsupported dashboard contract version ${payload.version}; ` +
      `this engine speaks version ${CONTRACT_VERSION}.`
    );
  }
  const buffers = {};
  for (const [key, buf] of Object.entries(payload.buffers)) {
    const Ctor = ARRAY_OF[buf.dtype];
    if (!Ctor) throw new Error(`Unknown buffer dtype ${buf.dtype}`);
    const bytes = b64ToBytes(buf.b64);
    // .slice() so the view owns aligned memory: a base64 payload gives
    // no alignment guarantee, and a misaligned TypedArray view throws.
    buffers[key] = new Ctor(bytes.buffer.slice(
      bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
  }
  const t = {
    logName: payload.log.name,
    nCases: payload.log.n_cases,
    nEvents: payload.log.n_events,
    intervalLog: payload.log.interval,
    sampledFrom: payload.log.sampled_from,
    droppedEvents: payload.log.dropped_events || 0,
    activities: payload.activities,
    resources: payload.resources || [],
    attributes: payload.attributes || [],
    buffers,
  };
  t.sampled = t.sampledFrom != null;
  t.off = buffers.off;
  t.starts = buffers.start;
  t.act = buffers.act;
  t.dt = buffers.dt;
  t._cache = {};
  return t;
}

export function attributeOf(t, name) {
  return t.attributes.find((a) => a.name === name || a.label === name) || null;
}

// ---------------------------------------------------------------------
// Structure helpers
// ---------------------------------------------------------------------

function caseOfEvent(t) {
  if (t._cache.coe) return t._cache.coe;
  const coe = new Int32Array(t.nEvents);
  for (let i = 0; i < t.nCases; i++) coe.fill(i, t.off[i], t.off[i + 1]);
  return (t._cache.coe = coe);
}

function actCode(t, name) {
  const i = t.activities.indexOf(name);
  return i;
}

/** [nCases] index of each case's first `code` event, or -1. */
function firstOccurrence(t, code) {
  const out = new Int32Array(t.nCases).fill(-1);
  if (code < 0) return out;
  for (let i = 0; i < t.nCases; i++) {
    for (let j = t.off[i]; j < t.off[i + 1]; j++) {
      if (t.act[j] === code) { out[i] = j; break; }
    }
  }
  return out;
}

/**
 * [nCases] index of the first `code` after event `after[i]` in the same
 * case, or -1. Ordering by index, not timestamp — see the header.
 */
function firstFollowing(t, after, code) {
  const out = new Int32Array(t.nCases).fill(-1);
  if (code < 0) return out;
  for (let i = 0; i < t.nCases; i++) {
    if (after[i] < 0) continue;
    for (let j = after[i] + 1; j < t.off[i + 1]; j++) {
      if (t.act[j] === code) { out[i] = j; break; }
    }
  }
  return out;
}

/**
 * Mean of value(j, i) over events where keep(j), per case; NaN if none.
 *
 * This is where case-weighting happens: a case with three occurrences of
 * an activity collapses to one value here, before any cross-case
 * aggregation. Mirrors engine._per_case_mean.
 */
function perCaseMean(t, keep, value) {
  const out = new Float64Array(t.nCases).fill(NaN);
  for (let i = 0; i < t.nCases; i++) {
    let sum = 0, n = 0;
    for (let j = t.off[i]; j < t.off[i + 1]; j++) {
      if (!keep(j)) continue;
      const v = value(j, i);
      if (Number.isFinite(v)) { sum += v; n++; }
    }
    if (n) out[i] = sum / n;
  }
  return out;
}

/** Previous event's dt within the case, NaN at a case's first event. */
function prevDt(t, j, i) {
  return j > t.off[i] ? t.dt[j - 1] : NaN;
}

/** Start-time delta of event j, or NaN when absent/invalid. */
function sdtOf(t, j) {
  const b = t.buffers.sdt;
  if (!b) return NaN;
  const v = b[j];
  return v === SENTINEL.uint32 ? NaN : v;
}

// ---------------------------------------------------------------------
// Per-case values
// ---------------------------------------------------------------------

export function perCaseValues(t, metric, params) {
  const p = params || {};
  const n = t.nCases;
  const out = new Float64Array(n).fill(NaN);

  switch (metric) {
    case "duration": {
      for (let i = 0; i < n; i++) out[i] = t.dt[t.off[i + 1] - 1] / DAY;
      return out;
    }
    case "eventCount": {
      for (let i = 0; i < n; i++) out[i] = t.off[i + 1] - t.off[i];
      return out;
    }
    case "timeBetween": {
      const a = firstOccurrence(t, actCode(t, p.from));
      const b = firstFollowing(t, a, actCode(t, p.to));
      for (let i = 0; i < n; i++) {
        if (a[i] >= 0 && b[i] >= 0) out[i] = (t.dt[b[i]] - t.dt[a[i]]) / DAY;
      }
      return out;
    }
    case "rework": {
      for (let i = 0; i < n; i++) {
        const seen = new Set();
        let dup = 0;
        for (let j = t.off[i]; j < t.off[i + 1]; j++) {
          if (seen.has(t.act[j])) { dup = 1; break; }
          seen.add(t.act[j]);
        }
        out[i] = dup;
      }
      return out;
    }
    case "actFreq":
    case "actPresence":
    case "actRepeats": {
      const code = actCode(t, p.activity);
      for (let i = 0; i < n; i++) {
        let c = 0;
        if (code >= 0) {
          for (let j = t.off[i]; j < t.off[i + 1]; j++) if (t.act[j] === code) c++;
        }
        out[i] = metric === "actFreq" ? c
          : metric === "actPresence" ? (c > 0 ? 1 : 0)
            : Math.max(c - 1, 0);
      }
      return out;
    }
    case "actSojourn": {
      const code = actCode(t, p.activity);
      return perCaseMeanFor(t, code, (j, i) => (t.dt[j] - prevDt(t, j, i)) / DAY);
    }
    case "actService": {
      if (!t.buffers.sdt) return out;
      const code = actCode(t, p.activity);
      return perCaseMeanFor(t, code, (j) => (t.dt[j] - sdtOf(t, j)) / DAY);
    }
    case "actWaiting": {
      if (!t.buffers.sdt) return out;
      const code = actCode(t, p.activity);
      // Negative waiting = the activity started before the previous event
      // finished. Real concurrency, kept rather than clamped.
      return perCaseMeanFor(t, code, (j, i) => (sdtOf(t, j) - prevDt(t, j, i)) / DAY);
    }
    case "edgeFreq":
    case "edgeTime":
    case "edgeShare": {
      const src = actCode(t, p.from), tgt = actCode(t, p.to);
      if (src < 0 || tgt < 0) {
        return metric === "edgeFreq" ? new Float64Array(n) : out;
      }
      for (let i = 0; i < n; i++) {
        let count = 0, sum = 0, reached = false;
        for (let j = t.off[i]; j < t.off[i + 1]; j++) {
          if (t.act[j] !== src) continue;
          reached = true;
          if (j + 1 < t.off[i + 1] && t.act[j + 1] === tgt) {
            count++;
            sum += (t.dt[j + 1] - t.dt[j]) / DAY;
          }
        }
        if (metric === "edgeFreq") out[i] = count;
        else if (metric === "edgeTime") out[i] = count ? sum / count : NaN;
        // edgeShare: cases that never reach `from` are not in the
        // denominator at all, so they stay NaN rather than becoming 0.
        else out[i] = reached ? (count > 0 ? 1 : 0) : NaN;
      }
      return out;
    }
  }
  if (SERIES_METRICS.includes(metric)) {
    throw new Error(`${metric} is a series metric — call seriesValues()`);
  }
  throw new Error(`Unknown metric ${metric}`);
}

function perCaseMeanFor(t, code, value) {
  if (code < 0) return new Float64Array(t.nCases).fill(NaN);
  return perCaseMean(t, (j) => t.act[j] === code, value);
}

// ---------------------------------------------------------------------
// Aggregation
// ---------------------------------------------------------------------

/** Linear-interpolation percentile — mirrors engine.percentile exactly. */
export function percentile(values, q) {
  const v = Array.from(values).filter(Number.isFinite).sort((a, b) => a - b);
  const n = v.length;
  if (n === 0) return null;
  if (n === 1) return v[0];
  const pos = q * (n - 1);
  const lo = Math.floor(pos), hi = Math.ceil(pos);
  if (lo === hi) return v[lo];
  return v[lo] + (v[hi] - v[lo]) * (pos - lo);
}

export function aggregate(values, kind) {
  const v = Array.from(values).filter(Number.isFinite);
  if (!v.length) return null;
  switch (kind) {
    case "share": return (100 * v.filter((x) => x !== 0).length) / v.length;
    case "sum": return v.reduce((s, x) => s + x, 0);
    case "min": return Math.min(...v);
    case "max": return Math.max(...v);
    case "median": return percentile(v, 0.5);
    case "p90": return percentile(v, 0.9);
    case "avg": return v.reduce((s, x) => s + x, 0) / v.length;
    default: throw new Error(`Unknown aggregation ${kind}`);
  }
}

// ---------------------------------------------------------------------
// Targets
// ---------------------------------------------------------------------

export function targetState(value, target) {
  if (value == null || !target || !target.on) return null;
  const goal = +target.value;
  const warn = target.warn == null ? goal : +target.warn;
  if (target.dir === ">=") {
    if (value >= goal) return "met";
    return value >= warn ? "risk" : "missed";
  }
  if (value <= goal) return "met";
  return value <= warn ? "risk" : "missed";
}

export function worstState(states) {
  const present = states.filter(Boolean);
  return STATE_ORDER.find((s) => present.includes(s)) || null;
}

// ---------------------------------------------------------------------
// Case-level derived columns
// ---------------------------------------------------------------------

export function caseResource(t) {
  const res = t.buffers.res;
  if (!res) return null;
  const out = new Int32Array(t.nCases);
  for (let i = 0; i < t.nCases; i++) {
    const v = res[t.off[i]];
    out[i] = v === SENTINEL[res.constructor === Uint8Array ? "uint8"
      : res.constructor === Uint16Array ? "uint16" : "uint32"] ? -1 : v;
  }
  return out;
}

function caseEnd(t, i) {
  return t.starts[i] + t.dt[t.off[i + 1] - 1];
}

function variantCodes(t) {
  if (t._cache.variant) return t._cache.variant;
  const keys = [];
  for (let i = 0; i < t.nCases; i++) {
    keys.push(Array.from(t.act.subarray(t.off[i], t.off[i + 1])).join(","));
  }
  const counts = new Map();
  for (const k of keys) counts.set(k, (counts.get(k) || 0) + 1);
  // Rank by descending count, ties broken by the key — same order as the
  // Python engine, so v1 means the same variant in both.
  const ranked = [...counts.keys()].sort(
    (a, b) => (counts.get(b) - counts.get(a)) || (a < b ? -1 : a > b ? 1 : 0));
  const order = new Map(ranked.map((k, i) => [k, i]));
  const codes = new Int32Array(keys.map((k) => order.get(k)));
  const labels = ranked.map((_, i) => `v${i + 1}`);
  const seqs = ranked.map((k) =>
    k.split(",").map((c) => t.activities[+c]).join(" → "));
  return (t._cache.variant = { codes, labels, seqs });
}

// ---------------------------------------------------------------------
// Segmentation
// ---------------------------------------------------------------------

/**
 * The activities most cases start and end with.
 *
 * Used to seed the composer's from/to pickers. Defaulting to the first
 * two activities alphabetically ("Amend Assessment" -> "Amend Claim" on
 * ClaimsPaymentLog) picks a pair that never co-occurs, so the composer
 * opens showing "—" and the user's first impression is of a broken
 * metric. The log's dominant start and end give a real number instead.
 */
export function commonEndpoints(t) {
  if (t._cache.endpoints) return t._cache.endpoints;
  const first = new Map(), last = new Map();
  for (let i = 0; i < t.nCases; i++) {
    if (t.off[i] === t.off[i + 1]) continue;
    const a = t.act[t.off[i]], b = t.act[t.off[i + 1] - 1];
    first.set(a, (first.get(a) || 0) + 1);
    last.set(b, (last.get(b) || 0) + 1);
  }
  const top = (m) => [...m.entries()].sort((x, y) => y[1] - x[1])[0];
  const f = top(first), l = top(last);
  const out = {
    from: f ? t.activities[f[0]] : t.activities[0],
    to: l ? t.activities[l[0]] : t.activities[t.activities.length - 1],
  };
  // A one-activity log, or one where the same activity dominates both
  // ends, would otherwise default from === to (a guaranteed zero).
  if (out.from === out.to && t.activities.length > 1) {
    out.to = t.activities.find((a) => a !== out.from);
  }
  return (t._cache.endpoints = out);
}

export function segmentAxes(t) {
  const axes = TIME_AXES.map((a) => ({
    id: a, label: a[0].toUpperCase() + a.slice(1), group: "time",
  }));
  if (t.buffers.res) {
    axes.push({ id: "resource", label: "Resource (first event)", group: "log" });
  }
  axes.push({ id: "variant", label: "Variant", group: "log" });
  for (const a of t.attributes) {
    axes.push({ id: `attr:${a.name}`, label: a.label, group: "attribute" });
  }
  return axes;
}

const pad2 = (x) => String(x).padStart(2, "0");

export function segmentKeys(t, axis) {
  const n = t.nCases;
  if (!axis || axis === "none") {
    return { codes: new Int32Array(n), labels: ["all"] };
  }
  const codes = new Int32Array(n).fill(-1);

  if (axis === "weekday") {
    for (let i = 0; i < n; i++) {
      // getUTCDay is Sunday-first; the engine's week is Monday-first.
      codes[i] = (new Date(t.starts[i] * 1000).getUTCDay() + 6) % 7;
    }
    return { codes, labels: WEEKDAYS.slice() };
  }

  if (axis === "year" || axis === "month" || axis === "quarter") {
    const keys = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      const d = new Date(t.starts[i] * 1000);
      const y = d.getUTCFullYear(), m = d.getUTCMonth();
      keys[i] = axis === "year" ? y : axis === "month" ? y * 12 + m
        : y * 4 + Math.floor(m / 3);
    }
    const uniq = [...new Set(keys)].sort((a, b) => a - b);
    const pos = new Map(uniq.map((k, i) => [k, i]));
    for (let i = 0; i < n; i++) codes[i] = pos.get(keys[i]);
    const labels = uniq.map((k) =>
      axis === "year" ? String(k)
        : axis === "month" ? `${Math.floor(k / 12)}-${pad2((k % 12) + 1)}`
          : `${Math.floor(k / 4)}-Q${(k % 4) + 1}`);
    return { codes, labels };
  }

  if (axis === "resource") {
    const res = caseResource(t);
    if (!res) return { codes, labels: [] };
    return { codes: res, labels: t.resources.slice() };
  }

  if (axis === "variant") {
    const v = variantCodes(t);
    return { codes: v.codes, labels: v.labels };
  }

  if (axis.startsWith("attr:")) {
    const attr = attributeOf(t, axis.slice(5));
    if (!attr) return { codes, labels: [] };
    const buf = t.buffers[attr.buffer];
    if (!buf) return { codes, labels: [] };
    if (attr.type === "integer") {
      if (!attr.bins || !attr.bins.length) return { codes, labels: [] };
      const bins = attr.bins;
      const lo = bins[0].lo, hi = bins[bins.length - 1].hi;
      for (let i = 0; i < n; i++) {
        const v = buf[i];
        if (!Number.isFinite(v) || v < lo || v > hi) { codes[i] = -1; continue; }
        // Bins are [lo, hi) with the last closed on both ends.
        let k = bins.length - 1;
        for (let b = 0; b < bins.length - 1; b++) {
          if (v < bins[b + 1].lo) { k = b; break; }
        }
        codes[i] = k;
      }
      return { codes, labels: bins.map((b) => b.label) };
    }
    const sent = SENTINEL[buf.constructor === Uint8Array ? "uint8"
      : buf.constructor === Uint16Array ? "uint16" : "uint32"];
    for (let i = 0; i < n; i++) codes[i] = buf[i] === sent ? -1 : buf[i];
    return { codes, labels: (attr.values || []).slice() };
  }

  throw new Error(`Unknown segmentation axis ${axis}`);
}

// ---------------------------------------------------------------------
// Filters
// ---------------------------------------------------------------------

export function applyFilters(t, filters) {
  const mask = new Uint8Array(t.nCases).fill(1);
  for (const f of filters || []) {
    const m = oneFilter(t, f);
    for (let i = 0; i < t.nCases; i++) if (!m[i]) mask[i] = 0;
  }
  return mask;
}

function negate(m, op) {
  if (op !== "not") return m;
  const out = new Uint8Array(m.length);
  for (let i = 0; i < m.length; i++) out[i] = m[i] ? 0 : 1;
  return out;
}

function oneFilter(t, f) {
  const n = t.nCases;
  const field = f.field || "";
  const op = f.op || "is";
  const value = f.value;
  const mask = new Uint8Array(n);

  if (field === "contains") {
    const first = firstOccurrence(t, actCode(t, value));
    for (let i = 0; i < n; i++) mask[i] = first[i] >= 0 ? 1 : 0;
    return negate(mask, op);
  }

  if (field === "date") {
    const [lo, hi] = value || [null, null];
    const loT = lo ? Date.parse(lo + "T00:00:00Z") / 1000 : null;
    // Inclusive of the whole final period, matching the Python engine:
    // "2012-12" means through the end of December.
    const hiT = hi ? endOfPeriod(hi) : null;
    for (let i = 0; i < n; i++) {
      const s = t.starts[i];
      mask[i] = (loT == null || s >= loT) && (hiT == null || s < hiT) ? 1 : 0;
    }
    return mask;
  }

  if (field === "resource") {
    const res = caseResource(t);
    if (!res) return mask.fill(1);
    const wanted = (Array.isArray(value) ? value : [value])
      .map((v) => t.resources.indexOf(v)).filter((i) => i >= 0);
    for (let i = 0; i < n; i++) mask[i] = wanted.includes(res[i]) ? 1 : 0;
    return negate(mask, op);
  }

  if (field === "segment") {
    const { codes, labels } = segmentKeys(t, value[0]);
    const k = labels.indexOf(value[1]);
    if (k < 0) return mask;
    for (let i = 0; i < n; i++) mask[i] = codes[i] === k ? 1 : 0;
    return negate(mask, op);
  }

  if (field.startsWith("attr:")) {
    const attr = attributeOf(t, field.slice(5));
    if (!attr) return mask.fill(1);
    const buf = t.buffers[attr.buffer];
    if (!buf) return mask.fill(1);
    if (attr.type === "integer") {
      for (let i = 0; i < n; i++) {
        const v = buf[i];
        if (!Number.isFinite(v)) { mask[i] = 0; continue; }
        let ok;
        if (op === "between") ok = v >= +value[0] && v <= +value[1];
        else if (op === ">") ok = v > +value;
        else if (op === ">=") ok = v >= +value;
        else if (op === "<") ok = v < +value;
        else if (op === "<=") ok = v <= +value;
        else if (op === "is") ok = v === +value;
        else if (op === "not") ok = v !== +value;
        else throw new Error(`Bad op ${op} for numeric attribute`);
        mask[i] = ok ? 1 : 0;
      }
      return mask;
    }
    const vals = attr.values || [];
    const wanted = (Array.isArray(value) ? value : [value])
      .map((v) => vals.indexOf(String(v))).filter((i) => i >= 0);
    for (let i = 0; i < n; i++) mask[i] = wanted.includes(buf[i]) ? 1 : 0;
    return negate(mask, op);
  }

  throw new Error(`Unknown filter field ${field}`);
}

function endOfPeriod(iso) {
  // "2012" -> end of 2012, "2012-12" -> end of December, else next day.
  const parts = String(iso).split("-");
  const d = new Date(Date.parse(
    parts.length === 1 ? `${parts[0]}-01-01T00:00:00Z`
      : parts.length === 2 ? `${parts[0]}-${pad2(parts[1])}-01T00:00:00Z`
        : `${iso}T00:00:00Z`));
  if (parts.length === 1) d.setUTCFullYear(d.getUTCFullYear() + 1);
  else if (parts.length === 2) d.setUTCMonth(d.getUTCMonth() + 1);
  else d.setUTCDate(d.getUTCDate() + 1);
  return d.getTime() / 1000;
}

// ---------------------------------------------------------------------
// Series metrics
// ---------------------------------------------------------------------

export function seriesValues(t, metric, mask) {
  if (!SERIES_METRICS.includes(metric)) {
    throw new Error(`${metric} is not a series metric`);
  }
  const starts = [], ends = [];
  for (let i = 0; i < t.nCases; i++) {
    if (mask && !mask[i]) continue;
    starts.push(t.starts[i]);
    ends.push(caseEnd(t, i));
  }
  if (!starts.length) return [];

  const lo = new Date(Math.min(...starts) * 1000);
  const hi = new Date(Math.max(...ends) * 1000);
  const months = [];
  const d = new Date(Date.UTC(lo.getUTCFullYear(), lo.getUTCMonth(), 1));
  const last = Date.UTC(hi.getUTCFullYear(), hi.getUTCMonth(), 1);
  while (d.getTime() <= last) {
    months.push(new Date(d.getTime()));
    d.setUTCMonth(d.getUTCMonth() + 1);
  }
  const bounds = months.map((m) => m.getTime() / 1000);

  const out = [];
  if (metric === "wip") {
    const ss = starts.slice().sort((a, b) => a - b);
    const es = ends.slice().sort((a, b) => a - b);
    for (let k = 0; k < bounds.length; k++) {
      out.push({
        label: monthLabel(months[k]),
        value: upperBound(ss, bounds[k]) - upperBound(es, bounds[k]),
      });
    }
    return out;
  }
  const which = (metric === "arrivalRate" ? starts : ends)
    .slice().sort((a, b) => a - b);
  for (let k = 0; k < bounds.length; k++) {
    const nextD = new Date(months[k].getTime());
    nextD.setUTCMonth(nextD.getUTCMonth() + 1);
    const next = nextD.getTime() / 1000;
    out.push({
      label: monthLabel(months[k]),
      value: lowerBound(which, next) - lowerBound(which, bounds[k]),
    });
  }
  return out;
}

const monthLabel = (d) => `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}`;

/** Count of entries <= x in a sorted array (numpy searchsorted 'right'). */
function upperBound(sorted, x) {
  let lo = 0, hi = sorted.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (sorted[mid] <= x) lo = mid + 1; else hi = mid;
  }
  return lo;
}

/** Count of entries < x in a sorted array (numpy searchsorted 'left'). */
function lowerBound(sorted, x) {
  let lo = 0, hi = sorted.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (sorted[mid] < x) lo = mid + 1; else hi = mid;
  }
  return lo;
}

// ---------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------

/**
 * Round half away from zero — 5.25 -> 5.3, 1234.5 -> 1235.
 *
 * Mirrors engine.round_half_away. Neither language's default agrees with
 * the other's: Python's round/%.1f round half to even, JS's toFixed
 * rounds half up. Both engines therefore implement this rule explicitly,
 * rather than each printing whatever its runtime happens to do.
 */
export function roundHalfAway(value, digits = 0) {
  if (value == null || !Number.isFinite(value)) return value;
  const m = Math.pow(10, digits);
  return Math.sign(value) * Math.floor(Math.abs(value) * m + 0.5) / m;
}

const withSeparators = (v) => roundHalfAway(v).toLocaleString("en-US");

export function fmt(value, unit) {
  if (value == null || !Number.isFinite(value)) return "—";
  if (unit === "%") return roundHalfAway(value, 1).toFixed(1) + "%";
  if (unit === "d") {
    return Math.abs(value) >= 100
      ? `${withSeparators(value)} d`
      : `${roundHalfAway(value, 1).toFixed(1)} d`;
  }
  return withSeparators(value);
}

/**
 * Garnet heat ramp.
 *
 * This is the one colour the CSS tokens cannot reach — it is a
 * continuous function of the value, not a fixed swatch — so the two
 * themes are spelled out here instead.
 *
 * Light: near-white to garnet, ink text flipping to white past ~55%
 * where the background gets too dark to read against.
 *
 * Dark: the cold end sits just above the card rather than going white
 * (a grid of near-white cells on a dark page is a glare panel, and it
 * would invert the meaning — cold values would shout). The hot end is
 * the true garnet, and light text reads on both ends, so there is no
 * flip.
 */
export function heat(u, dark) {
  const mix = (a, b) => Math.round(a + (b - a) * u);
  if (dark) {
    return {
      bg: `rgb(${mix(34, 143)},${mix(38, 0)},${mix(46, 26)})`,
      fg: "#f0eee9",
    };
  }
  return {
    bg: `rgb(${mix(247, 143)},${mix(236, 0)},${mix(238, 26)})`,
    fg: u > 0.55 ? "#fff" : "#1c1b1a",
  };
}

// ---------------------------------------------------------------------
// Widget computation
// ---------------------------------------------------------------------

export function computeWidget(spec, t, catalog, dashboardFilters) {
  const metric = catalog[spec.metric];
  if (!metric) throw new Error(`Unknown metric ${spec.metric}`);

  const filters = [...(dashboardFilters || []), ...(spec.filter || [])];
  const mask = applyFilters(t, filters);
  let nCases = 0;
  for (let i = 0; i < t.nCases; i++) if (mask[i]) nCases++;

  let unit = UNIT_BY_TYPE[metric.resultType];
  let aggKind = spec.agg || metric.defaultAgg;
  if (metric.resultType === "percent") aggKind = "share";

  const out = {
    id: spec.id, title: spec.title || metric.label,
    viz: spec.viz || "kpi", unit, resultType: metric.resultType,
    nCases, agg: aggKind, sampled: t.sampled,
  };
  if (out.viz === "model") return out;

  if (SERIES_METRICS.includes(spec.metric)) {
    const pts = seriesValues(t, spec.metric, mask);
    out.series = pts;
    out.value = aggregate(pts.map((p) => p.value), aggKind);
    out.text = fmt(out.value, unit);
    out.state = targetState(out.value, spec.target);
    return out;
  }

  const raw = perCaseValues(t, spec.metric, spec.params);
  const values = new Float64Array(t.nCases);
  for (let i = 0; i < t.nCases; i++) values[i] = mask[i] ? raw[i] : NaN;

  const rowsAx = (spec.segment && spec.segment.rows) || "none";
  const colsAx = (spec.segment && spec.segment.cols) || "none";
  const target = spec.target;

  if (rowsAx === "none" && colsAx === "none") {
    return Object.assign(out, kpi(values, aggKind, unit, target, nCases));
  }
  if (rowsAx === "none" || colsAx === "none") {
    const axis = rowsAx !== "none" ? rowsAx : colsAx;
    return Object.assign(out, seriesBy(t, values, axis, aggKind, unit, target));
  }
  return Object.assign(out, grid(t, values, rowsAx, colsAx, aggKind, unit, target));
}

/** [value, state, distribution] for one group — mirrors engine._score. */
function score(values, aggKind, target) {
  if (!target || !target.on || target.mode !== "per_case") {
    const value = aggregate(values, aggKind);
    return [value, targetState(value, target), null];
  }
  const v = Array.from(values).filter(Number.isFinite);
  if (!v.length) return [null, null, null];
  const states = v.map((x) => targetState(x, target));
  const dist = {};
  for (const s of ["met", "risk", "missed"]) {
    dist[s] = (100 * states.filter((x) => x === s).length) / states.length;
  }
  let state = null;
  if (target.shareGoal != null) {
    state = targetState(dist.met, {
      on: true, dir: ">=", value: target.shareGoal,
      warn: target.shareWarn == null ? target.shareGoal : target.shareWarn,
    });
  }
  return [dist.met, state, dist];
}

const g = (x) => (Number.isInteger(x) ? String(x) : String(x));

function kpi(values, aggKind, unit, target, nCases) {
  const perCase = !!(target && target.mode === "per_case");
  const [value, state, dist] = score(values, aggKind, target);
  const u = perCase ? "%" : unit;
  const out = { value, text: fmt(value, u), state, unit: u };
  if (dist) out.distribution = dist;
  if (target && target.on) {
    const arrow = target.dir === ">=" ? "≥" : "≤";
    if (perCase) {
      out.sub = target.shareGoal != null
        ? `goal ≥ ${g(target.shareGoal)}%`
        : `per case ${arrow} ${g(target.value)}`;
    } else {
      out.sub = `target ${arrow} ${g(target.value)}` +
        (u === "d" ? " d" : u === "%" ? "%" : "");
    }
  } else {
    out.sub = `${aggKind} over ${nCases.toLocaleString("en-US")} cases`;
  }
  return out;
}

function seriesBy(t, values, axis, aggKind, unit, target) {
  const { codes, labels } = segmentKeys(t, axis);
  const series = [], states = [];
  for (let k = 0; k < labels.length; k++) {
    const sel = [];
    let any = false;
    for (let i = 0; i < t.nCases; i++) {
      if (codes[i] === k && Number.isFinite(values[i])) { sel.push(values[i]); any = true; }
    }
    if (!any) continue; // an empty segment is not a zero bar
    const [value, state] = score(sel, aggKind, target);
    series.push({
      label: labels[k], value, text: fmt(value, unit), state,
      nCases: sel.length,
    });
    states.push(state);
  }
  const overall = aggregate(values, aggKind);
  return {
    axis, series, state: worstState(states),
    value: overall, text: fmt(overall, unit),
  };
}

function grid(t, values, rowsAx, colsAx, aggKind, unit, target) {
  const R = segmentKeys(t, rowsAx), C = segmentKeys(t, colsAx);
  const live = (i) => Number.isFinite(values[i]);

  const rUsed = [], cUsed = [];
  for (let k = 0; k < R.labels.length; k++) {
    for (let i = 0; i < t.nCases; i++) {
      if (R.codes[i] === k && live(i)) { rUsed.push(k); break; }
    }
  }
  for (let k = 0; k < C.labels.length; k++) {
    for (let i = 0; i < t.nCases; i++) {
      if (C.codes[i] === k && live(i)) { cUsed.push(k); break; }
    }
  }

  // One pass to bucket cases, rather than nRows x nCols passes.
  const rPos = new Map(rUsed.map((k, i) => [k, i]));
  const cPos = new Map(cUsed.map((k, i) => [k, i]));
  const buckets = rUsed.map(() => cUsed.map(() => []));
  for (let i = 0; i < t.nCases; i++) {
    if (!live(i)) continue;
    const r = rPos.get(R.codes[i]), c = cPos.get(C.codes[i]);
    if (r === undefined || c === undefined) continue;
    buckets[r][c].push(values[i]);
  }

  const cells = [], states = [];
  for (let r = 0; r < rUsed.length; r++) {
    const row = [];
    for (let c = 0; c < cUsed.length; c++) {
      const sel = buckets[r][c];
      if (!sel.length) { row.push({ value: null, text: "—", state: null, nCases: 0 }); continue; }
      const [value, state] = score(sel, aggKind, target);
      row.push({ value, text: fmt(value, unit), state, nCases: sel.length });
      states.push(state);
    }
    cells.push(row);
  }
  const overall = aggregate(values, aggKind);
  return {
    rowsAxis: rowsAx, colsAxis: colsAx,
    rows: rUsed.map((k) => R.labels[k]),
    cols: cUsed.map((k) => C.labels[k]),
    cells, state: worstState(states),
    value: overall, text: fmt(overall, unit),
  };
}

/**
 * The goal a widget is scored against, in words. Mirrors
 * engine.target_goal_text.
 *
 * In per_case mode the widget's value is a *share*, so its goal is the
 * share goal — not the per-case threshold. Reading the threshold with
 * the share's unit would print "<= 14%" for a target that actually reads
 * ">= 90% of cases within 14 days": a different claim, not a rounding
 * difference.
 */
export function targetGoalText(spec, catalog) {
  const target = spec.target || {};
  const metric = catalog[spec.metric];
  const unit = metric ? UNIT_BY_TYPE[metric.resultType] : "";
  const suffix = unit === "d" ? " d" : unit === "%" ? "%" : "";
  const arrow = target.dir === ">=" ? "≥" : "≤";

  if (target.mode === "per_case") {
    if (target.shareGoal != null) return `≥ ${g(target.shareGoal)}% of cases`;
    return `per case ${arrow} ${g(target.value)}${suffix}`;
  }
  return `${arrow} ${g(target.value)}${suffix}`;
}

/** The number targetGoalText describes — what a bar measures against. */
export function targetGoalValue(spec) {
  const target = spec.target || {};
  return target.mode === "per_case" ? target.shareGoal : target.value;
}

export function scorecard(specs, t, catalog, dashboardFilters) {
  const rows = [];
  for (const spec of specs) {
    const target = spec.target;
    if (!target || !target.on) continue;
    const w = computeWidget(spec, t, catalog, dashboardFilters);
    rows.push({
      id: spec.id, title: w.title,
      goal: targetGoalText(spec, catalog),
      actual: w.text || "—", value: w.value == null ? null : w.value,
      state: w.state || null, nCases: w.nCases || 0,
    });
  }
  return rows;
}
