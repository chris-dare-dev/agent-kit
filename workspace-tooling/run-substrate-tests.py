#!/usr/bin/env python3
"""Run the artifact-memory substrate suite, or decline loudly.

The substrate is POSIX-only today: ``fcntl`` and ``os.geteuid`` are imported
unconditionally at module scope, and the memory service binds an AF_UNIX socket.
Running the documented ``unittest discover`` command on Windows therefore
collects a fraction of the suite and reports a wall of ``ModuleNotFoundError``
and ``AttributeError`` — noise that looks like the substrate is broken when the
real answer is "this platform is not supported yet".

This entry point states that in one line instead. It does NOT fix the modules;
that port is tracked separately (see the milestones named below).

Exit codes:
    0  the suite ran and passed, OR the platform is unsupported and it declined
       (the banner says NOT RUN — this is never reported as a pass)
    1  the suite ran and failed
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent / "tests"

SUPPORTED = ("linux", "darwin")
BANNER = """
================================================================================
  SUBSTRATE SUITE NOT RUN - unsupported platform: {platform}
================================================================================
  The artifact-memory substrate is POSIX-only today. It imports fcntl and
  os.geteuid at module scope and binds an AF_UNIX socket, none of which exist
  on Windows, so this suite cannot even be collected here.

  Run it on one of:
      - Linux
      - macOS
      - Windows via WSL2   (wsl -e bash -lc '...')

  Making the substrate importable off macOS is milestone M2 "Gates Green";
  native Windows transport and supervision is M5 "Native Everywhere".

  This is NOT a pass. Nothing was verified on this platform.
================================================================================
"""


def main() -> int:
    if sys.platform not in SUPPORTED:
        print(BANNER.format(platform=sys.platform), file=sys.stderr)
        return 0

    suite = unittest.defaultTestLoader.discover(
        start_dir=str(TESTS_DIR),
        pattern="test_*.py",
        top_level_dir=str(TESTS_DIR),
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
