#!/usr/bin/env python3
"""Drift tripwire between the top-level requirements and the resolved lock (F-10).

The lock is a `pip freeze` of the proven virtualenv, so nothing structurally ties
it back to the two top-level pins it was resolved from. Without this check a
bump to requirements-artifact-ingestion.txt (say qdrant-client 1.16.1 -> 1.17.0)
leaves a stale lock that still installs 1.16.1 — the pipeline stays green while
the lock quietly stops describing what the substrate actually runs. That is the
exact silent-drift class F-10 was raised about, reintroduced one layer up.

Exit codes: 0 consistent | 2 drift | 3 malformed input | 4 file missing.

Self-test: python3 lock_check.py --self-test
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOP_LEVEL = HERE / "requirements-artifact-ingestion.txt"
LOCK = HERE / "requirements-artifact-ingestion.lock.txt"

# `name[extra1,extra2]==version` — extras are declared on the top-level pins
# (qdrant-client[fastembed]) but never appear in pip freeze output, so they are
# captured and discarded rather than compared.
_REQ = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*==\s*(?P<version>[^\s;#]+)")


def _normalize(name: str) -> str:
    """PEP 503 normalization: FalkorDB, falkordb and falkor_db are one project."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse(path: Path) -> dict[str, str]:
    if not path.is_file():
        print(f"lock-check: missing {path}", file=sys.stderr)
        raise SystemExit(4)
    pins: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        match = _REQ.match(line)
        if not match:
            print(f"lock-check: {path.name}:{lineno}: unparseable pin: {raw!r}", file=sys.stderr)
            raise SystemExit(3)
        pins[_normalize(match.group("name"))] = match.group("version")
    return pins


def compare(top: dict[str, str], lock: dict[str, str]) -> list[str]:
    problems = []
    for name, want in sorted(top.items()):
        got = lock.get(name)
        if got is None:
            problems.append(f"  {name}=={want} is pinned top-level but ABSENT from the lock")
        elif got != want:
            problems.append(f"  {name}: top-level pins =={want}, lock has =={got}")
    return problems


def _self_test() -> int:
    # Case folding and separator runs collapse; note PEP 503 does NOT make
    # "falkor_db" equal to "FalkorDB" (-> falkor-db vs falkordb) — they are
    # distinct project names, and conflating them would mask a real drift.
    assert _normalize("FalkorDB") == _normalize("falkordb") == "falkordb"
    assert _normalize("python_dateutil") == _normalize("python.dateutil") == "python-dateutil"
    assert _REQ.match("qdrant-client[fastembed]==1.16.1").group("version") == "1.16.1"
    assert _REQ.match("numpy==2.5.1").group("name") == "numpy"
    assert compare({"a": "1"}, {"a": "1"}) == []
    assert len(compare({"a": "1"}, {"a": "2"})) == 1
    assert len(compare({"a": "1"}, {})) == 1
    # An extra transitive package in the lock is expected, never a failure.
    assert compare({"a": "1"}, {"a": "1", "b": "9"}) == []
    print("lock-check: self-test OK")
    return 0


def main() -> int:
    if "--self-test" in sys.argv[1:]:
        return _self_test()
    problems = compare(parse(TOP_LEVEL), parse(LOCK))
    if problems:
        print("lock-check: the lock no longer matches the top-level pins:", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        print(
            "\nRegenerate it from a virtualenv built off the new top-level pins:\n"
            "  python -m pip install -r requirements-artifact-ingestion.txt\n"
            "  python -m pip freeze  # -> requirements-artifact-ingestion.lock.txt body",
            file=sys.stderr,
        )
        return 2
    print(f"lock-check: OK — {len(parse(TOP_LEVEL))} top-level pins consistent "
          f"with a {len(parse(LOCK))}-package lock")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
