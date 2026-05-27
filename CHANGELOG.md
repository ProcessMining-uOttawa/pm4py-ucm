# Changelog

All notable changes to **pm4py-ucm** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1] — 2026-05-20

### Fixed (resource mining — silent attribute override)

- **`resource_attribute="org:role"` and `resource_attribute="org:resource"`
  produced identical component vocabularies on logs that carried both
  XES attributes.** Root cause was an enum-aliasing collapse in
  `pm4py_ucm.algo.discovery.resources.algorithm.Variants`: both
  `ACTIVITY_ATTRIBUTE` and `ROLE_THEN_RESOURCE` had the same value
  (the `activity_attribute` module), so `enum.Enum` silently made the
  second an alias for the first. The guard
  `if variant is Variants.ROLE_THEN_RESOURCE` then fired on *every*
  call to `apply()` / `distinct_components()` and injected the
  role-first `attribute_priority = ["org:role", "org:resource",
  "org:group"]` list — which overrode the user's
  `attribute="org:resource"` because the priority list takes
  precedence in the underlying variant.

  Fix: give the two `Variants` members distinct string values and
  resolve to the backend module via a separate
  `_VARIANT_BACKENDS` lookup table. The variant identity check
  in `apply()` / `distinct_components()` now works correctly,
  so the role-first priority is only injected when the caller
  explicitly asks for the `ROLE_THEN_RESOURCE` variant.

  Three regression tests in `tests/test_resources.py` lock in the
  fix: same log, two attributes, distinct vocabularies; distinct
  activity bindings; and the enum members are now distinct
  identities. Visible effect: on `ClaimsPaymentLog.zip`,
  `resource_attribute="org:resource"` now produces 58 components
  vs `"org:role"`'s 10 (previously both produced 10).

## [0.3.0] — 2026-05-20

### Added (decomposition)

- **`on_alternative` boundary rule.** Each branch of an `×` (XOR) or
  `∨` (OR) operator becomes its own plug-in map (symmetric to
  `on_parallel`). The OR-fork / OR-join stays on the parent map; the
  alternative bodies move into per-branch plug-ins. Included in
  `AUTO_DEFAULTS` and `AGGRESSIVE_DEFAULTS` as `True`. Exposed in the
  web app's Decomposition - advanced expander as a checkbox.

### Changed (decomposition)

- **Cap-induced cuts under `×` / `∨` use the same first-to-last
  naming recipe as parallel branches** instead of the dull
  `"sub <first-label>"` fallback. Stub names now read e.g.
  `"alpha to delta"` rather than `"sub alpha"`.

### Fixed (decomposition)

- **Loop at the *root* of the tree is now extracted under `on_loop`.**
  When the outermost operator of the input tree is `*` and `on_loop`
  is enabled, the tree is wrapped in a synthetic single-child
  sequence so the loop becomes a cut candidate. The root map gets a
  single stub pointing to the loop plug-in (instead of having the
  full loop machinery — OrFork / OrJoin / body — drawn inline, which
  was the prior behaviour). When `on_loop` is off, behaviour is
  unchanged (the loop renders inline).

### Notes

- Bumped to a **minor version** (0.2.x → 0.3.0) because the
  decomposition `auto` / `aggressive` presets now extract XOR/OR
  branches by default — output of `discover_ucm_inductive(log,
  decomposition="auto")` on logs with alternative paths will differ
  from earlier releases (more, smaller plug-in maps). The `"off"`
  path is unchanged and byte-stable with all prior releases.

## [0.2.1] — 2026-05-17

### Added (web v1.5)

- **Bundled sample logs.** A new *Sample log* tab in the upload area
  lets users pick a pre-bundled XES (zipped) without having to find
  their own event log. Two ship out of the box
  (`IssueTrackerSyntheticLog.zip`, `ClaimsPaymentLog.zip`); drop more
  files into `web/samples/` to extend the list — they're picked up
  automatically on next start.
- **ZIP archives are first-class inputs.** The file uploader accepts
  `.zip` alongside `.xes` / `.xes.gz` / `.csv`. The miner extracts
  the first `.xes` / `.xes.gz` entry inside, with zip-slip protection
  (entries with `..` or absolute paths are rejected).
