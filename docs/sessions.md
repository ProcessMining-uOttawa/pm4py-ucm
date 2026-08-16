# Sessions & projects — save, share, resume

**Status:** design (not yet implemented). This document is the spec; it is
meant to be reviewed and to guide implementation in small, testable steps.

**Why this design leans hard on structure:** PM4Py-UCM's web app evolves
quickly — new settings, views and widgets land often. A naïve "gather the
current settings into a dict" would rot within a release: every new setting a
contributor forgets to add would silently vanish from saved projects. The
whole design below exists to make persistence **the default, enforced by a
single registry and a CI guard**, so that resuming a session keeps working as
the app grows.

---

## 1. Goals

- **Resume** an analysis later (same machine, same or a re-supplied log)
  without redoing the setup: log, CSV mapping, renaming, filters, performers,
  performance overlays, decomposition, family attributes, dashboards.
- **Share** a configured analysis with a colleague as a single file.
- Serve *resume* and *share* equally (a deliberate project decision).
- **Be maintainable as the app evolves** — adding a setting must not silently
  break resume, and old project files must keep opening in new app versions.

## 2. Non-goals

- **Do not store derived artifacts** (mined UCMs, scenarios, families,
  reports, filtered-log exports). They are a pure function of *config + log*
  and are already cached (`@st.cache_data`, keyed by `_arg_fingerprint`). A
  project stores *inputs*; opening it recomputes outputs. This keeps project
  files tiny and immune to changes in how a model is mined.
- No server-side/cloud project store in the first phases (Streamlit Community
  Cloud has no persistent server storage). Everything is file-based.
- No execution of code from a project file — project files are **pure data**
  (JSON), never pickle. See §12.

## 3. The core principle: config vs data vs derived

| Tier | Examples | In a project? |
|------|----------|---------------|
| **Config** (the manual effort) | CSV mapping, rename map, filter spec, performers, overlays, decomposition, miner settings, notation, family attrs, dashboards | **stored** |
| **Log** | the XES/CSV bytes | **referenced** (settings file) or **bundled** (project file) |
| **Derived** | UCM, scenarios, family, reports | **never stored** — recomputed |

Resume = *re-apply config + re-attach log*; the caches repopulate.

## 4. Design tenets (how we stay future-proof)

1. **Single source of truth: a Session Parameter Registry.** Every persistable
   setting is declared once (§5). `collect()` and `apply()` iterate the
   registry — never a hand-written per-setting list in two places.
2. **CI drift guard.** A test fails if a keyed config widget is neither in the
   registry nor on an explicit ignore-list (§6). Adding a setting *forces* a
   decision: persist it, or opt out on purpose.
3. **Logical ids, not internal keys.** The file stores stable logical ids
   (e.g. `noise_threshold`), decoupled from Streamlit widget keys (which are
   often log-scoped like `flt_arank::<hash>`). Renaming an internal key never
   breaks the file format.
4. **Versioned schema + migrations.** Files carry `schema_version`; a chain of
   tiny migration functions upgrades old files (§9).
5. **Preserve the unknown.** Unrecognised keys are round-tripped, not dropped,
   so a file written by a *newer* app still round-trips through an older one
   (§10).
6. **Graceful degradation.** A setting that no longer applies (e.g. a rename
   for an activity absent from the re-supplied log) is skipped with a warning,
   never a crash (§8).
7. **Isolation.** All of this lives in one module (§11), not sprinkled through
   the 4000-line app. The app calls `save_project()` / `load_project()` and
   nothing else.

## 5. The Session Parameter Registry

Each persistable setting is one declarative entry. Sketch of the shape (final
form decided in implementation):

```python
@dataclass(frozen=True)
class Param:
    id: str                       # stable logical id, the file key
    category: str                 # "miner" | "filter" | "rename" | "family" | ...
    default: Any                  # value when absent from a file
    # How to read the current value from the app on save:
    get: Callable[[Ctx], Any]
    # How to stage the value for the next run on load (seed session_state):
    apply: Callable[[Ctx, Any], None]
    to_json:   Callable[[Any], Any] = identity   # value -> JSON-safe
    from_json: Callable[[Any], Any] = identity   # JSON -> value
```

`Ctx` carries what the get/apply need — chiefly the current `file_hash` (so a
param whose widget key is `flt_arank::<hash>` can build the right key for the
*loaded* log). Example entries:

- `noise_threshold` — get: read the slider's key; apply: seed it. default 0.2.
- `decomposition` — the resolved `applied_decomp` spec (a `"off"` string or a
  sorted tuple of `(key, value)`), already hashable/serialisable.
