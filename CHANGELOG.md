# Changelog

All notable changes to **pm4py-ucm** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

## [Unreleased — previously]

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

[0.1.0]: https://github.com/YOUR-GITHUB-USERNAME/pm4py-ucm/releases/tag/v0.1.0
