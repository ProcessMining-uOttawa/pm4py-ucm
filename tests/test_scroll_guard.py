"""The page must stay scrollable to its end after a rerun adds content.

In Chrome 157 the main area's scroll range *for real input* (scrollbar,
two-finger scroll, End key) stays that of the shorter page when a rerun
appends content to a view already on screen: the synthesis results, the
family grid or a diagram cannot be reached without zooming the browser out,
although ``scrollHeight`` is right and a script can scroll there. The app
works around it with ``_SCROLL_GUARD_HTML`` in ``web/streamlit_app_v6.py``,
a script that watches the main area's height and, once it has grown and
settled, takes the main block out of layout for a frame and puts it back —
the one thing found to make Chrome rebuild the range.

These tests pin that the guard is delivered and how it behaves, because a
mistake here is worse than the bug: a guard that never restores the block
leaves a blank page. The behaviour is exercised by running the script itself
under Node against a small stand-in for the page (skipped without Node); the
delivery is checked on the app's source, which cannot be imported without a
running Streamlit.
"""
import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_APP = _ROOT / "web" / "streamlit_app_v6.py"


@pytest.fixture(scope="module")
def src():
    return _APP.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def guard_html(src):
    for node in ast.parse(src).body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name)
                        and t.id == "_SCROLL_GUARD_HTML"
                        for t in node.targets)):
            return ast.literal_eval(node.value)
    raise AssertionError("_SCROLL_GUARD_HTML not found")


@pytest.fixture(scope="module")
def guard_js(guard_html):
    m = re.search(r"<script>(.*)</script>", guard_html, flags=re.S)
    assert m, "the guard carries no script"
    return m.group(1)


def test_guard_is_delivered_with_scripts_enabled(src):
    assert re.search(
        r"st\.html\(\s*_SCROLL_GUARD_HTML\s*,\s*unsafe_allow_javascript=True",
        src), "the guard must go through st.html with JavaScript allowed"


def test_guard_carrier_takes_no_space(src, guard_html):
    assert 'class="pm-scroll-guard"' in guard_html
    assert re.search(
        r":has\(\.pm-scroll-guard\)\s*\{\s*display:\s*none\s*!important",
        src), "the carrier's element container should be collapsed"


def test_guard_installs_once_per_page(guard_js, harness):
    assert "window.__pm4pyScrollGuard" in guard_js
    assert re.search(r"if\s*\(window\.__pm4pyScrollGuard\b[^)]*\)\s*return",
                     guard_js)


# A stand-in for the page: one scroller with one block, a controllable
# height, and hand-cranked timers and animation frames. ``steps`` drive it;
# the harness reports every change of the block's ``display``.
_HARNESS = r"""
(() => {
const guard = process.argv[2], steps = JSON.parse(process.argv[3]);
const log = [];
let height = 1000, ticks = [], frames = [], timeouts = [], popover = false;
const listeners = {};
const block = { _d: "", style: {} };
Object.defineProperty(block.style, "display", {
  get() { return block._d; },
  set(v) { block._d = v; log.push(v === "none" ? "hide" : "show"); } });
const main = {
  scrollTop: 0, firstElementChild: block,
  get scrollHeight() { return block._d === "none" ? 600 : height; },
  get offsetHeight() { return 600; } };
const document = {
  hidden: false, activeElement: null,
  querySelector(sel) {
    if (sel.includes("stMain")) return main;
    return popover ? {} : null; } };
const window = { chrome: steps.chrome === false ? undefined : {} };
const addEventListener = (t, f) => { (listeners[t] = listeners[t] || []).push(f); };
const setInterval = (f) => { ticks.push(f); return 1; };
const setTimeout = (f) => { timeouts.push(f); return 1; };
const requestAnimationFrame = (f) => { frames.push(f); return 1; };
new Function("window", "document", "addEventListener", "setInterval",
             "setTimeout", "requestAnimationFrame", guard)(
  window, document, addEventListener, setInterval, setTimeout,
  requestAnimationFrame);
const fire = (t) => (listeners[t] || []).forEach((f) => f({}));
for (const s of steps.run) {
  if (s.height !== undefined) height = s.height;
  if (s.popover !== undefined) popover = s.popover;
  if (s.hidden !== undefined) document.hidden = s.hidden;
  if (s.scrollTop !== undefined) main.scrollTop = s.scrollTop;
  if (s.event) fire(s.event);
  if (s.tick) ticks.forEach((f) => f());
  if (s.frame) { const q = frames; frames = []; q.forEach((f) => f()); }
  if (s.timeout) { const q = timeouts; timeouts = []; q.forEach((f) => f()); }
}
console.log(JSON.stringify({
  log, display: block._d, scrollTop: main.scrollTop, ticks: ticks.length }));
})();
"""


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    path = tmp_path_factory.mktemp("scroll_guard") / "harness.js"
    path.write_text(_HARNESS, encoding="utf-8")
    return path


