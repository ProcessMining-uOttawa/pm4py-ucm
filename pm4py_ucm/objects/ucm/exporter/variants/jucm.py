"""Exporter for the jUCMNav ``.jucm`` file format (EMF XMI 2.0).

Produces files that load directly in jUCMNav. The serialised structure
matches what jUCMNav itself writes:

* root ``<urn:URNspec>`` with namespace URIs ``http:///urn.ecore`` and
  ``http:///ucm/map.ecore``, plus the standard XMI / XSI namespaces;
* attributes on the root: ``name``, ``author``, ``created``, ``modified``,
  ``specVersion``, ``urnVersion``, ``nextGlobalID``;
* child element order **ucmspec → grlspec → urndef**;
* inside ``<urndef>``: **responsibilities → specDiagrams → components**;
* inside each ``<specDiagrams xsi:type="ucm.map:UCMmap">``:
  **nodes → contRefs → connections**;
* responsibilities and component definitions carry the bidirectional
  back-references ``respRefs="36"`` / ``contRefs="14 42"`` listing the
  node / component-ref IDs that point at them;
* connections are **anonymous** — they have no ``id`` attribute, only
  ``source``/``target`` integer references to node IDs;
* nodes refer to their incoming/outgoing connections by **XPath
  fragments** (``//@urndef/@specDiagrams.0/@connections.7``), the only
  reference style for anonymous targets;
* ``StartPoint`` carries a ``<precondition>``, ``EndPoint`` a
  ``<postcondition>``, and any node that displays a name carries a
  ``<label/>`` child;
* ``<condition>`` on a connection has a ``label`` attribute for the
  human-readable name (``"TrueBranch"``) and a separate ``expression``
  attribute for the logical guard (default ``"true"``);
* ``nextGlobalID`` is exactly ``max(all IDs) + 1`` — the value jUCMNav
  would assign to the next newly-created element.

The auto-layouter is invoked before serialisation when no node has been
positioned, so that diagrams discovered from process trees render
sensibly in the editor.
"""

from __future__ import annotations

import datetime
import io
import time
from typing import IO, Dict, Iterable, List, Optional, Tuple, Union

from ...obj import UCM
from ...layout.layouter import apply_layout
from ...util.component_colors import (
    component_color as _component_color,
    hex_to_rgb_triplet as _hex_to_rgb,
)
from ...util.name_wrap import wrap_name, DEFAULT_MAX_WIDTH as _DEFAULT_WRAP_WIDTH


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: EMF/XMI URI for the root URN package — not a real URL, jUCMNav uses
#: this exact opaque form.
_NS_URN = "http:///urn.ecore"
_NS_UCM_MAP = "http:///ucm/map.ecore"
_NS_XMI = "http://www.omg.org/XMI"
_NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"

#: Mapping from PathNode subclass -> ``xsi:type`` value used in the XMI.
_NODE_XSI_TYPES = {
    UCM.StartPoint: "ucm.map:StartPoint",
    UCM.EndPoint: "ucm.map:EndPoint",
    UCM.EmptyPoint: "ucm.map:EmptyPoint",
    UCM.RespRef: "ucm.map:RespRef",
    UCM.OrFork: "ucm.map:OrFork",
    UCM.OrJoin: "ucm.map:OrJoin",
    UCM.AndFork: "ucm.map:AndFork",
    UCM.AndJoin: "ucm.map:AndJoin",
    UCM.WaitingPlace: "ucm.map:WaitingPlace",
    UCM.Timer: "ucm.map:Timer",
    UCM.Stub: "ucm.map:Stub",
    UCM.Connect: "ucm.map:Connect",
    UCM.DirectionArrow: "ucm.map:DirectionArrow",
    UCM.FailurePoint: "ucm.map:FailurePoint",
    UCM.Anything: "ucm.map:Anything",
}

#: Path node types that carry a ``<label/>`` child element in jUCMNav
#: (the label positions the displayed name on the diagram).
_NODES_WITH_LABEL = (
    UCM.StartPoint, UCM.EndPoint, UCM.RespRef, UCM.Stub, UCM.FailurePoint,
)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def apply(
    ucm: UCM,
    file_path: Union[str, IO],
    parameters: Optional[dict] = None,
) -> None:
    """Serialise ``ucm`` to ``file_path`` in jUCMNav's ``.jucm`` format.

    Parameters
    ----------
    ucm
        UCM to export.
    file_path
        Either a string path or a writable text file-like object.
    parameters
        Recognised keys:

        ``layout``
            Boolean, default ``True``. Run the auto-layouter when no node
            has yet been positioned (every node still at the origin).
        ``encoding``
            Output encoding declared in the XML prolog. Default
            ``"UTF-8"``; jUCMNav also accepts ``"ISO-8859-1"``.
        ``wrap_names``
            Boolean, default ``True``. Break long element names across
            two or three lines so that jUCMNav draws narrower
            rectangles (the line breaks survive as ``&#xD;&#xA;`` in the
            XML attribute values).
        ``wrap_width``
            Integer character budget per line; default
            :data:`~pm4py_ucm.objects.ucm.util.name_wrap.DEFAULT_MAX_WIDTH`.
        ``layout_engine``
            ``"graphviz"`` (default) computes positions via graphviz so
            the ``.jucm`` lays out like the PNG; ``"builtin"`` forces
            the in-house Sugiyama layouter.
    """
    parameters = dict(parameters or {})

    text = serialize_to_string(
        ucm,
        layout=parameters.get("layout", True),
        encoding=parameters.get("encoding", "UTF-8"),
        wrap_names=parameters.get("wrap_names", True),
        wrap_width=parameters.get("wrap_width", _DEFAULT_WRAP_WIDTH),
        layout_engine=parameters.get("layout_engine", "graphviz"),
        emit_conditions=parameters.get("emit_conditions", False),
    )

    if hasattr(file_path, "write"):
        file_path.write(text)
    else:
        with open(file_path, "w",
                  encoding=parameters.get("encoding", "utf-8")) as f:
            f.write(text)