- **Per-phase mining progress.** A multi-step `st.status` panel
  reports the current phase (*Reading CSV* → *Formatting events* →
  *Discovering process tree* → *Converting to UCM* → *Writing
  .jucm*), so a 100k+ event log no longer looks hung during the
  multi-minute inductive-miner step.

### Changed (hardening for public deployment)

- **`.streamlit/config.toml`** caps `maxUploadSize` at 75 MB and turns
  off Streamlit's usage telemetry.
- **Pillow's decompression-bomb guard** is now raised to 1 billion
  pixels (was disabled) — keeps protection while still permitting
  realistic mined UCMs.
- **Download filenames are sanitised** to `[A-Za-z0-9._-]`.
- **Mining failures** surface a clean one-line error inline plus an
  expandable *Show technical details* panel, instead of dumping a
  raw traceback to the page.
- **CSV reads use `low_memory=False`** so columns with mid-file dtype
  changes no longer trigger `DtypeWarning` or downstream type
  confusion (root cause of an earlier "import hangs" report).

### Fixed (web bugs)

- **Notation switch no longer flashes "Mining UCM..."** The
  `st.status` panel was created unconditionally around the cached
  `_mine` call and rendered briefly even on instant cache hits.
  An arg-fingerprint check now detects guaranteed cache hits before
  the call and skips the status panel entirely; only genuine
  cache misses surface a status panel / spinner.
- **Role / Resource column selectors no longer reset unexpectedly.**
  The per-rerun "defensive" `_seed_csv_selectors(only_invalid=True)`
  pass was replaced by a strict per-file-hash seeding gate — the
  CSV section seeds the selectors exactly once per uploaded file
  and never overwrites them afterwards. A safety net reseeds an
  individual key only if its stored value is no longer a valid
  option for the current file.
- **Decomposition Apply no longer requires re-confirming the CSV
  column mapping.** Once a column mapping has been applied, pending
  edits to the selectors show a warning + remine button but do not
  block other settings from triggering a remine — mining continues
  against the last-applied column mapping.
- **"Apply column mapping" now reliably starts mining.** The bug
  where the post-click rerun looped back to the *Click Apply…*
  prompt (because `st.file_uploader` returns the same UploadedFile
  on every rerun, and the unconditional reset in the
  `if uploaded is not None:` block fired afterward) is fixed by
  hashing the bytes and only resetting state on a genuinely new
  file.

### Added (inductive miner)

- **Noise-threshold slider** in the sidebar exposes the IMf
  (Inductive Miner — infrequent) threshold. Default 0.2 (the common
  practical default in PM4Py tutorials), range 0.0 – 1.0. The web
  layer pre-mines the process tree with the chosen threshold and
  hands it to `discover_ucm_inductive` via `parameters["process_tree"]`
  — no changes to the package's public API.

### Changed (UX)

- **Updated app caption** to mention CSV alongside XES and the
  sample-log option: "Mine a Use Case Map model from an XES or CSV
  event log and export it to jUCMNav, or to PNG files with BPMN or
  UCM views. Choose an existing log or upload your own."

## [0.2.0] — 2026-05-17

### Added (web front-end)

- **Streamlit web interface** in `web/`. Upload an XES (`.xes` /
  `.xes.gz`) or CSV event log, tune the inductive miner / decomposition /
  performer settings interactively, preview the mined UCM in either UCM
  or BPMN notation, and download the rendered PNG plus the `.jucm` file.
  Mining and rendering are cached separately so toggling notation
  re-renders without re-mining; decomposition advanced overrides and CSV
  column mappings are buffered behind explicit "Apply" buttons so the
  user can stage multiple changes before triggering a remine. Ships with
  Streamlit Community Cloud deployment files (`web/requirements.txt`,
  `web/packages.txt`). See [`web/README.md`](web/README.md) for the full
  walkthrough.

### Changed (UCM-only PNG polish)

- **Thicker UCM paths and contours.** Path penwidth raised from
  graphviz's default `1.0` to `2.6` for the UCM style only — matches
  jUCMNav's heavier line weight and reads more clearly through the
  responsibility-marker squares. Component-cluster border raised to
  `3.0` pt for actors / `2.2` pt otherwise; stub diamond contour
  raised to `2.5` pt. BPMN style keeps the lighter defaults.
- **Bold responsibility and stub labels.** UCM-style `RespRef` and
  `Stub` names render with graphviz HTML `<B>…</B>` labels so they
  stand out from path-line decorations. BPMN style unchanged.
