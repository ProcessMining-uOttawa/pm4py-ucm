"""Drift guards for the things a release has to keep true.

Written after an audit of what actually went stale in this repo versus what
did not. The answer was unambiguous: **everything with an executable guard
held; everything that was prose drifted.** The version sites, the session
registry's gather and restore, the filter keys and the emitted transform all
stayed correct across a dozen releases because a test fails when they do not.
Meanwhile the root README called V5 the deployed app for weeks after V6
shipped, the deploy instructions described a shim that ran V5, and a retired
deployment stayed linked — none of which any test could see.

So these are the cross-cutting facts that a release must not falsify, encoded
as checks rather than as another checklist.

**Each rule here was verified against the source before being written.** Three
plausible-sounding rules were discarded during that pass because the repo does
not actually promise them:

* *"every registry parameter is documented in docs/sessions.md"* — §5 of that
  document is explicitly an **Example entries** list, not a catalogue, and it
  ends by pointing at the registry test as the real guard. Twelve parameters
  are absent by design.
* *"notebooks pin the current version"* — the tutorial's ``0.3.1`` is a
  historical *"As of pm4py-ucm 0.3.1"* note, which is correct and should stay.
* *"screenshots are regenerated"* — they are captured by hand, deliberately.

A guard that encodes a rule the project never made is worse than no guard: it
fails for the wrong reason and teaches people to ignore it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_WEB = _ROOT / "web"

#: Prose files a release has to keep honest.
_DOCS = [
    _ROOT / "README.md",
    _ROOT / "CHANGELOG.md",
    _ROOT / "HANDOFF.md",
    _WEB / "README.md",
    _ROOT / "demo" / "README.md",
    _ROOT / "tests" / "README.md",
    *sorted((_ROOT / "docs").glob("*.md")),
]


def _shimmed_app() -> str:
    """The app file ``web/streamlit_app.py`` runs — the single source of truth
    for which version is deployed.

    Everything else that needs to know "which app is current" derives it from
    here rather than hard-coding a version, so the day V7 lands these guards
    move with it instead of silently checking the wrong file.
    """
    src = (_WEB / "streamlit_app.py").read_text(encoding="utf-8")
    found = re.findall(r'"(streamlit_app_v\d+\.py)"', src)
    assert found, "could not tell which app streamlit_app.py runs"
    return found[0]


def test_the_shim_names_exactly_one_app():
    """If the shim ever names two, every guard below is measuring nothing."""
    src = (_WEB / "streamlit_app.py").read_text(encoding="utf-8")
    named = set(re.findall(r'"(streamlit_app_v\d+\.py)"', src))
    assert len(named) == 1, f"the shim names {named}"
    assert (_WEB / named.pop()).exists()


def test_every_older_app_is_deprecated_and_points_at_the_current_one():
    """Generalises the hard-coded V6 check that used to live in the sessions
    tests. Derived from the shim, so it keeps working after the next major app
    version instead of quietly guarding a file nobody runs — which is the exact
    failure this whole module exists to prevent.
    """
    current = _shimmed_app()
    older = sorted(f for f in _WEB.glob("streamlit_app_v*.py")
                   if f.name != current)
    assert older, "expected at least one superseded app"
    for f in older:
        head = f.read_text(encoding="utf-8")[:2500]
        assert "eprecated" in head, f"{f.name} is not marked deprecated"
        assert current in head, (
            f"{f.name} does not point readers at {current}")


def test_the_readmes_name_the_app_that_is_actually_deployed():
    """The drift that started this: the root README called V5 current for weeks
    after the shim moved to V6, and `web/README.md`'s deploy instructions said
    "a shim that runs V5"."""
    current = _shimmed_app()
    for doc in (_ROOT / "README.md", _WEB / "README.md"):
        text = doc.read_text(encoding="utf-8")
        assert current in text, (
            f"{doc.name} never mentions {current}, the app the shim runs")


def test_no_superseded_app_is_described_as_the_deployed_one():
    """A line may say V5 *was* deployed, or that it is deprecated. It may not
    say it *is* what the deployment serves."""
    current = _shimmed_app()
    cur_v = re.search(r"v(\d+)", current).group(1)
    claim = re.compile(
        r"streamlit_app_v(\d+)\.py[^\n]*?"
        r"(?:\*\*)?(?:is |as )?(?:the )?(?:deployed app|app the deployment "
        r"serves|current app|latest)", re.I)
    past = re.compile(r"\b(was|were|used to|no longer|until|deprecated|"
                      r"superseded|former)\b", re.I)
    offenders = []
    for doc in _DOCS:
        if not doc.exists():
            continue
        for i, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            m = claim.search(line)
            if m and m.group(1) != cur_v and not past.search(line):
                offenders.append(f"{doc.name}:{i}: {line.strip()[:90]}")
    assert not offenders, (
        f"a superseded app is described as deployed (current is {current}):\n  "
        + "\n  ".join(offenders))


def test_retired_deployments_are_not_linked():
    """`pm4py-ucm-scenarios.streamlit.app` was deleted on 2026-08-26. The name
    may stay as plain text — it explains why V2 was frozen — but a link or a
    bare URL sends a reader to a 404, and both GitHub and pdoc linkify a bare
    URL automatically.

    The changelog is exempt: it records what was true at the time."""
    retired = "pm4py-ucm-scenarios.streamlit.app"
    offenders = []
    for doc in _DOCS:
        # The changelog is an append-only record: an entry saying that
        # deployment *served* V2 was true when it was written, and editing it
        # would falsify history. This rule is about prose that describes the
        # system as it is now.
        if not doc.exists() or doc.name == "CHANGELOG.md":
            continue
        for i, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            if retired not in line:
                continue
            # A bare URL, or a markdown link target: both become clickable.
            if re.search(r"https?://[^\s)\]]*" + re.escape(retired), line) \
                    or re.search(r"\]\([^)]*" + re.escape(retired), line):
                offenders.append(f"{doc.name}:{i}: {line.strip()[:90]}")
    assert not offenders, (
        "a retired deployment is still linked (keep the name as plain text "
        "instead):\n  " + "\n  ".join(offenders))


def test_local_links_and_images_resolve():
    """Catches a rename or a move that leaves the prose pointing at nothing —
    the failure mode behind "the Structural-validation cross-reference pointed
    at the cost screen"."""
    broken = []
    for doc in _DOCS:
        if not doc.exists():
            continue
        base = doc.parent
        # An HTML comment is not rendered, so a link inside one is not a live
        # link -- that is how a screenshot placeholder names the file it is
        # waiting for without the guard calling it broken.
        text = re.sub(r"<!--.*?-->", "", doc.read_text(encoding="utf-8"),
                      flags=re.S)
        for m in re.finditer(r"\]\(([^)#][^)]*)\)", text):
            target = m.group(1).split("#")[0].strip()
            if not target or target.startswith(("http", "mailto:")):
                continue
            if not (base / target).exists() and not (_ROOT / target).exists():
                broken.append(f"{doc.name} -> {target}")
    assert not broken, "broken local links:\n  " + "\n  ".join(broken)


