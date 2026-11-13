# Sessions & projects — save, share, resume

**Status:** design note, built (0.7.1; `web/sessions/`, the app's **Project**
group in the rail). It describes the feature as it stands and why it was
shaped this way. The registry (§5), its drift guards (§6) and the maintenance
checklist (§17) are held to the code by `tests/test_sessions_registry.py`.

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
- No server-side/cloud project store (Streamlit Community Cloud has no
  persistent server storage). Everything is file-based.
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
   setting is declared once (§5). The app's gather and its restore are both
   checked against that list — never two hand-written lists that can drift.
2. **CI drift guard.** Tests fail if the app saves a setting the registry does
   not know, omits one it does, or never restores one (§6). Adding a setting
   *forces* the work to be finished.
3. **Logical ids, not internal keys.** The file stores stable logical ids
   (e.g. `noise_threshold`), decoupled from Streamlit widget keys (which are
   often log-scoped like `flt_arank::<hash>`). Renaming an internal key never
   breaks the file format.
4. **Versioned schema + migrations.** Files carry `schema_version`; a chain of
   tiny migration functions upgrades old files (§10).
5. **Preserve the unknown.** Unrecognised keys are round-tripped by the
   document layer, not dropped (§10).
6. **Graceful degradation.** A setting that no longer applies (e.g. a rename
   for an activity absent from the re-supplied log) is skipped with a note,
   never a crash (§8).
7. **Isolation.** The file format lives in one Streamlit-free package (§13),
   not sprinkled through the app. The app gathers values, calls the package,
   and seeds its own widgets on load.

## 5. The Session Parameter Registry

Each persistable setting is one declarative entry in
`web/sessions/registry.py`:

```python
@dataclass(frozen=True)
class Param:
    id: str               # stable logical id, the file key
    category: str         # "miner" | "transform" | "performers" | "overlay"
                          # | "csv" | "scenarios" | "simulation" | "family"
                          # | "compare" | "view"
    default: Any = None   # value when absent from a file
```

The registry is value-oriented: it knows *what* is stored, not how to read or
seed a widget. The app gathers the current settings into a `{id: value}`
mapping (`_proj_values`) and calls `collect(values)`, which returns the
JSON-safe `config` block and **raises `KeyError`** on an id that is not
registered or a registered id that is missing. Restoring is the app's
`_apply_project_config(cfg, file_hash, csv_columns)`, which seeds
`st.session_state` for each id present in the file and leaves the widget
default otherwise. (The design sketched per-parameter `get` / `apply` callables on `Param`;
what was built keeps `Param` declarative and leaves the Streamlit-specific
reading and seeding in the app, with §6's guards tying the two together, so
the package stays free of Streamlit and the code exporter can read it.)

`param_ids()` lists the ids and `defaults()` gives the `{id: default}`
mapping.

Example entries — **not a catalogue**; the registry is the list:

- `noise_threshold` (default 0.2), `min_support`, `notation`
  (`"ucm"` | `"bpmn"`), `active_view`.
- `decomposition` — the resolved `applied_decomp` spec (the string `"off"` or
  a sorted list of `[key, value]` pairs), already serialisable. On load the
  dimensions are mapped back to the dropdown choice (auto, a pre-set, or
  Custom with its fields seeded).
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
  3-way scale carried a boolean `overlay_heatmap_global`, mapped on load.
- `csv_columns` — the CSV mapping (case, activity, timestamp, role, resource,
  and an optional sixth day-first flag; older projects saved five). It is
  also recorded on the log reference (§9).
- `filter_spec` — the **semantic** filter spec, a list of `[key, value]`
  pairs (`activity_ranks`, `exclude_activities`, `attr_expr`, `duration_pct`,
  `variant_ranks`, `variant_cap`, `activity_cap`, `time_from`, `time_to`,
  `time_mode`, `rename_map`). Storing the semantic spec (not raw widget
  positions) is robust; `_apply_filter_spec_to_state` reverse-seeds the
  filter widgets from it. Note `rename_map` lives here.

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
  immune to the first.

  `attr_expr` is a ƒ-language predicate string (the same grammar as a custom
  dashboard metric) that keeps the cases it evaluates true for — e.g.
  `attr("Channel") == "Web" and duration() > 5`.
- `family_attrs`, `family_min_cases`, `family_max_values`, `family_bins`,
  `family_include_values`, `family_dedup` — the Family view. Restoring
  `family_attrs` also flags a one-shot auto-mine, because that view mines
  only on a button click and Compare reads the mined family.
