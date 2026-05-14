"""Default visualisation parameters for Use Case Maps.

Kept in a separate module so that variants can share constants without
each variant pulling in the parameters of the others.
"""

DEFAULT_FONT = "Helvetica"
DEFAULT_FONT_SIZE = 11
DEFAULT_RANKDIR = "LR"  # left-to-right matches the natural reading direction
#: Background colour. Plain white by default — transparency was confusing
#: when multi-map PNGs are composited (Pillow's RGB conversion turns
#: transparent pixels black, producing black map backgrounds against a
#: white separator strip) and seldom useful in practice. Pass
#: ``bgcolor="transparent"`` explicitly to opt back in.
DEFAULT_BG = "white"

#: Whether the visualizer renders ``[condition]`` labels on edges by
#: default. Discovered UCMs carry synthetic conditions like
#: ``branch0`` / ``redo`` / ``exit`` from the process-tree converter,
#: but those carry no domain information for the reader — they just
#: clutter the diagram. The exporter writes them to ``.jucm`` either
#: way (jUCMNav users can keep, rename, or delete them there). Pass
#: ``show_conditions=True`` to the visualizer to render them in the
#: PNG.
DEFAULT_SHOW_CONDITIONS = False