- **Component-reference label sizes tuned.** Bold (`Helvetica-Bold`)
  cluster labels in both styles, sized at `DEFAULT_FONT_SIZE + 3`
  for UCM and `+4` for BPMN.

- **Thicker UCM paths.** Graph-level `penwidth` raised from
  graphviz's default `1.0` to `1.8` for the UCM style only —
  matches jUCMNav's heavier line weight and reads more clearly
  through the responsibility-marker squares. BPMN keeps the
  default 1.0 pt line.
- **Larger, bolder component labels in UCM.** Cluster label
  fontsize bumped from `DEFAULT_FONT_SIZE + 1` to
  `DEFAULT_FONT_SIZE + 5` for the UCM style only; Helvetica-Bold
  unchanged. BPMN keeps the more restrained `+1` pt.

### Fixed (UCM RespRef marker — continuous path)

- The UCM `RespRef` marker is now a small filled **black square
  node** rather than a graphviz `box`-arrowhead glyph. The previous
  approach (invisible point + `arrowhead=box` on incoming edges)
  left a visible white-space gap between the line and the marker
  because `splines=spline` routes around the (still-present) node
  bbox and graphviz places arrowheads with a small offset from
  the target. With a real square node and `arrowhead=none` on the
  edges that touch it, adjacent path segments now meet at the
  square's bbox boundary and the line reads as uninterrupted
  through the marker. The activity name still floats as an
  `xlabel`; BPMN style unchanged.

### Fixed (PNG arrow direction + DirectionArrow rendering)

- **`splines=curved` reverted to `splines=spline`**. The curved
  variant routed rank-back edges (e.g. the redo branch of a loop)
  with their arrowhead at the wrong end — the UCM `box` marker and
  BPMN normal arrowhead would land on the empty point upstream of
  the OR-join instead of on the OR-join itself. `splines=spline`
  (graphviz's b-spline routing around nodes) keeps direction
  consistent.
- **`DirectionArrow` nodes now render as edge-coloured pixels in
  PNG**, identical to `EmptyPoint`. When a `.jucm` produced by
  this exporter is re-imported, the two empty points before a
  loop's OR-join come back as `DirectionArrow` objects (per the
  exporter's promotion rule). Rendering them as the previous
  `rarrow` shape produced large duplicate arrow glyphs on top of
  the path lines, which already carry their own arrowheads.
  Edges into DirectionArrows also drop their arrowhead (matching
  the EmptyPoint treatment), so re-imported models look the same
  as the originals.

### Changed (UCM × marker + label placement)

- **PNG**: the × glyph that used to float above the path as plaintext
  has been replaced by a graphviz `box` arrowhead drawn at the end
  of every edge entering a RespRef. The marker now sits *on* the
  path line instead of as separate text floating above it. The
  RespRef node itself is invisible (`shape=point` with transparent
  colours); the activity name floats as an `xlabel`, both for
  bound and unbound RespRefs. BPMN style is unchanged.
- **PNG**: wider graphviz spacing (`nodesep` 0.40 → 0.75, `ranksep`
  0.55 → 1.00) so external (`xlabel`) names have room to breathe
  instead of overlapping adjacent paths and component boxes.
- **`.jucm`** label-placement heuristic is now bidirectional and
  more aggressive. For every unbound `RespRef` / `Stub`, the
  exporter counts neighbours in a tight column directly above
  *and* below the symbol (within ±80 px x and ±80 px y, up from
  the previous ±35 / ±60). The label is then placed on the
  quieter side: `deltaY=-55` for below (was -20), or `deltaY=+40`
  to push it further above when below is more crowded. Default
  (no override) only fires when both sides are clear.

### Changed (UCM colour + line crossing + arrow visibility)

- **Per-component colours in `.jucm`.** Each `<components>` definition
  now carries `lineColor`, `fillColor` and `filled="true"` attributes
  with the same hashed pastel palette the PNG visualizer uses —
  RGB-triplet format (`r,g,b` decimal) matching jUCMNav's
  convention. Example:
  `<components name="Claims Administrator" id="145" lineColor="0,64,128" fillColor="160,255,255" filled="true" contRefs="42"/>`.
  Colour is attached at the URN-level definition (not per
  ComponentRef) so every reference to the same actor inherits the
  same colour. The hash function moved to
  `pm4py_ucm.objects.ucm.util.component_colors.component_color()` and
  is shared between the exporter and the visualizer.
- **Path lines visually cross the × glyph** in PNG. Edges with a
  UCM-style `RespRef` target now also set `headclip=false`; edges
  with a `RespRef` source set `tailclip=false`. Both segments extend
  to the bbox centroid, so the two halves meet at the × instead of
  a few pixels away. `_BPMN_STYLES` unchanged.
- **Visible arrows into OR-joins / OR-forks** in PNG. The graph-level
  `arrowsize` raised from `0.7` to `1.0` (graphviz default) so
  arrowheads at the tiny OR-join dots stay legible, and an explicit
  `dir=forward` ensures back-edges (e.g. the redo branch of a loop)
  always draw their arrowhead at the *target* side even when
  graphviz routes the curve "backwards" in rank space.

### Changed (UCM PNG polish)

- **Per-component pastel colours.** Component-cluster fill and border
  are now chosen deterministically from a 12-entry professional
  pastel palette via MD5 hash of the component's name, so the same
  team gets the same colour across every map in one render and
  across runs. Actors keep the bold outline that distinguishes them
  but draw from the same hashed palette.
- **Bold component names** in the PNG (cluster labels switched to
  `Helvetica-Bold`). The `.jucm` is untouched — jUCMNav's own font
  settings stay in charge there.
- **Continuous path line through the × glyph.** UCM-style edges that
  terminate at a `RespRef` now drop their arrowhead, so the path
  reads as an unbroken line crossing the × marker rather than
  arrowing into a box and arrowing out again. BPMN style is
  unchanged (its boxed activities are valid flow destinations).
- **Smoother edges** via `splines=curved` on the graphviz top-level
  graph (was `splines=spline`). Produces softer bezier-style
  routing.

### Changed (UCM label placement)

- **PNG (UCM style)**: unbound `RespRef` and `Stub` elements now use
  graphviz's external `xlabel` for their name (with `forcelabels=true`
  on the graph) so the activity label floats next to the symbol
  rather than sitting on the path line. RespRefs/Stubs *inside* a
  ComponentRef cluster keep the compact inline label — the cluster
  already gives them clear space.