- `scenario_strategy` (`variant` | `data-driven`), `scenario_group_name`,
  `scenario_max_loop_iterations`, `scenario_decision_tree_max_depth` — the
  four synthesis controls, so a saved run reproduces the same variants and
  `.jucm`.
- `simulation_mode`, `simulation_scenarios` (the Coverage multiselect),
  `simulation_a` / `simulation_b` (Compare mode) — the Scenarios view's
  highlight, so a resumed project comes back showing what it showed.
  Two rules apply and both were learned the hard way. The **mode is an
  identifier** (`"coverage"` / `"compare"`), never the radio's words — the
  labels come from `_SIM_MODES` through `format_func`, exactly as `active_view`
  does; a label in a file breaks the day the wording changes. And the
  scenarios are stored **by name, not by index**, because a re-mine can rename
  or drop one: the A/B selectboxes clamp an unknown name to their default and
  the Coverage picks are filtered, so a stale selection degrades instead of
  silently highlighting a different scenario. All four are main-area widgets
  in a view that is usually *not* the active one on load, so they save through
  `_sticky_get` and restore through `_sticky_seed` (§7).
- `compare_a` / `compare_b` — the two selected family members (Process A /
  Process B), stored by cell label. These reference *family cells*, so they
  only mean anything once the family is re-mined; they are applied
  best-effort after family mining, and a cell that no longer exists falls
  back to the default (§8).

**Dashboards are not a registry entry.** They live in the browser island's
`localStorage`, not in `st.session_state`, so they travel in their own
top-level `dashboards` block through the bridge of §11.

**Adding a setting later = add one `Param`, gather it, restore it.** That is
the whole maintenance cost (§17), and §6 makes forgetting a step a test
failure.

## 6. CI drift guard

Adding a setting without finishing the job turns CI red, so the contributor
decides consciously. This is the single most important line of defence
against drift. `collect()` enforces it at run time (an unknown or missing id
raises), and three static guards in `tests/test_sessions_registry.py` keep
both *directions* honest by parsing the deployed app without running it:

* the **save** side — the app's `_proj_values` gather dict must have exactly the
  registered ids;
* the **restore** side — every registered id must be referenced where a loaded
  project is applied (`_apply_project_config`), so a Param can't be saved yet
  silently never restored;
* the **filter sub-keys** — the reverse-map `_apply_filter_spec_to_state` must
  restore every key the transform `_apply_log_filters` reads (these live *inside*
  `filter_spec`, so the registry guard alone would miss a new one — e.g. the
  cycle-time `duration_pct` band).

The same file spells out one value per id (`FULL_VALUES`), so a change to the
persisted set is a conscious edit there too, and
`tests/test_release_consistency.py` fails when this document or
[`code_export.md`](code_export.md) names a parameter that has been removed.

The design proposed a different guard — scan every keyed widget and require
it to be registered or on an ignore-list. What was built checks the gather
and the restore instead: there is no ignore-list, and a widget that is never
gathered is simply not persisted.

## 7. Stable widget keys and sticky widgets

A widget's state can only be seeded on load if it has a stable `key=`, so
every persisted widget has one (`cfg_noise`, `cfg_notation`,
`cfg_decomp_preset`, …; log-scoped ones embed the file hash, e.g.
`overlay_nodes::<hash>`).

Two Streamlit behaviours shape how values are read and seeded:

- **Main-area widgets lose their state when their view is not rendered.**
  The Family, Compare and Simulation controls therefore keep a durable
  `_keep::<key>` mirror (`_sticky`). The gather reads it with `_sticky_get`
  — the Save UI is in the sidebar and runs on every view — and a restore
  writes it with `_sticky_seed`, because the raw widget key would be
  discarded before the view is ever opened.
- **A widget that passes `value=` / `index=` cannot also have its key
  pre-set.** Those restore through a one-shot slot (`_project_restore`, read
  with `_rv`), consumed the first time the widget renders.

## 8. Load / resume flow (rehydration)

Streamlit renders top-to-bottom, so a widget's value must be in
`st.session_state` **before** it instantiates. The app defers the work to the
top of the next run:

1. **Load** (`sessions.load(bytes)`, which tells a bundle from a settings
   file by the zip magic) parses and migrates the file and stores it as
   `st.session_state["pending_project"]`, then reruns.
