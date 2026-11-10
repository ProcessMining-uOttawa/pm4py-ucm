# BPMN 2.0 export

> **Status: all five stages implemented** in
> `pm4py_ucm/objects/ucm/exporter/variants/bpmn.py`, exposed as
> `pm4py_ucm.write_bpmn` / `convert_to_bpmn_xml`, tested by
> `tests/test_bpmn_export.py` (XSD conformance against the vendored OMG
> schema, pm4py round trip, lane tiling, layout transfer, stubs, metrics).
> Verified by eye in bpmn-js: flat, decomposed and dynamic-stub models import
> without warnings, and so do heat-mapped and canonical-size exports of the
> demo claims log, as do inline-stub, data-object and orthogonally routed
> exports; the V6 Model and Family views offer the downloads. Nothing of the
> plan is left; §13 lists what a later iteration could add. The three structural decisions were settled by
> Daniel Amyot on 2026-11-10; §10 lists them and what implementation decided
> since.

## 1. The feature

Export a mined (or imported) UCM as a standard **BPMN 2.0 XML** file carrying
**BPMNDI** diagram interchange, so that the model opens in bpmn.io, Camunda
Modeler, Signavio, Bizagi, and `pm4py.read_bpmn`, laid out the way the app's
BPMN-style preview already shows it.

Scope, in order of importance:

1. Everything the generators produce today must map without loss of control
   flow: `StartPoint`, `EndPoint`, `RespRef`, `OrFork`, `OrJoin`, `AndFork`,
   `AndJoin`, `EmptyPoint`, static `Stub` (hierarchical decomposition) and
   dynamic `Stub` (family assembly). These are the only `PathNode` kinds any
   converter in `pm4py_ucm/objects/ucm/conversion/` or
   `pm4py_ucm/algo/discovery/` instantiates.
2. Components must survive as **lanes**, with the swim-lane geometry intact.
3. Performance overlays must survive, machine-readably and, optionally, visibly.
4. Model families must have a defensible rendering.
5. Elements that only arrive through the `.jucm` importer (`WaitingPlace`,
   `Timer`, `FailurePoint`, `Anything`, `Connect`, `DirectionArrow`) get a
   best-effort mapping and are never a reason to refuse an export.

Out of scope for this design: importing BPMN into a UCM (pm4py already reads
BPMN; a `convert_to_ucm(bpmn)` is a separate feature), executable semantics
(`isExecutable="false"` always), message flows, and any GRL content.

## 2. Writer: our own, not pm4py's

pm4py has a BPMN object model and an exporter, and the tempting shortcut is to
build a `pm4py.objects.bpmn.obj.BPMN` and call `pm4py.write_bpmn`. Rejected:
that object model has no lanes, no nested lane sets, no call activities, no
`extensionElements`, no conditions on sequence flows and no colour attributes.
It would lose §4 (lanes), most of §3 (conditions, stubs) and all of §7
(annotations).

We write the XML ourselves, with the small element writer the `.jucm` exporter
already uses (`pm4py_ucm/objects/ucm/exporter/variants/jucm.py`). pm4py's
reader then becomes a **round-trip test**, not the implementation.

Namespaces emitted:

| prefix | namespace | purpose |
|---|---|---|
| (default) `bpmn` | `http://www.omg.org/spec/BPMN/20100524/MODEL` | semantics |
| `bpmndi` | `http://www.omg.org/spec/BPMN/20100524/DI` | diagram |
| `dc`, `di` | OMG DD `DC` / `DI` | bounds, waypoints |
| `color` | `http://www.omg.org/spec/BPMN/non-normative/color/1.0` | BPMN in Color (heat-map) |
| `bioc` | `http://bpmn.io/schema/bpmn/biocolor/1.0` | bpmn.io's older colour twin |
| `ucm` | `http://pm4py-ucm.org/schema/bpmn/1.0` (to confirm) | extension elements: ids, kinds, metrics |

## 3. Element mapping

### 3.1 Containers

| UCM | BPMN |
|---|---|
| `UCM` | `definitions` (`name`, `exporter="pm4py-ucm"`, `exporterVersion`) + one `collaboration` with one `participant` (the pool, named after the UCM) referencing the root `process`. |
| root `UCMmap` | `process` with `isExecutable="false"`. `UCM.description` / `author` → `documentation`. |
| every other `UCMmap` | its own `process`, and its own `bpmndi:BPMNDiagram`. Multi-diagram files are standard BPMN 2.0; bpmn.io and Camunda Modeler offer a diagram switcher. |