- `resource_attribute` — the performers string.
- `overlay_nodes` / `overlay_edges` — the two multiselect lists, stored as the
  user **picked** them. When `overlay_replay` is off the app renders the
  event-based fallbacks instead, but the picks are what gets persisted, so
  turning the replay back on after a resume restores the conserving counts
  rather than silently keeping the fallbacks.
- `overlay_replay` — whether the traversal metrics may replay the log on the
  model (default `True`). The exported script carries it as `OVERLAY_REPLAY`
  and resolves the same fallback at runtime.
- `overlay_heatmap` / `overlay_heatmap_scope` — the heat-map's on/off and its
  scale (`"local"` / `"global"` / `"family"`). A project written before the
  3-way scale carried a boolean `overlay_heatmap_global`, migrated on load.
- `filter_spec` — the **semantic** filter tuple (`activity_ranks`,
  `exclude_activities`, `attr_expr`, `duration_pct`, `variant_ranks`,
  `variant_cap`, `activity_cap`, `time_*`, `rename_map`). Storing the semantic
  spec (not raw widget positions) is robust; `apply` reverse-seeds the filter
  widgets from it. Note `rename_map` already lives here.

  `variant_cap` and `activity_cap` are V6's two **one-click reductions** from
  the cost screen, and both follow one rule: *a one-click reduction records
  what it selected, and never lives in a widget.* `variant_cap` is `(lo, hi,
  base_spec)` — the **cases** that variants `lo…hi` selected on the log
  filtered by `base_spec`, the spec in force when it was clicked.
  `activity_cap` is a tuple of activity **names**.

  Neither is stored as a rank range, for two independent reasons that each
  caused a bug. A rank range is relative to a population, and any other filter
  moves that population — re-reading "the top 2,000 variants" after an
  activity reduction selected every remaining variant, restoring the cases it
  had dropped. And Streamlit owns widget state and may discard it on a rerun
  that changes nothing — answering the replay prompt was enough to reset the
  activity slider to its full range, restoring the whole alphabet. Session
  entries the app owns are immune to the second; naming the selection is
  immune to the first. `attr_expr` is a ƒ-language predicate string (the same grammar as a
  custom dashboard metric) that keeps the cases it evaluates true for — e.g.
  `attr("Channel") == "Web" and duration() > 5`.
- `family` — `selected_attrs`, `min_cases`, `max_values`, `bins`,
  `include_values`, `dedup`.
- `scenarios` — the four synthesis controls, so a saved run reproduces the
  same variants and `.jucm`: `condition_strategy` (`variant` | `data-driven`),
  `group_name` (the scenario-group name), `max_loop_iterations`, and
  `decision_tree_max_depth`.
- `compare` — the two selected members, `cmp_cell_a` and `cmp_cell_b` (Process
  A / Process B). These reference *family cells*, so they only mean anything
  once the family is re-mined; they are applied **best-effort after** family
  mining, and silently skipped if the named cells aren't present (§8). Stored
  by cell label; if labels prove unstable across re-mines we fall back to the
  cell index.
- `dashboards` — **serialised and deserialised** via the island bridge (§11):
  saved to `project.dashboards` on save and posted back to the island on load.
  Unlike the entries above it is not a plain config scalar, so it lives in its
  own top-level `dashboards` block rather than under `config`.
- `notation`, `min_support`, `csv_columns`, `active_view`.

`collect(ctx)` → `{p.id: p.to_json(p.get(ctx)) for p in REGISTRY}`.
`apply(ctx, cfg)` → for each `p`, if `p.id in cfg`: `p.apply(ctx, p.from_json(cfg[p.id]))`; else leave the default. Unknown keys in `cfg` are stashed verbatim (see §10).

**Adding a setting later = add one `Param`.** That is the whole maintenance
cost, and §6 makes forgetting it a test failure.

## 6. CI drift guard

A unit test enumerates the app's keyed config widgets (a small static scan, or
a curated list the app exposes) and asserts:

> every keyed configuration widget is either covered by a registry `Param` or
> listed in `PERSIST_IGNORE` (with a one-line reason).

New setting without a registry entry → red CI → the contributor decides
consciously. This is the single most important line of defence against drift.

Three complementary static guards keep both *directions* honest (see
`tests/test_sessions_registry.py`):

* the **save** side — the app's `_proj_values` gather dict must have exactly the
  registered ids;
* the **restore** side — every registered id must be referenced where a loaded
  project is applied (`_apply_project_config`), so a Param can't be saved yet
  silently never restored;
