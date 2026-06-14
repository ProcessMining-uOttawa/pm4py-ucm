"""
Object model for the Use Case Map (UCM) modelling notation.

This module is the structural counterpart of ``pm4py.objects.bpmn.obj``: a
single ``UCM`` class with nested classes for every concept of the notation,
plus a containing graph object that aggregates them.

A few core distinctions of the URN metamodel are surfaced explicitly in the
API and should be kept in mind:

**Definition vs reference.** UCM separates the *definition* of a reusable
domain concept from each *graphical occurrence* of it on a map.

* :class:`UCM.Responsibility` (a "*responsibility definition*") names a
  reusable activity. Each visual occurrence on a map is a separate
  :class:`UCM.RespRef` (a path node — the cross "✕" symbol). Many RespRefs
  may share the same Responsibility.
* :class:`UCM.ComponentElement` (a "*component definition*") names an
  actor, team, system, agent, …. Each visual occurrence on a map is a
  separate :class:`UCM.ComponentRef` (a labelled rectangle) with its own
  geometry. Many ComponentRefs may share the same ComponentElement.

Path nodes (``StartPoint``, ``RespRef``, ``OrFork``, …) may carry an
optional :attr:`UCM.PathNode.cont_ref` link declaring *which* component
reference visually contains them; ``None`` means the node sits outside any
component rectangle.

The class names follow the EMF metamodel so that an EMF/jUCMNav reader can
navigate this file by analogy.
"""
from __future__ import annotations

import itertools
from enum import Enum
from typing import List, Optional


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------
# jUCMNav .jucm files use small, monotonically increasing integer IDs.
# We mirror that scheme so that exported files look natural in jUCMNav and
# do not gratuitously diff against ones produced by the editor itself.

class _IdCounter:
    """Monotonic integer ID generator."""

    __slots__ = ("_value",)

    def __init__(self, start: int = 0) -> None:
        self._value = start

    def next(self) -> int:
        v = self._value
        self._value += 1
        return v

    def reserve(self, value: int) -> None:
        """Bump the counter past ``value`` so subsequent IDs do not collide."""
        if value >= self._value:
            self._value = value + 1

    def peek(self) -> int:
        return self._value


# Module-global counter used as a fallback when no UCM container is yet
# attached. Each ``UCM`` instance owns its own counter.
_GLOBAL_COUNTER = _IdCounter()


