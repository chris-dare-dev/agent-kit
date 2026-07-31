#!/usr/bin/env python3
"""Smoke test for agent-memory-consolidate.sh + agent-memory-root.sh — the S2.2 acceptance gate
of self-improving-tooling-m2 (E2: agent-memory-root consolidation).

Stdlib only (unittest, os, subprocess, tempfile, tarfile, pathlib). Run:
    python3 data/scripts/agent-memory-consolidate-test.py

It builds a SYNTHETIC fixture farm in a tempdir — it NEVER touches the user's live
`.claude/agent-memory` roots (~40+, count drifts as pipelines touch new repos). Naming follows
the M1 sibling `pipeline-outcome-log-test.py`.

The S2.2 gate has three legs (all proven here on the fixture, not assumed):
  Test 1  INODE-EQUALITY + NO-LOSS + DEDUP + BACKUP + IDEMPOTENCY
          - every fixture legacy root resolves to the SAME unified inode (symlink-farm)
          - the union of all pre-migration distinct lines is a SUBSET of the merged corpus
            (the 18-disjoint-copies case: NEVER drop a lesson)
          - a deliberately-shared line appears exactly once after merge (dedup works)
          - a backup tarball exists per migrated root and round-trips
          - a second --apply run is a no-op (idempotent, still symlinks, no double-merge)
  Test 2  DELIBERATELY-MISSED-PATH FALLBACK (orphan-prevention TESTED, not assumed)
          - a legacy cwd-root that escaped the cutover still resolves to its own lessons via
            agent-memory-root.sh's new-first/legacy-second order (fallback branch fired)
          - with $AGENT_MEMORY_ROOT set, the same call returns the unified path (happy path)
  Test 3  EMPTY-STUB ROOT — an agent dir with a 0-line lessons.md migrates without error and
          still becomes a symlink (the live sub-dir `lessons=0` noise is migrate-safe)
"""

from __future__ import annotations

import os
import re
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONSOLIDATE = HERE / "agent-memory-consolidate.sh"
RESOLVER = HERE / "agent-memory-root.sh"


def _distinct_nonblank_lines(text: str) -> set[str]:
    return {ln.rstrip() for ln in text.splitlines() if ln.strip()}


def _summary_counts(stdout: str) -> dict[str, int]:
    """Parse the `summary: discovered=N migrated=N already-symlinked=N skipped=N` line."""
    m = re.search(
        r"summary:\s*discovered=(\d+)\s+migrated=(\d+)\s+"
        r"already-symlinked=(\d+)\s+skipped=(\d+)",
        stdout,
    )
    assert m, f"summary line not found in stdout:\n{stdout}"
    return {
        "discovered": int(m.group(1)),
        "migrated": int(m.group(2)),
        "already-symlinked": int(m.group(3)),
        "skipped": int(m.group(4)),
    }


def _run_consolidate(
    workspace: Path, unified: Path, backup: Path, apply: bool, quiet: bool = True
):
    args = [
        "bash",
        str(CONSOLIDATE),
        "--root",
        str(unified),
        "--workspace",
        str(workspace),
        "--backup-dir",
        str(backup),
    ]
    if quiet:
        args.append("--quiet")
    if apply:
        args.append("--apply")
    # Run with a clean env (no inherited AGENT_MEMORY_ROOT/WORKSPACE_ROOT leaking in).
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("AGENT_MEMORY_ROOT", "WORKSPACE_ROOT")
    }
    return subprocess.run(args, capture_output=True, text=True, env=env)