The whole mined model is **one** control flow, so there is exactly one pool.
See §4 for why components are never pools.

### 3.2 Path nodes

| UCM | BPMN | Notes |
|---|---|---|
| `StartPoint` | `startEvent` | `pre_condition` → `documentation` and `ucm:precondition` extension. Not a `conditionalEventDefinition`: that changes execution semantics and most viewers draw it differently. |
| `EndPoint` | `endEvent` | `post_condition` likewise. |
| `RespRef` | `task` | BPMN has no definition/reference split. Every `RespRef` is its own task named after its `Responsibility`; `ucm:responsibility="<def id>"` keeps shared responsibilities traceable. `Responsibility.expression` → `documentation`. Always a plain `task`: typing by component kind (`userTask`, `serviceTask`) was considered and **rejected**, since a mined model carries no evidence for it. The kind stays visible on the lane (§4). |
| `OrFork` | `exclusiveGateway` (`gatewayDirection="Diverging"`) | Arc `Condition` → `sequenceFlow/conditionExpression` of type `tFormalExpression`, `language` = jUCMNav's expression language, text = `Condition.expression`; `Condition.label` → the flow's `name`. A branch whose expression is the complement of all others (the synthesizer's "else") becomes the gateway's `default` flow instead of a condition. |
| `OrJoin` | `exclusiveGateway` (`Converging`) | Loop back-edges need nothing special; BPMN sequence flows may form cycles. |
| `AndFork` / `AndJoin` | `parallelGateway` | Direct. |
| `EmptyPoint` | removed | The incoming and outgoing connections are merged into one `sequenceFlow`; the point's `(x, y)` becomes a waypoint, so routing bends survive (§5). An empty point carrying a condition or a non-empty name keeps its text as the merged flow's `name`. |
| `Connect`, `DirectionArrow` | removed | Same treatment as `EmptyPoint`. |
| `NodeConnection` | `sequenceFlow` | `threshold` → `ucm:threshold` extension only. `Condition.delta_x/delta_y` → the edge's `bpmndi:BPMNLabel` bounds. |
| `WaitingPlace` | `intermediateCatchEvent` with `conditionalEventDefinition` | Import-only. |
| `Timer` | `intermediateCatchEvent` with `timerEventDefinition` | Import-only. A timeout path (a second outgoing connection) becomes an `eventBasedGateway` in front of two catch events (timer, conditional). |
| `FailurePoint` | `intermediateThrowEvent` with `errorEventDefinition` | Import-only. |
| `Anything` | `task` named `anything`, `ucm:kind="anything"` | Import-only. |

**Inclusive choice.** `from_process_tree` maps both `×` and `∨` to `OrFork`
and records nothing about which. If the converter tags the fork
(`metadata name="_op" value="OR"` or a new attribute), the exporter emits
`inclusiveGateway` for the fork and its matching join; otherwise exclusive.
This is the only place the UCM side would need a change for BPMN's sake, and
it is optional.

### 3.3 Stubs

| UCM | BPMN | Notes |
|---|---|---|
| static `Stub`, one `PluginBinding`, one in / one out | `callActivity` with `calledElement` = the plug-in map's `process` id | **Default** (`stubs="call"`). Mirrors the map structure one to one: the plug-in keeps its own process, lane set and diagram. The `InBinding`'s `StartPoint` and the `OutBinding`'s `EndPoint` are that process's start and end events. |
| any `Stub` (`stubs="inline"`) | **collapsed** `subProcess` whose body is the plug-in map, with its own `BPMNDiagram` | Only root maps become processes; a plug-in bound to several stubs is written once per stub (distinct ids). The sub-process owns the plug-in's `laneSet`, and bpmn-js draws those lanes on the sub-process's plane. A dynamic stub's inclusive choice leads to one nested sub-process per plug-in. The point of the mode: no `callActivity`, so pm4py's reader reads the file. |
| dynamic `Stub`, N `PluginBinding`s | `subProcess` named after the stub containing `gateway → N callActivity → gateway` | Each `PluginBinding.precondition` → the `conditionExpression` on its branch. The sub-process is drawn **collapsed** in the parent (one box at the stub's coordinates, §5) with its own diagram, so the parent keeps the UCM's shape. |
| gateway kind for dynamic stubs | always `inclusiveGateway`, diverging and converging | This is the Z.151 semantics: a dynamic stub selects **every** plug-in whose precondition holds, possibly several, and the paths rejoin when all selected plug-ins have completed. An inclusive join is exactly that. No syntactic "are the preconditions exclusive" detection: when the family's preconditions happen to be exclusive the inclusive gateway behaves identically, and guessing would mis-state the model whenever the guess is wrong. |
| stub with several entries or exits | lossy | A call activity has one implicit entry, so several incoming flows merge. Each binding's start/end event in the called process is named after the stub entry/exit, and `ucm:binding` extension elements record the full binding table. No generator produces this shape today. |
| a plug-in map bound to several stubs | one `process`, several `callActivity` | Free with `stubs="call"`; `stubs="inline"` duplicates it. |

### 3.4 Scenario layer

BPMN has no scenarios. By default nothing is emitted. With `data="objects"`,
each `Variable` becomes a `dataObject` (+ `dataObjectReference`, drawn in a
strip above the pool), `EnumerationType` values and the variable's type go to
its `documentation`, and each `ScenarioDef` becomes a `documentation`
paragraph on the root process (`Scenario <group> / <name>: var = value, …;
starts at …; ends at …`). Conditions already live on the flows (§3.2), so the
executable part of a scenario is present either way.

## 4. Components → one pool, nested lanes

A UCM path crosses component boundaries as **control flow**. In BPMN, flow
crosses lanes but never pools (pools talk through message flows). Components
as pools would therefore misstate the model, so:

- **One `participant`** (pool) for the whole model.
- **Every top-level `ComponentRef` is a `lane`** in the process's `laneSet`.
  Nested refs become `lane/childLaneSet/lane`, which BPMN allows to any depth
  and bpmn.io, Camunda and Signavio render.
- A lane lists its bound nodes as `flowNodeRef`s (`PathNode.cont_ref`).
- `ComponentElement.kind` → `ucm:kind` on the lane and, with
  `lane_names="with_kind"`, a suffix in the lane name (`Clerk «Actor»`).
- Nodes bound to no component go into an implicit lane named `(unassigned)`,
  because flow nodes floating outside every lane of a laned pool render oddly.
  Option `lanes="none"` drops lanes altogether.
- Each plug-in map's `cont_refs` produce that process's own `laneSet`. The same
  `ComponentElement` referenced from several maps therefore appears as a lane
  with the same name in several processes, which is exactly what UCM maps do.

**Geometry constraint.** BPMN lanes must tile the pool as full-width,
contiguous horizontal bands. Two facts about our layouters decide how this is
met:

- The bundled Sugiyama layouter (`layout/layouter.py`) allocates each
  top-level component a **disjoint Y-band** by construction. Its output is
  lane-compatible as is: lane bounds = the component's Y-range stretched to
  the pool's full width. Gaps between bands (where unbound nodes sit) become
  the `(unassigned)` lane or are absorbed by the neighbouring lane when no
  unbound node is there.
- The graphviz layouter (`layout/graphviz_layouter.py`) draws components as
  **clusters**, i.e. bounding boxes, which may sit side by side at the same
  height. Two sibling components at the same Y cannot be lanes.

Decision: the BPMN exporter **always runs the bundled swim-lane layouter**
when it has to lay out, and when it is given a model with coordinates (e.g.
imported from jUCMNav, or laid out by graphviz) it checks the lane condition
and, if violated, re-stacks the offending components vertically with a
**lane-fitting pass** that keeps X and only shifts Y per component band. The
pass is reported in the result so the user knows the picture moved.

## 5. Preserving the layout

BPMNDI coordinates are unitless pixels with the origin top-left, exactly like
jUCMNav's, so **node centres transfer 1:1**. What differs is shape size: a
`RespRef` is a small cross with its label underneath; a BPMN task is a box.

The PNG renderer's `style="bpmn"` already solved this: it draws a
**label-sized rounded box** at the UCM coordinates, using the same wrapping as
the exporter (`util/name_wrap.label_dimensions`). The BPMN export uses that
footprint, so the file opens in bpmn.io looking like the app's BPMN-style
preview, which is the expectation to set.

| element | `BPMNShape` bounds (`sizes="layout"`, default) |
|---|---|
| task | `label_dimensions(name)` + padding, centred on `(x, y)` |
| gateway | 36 × 36 centred (BPMN's habitual gateway size, close to the UCM glyph) |
| start / end / intermediate event | 30 × 30 centred |
| call activity / collapsed sub-process | as task, with `isExpanded="false"` |
| lane | see §4; `isHorizontal="true"` |
| pool | bounding box of all lanes plus a 30 px label strip on the left |

`sizes="canonical"` switches to the standard 100 × 80 task / 50 × 50 gateway /
36 × 36 event boxes and **scales the whole layout uniformly** by the smallest
factor ≥ 1 that keeps every flow's two ends, and every two shapes sharing a
column or a row, 20 px apart (`CANONICAL_GAP`). Component rectangles, lanes
and bends scale with it. Topology and relative arrangement are preserved;
absolute coordinates are not.

Edges: a `BPMNEdge` gets waypoints source-centre → each removed
`EmptyPoint`/`Connect`/`DirectionArrow` centre in order → target-centre. The
first and last waypoint are then clipped to the shape border so the arrow
head touches the box, as the PNG does. `routing="orthogonal"` turns every
slanted segment into an elbow first (horizontal-then-vertical on a forward
segment, vertical-then-horizontal on a backward one), which is what keeps a
loop back-edge from cutting through the loop body's last task box; the
default `"straight"` keeps the UCM's own lines. Condition labels get a `BPMNLabel`
positioned by `Condition.delta_x/delta_y` relative to the first waypoint.

Plug-in diagrams use the plug-in map's own coordinates. The collapsed
call-activity box in the parent sits at the stub's `(x, y)`.

## 6. Why not just convert and let the BPMN tool lay out

Because the layout is the point. Analysts compare the UCM and the BPMN side by
side, the paper figures and the app preview use the swim-lane arrangement, and
auto-layout in bpmn.io or Camunda ignores lanes. A file without BPMNDI is also
unreadable in most modellers until laid out. So BPMNDI is always written.

## 7. Performance annotations

BPMN has no standard place for mining metrics. Two layers, both on by default,
each independently switchable:

**Machine layer** (`metrics="extension"`, always safe). Every `_perf` metric
that `pm4py_ucm/algo/performance.py` attached becomes a child of the node's
or flow's `extensionElements`:

```xml
<bpmn:extensionElements>
  <ucm:metric name="traversal_frequency" value="258"/>
  <ucm:metric name="mean_time" value="3.21" unit="d"/>
</bpmn:extensionElements>
```

This mirrors what the `.jucm` exporter does with `<metadata>` and what the
jUCMNav properties view shows. Tools ignore it; pm4py preserves it; nothing
is lost. (Note the `.jucm` exporter cannot put metadata on connections because
jUCMNav's metamodel lacks it; BPMN's `sequenceFlow` has `extensionElements`,
so edge metrics are **more** complete in BPMN than in `.jucm`.)

**Visible layer** (`annotate="edges"` default; `"none"`, `"all"`):

- **Heat-map colours** (`heatmap_node=` / `heatmap_edge=` metric keys,
  `heatmap_global=`; the PNG renderer's parameter names). The node fill and
  edge stroke colours, computed by the classic renderer's own helpers (value
  parsing, segment attribution, normalisation, blue ramp for counts and red
  for times), go on the `BPMNShape`/`BPMNEdge` as `color:background-color` /
  `color:border-color` (OMG BPMN in Color) and the equivalent `bioc:fill` /
  `bioc:stroke`. Edge thickness has no standard; it is dropped (the colour
  carries the value). Only tasks and flows are coloured, as in the PNG.
- **Edge metrics** as the `sequenceFlow` `name`, in the same short form the
  PNG shows (`258`, `25% of 258`, `3.2 d`). Every tool renders flow names as
  edge labels. When a flow already has a condition label, the metric is
  appended on a new line.
- **Node metrics** (`annotate="all"` only) as a `textAnnotation` linked by an
  `association`, placed just under the task box. Standard artifacts, visible
  everywhere, but noisy, hence off by default.

Nothing ever goes into task names. The exported file stays usable as a plain
process model.

## 8. Model families

BPMN has no variability mechanism. Two outputs, mirroring the UCM side:

- **`write_bpmn_family(family, path)`** writes the **assembled** family
  (`families/assembly.py`) as one `.bpmn`: the shared skeleton is the root
  process and each variation point is the dynamic-stub mapping of §3.3, a
  data-based inclusive choice on the partition attribute over one called
  process per cell (inclusive per §3.3; with one attribute per variation
  point exactly one branch fires, so the picture reads as a plain variant
  switch). This is a genuinely idiomatic BPMN rendering of a family
  (a single model with `${region == "EU"}`-style gateways is how BPMN
  practitioners model variants), and it is lossless with respect to the
  assembled UCM.
- When `path` ends in `.zip`, the archive additionally holds one `.bpmn` per
  `FamilyCell`, named by `ModelFamily.file_stem`, for analysts who want each
  cell standalone, exactly as `write_ucm_family` does for `.jucm`.
- The family **statistics report** and the **Compare** view stay HTML. They
  have no BPMN counterpart and inventing one adds nothing.

Cell performance overlays travel inside each cell's file by §7; the
assembled model carries the overlays the assembly already attached.

## 9. API and app surface

Library (`pm4py_ucm/api.py`, implemented in
`pm4py_ucm/objects/ucm/exporter/variants/bpmn.py`):

```python
pm4py_ucm.write_bpmn(
    ucm, path, *,
    stubs="call",            # | "inline"
    lanes="components",      # | "none"
    lane_names="plain",      # | "with_kind"
    sizes="layout",          # | "canonical"
    metrics="extension",     # | "none"
    annotate="edges",        # | "none" | "all"
    overlay=None,            # metric key for heat-map colours, as the visualizer takes it
    data="none",             # | "objects"
    layout=None,             # reuse coordinates if present, else swim-lane layouter
    validate=True,           # validate_ucm() first, as every other exporter does
) -> str

pm4py_ucm.write_bpmn_family(family, path, assembled=None,
                            family_scale=False, **same) -> str
pm4py_ucm.convert_to_bpmn_xml(ucm, **same) -> str   # the XML as a string, for the app's download button
```

Web app (V6 only, per `HANDOFF.md`): a **Prepare BPMN… / Download BPMN**
pair next to the `.jucm` download in the Model view, honouring the view's
current overlay and heat-map selection (built on request and cached per
model + heat-map, like the PNG), and a **BPMN 2.0 (.zip)** in the Family
view's Downloads row: one `.bpmn` per cell plus `family_umbrella.bpmn`, on
the family-wide heat-map scale when that scope is selected. Both read the
model back from the cached `.jucm` bytes, whose graphviz layout places
component clusters side by side, so the exporter re-lays the maps out in
swim-lanes (the warning is silenced there and the help text says the
picture may differ from the on-screen one).

**Known layout consequence.** The app's on-screen render and `.jucm` use
graphviz, and graphviz clusters are not lane-compatible (§4), so an app
export never carries the on-screen coordinates; it carries the swim-lane
layouter's. A library user who lays out with the bundled layouter (the
default when no coordinates are set) gets the 1:1 transfer of §5. The session registry
does not change: the export is derived, not a stored parameter. The exported
Python script (`web/sessions/codegen.py`) gains one `write_bpmn(...)` line
when the user downloaded a BPMN during the session is **not** proposed; keep
codegen tied to stored parameters.

Validation gates, all in `tests/`:

1. **XSD**: validate against the OMG `BPMN20.xsd` family (vendored under
   `tests/data/bpmn-xsd/`, ~300 KB).
2. **Round trip**: `pm4py.read_bpmn` reads every exported fixture and the
   recovered node and flow multiset equals the expected one (tasks, gateways by
   type, events, flows).
3. **Lane tiling**: every lane's Y-range is disjoint from its siblings and the
   union covers the pool.
4. **Golden files** for the running example, a decomposed model, an imported
   `.jucm` with timers, and a family, byte-compared like the `.jucm` goldens.
5. **Manual**: open in bpmn.io's demo modeller in the browser pane and
   screenshot; Camunda Modeler if available.

## 10. Decisions

Settled (Daniel Amyot, 2026-11-10):

| # | decision | choice | why |
|---|---|---|---|
| 1 | call activity vs inlined sub-process for static stubs | **`callActivity` default**; `stubs="inline"` stays an option | Lanes inside inlined sub-processes are inconsistent across tools; `call` keeps one process and one diagram per map. |
| 2 | gateway kind for dynamic stubs | **always inclusive** | Proper UCM semantics: a dynamic stub may select several plug-ins and the inclusive join waits for all selected ones. |
| 3 | task typing by component kind | **none**; every responsibility is a plain `task` | Mined models give no evidence for `userTask`/`serviceTask`; the kind is on the lane. |

Still open:

| # | decision | proposed default | why it matters |
|---|---|---|---|
| 4 | tag `∨` forks in the converter so BPMN can emit inclusive gateways | yes, as metadata | Tiny change to `from_process_tree`; otherwise every OR becomes XOR. The exporter already honours `_op = OR` metadata. |

Decided during implementation (Stage 1):

| # | decision | choice | why |
|---|---|---|---|
| 5 | re-layout vs reuse coordinates | **reuse**; a map whose sibling components overlap vertically is re-laid out with the swim-lane layouter and a `UserWarning` says so | A per-component Y-shift pass would have to drag the bound nodes and the unbound ones between them along; re-layout is honest and rare (hand-drawn or graphviz layouts only). |
| 6 | extension namespace URI | `urn:pm4py-ucm:bpmn:1.0`, prefix `ucm` | A URN claims no web domain. |
| 7 | nodes bound to no component | **nearest lane** by geometry (the lane whose band holds the node's Y, else the closest band) | A separate "(unassigned)" band would have to move the nodes and showed up as a sliver between two real lanes. Gateways and events sitting in a participant's lane is ordinary BPMN. |
| 8 | generated gateway names | **dropped** (`OrFork`, `LoopJoin`, … as the converters name them) | They say nothing a gateway glyph does not; user-given names stay. |
| 9 | condition labels on unguarded branches | **dropped**, like the `.jucm` default | The converter labels choice arcs `branch0`/`branch1` with expression `true`; only a real guard yields a `conditionExpression` and a visible label, and a lone unguarded branch among guarded ones becomes the gateway's `default`. |

## 11. Risks

- **Overlapping shapes** with `sizes="layout"` on very dense layouts: label
  boxes are wider than the UCM glyphs they replace. Mitigation: the
  `X_GAP`/`Y_GAP` the swim-lane layouter already applies were tuned for the
  same label footprints, so mined models are fine; imported tight layouts get a
  warning suggesting `sizes="canonical"`.
- **Tool differences**: nested lanes and multi-diagram files are standard but
  Bizagi and some validators are picky about `childLaneSet` and about a
  `participant` whose `processRef` has lanes. Golden files are checked in
  bpmn.io and `pm4py.read_bpmn` first; others as found.
- **pm4py's reader** ignores lanes, conditions and extensions, so the
  round-trip test can only cover control flow. It also does not know
  `callActivity` (2.7.x crashes on a flow into one), so `pm4py.read_bpmn`
  only reads **stub-free** exports; models with stubs are checked
  structurally and in bpmn-js instead. `stubs="inline"` (Stage 5) would be
  the pm4py-friendly alternative.
- **Loop back-edges** drawn with the UCM's straight segments may cross the
  loop body's last task box, since a BPMN task is wider than a UCM cross.
  `routing="orthogonal"` avoids that; the default stays faithful to the UCM
  lines.
- **Scope creep** into a BPMN importer. Not this feature.

## 12. Plan

1. **Stage 1, core writer** — **done**: §3.1, §3.2 for every node kind, §4,
   §5 (`sizes="layout"`), §7 machine layer and the `annotate` edge/all
   layers, XSD + round-trip + lane + layout tests. `write_bpmn` and
   `convert_to_bpmn_xml` in the API. (No byte-golden yet: the format may
   still move in Stage 3; the tests pin structure and geometry instead.)
2. **Stage 2, stubs** — **done**: §3.3 with `stubs="call"`, multi-diagram
   files, dynamic stubs as collapsed sub-processes with inclusive gateways
   and their own diagram.
3. **Stage 3, visible annotations and sizes** — **done**: §7 heat-map
   colours (`heatmap_node` / `heatmap_edge` / `heatmap_global`),
   `sizes="canonical"`. The orthogonal-routing option for loop back-edges
   was not built; it stays a candidate.
4. **Stage 4, app** — **done**: Prepare/Download BPMN in the Model view,
   BPMN 2.0 (.zip) in the Family view, both verified in the running app on
   the demo claims log.
5. **Stage 5, long tail** — **done**: `stubs="inline"` (nested collapsed
   sub-processes, pm4py-readable), `data="objects"`, `routing="orthogonal"`.
   Done earlier: import-only elements (Stage 1), `write_bpmn_family` (with
   Stage 4), README section and CHANGELOG.

## 13. Possible later work

Not planned, noted so they are not forgotten: a BPMN **importer**
(`convert_to_ucm(bpmn)`; pm4py already parses BPMN), exposing `stubs`,
`sizes` and `routing` as choices in the web app's download (today the app
uses the defaults), tagging `∨` forks in the tree converter (decision 4), and
a Camunda Modeler / Signavio pass over the goldens beyond bpmn-js.