class UCM:
    """A Use Case Map (URN spec restricted to UCM).

    The ``UCM`` class is the public surface of this package. It contains:

    * a list of *maps* (``UCM.UCMmap``) — each map being a UCM diagram with
      its path nodes and node connections;
    * a registry of *responsibility definitions* and *component
      definitions* that path nodes / component references on the maps point
      to;
    * the URN-level metadata (name, description, author, modification
      timestamp) that jUCMNav stores at the URNspec level.

    For convenience and to keep parity with PM4Py's BPMN object, the most
    common operations — ``add_node``, ``add_connection`` — operate on the
    "default" map (lazily created on first use).
    """

    #: URN metamodel version we serialise to. ``1.27`` is the format used by
    #: jUCMNav as of mid-2020 onward; older readers also accept it.
    URN_VERSION = "1.27"

    #: jUCMNav spec version attribute on the URNspec root.
    SPEC_VERSION = "4"

    # =========================================================================
    # Inner classes
    # =========================================================================

    class URNmodelElement:
        """Abstract base of every UCM model element.

        In the EMF metamodel this is the root class of all elements. It carries
        an integer ``id`` and a ``name``, optionally a ``description`` and a
        list of ``Metadata`` annotations.
        """

        def __init__(
            self,
            id: Optional[int] = None,
            name: str = "",
            description: str = "",
        ) -> None:
            self._id: Optional[int] = id
            self._name: str = name
            self._description: str = description
            self._metadata: List["UCM.Metadata"] = []
            self._owner: Optional["UCM"] = None

        # --- ID handling -----------------------------------------------------
        @property
        def id(self) -> int:
            if self._id is None:
                # Lazily allocate; prefer the owning container's counter.
                counter = self._owner._counter if self._owner else _GLOBAL_COUNTER
                self._id = counter.next()
            return self._id

        def set_id(self, value: int) -> None:
            self._id = value
            if self._owner is not None:
                self._owner._counter.reserve(value)

        # --- Name/description -----------------------------------------------
        @property
        def name(self) -> str:
            return self._name

        def set_name(self, value: str) -> None:
            self._name = value

        @property
        def description(self) -> str:
            return self._description

        def set_description(self, value: str) -> None:
            self._description = value

        # --- Metadata -------------------------------------------------------
        @property
        def metadata(self) -> List["UCM.Metadata"]:
            return self._metadata

        def add_metadata(self, name: str, value: str) -> None:
            self._metadata.append(UCM.Metadata(name=name, value=value))

        # --- Equality / hashing --------------------------------------------
        def __hash__(self) -> int:
            return id(self)  # identity hashing — matches BPMN.BPMNNode

        def __eq__(self, other) -> bool:  # type: ignore[override]
            return self is other

        def default_name(self) -> str:
            """Auto-generated name in jUCMNav's style: ``{TypeName}{id}``.

            jUCMNav assigns names like ``OrFork49`` or ``RespRef36`` to
            newly-created elements based on the element's type and its
            (global) ID. We do the same when no explicit name has been set
            so that exported files look natural in the editor.
            """
            return f"{self.__class__.__name__}{self.id}"

        @property
        def effective_name(self) -> str:
            """``self.name`` if set, otherwise the auto-generated default."""
            return self._name or self.default_name()

        def __repr__(self) -> str:
            cls = self.__class__.__name__
            return f"{cls}(id={self._id!r}, name={self._name!r})"

    class Metadata:
        """A free-form ``(name, value)`` annotation attached to any element."""

        def __init__(self, name: str = "", value: str = "") -> None:
            self.name = name
            self.value = value

    # ------------------------------------------------------------------
    # Path-node hierarchy (`ucm.map` package)
    # ------------------------------------------------------------------

    class PathNode(URNmodelElement):
        """Abstract path node. Every concrete UCM symbol on a map subclasses
        this. It owns a list of incoming and outgoing ``NodeConnection``
        objects (predecessors / successors)."""

        # Default visual extent — irrelevant for semantics but useful for
        # exporters/visualizers.
        DEFAULT_WIDTH = 18
        DEFAULT_HEIGHT = 18

        def __init__(
            self,
            id: Optional[int] = None,
            name: str = "",
            description: str = "",
        ) -> None:
            super().__init__(id=id, name=name, description=description)
            self._pred: List[UCM.NodeConnection] = []
            self._succ: List[UCM.NodeConnection] = []
            # Layout coordinates (for export and visualization)
            self.x: int = 0
            self.y: int = 0
            self.width: int = self.DEFAULT_WIDTH
            self.height: int = self.DEFAULT_HEIGHT
            # Owning map (set when the node is added to a UCMmap)
            self._map: Optional[UCM.UCMmap] = None
            #: Optional component reference (``ucm.map.PathNode.contRef`` in
            #: the metamodel). When set, declares that this node is drawn
            #: inside the given component ref's rectangle on the diagram.
            self.cont_ref: Optional[UCM.ComponentRef] = None

        @property
        def pred_connections(self) -> List["UCM.NodeConnection"]:
            return self._pred

        @property
        def succ_connections(self) -> List["UCM.NodeConnection"]:
            return self._succ

        @property
        def diagram(self) -> Optional["UCM.UCMmap"]:
            return self._map

    class Condition:
        """A logical guard attached to a connection, a start point, or an
        end point. Mirrors the ``urncore::Condition`` metaclass.

        Has three independent string fields:

        * :attr:`label` — short human-readable name displayed on the
          diagram (e.g. ``"TrueBranch"``);
        * :attr:`expression` — the actual logical expression evaluated at
          runtime (e.g. ``"true"``, ``"x > 0"``). Defaults to ``"true"`` if
          not specified;
        * :attr:`description` — optional longer description.

        Constructed conveniently from a bare string, in which case the
        string is taken as the :attr:`label` and the expression defaults to
        ``"true"``.
        """

        __slots__ = ("label", "expression", "description",
                     "delta_x", "delta_y")

        def __init__(
            self,
            label: str = "",
            expression: str = "true",
            description: str = "",
            delta_x: int = 0,
            delta_y: int = 0,
        ) -> None:
            self.label = label
            self.expression = expression
            self.description = description
            self.delta_x = delta_x
            self.delta_y = delta_y

        @classmethod
        def coerce(cls, value) -> Optional["UCM.Condition"]:
            """Accept ``None``, a string, a dict, or a Condition; return a
            Condition or ``None``. Strings become the *label* with the
            expression defaulting to ``"true"`` — matching the way labels
            like ``[TrueBranch]`` are written in jUCMNav."""
            if value is None or isinstance(value, cls):
                return value
            if isinstance(value, str):
                return cls(label=value)
            if isinstance(value, dict):
                return cls(**value)
            raise TypeError(
                f"Cannot coerce {type(value).__name__} into a Condition")

        def __str__(self) -> str:
            return self.label or self.expression

        def __repr__(self) -> str:
            return (f"Condition(label={self.label!r}, "
                    f"expression={self.expression!r})")

    class StartPoint(PathNode):
        """Filled circle. Entry of a UCM path. At most one outgoing connection."""

        def __init__(self, id=None, name: str = "", description: str = "",
                     pre_condition=None) -> None:
            super().__init__(id=id, name=name, description=description)
            # urncore::Condition (0..1) in the metamodel.
            self.pre_condition: Optional[UCM.Condition] = (
                UCM.Condition.coerce(pre_condition)
            )

    class EndPoint(PathNode):
        """Bar. Exit of a UCM path. At most one incoming connection."""

        def __init__(self, id=None, name: str = "", description: str = "",
                     post_condition=None) -> None:
            super().__init__(id=id, name=name, description=description)
            self.post_condition: Optional[UCM.Condition] = (
                UCM.Condition.coerce(post_condition)
            )

    class EmptyPoint(PathNode):
        """Pass-through point. Exactly one in / one out. The UCM analogue of
        a BPMN tau transition."""

    class RespRef(PathNode):
        """Cross. Reference to a Responsibility — the named "activity" of a UCM."""

        def __init__(
            self,
            id=None,
            name: str = "",
            description: str = "",
            resp_def: Optional["UCM.Responsibility"] = None,
        ) -> None:
            super().__init__(id=id, name=name, description=description)
            self._resp_def: Optional[UCM.Responsibility] = resp_def

        @property
        def resp_def(self) -> Optional["UCM.Responsibility"]:
            return self._resp_def

        def set_resp_def(self, resp: "UCM.Responsibility") -> None:
            self._resp_def = resp

    class OrFork(PathNode):
        """1 in, n out. Choice — the UCM equivalent of BPMN exclusive gateway."""

    class OrJoin(PathNode):
        """n in, 1 out. Convergence after a choice."""

    class AndFork(PathNode):
        """1 in, n out. Concurrency split — BPMN parallel gateway analogue."""

    class AndJoin(PathNode):
        """n in, 1 out. Concurrency join."""

    class WaitingPlace(PathNode):
        """Waiting place — synchronizes a path until a condition holds."""

    class Timer(WaitingPlace):
        """Hourglass. Waiting place with a timeout branch."""

    class Stub(PathNode):
        """Diamond. Sub-map call site (static or dynamic)."""

        def __init__(self, id=None, name: str = "", description: str = "",
                     dynamic: bool = False) -> None:
            super().__init__(id=id, name=name, description=description)
            self.dynamic: bool = dynamic
            self.bindings: List[UCM.PluginBinding] = []

    class Connect(PathNode):
        """Path-segment join via asynchronous interaction."""

    class DirectionArrow(PathNode):
        """Mid-path arrow used purely for layout disambiguation."""

    class FailurePoint(PathNode):
        """Pentagon. Path failure point."""

    class Anything(PathNode):
        """Aspect-oriented "Anything" wildcard PathNode."""

    # ------------------------------------------------------------------
    # NodeConnection
    # ------------------------------------------------------------------

    class NodeConnection(URNmodelElement):
        """A directed connection between two PathNodes — analogue of
        ``ucm.map.NodeConnection``.

        NodeConnections in jUCMNav are **anonymous** — they have no global
        ID and are referenced from other elements positionally
        (``//@urndef/@specDiagrams.0/@connections.7``). The model still
        gives each connection a Python-level ``id`` so that local registries
        can key off it, but the exporter never serialises it.
        """

        def __init__(
            self,
            source: "UCM.PathNode",
            target: "UCM.PathNode",
            id: Optional[int] = None,
            name: str = "",
            condition=None,
        ) -> None:
            super().__init__(id=id, name=name)
            self._source = source
            self._target = target
            #: :class:`UCM.Condition` or ``None``. A bare string passed at
            #: construction time becomes the condition's ``label``.
            self._condition: Optional[UCM.Condition] = UCM.Condition.coerce(condition)
            # Wire up adjacency
            source._succ.append(self)
            target._pred.append(self)
            # Threshold attribute used by aspect-oriented UCMs
            self.threshold: float = 0.0

        @property
        def source(self) -> "UCM.PathNode":
            return self._source

        @property
        def target(self) -> "UCM.PathNode":
            return self._target

        @property
        def condition(self) -> Optional["UCM.Condition"]:
            return self._condition

        def set_condition(self, value) -> None:
            self._condition = UCM.Condition.coerce(value)

        def __repr__(self) -> str:
            s_name = self._source.effective_name
            t_name = self._target.effective_name
            return f"NodeConnection({s_name} -> {t_name})"

    # ------------------------------------------------------------------
    # Responsibility / Component definitions and references
    # ------------------------------------------------------------------

    class Responsibility(URNmodelElement):
        """A reusable, named **responsibility definition** (an activity).

        The definition lives at the URN level — directly on
        :attr:`UCM.responsibilities`. Each visual occurrence on a map is a
        separate :class:`UCM.RespRef` path node that points back here via
        :attr:`UCM.RespRef.resp_def`. Many RespRefs may share the same
        Responsibility (the same activity drawn at multiple places on the
        same or different maps).

        :attr:`performer` is an optional link to the
        :class:`UCM.ComponentElement` that *performs* this responsibility.
        It is a **semantic** binding: it says "this activity is done by
        that actor/team", independent of how the diagram is laid out. The
        converter and the layouter use the performer link to decide which
        :class:`UCM.ComponentRef` rectangle each :class:`UCM.RespRef`
        symbol should be drawn inside (via :attr:`UCM.PathNode.cont_ref`).

        Use :meth:`UCM.get_or_add_responsibility` to look one up by name
        (creating it if absent) rather than constructing this directly."""

        def __init__(self, id=None, name: str = "", description: str = "",
                     expression: str = "",
                     performer: Optional["UCM.ComponentElement"] = None) -> None:
            super().__init__(id=id, name=name, description=description)
            self.expression = expression  # behaviour expression (optional)
            self.performer: Optional[UCM.ComponentElement] = performer

        def set_performer(
            self, performer: Optional["UCM.ComponentElement"]
        ) -> None:
            self.performer = performer

    class ComponentElement(URNmodelElement):
        """A reusable, named **component definition** — an actor, team,
        agent, role, sub-system, or other entity the path crosses.

        The definition lives at the URN level — directly on
        :attr:`UCM.components`. Each visual occurrence on a map is a
        separate :class:`UCM.ComponentRef` (a rectangle) that points back
        here via :attr:`UCM.ComponentRef.cont_def`. Many ComponentRefs may
        share the same ComponentElement.

        :attr:`kind` distinguishes Team/Object/Process/Agent/Actor/ISR/Other
        and is rendered differently by jUCMNav (e.g. an Actor is drawn with
        a stick figure)."""

        class Kind(Enum):
            #: Default — plain rectangle.
            TEAM = "Team"
            OBJECT = "Object"
            PROCESS = "Process"
            AGENT = "Agent"
            #: Stick-figure decoration in jUCMNav.
            ACTOR = "Actor"
            #: Information Service Role.
            ISR = "ISR"
            OTHER = "Other"

        def __init__(self, id=None, name: str = "", description: str = "",
                     kind=None) -> None:
            super().__init__(id=id, name=name, description=description)
            self.kind = (
                kind if kind is not None else UCM.ComponentElement.Kind.TEAM
            )

    class ComponentRef(URNmodelElement):
        """A **component reference** — the visual occurrence of a
        :class:`UCM.ComponentElement` on a UCMmap.

        The geometric fields (:attr:`x`, :attr:`y`, :attr:`width`,
        :attr:`height`) are specific to *this occurrence*; they describe the
        rectangle drawn on the diagram. The :attr:`cont_def` link points to
        the (shared) component definition. Several ComponentRefs may refer
        to the same ComponentElement — e.g. the same actor drawn on
        multiple maps, or twice on the same map.

        ComponentRefs nest via :attr:`parent` / :attr:`children` to express
        sub-components (e.g. the *Sub-Component* drawn inside *Component* in
        a typical jUCMNav file)."""

        def __init__(
            self,
            cont_def: "UCM.ComponentElement",
            id: Optional[int] = None,
            x: int = 0, y: int = 0, width: int = 200, height: int = 100,
            parent: Optional["UCM.ComponentRef"] = None,
        ) -> None:
            super().__init__(id=id, name=cont_def.name)
            self.cont_def = cont_def
            self.x, self.y = x, y
            self.width, self.height = width, height
            self.children: List["UCM.ComponentRef"] = []
            self.parent: Optional["UCM.ComponentRef"] = None
            if parent is not None:
                self.set_parent(parent)

        def set_parent(self, parent: Optional["UCM.ComponentRef"]) -> None:
            """Re-parent this reference, maintaining both sides of the link."""
            if self.parent is parent:
                return
            if self.parent is not None and self in self.parent.children:
                self.parent.children.remove(self)
            self.parent = parent
            if parent is not None and self not in parent.children:
                parent.children.append(self)

    # ------------------------------------------------------------------
    # PluginBinding (sub-map binding for stubs)
    # ------------------------------------------------------------------

    class PluginBinding:
        """Binding of a plug-in :class:`UCM.UCMmap` to a :class:`UCM.Stub`.

        A stub is a placeholder for a sub-map. The binding tells jUCMNav
        which sub-map fills the placeholder and how the parent map's
        in/out arcs connect to the plug-in's start and end points.

        Structure (mirrors the jUCMNav XMI shape):

        * :attr:`plugin` — the :class:`UCM.UCMmap` that fills the stub.
        * :attr:`in_bindings` — list of :class:`UCM.InBinding`,
          one per arc *entering* the stub on the parent map. Each entry
          pairs that parent-side ``NodeConnection`` with a
          :class:`UCM.StartPoint` on the plug-in map.
        * :attr:`out_bindings` — list of :class:`UCM.OutBinding`,
          one per arc *leaving* the stub on the parent map. Each entry
          pairs a :class:`UCM.EndPoint` on the plug-in map with that
          parent-side ``NodeConnection``.
        * :attr:`precondition` — optional ``UCM.Condition`` evaluated
          when choosing this plug-in for a dynamic stub. ``None`` for
          static stubs."""

        def __init__(
            self,
            stub: "UCM.Stub",
            plugin: "UCM.UCMmap",
            id: Optional[int] = None,
        ) -> None:
            self.id = id
            self.stub = stub
            self.plugin = plugin
            self.in_bindings: List["UCM.InBinding"] = []
            self.out_bindings: List["UCM.OutBinding"] = []
            self.precondition: Optional["UCM.Condition"] = None

        # Convenience builders -----------------------------------------
        def add_in(
            self,
            parent_connection: "UCM.NodeConnection",
            plugin_start: "UCM.StartPoint",
        ) -> "UCM.InBinding":
            """Bind a parent-map arc entering the stub to a plug-in StartPoint."""
            ib = UCM.InBinding(stub_entry=parent_connection,
                                start_point=plugin_start)
            self.in_bindings.append(ib)
            return ib

        def add_out(
            self,
            plugin_end: "UCM.EndPoint",
            parent_connection: "UCM.NodeConnection",
        ) -> "UCM.OutBinding":
            """Bind a plug-in EndPoint to a parent-map arc leaving the stub."""
            ob = UCM.OutBinding(end_point=plugin_end,
                                 stub_exit=parent_connection)
            self.out_bindings.append(ob)
            return ob

    class InBinding:
        """One entry in :attr:`UCM.PluginBinding.in_bindings`.

        Pairs an arc entering the stub on the *parent* map
        (:attr:`stub_entry`, a :class:`UCM.NodeConnection`) with the
        :class:`UCM.StartPoint` on the plug-in map that the path
        continues at (:attr:`start_point`)."""

        def __init__(
            self,
            stub_entry: "UCM.NodeConnection",
            start_point: "UCM.StartPoint",
        ) -> None:
            self.stub_entry = stub_entry
            self.start_point = start_point

    class OutBinding:
        """One entry in :attr:`UCM.PluginBinding.out_bindings`.

        Pairs a :class:`UCM.EndPoint` on the plug-in map
        (:attr:`end_point`) with the arc leaving the stub on the
        *parent* map (:attr:`stub_exit`, a :class:`UCM.NodeConnection`)
        that the path resumes on."""

        def __init__(
            self,
            end_point: "UCM.EndPoint",
            stub_exit: "UCM.NodeConnection",
        ) -> None:
            self.end_point = end_point
            self.stub_exit = stub_exit

    # ------------------------------------------------------------------
    # UCMmap (a single diagram)
    # ------------------------------------------------------------------

    class UCMmap(URNmodelElement):
        """A single Use Case Map diagram.

        Holds an ordered list of nodes, an ordered list of connections, and
        an ordered list of component references. Order matters because it
        determines the indices used in legacy XPath cross-references in
        jUCMNav .jucm files (``//@urndef/@specDiagrams.0/@nodes.7``).

        Membership in each list is also tracked via a parallel ``set`` so
        ``add_node`` / ``add_connection`` / ``remove_node`` /
        ``remove_connection`` run in O(1) rather than O(N). This matters
        for large discovered maps — software event logs in particular can
        easily yield thousands of nodes."""

        def __init__(self, id=None, name: str = "Map", description: str = "") -> None:
            super().__init__(id=id, name=name, description=description)
            self.nodes: List[UCM.PathNode] = []
            self.connections: List[UCM.NodeConnection] = []
            self.cont_refs: List[UCM.ComponentRef] = []
            # O(1) membership sets, kept in sync with the lists above.
            # URN model elements use identity-based __hash__ / __eq__, so
            # these sets behave like ``set[id(x)]`` without the indirection.
            self._node_set: set = set()
            self._connection_set: set = set()
            self._cont_ref_set: set = set()

        # --- Mutation -------------------------------------------------------
        def add_node(self, node: "UCM.PathNode") -> "UCM.PathNode":
            if node._map is not None and node._map is not self:
                raise ValueError(f"Node {node!r} already belongs to another UCMmap")
            node._map = self
            # Bind to the same URN container as the map so IDs flow from
            # one shared counter.
            if self._owner is not None:
                node._owner = self._owner
                if node._id is not None:
                    self._owner._counter.reserve(node._id)
            if node not in self._node_set:
                self._node_set.add(node)
                self.nodes.append(node)
            return node

        def add_connection(self, connection_or_source, target=None, condition=None):
            """Add a NodeConnection. Accepts either a pre-built ``NodeConnection``
            instance, or a ``(source, target[, condition])`` triple as a shortcut.
            """
            if target is not None:
                connection = UCM.NodeConnection(
                    source=connection_or_source,
                    target=target,
                    condition=condition,
                )
            else:
                connection = connection_or_source
            if self._owner is not None:
                connection._owner = self._owner
            self.add_node(connection.source)
            self.add_node(connection.target)
            if connection not in self._connection_set:
                self._connection_set.add(connection)
                self.connections.append(connection)
            return connection

        def remove_connection(self, connection: "UCM.NodeConnection") -> None:
            if connection in self._connection_set:
                self._connection_set.discard(connection)
                self.connections.remove(connection)
            try:
                connection.source._succ.remove(connection)
            except ValueError:
                pass
            try:
                connection.target._pred.remove(connection)
            except ValueError:
                pass

        def remove_node(self, node: "UCM.PathNode") -> None:
            for c in list(node._pred) + list(node._succ):
                self.remove_connection(c)
            if node in self._node_set:
                self._node_set.discard(node)
                self.nodes.remove(node)
            node._map = None

        def add_cont_ref(self, ref: "UCM.ComponentRef") -> "UCM.ComponentRef":
            if self._owner is not None:
                ref._owner = self._owner
                if ref._id is not None:
                    self._owner._counter.reserve(ref._id)
            if ref not in self._cont_ref_set:
                self._cont_ref_set.add(ref)
                self.cont_refs.append(ref)
            return ref

        def add_component_ref(
            self,
            cont_def: "UCM.ComponentElement",
            x: int = 0, y: int = 0,
            width: int = 200, height: int = 100,
            parent: Optional["UCM.ComponentRef"] = None,
        ) -> "UCM.ComponentRef":
            """Create and register a new :class:`UCM.ComponentRef` for the
            given component definition. Convenience over manually
            constructing a ComponentRef and calling :meth:`add_cont_ref`.
            """
            ref = UCM.ComponentRef(
                cont_def=cont_def,
                x=x, y=y, width=width, height=height,
                parent=parent,
            )
            return self.add_cont_ref(ref)

        def nodes_in(self, comp_ref: "UCM.ComponentRef") -> List["UCM.PathNode"]:
            """Return all path nodes whose ``cont_ref`` link points at the
            given component reference."""
            return [n for n in self.nodes if n.cont_ref is comp_ref]

        # --- Convenience iterators -----------------------------------------
        @property
        def start_points(self) -> List["UCM.StartPoint"]:
            return [n for n in self.nodes if isinstance(n, UCM.StartPoint)]

        @property
        def end_points(self) -> List["UCM.EndPoint"]:
            return [n for n in self.nodes if isinstance(n, UCM.EndPoint)]

        @property
        def resp_refs(self) -> List["UCM.RespRef"]:
            return [n for n in self.nodes if isinstance(n, UCM.RespRef)]

        # --- Degree helpers -------------------------------------------------
        def in_degree(self, node: "UCM.PathNode") -> int:
            return len(node._pred)

        def out_degree(self, node: "UCM.PathNode") -> int:
            return len(node._succ)

        def __repr__(self) -> str:
            return (f"UCMmap(name={self._name!r}, nodes={len(self.nodes)}, "
                    f"connections={len(self.connections)})")

    # ------------------------------------------------------------------
    # Scenario metamodel (urncore::EnumerationType / Variable /
    # ucm::ScenarioGroup / ScenarioDef / Initialization / ScenarioStartPoint
    # / ScenarioEndPoint). Live on the URN spec root (this UCM container)
    # except for ScenarioDef and Initialization/StartPoint/EndPoint, which
    # live inside their owning ScenarioGroup/ScenarioDef.
    # ------------------------------------------------------------------

    class EnumerationType(URNmodelElement):
        """A named set of string values. Mirrors ``urncore::EnumerationType``.

        jUCMNav serialises ``values`` as a single comma-separated string
        attribute. Each value is a free-form identifier; semantics are
        carried by :class:`UCM.Variable` references."""

        def __init__(
            self,
            id: Optional[int] = None,
            name: str = "",
            description: str = "",
            values: Optional[List[str]] = None,
        ) -> None:
            super().__init__(id=id, name=name, description=description)
            self.values: List[str] = list(values or [])

    class Variable(URNmodelElement):
        """A named, typed scenario variable. Mirrors ``urncore::Variable``.

        :attr:`type` is one of ``"Enumeration"``, ``"Integer"``,
        ``"Boolean"``, or any other identifier accepted by jUCMNav. When
        ``type == "Enumeration"``, :attr:`enumeration_type` must point at
        a :class:`UCM.EnumerationType` defined on the same URN spec."""

        def __init__(
            self,
            id: Optional[int] = None,
            name: str = "",
            description: str = "",
            type: str = "Enumeration",
            enumeration_type: Optional["UCM.EnumerationType"] = None,
        ) -> None:
            super().__init__(id=id, name=name, description=description)
            self.type: str = type
            self.enumeration_type: Optional[UCM.EnumerationType] = enumeration_type

    class ScenarioGroup(URNmodelElement):
        """Container for a set of related scenarios. Lives on the UCM spec.

        URN allows multiple groups; this implementation accommodates that
        but the scenario synthesis driver produces a single group per
        discovery run by default."""

        def __init__(
            self,
            id: Optional[int] = None,
            name: str = "ScenarioGroup",
            description: str = "",
        ) -> None:
            super().__init__(id=id, name=name, description=description)
            self.scenarios: List[UCM.ScenarioDef] = []

        def add_scenario(self, scenario: "UCM.ScenarioDef") -> "UCM.ScenarioDef":
            scenario._owner = self._owner
            if self._owner is not None and scenario._id is not None:
                self._owner._counter.reserve(scenario._id)
            self.scenarios.append(scenario)
            return scenario

    class Initialization:
        """One ``(variable, value)`` pair set at scenario entry.

        Mirrors ``ucm::Initialization``. The value is a string; for
        enumeration-typed variables it must be one of the enumeration's
        values."""

        __slots__ = ("variable", "value")

        def __init__(
            self,
            variable: "UCM.Variable",
            value: str,
        ) -> None:
            self.variable: UCM.Variable = variable
            self.value: str = value

    class ScenarioStartPoint:
        """A scenario's entry point — references a :class:`UCM.StartPoint`
        on a map. ``enabled=True`` (the default) means the scenario engine
        activates this start point; ``False`` is rarely used and provided
        for round-trip fidelity with jUCMNav."""

        __slots__ = ("start_point", "enabled")

        def __init__(
            self,
            start_point: "UCM.StartPoint",
            enabled: bool = True,
        ) -> None:
            self.start_point: UCM.StartPoint = start_point
            self.enabled: bool = enabled

    class ScenarioEndPoint:
        """A scenario's expected exit — references a :class:`UCM.EndPoint`
        on a map. Same enabled semantics as :class:`UCM.ScenarioStartPoint`."""

        __slots__ = ("end_point", "enabled")

        def __init__(
            self,
            end_point: "UCM.EndPoint",
            enabled: bool = True,
        ) -> None:
            self.end_point: UCM.EndPoint = end_point
            self.enabled: bool = enabled

    class ScenarioDef(URNmodelElement):
        """One executable scenario specification. Mirrors ``ucm::ScenarioDef``.

        A scenario is a triple:

        * ``initializations`` — variable values to set at scenario entry;
        * ``start_points`` / ``end_points`` — which path entries/exits are
          activated (multiple supported, but the typical case is one each);
        * ``description`` — free-form text. The scenario synthesis driver
          uses this slot to carry the variant's partial-order expression
          and the list of case IDs that the variant covers, so anyone
          opening the model in jUCMNav can trace each scenario back to
          the original log."""

        def __init__(
            self,
            id: Optional[int] = None,
            name: str = "",
            description: str = "",
        ) -> None:
            super().__init__(id=id, name=name, description=description)
            self.initializations: List[UCM.Initialization] = []
            self.start_points: List[UCM.ScenarioStartPoint] = []
            self.end_points: List[UCM.ScenarioEndPoint] = []

        def add_initialization(
            self, variable: "UCM.Variable", value: str,
        ) -> "UCM.Initialization":
            init = UCM.Initialization(variable=variable, value=value)
            self.initializations.append(init)
            return init

        def add_start_point(
            self, start_point: "UCM.StartPoint", enabled: bool = True,
        ) -> "UCM.ScenarioStartPoint":
            sp = UCM.ScenarioStartPoint(start_point=start_point, enabled=enabled)
            self.start_points.append(sp)
            return sp

        def add_end_point(
            self, end_point: "UCM.EndPoint", enabled: bool = True,
        ) -> "UCM.ScenarioEndPoint":
            ep = UCM.ScenarioEndPoint(end_point=end_point, enabled=enabled)
            self.end_points.append(ep)
            return ep

    # =========================================================================
    # End of inner classes. The UCM container itself follows.
    # =========================================================================

    def __init__(
        self,
        name: str = "URNspec",
        description: str = "",
        author: str = "pm4py-ucm",
    ) -> None:
        self.name = name
        self.description = description
        self.author = author
        self.created: str = ""   # filled at export time if blank
        self.modified: str = ""  # filled at export time

        # ID generator shared by every element added to this container.
        # IDs start at 1; jUCMNav avoids 0.
        self._counter: _IdCounter = _IdCounter(start=1)

        # URNdefinition contents
        self.maps: List[UCM.UCMmap] = []
        self.responsibilities: List[UCM.Responsibility] = []
        self.components: List[UCM.ComponentElement] = []

        # Scenario metamodel: enumerations, variables, scenario groups.
        # Empty by default — :func:`pm4py_ucm.discover_scenarios` populates
        # them; the exporter only emits ``<ucmspec>`` content when any of
        # these lists is non-empty.
        self.enumeration_types: List[UCM.EnumerationType] = []
        self.variables: List[UCM.Variable] = []
        self.scenario_groups: List[UCM.ScenarioGroup] = []

    # ------------------------------------------------------------------
    # ID introspection
    # ------------------------------------------------------------------

    def all_elements(self) -> List["UCM.URNmodelElement"]:
        """Every URN model element this container owns, in a stable order.

        Used by the exporter to compute ``nextGlobalID`` and to walk the
        model uniformly. NodeConnections are *not* included because
        jUCMNav does not assign them global IDs.
        """
        out: List[UCM.URNmodelElement] = []
        for m in self.maps:
            out.append(m)
            for n in m.nodes:
                out.append(n)
            for cr in m.cont_refs:
                out.append(cr)
        for r in self.responsibilities:
            out.append(r)
        for c in self.components:
            out.append(c)
        # Scenario-layer definitions also carry global IDs in jUCMNav.
        for et in self.enumeration_types:
            out.append(et)
        for v in self.variables:
            out.append(v)
        for sg in self.scenario_groups:
            out.append(sg)
            for sc in sg.scenarios:
                out.append(sc)
        return out

    def max_id(self) -> int:
        """Largest ID currently allocated to a URN model element. Returns
        ``0`` if nothing has been allocated yet."""
        ids = [e._id for e in self.all_elements() if e._id is not None]
        return max(ids) if ids else 0

    def next_global_id(self) -> int:
        """Value of jUCMNav's ``nextGlobalID`` attribute as of *now* — the
        next integer ID the editor would assign to a newly-created element.
        Always ``max(all IDs) + 1`` (or the counter's current value if no
        IDs have been allocated yet)."""
        return max(self.max_id() + 1, self._counter.peek())

    def assign_ids(self) -> None:
        """Force lazy ID allocation on every element. Useful before
        serialisation so that ``element._id`` is no longer ``None``."""
        for e in self.all_elements():
            _ = e.id  # touch the property to allocate

    def find_resp_refs(self, resp: "UCM.Responsibility") -> List["UCM.RespRef"]:
        """All ``RespRef`` nodes (across all maps) that point at ``resp``."""
        return [
            n for m in self.maps for n in m.nodes
            if isinstance(n, UCM.RespRef) and n.resp_def is resp
        ]

    def find_cont_refs(
        self, comp: "UCM.ComponentElement"
    ) -> List["UCM.ComponentRef"]:
        """All ``ComponentRef`` occurrences (across all maps) that point at
        ``comp``."""
        return [
            cr for m in self.maps for cr in m.cont_refs
            if cr.cont_def is comp
        ]

    # ------------------------------------------------------------------
    # Map management
    # ------------------------------------------------------------------

    def add_map(self, ucm_map: Optional["UCM.UCMmap"] = None,
                name: Optional[str] = None) -> "UCM.UCMmap":
        if ucm_map is None:
            ucm_map = UCM.UCMmap(name=name or f"Map{len(self.maps)}")
        ucm_map._owner = self
        if ucm_map._id is not None:
            self._counter.reserve(ucm_map._id)
        self.maps.append(ucm_map)
        # Walk every contained element and rebind its owner so IDs flow
        # from this container.
        self._rebind(ucm_map)
        return ucm_map

    def _rebind(self, ucm_map: "UCM.UCMmap") -> None:
        for n in ucm_map.nodes:
            n._owner = self
            if n._id is not None:
                self._counter.reserve(n._id)
        for c in ucm_map.connections:
            c._owner = self
            if c._id is not None:
                self._counter.reserve(c._id)
        for r in ucm_map.cont_refs:
            r._owner = self
            if r._id is not None:
                self._counter.reserve(r._id)
        ucm_map._owner = self
        if ucm_map._id is not None:
            self._counter.reserve(ucm_map._id)

    @property
    def default_map(self) -> "UCM.UCMmap":
        if not self.maps:
            self.add_map(name="Map0")
        return self.maps[0]

    # ------------------------------------------------------------------
    # Responsibility / Component definitions
    # ------------------------------------------------------------------

    def get_or_add_responsibility(self, name: str) -> "UCM.Responsibility":
        for r in self.responsibilities:
            if r.name == name:
                return r
        r = UCM.Responsibility(name=name)
        r._owner = self
        self.responsibilities.append(r)
        return r

    def add_responsibility(self, resp: "UCM.Responsibility") -> "UCM.Responsibility":
        resp._owner = self
        self.responsibilities.append(resp)
        return resp

    def get_or_add_component(
        self,
        name: str,
        kind=None,
    ) -> "UCM.ComponentElement":
        for c in self.components:
            if c.name == name:
                return c
        c = UCM.ComponentElement(name=name, kind=kind)
        c._owner = self
        self.components.append(c)
        return c

    # ------------------------------------------------------------------
    # Scenario metamodel — registries
    # ------------------------------------------------------------------

    def get_or_add_enumeration_type(
        self, name: str, values: Optional[List[str]] = None,
    ) -> "UCM.EnumerationType":
        """Return the named enumeration type, creating one if absent.

        When the enumeration already exists, ``values`` is merged
        (new values appended in order, duplicates skipped)."""
        for et in self.enumeration_types:
            if et.name == name:
                if values:
                    for v in values:
                        if v not in et.values:
                            et.values.append(v)
                return et
        et = UCM.EnumerationType(name=name, values=values or [])
        et._owner = self
        self.enumeration_types.append(et)
        return et

    def get_or_add_variable(
        self,
        name: str,
        type: str = "Enumeration",
        enumeration_type: Optional["UCM.EnumerationType"] = None,
    ) -> "UCM.Variable":
        """Return the named variable, creating one if absent. Does not
        change the type or enumeration of an existing variable."""
        for v in self.variables:
            if v.name == name:
                return v
        v = UCM.Variable(
            name=name, type=type, enumeration_type=enumeration_type,
        )
        v._owner = self
        self.variables.append(v)
        return v

    def add_scenario_group(
        self,
        name: str = "ScenarioGroup",
        description: str = "",
    ) -> "UCM.ScenarioGroup":
        """Create and register a new :class:`UCM.ScenarioGroup`."""
        sg = UCM.ScenarioGroup(name=name, description=description)
        sg._owner = self
        self.scenario_groups.append(sg)
        return sg

    # ------------------------------------------------------------------
    # Performer bindings (semantic <-> visual)
    # ------------------------------------------------------------------

    def bind_performers(
        self,
        performers: dict,
        kind=None,
        kind_for=None,
        additional_components=None,
    ) -> None:
        """Wire a ``{responsibility_name: performer_name}`` mapping into
        the model — the standard way to attach mined resource information
        to a UCM.

        The method works in two layers:

        **Semantic layer.** For every entry of ``performers``, ensures a
        :class:`UCM.ComponentElement` exists with the given performer
        name, then sets :attr:`UCM.Responsibility.performer` on the
        matching responsibility definition.

        **Visual layer.** For every map and every :class:`UCM.RespRef` on
        it, creates (lazily, at most one per component per map) a
        :class:`UCM.ComponentRef` for the responsibility's performer and
        sets :attr:`UCM.PathNode.cont_ref` on the RespRef to that
        rectangle. The layouter will then size the rectangle to enclose
        its bound nodes.

        Parameters
        ----------
        performers
            ``{responsibility_name: performer_name}`` mapping. A value may
            also be a ``(name, kind)`` tuple to set the
            :class:`UCM.ComponentElement.Kind` for that component.
        kind
            Default component kind for entries that don't specify one.
        kind_for
            Optional ``{performer_name: Kind}`` overrides (most specific).
        additional_components
            Optional iterable of component names to define on the URN
            spec **without** binding any responsibility to them. Useful
            when the log contains actors that exist but don't have a
            clean modal binding to any specific activity — the actor
            still belongs in the URN model as a known component, even
            though no :class:`UCM.RespRef` on the current map points at
            it. These get a :class:`UCM.ComponentElement` definition but
            no :class:`UCM.ComponentRef` on any map.
        """
        kind_for = kind_for or {}

        # --- Semantic layer ----------------------------------------
        for resp_name, perf_spec in performers.items():
            if isinstance(perf_spec, tuple):
                perf_name, perf_kind = perf_spec
            else:
                perf_name = perf_spec
                perf_kind = kind_for.get(perf_name, kind)
            if not perf_name:
                continue
            comp = self.get_or_add_component(perf_name, kind=perf_kind)
            # set/refresh the resp's performer
            for r in self.responsibilities:
                if r.name == resp_name:
                    r.set_performer(comp)
                    break

        # --- Visual layer ------------------------------------------
        for m in self.maps:
            comp_to_ref: dict = {}
            # First, register any pre-existing ComponentRefs so we don't
            # add duplicates.
            for cr in m.cont_refs:
                comp_to_ref.setdefault(cr.cont_def, cr)
            # Now bind each RespRef.
            for n in m.nodes:
                if not isinstance(n, UCM.RespRef):
                    continue
                if n.resp_def is None or n.resp_def.performer is None:
                    continue
                comp = n.resp_def.performer
                if comp not in comp_to_ref:
                    comp_to_ref[comp] = m.add_component_ref(comp)
                n.cont_ref = comp_to_ref[comp]

        # --- Additional component definitions (no map references) -
        if additional_components:
            for comp_name in additional_components:
                if not comp_name:
                    continue
                self.get_or_add_component(
                    comp_name, kind=kind_for.get(comp_name, kind))

    # ------------------------------------------------------------------
    # Convenience pass-throughs (operate on the default map)
    # ------------------------------------------------------------------

    def add_node(self, node: "UCM.PathNode") -> "UCM.PathNode":
        node._owner = self
        return self.default_map.add_node(node)

    def add_connection(
        self,
        source: "UCM.PathNode",
        target: "UCM.PathNode",
        condition: Optional[str] = None,
    ) -> "UCM.NodeConnection":
        c = UCM.NodeConnection(source=source, target=target, condition=condition)
        c._owner = self
        return self.default_map.add_connection(c)

    # ------------------------------------------------------------------
    # Read-only convenience views
    # ------------------------------------------------------------------

    def get_path_nodes(self) -> List["UCM.PathNode"]:
        return list(itertools.chain.from_iterable(m.nodes for m in self.maps))

    def get_node_connections(self) -> List["UCM.NodeConnection"]:
        return list(itertools.chain.from_iterable(m.connections for m in self.maps))

    def __repr__(self) -> str:
        return (f"UCM(name={self.name!r}, maps={len(self.maps)}, "
                f"responsibilities={len(self.responsibilities)}, "
                f"components={len(self.components)})")
