#!/usr/bin/env python3
"""template-settings-check.py -- every hook the templates register must exist.

The settings templates are merged into a user's Claude Code settings.json, so a
hook path that does not resolve becomes a guard the user believes is running and
which silently is not. That is worse than shipping no hook: it converts an absent
control into a false sense of one.

This gate previously had nothing to catch it. The PostToolUse Edit|Write hook
registered `${CLAUDE_PROJECT_DIR}/scripts/project-linker-hook.sh` -- there is no
`scripts/` directory holding that file (it lives in workspace-tooling/) -- and
the `[ -f ... ] || exit 0` fallback meant it failed silently forever.

It resolves each registered path against what the installer actually plants:
`.claude/hooks/*` comes from `data/hooks/`, `.claude/scripts/*` from
`data/scripts/` (see PLANT_DIRS in scripts/init.mjs).

Usage:
    python3 data/scripts/template-settings-check.py --check
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ("template-settings.json", "template-settings.k8s.json")

# What init.mjs plants where: <workspace>/.claude/<sub> <- data/<sub>
PLANTED_FROM = {".claude/hooks/": "data/hooks/", ".claude/scripts/": "data/scripts/"}

HOOK_PATH_RE = re.compile(r'\$\{CLAUDE_PROJECT_DIR:-\$PWD\}/([^"\']+)')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)

    problems: list[str] = []
    checked = 0

    for name in TEMPLATES:
        path = REPO_ROOT / "data" / "scripts" / name
        if not path.exists():
            problems.append(f"{name}: missing")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{name}: invalid JSON ({exc})")
            continue

        for event, groups in (payload.get("hooks") or {}).items():
            for group in groups:
                for entry in group.get("hooks", []):
                    command = entry.get("command", "")
                    found = HOOK_PATH_RE.findall(command)
                    if not found:
                        problems.append(
                            f"{name}: {event} hook has no "
                            "${CLAUDE_PROJECT_DIR}-rooted path -- every hook path "
                            "must be project-rooted, with no machine-specific fallback"
                        )
                        continue
                    for rel in found:
                        checked += 1
                        source = None
                        for prefix, data_dir in PLANTED_FROM.items():
                            if rel.startswith(prefix):
                                source = REPO_ROOT / data_dir / rel[len(prefix):]
                                break
                        if source is None:
                            problems.append(
                                f"{name}: {event} registers '{rel}', which the "
                                f"installer never plants (known prefixes: "
                                f"{', '.join(PLANTED_FROM)})"
                            )
                        elif not source.exists():
                            problems.append(
                                f"{name}: {event} registers '{rel}' but its source "
                                f"{source.relative_to(REPO_ROOT).as_posix()} does not exist"
                            )

        # A machine-specific fallback is the defect this file exists to prevent.
        raw = path.read_text(encoding="utf-8")
        for smell in ("PERSONAL_WORKSPACE_ROOT", "Work/workspace", "$HOME/"):
            if smell in raw:
                problems.append(f"{name}: contains a machine-specific path fallback ({smell})")

    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} problem(s)", file=sys.stderr)
        return 2 if args.check else 0
    print(f"OK: {checked} registered hook path(s) resolve to a planted source")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
