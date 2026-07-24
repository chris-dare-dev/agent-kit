"""F-10 mirror lint: every workspace-tooling/*.py must be byte-identical to its
Work/workspace/scripts/*.py twin (top-level + tests/), with no file in one and not the
other.

Why this exists: the live launchd agents and the runtime import the
Work/workspace/scripts/ copy, while repo edits happen in workspace-tooling/. F-10
keeps the two byte-identical until the phase-2 cutover, and divergence is SILENT
-- there is no VCS on scripts/ to catch it. m1, m2, AND fix-2 each mirrored their
*source* files but left the *test* files (and lock_check.py) behind; nothing
noticed until a manual audit. This lint turns that silent class loud: it fails on
any content drift OR fileset asymmetry across the whole *.py set. The narrower
test_offdevice_mirror_is_byte_identical (one source file) is subsumed by this.

It is a WORKSPACE-LOCAL invariant, not a repo-portable one: the mirror only exists
on a mirroring engineer's machine. So it SKIPS cleanly wherever the layout is
absent (CI, the sandbox/devcontainer, engineers who do not mirror). Both canonical
directories are derived from the home layout so the check is identical whichever
copy (workspace-tooling/tests or scripts/tests) is executing it. It ALSO skips when
executed from a non-canonical checkout (a git worktree, which has no scripts/ twin):
otherwise the hardcoded home paths would pass GREEN while the worktree's own edits
went unchecked -- run F-10 from the home clone, or the locked release preflight.
"""

from __future__ import annotations

import unittest
from pathlib import Path

_WORKSPACE = Path.home() / "Work" / "workspace"
WORKSPACE_TOOLING = (
    _WORKSPACE
    / "GitLab"
    / "SWAT DevOps - SDO"
    / "platform"
    / "tools"
    / "claude-mcp-server"
    / "workspace-tooling"
)
SCRIPTS = _WORKSPACE / "scripts"

# The checkout this test file is EXECUTING from (its own workspace-tooling/ or
# scripts/ parent). The F-10 mirror is a home-layout invariant between the
# canonical home clone and ~/Work/workspace/scripts; a throwaway `git worktree` has no
# scripts/ twin, so from one the hardcoded home paths above would compare the
# canonical clone against the home scripts/ and pass GREEN while the engineer's OWN
# worktree edits went unchecked -- a false all-clear. See the setUp guard.
_EXECUTING_ROOT = Path(__file__).resolve().parents[1]
_CANONICAL_ROOTS = (WORKSPACE_TOOLING, SCRIPTS)


def _is_canonical_checkout(executing_root: Path, canonical_roots: tuple[Path, ...]) -> bool:
    """True iff the test is running from one of the canonical mirror roots.

    False for a `git worktree` or other clone -- which has no scripts/ twin, so the
    home-layout invariant does not apply there and the test must SKIP rather than
    compare the canonical home clone and report a misleading GREEN about edits the
    worktree never mirrored.
    """
    return executing_root in canonical_roots


def _rel_py(root: Path) -> set[str]:
    """Every mirrored *.py under root: top-level plus the tests/ dir one level down.

    Matches how the mirror is structured (flat top-level modules + a tests/ dir);
    deeper trees are intentionally out of scope.
    """
    found = {path.name for path in root.glob("*.py")}
    found |= {f"tests/{path.name}" for path in (root / "tests").glob("*.py")}
    return found


class F10MirrorLintTests(unittest.TestCase):
    def setUp(self) -> None:
        if not WORKSPACE_TOOLING.is_dir() or not SCRIPTS.is_dir():
            self.skipTest("F-10 mirror layout not present in this environment")
        if not _is_canonical_checkout(_EXECUTING_ROOT, _CANONICAL_ROOTS):
            self.skipTest(
                "F-10 mirror is a home-layout invariant checked from the canonical "
                f"home clone or ~/Work/workspace/scripts; executing from {_EXECUTING_ROOT} "
                "(a worktree/other checkout with no scripts/ twin) -- run F-10 from the "
                "home clone or the locked release preflight, not a worktree."
            )

    def test_same_py_fileset_both_directions(self) -> None:
        workspace = _rel_py(WORKSPACE_TOOLING)
        scripts = _rel_py(SCRIPTS)
        self.assertEqual(
            workspace,
            scripts,
            "F-10 fileset drift -- "
            f"workspace-only: {sorted(workspace - scripts)}; "
            f"scripts-only: {sorted(scripts - workspace)} -- re-sync the mirror",
        )

    def test_every_py_is_byte_identical(self) -> None:
        drift = [
            rel
            for rel in sorted(_rel_py(WORKSPACE_TOOLING) & _rel_py(SCRIPTS))
            if (WORKSPACE_TOOLING / rel).read_bytes() != (SCRIPTS / rel).read_bytes()
        ]
        self.assertEqual(
            drift, [], f"F-10 byte drift in {drift} -- re-sync workspace-tooling <-> scripts/"
        )


class F10GuardLogicTests(unittest.TestCase):
    """The worktree-safety guard runs everywhere (no layout dependency): a
    non-canonical checkout must not be treated as canonical -- that was the
    false-GREEN gap the 2026-07-20 review flagged (finding #9)."""

    def test_canonical_home_roots_are_canonical(self) -> None:
        self.assertTrue(_is_canonical_checkout(WORKSPACE_TOOLING, _CANONICAL_ROOTS))
        self.assertTrue(_is_canonical_checkout(SCRIPTS, _CANONICAL_ROOTS))

    def test_worktree_or_other_checkout_is_not_canonical(self) -> None:
        worktree = Path(
            "/tmp/workspace-worktree-abc123/platform/tools/claude-mcp-server/workspace-tooling"
        )
        self.assertFalse(_is_canonical_checkout(worktree, _CANONICAL_ROOTS))
        self.assertFalse(
            _is_canonical_checkout(Path("/some/other/clone/workspace-tooling"), _CANONICAL_ROOTS)
        )


if __name__ == "__main__":
    unittest.main()
