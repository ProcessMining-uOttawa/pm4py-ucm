"""Variant-dispatching visualiser for Use Case Maps.

Mirrors the public surface of ``pm4py.visualization.bpmn.visualizer``::

    from pm4py_ucm.visualization.ucm import visualizer as ucm_visualizer
    gviz = ucm_visualizer.apply(ucm)
    ucm_visualizer.view(gviz)
    ucm_visualizer.save(gviz, "diagram.png")

The graphviz binaries must be available on ``PATH`` for rendering. If only
the source string is needed (no rendering), use
``ucm_visualizer.apply(ucm).source``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from enum import Enum
from typing import Any, Dict, Optional

from graphviz import Digraph

from .variants import classic as _classic
from ...objects.ucm.obj import UCM


class Variants(Enum):
    """Available rendering back-ends."""
    CLASSIC = _classic


DEFAULT_VARIANT = Variants.CLASSIC


def apply(
    ucm: UCM,
    variant: Variants = DEFAULT_VARIANT,
    parameters: Optional[Dict[str, Any]] = None,
) -> Digraph:
    """Build a :class:`graphviz.Digraph` for the given UCM model.

    The returned object is *not* yet rendered to disk. Call :func:`view` or
    :func:`save` to materialise it.
    """
    return variant.value.apply(ucm, parameters=parameters)


def save(gviz: Digraph, output_file_path: str) -> str:
    """Render ``gviz`` and write it to ``output_file_path``.

    The output format is taken from the file extension when possible
    (``.png``, ``.svg``, ``.pdf`` …); otherwise the graph's current
    ``format`` attribute is used. Returns the resulting file path.
    """
    base, ext = os.path.splitext(output_file_path)
    if ext:
        gviz.format = ext.lstrip(".").lower()
    rendered = gviz.render(filename=base, cleanup=True)
    if rendered != output_file_path:
        shutil.move(rendered, output_file_path)
    return output_file_path


def view(gviz: Digraph) -> None:
    """Render ``gviz`` to a temporary file and open it in the system viewer.

    On headless systems this falls back to writing the rendered file into a
    temporary directory and printing the path.
    """
    tmpdir = tempfile.mkdtemp(prefix="pm4py_ucm_viz_")
    out_path = os.path.join(tmpdir, "ucm")
    rendered = gviz.render(filename=out_path, cleanup=True, view=True)
    return rendered


def matplotlib_view(gviz: Digraph) -> None:
    """Render and display the diagram inline using matplotlib.

    Convenience for notebook use; intentionally matches the helper of the
    same name in ``pm4py.visualization.bpmn.visualizer``.
    """
    import matplotlib.pyplot as plt  # local import: keep mpl optional
    import matplotlib.image as mpimg

    gviz.format = "png"
    tmpdir = tempfile.mkdtemp(prefix="pm4py_ucm_viz_")
    out_path = os.path.join(tmpdir, "ucm")
    rendered = gviz.render(filename=out_path, cleanup=True)
    img = mpimg.imread(rendered)
    plt.axis("off")
    plt.tight_layout(pad=0)
    plt.imshow(img)
    plt.show()