2. At the **top** of the run, a pending project that has its log — a bundle's
   own, or a bundled sample named by the file — attaches it, runs
   `_apply_project_config`, queues the dashboards for the bridge (§11), and
   reruns once more so widgets render pre-filled.
3. **Log matching.** A settings-only project for an uploaded log waits: the
   app asks for the log and applies the project once one is loaded. If its
   hash differs from the recorded `sha256` the project is applied anyway,
   with a note. Activity-name-scoped settings (rename, exclude) degrade
   gracefully — renaming uses `get(name, name)`, filters intersect with what
   exists.

Anything only partially applied is reported as a note under **Resume a saved
project**. Loading a different log afterwards retires the resumed project
(its notes and the uploader's selection).

## 9. File formats & schema

Two artifacts, one schema:

- **Settings file** — `<name>.ucmproj.json`. Small, email-able,
  privacy-preserving (no data).
- **Project bundle** — `<name>.ucmproj.zip` = `project.json` +
  `log/<original file name>`. Self-contained, one-click resume/share. The log
  is stored as the **original bytes** (XES, CSV or zip, with `log.kind`
  recording which); the zip's own DEFLATE does the compression, so there is
  no separate gzip step.

`project.json`:

```jsonc
{
  "format": "pm4py-ucm-project",
  "schema_version": 1,
  "app_version": "0.9.0",          // informational; never used to gate loading
  "created_utc": "",               // reserved; the app leaves it empty
  "log": {
    "source": "sample" | "upload",
    "name": "ClaimsPaymentLog.xes",
    "kind": "xes" | "csv" | "zip",
    "sha256": "…",                  // matches the app's file_hash
    "csv_columns": ["case", "activity", "timestamp", "role", "resource"]
  },
  "config": { /* registry id -> JSON value; unknown ids preserved */ },
  "dashboards": { /* {"version", "registry"} — see §11; omitted if none */ }
}
```

`log.source` says whether the log is one of the app's bundled samples (which
a settings file can then re-attach by name) or an upload. `sha256` lets a
settings file recognise the right log.

## 10. Versioning, migration & compatibility

- **`schema_version`** is an integer bumped only on a breaking change to the
  on-disk shape. Loading runs an ordered chain of migration functions
  (`web/sessions/migrate.py`, `{from_version: fn}`) up to the current
  version; each is a tiny pure function. **The schema is still at version 1
  and the chain is empty** — every change so far has been additive. A file
  from a *newer* schema still loads (its unknown keys are preserved);
  `ProjectDoc.from_newer_app` is then true and the app says so in its load
  notes, since settings it does not know were not applied.
- **Additive changes need no migration.** A new id falls back to its default
  in an old file. The one renamed setting, `overlay_heatmap_global` →
  `overlay_heatmap_scope`, is read by `_apply_project_config` when the new id
  is absent, rather than by a schema migration.
- **Never gate on `app_version`.** It is informational (and useful in bug
  reports). Compatibility is governed by `schema_version` + graceful
  degradation, so any app version opens any project it understands.
- **Forward compatibility by preservation.** `ProjectDoc` keeps unknown
  top-level keys (in `extra`) and unknown `config` keys, and writes them back
  when the same document is serialised again. This holds for the document
  layer (`loads` → `dumps`); the app's **Save** builds a fresh document from
  the live session, so it does not carry forward keys from a file loaded
  earlier that this app version does not know.
- **Backward compatibility by defaults.** Missing keys fall back to the
  widget default, which matches the registry default.
- The dashboards block is versioned on its own (`BRIDGE_VERSION`), and
  `unwrap_registry` also accepts a bare registry without the envelope.

## 11. Dashboards — the bridge

Dashboards live in the browser island and persist to `localStorage`; the
ordinary Python↔island embedding is one-way (Python → island). To include
dashboards in a project there is a **small, versioned bridge**, reusing the
existing dashboard-spec contract so the app and the export can't drift. If
the bridge is unavailable the project simply omits dashboards, which remain
saveable via their own HTML/session-report export.

The bridge is `web/dashboards_bridge/` — a *declared* Streamlit component
(one hand-written static `frontend/index.html`, no build step, no new
dependency). Because it is served from the app's own origin it
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
Edits made in the island do not trigger a Streamlit rerun on their own, so
the Project group has a **Refresh dashboards from browser** button and says
how many dashboards and widgets the save will carry.

## 12. Security & privacy

