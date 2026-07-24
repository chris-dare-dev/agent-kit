#!/usr/bin/env python3
"""Link-existence gate for `.claude/<tier>/<file>` path refs in content files.

Every `.claude/<tier>/<file>` path reference in a skill / agent / command body
must resolve against its canonical target `data/<tier>/<file>` (the `.claude/<tier>`
paths are symlinks into `data/<tier>`, so this is exactly what a Claude session
resolves). This is the regression gate for the S3.6 broken-ref class: the old
naive Codex mirror (`sync-codex-skills.py`) blindly rewrote `.claude/` -> `.Codex/`,
producing references to directories that never existed; this check catches any
NEW broken ref at the SOURCE — across all three content tiers the adapter
generators consume (skills 40, agents 97, commands 16) — before they propagate it.

Placeholder tolerance: a doc pattern like `.claude/scripts/<name>-<file>` (a
literal `<...>` placeholder) is skipped, not flagged.

Stdlib only. CWD-independent. Exit 0 clean | 1 broken ref(s). `--self-test` self-checks.

Usage: python3 data/scripts/skill-ref-check.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # data/scripts/x.py -> repo root
DATA = ROOT / "data"

# .claude/<tier>/<file> — the tiers that are symlinks into data/<tier>.
REF_RE = re.compile(r"\.claude/(skills|scripts|references|hooks|agents|commands)/([A-Za-z0-9._/-]+)")

SOURCES = [
    *sorted(DATA.glob("skills/*/SKILL.md")),
    *sorted(DATA.glob("agents/*.md")),
    *sorted(DATA.glob("commands/*.md")),
]


def broken_refs(text: str) -> list[tuple[str, str]]:
    """Return (tier, rest) pairs whose data/<tier>/<rest> target is missing."""
    out = []
    for m in REF_RE.finditer(text):
        # Skip literal `<...>` placeholders (`.claude/scripts/foo-<file>`).
        if text[m.end():m.end() + 1] == "<":
            continue
        tier, rest = m.group(1), m.group(2).rstrip(").,;:`'\"")
        if not (DATA / tier / rest).exists():
            out.append((tier, rest))
    return out


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        assert broken_refs("see .claude/references/model-policy.md ok") == []
        assert broken_refs(".claude/scripts/does-not-exist-xyz.py") == [("scripts", "does-not-exist-xyz.py")]
        assert broken_refs(".claude/scripts/<file> placeholder") == []
        print("skill-ref-check self-test: OK")
        return 0

    broken: list[str] = []
    checked = 0
    for src in SOURCES:
        text = src.read_text(encoding="utf-8")
        rel = src.relative_to(ROOT)
        for m in REF_RE.finditer(text):
            if text[m.end():m.end() + 1] == "<":
                continue
            checked += 1
        for tier, rest in broken_refs(text):
            broken.append(f"{rel}: .claude/{tier}/{rest} -> missing data/{tier}/{rest}")

    for b in broken:
        print(f"FAIL: {b}", file=sys.stderr)
    if broken:
        print(f"{len(broken)} broken ref(s) of {checked} checked", file=sys.stderr)
        return 1
    print(f"skill-ref-check: {checked} path ref(s) across skills/agents/commands all resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
