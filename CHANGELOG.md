# Changelog

All notable changes to **pm4py-ucm** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
