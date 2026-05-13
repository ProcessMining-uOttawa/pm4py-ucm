# pm4py-ucm

[![tests](https://github.com/YOUR-GITHUB-USERNAME/pm4py-ucm/actions/workflows/tests.yml/badge.svg)](https://github.com/YOUR-GITHUB-USERNAME/pm4py-ucm/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org/)
[![License: GPL v3+](https://img.shields.io/badge/license-GPLv3%2B-blue.svg)](LICENSE)

**Use Case Map (UCM) extension for [PM4Py](https://github.com/process-intelligence-solutions/pm4py).**

`pm4py-ucm` adds first-class support for the [Use Case Map][ucm-wiki]
modelling notation — part of the ITU-T Z.151 *User Requirements Notation*
(URN) standard, supported by the open-source [jUCMNav][jucmnav] tool — to
PM4Py-style process-mining workflows.

The package is structured as a **drop-in companion** to PM4Py's existing
BPMN support: every public helper has the same shape (`read_*`, `write_*`,
`discover_*_inductive`, `view_*`, `convert_to_*`) so adopting UCM as an
additional output of process mining is a one-word change in user code.

```python
import pm4py
import pm4py_ucm

log = pm4py.read_xes("running-example.xes")
ucm = pm4py_ucm.discover_ucm_inductive(log)
pm4py_ucm.view_ucm(ucm)
pm4py_ucm.write_ucm(ucm, "running-example.jucm")  # opens in jUCMNav
```

[ucm-wiki]: https://en.wikipedia.org/wiki/Use_Case_Maps
[jucmnav]: https://github.com/JUCMNAV/projetseg-update

## Why UCM alongside BPMN?

BPMN is excellent for procedural choreographies, but it forces a single
abstraction level: every flow object is a step in *the* process. UCM, by
contrast, is a *scenario* notation. Its elements (path nodes — start
points, responsibilities, OR/AND forks and joins, stubs, timers, …) are
laid over a backdrop of **components**, which lets a single map describe a
behaviour that crosses architectural boundaries. This makes UCM a natural
target when the discovered process tree describes a workflow that spans
multiple services or organisational units, or when the goal is requirements
engineering rather than execution. See ITU-T Recommendation Z.151 for the
full notation reference.

## Installation

```bash
pip install pm4py-ucm           # core package + graphviz Python bindings
pip install pm4py-ucm[pm4py]    # also install pm4py for discovery
pip install pm4py-ucm[viz]      # add matplotlib for inline notebook display
pip install pm4py-ucm[dev]      # everything (pytest, pm4py, matplotlib)
```

The graphviz **system binary** must be on `PATH` for rendering (the
`graphviz` Python wheel only provides bindings):

```bash
# Debian / Ubuntu
sudo apt-get install graphviz
# macOS
brew install graphviz
# Windows
choco install graphviz
```

## Quick tour

### Build a UCM by hand

```python
from pm4py_ucm import UCM, write_ucm

ucm = UCM(name="OnlineShop")
m = ucm.add_map(name="ShoppingFlow")

start = m.add_node(UCM.StartPoint(name="start"))
login = m.add_node(UCM.RespRef(resp_def=ucm.get_or_add_responsibility("Login")))
fork  = m.add_node(UCM.OrFork(name="choose"))
search = m.add_node(UCM.RespRef(resp_def=ucm.get_or_add_responsibility("Search")))
browse = m.add_node(UCM.RespRef(resp_def=ucm.get_or_add_responsibility("Browse")))
join   = m.add_node(UCM.OrJoin())
checkout = m.add_node(UCM.RespRef(resp_def=ucm.get_or_add_responsibility("Checkout")))
end    = m.add_node(UCM.EndPoint(name="end"))

m.add_connection(start, login)
m.add_connection(login, fork)
m.add_connection(fork, search, condition="search")    # label on the OR branch
m.add_connection(fork, browse, condition="browse")    # label on the OR branch
m.add_connection(search, join)
m.add_connection(browse, join)
m.add_connection(join, checkout)
m.add_connection(checkout, end)

write_ucm(ucm, "online_shop.jucm")  # open in jUCMNav
```

### Mine a UCM from an event log

```python
import pm4py
import pm4py_ucm

log = pm4py.read_xes("log.xes")
ucm = pm4py_ucm.discover_ucm_inductive(log)
pm4py_ucm.view_ucm(ucm)                                # UCM notation
pm4py_ucm.save_vis_ucm(ucm, "diagram_ucm.png")         # UCM notation
pm4py_ucm.save_vis_ucm(ucm, "diagram_bpmn.png",        # BPMN notation
                       style="bpmn")
pm4py_ucm.write_ucm(ucm, "log.jucm")
```

`discover_ucm_inductive` is a thin wrapper around
`pm4py.discover_process_tree_inductive` followed by the bundled
process-tree → UCM converter, so all of PM4Py's tuning parameters for the
inductive miner are available via the `parameters` dict.

The PNG renderer supports two visual styles:

* **`style="ucm"` (default)** — Z.151 / jUCMNav notation: filled circle
  for the start point, perpendicular bar for the end point, `×` glyph
  with the responsibility name underneath, thick perpendicular bar for
  AND-fork / AND-join (synchronisation bars), small filled dot for
  OR-fork / OR-join, diamond reserved for stubs.
* **`style="bpmn"`** — BPMN-friendly look: activity boxes for
  responsibilities, gateway diamonds with `X` / `+` markers for
  XOR / AND gateways, thin-border start circle and thick-border end
  circle (the canonical BPMN end event).

Both styles preserve the swim-lane layout (one rectangle per
component, never overlapping unless nested) and wrap long
responsibility names onto two or three lines so the diagram stays
compact.

### `.jucm` layout matches the PNG layout

When writing a `.jucm` file, pm4py-ucm uses graphviz's `dot` engine to
compute the coordinates — exactly the same engine that drives the PNG
renderer. Open the resulting file in jUCMNav and you'll see the same
arrangement of nodes and component rectangles as in the rendered PNG.

If the graphviz binary isn't on `PATH`, the exporter falls back
silently to the bundled Sugiyama-style layouter so writing still
works; the visual result will be a layered drawing rather than a
graphviz one. Force the built-in layouter explicitly with:

```python
pm4py_ucm.write_ucm(ucm, "log.jucm",
                    parameters={"layout_engine": "builtin"})
```

### Mine performers too — surface them as URN components

If the log records who performed each activity (typically the
`org:resource` or `org:role` event attribute), pm4py-ucm can mine the
activity→performer mapping and use it to populate URN components
automatically: each unique performer becomes a `ComponentElement`, each
`Responsibility` is linked to its performer, and on every map the
`RespRef` symbol for that activity is drawn inside the component's
rectangle.

```python
import pm4py
import pm4py_ucm

log = pm4py.read_xes("log.xes")

# One-shot: mine + bind in a single call.
ucm = pm4py_ucm.discover_ucm_inductive(log, parameters={
    "resource_attribute": ["org:role", "org:resource"],  # priority list
})

# Or, the explicit three-step flow with more control.
performers = pm4py_ucm.discover_resources(
    log,
    attribute_priority=["org:role", "org:resource"],
    strategy="mode",        # or "first", "all", "unbound"
    min_support=0.0,        # default — pick the modal performer
                            # even when no single one owns a majority
)
ucm = pm4py_ucm.discover_ucm_inductive(log)
pm4py_ucm.bind_performers(ucm, performers)

pm4py_ucm.write_ucm(ucm, "log.jucm")
```

#### Component discovery vs activity binding

There are two related questions to answer when mining resources:

* **Which activities have a clearly-identified performer?** This is what
  `discover_resources` answers — it returns one performer per activity
  using the configured aggregation strategy (`mode` by default). An
  activity with many performers spread roughly equally gets bound to
  the modal one; an activity with no `org:resource` annotation at all
  is omitted.
* **Which actors exist in the log?** This is what `discover_components`
  answers — it returns the full *vocabulary* of every distinct
  performer value that appears anywhere in the log, sorted. The
  high-level `discover_ucm_inductive` calls both, so every distinct
  actor becomes a URN `ComponentElement` even when no single
  responsibility is cleanly bound to it. The unbound components show
  up in the URN tree but not as rectangles on the map.

This matters for logs with a dispersed resource pool — e.g. the BPI
Road Traffic Fines log has 148 distinct `org:resource` values where the
modal performer of *Create Fine* owns only 5.7% of events. Without these
two adjustments (modal binding without majority support, and full
component-vocabulary discovery), only one of the 148 actors would
appear in the resulting URN spec.

In the rendered diagram and the exported `.jucm`, each activity now
appears inside the rectangle of the team that owns it. See
[Definitions vs references](#definitions-vs-references) below for the
data-model story.

### Read an existing jUCMNav file

```python
from pm4py_ucm import read_ucm

ucm = read_ucm("requirements.jucm")
print(ucm)                          # → UCM(name='…', maps=…, responsibilities=…)
for n in ucm.maps[0].nodes:
    print(n)
```

## Process-tree → UCM mapping

The converter implements the following correspondences between PM4Py's
process tree operators and UCM constructs. UCM has no native loop, so loops
are encoded as an OR-fork/OR-join pair guarded by `[redo]` / `[exit]`
conditions — the canonical idiom in jUCMNav.

| Process tree                            | UCM construct                                                                 |
|-----------------------------------------|-------------------------------------------------------------------------------|
| Activity leaf with label `A`            | `RespRef` referencing a `Responsibility` named `A`                            |
| Silent (τ) leaf                         | A direct `NodeConnection` with no responsibility                              |
| `→ (sequence)` of children              | Children chained with `EmptyPoint` connectors (collapsed by simplifier)        |
| `× (xor)` choice                        | `OrFork` → branches → `OrJoin`                                                |
| `+ (parallel)`                          | `AndFork` → branches → `AndJoin`                                              |
| `o (interleaving)`                      | Treated as `+ (parallel)`                                                     |
| `∨ (or)`                                | Treated as `× (xor)`                                                          |
| `↻ (loop, do, redo)`                    | `OrJoin` → *do* → `OrFork` with `[redo]` back-edge and `[exit]` forward edge   |

After conversion, an `EmptyPoint` simplification pass collapses chains of
unnamed degree-2 connectors so that the resulting map renders compactly.

## Module layout

```
pm4py_ucm/
├── api.py                                 # high-level read_/write_/discover_/view_
├── objects/ucm/
│   ├── obj.py                             # UCM object model (URN metamodel)
│   ├── conversion/from_process_tree.py    # PM4Py process tree → UCM
│   ├── exporter/variants/jucm.py          # UCM → jUCMNav .jucm (XMI 2.0)
│   ├── importer/variants/jucm.py          # jUCMNav .jucm → UCM
│   └── layout/layouter.py                 # auto-layout for jUCMNav graphical view
├── algo/discovery/ucm/
│   ├── algorithm.py                       # discovery dispatcher (mirrors BPMN)
│   └── variants/inductive.py              # inductive-miner-based discovery
└── visualization/ucm/
    ├── visualizer.py                      # apply / view / save (mirrors BPMN)
    └── variants/classic.py                # graphviz-based renderer
```

The object model in `objects/ucm/obj.py` mirrors the [jUCMNav EMF
metamodel][jucmnav-meta] (`urn`, `urncore`, `ucm.map`) closely enough to
emit XMI files that load directly in jUCMNav.

[jucmnav-meta]: https://github.com/JUCMNAV/projetseg-update/tree/master/seg.jUCMNav/src/seg/jUCMNav/emf

## Definitions vs references

UCM keeps a sharp distinction between a *definition* (a reusable named
concept declared once at the URN level) and each visual *reference* to it
on a map. The object model surfaces this distinction explicitly:

| Definition (one) | Reference (many) | Where reference lives |
|---|---|---|
| `UCM.Responsibility` (an activity) | `UCM.RespRef` (the "✕" symbol) | inside a map's `nodes` list |
| `UCM.ComponentElement` (an actor / team / role / system) | `UCM.ComponentRef` (the labelled rectangle) | inside a map's `cont_refs` list |

A definition lives on the URN container (`ucm.responsibilities`,
`ucm.components`). Each *visual occurrence* on a diagram is a separate
reference object that points back to the definition via `resp_def` (for
`RespRef`) or `cont_def` (for `ComponentRef`). Many references may share
the same definition — that is exactly what lets the same activity or the
same actor appear in multiple places without being declared twice.

A third link — `Responsibility.performer` — runs *between definitions*:
it expresses the semantic fact that a given activity is performed by a
given actor/team. This is a logical binding, independent of layout. The
visual binding (`RespRef.cont_ref → ComponentRef`) is derived from it
whenever a map is built or `UCM.bind_performers()` is called.

```text
─ URN level ──────────────────────────────────────────
 Responsibility "Login" ──performer──> ComponentElement "AuthService"
            ▲                                  ▲
            │ resp_def                         │ cont_def
─ Map level ──────────────────────────────────────────
   RespRef #5  ─────cont_ref─────────>  ComponentRef #12
   (drawn as ✕)                         (drawn as a rectangle)
```

Build them through the helpers:

```python
from pm4py_ucm import UCM

ucm = UCM(name="Example")
m   = ucm.add_map(name="MainMap")

# --- DEFINITIONS (one per concept, declared on the URN container) ---
login_def = ucm.get_or_add_responsibility("Login")          # Responsibility
actor_def = ucm.get_or_add_component(                       # ComponentElement
    "User", kind=UCM.ComponentElement.Kind.ACTOR)

# --- REFERENCES (many per definition, drawn on the map) -------------
login_node = m.add_node(UCM.RespRef(resp_def=login_def))    # RespRef
actor_box  = m.add_component_ref(actor_def, width=200, height=120)

# A path node may declare which component reference visually contains it:
login_node.cont_ref = actor_box
```

In the exported `.jucm` you'll see this distinction reflected as the
bidirectional links jUCMNav uses internally:

```xml
<responsibilities name="Login" id="35" respRefs="36"/>
<components       name="User"  id="45" contRefs="46" kind="Actor"/>
…
<nodes   xsi:type="ucm.map:RespRef" id="36" respDef="35" contRef="46" …/>
<contRefs xsi:type="ucm.map:ComponentRef" id="46" contDef="45" nodes="36" …/>
```

The `respRefs`/`contRefs` attributes on a definition list back-references
to every occurrence of it; `respDef`/`contDef` on the reference point
forward to the definition. The exporter computes the back-references
automatically — you only need to set the forward links.

## Compatibility with jUCMNav

The exporter produces files in the modern jUCMNav format (matches output
of jUCMNav 5.5 and later):

* Root element `<urn:URNspec>` declares four namespaces — `xmi`, `xsi`,
  `urn` (`http:///urn.ecore`), and `ucm.map` (`http:///ucm/map.ecore`).
  The `urncore` and `grl` packages do not need declarations because
  their concepts use unqualified element names inside the URN
  containment tree.
* URN-level metadata (`urnVersion="1.27"`, `specVersion="4"`, `name`,
  `author`, `created`, `modified`, `nextGlobalID`) lives on the root
  as **attributes**, not child elements.
* Children of `<urn:URNspec>` appear in the canonical order
  **ucmspec → grlspec → urndef**. Children of `<urndef>` appear in the
  order **responsibilities → specDiagrams → components**. Children of a
  `<specDiagrams>` UCMmap appear as **nodes → contRefs → connections**.
* Connections are **anonymous** — `<connections>` carries no `id`
  attribute. Endpoints use integer node IDs (`source="18" target="19"`).
* Nodes refer to their connections via XPath fragments
  (`succ="//@urndef/@specDiagrams.0/@connections.7"`), the only style
  available because connections lack IDs.
* `<condition>` elements distinguish a human-readable `label` (e.g.
  `"TrueBranch"`) from the logical `expression` (default `"true"`).
* `nextGlobalID` is exactly `max(all IDs) + 1` — the integer ID jUCMNav
  would assign to the next newly-created element.
* An auto-layouter places nodes on a layered left-to-right grid before
  export so the diagram is immediately readable in jUCMNav's editor.

## Testing

```bash
pip install -e .[dev]
python -m unittest discover -s tests -v
```

The default test suite does **not** require PM4Py to be installed: the
process-tree → UCM converter accepts duck-typed trees (`operator.value`,
`children`, `label`), which the tests use to exercise every operator in
isolation. The `tests/test_export_import.py` suite verifies that round-trip
through the jUCMNav XMI back-end is byte-deterministic.

## License

GPL-3.0-or-later, matching the upstream [PM4Py][pm4py-gpl] license.
The jUCMNav metamodel reproduced (in spirit) here is itself distributed
under EPL-2.0; this package only re-implements the metamodel in Python and
does not bundle any jUCMNav source.

[pm4py-gpl]: https://github.com/process-intelligence-solutions/pm4py/blob/release/LICENSE