def serialize_to_string(
    ucm: UCM,
    layout: bool = True,
    encoding: str = "UTF-8",
    wrap_names: bool = True,
    wrap_width: int = None,
    layout_engine: str = "graphviz",
    emit_conditions: bool = False,
) -> str:
    """Return the XMI serialisation of ``ucm`` as a string.

    Parameters
    ----------
    ucm
        The model to serialise.
    layout
        When ``True`` (the default) and the model has no positioned
        node yet, run the layouter selected by ``layout_engine``.
    layout_engine
        ``"graphviz"`` (default) uses graphviz's ``dot`` engine to
        compute positions — the resulting ``.jucm`` lays out exactly
        like the PNG renderer. Falls back transparently to the built-in
        Sugiyama-style layouter if the graphviz binary is unavailable.
        ``"builtin"`` forces the built-in layouter even when graphviz
        is available.
    encoding
        XML output encoding declared in the prolog. Default ``"UTF-8"``.
    wrap_names
        Wrap long names across multiple lines (default ``True``).
    wrap_width
        Per-line character budget for wrapping; default
        :data:`~pm4py_ucm.objects.ucm.util.name_wrap.DEFAULT_MAX_WIDTH`.
    """
    if wrap_width is None:
        wrap_width = _DEFAULT_WRAP_WIDTH
    if layout and _needs_layout(ucm):
        # Prefer graphviz: its output matches the PNG visualizer and gives
        # a much more readable .jucm than the built-in layered solver. The
        # graphviz layouter returns False (and is then a no-op) if the
        # binary isn't present, in which case we fall back gracefully.
        used_gv = False
        if layout_engine == "graphviz":
            try:
                from ...layout.graphviz_layouter import apply_graphviz_layout
                used_gv = apply_graphviz_layout(ucm)
            except Exception:
                used_gv = False
        if not used_gv:
            apply_layout(ucm)

    # Allocate any pending IDs so we can compute nextGlobalID accurately.
    ucm.assign_ids()

    # Stamp timestamps if absent.
    if not ucm.created:
        ucm.created = _format_timestamp()
    ucm.modified = _format_timestamp()

    w = _XmlWriter(encoding=encoding)
    _emit_urnspec(w, ucm, wrap_names=wrap_names, wrap_width=wrap_width,
                  emit_conditions=emit_conditions)
    return w.text()


# ---------------------------------------------------------------------------
# Top-level structure
# ---------------------------------------------------------------------------

def _emit_urnspec(w, ucm, wrap_names=True, wrap_width=_DEFAULT_WRAP_WIDTH,
                   emit_conditions=False):
    """Emit the ``<urn:URNspec>`` root and all its children, in the order
    jUCMNav itself uses."""
    if wrap_names:
        _wrap = lambda s: wrap_name(s, max_width=wrap_width)
    else:
        _wrap = lambda s: s
    root_attrs = [
        ("xmi:version", "2.0"),
        ("xmlns:xmi", _NS_XMI),
        ("xmlns:xsi", _NS_XSI),
        ("xmlns:ucm.map", _NS_UCM_MAP),
        ("xmlns:urn", _NS_URN),
        ("name", ucm.name or "URNspec"),
        ("author", ucm.author or "pm4py-ucm"),
        ("created", ucm.created),
        ("modified", ucm.modified),
        ("specVersion", UCM.SPEC_VERSION),
        ("urnVersion", UCM.URN_VERSION),
        ("nextGlobalID", str(ucm.next_global_id())),
    ]
    # The stub-and-scenario context is shared across <ucmspec> (for
    # ``instances`` back-references on enumerationTypes) and <urndef>
    # (for ``scenarioStartPoints`` / ``scenarioEndPoints`` back-
    # references on StartPoint / EndPoint nodes). Building it once up
    # here means both emit sites see the same lookup tables.
    if ucm.scenario_groups and not emit_conditions:
        emit_conditions = True
    ctx = _build_stub_context(ucm, emit_conditions=emit_conditions)
    w.open("urn:URNspec", root_attrs)
    _emit_ucmspec(w, ucm, ctx=ctx)
    _emit_grlspec(w, ucm)
    _emit_urndef(w, ucm, _wrap, emit_conditions=emit_conditions, ctx=ctx)
    w.close("urn:URNspec")


def _emit_ucmspec(w, ucm, ctx: Optional["_StubContext"] = None) -> None:
    """Emit ``<ucmspec>`` with the scenario-layer content attached to
    the UCM.

    When the UCM carries no scenario groups, variables, or enumeration
    types (the common case for plain mined-only outputs), the element
    is empty — preserving the legacy byte-for-byte output. When the
    scenario-synthesis pipeline has populated those lists, they are
    emitted as children in the order jUCMNav itself uses:
    ``scenarioGroups`` first, then ``variables``, then
    ``enumerationTypes``."""
    has_content = (
        ucm.scenario_groups or ucm.variables or ucm.enumeration_types
    )
    if not has_content:
        w.empty("ucmspec", [])
        return
    w.open("ucmspec", [])
    for sg in ucm.scenario_groups:
        _emit_scenario_group(w, sg)
    for v in ucm.variables:
        _emit_variable(w, v)
    for et in ucm.enumeration_types:
        _emit_enumeration_type(w, et, ctx=ctx)
    w.close("ucmspec")


def _emit_scenario_group(w, sg: "UCM.ScenarioGroup") -> None:
    """Emit one ``<scenarioGroups>`` element with its scenarios."""
    attrs: List = [
        ("name", sg.effective_name),
        ("id", str(sg.id)),
    ]
    if sg.description:
        attrs.append(("description", sg.description))
    if not sg.scenarios:
        w.empty("scenarioGroups", attrs)
        return
    w.open("scenarioGroups", attrs)
    for sc in sg.scenarios:
        _emit_scenario_def(w, sc)
    w.close("scenarioGroups")