def _run(guard_js, *, run, harness, **opts):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    out = subprocess.run(
        [node, str(harness), guard_js, json.dumps({"run": run, **opts})],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_TICK = {"tick": True}
_FRAME = {"frame": True}
# First two ticks settle on the starting height (it "grew" from nothing).
_SETTLE = [_TICK, _TICK, _FRAME, _FRAME]


def test_growth_rebuilds_once_and_restores(guard_js, harness):
    r = _run(guard_js, harness=harness, run=_SETTLE + [
        {"height": 3000, "scrollTop": 420, "tick": True},   # grew: wait
        _TICK,                                              # steady: rebuild
        _FRAME, _FRAME,
        _TICK, _TICK, _TICK,                                # nothing more
    ])
    assert r["log"] == ["hide", "show", "hide", "show"]
    assert r["display"] == ""
    assert r["scrollTop"] == 420, "the scroll position must be put back"


def test_no_rebuild_while_height_is_still_changing(guard_js, harness):
    r = _run(guard_js, harness=harness, run=_SETTLE + [
        {"height": 2000, "tick": True},
        {"height": 2500, "tick": True},
        {"height": 3000, "tick": True},
    ])
    assert r["log"] == ["hide", "show"], "only the initial settle"


def test_shrinking_page_is_left_alone(guard_js, harness):
    r = _run(guard_js, harness=harness, run=_SETTLE + [
        {"height": 700, "tick": True}, _TICK, _TICK])
    assert r["log"] == ["hide", "show"]


def test_same_height_never_rebuilds_again(guard_js, harness):
    r = _run(guard_js, harness=harness, run=_SETTLE + [_TICK] * 10)
    assert r["log"] == ["hide", "show"], "the guard must not loop"


@pytest.mark.parametrize("blocker, release", [
    ({"event": "pointerdown"}, {"event": "pointerup"}),
    ({"popover": True}, {"popover": False}),
    ({"hidden": True}, {"hidden": False}),
])
def test_rebuild_waits_while_the_page_is_in_use(guard_js, harness, blocker, release):
    r = _run(guard_js, harness=harness, run=_SETTLE + [
        blocker,
        {"height": 3000, "tick": True}, _TICK, _TICK,
    ])
    assert r["log"] == ["hide", "show"], "must not interrupt the user"
    r = _run(guard_js, harness=harness, run=_SETTLE + [
        blocker,
        {"height": 3000, "tick": True}, _TICK,
        release, _TICK, _TICK, _FRAME, _FRAME,
    ])
    assert r["log"] == ["hide", "show", "hide", "show"]


def test_block_is_restored_even_without_animation_frames(guard_js, harness):
    """A throttled tab may never run the frames: the timer must restore."""
    r = _run(guard_js, harness=harness, run=[_TICK, _TICK, {"timeout": True}])
    assert r["log"] == ["hide", "show"]
    assert r["display"] == ""
    # Late frames after the timer must not hide or show anything again.
    r = _run(guard_js, harness=harness,
             run=[_TICK, _TICK, {"timeout": True}, _FRAME, _FRAME])
    assert r["log"] == ["hide", "show"]


def test_guard_is_inert_outside_chromium(guard_js, harness):
    r = _run(guard_js, harness=harness, run=[_TICK, _TICK], chrome=False)
    assert r["ticks"] == 0 and r["log"] == []