- **Data, not code.** Project files are JSON (and the log bytes in a
  bundle). Never pickle, never `eval`. Loading checks the `format` marker and
  the presence of a `log` section, and ignores anything it doesn't recognise.
- **Bundle limits.** `read_bundle` rejects a non-zip, a zip without
  `project.json` or without a log under `log/`, a member path that escapes
  the archive (zip-slip), a `project.json` over 32 MiB and an embedded log
  that would inflate past 2 GiB. The log then goes through the same loader as
  an upload.
- **Sharing hygiene.** A settings file carries *no* event data — the
  recommended way to share when the log is sensitive. The bundle *does* carry
  the log, and the Save button's help says so plainly so a user doesn't hand
  off data by accident.

## 13. Module layout

All of it lives in `web/sessions/` — beside the app rather than in the
published library, and with no Streamlit import, so it is testable headless:

- `registry.py` — the `Param` list (the source of truth), `collect`,
  `param_ids`, `defaults`.
- `schema.py` — `ProjectDoc`, `LogRef`, `ProjectError`, `SCHEMA_VERSION`,
  `FORMAT`.
- `io.py` — `dumps`, `loads`, `save_settings`, `save_bundle`, `load`.
- `bundle.py` — the zip container and its safety checks.
- `migrate.py` — the migration chain.
- `dashboards.py` — the versioned envelope for the dashboards block.
- `codegen.py` — the Python exporter ([`code_export.md`](code_export.md)).

The app holds what is necessarily Streamlit's: gathering widget values,
seeding them on load, and the UI.

## 14. Testing strategy (this feature must be tested to stay alive)

- **Round-trip** (`tests/test_sessions.py`): a document survives `dumps` →
  `loads` and a bundle survives `save_bundle` → `load`, unknown keys
  included; a full gather survives `collect` → project file → `loads`.
- **Drift guards (§6):** the gather, the restore and the filter sub-keys.
- **Sticky restore:** a value saved from, or restored before, a view that is
  not open survives (family settings, simulation selection), and a restored
  scenario that no longer exists is dropped.
- **Bundle safety:** non-zip, missing `project.json` and zip-slip inputs are
  rejected.
- **Compatibility:** a file with a future `schema_version` still loads; the
  dashboards envelope is tolerant of a bare registry.

There is no stored fixture per historical `schema_version` yet, because
there has only been one.

## 15. UX surface

Saving is the **Project** group in the rail:

- **Save settings** → `<log>.ucmproj.json`.
- **Save project bundle** → `<log>.ucmproj.zip` (its help notes that it ships
  the data).
- **Export as Python** — see [`code_export.md`](code_export.md).

Loading is **Resume a saved project**, above the log picker so a fresh
session can use it: one uploader accepting either artifact; it applies the
settings, restores or re-requests the log, and lists anything only partially
applied.

## 16. What is not built

- **Browser auto-save** of the config to `localStorage`, so a refresh would
  restore the last session without a file. It could never include the log
  (`localStorage` is limited to about 5 MB).
- **Shareable project links** with data. Out of scope until there is
  persistent server storage.

## 17. Maintenance checklist — "I added a setting, now what?"

1. Give the widget a stable `key=` (and wrap it in `_sticky` if it is a
   main-area widget of a view that is not always rendered — §7).
2. Add one `Param` to `web/sessions/registry.py` (id, category, default).
3. Gather it in the app's `_proj_values` dict.
4. Restore it in `_apply_project_config` (a new key *inside* `filter_spec`
   goes in `_apply_filter_spec_to_state` instead, and needs no `Param`).
5. Add a value for it to `FULL_VALUES` in `tests/test_sessions_registry.py`,
   and run the tests — the drift guards tell you if you missed a step.
6. If an exported script should honour it, emit it in
   `web/sessions/codegen.py` ([`code_export.md`](code_export.md)).

A setting that should *not* persist needs nothing: leave it out of the
registry and the gather.

That is the entire recurring cost. Everything else — files, migrations,
sharing, dashboards — keeps working unchanged.

## 18. Questions settled during the build

- **Filter widgets on load:** only the semantic `filter_spec` is stored, and
  the widgets are reverse-seeded from it — one source of truth.
- **Bundle log format:** the original bytes are preserved and `log.kind`
  records the format; nothing is re-emitted as XES.
- **Compare cells:** stored by label; a label that no longer exists falls
  back to the default cell.
