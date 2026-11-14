"""The inline SVG viewer must not trap the mouse wheel.

Every model diagram in the web app (the Model, Scenarios, Family and Compare
views) is shown by one HTML/JS island, ``_SVG_VIEWER_TEMPLATE`` in
``web/streamlit_app_v6.py``, embedded in an iframe. Up to 0.9.2 its wheel
listener called ``preventDefault`` on every notch and zoomed instead. The
viewer is routinely as tall as the browser window — 620 px by default, up to
1600 px on the *Diagram height* slider — so with the pointer anywhere over
the diagram the wheel could neither scroll to the rest of the diagram nor
move the page on to the download / export / pin row under it. The page
simply seemed stuck.

The contract these tests pin:

* a **plain wheel belongs to the browser** — the listener returns before
  ``preventDefault`` unless Ctrl/Cmd is held (a trackpad pinch arrives as
  ctrl+wheel, so pinch still zooms);
* the scroll may **chain out to the page** — no ``overscroll-behavior:
  contain`` on the stage, which would stop it at the stage's edge;
* a **finger can scroll through** the viewer — no ``touch-action: none``;
* **zoom stays reachable without the wheel** — the − / + / Fit buttons exist
  and are wired;
* the **iframe is at least as tall as the stage**, so the stage's own bottom
  edge and horizontal scrollbar are never clipped.

The app script cannot be imported without a running Streamlit, so the
template and the viewer function are read out of the source with ``ast`` —
the same approach the other web-app tests take.
"""
import ast
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_APP = _ROOT / "web" / "streamlit_app_v6.py"


def _viewer_parts(src: str):
    """``(template, viewer_function_source)`` from the app's source text."""
    tree = ast.parse(src)
    template = viewer = None
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name)
                        and t.id == "_SVG_VIEWER_TEMPLATE"
                        for t in node.targets)):
            template = ast.literal_eval(node.value)
        elif isinstance(node, ast.FunctionDef) and node.name == "_svg_viewer":
            viewer = ast.get_source_segment(src, node)
    assert template is not None, "_SVG_VIEWER_TEMPLATE not found"
    assert viewer is not None, "_svg_viewer not found"
    return template, viewer


@pytest.fixture(scope="module")
def parts():
    return _viewer_parts(_APP.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def template(parts):
    return parts[0]


def _wheel_listeners(template: str):
    """Bodies of every ``addEventListener("wheel", …)`` in the template."""
    bodies = re.findall(
        r'addEventListener\(\s*"wheel"\s*,(.*?)\{\s*passive:\s*false\s*\}\s*\)',
        template, flags=re.S)
    # A wheel listener registered any other way (passive, or on window)
    # would escape the checks below — insist the count matches.
    assert len(bodies) == template.count('"wheel"'), \
        "a wheel listener is registered in a form this test does not parse"
    return bodies


def test_there_is_a_wheel_listener_to_check(template):
    assert _wheel_listeners(template), "the viewer no longer zooms on wheel"


def test_plain_wheel_is_not_swallowed(template):
    """``preventDefault`` only after a Ctrl/Cmd guard has let plain wheels go."""
    for body in _wheel_listeners(template):
        assert "preventDefault" in body
        guard = re.search(
            r"if\s*\(\s*!\s*\(\s*e\.ctrlKey\s*\|\|\s*e\.metaKey\s*\)\s*\)"
            r"\s*return\s*;", body)
        assert guard, "the wheel listener has no Ctrl/Cmd guard"
        assert guard.end() <= body.index("preventDefault"), \
            "preventDefault runs before the Ctrl/Cmd guard"


def test_scroll_can_chain_to_the_page(template):
    css = template[:template.index("</style>")]
    assert "overscroll-behavior" not in css


def test_touch_can_scroll_through_the_viewer(template):
    css = template[:template.index("</style>")]
    actions = re.findall(r"touch-action\s*:\s*([^;]+);", css)
    assert actions, "the stage should declare its touch-action"
    assert all(a.strip() != "none" for a in actions)


@pytest.mark.parametrize("button", ["zin", "zout", "zfit"])
def test_zoom_is_reachable_without_the_wheel(template, button):
    assert f'id="{button}"' in template
    assert re.search(
        rf'getElementById\("{button}"\)\s*\.addEventListener\(\s*"click"',
        template), f"#{button} is not wired"


def test_iframe_is_at_least_as_tall_as_the_stage(parts):
    template, viewer = parts
    # The stage fills #wrap, whose height is the requested one …
    assert re.search(r"#wrap\s*\{[^}]*height:\s*__HEIGHT__px", template)
    assert re.search(r"#stage\s*\{[^}]*height:\s*100%", template)
    # … and the iframe is given that height plus a non-negative margin.
    margin = re.search(r"height=height \+ (\d+)", viewer)
    assert margin and int(margin.group(1)) >= 0


def test_guard_fails_on_the_0_9_2_listener():
    """The check above must reject the listener that caused the bug."""
    old = ('stage.addEventListener("wheel", (e) => {\n'
           '    e.preventDefault(); closeMenu();\n'
           '  }, { passive: false });')
    with pytest.raises(AssertionError):
        test_plain_wheel_is_not_swallowed(old)