def _resolve(agent: str, legacy: str, env_overrides: dict) -> str:
    """Invoke agent-memory-root.sh as a CLI and return its stdout (the resolved dir)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("AGENT_MEMORY_ROOT", "WORKSPACE_ROOT")
    }
    env.update(env_overrides)
    res = subprocess.run(
        ["bash", str(RESOLVER), agent, legacy],
        capture_output=True,
        text=True,
        env=env,
    )
    return res.stdout.strip()


class TestConsolidate(unittest.TestCase):
    def _build_farm(self, ws: Path):
        """Create a synthetic workspace: a unified root + several nested legacy roots, each
        with DIVERGENT per-agent lessons (distinct lines per copy + one shared line)."""
        unified = ws / ".claude" / "agent-memory"
        unified.mkdir(parents=True)
        # Seed the unified root with a pre-existing partial corpus (mimics the richest root).
        (unified / "milestone-researcher").mkdir()
        (unified / "milestone-researcher" / "lessons.md").write_text(
            "# milestone-researcher lessons\nSHARED-LINE common to every copy\n"
            "unified-seed lesson A\n"
        , encoding="utf-8")

        # Several legacy roots at nested paths, mimicking charts/velero, source/foo, plans, poc.
        legacy_specs = {
            "charts/velero": {
                "milestone-researcher": "SHARED-LINE common to every copy\nvelero-only lesson X\n",
                "milestone-adversary": "velero adversary lesson 1\n",
            },
            "source/foo": {
                "milestone-researcher": "SHARED-LINE common to every copy\nfoo-only lesson Y\nfoo-only lesson Z\n",
            },
            "infra/bar/l3-compute": {
                "milestone-adversary": "SHARED-LINE common to every copy\nbar adversary lesson 2\n",
            },
            "plans": {
                "roadmap-materializer": "plans roadmap lesson\n",
            },
        }
        legacy_roots = []
        for relpath, agents in legacy_specs.items():
            root = ws / relpath / ".claude" / "agent-memory"
            for agent, lessons in agents.items():
                d = root / agent
                d.mkdir(parents=True)
                (d / "lessons.md").write_text(lessons, encoding="utf-8")
            legacy_roots.append(root)
        return unified, legacy_roots, legacy_specs

    def test_1_farm_inode_noloss_dedup_backup_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            unified, legacy_roots, specs = self._build_farm(ws)
            backup = ws / "_backups"

            # Capture ALL pre-migration distinct lines per agent (incl. the unified seed).
            pre_lines: dict[str, set[str]] = {}
            for adir in unified.iterdir():
                f = adir / "lessons.md"
                if f.is_file():
                    pre_lines.setdefault(adir.name, set()).update(
                        _distinct_nonblank_lines(f.read_text(encoding="utf-8"))
                    )
            for root in legacy_roots:
                for adir in root.iterdir():
                    f = adir / "lessons.md"
                    if f.is_file():
                        pre_lines.setdefault(adir.name, set()).update(
                            _distinct_nonblank_lines(f.read_text(encoding="utf-8"))
                        )

            # Dry-run first: must touch nothing.
            res = _run_consolidate(ws, unified, backup, apply=False)
            self.assertEqual(res.returncode, 0, res.stderr)
            for root in legacy_roots:
                self.assertFalse(root.is_symlink(), "dry-run must not symlink")
            self.assertFalse(backup.exists(), "dry-run must not create backups")

            # Apply.
            res = _run_consolidate(ws, unified, backup, apply=True)
            self.assertEqual(res.returncode, 0, res.stderr)

            # (a) INODE-EQUALITY across the symlink-farm.
            for root in legacy_roots:
                self.assertTrue(
                    root.is_symlink(), f"{root} should be a symlink after --apply"
                )
                for adir in unified.iterdir():
                    u = unified / adir.name / "lessons.md"
                    leg = root / adir.name / "lessons.md"
                    if u.is_file() and leg.exists():
                        self.assertEqual(
                            os.stat(u).st_ino,
                            os.stat(leg).st_ino,
                            f"inode mismatch: {leg} vs {u}",
                        )

            # (b) NO LESSON LOST: every pre-migration distinct line survives in the merged corpus.
            for agent, lines in pre_lines.items():
                merged = unified / agent / "lessons.md"
                self.assertTrue(merged.is_file(), f"missing merged corpus for {agent}")
                post = _distinct_nonblank_lines(merged.read_text(encoding="utf-8"))
                missing = lines - post
                self.assertEqual(
                    missing, set(), f"{agent}: lessons LOST in merge: {missing}"
                )

            # (c) DEDUP: the shared line appears exactly once in milestone-researcher's corpus.
            mr = (
                (unified / "milestone-researcher" / "lessons.md")
                .read_text(encoding="utf-8")
                .splitlines()
            )
            shared = [
                ln for ln in mr if ln.rstrip() == "SHARED-LINE common to every copy"
            ]
            self.assertEqual(
                len(shared), 1, f"shared line should be deduped to 1, got {len(shared)}"
            )

            # (d) BACKUP exists per migrated root and round-trips.
            tarballs = list(backup.glob("*.tar.gz"))
            self.assertEqual(
                len(tarballs), len(legacy_roots), "one backup tarball per migrated root"
            )
            with tarfile.open(tarballs[0], encoding="utf-8") as tf:
                names = tf.getnames()
                self.assertTrue(
                    any(n.endswith("lessons.md") for n in names),
                    "backup tarball should contain lessons.md",
                )

            # (e) IDEMPOTENCY: re-apply is a no-op — exit 0, line counts unchanged, still symlinks.
            #     Run NON-quiet so we can parse the summary and assert the counter is TRUTHFUL:
            #     the 2nd --apply must report every migrated root as `already-symlinked` (NOT
            #     silently skipped as "is unified root", which the pre-M1 skip-ordering bug did).
            counts_before = {
                a: (unified / a / "lessons.md").read_text(encoding="utf-8")
                for a in [
                    d.name
                    for d in unified.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                ]
            }
            res2 = _run_consolidate(
                ws, unified, ws / "_backups2", apply=True, quiet=False
            )
            self.assertEqual(res2.returncode, 0, res2.stderr)
            for root in legacy_roots:
                self.assertTrue(root.is_symlink(), "still a symlink after 2nd apply")
            for a, txt in counts_before.items():
                self.assertEqual(
                    (unified / a / "lessons.md").read_text(encoding="utf-8"),
                    txt,
                    f"{a}: corpus changed on idempotent re-run",
                )

            # (e2) TRUTHFUL SUMMARY (M1 regression-guard): every fixture legacy root is reported
            #      as already-symlinked, and none is re-migrated, on the 2nd --apply.
            counts2 = _summary_counts(res2.stdout)
            self.assertEqual(
                counts2["already-symlinked"],
                len(legacy_roots),
                f"2nd --apply must report all {len(legacy_roots)} migrated roots as "
                f"already-symlinked, got {counts2['already-symlinked']} "
                f"(skipped={counts2['skipped']}, migrated={counts2['migrated']}) — "
                "skip-ordering must not mask the already-symlinked counter",
            )
            self.assertEqual(
                counts2["migrated"],
                0,
                "2nd --apply must re-migrate nothing (idempotent)",
            )

    def test_2_missed_path_fallback_and_happy_path(self):
        """The deliberately-missed-path back-compat fallback (S2.2 second AC) AND the happy path."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            # A legacy cwd-root that the migration did NOT process (escaped the cutover).
            missed = ws / "escaped-repo" / ".claude" / "agent-memory"
            (missed / "milestone-researcher").mkdir(parents=True)
            (missed / "milestone-researcher" / "lessons.md").write_text(
                "escaped lesson\n"
            , encoding="utf-8")

            # FALLBACK branch: AGENT_MEMORY_ROOT unset AND WORKSPACE_ROOT points at a dir
            # with NO new root present -> resolver must return the LEGACY path, never empty,
            # never the absent new root.
            empty_ws = ws / "no-new-root"
            empty_ws.mkdir()
            resolved = _resolve(
                "milestone-researcher",
                str(missed),
                {"WORKSPACE_ROOT": str(empty_ws)},
            )
            self.assertEqual(
                resolved,
                f"{missed}/milestone-researcher",
                "fallback must land on the legacy cwd-root, not empty/absent root",
            )
            self.assertNotEqual(
                resolved, "", "resolver must never return empty (silent-loss guard)"
            )
            # The legacy lessons are physically reachable at the resolved path.
            self.assertTrue(Path(resolved, "lessons.md").is_file())

            # HAPPY path: AGENT_MEMORY_ROOT set -> same call returns the unified path.
            unified = ws / "unified-root"
            resolved2 = _resolve(
                "milestone-researcher",
                str(missed),
                {"AGENT_MEMORY_ROOT": str(unified)},
            )
            self.assertEqual(
                resolved2,
                f"{unified}/milestone-researcher",
                "override must win (new-first)",
            )

    def test_3_empty_stub_root_migrates(self):
        """An empty-stub legacy root (agent dir with a 0-line lessons.md) migrates cleanly."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            unified = ws / ".claude" / "agent-memory"
            unified.mkdir(parents=True)
            stub = ws / "stub-repo" / ".claude" / "agent-memory"
            (stub / "spike-designer").mkdir(parents=True)
            (stub / "spike-designer" / "lessons.md").write_text("", encoding="utf-8")  # 0 lines

            res = _run_consolidate(ws, unified, ws / "_b", apply=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue(
                stub.is_symlink(), "empty-stub root should still become a symlink"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
