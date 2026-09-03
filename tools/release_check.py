"""Is this tree ready to cut a release?

    python tools/release_check.py            # report
    python tools/release_check.py 0.8.3      # report, checked against a target

Reports the state of everything a release has to get right, in one place, so
the cut starts from a green board instead of from memory. Every check here
either passed silently for a dozen releases or cost real time when it failed:

* the three version sites agreeing (a test enforces this; shown for context);
* the changelog carrying an ``[Unreleased]`` section with something in it;
* the tag not already existing, compared by **version** order — a lexical
  sort puts ``v0.7.10`` before ``v0.7.5`` and would say the tag is free when
  it is not;
* the tag's commit containing ``.github/workflows/publish.yml``, without which
  the release-triggered run is created *silently not at all*;
* a clean tree on ``main``, so the tag lands on what was reviewed.

It does not run the test suite — that is the slow part and CI does it — and it
does not touch anything. Exit code 0 means ready.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OK, WARN, BAD = "ok  ", "warn", "FAIL"


def sh(*args: str) -> str:
    try:
        return subprocess.run(args, cwd=ROOT, capture_output=True,
                              text=True, check=False).stdout.strip()
    except OSError:
        return ""


def version_sites() -> dict:
    out = {}
    for path, pattern in (
        ("pyproject.toml", r'^version\s*=\s*"([^"]+)"'),
        ("pm4py_ucm/__init__.py", r'^__version__\s*=\s*"([^"]+)"'),
        ("web/sessions/codegen.py", r'^GENERATOR_VERSION\s*=\s*"([^"]+)"'),
    ):
        text = (ROOT / path).read_text(encoding="utf-8")
        m = re.search(pattern, text, re.M)
        out[path] = m.group(1) if m else None
    return out


def version_key(tag: str):
    return tuple(int(p) for p in re.findall(r"\d+", tag))


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else None
    rows, ready = [], True

    # --- version sites -------------------------------------------------
    sites = version_sites()
    versions = set(sites.values())
    if len(versions) == 1 and None not in versions:
        current = versions.pop()
        rows.append((OK, "version sites", f"all three at {current}"))
    else:
        current = None
        ready = False
        rows.append((BAD, "version sites", f"disagree: {sites}"))

    if target and current and target != current:
        ready = False
        rows.append((BAD, "target version",
                     f"asked for {target}, sites say {current} — bump first"))
    elif target:
        rows.append((OK, "target version", f"{target} matches the sites"))

    # --- changelog -----------------------------------------------------
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if "## [Unreleased]" not in changelog:
        ready = False
        rows.append((BAD, "changelog", "no [Unreleased] section"))
    else:
        after = changelog.split("## [Unreleased]", 1)[1]
        body = after.split("\n## ", 1)[0].strip()
        if body:
            rows.append((OK, "changelog",
                         f"[Unreleased] has {len(body.splitlines())} lines to "
                         f"retitle"))
        else:
            rows.append((WARN, "changelog",
                         "[Unreleased] is empty — nothing to release?"))
        if current and f"## [{current}]" in changelog:
            rows.append((WARN, "changelog",
                         f"[{current}] already dated — already cut?"))

    # --- git state ------------------------------------------------------
    branch = sh("git", "rev-parse", "--abbrev-ref", "HEAD")
    rows.append((OK if branch == "main" else WARN, "branch",
                 branch or "unknown"))
    dirty = sh("git", "status", "--porcelain")
    if dirty:
        ready = False
        rows.append((BAD, "working tree",
                     f"{len(dirty.splitlines())} uncommitted change(s)"))
    else:
        rows.append((OK, "working tree", "clean"))

    # --- the tag --------------------------------------------------------
    remote = sh("git", "ls-remote", "--tags", "origin")
    tags = sorted({re.sub(r".*refs/tags/", "", l).replace("^{}", "")
                   for l in remote.splitlines() if "refs/tags/" in l},
                  key=version_key)
    latest = tags[-1] if tags else "(none)"
    rows.append((OK, "latest tag", f"{latest}   (version-sorted, not lexical)"))
    if current:
        want = f"v{current}"
        if want in tags:
            ready = False
            rows.append((BAD, "tag free", f"{want} already exists"))
        else:
            rows.append((OK, "tag free", f"{want} is available"))

    # --- the publish workflow must be in the tagged commit --------------
    wf = sh("git", "show", "HEAD:.github/workflows/publish.yml")
    if wf:
        rows.append((OK, "publish.yml", "present at HEAD"))
    else:
        ready = False
        rows.append((BAD, "publish.yml",
                     "absent at HEAD — the release run is created silently "
                     "not at all"))

    width = max(len(k) for _, k, _ in rows)
    print()
    for status, key, detail in rows:
        print(f"  [{status}] {key:<{width}}  {detail}")
    print()
    print("READY to cut" if ready else "NOT ready — fix the FAILs above")
    print("(the drift guards live in tests/test_release_consistency.py; "
          "run the suite too)")
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