def _emit_scenario_def(w, sc: "UCM.ScenarioDef") -> None:
    """Emit one ``<scenarios>`` element representing a ScenarioDef.

    The element carries:

    * the scenario's name + id;
    * a free-form ``description`` (used by the synthesis pipeline to
      carry the variant's partial-order expression and the case-ID
      list — jUCMNav surfaces this in the scenario panel);
    * one ``<initializations>`` child per variable assignment;
    * one ``<startPoints>`` child per scenario start;
    * one ``<endPoints>`` child per scenario end.

    Booleans default to ``true``, matching jUCMNav's own output.
    """
    attrs: List = [
        ("name", sc.effective_name),
        ("id", str(sc.id)),
    ]
    if sc.description:
        attrs.append(("description", sc.description))
    has_children = bool(
        sc.initializations or sc.start_points or sc.end_points,
    )
    if not has_children:
        w.empty("scenarios", attrs)
        return
    w.open("scenarios", attrs)
    # Attribute order on the children matches jUCMNav's own output
    # (verified against ``aemsURN.jucm``): value/enabled comes first,
    # then the typed reference. This keeps the diff against editor-
    # produced files minimal and avoids cosmetic merge churn.
    for init in sc.initializations:
        init_attrs: List = [
            ("value", init.value),
            ("variable", str(init.variable.id)),
        ]
        w.empty("initializations", init_attrs)
    for sp in sc.start_points:
        sp_attrs: List = [
            ("enabled", "true" if sp.enabled else "false"),
            ("startPoint", str(sp.start_point.id)),
        ]
        w.empty("startPoints", sp_attrs)
    for ep in sc.end_points:
        ep_attrs: List = [
            ("enabled", "true" if ep.enabled else "false"),
        ]
        # ``mandatory`` is emitted only when true (jUCMNav omits it
        # when the scenario doesn't require reaching the end point).
        # The synthesizer sets it to true by default — scenarios that
        # don't enforce reachability would be misleading for variant-
        # driven traceability.
        if ep.mandatory:
            ep_attrs.append(("mandatory", "true"))
        ep_attrs.append(("endPoint", str(ep.end_point.id)))
        w.empty("endPoints", ep_attrs)
    w.close("scenarios")


def _emit_grlspec(w, ucm) -> None:
    """Minimal but well-formed grlspec — empty ``impactModel`` and
    ``featureModel`` are mandatory containment children."""
    w.open("grlspec", [])
    w.empty("impactModel", [])
    w.empty("featureModel", [])
    w.close("grlspec")


def _emit_urndef(w, ucm, _wrap, emit_conditions=False,
                  ctx: Optional["_StubContext"] = None) -> None:
    """Emit ``<urndef>`` with the canonical child order
    responsibilities -> specDiagrams -> components.

    Receives the stub-and-scenario ``ctx`` built once in
    :func:`_emit_urnspec` so the OR-fork-condition filter and the
    scenario back-reference tables are visible to every nested emit
    site.
    """
    if ctx is None:
        # Backwards-compatible fallback for callers that bypassed
        # ``_emit_urnspec`` (none in the package, but keep the option
        # open for hand-written tests).
        if ucm.scenario_groups and not emit_conditions:
            emit_conditions = True
        ctx = _build_stub_context(ucm, emit_conditions=emit_conditions)

    w.open("urndef", [])
    for r in ucm.responsibilities:
        _emit_responsibility(w, ucm, r, _wrap)
    for i, ucm_map in enumerate(ucm.maps):
        _emit_ucmmap(w, ucm, ucm_map, map_index=i, _wrap=_wrap, ctx=ctx)
    for c in ucm.components:
        _emit_component(w, ucm, c, ctx=ctx)
    w.close("urndef")


def _emit_enumeration_type(
    w, et: "UCM.EnumerationType",
    ctx: Optional["_StubContext"] = None,
) -> None:
    """Emit one ``<enumerationTypes>`` element.

    Carries an ``instances`` back-reference attribute when at least
    one :class:`UCM.Variable` uses this enum as its type. jUCMNav
    treats variables without a matching ``instances`` entry as
    untyped and refuses to surface their enumeration values in the
    scenario panel."""
    attrs: List = [
        ("name", et.effective_name),
        ("id", str(et.id)),
        ("values", ",".join(et.values)),
    ]
    if et.description:
        attrs.append(("description", et.description))
    if ctx is not None:
        var_ids = ctx.enum_to_variable_ids.get(id(et))
        if var_ids:
            attrs.append(("instances",
                          " ".join(str(vid) for vid in var_ids)))
    w.empty("enumerationTypes", attrs)


def _emit_variable(w, v: "UCM.Variable") -> None:
    """Emit one ``<variables>`` element with its type discriminator
    and (for enumerations) the back-link to its EnumerationType.

    Attribute order matches jUCMNav's convention (verified against
    ``aemsURN.jucm``): ``name`` -> ``id`` -> ``description`` ->
    ``type`` -> ``enumerationType``."""
    attrs: List = [
        ("name", v.effective_name),
        ("id", str(v.id)),
    ]
    if v.description:
        attrs.append(("description", v.description))
    attrs.append(("type", v.type))
    if v.enumeration_type is not None:
        attrs.append(("enumerationType", str(v.enumeration_type.id)))
    w.empty("variables", attrs)


# ---------------------------------------------------------------------------
# Stub binding context (XPath bookkeeping)
# ---------------------------------------------------------------------------