- **PNG (UCM style)**: component-cluster labels are pinned to the
  top-left (`labeljust=l, labelloc=t`) so the name stays away from
  path lines crossing through the middle of the rectangle.
- **`.jucm` exporter**: unbound `RespRef` and `Stub` labels get a
  positive `deltaY` (placing them below the symbol) when the model
  has a sibling element directly above — typically a parallel branch
  one row up. Bound elements and elements whose "above" region is
  clear keep the default `<label/>` so jUCMNav renders the name in
  its standard position above the symbol. ComponentRef labels are
  untouched: jUCMNav's default already places them at the top-left.

### Added

- **Hierarchical decomposition.** A new `decomposition=` keyword argument
  on `discover_ucm_inductive(...)` and `convert_to_ucm(...)` splits the
  result into a *root* UCM map plus *plug-in* (sub-)maps connected by
  UCM `Stub` nodes via the existing `PluginBinding` machinery (no model
  changes required). Three combinable boundary rules drive the split:
  - `on_root_sequence` — each top-level `->` child becomes a plug-in.
  - `on_parallel` — each `+` branch becomes a plug-in.
  - `on_loop` — each `*` operator's entire expansion becomes a plug-in.

  Layered safety parameters: `max_leaves_per_map` (hard cap on any
  single map's leaf count, recursively enforced), `min_leaves_to_decompose`
  (floor — small subtrees stay inlined), and `balance_ratio` (sibling
  share threshold — prevents a tiny child of a dominant sibling from
  being pulled into its own near-empty plug-in).

  Accepts `None` / `"off"` (default — single map, byte-stable with
  pre-change exports), `"auto"`, `"aggressive"`, or a dict that merges
  with the `"auto"` defaults. Unknown keys raise `ValueError`.
- **Stacked PNG rendering.** When a UCM contains multiple maps,
  `view_ucm(ucm)` and `save_vis_ucm(ucm, ...)` render every map
  vertically stacked into a single PNG — root map at the top, plug-in
  maps below in pre-order DFS, each panel carrying a title strip with
  its name and separated by a thin horizontal rule. The composite is
  produced via Pillow.
- **Map filter for visualisation.** Both visualisation entry points
  accept a `map="name"` kwarg to render exactly one map's panel — the
  existing single-map UX, scoped to a named plug-in.
- **Stub captions.** Bound stubs gain a small `→ <plug-in name>`
  external label in both the UCM and BPMN styles so the reader can
  follow each stub to its plug-in map.

### Changed

- `Pillow>=10` is now a runtime dependency (used for the stacked PNG
  composite). It is small and pure-Python.
- The process-tree converter's internal `_attach` now writes into an
  explicit `UCMmap` argument rather than the URN container's default
  map. The visible single-map behaviour is unchanged and byte-stable
  against pre-change exports (verified by a pinned `.jucm` fixture).
- **PNG background defaults to white** instead of transparent.
  Multi-map composites previously produced black-on-white panels when
  Pillow converted transparent regions to RGB. Pass
  `parameters={"bgcolor": "transparent"}` to the visualizer to opt
  back in; the composite now alpha-blends correctly in that case too.
- **Edge branch-condition labels are hidden by default** in the PNG
  visualizer. Synthetic conditions emitted by the process-tree
  converter (`redo`, `exit`, `branch0`…) carry no domain information
  for the reader and clutter the diagram. The `.jucm` export still
  carries them — jUCMNav users can rename or delete them there. Pass
  `parameters={"show_conditions": True}` to the visualizer to render
  them in the PNG.

### Fixed

- Plug-in maps now receive auto-layout coordinates in the exported
  `.jucm`. The graphviz layouter was previously called with a fixed
  `map_index=0`, leaving every plug-in map's nodes at the origin.
- `discover_ucm_inductive` now accepts a string path to an XES file
  (already advertised by the docstring); the path is materialised via
  `pm4py.read_xes` before being passed to the inductive miner and the
  resource miner.
- **ComponentRef propagation through Stubs.** When decomposition
  pushed every RespRef into plug-in maps, parent maps ended up with
  only Stubs and no ComponentRefs — jUCMNav then drew the root with
  no actor context. Each bound stub now surfaces its plug-in's
  components on its parent map (transitively, through nested stubs);
  when the plug-in uses exactly one component, the parent-side stub
  is drawn inside that component's rectangle.
- **Resource discovery is now on by default** in
  `discover_ucm_inductive`. When the log carries any of the standard
  XES "who" attributes (`org:resource` / `org:role` / `org:group`),
  components are generated even when the caller doesn't pass
  `resource_attribute` explicitly. Pass `resource_attribute=False` to
  opt out, or pass a specific attribute name to override the fallback
  list.
- **Wrapped labels rendered with single line spacing** in the PNG.
  The shared name-wrap helper joins lines with `\r\n` to match
  jUCMNav's encoding, but graphviz interprets `\r` and `\n` as two
  line breaks, double-spacing every wrapped label. The visualizer
  now normalises to `\n` before rendering.

### Changed (visualization)

- **6-word cap** on derived plug-in / stub names. Long
  ``first to last`` sequences are truncated to the first 6
  whitespace-separated words so stub captions stay readable.
- **No more ``→ <plugin-name>`` caption** under bound Stubs. The
  stub's own name is sufficient context; the arrow caption added
  clutter, especially in stacked multi-map PNGs.
- **Bold, slightly larger font for map names.** The stacked-PNG
  title strips load a bold TrueType variant and use 18pt (was
  16pt); cluster labels in multi-map single-Digraph mode use
  `Helvetica-Bold` at +3pt over the node-label size.
- **BPMN end point redesigned.** The previous styling was a thick-
  bordered black filled circle, visually identical to the start
  point. The end point is now a thick-bordered *white-filled*
  circle with a small black bullet in the centre — the BPMN
  Terminate End Event look.
- **BPMN stub gains the ``⊞`` decomposition marker** below the
  name, matching the BPMN sub-process convention. The UCM style
  continues to rely on the diamond shape alone.

## [0.1.0] — 2026-05-13

Initial public release.

### Added

- **Object model** mirroring the jUCMNav metamodel — `UCM` container with
  `Responsibility`, `ComponentElement`, `UCMmap`, the full `PathNode`
  hierarchy (`StartPoint`, `EndPoint`, `RespRef`, `OrFork`, `OrJoin`,
  `AndFork`, `AndJoin`, `Stub`, `WaitingPlace`, `Timer`, `Connect`,
  `DirectionArrow`, `FailurePoint`, `Anything`, `EmptyPoint`),
  `NodeConnection`, `ComponentRef`, `PluginBinding` (with `InBinding` /
  `OutBinding`), and a `Condition` value-object.
- **Process-tree → UCM converter** producing standard UCM shapes for
  sequence, XOR, parallel, and loop operators, followed by an empty-point
  simplification pass.
- **jUCMNav `.jucm` exporter and importer** — XMI 2.0 emit/parse with
  byte-stable round-trip against jUCMNav's own output, including
  bidirectional `respRefs` / `contRefs` back-references, anonymous
  `NodeConnection` XPath fragments, and full plug-in binding fidelity
  (`parentStub`, `inBindings`, `outBindings`).
- **Inductive UCM discovery** — `discover_ucm_inductive(log)` mines a
  UCM from an event log end-to-end.
- **Resource mining** — discovers `{activity: performer}` from an event
  log's `org:resource` / `org:role` / `org:group` attributes; binds
  responsibilities to component definitions both semantically
  (`Responsibility.performer`) and visually (`RespRef.cont_ref →
  ComponentRef`). Configurable aggregation (`mode`, `first`, `unbound`,
  `all`) and attribute priority.
- **Component vocabulary discovery** — separately surfaces every actor
  seen in the log, so URN-level component definitions exist for actors
  with no per-activity majority binding.
- **Swim-lane layout** — each top-level component gets an exclusive
  horizontal Y-band; nested children inhabit sub-bands of their parent's
  band. Non-overlap and containment are geometric guarantees, not
  constraint-satisfaction problems.
- **Two layout engines** — a built-in Sugiyama-style layouter and a
  graphviz-based layouter that uses the same engine as the PNG
  rendering, so `.jucm` files lay out in jUCMNav the way they look in
  the PNG. Graphviz-based is the default; falls back transparently when
  the graphviz binary is unavailable.
- **Two PNG styles** — `style="ucm"` (filled circle starts, perpendicular
  bar ends, × responsibilities, bars for AND-fork/join, dots for
  OR-fork/join, diamonds reserved for stubs) and `style="bpmn"`
  (activity boxes, gateway diamonds with `X`/`+`, thick-border end
  events).
- **Multi-line label rendering** in `.jucm` exports via `wrap_name()`,
  preserved as unbroken logical names on import.
- **Routing empty points** inserted around forks and joins for smoother
  edges and more layout flexibility (idempotent).
- **High-level API** — `read_ucm`, `write_ucm`, `discover_ucm_inductive`,
  `convert_to_ucm`, `view_ucm`, `save_vis_ucm`, `discover_components`,
  `bind_performers`.
- **Demo** — synthetic 1000-event issue-tracker XES log plus a one-shot
  `mine_and_export.py` script that runs the full pipeline.
- **Test suite** — 108 tests across object model, conversion, export /
  import, visualization, layout, graphviz layout, resources, name
  wrapping, routing points, and stub bindings. Three tests skip
  gracefully when the graphviz binary is unavailable.

### Performance

- O(1) membership checks in `UCMmap` (parallel `set`s next to ordered
  lists) and a linear-pass `simplify_empty_points` eliminate quadratic
  hot spots; the full NASA Java-instrumentation pipeline (78,772 events
  / 2,616 distinct activities) drops from 6.8s to 1.0s end-to-end.

### Notes

This release was developed across four engineering sessions; see
`project-history.md` (if shipped separately) for the development arc
and design decisions in retrospect.

[0.3.1]: https://github.com/ProcessMining-uOttawa/pm4py-ucm/releases/tag/v0.3.1
[0.3.0]: https://github.com/ProcessMining-uOttawa/pm4py-ucm/releases/tag/v0.3.0
[0.2.1]: https://github.com/ProcessMining-uOttawa/pm4py-ucm/releases/tag/v0.2.1
[0.2.0]: https://github.com/ProcessMining-uOttawa/pm4py-ucm/releases/tag/v0.2.0
[0.1.0]: https://github.com/ProcessMining-uOttawa/pm4py-ucm/releases/tag/v0.1.0