* the **filter sub-keys** — the reverse-map `_apply_filter_spec_to_state` must
  restore every key the transform `_apply_log_filters` reads (these live *inside*
  `filter_spec`, so the registry guard alone would miss a new one — e.g. the
  cycle-time `duration_pct` band).

## 7. Prerequisite: stable widget keys

Several core widgets currently have **no `key=`** (e.g. `noise_threshold`,
`decomposition_preset`, `notation`, `min_support`, `resource_attribute`), so
their state can't be seeded on load. Step 0 of implementation is to give every
persistable widget a stable, documented key. This is mechanical and low-risk,
and it also makes the drift guard (§6) meaningful.

## 8. Load / resume flow (rehydration)

Streamlit renders top-to-bottom, so a widget's value must be in
`st.session_state` **before** it instantiates. The app already has this
pattern: `pending_pin` and `goto_view` stash a deferred action and consume it
at the top of the next run. Reuse it:

1. **Load** parses the file, validates + migrates it, and stores the config as
   `st.session_state["pending_project"]`, then `st.rerun()`.
2. At the **very top** of the run (before the sidebar), if `pending_project`
   exists: run `apply(ctx, cfg)` to seed every widget's session_state, attach
   the log (bundle) or record the expected `log.sha256` (settings file), pop
   the pending state, and `st.rerun()` once more so widgets render pre-filled.
3. **Log matching.** If the file is settings-only, prompt for the log and
   compare `file_hash`; on mismatch, warn but proceed. Activity-name-scoped
   settings (rename, exclude) degrade gracefully — renaming already uses
   `get(name, name)`, filters intersect with what exists — and any dropped
   reference is reported, not fatal.

## 9. File formats & schema

Two artifacts, one schema:

- **Settings file** — `<name>.ucmproj.json`. Small, email-able,
  privacy-preserving (no data).
- **Project bundle** — `<name>.ucmproj.zip` = `project.json` + `log.xes.gz`
  (or the original CSV, gzipped). Self-contained, one-click resume/share.

`project.json`:

```jsonc
{
  "format": "pm4py-ucm-project",
  "schema_version": 1,
  "app_version": "0.7.0",          // informational; never used to gate loading
  "created_utc": "2026-07-18T...",
  "log": {
    "source": "sample" | "upload",
    "name": "ClaimsPaymentLog.xes",
    "kind": "xes" | "csv" | "zip",
    "sha256": "…",                  // matches file_hash
    "csv_columns": ["case", "activity", "timestamp", "role", "resource"]
  },
  "config": { /* registry id -> JSON value; unknown ids preserved */ },
  "dashboards": { /* island spec payload, versioned — see §11 */ }
}
```

The bundle stores the same `project.json` with `log.source = "upload"` plus the
log file alongside. `sha256` lets a settings file recognise the right log and
lets a bundle verify integrity.

## 10. Versioning, migration & compatibility

- **`schema_version`** is an integer bumped only on breaking schema changes.
  Loading runs an ordered chain of `migrate_v{n}_to_v{n+1}(doc)` functions up
  to the current version. Each migration is a tiny, unit-tested pure function.
- **Never gate on `app_version`.** It is informational (and useful in bug
  reports). Compatibility is governed by `schema_version` + graceful
  degradation, so any app version opens any project it understands.
- **Forward compatibility by preservation.** Unknown top-level and unknown
  `config` keys are retained in memory and **written back on the next save**,
  so a project touched by an older app doesn't lose a newer app's settings.
  (Best-effort; documented as such.)
- **Backward compatibility by defaults.** Missing keys fall back to the
  registry default.

## 11. Dashboards — the one bridge we add (option 2)

Dashboards live in the browser island and persist to `localStorage`; the
Python↔island embedding is currently **one-way** (Python → island, via
`_embed_html`). To include dashboards in a project we add a **small, versioned
bridge**: the island — which already serialises its specs for the HTML/session
export (`dashboard_html`) — emits that same specs JSON back to the Streamlit
host on request (e.g. a postMessage the host reads, or a hidden component
return value). Python stores it under `project.dashboards` on save and posts
it back to the island on load.

Keep the bridge **minimal and versioned**: one message shape
(`{version, specs}`), reusing the existing dashboard-spec contract so the app
and the export can't drift. If the island can't answer (older embed), the
project simply omits dashboards and says so — dashboards then remain saveable
via their own existing HTML/session-report export.