class _StubContext:
    """Pre-computed XPath references for every plug-in binding in a UCM.

    jUCMNav addresses each binding by its position inside its
    containing stub node, which is itself addressed by its position in
    its containing map. Computing those positions once at the start of
    serialisation lets every downstream emit site (connections, plug-in
    map header, plug-in start/end points) cheaply look up the right
    cross-reference string.
    """

    def __init__(self) -> None:
        # plug-in map -> (parent_map_index, parent_stub_index_in_map,
        #                 binding_index_among_that_stub's_bindings)
        self.plugin_owner: Dict["UCM.UCMmap", Tuple[int, int, int]] = {}
        # PluginBinding -> XPath to its <bindings> element on the parent stub
        self.binding_xpath: Dict["UCM.PluginBinding", str] = {}
        # InBinding  -> XPath to its <in>  child
        self.in_xpath: Dict["UCM.InBinding", str] = {}
        # OutBinding -> XPath to its <out> child
        self.out_xpath: Dict["UCM.OutBinding", str] = {}
        # parent NodeConnection -> InBinding it's the entry of
        self.connection_to_in: Dict["UCM.NodeConnection", "UCM.InBinding"] = {}
        # parent NodeConnection -> OutBinding it's the exit of
        self.connection_to_out: Dict["UCM.NodeConnection", "UCM.OutBinding"] = {}
        # plug-in StartPoint -> InBinding that points at it
        self.startpoint_to_in: Dict["UCM.StartPoint", "UCM.InBinding"] = {}
        # plug-in EndPoint -> OutBinding that points at it
        self.endpoint_to_out: Dict["UCM.EndPoint", "UCM.OutBinding"] = {}
        # ----- Export filters (populated in _build_stub_context) -----
        #
        # Whether ``<condition>`` elements should be emitted on
        # ``<connections>``. Default ``False`` — the converter-generated
        # ``redo`` / ``exit`` / ``branch0`` labels are synthetic and
        # clutter the diagram; users wanting real conditions can pass
        # ``emit_conditions=True`` to the exporter.
        self.emit_conditions: bool = False
        # ids of PathNodes that are :class:`UCM.EmptyPoint` in the model
        # but should be serialised as :class:`UCM.DirectionArrow`. Used
        # to draw direction arrows at the entry of a loop's OR-join,
        # where the routing bend points would otherwise just look like
        # dots on the path.
        self.direction_arrow_ids: set = set()
        # Scenario back-references. jUCMNav stores a bidirectional link
        # between every scenario start/end point and the underlying
        # path-level StartPoint/EndPoint node: the ``<startPoints>``
        # carries a ``startPoint="<id>"`` forward reference, and the
        # ``<nodes xsi:type="ucm.map:StartPoint">`` carries a
        # ``scenarioStartPoints="<xpath> <xpath> ..."`` back-reference
        # listing every ``<startPoints>`` element that points at it.
        # When the back-reference is missing, jUCMNav reports the
        # scenario's start point as "undefined" and refuses to run it.
        # The two dicts below hold the XPath fragment list for each
        # StartPoint / EndPoint, keyed by ``id(node)`` (path-node
        # identity, not the integer URN id).
        self.start_to_scenario_xpaths: Dict[int, List[str]] = {}
        self.end_to_scenario_xpaths: Dict[int, List[str]] = {}
        # EnumerationType back-reference: ``instances`` attribute on
        # ``<enumerationTypes>`` lists the IDs of every Variable that
        # uses this enum as its type. Keyed by ``id(EnumerationType)``.
        self.enum_to_variable_ids: Dict[int, List[int]] = {}
        # For each map (key: ``id(ucm_map)``), the ``id``s of
        # :class:`UCM.ComponentRef`s to keep on export. ComponentRefs
        # with no bound nodes (and no descendants with bound nodes) are
        # omitted so jUCMNav doesn't show empty actor boxes.
        self.kept_cont_refs: Dict[int, set] = {}


def _build_stub_context(
    ucm: UCM, emit_conditions: bool = False,
) -> _StubContext:
    """Walk every stub of every map and build the lookup tables that
    let downstream emitters resolve binding XPaths in O(1).

    Also computes the export filters: which routing :class:`UCM.EmptyPoint`s
    should be promoted to :class:`UCM.DirectionArrow` (those preceding a
    loop's OR-join) and which :class:`UCM.ComponentRef`s have at least one
    bound node and should therefore survive into the ``.jucm``.
    """
    ctx = _StubContext()
    ctx.emit_conditions = emit_conditions

    for map_idx, ucm_map in enumerate(ucm.maps):
        # Direction-arrow promotion: any EmptyPoint whose unique
        # successor is the loop's OrJoin ("LoopJoin", set by
        # ``from_process_tree._attach``) is rendered as a
        # DirectionArrow on export. There are two of these per loop —
        # one on the entry path, one on the redo back-edge — and
        # plain bend points there read worse than direction arrows
        # in jUCMNav.
        for node in ucm_map.nodes:
            if not isinstance(node, UCM.EmptyPoint):
                continue
            succs = node.succ_connections
            if len(succs) != 1:
                continue
            target = succs[0].target
            if isinstance(target, UCM.OrJoin) and target.name == "LoopJoin":
                ctx.direction_arrow_ids.add(id(node))

        # ComponentRef keep-set: a ComponentRef is kept iff some
        # PathNode on this map points at it OR at one of its
        # descendants. The ancestor walk preserves the nesting
        # hierarchy when an inner reference is bound but the outer
        # wrapper has no direct nodes of its own.
        used_directly = {
            n.cont_ref for n in ucm_map.nodes if n.cont_ref is not None
        }
        keep: set = set()
        for cr in used_directly:
            while cr is not None and id(cr) not in keep:
                keep.add(id(cr))
                cr = cr.parent
        ctx.kept_cont_refs[id(ucm_map)] = keep

        for node_idx, node in enumerate(ucm_map.nodes):
            if not isinstance(node, UCM.Stub):
                continue
            for binding_idx, binding in enumerate(node.bindings):
                stub_xpath = (f"//@urndef/@specDiagrams.{map_idx}"
                              f"/@nodes.{node_idx}")
                bind_xpath = f"{stub_xpath}/@bindings.{binding_idx}"
                ctx.binding_xpath[binding] = bind_xpath
                if binding.plugin is not None:
                    ctx.plugin_owner[binding.plugin] = (
                        map_idx, node_idx, binding_idx,
                    )
                for in_idx, ib in enumerate(binding.in_bindings):
                    ib_xpath = f"{bind_xpath}/@in.{in_idx}"
                    ctx.in_xpath[ib] = ib_xpath
                    if ib.stub_entry is not None:
                        ctx.connection_to_in[ib.stub_entry] = ib
                    if ib.start_point is not None:
                        ctx.startpoint_to_in[ib.start_point] = ib
                for out_idx, ob in enumerate(binding.out_bindings):
                    ob_xpath = f"{bind_xpath}/@out.{out_idx}"
                    ctx.out_xpath[ob] = ob_xpath
                    if ob.stub_exit is not None:
                        ctx.connection_to_out[ob.stub_exit] = ob
                    if ob.end_point is not None:
                        ctx.endpoint_to_out[ob.end_point] = ob

    # Scenario-layer back-references. For every ScenarioStartPoint /
    # ScenarioEndPoint, record the XPath of the <startPoints> /
    # <endPoints> child so that the StartPoint / EndPoint node can
    # later emit a back-pointing ``scenarioStartPoints`` /
    # ``scenarioEndPoints`` attribute. Without these jUCMNav rejects
    # the scenario as having an undefined start/end.
    for group_idx, sg in enumerate(ucm.scenario_groups):
        for sc_idx, sc in enumerate(sg.scenarios):
            for sp_idx, sp in enumerate(sc.start_points):
                xpath = (f"//@ucmspec/@scenarioGroups.{group_idx}"
                         f"/@scenarios.{sc_idx}/@startPoints.{sp_idx}")
                ctx.start_to_scenario_xpaths.setdefault(
                    id(sp.start_point), []).append(xpath)
            for ep_idx, ep in enumerate(sc.end_points):
                xpath = (f"//@ucmspec/@scenarioGroups.{group_idx}"
                         f"/@scenarios.{sc_idx}/@endPoints.{ep_idx}")
                ctx.end_to_scenario_xpaths.setdefault(
                    id(ep.end_point), []).append(xpath)

    # EnumerationType ``instances`` back-reference: list the IDs of
    # every Variable whose ``enumeration_type`` points at this enum.
    for v in ucm.variables:
        if v.enumeration_type is not None:
            ctx.enum_to_variable_ids.setdefault(
                id(v.enumeration_type), []).append(v.id)

    return ctx


