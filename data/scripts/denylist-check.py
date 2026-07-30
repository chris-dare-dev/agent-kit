#!/usr/bin/env python3
"""denylist-check.py -- no employer identifier may re-enter a tracked file.

This repository is a genericized fork of an internal tool. A sweep removed the
former employer's identifiers from shipped content; this gate is what stops a
future sync from the internal tree putting them back. Without it the sweep decays
the first time someone copies a file across.

DESIGN NOTES, because the obvious implementations are all wrong:

* It scans TRACKED files (git ls-files), not the worktree. Untracked scratch
  files are not shipped and flagging them trains people to ignore the gate.

* It must not match its OWN patterns. A gate that forbids a string has to contain
  that string, so PATTERNS are assembled from fragments at runtime and this file
  is excluded from its own scan. Without that, the gate is red the moment it
  exists -- which is how gates get deleted.

* Bare "gitlab" is NOT a denylist term. Roughly 200 lines across 81 files use it
  as a product name (.gitlab-ci.yml, gitlab-workflow, the glpat- secret-detector
  prefix, a provider enum value). plans/ci-proves-it/roadmap.yaml alone would be
  100% false positives. The VCS-root rule therefore requires a trailing
  separator and is case-sensitive on the G, so "GitLab/" matches and "gitlab-ci"
  does not.

* history-file exemptions are legal ONLY under docs/** and plans/**, enforced
  below. Findings documents and roadmaps must be able to quote what they found;
  an executable or shipped file must not get a whole-file pass. Widening that
  surface requires editing THIS file, so it shows up in review.

Exit codes: 0 clean, 2 a violation (with --check), 1 a usage/environment error.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_PATH = Path(__file__).resolve().parent / "denylist-allowlist.json"
SELF_NAME = "data/scripts/denylist-check.py"

# Assembled from fragments so this file does not match itself.
_G = "G" + "itLab"
_SWAT = "SWAT" + " DevOps"
_NALEJ = "nale" + "j-"
_CMS = "claude-" + "mcp-server"
_GLX = "gitlab" + ".example"

PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("employer-group", re.compile(re.escape(_SWAT)), "former employer's internal group name"),
    ("employer-product", re.compile(re.escape(_NALEJ)), "former employer's product key"),
    ("pre-fork-package", re.compile(re.escape(_CMS)), "pre-fork package name; this kit is agent-kit"),
    ("employer-host", re.compile(re.escape(_GLX)), "employer-shaped example host"),
    # Trailing separator + case-sensitive G: matches a VCS-root PATH segment,
    # never the product name. See the design note above.
    ("vcs-root-path", re.compile(_G + r"[/\\]"), "pre-fork monorepo path segment"),
]

# Only these roots may carry a whole-file history exemption.
HISTORY_ROOTS = ("docs/", "plans/")


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        sys.exit("error: git ls-files failed; run this inside the repository")
    return [line for line in result.stdout.splitlines() if line.strip()]


def load_allowlist() -> tuple[list[dict], list[str]]:
    if not ALLOWLIST_PATH.exists():
        return [], []
    payload = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    entries = payload.get("entries", [])
    problems: list[str] = []
    for entry in entries:
        if not entry.get("reason"):
            problems.append(f"allowlist entry {entry!r} has no reason")
        if entry.get("kind") == "history-file":
            path = entry.get("path", "")
            if not path.startswith(HISTORY_ROOTS):
                problems.append(
                    f"allowlist: history-file exemption '{path}' is outside "
                    f"{HISTORY_ROOTS}. Whole-file passes are legal only for "
                    "findings documents and roadmaps; use a snippet entry instead."
                )
    return entries, problems


def exempt(entries: list[dict], path: str, line: str) -> bool:
    for entry in entries:
        kind = entry.get("kind")
        if kind == "history-file":
            pattern = entry.get("path", "")
            if pattern.endswith("/**"):
                if path.startswith(pattern[:-2]):
                    return True
            elif pattern == path or Path(path).match(pattern):
                return True
        elif kind == "snippet":
            if entry.get("path") == path and entry.get("snippet", "\0") in line:
                return True
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="exit 2 on any violation")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    entries, problems = load_allowlist()
    violations: list[str] = []
    scanned = 0

    for path in tracked_files():
        if path == SELF_NAME:
            continue  # see the design note: it contains what it forbids
        full = REPO_ROOT / path
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except (OSError, IsADirectoryError):
            continue
        scanned += 1
        for number, line in enumerate(text.splitlines(), 1):
            for name, pattern, why in PATTERNS:
                if pattern.search(line) and not exempt(entries, path, line):
                    violations.append(
                        f"{path}:{number}: [{name}] {why}\n      {line.strip()[:120]}"
                    )

    if args.verbose:
        print(f"scanned {scanned} tracked files against {len(PATTERNS)} patterns")
        print(f"allowlist entries: {len(entries)}")

    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    for violation in violations:
        print(f"FAIL: {violation}", file=sys.stderr)

    total = len(problems) + len(violations)
    if total:
        print(
            f"\n{total} denylist violation(s). Either remove the identifier, or -- "
            "if it is genuinely history or a decision recorded elsewhere -- add a "
            f"narrow entry with a reason to {ALLOWLIST_PATH.name}.",
            file=sys.stderr,
        )
        return 2 if args.check else 0

    print(f"OK: {scanned} tracked files carry no employer identifier")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