**As implemented.** The bridge is `web/dashboards_bridge/` — a *declared*
Streamlit component (one hand-written static `frontend/index.html`, no build
step, no new dependency). Because it is served from the app's own origin it
shares the island's `localStorage`, so it reads the island's registry
(`pm4py-ucm:dash:{file_hash}:set`) straight back to Python on **save**, and
writes a resumed project's registry there on **load** (idempotently, once per
restore token). The versioned envelope — `{"version", "registry"}` — lives in
`web/sessions/dashboards.py` (`wrap_registry` / `unwrap_registry`, Streamlit-
free and unit-tested). The component renders invisibly in the **main** area
(not the sidebar, which unmounts when collapsed) so save-capture and
restore-write are always live. One wrinkle it handles: Streamlit keeps an
unchanged island iframe across reruns, so after a same-log restore the app
bumps a *restore generation* appended to the island HTML, forcing a single
reload so the on-screen dashboards refresh without the user navigating away.

## 12. Security & privacy

- **Data, not code.** Project files are JSON (and gzipped log bytes in a
  bundle). Never pickle, never `eval`. Loading validates types against the
  schema and ignores anything it doesn't recognise.
- **Bundle limits.** Cap the embedded log size; validate it parses as XES/CSV
  before use; guard the zip against path traversal (the app already has
  `_extract_xes_from_zip` with zip-slip protection to reuse).
- **Sharing hygiene.** A settings file carries *no* event data — the
  recommended way to share when the log is sensitive. The bundle *does* carry
  the log; the Save UI should say so plainly so a user doesn't hand off data by
  accident.

## 13. Module layout

All persistence logic in one place, e.g. `pm4py_ucm/algo/sessions/`:

- `registry.py` — the `Param` list (the source of truth).
- `schema.py` — dataclasses, `schema_version`, validation.
- `io.py` — `save_project()`, `load_project()`, bundle zip read/write.
- `migrate.py` — the migration chain.

The web app imports `save_project` / `load_project` and renders the UI; it
holds none of the persistence knowledge itself. This isolation is what lets the
feature be maintained without spelunking the app.

## 14. Testing strategy (this feature must be tested to stay alive)

- **Round-trip:** build a config → `collect` → `save` → `load` → `apply` →
  `collect` again → assert equal. Runs headless (no browser) against a
  registry-driven fake `Ctx`.
- **Drift guard (§6):** every keyed config widget is registered or ignored.
- **Migration:** a stored fixture per historical `schema_version` loads and
  upgrades to current.
- **Graceful degradation:** load a project against a *different* log; assert
  warnings, no crash, sensible partial application.
- **Bundle safety:** oversized / malformed / zip-slip inputs are rejected.

## 15. UX surface (initial)

A small **Project** group in the rail:

- **Save settings** → `<log>.ucmproj.json`.
- **Save project bundle** → `<log>.ucmproj.zip` (with a "this includes the
  event log" note).
- **Load project** → one uploader accepting either; applies settings, restores
  or re-requests the log, reports any skipped settings.

## 16. Phased rollout

1. **P0 — keys + registry + round-trip test.** ✅ *Done.* Give widgets keys
   (§7), build the registry (§5), the drift guard (§6), and the headless
   round-trip test. No UI yet. This is the risky/foundational part; land it
   first.
2. **P1 — Save/Load UI, settings file + bundle** (§9, §15), log matching (§8),
   migrations scaffold (§10). ✅ *Done.* Dashboards saved *separately* for now.
3. **P2 — dashboards bridge** (§11): fold dashboards into the project.
   ✅ *Done* — implemented as `web/dashboards_bridge/` (a small bidirectional
   component) plus `web/sessions/dashboards.py` (the versioned envelope).
4. **P3 (optional) — browser auto-save** of the config to `localStorage` so a
   refresh restores the last session without a file (log re-prompt on hash
   mismatch).
5. **P4 (optional, needs a backend) — shareable project links** with data.
   Out of scope until there's persistent server storage.

## 17. Maintenance checklist — "I added a setting, now what?"

1. Give the widget a stable `key=`.
2. Add one `Param` to `registry.py` (id, default, get, apply).
3. If it should *not* persist, add it to `PERSIST_IGNORE` with a reason.
4. Run the tests — the drift guard tells you if you missed it.

That is the entire recurring cost. Everything else — files, migrations,
sharing, dashboards — keeps working unchanged.

## 18. Open questions

- **Filter widgets on load:** re-seed the individual filter widgets from the
  semantic `filter_spec`, or store the raw widget values too? (Leaning:
  semantic only, reverse-seed — one source of truth.)
- **Auto-save scope (P3):** whole config, or config-minus-log only? Size limits
  of `localStorage` (~5 MB) rule out the log regardless.
- **Bundle log format:** always re-emit as XES.gz for portability, or preserve
  the original bytes (CSV/zip)? (Leaning: preserve original + record `kind`.)