# ---------------------------------------------------------------------------
# Responsibilities and Components (URN-level definitions)
# ---------------------------------------------------------------------------

def _emit_responsibility(
    w: "_XmlWriter", ucm: UCM, resp: "UCM.Responsibility", _wrap=lambda s: s,
) -> None:
    """Emit a ``<responsibilities>`` element with its bidirectional
    ``respRefs`` back-reference listing the RespRefs that point at it."""
    refs = ucm.find_resp_refs(resp)
    attrs = [("name", _wrap(resp.effective_name)), ("id", str(resp.id))]
    if refs:
        attrs.append(("respRefs", " ".join(str(r.id) for r in refs)))
    if resp.description:
        attrs.append(("description", resp.description))
    if resp.expression:
        attrs.append(("expression", resp.expression))
    w.empty("responsibilities", attrs)


def _emit_component(
    w: "_XmlWriter", ucm: UCM, comp: "UCM.ComponentElement",
    ctx: Optional["_StubContext"] = None,
) -> None:
    """Emit a ``<components>`` element with its bidirectional
    ``contRefs`` back-reference. ``kind`` is omitted when it equals the
    default ``Team``.

    The ``contRefs`` list is restricted to the ComponentRefs that
    actually survive emission (per :attr:`_StubContext.kept_cont_refs`);
    listing skipped ones would produce dangling XPath references in
    the XMI."""
    refs = ucm.find_cont_refs(comp)
    if ctx is not None and ctx.kept_cont_refs:
        kept_total: set = set()
        for s in ctx.kept_cont_refs.values():
            kept_total.update(s)
        refs = [r for r in refs if id(r) in kept_total]
    attrs = [("name", comp.effective_name), ("id", str(comp.id))]
    # Per-component pastel colour, hashed deterministically from the
    # component name so the same actor wears the same colour across
    # maps in this file *and* across runs. jUCMNav reads ``lineColor``
    # / ``fillColor`` (plus ``filled="true"``) directly off the
    # ``<components>`` definition as SWT RGB triplets
    # (``r,g,b`` decimal). Same colours the PNG visualizer uses.
    if comp.name:
        fill_hex, line_hex = _component_color(comp.name)
        attrs.append(("lineColor", _hex_to_rgb(line_hex)))
        attrs.append(("fillColor", _hex_to_rgb(fill_hex)))
        attrs.append(("filled", "true"))
    if refs:
        attrs.append(("contRefs", " ".join(str(r.id) for r in refs)))
    if comp.description:
        attrs.append(("description", comp.description))
    kind_value = (
        comp.kind.value if hasattr(comp.kind, "value") else str(comp.kind)
    )
    if kind_value and kind_value != UCM.ComponentElement.Kind.TEAM.value:
        attrs.append(("kind", kind_value))
    w.empty("components", attrs)


# ---------------------------------------------------------------------------
# UCMmap (specDiagrams)
# ---------------------------------------------------------------------------

def _emit_ucmmap(
    w: "_XmlWriter",
    ucm: UCM,
    m: "UCM.UCMmap",
    map_index: int,
    _wrap=lambda s: s,
    ctx: Optional["_StubContext"] = None,
) -> None:
    """Emit one ``<specDiagrams xsi:type="ucm.map:UCMmap">`` with its
    nodes, component references, and connections — in that order."""
    attrs = [
        ("xsi:type", "ucm.map:UCMmap"),
        ("name", m.effective_name),
        ("id", str(m.id)),
    ]
    if m.description:
        attrs.append(("description", m.description))
    # When this map is a plug-in for a stub, jUCMNav expects
    # ``parentStub`` to point at the parent's PluginBinding.
    if ctx is not None and m in ctx.plugin_owner:
        parent_map_idx, parent_node_idx, binding_idx = ctx.plugin_owner[m]
        attrs.append(("parentStub",
                      f"//@urndef/@specDiagrams.{parent_map_idx}"
                      f"/@nodes.{parent_node_idx}"
                      f"/@bindings.{binding_idx}"))
    w.open("specDiagrams", attrs)

    # NodeConnection -> index, used to build XPath references from nodes.
    conn_index = {c: i for i, c in enumerate(m.connections)}

    for node in m.nodes:
        _emit_node(w, m, node, map_index, conn_index, _wrap=_wrap, ctx=ctx)
    # Filter ComponentRefs to those with at least one bound node (or a
    # descendant with one) on this map. Computed once in
    # :func:`_build_stub_context`; falls through to "keep everything"
    # when no context is supplied (legacy callers).
    keep_ids = (
        ctx.kept_cont_refs.get(id(m)) if ctx is not None else None
    )
    for cr in m.cont_refs:
        if keep_ids is not None and id(cr) not in keep_ids:
            continue
        _emit_cont_ref(w, m, cr, ctx=ctx)
    for c in m.connections:
        _emit_connection(w, c, ctx=ctx)

    w.close("specDiagrams")


