#!/usr/bin/env python3
"""ONLINE gate: an exemption may not outlive the issue that justifies it.

Suppressions in this repository are written against an open issue and say so --
"Remove this entry when #38 lands", "fails at HEAD ... issue #59". Nothing ever
checked whether that issue was still open. `denylist-check.py`'s
`workspace-tooling/project-map.json` exemption is the live example: issue #38 is
CLOSED, the exemption is still there, and it is still actively suppressing
matches. It is not stale by the offline gate's measure -- it fires -- so only
the issue's state can reveal it.

DELIBERATELY NOT IN `npm run gates`. The offline runner must stay usable on a
plane, in a fresh clone, behind a proxy; a gate that needs GitHub is a gate that
fails for reasons having nothing to do with the code. This is a separate command
for CI (or a manual run), and the offline staleness check in denylist-check.py
covers the network-free half of the same question.

FAILS CLOSED. If GitHub cannot be reached, this exits non-zero rather than
reporting success, because "I could not check" must never render as "checked,
fine" -- which is the failure mode the whole exemption problem is made of.

Stdlib only, plus the `gh` CLI for authentication. Exit 0 clean | 1 closed
issue referenced | 2 could not check. `--self-test` covers the parsing offline.

Usage: python3 data/scripts/issue-refs-check.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: Files whose suppressions cite an issue as their expiry condition. Each is
#: scanned as raw text, so a JSON "reason" and a JS comment are handled alike.
SOURCES = (
    "data/scripts/denylist-allowlist.json",
    "data/scripts/secret-scan-allowlist.json",
    "scripts/run-gates.mjs",
)

ISSUE_RE = re.compile(r"(?:issues?\s*)?#(\d{1,6})\b")

#: A reference is only an EXPIRY claim in the right company. "see #12" is a
#: pointer; "remove when #12 lands" is a promise this file should not outlive.
EXPIRY_HINTS = (
    "remove", "removes", "until", "when", "lands", "fixed by", "fails at head",
    "tracked as", "out of scope for", "issue",
)


def references(text: str) -> dict[int, str]:
    """Issue number -> the line claiming it, for lines that read as expiry."""
    found: dict[int, str] = {}
    for line in text.splitlines():
        lowered = line.lower()
        if not any(hint in lowered for hint in EXPIRY_HINTS):
            continue
        for match in ISSUE_RE.finditer(line):
            number = int(match.group(1))
            found.setdefault(number, line.strip()[:160])
    return found


def issue_state(number: int) -> str:
    """OPEN / CLOSED, or raise if GitHub cannot answer."""
    run = subprocess.run(
        ["gh", "issue", "view", str(number), "--json", "state"],
        capture_output=True, text=True,
    )
    if run.returncode != 0:
        raise RuntimeError(f"gh could not read issue #{number}: {run.stderr.strip()[:160]}")
    return json.loads(run.stdout)["state"]


def _self_test() -> int:
    assert references("Remove this entry when #38 lands.") == {38: "Remove this entry when #38 lands."}
    assert references("fails at HEAD: ... issue #59") .keys() == {59}
    assert list(references("tracked as issue #267, not a sweep.")) == [267]
    # Multiple on one line, both captured.
    assert set(references("fails at HEAD: locking + cwd -- issues #69, #60")) == {69, 60}
    # A bare pointer with no expiry language is not an expiry claim.
    assert references("See #12 for background colour.") == {}
    # Not an issue number.
    assert references("remove the #! shebang") == {}
    print("issue-refs-check self-test: OK")
    return 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return _self_test()

    cited: dict[int, list[str]] = {}
    scanned = 0
    for relative in SOURCES:
        path = ROOT / relative
        if not path.exists():
            continue
        scanned += 1
        for number, line in references(path.read_text(encoding="utf-8")).items():
            cited.setdefault(number, []).append(f"{relative}: {line}")

    if not cited:
        print(f"issue-refs-check: no issue-gated suppressions in {scanned} file(s)")
        return 0

    closed: list[str] = []
    try:
        states = {number: issue_state(number) for number in sorted(cited)}
    except (RuntimeError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(
            f"FAIL: could not establish issue state ({exc}).\n"
            "This gate needs GitHub and the `gh` CLI. It fails rather than "
            "reporting success, because an unchecked exemption must not read as "
            "a checked one. It is not part of `npm run gates` for this reason.",
            file=sys.stderr,
        )
        return 2

    for number, state in states.items():
        if state != "OPEN":
            for where in cited[number]:
                closed.append(f"#{number} is {state} but is still cited as the expiry condition\n      {where}")

    for item in closed:
        print(f"FAIL: {item}", file=sys.stderr)
    if closed:
        print(
            f"\n{len(closed)} suppression(s) outlived their issue. Either remove "
            "the suppression, or reopen/replace the issue it names.",
            file=sys.stderr,
        )
        return 1
    print(f"issue-refs-check: {len(states)} cited issue(s) across {scanned} file(s), all open")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
