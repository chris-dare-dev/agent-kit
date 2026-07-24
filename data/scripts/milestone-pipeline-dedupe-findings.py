#!/usr/bin/env python3
"""DEPRECATION STUB — dedupe moved into milestone-pipeline-findings.py (2026-07-09).

This filename is retained only so in-flight sessions that loaded the pre-uplift
command/agent bodies (restart-to-reload: edited bodies reach live sessions only
after session restart) keep working. It delegates to:

    python3 milestone-pipeline-findings.py dedupe <critique.md>

which runs the SAME shared parser as extract/--check — fail-loud on malformed
finding blocks (the old script's silent `continue` skip is retired; a critique
that drifts from format v1.0 now blocks instead of losing findings invisibly).

New callers: use `milestone-pipeline-findings.py dedupe` directly.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    target = Path(__file__).resolve().parent / "milestone-pipeline-findings.py"
    print(
        "NOTE: milestone-pipeline-dedupe-findings.py is a deprecation stub — "
        f"delegating to {target.name} dedupe",
        file=sys.stderr,
    )
    return subprocess.call([sys.executable, str(target), "dedupe", argv[1]])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