def _emit_node(
    w: "_XmlWriter",
    m: "UCM.UCMmap",
    n: "UCM.PathNode",
    map_index: int,
    conn_index: dict,
    _wrap=lambda s: s,
    ctx: Optional["_StubContext"] = None,
) -> None:
    """Emit a single ``<nodes>`` element with the right ``xsi:type``,
    geometry, container reference, succ/pred XPath lists, and any
    type-specific child elements (``<label/>``, ``<precondition>``,
    ``<bindings>``, etc.).
    """
    xsi_type = _NODE_XSI_TYPES.get(type(n), "ucm.map:PathNode")
    # Promote pre-loop-join EmptyPoints to DirectionArrows on export
    # (see :func:`_build_stub_context` for the detection logic). The
    # in-memory Python class stays ``EmptyPoint`` so PNG rendering and
    # programmatic access are unaffected — only the serialised XMI
    # element type is changed.
    if (ctx is not None
            and isinstance(n, UCM.EmptyPoint)
            and id(n) in ctx.direction_arrow_ids):
        xsi_type = _NODE_XSI_TYPES[UCM.DirectionArrow]

    # RespRef and Stub both carry user-meaningful display names worth
    # wrapping (responsibility text and plug-in map names, respectively).
    # Control nodes (StartPoint, OrFork, …) keep their short
    # type-derived names so jUCMNav doesn't break "OrFork49" across
    # lines.
    display = (_wrap(n.effective_name)
               if isinstance(n, (UCM.RespRef, UCM.Stub))
               else n.effective_name)
    attrs: List = [
        ("xsi:type", xsi_type),
        ("name", display),
        ("id", str(n.id)),
        ("x", str(int(n.x))),
        ("y", str(int(n.y))),
    ]
    if n.description:
        attrs.append(("description", n.description))

    # contRef: integer ID of the ComponentRef this node sits inside.
    if n.cont_ref is not None:
        attrs.append(("contRef", str(n.cont_ref.id)))

    # succ / pred: space-separated XPath fragments to anonymous connections.
    if n.succ_connections:
        attrs.append(("succ", _xpath_connections(
            map_index, (conn_index[c] for c in n.succ_connections))))
    if n.pred_connections:
        attrs.append(("pred", _xpath_connections(
            map_index, (conn_index[c] for c in n.pred_connections))))

    # RespRef: respDef integer ID.
    if isinstance(n, UCM.RespRef) and n.resp_def is not None:
        attrs.append(("respDef", str(n.resp_def.id)))

    # Stub: ``dynamic="true"`` when applicable. jUCMNav defaults to
    # static (``false``) and omits the attribute in that case, so we do
    # the same for byte-identical round-trips on static stubs.
    if isinstance(n, UCM.Stub) and getattr(n, "dynamic", False):
        attrs.append(("dynamic", "true"))

    # Plug-in StartPoint -> inBindings; plug-in EndPoint -> outBindings.
    # These attributes only appear on start/end nodes of a plug-in map
    # (jUCMNav uses them to find back the parent binding from the
    # plug-in side).
    if ctx is not None:
        if isinstance(n, UCM.StartPoint) and n in ctx.startpoint_to_in:
            attrs.append(("inBindings", ctx.in_xpath[ctx.startpoint_to_in[n]]))
        if isinstance(n, UCM.EndPoint) and n in ctx.endpoint_to_out:
            attrs.append(("outBindings",
                          ctx.out_xpath[ctx.endpoint_to_out[n]]))
        # Bidirectional scenario back-references — the missing half of
        # the link that jUCMNav's scenario tool follows when resolving
        # a ScenarioStartPoint / ScenarioEndPoint. Without this the
        # editor reports the scenario start/end as "undefined".
        if isinstance(n, UCM.StartPoint):
            xpaths = ctx.start_to_scenario_xpaths.get(id(n))
            if xpaths:
                attrs.append(("scenarioStartPoints", " ".join(xpaths)))
        if isinstance(n, UCM.EndPoint):
            xpaths = ctx.end_to_scenario_xpaths.get(id(n))
            if xpaths:
                attrs.append(("scenarioEndPoints", " ".join(xpaths)))

    # Type-specific children?
    pre = isinstance(n, UCM.StartPoint) and n.pre_condition is not None
    post = isinstance(n, UCM.EndPoint) and n.post_condition is not None
    needs_label = isinstance(n, _NODES_WITH_LABEL)
    has_bindings = (isinstance(n, UCM.Stub) and bool(n.bindings)
                    and ctx is not None)

    if not (pre or post or needs_label or has_bindings):
        w.empty("nodes", attrs)
        return

    w.open("nodes", attrs)
    if needs_label:
        label_attrs: List = []
        dx, dy = _compute_label_delta(n, m)
        if dx:
            label_attrs.append(("deltaX", str(dx)))
        if dy:
            label_attrs.append(("deltaY", str(dy)))
        w.empty("label", label_attrs)
    if pre:
        _emit_condition_element(w, "precondition", n.pre_condition)
    if post:
        _emit_condition_element(w, "postcondition", n.post_condition)
    if has_bindings:
        for binding in n.bindings:
            _emit_plugin_binding(w, binding, ctx)
    w.close("nodes")


#: Horizontal tolerance for the "is something else in my column?"
#: probe. Responsibility names render at ~70–110 pixels wide in
#: jUCMNav's default font, so a sibling within 80 pixels x and a
#: similar band of y is a credible label clash.
_LABEL_X_TOLERANCE = 80
#: Vertical band the probe examines. ~80 pixels covers the typical
#: gap between adjacent ranks in a UCM layout.
_LABEL_Y_CLEARANCE = 80
#: jUCMNav's deltaY convention is inverted vs the model's pixel y axis:
#: a *negative* deltaY moves the label *downwards* on the diagram, a
#: *positive* deltaY moves it further *upwards*. The magnitudes are
#: chosen larger than the prior 20 px so labels actually clear
#: neighbouring paths and component rectangles — 20 px was too timid
#: and labels still ended up sitting on top of adjacent edges.
_LABEL_BELOW_DELTA_Y = -55
_LABEL_FURTHER_ABOVE_DELTA_Y = 40


