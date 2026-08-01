#!/usr/bin/env python3
"""Run the artifact-memory substrate suite and report honestly on every platform.

History, because the previous version of this file is the reason it exists: the
substrate used to import ``fcntl`` and ``os.geteuid`` at module scope, so on
Windows it could not be imported at all and the documented ``unittest discover``
command produced a wall of ModuleNotFoundError and AttributeError. This entry
point printed a banner and returned 0 instead.

That banner is now WRONG on both counts. M2 routed those imports through
``platform_compat``, and the suite collects on Windows -- 260 tests before,
646 after. And returning 0 for a suite that ran nothing is the skip-to-green
pattern the same milestone exists to remove, however loudly the banner said
"NOT RUN".

So this runs the suite everywhere and reports what actually happened, against a
recorded per-platform baseline. Windows is still KNOWN RED pending the remaining
work in M2/#70; the header says so and the exit code still tells the truth,
because a known-red platform that reports green is how a real regression hides.

Exit codes:
    0  the suite ran and passed
    1  the suite ran and something failed or errored

What still does NOT run on Windows is the resident service (AF_UNIX) -- see
``docs/platforms/windows-wsl.md`` and the support matrix in the root README.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent / "tests"

#: Measured residue per platform, so "did I break something" is answerable
#: without a clean tree to compare against. Update these when an issue closes.
#:
#: Do NOT read a matching count as a pass -- it is a match against a known-bad
#: baseline. The exit code stays non-zero whenever anything failed, because a
#: known-red platform reporting green is how a real regression hides.
BASELINES = {
    "linux": {
        "failures": 1,
        "errors": 30,
        "note": (
            "without the provisioned venv. ~30 errors are cases that import "
            "qdrant_client at call time; the 1 failure is "
            "test_embedder_shortfall_fails_loudly_instead_of_partial_success. "
            "With the venv the errors clear."
        ),
    },
    "darwin": {
        "failures": 1,
        "errors": 30,
        "note": "same as linux; both are POSIX and share the venv dependency.",
    },
    "win32": {
        "failures": 30,
        "errors": 129,
        "note": (
            "the suite COLLECTS here (260 -> 646 tests in M2). The teardown, "
            "mode-bit and directory-fsync defects are fixed; what remains is "
            "101 cases in three classes that should become machine-readable "
            "SKIPs (29 need the provisioned venv, 44 need symlink privilege, "
            "28 need AF_UNIX) plus 57 genuine residue. M2 issue #70."
        ),
    },
}

BASELINE_NOTE = """
--------------------------------------------------------------------------------
  Recorded baseline for {platform}: {failures} failure(s), {errors} error(s).

  {note}

  A non-zero exit that MATCHES the baseline is expected and is not evidence
  that your change broke something. A count above it is.
--------------------------------------------------------------------------------
"""


def main() -> int:
    baseline = BASELINES.get(sys.platform)
    if baseline is not None:
        sys.stderr.write(BASELINE_NOTE.format(platform=sys.platform, **baseline))

    suite = unittest.defaultTestLoader.discover(
        start_dir=str(TESTS_DIR),
        pattern="test_*.py",
        top_level_dir=str(TESTS_DIR),
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)

    failures, errors = len(result.failures), len(result.errors)
    print(
        f"substrate suite on {sys.platform}: {result.testsRun} run, "
        f"{failures} failed, {errors} errored, {len(result.skipped)} skipped",
        file=sys.stderr,
    )
    if baseline is not None:
        delta_f = failures - baseline["failures"]
        delta_e = errors - baseline["errors"]
        if delta_f > 0 or delta_e > 0:
            print(
                f"  ABOVE BASELINE by {max(delta_f, 0)} failure(s) and "
                f"{max(delta_e, 0)} error(s) -- this looks like a regression",
                file=sys.stderr,
            )
        elif delta_f < 0 or delta_e < 0:
            print(
                f"  BELOW baseline by {-min(delta_f, 0)} failure(s) and "
                f"{-min(delta_e, 0)} error(s) -- update BASELINES in this file",
                file=sys.stderr,
            )
        else:
            print("  exactly at the recorded baseline", file=sys.stderr)

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
