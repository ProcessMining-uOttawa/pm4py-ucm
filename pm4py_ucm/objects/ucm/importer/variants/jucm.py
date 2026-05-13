"""Importer for the jUCMNav ``.jucm`` file format (EMF XMI 2.0).

Round-trips with :mod:`pm4py_ucm.objects.ucm.exporter.variants.jucm`.

The parser handles two reference styles transparently:

* **Integer IDs** (``"35"``), used by jUCMNav for definition/reference
  cross-links: ``RespRef.respDef``, ``ComponentRef.contDef``,
  ``PathNode.contRef``, connection ``source``/``target``,
  ``Responsibility.respRefs``, ``Component.contRefs``.
* **XPath fragments** (``"//@urndef/@specDiagrams.0/@connections.7"``),
  used for references to anonymous targets — only relevant for
  ``PathNode.succ`` / ``PathNode.pred`` (connections have no IDs).

For backward compatibility with output of earlier versions of this
package, the importer also accepts:

* the legacy attribute names ``succConnections`` / ``predConnections``;
* connection ``source`` / ``target`` given as XPath fragments;
* the legacy element name ``componentDefinitions``;
* the legacy attribute name ``compRef`` (now ``contRef``);
* a ``<condition>`` whose payload is in ``expression`` rather than
  ``label`` (the value is treated as the label).

Anything outside the UCM scenario notation — GRL spec contents, scenario
definitions, performance information, dynamic contexts — is parsed only
enough to be ignored gracefully; the round-trip is UCM-only.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Dict, IO, List, Optional, Tuple, Union

from ...obj import UCM


_NS_XSI = "{http://www.w3.org/2001/XMLSchema-instance}"

#: All path-node ``xsi:type`` values jUCMNav emits, mapped to our classes.
_XSI_TYPE_TO_CLS = {
    "ucm.map:StartPoint":      UCM.StartPoint,
    "ucm.map:EndPoint":        UCM.EndPoint,
    "ucm.map:EmptyPoint":      UCM.EmptyPoint,
    "ucm.map:RespRef":         UCM.RespRef,
    "ucm.map:OrFork":          UCM.OrFork,
    "ucm.map:OrJoin":          UCM.OrJoin,
    "ucm.map:AndFork":         UCM.AndFork,
    "ucm.map:AndJoin":         UCM.AndJoin,
    "ucm.map:WaitingPlace":    UCM.WaitingPlace,
    "ucm.map:Timer":           UCM.Timer,
    "ucm.map:Stub":            UCM.Stub,
    "ucm.map:Connect":         UCM.Connect,
    "ucm.map:DirectionArrow":  UCM.DirectionArrow,
    "ucm.map:FailurePoint":    UCM.FailurePoint,
    "ucm.map:Anything":        UCM.Anything,
}

_INT_RE = re.compile(r"^\d+$")


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def apply(
    file_path: Union[str, IO],
    parameters: Optional[dict] = None,
) -> UCM:
    """Parse a ``.jucm`` file and return the corresponding :class:`UCM`."""
    if hasattr(file_path, "read"):
        tree = ET.parse(file_path)
    else:
        tree = ET.parse(str(file_path))
    return _parse_root(tree.getroot())


def parse_string(xml_text: str) -> UCM:
    """Parse a UCM model from a ``.jucm`` XML string."""
    return _parse_root(ET.fromstring(xml_text))


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

def _parse_root(root: ET.Element) -> UCM:
    if not root.tag.endswith("URNspec"):
        raise ValueError(f"Expected <urn:URNspec> as root, got <{root.tag}>")

    ucm = UCM(
        name=_unwrap_name(root.get("name", "URNspec")),
        description=root.get("description", ""),
        author=root.get("author", "pm4py-ucm"),
    )
    ucm.created = root.get("created", "")
    ucm.modified = root.get("modified", "")

    urndef = root.find("urndef")
    if urndef is None:
        return ucm

    # ---- Resolution tables --------------------------------------------
    by_id: Dict[int, UCM.URNmodelElement] = {}
    by_xpath: Dict[str, UCM.URNmodelElement] = {}

    # ---- Pass 1: Responsibility and Component DEFINITIONS ------------
    # Indexed first so that path nodes and component refs can resolve their
    # back-pointers as soon as they're parsed.
    for i, resp_el in enumerate(urndef.findall("responsibilities")):
        resp = UCM.Responsibility(
            id=_int_or_none(resp_el.get("id")),
            name=_unwrap_name(resp_el.get("name", "")),
            description=resp_el.get("description", ""),
            expression=resp_el.get("expression", ""),
        )
        ucm.add_responsibility(resp)
        if resp._id is not None:
            ucm._counter.reserve(resp._id)
            by_id[resp._id] = resp
        by_xpath[f"//@urndef/@responsibilities.{i}"] = resp

    # jUCMNav uses ``components``; older files used ``componentDefinitions``.
    for tagname in ("components", "componentDefinitions"):
        for i, comp_el in enumerate(urndef.findall(tagname)):
            kind_str = comp_el.get("kind")
            try:
                kind = (
                    UCM.ComponentElement.Kind(kind_str)
                    if kind_str else UCM.ComponentElement.Kind.TEAM
                )
            except ValueError:
                kind = UCM.ComponentElement.Kind.OTHER
            comp = UCM.ComponentElement(
                id=_int_or_none(comp_el.get("id")),
                name=_unwrap_name(comp_el.get("name", "")),
                description=comp_el.get("description", ""),
                kind=kind,
            )
            ucm.components.append(comp)
            comp._owner = ucm
            if comp._id is not None:
                ucm._counter.reserve(comp._id)
                by_id[comp._id] = comp
            by_xpath[f"//@urndef/@{tagname}.{i}"] = comp

    # ---- Pass 2: maps -----------------------------------------------
    # Two-stage: build skeletons first so cross-map references resolve.
    deferred_resp_def: List[Tuple[UCM.RespRef, str]] = []
    deferred_cont_ref: List[Tuple[UCM.PathNode, str]] = []
    deferred_parent: List[Tuple[UCM.ComponentRef, str]] = []
    # Stub bindings can't be resolved until every map's nodes and
    # connections are parsed (they reference plug-in maps, NodeConnections
    # and StartPoint/EndPoint nodes across maps). Stash the (stub, XML
    # element) pairs and wire them up after the main loop.
    deferred_bindings: List[Tuple[UCM.Stub, ET.Element]] = []

    for map_idx, diag_el in enumerate(urndef.findall("specDiagrams")):
        xtype = diag_el.get(_NS_XSI + "type", "")
        if xtype and "UCMmap" not in xtype:
            # Skip GRLGraphs and other diagram kinds — we only model UCMs.
            continue

        ucm_map = UCM.UCMmap(
            id=_int_or_none(diag_el.get("id")),
            name=_unwrap_name(diag_el.get("name", f"Map{map_idx}")),
            description=diag_el.get("description", ""),
        )
        ucm.add_map(ucm_map)
        if ucm_map._id is not None:
            by_id[ucm_map._id] = ucm_map
        prefix = f"//@urndef/@specDiagrams.{map_idx}"
        by_xpath[prefix] = ucm_map

        # ---- nodes --------------------------------------------------
        for n_idx, node_el in enumerate(diag_el.findall("nodes")):
            node = _build_node(node_el)
            if node is None:
                continue
            ucm_map.add_node(node)
            if node._id is not None:
                by_id[node._id] = node
                ucm._counter.reserve(node._id)
            by_xpath[f"{prefix}/@nodes.{n_idx}"] = node

            # RespRef.respDef
            if isinstance(node, UCM.RespRef):
                rd = node_el.get("respDef")
                if rd:
                    deferred_resp_def.append((node, rd))

            # PathNode.contRef (legacy: compRef)
            cr_ref = node_el.get("contRef") or node_el.get("compRef")
            if cr_ref:
                deferred_cont_ref.append((node, cr_ref))

            # Pre/post conditions
            if isinstance(node, UCM.StartPoint):
                pc = node_el.find("precondition")
                if pc is not None:
                    node.pre_condition = _parse_condition(pc)
            if isinstance(node, UCM.EndPoint):
                postc = node_el.find("postcondition")
                if postc is not None:
                    node.post_condition = _parse_condition(postc)

            # Stub bindings — captured here, resolved in a later pass
            # once all maps' nodes and connections are available.
            if isinstance(node, UCM.Stub):
                for b_el in node_el.findall("bindings"):
                    deferred_bindings.append((node, b_el))

        # ---- contRefs (component references) -----------------------
        for r_idx, cref_el in enumerate(diag_el.findall("contRefs")):
            cont_def_attr = cref_el.get("contDef", "")
            cont_def = _resolve(by_id, by_xpath, cont_def_attr)
            if not isinstance(cont_def, UCM.ComponentElement):
                continue
            cref = UCM.ComponentRef(
                cont_def=cont_def,
                id=_int_or_none(cref_el.get("id")),
                x=_int_or(cref_el.get("x"), 0),
                y=_int_or(cref_el.get("y"), 0),
                width=_int_or(cref_el.get("width"), 200),
                height=_int_or(cref_el.get("height"), 100),
            )
            ucm_map.add_cont_ref(cref)
            if cref._id is not None:
                by_id[cref._id] = cref
                ucm._counter.reserve(cref._id)
            by_xpath[f"{prefix}/@contRefs.{r_idx}"] = cref

            parent_attr = cref_el.get("parent")
            if parent_attr:
                deferred_parent.append((cref, parent_attr))

        # ---- connections -------------------------------------------
        for c_idx, conn_el in enumerate(diag_el.findall("connections")):
            src = _resolve(by_id, by_xpath, conn_el.get("source", ""))
            tgt = _resolve(by_id, by_xpath, conn_el.get("target", ""))
            if not isinstance(src, UCM.PathNode) or not isinstance(tgt, UCM.PathNode):
                continue
            cond_el = conn_el.find("condition")
            condition = _parse_condition(cond_el) if cond_el is not None else None
            conn = UCM.NodeConnection(
                source=src, target=tgt,
                name=_unwrap_name(conn_el.get("name", "")),
                condition=condition,
            )
            ucm_map.add_connection(conn)
            # Connections have no ID in modern files; if present (legacy),
            # reserve it on the counter to prevent collisions.
            cid = _int_or_none(conn_el.get("id"))
            if cid is not None:
                conn.set_id(cid)
            by_xpath[f"{prefix}/@connections.{c_idx}"] = conn

    # ---- Resolve deferred references --------------------------------
    for node, ref in deferred_resp_def:
        resolved = _resolve(by_id, by_xpath, ref)
        if isinstance(resolved, UCM.Responsibility):
            node.set_resp_def(resolved)

    for node, ref in deferred_cont_ref:
        resolved = _resolve(by_id, by_xpath, ref)
        if isinstance(resolved, UCM.ComponentRef):
            node.cont_ref = resolved

    for cref, ref in deferred_parent:
        resolved = _resolve(by_id, by_xpath, ref)
        if isinstance(resolved, UCM.ComponentRef):
            cref.set_parent(resolved)

    # ---- Resolve stub bindings -------------------------------------
    # All maps, nodes and connections are now in place and registered
    # in by_id / by_xpath, so the cross-references inside <bindings>
    # can be wired up.
    for stub, b_el in deferred_bindings:
        binding = _build_plugin_binding(stub, b_el, by_id, by_xpath)
        if binding is not None:
            stub.bindings.append(binding)

    return ucm


def _build_plugin_binding(
    stub: "UCM.Stub",
    b_el: ET.Element,
    by_id: dict,
    by_xpath: dict,
) -> Optional["UCM.PluginBinding"]:
    """Construct a :class:`UCM.PluginBinding` from a ``<bindings>`` XML
    element. Returns ``None`` when the bound plug-in map can't be
    resolved (treating that as a soft failure rather than aborting the
    whole import — partial models still load)."""
    plugin_ref = b_el.get("plugin", "")
    plugin = _resolve(by_id, by_xpath, plugin_ref)
    if not isinstance(plugin, UCM.UCMmap):
        return None
    binding = UCM.PluginBinding(stub=stub, plugin=plugin)

    for in_el in b_el.findall("in"):
        entry = _resolve(by_id, by_xpath, in_el.get("stubEntry", ""))
        sp = _resolve(by_id, by_xpath, in_el.get("startPoint", ""))
        if isinstance(entry, UCM.NodeConnection) and isinstance(sp, UCM.StartPoint):
            binding.add_in(parent_connection=entry, plugin_start=sp)

    for out_el in b_el.findall("out"):
        exit_ = _resolve(by_id, by_xpath, out_el.get("stubExit", ""))
        ep = _resolve(by_id, by_xpath, out_el.get("endPoint", ""))
        if isinstance(exit_, UCM.NodeConnection) and isinstance(ep, UCM.EndPoint):
            binding.add_out(plugin_end=ep, parent_connection=exit_)

    pc_el = b_el.find("precondition")
    if pc_el is not None:
        binding.precondition = _parse_condition(pc_el)

    return binding


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_node(el: ET.Element) -> Optional[UCM.PathNode]:
    """Construct a path node from a ``<nodes …>`` element."""
    xtype = el.get(_NS_XSI + "type", "")
    cls = _XSI_TYPE_TO_CLS.get(xtype)
    if cls is None:
        # Unknown subtype — fall back to EmptyPoint to keep the path
        # connected. We never want to silently drop a node.
        cls = UCM.EmptyPoint
    n_id = _int_or_none(el.get("id"))
    name = _unwrap_name(el.get("name", ""))
    descr = el.get("description", "")
    if cls is UCM.Stub:
        node = cls(
            id=n_id, name=name, description=descr,
            dynamic=(el.get("dynamic", "false").lower() == "true"),
        )
    else:
        node = cls(id=n_id, name=name, description=descr)
    node.x = _int_or(el.get("x"), 0)
    node.y = _int_or(el.get("y"), 0)
    node.width = _int_or(el.get("width"), node.width)
    node.height = _int_or(el.get("height"), node.height)
    return node


def _parse_condition(el: ET.Element) -> "UCM.Condition":
    """Build a :class:`UCM.Condition` from a ``<condition>``,
    ``<precondition>``, or ``<postcondition>`` element.

    Accepts both modern files (``label`` + ``expression`` attributes) and
    legacy ones produced by earlier versions of this package (only
    ``expression``, with the user-visible name stored there)."""
    label = el.get("label", "")
    expression = el.get("expression", "")
    # Legacy: if there's only an expression and no label, treat the
    # expression as the label so the import looks the same as the export.
    if not label and expression and expression != "true" and expression != "!true":
        label, expression = expression, "true"
    return UCM.Condition(
        label=label,
        expression=expression or "true",
        description=el.get("description", ""),
        delta_x=_int_or(el.get("deltaX"), 0),
        delta_y=_int_or(el.get("deltaY"), 0),
    )


def _resolve(
    by_id: Dict[int, UCM.URNmodelElement],
    by_xpath: Dict[str, UCM.URNmodelElement],
    ref: str,
) -> Optional[UCM.URNmodelElement]:
    """Resolve a reference attribute, accepting either ``"42"`` (ID) or
    ``"//@urndef/@specDiagrams.0/@nodes.7"`` (XPath)."""
    if not ref:
        return None
    ref = ref.strip()
    if _INT_RE.match(ref):
        return by_id.get(int(ref))
    return by_xpath.get(ref)


def _int_or_none(val: Optional[str]) -> Optional[int]:
    if val is None or val == "":
        return None
    try:
        return int(val)
    except ValueError:
        return None


def _int_or(val: Optional[str], default: int) -> int:
    v = _int_or_none(val)
    return default if v is None else v


_NAME_LINEBREAK_RE = re.compile(r"\r\n|\r|\n")


def _unwrap_name(name: str) -> str:
    """Canonicalise a name read from a ``.jucm`` file.

    Line breaks inside a name are a *visual* formatting decision (the
    jUCMNav editor and our exporter break long names across two or three
    lines so that responsibility rectangles stay narrow). The logical
    name has no line breaks — so we collapse any ``\\r\\n`` / ``\\r`` /
    ``\\n`` sequence back to a single space here, restoring the canonical
    form. Re-exporting the model is free to re-wrap as it sees fit."""
    if not name or ("\n" not in name and "\r" not in name):
        return name
    return _NAME_LINEBREAK_RE.sub(" ", name).strip()