def _compute_label_delta(
    node: "UCM.PathNode", ucm_map: "UCM.UCMmap",
) -> Tuple[int, int]:
    """Return ``(deltaX, deltaY)`` for the ``<label>`` child of ``node``.

    The heuristic looks at every other element on the same map and
    counts how many sit in a tight band directly *above* and *below*
    the current node (within :data:`_LABEL_X_TOLERANCE` x and
    :data:`_LABEL_Y_CLEARANCE` y). The label is then placed on the
    quieter side:

    * If something is above and the area below is clear → label below.
    * If both sides have neighbours but above has more → label below.
    * If both sides have neighbours but below has more (or equal) →
      nudge the label *further above* (positive deltaY) to clear the
      symbol entirely.
    * If both sides are clear → no override; jUCMNav uses its default.

    Bound elements (``cont_ref`` set) sit inside their actor
    swim-lane where the cluster already gives the label clear space,
    so the heuristic doesn't touch them. Likewise for elements that
    haven't been laid out yet (every coordinate still at the origin).
    """
    if not isinstance(node, (UCM.RespRef, UCM.Stub)):
        return (0, 0)
    if node.cont_ref is not None:
        return (0, 0)
    xe, ye = int(node.x), int(node.y)
    if xe == 0 and ye == 0:
        return (0, 0)
    above = below = 0
    for other in ucm_map.nodes:
        if other is node:
            continue
        ox, oy = int(other.x), int(other.y)
        if abs(ox - xe) >= _LABEL_X_TOLERANCE:
            continue
        dy = ye - oy
        if 0 < dy < _LABEL_Y_CLEARANCE:
            above += 1
        elif 0 < -dy < _LABEL_Y_CLEARANCE:
            below += 1
    if above == 0 and below == 0:
        return (0, 0)
    if above > 0 and below == 0:
        return (0, _LABEL_BELOW_DELTA_Y)
    if above > below:
        return (0, _LABEL_BELOW_DELTA_Y)
    # below >= above: keep label above but push it further up so it
    # clears the symbol and any side-by-side siblings.
    return (0, _LABEL_FURTHER_ABOVE_DELTA_Y)


def _emit_plugin_binding(
    w: "_XmlWriter",
    binding: "UCM.PluginBinding",
    ctx: "_StubContext",
) -> None:
    """Emit one ``<bindings plugin="...">`` element with its ``<in>``,
    ``<out>`` and optional ``<precondition>`` children."""
    b_attrs: List = []
    if binding.plugin is not None:
        b_attrs.append(("plugin", str(binding.plugin.id)))
    has_children = (binding.in_bindings or binding.out_bindings or
                    binding.precondition is not None)
    if not has_children:
        w.empty("bindings", b_attrs)
        return
    w.open("bindings", b_attrs)
    for ib in binding.in_bindings:
        in_attrs: List = []
        if ib.start_point is not None:
            in_attrs.append(("startPoint", str(ib.start_point.id)))
        if ib.stub_entry is not None:
            # The stub_entry is a NodeConnection on the parent map. We
            # reach it via the same XPath the parent map's <connections>
            # uses. The connection's enclosing map index and position
            # were captured when we built ctx, but we can reconstruct
            # them cheaply from the binding's owner.
            entry_xpath = _connection_xpath_for(ib.stub_entry, binding, ctx)
            if entry_xpath:
                in_attrs.append(("stubEntry", entry_xpath))
        w.empty("in", in_attrs)
    for ob in binding.out_bindings:
        out_attrs: List = []
        if ob.end_point is not None:
            out_attrs.append(("endPoint", str(ob.end_point.id)))
        if ob.stub_exit is not None:
            exit_xpath = _connection_xpath_for(ob.stub_exit, binding, ctx)
            if exit_xpath:
                out_attrs.append(("stubExit", exit_xpath))
        w.empty("out", out_attrs)
    if binding.precondition is not None:
        _emit_condition_element(w, "precondition", binding.precondition)
    w.close("bindings")


def _connection_xpath_for(
    conn: "UCM.NodeConnection",
    binding: "UCM.PluginBinding",
    ctx: "_StubContext",
) -> Optional[str]:
    """Resolve the XPath of ``conn`` — a NodeConnection on the parent
    map of ``binding``'s stub. We look up the parent-map index from
    the context's ``plugin_owner`` table (it was recorded when the
    stub was discovered) and then find ``conn``'s position in that
    map's ``connections`` list."""
    plugin = binding.plugin
    if plugin is None or plugin not in ctx.plugin_owner:
        return None
    parent_map_idx, _, _ = ctx.plugin_owner[plugin]
    parent_map = binding.stub._map
    if parent_map is None:
        return None
    try:
        conn_idx = parent_map.connections.index(conn)
    except ValueError:
        return None
    return (f"//@urndef/@specDiagrams.{parent_map_idx}"
            f"/@connections.{conn_idx}")


def _emit_cont_ref(
    w: "_XmlWriter", m: "UCM.UCMmap", cr: "UCM.ComponentRef",
    ctx: Optional["_StubContext"] = None,
) -> None:
    """Emit one ``<contRefs xsi:type="ucm.map:ComponentRef">`` with
    geometry, contained-node list, child-ref list, parent link, and
    ``<label/>`` child.

    When ``ctx`` carries a non-empty :attr:`_StubContext.kept_cont_refs`
    set for this map, the ``children`` attribute lists only kept
    children — skipped ComponentRefs must not appear as dangling
    references."""
    attrs: List = [
        ("xsi:type", "ucm.map:ComponentRef"),
        ("name", cr.effective_name),
        ("id", str(cr.id)),
        ("x", str(int(cr.x))),
        ("y", str(int(cr.y))),
        ("width", str(int(cr.width))),
        ("height", str(int(cr.height))),
        ("contDef", str(cr.cont_def.id)),
    ]
    contained = m.nodes_in(cr)
    if contained:
        attrs.append(("nodes", " ".join(str(n.id) for n in contained)))
    keep_ids = (
        ctx.kept_cont_refs.get(id(m)) if ctx is not None else None
    )
    if cr.children:
        kept_children = [
            c for c in cr.children
            if keep_ids is None or id(c) in keep_ids
        ]
        if kept_children:
            attrs.append((
                "children",
                " ".join(str(c.id) for c in kept_children),
            ))
    if cr.parent is not None:
        attrs.append(("parent", str(cr.parent.id)))
    w.open("contRefs", attrs)
    w.empty("label", [])
    w.close("contRefs")