def test_docs_do_not_name_a_session_parameter_that_no_longer_exists():
    """The save/resume documentation names parameters individually. The
    registry is free to grow — §5 is an example list, not a catalogue — but a
    document naming a parameter that has been *removed* is telling the reader
    something false about what a saved project contains."""
    import sys
    sys.path.insert(0, str(_WEB))
    from sessions.registry import param_ids

    known = set(param_ids())
    # Names that read like registry ids, as the docs write them: `family_dedup`.
    candidate = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+){1,4})`")
    # Ids retired on purpose, kept in the docs to explain a migration.
    migrated = {"overlay_heatmap_global"}
    stale = []
    for doc in (_ROOT / "docs" / "sessions.md", _ROOT / "docs" / "code_export.md"):
        if not doc.exists():
            continue
        for i, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            for name in candidate.findall(line):
                # Only judge names that look like OUR registry ids: they share
                # a prefix with one. Anything else is some other identifier.
                prefix = name.split("_")[0]
                if name in known or name in migrated:
                    continue
                if any(k.split("_")[0] == prefix for k in known) and \
                        prefix in {"overlay", "scenario", "family", "compare",
                                   "simulation"}:
                    stale.append(f"{doc.name}:{i}: `{name}`")
    assert not stale, (
        "documentation names session parameters that are not in the registry "
        "(renamed or removed?):\n  " + "\n  ".join(sorted(set(stale))))


def test_the_release_tooling_is_present_and_runnable():
    """The skill in `.claude/skills/release/` tells a reader to run these; if
    one is renamed or dropped, the procedure silently becomes prose again —
    which is the failure mode this whole module exists to prevent."""
    import subprocess
    import sys

    checker = _ROOT / "tools" / "release_check.py"
    audit = _ROOT / "tools" / "cache_audit.py"
    skill = _ROOT / ".claude" / "skills" / "release" / "SKILL.md"
    for p in (checker, audit, skill):
        assert p.exists(), f"missing {p.relative_to(_ROOT)}"

    # The checker must run and report, whatever its verdict on today's tree.
    proc = subprocess.run([sys.executable, str(checker)],
                          capture_output=True, text=True, cwd=str(_ROOT))
    assert "version sites" in proc.stdout, proc.stdout + proc.stderr

    # Every command the skill tells a reader to run must exist.
    text = skill.read_text(encoding="utf-8")
    for referenced in ("tools/release_check.py",
                       "tests/test_release_consistency.py",
                       "web/sessions/registry.py"):
        assert referenced in text, f"skill no longer mentions {referenced}"
        assert (_ROOT / referenced).exists(), f"{referenced} does not exist"
