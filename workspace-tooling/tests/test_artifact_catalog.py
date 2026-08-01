from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
import stat
import sys
import tempfile
import unittest

import platform_skips
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import artifact_catalog as catalog  # noqa: E402


class ArtifactCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.output = self.root / "derived"
        self.workspace.mkdir()
        for required in ("plans", ".claude", "docs", "repos"):
            (self.workspace / required).mkdir()
        self.policy = self.root / "policy.json"
        self.policy.write_text(json.dumps({"schema_version": 1, "catalog": {
            "canonical_roots": ["plans", ".claude", "docs", "repos"],
            "top_level_globs": ["*.md"],
            "include_path_globs": [
                "plans/**",
                ".claude/notes/**",
                ".claude/agent-memory/**",
                "docs/**",
                "repos/**/plans/**",
                "repos/**/.claude/notes/**",
                "repos/**/.claude/agent-memory/**",
                "repos/**/docs/**",
            ],
            "exclude_roots": ["Vault", "Notes", ".worktrees"],
            "prune_directory_names": [".aggregate", ".git", ".venv", "__pycache__", "node_modules"],
            "prune_path_globs": ["repos/**/apps/ai-studio/**", "**/.aggregate/**", "**/*run.failed*/**", "**/cue.mod/pkg/**", "**/vendor/**"],
        }}), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, relative: str, content: str = "# note\n") -> Path:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    @platform_skips.requires_posix_modes
    def test_catalogs_classifies_and_reports_duplicates(self) -> None:
        roadmap = self.write("plans/alpha-roadmap.md", "# Alpha\n")
        handoff = self.write("plans/handoff-continuation.md", "status: requested\n")
        self.write("docs/architecture/adr-001.md", "# decision\n")
        self.write("repos/thing/docs/README.md", "# Alpha\n")
        self.write("Vault/AgentDocs/nope.md")
        self.write("repos/thing/vendor/nope.md")
        summary = catalog.run_catalog(self.workspace, self.output, self.policy, dry_run=False)

        self.assertEqual(summary["counts"]["artifacts"], 4)
        self.assertEqual(summary["counts"]["duplicate_content_groups"], 1)
        self.assertTrue((self.output / "artifact-catalog.sqlite3").exists())
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE(
                (self.output / "artifact-catalog.sqlite3").stat().st_mode
            ),
            0o600,
        )
        self.assertEqual(
            stat.S_IMODE(
                (self.output / "artifact-catalog-summary.json").stat().st_mode
            ),
            0o600,
        )
        self.assertEqual(roadmap.read_text(), "# Alpha\n")
        self.assertEqual(handoff.read_text(), "status: requested\n")
        with closing(sqlite3.connect(self.output / "artifact-catalog.sqlite3")) as connection, connection:
            rows = connection.execute("SELECT artifact_type, authority_class, lifecycle_hints_json FROM artifact_revisions ORDER BY relative_path").fetchall()
            duplicate_count = connection.execute("SELECT COUNT(*) FROM duplicate_content").fetchone()[0]
        self.assertEqual(duplicate_count, 1)
        self.assertIn(("roadmap", "working", '["active"]'), rows)
        self.assertIn(("handoff", "working", '["review_open"]'), rows)
        self.assertIn(("decision", "canonical", '["active"]'), rows)

    def test_ids_are_stable_and_new_content_creates_a_revision(self) -> None:
        target = self.write("plans/item-roadmap.md", "one\n")
        first = catalog.run_catalog(self.workspace, self.output, self.policy, dry_run=False)
        target.write_text("two\n", encoding="utf-8")
        second = catalog.run_catalog(self.workspace, self.output, self.policy, dry_run=False)
        self.assertNotEqual(first["catalog"]["run_id"], second["catalog"]["run_id"])
        with closing(sqlite3.connect(self.output / "artifact-catalog.sqlite3")) as connection, connection:
            ids = connection.execute("SELECT DISTINCT artifact_id FROM artifact_revisions").fetchall()
            revisions = connection.execute("SELECT COUNT(*) FROM artifact_revisions").fetchone()[0]
            runs = connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
        self.assertEqual(len(ids), 1)
        self.assertEqual(revisions, 2)
        self.assertEqual(runs, 2)

    def test_dry_run_creates_no_derived_output(self) -> None:
        self.write("plans/a.md")
        summary = catalog.run_catalog(self.workspace, self.output, self.policy, dry_run=True)
        self.assertEqual(summary["mode"], "dry-run")
        self.assertFalse(self.output.exists())

    def test_output_anywhere_inside_workspace_is_rejected(self) -> None:
        self.write("plans/a.md")
        with self.assertRaisesRegex(ValueError, "outside the source workspace"):
            catalog.run_catalog(self.workspace, self.workspace / "plans" / "derived", self.policy, dry_run=False)

    def test_default_deny_and_generated_pruning(self) -> None:
        self.write("plans/wanted.md")
        self.write("repos/a/arbitrary.yaml")
        self.write("repos/a/docs/wanted.md")
        self.write("repos/a/.aggregate/run.failed/docs/generated.md")
        self.write("Vault/AgentDocs/alias.md")
        self.write("Notes/user-note.md")
        self.write("repos/a/deps/dependency.md")
        self.write("repos/a/cue.mod/pkg/package.md")
        self.write("repos/a/apps/ai-studio/no-touch.md")
        summary = catalog.run_catalog(self.workspace, self.output, self.policy, dry_run=True)
        self.assertEqual(summary["counts"]["artifacts"], 2)
        self.assertEqual(summary["counts"]["outside_artifact_scope"], 1)

    @platform_skips.requires_symlinks
    def test_symlink_root_and_overlapping_roots_are_rejected(self) -> None:
        external = self.root / "external"
        external.mkdir()
        (self.workspace / "plans").rmdir()
        os.symlink(external, self.workspace / "plans")
        with self.assertRaisesRegex(catalog.PolicyError, "must not be a symlink"):
            catalog.run_catalog(self.workspace, self.output, self.policy, dry_run=True)

        (self.workspace / "plans").unlink()
        (self.workspace / "plans").mkdir()
        raw = json.loads(self.policy.read_text(encoding="utf-8"))
        raw["catalog"]["canonical_roots"] = [".", "plans"]
        self.policy.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(catalog.PolicyError, "overlap"):
            catalog.run_catalog(self.workspace, self.output, self.policy, dry_run=True)

    def test_duplicate_view_is_current_run_not_history(self) -> None:
        left = self.write("plans/left.md", "same\n")
        right = self.write("plans/right.md", "same\n")
        catalog.run_catalog(self.workspace, self.output, self.policy, dry_run=False)
        right.write_text("different\n", encoding="utf-8")
        catalog.run_catalog(self.workspace, self.output, self.policy, dry_run=False)
        self.assertEqual(left.read_text(), "same\n")
        with closing(sqlite3.connect(self.output / "artifact-catalog.sqlite3")) as connection, connection:
            current = connection.execute("SELECT COUNT(*) FROM duplicate_content").fetchone()[0]
            history = connection.execute("SELECT COUNT(*) FROM duplicate_content_history").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(current, 0)
        self.assertEqual(history, 1)
        self.assertEqual(version, catalog.SCHEMA_VERSION)

    def test_failed_generation_preserves_prior_authoritative_current(self) -> None:
        target = self.write("plans/current.md", "version one\n")
        first = catalog.run_catalog(
            self.workspace, self.output, self.policy, dry_run=False
        )
        first_run = first["catalog"]["run_id"]
        first_hash = catalog._read_file_snapshot(target)[0]

        target.write_text("version two\n", encoding="utf-8")
        (self.workspace / "docs").rmdir()
        failed = catalog.run_catalog(
            self.workspace, self.output, self.policy, dry_run=False
        )
        self.assertEqual(failed["generation"]["status"], "failed")
        self.assertEqual(failed["catalog"]["authoritative_run_id"], first_run)
        with closing(sqlite3.connect(
            self.output / "artifact-catalog.sqlite3"
        )) as connection, connection:
            latest_status = connection.execute(
                "SELECT status FROM scan_runs ORDER BY run_id DESC LIMIT 1"
            ).fetchone()[0]
            current_hash = connection.execute(
                "SELECT content_sha256 FROM current_artifact_revisions "
                "WHERE relative_path='plans/current.md'"
            ).fetchone()[0]
        self.assertEqual(latest_status, "failed")
        self.assertEqual(current_hash, first_hash)

    def test_exhausted_changed_file_retry_is_degraded_and_not_current(self) -> None:
        target = self.write("plans/current.md", "stable\n")
        first = catalog.run_catalog(
            self.workspace, self.output, self.policy, dry_run=False
        )
        original = catalog._read_file_snapshot

        def unstable(path: Path, hint_limit: int = 128 * 1024):
            if path.resolve() == target.resolve():
                raise catalog.ChangedDuringScan(str(path))
            return original(path, hint_limit)

        with mock.patch.object(catalog, "_read_file_snapshot", side_effect=unstable):
            degraded = catalog.run_catalog(
                self.workspace, self.output, self.policy, dry_run=False
            )
        self.assertEqual(degraded["generation"]["status"], "degraded")
        self.assertEqual(degraded["counts"]["changed_during_scan"], 1)
        self.assertEqual(degraded["counts"]["read_retries"], 2)
        self.assertEqual(
            degraded["catalog"]["authoritative_run_id"],
            first["catalog"]["run_id"],
        )


if __name__ == "__main__":
    unittest.main()