def _emit_connection(
    w: "_XmlWriter",
    c: "UCM.NodeConnection",
    ctx: Optional["_StubContext"] = None,
) -> None:
    """Emit one ``<connections xsi:type="ucm.map:NodeConnection">``. The
    connection has no ``id`` (jUCMNav does not assign IDs to connections);
    ``source`` and ``target`` carry the integer IDs of the path nodes.

    If ``c`` is the entry of a plug-in binding (i.e. the parent-map arc
    that *enters* a stub bound to a sub-map), ``inBindings`` points at
    the corresponding ``<in>`` element. Likewise ``outBindings`` for
    arcs *leaving* a stub. jUCMNav uses these cross-references to draw
    the dotted lines that connect parent paths to plug-in start/end
    points when the user drills into the sub-map.
    """
    attrs: List = [
        ("xsi:type", "ucm.map:NodeConnection"),
        ("source", str(c.source.id)),
        ("target", str(c.target.id)),
    ]
    if ctx is not None:
        if c in ctx.connection_to_in:
            attrs.append(("inBindings",
                          ctx.in_xpath[ctx.connection_to_in[c]]))
        if c in ctx.connection_to_out:
            attrs.append(("outBindings",
                          ctx.out_xpath[ctx.connection_to_out[c]]))
    emit_cond = (
        c.condition is not None
        and (ctx is None or ctx.emit_conditions)
    )
    if not emit_cond:
        w.empty("connections", attrs)
        return
    w.open("connections", attrs)
    _emit_condition_element(w, "condition", c.condition)
    w.close("connections")


def _emit_condition_element(
    w: "_XmlWriter", tag: str, cond: "UCM.Condition"
) -> None:
    """Emit a ``<condition>``, ``<precondition>``, or ``<postcondition>``
    child element with the right attribute set."""
    attrs: List = []
    if cond.delta_x or cond.delta_y:
        attrs.append(("deltaX", str(int(cond.delta_x))))
        attrs.append(("deltaY", str(int(cond.delta_y))))
    attrs.append(("label", cond.label or ""))
    attrs.append(("expression", cond.expression or "true"))
    if cond.description:
        attrs.append(("description", cond.description))
    w.empty(tag, attrs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _xpath_connections(map_index: int, indices: Iterable[int]) -> str:
    """Build a space-separated list of XPath fragments referencing
    connections by their index inside ``specDiagrams.{map_index}``."""
    return " ".join(
        f"//@urndef/@specDiagrams.{map_index}/@connections.{i}"
        for i in indices
    )


def _needs_layout(ucm: UCM) -> bool:
    """True if every node still sits at (0, 0) — i.e. no layouter has run
    yet and the user has not assigned coordinates by hand."""
    for m in ucm.maps:
        for n in m.nodes:
            if n.x != 0 or n.y != 0:
                return False
    # Default: layout only if at least one node exists; an empty model
    # doesn't need it either way.
    return any(m.nodes for m in ucm.maps)


def _format_timestamp() -> str:
    """jUCMNav-style timestamp string.

    jUCMNav writes timestamps such as ``May 12, 2026, 2:43:23 PM EDT``.
    We match that format with the local timezone abbreviation; if the
    abbreviation is unavailable (some environments) the offset is used.
    """
    now = datetime.datetime.now()
    tz = time.strftime("%Z") or now.astimezone().strftime("%z")
    # Use %d / %I and strip a leading zero by hand for cross-platform support
    # (Windows' strftime does not support the %-d / %-I extensions).
    base = now.strftime("%B %d, %Y, %I:%M:%S %p")
    base = base.replace(" 0", " ", 1)  # day
    # Day-of-month may not appear right after a space if the month name
    # already ended without a trailing space — but our format guarantees it.
    # Trim leading zero on hour, too:
    base_parts = base.rsplit(", ", 1)
    if len(base_parts) == 2:
        time_part = base_parts[1]
        if time_part.startswith("0"):
            time_part = time_part[1:]
        base = base_parts[0] + ", " + time_part
    return f"{base} {tz}".strip()


# ---------------------------------------------------------------------------
# Minimal XML writer
# ---------------------------------------------------------------------------

class _XmlWriter:
    """Tiny XML writer that emits exactly the format jUCMNav uses.

    Using a hand-written writer (rather than ``ElementTree`` or ``lxml``)
    keeps the output byte-stable and lets us preserve the exact attribute
    order, opaque namespace URIs, and self-closing-tag conventions that
    jUCMNav itself writes."""

    def __init__(self, encoding: str = "UTF-8", indent: str = "  ") -> None:
        self._buf = io.StringIO()
        self._depth = 0
        self._indent = indent
        self._buf.write(f'<?xml version="1.0" encoding="{encoding}"?>\n')

    # ----- public API ----------------------------------------------------

    def open(self, tag: str, attrs) -> None:
        self._buf.write(self._indent * self._depth)
        self._buf.write(f"<{tag}{self._attrs(attrs)}>\n")
        self._depth += 1

    def close(self, tag: str) -> None:
        self._depth -= 1
        self._buf.write(self._indent * self._depth)
        self._buf.write(f"</{tag}>\n")

    def empty(self, tag: str, attrs) -> None:
        self._buf.write(self._indent * self._depth)
        self._buf.write(f"<{tag}{self._attrs(attrs)}/>\n")

    def text(self) -> str:
        return self._buf.getvalue()

    # ----- internals -----------------------------------------------------

    @staticmethod
    def _attrs(pairs) -> str:
        parts = []
        for k, v in pairs:
            parts.append(f' {k}="{_xml_escape_attr(str(v))}"')
        return "".join(parts)


def _xml_escape_attr(s: str) -> str:
    """Escape characters not permitted in an XML attribute value."""
    return (s.replace("&", "&amp;")
             .replace('"', "&quot;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace("\n", "&#xA;")
             .replace("\r", "&#xD;")
             .replace("\t", "&#x9;"))
