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


def entry_key(entry: dict) -> str:
    """Stable identity for an exemption, for usage reporting."""
    kind = entry.get("kind", "?")
    path = entry.get("path", "?")
    snippet = entry.get("snippet")
    return f"{kind} {path}" + (f" [{snippet}]" if snippet else "")


def exempt(entries: list[dict], path: str, line: str, used: set[str] | None = None) -> bool:
    """Does any entry cover this line? Records which one fired in `used`.

    Usage tracking is the point. An exemption that suppresses nothing is stale,
    and a stale one is invisible: it neither fails nor announces itself, so it
    outlives the issue it was filed against and silently widens the gate the day
    a matching violation appears. `workspace-tooling/project-map.json` is the
    live example -- its reason says "remove when #38 lands", and it also asserts
    the file is untracked, which it no longer is.
    """
    covered = False
    # Every matching entry is marked, not just the first. Short-circuiting made
    # usage order-dependent: a deliberately explicit duplicate could never fire,
    # because a broader rule above it always answered first, so the staleness
    # check demanded the deletion of a rule that was doing its job.
    for entry in entries:
        kind = entry.get("kind")
        hit = False
        if kind == "history-file":
            pattern = entry.get("path", "")
            if pattern.endswith("/**"):
                hit = path.startswith(pattern[:-2])
            else:
                hit = pattern == path or Path(path).match(pattern)
        elif kind == "snippet":
            hit = entry.get("path") == path and entry.get("snippet", "\0") in line
        if hit:
            covered = True
            if used is None:
                return True  # no usage tracking wanted: first match is enough
            used.add(entry_key(entry))
    return covered


def _self_test() -> int:
    history = {"kind": "history-file", "path": "docs/x.md", "reason": "r"}
    broad = {"kind": "history-file", "path": "plans/*/roadmap.yaml", "reason": "r"}
    exact = {"kind": "history-file", "path": "plans/a/roadmap.yaml", "reason": "r"}
    snippet = {"kind": "snippet", "path": "a.py", "snippet": "tok", "reason": "r"}

    used: set[str] = set()
    assert exempt([history], "docs/x.md", "any", used)
    assert entry_key(history) in used

    # Order independence: a broader rule listed first must not starve the
    # explicit duplicate below it, or staleness reporting punishes redundancy.
    used = set()
    assert exempt([broad, exact], "plans/a/roadmap.yaml", "any", used)
    assert used == {entry_key(broad), entry_key(exact)}, used

    # Snippet entries match on path AND content.
    used = set()
    assert exempt([snippet], "a.py", "has tok here", used)
    assert not exempt([snippet], "a.py", "no match", set())
    assert not exempt([snippet], "b.py", "has tok here", set())

    # Without a usage set the first match short-circuits, as before.
    assert exempt([broad, exact], "plans/a/roadmap.yaml", "any") is True
    assert exempt([], "anything", "any") is False

    print("denylist-check self-test: OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="exit 2 on any violation")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--self-test", action="store_true", help="self-check the exemption logic")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    entries, problems = load_allowlist()
    violations: list[str] = []
    used: set[str] = set()
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
                if pattern.search(line) and not exempt(entries, path, line, used):
                    violations.append(
                        f"{path}:{number}: [{name}] {why}\n      {line.strip()[:120]}"
                    )

    # An exemption that suppressed nothing has outlived whatever it was filed
    # for. Reported as a failure, not a note: the whole hazard is that a stale
    # exemption is silent right up until it starts hiding a real violation.
    #
    # Entries marked "pre-emptive" are exempt from this, because covering a
    # currently-clean surface is their stated purpose -- but the exemption has
    # to SAY so, in the reason, where a reviewer sees it.
    for entry in entries:
        key = entry_key(entry)
        if key in used:
            continue
        if "pre-emptive" in (entry.get("reason") or "").lower():
            continue
        problems.append(
            f"allowlist: exemption '{key}' matched nothing. Either it is stale "
            f"and should be deleted, or its reason should say 'pre-emptive'. "
            f"Reason on file: {(entry.get('reason') or '')[:120]}"
        )

    if args.verbose:
        print(f"scanned {scanned} tracked files against {len(PATTERNS)} patterns")
        print(f"allowlist entries: {len(entries)}, of which {len(used)} fired")

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
