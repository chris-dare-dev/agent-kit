from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest

import platform_skips
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = SCRIPT_DIR / "build-agent-vault.sh"


class BuildAgentVaultNoDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.vault = self.root / "vault"
        self.workspace.mkdir()
        self.vault.mkdir()
        (self.workspace / "docs/_curated").mkdir(parents=True)
        (self.workspace / "repos/repo").mkdir(parents=True)
        (self.workspace / ".claude/notes/milestones/m1").mkdir(parents=True)
        (self.vault / "AgentDocs").mkdir()
        self.policy = self.root / "policy.json"
        self.policy.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "vault_projection": {
                        "default_action": "exclude",
                        "candidate_roots": ["docs"],
                        "prune_directory_names": [".git"],
                        "exclude_rules": [
                            {
                                "id": "runtime",
                                "globs": [".claude/notes/**", "repos/**"],
                            }
                        ],
                        "allow_rules": [
                            {
                                "id": "curated",
                                "globs": [
                                    "docs/_curated/*.md",
                                    "docs/_curated/**/*.md",
                                ],
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write(self, relative: str, content: str = "# Note\n") -> Path:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def alias(self, source: Path, relative: str) -> Path:
        path = self.vault / "AgentDocs" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(os.path.relpath(source, path.parent), path)
        return path

    def run_builder(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment.update(
            {
                "PERSONAL_WORKSPACE_ROOT": str(self.workspace),
                "PERSONAL_VAULT_ROOT": str(self.vault),
            }
        )
        return subprocess.run(
            [
                # Resolved, not hardcoded: Git for Windows ships bash and this
                # script runs there unchanged, so /bin/bash was excluding a
                # platform that can in fact run the test.
                platform_skips.BASH or "/bin/bash",
                str(BUILD_SCRIPT),
                "--policy",
                str(self.policy),
                *arguments,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def snapshot(self, *paths: Path) -> dict[Path, tuple[str, int]]:
        return {
            path: (os.readlink(path), path.lstat().st_mtime_ns)
            for path in paths
        }

    @platform_skips.requires_symlinks
    def test_default_run_creates_allowed_and_preserves_every_existing_alias(self) -> None:
        curated = self.write("docs/_curated/architecture.md")
        excluded_new = self.write("repos/repo/README.md")
        runtime = self.write(".claude/notes/milestones/m1/report.md")
        existing_excluded = self.alias(runtime, "claude/notes/milestones/m1/report.md")
        wrong_target_source = self.write("docs/wrong.md")
        wrong_target = self.alias(
            wrong_target_source, "docs/_curated/architecture.md"
        )
        broken = self.vault / "AgentDocs/repos/repo/missing.md"
        broken.parent.mkdir(parents=True, exist_ok=True)
        os.symlink("../../../../workspace/repos/repo/missing.md", broken)
        before = self.snapshot(existing_excluded, wrong_target, broken)

        result = self.run_builder()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(
            (self.vault / "AgentDocs/repos/repo/README.md").is_symlink()
        )
        self.assertEqual(before, self.snapshot(existing_excluded, wrong_target, broken))
        self.assertNotEqual(wrong_target.resolve(), curated.resolve())
        self.assertTrue(excluded_new.exists())
        self.assertIn("0 deleted", result.stdout)

    @platform_skips.requires_symlinks
    def test_default_run_is_idempotent_for_new_allowed_alias(self) -> None:
        self.write("docs/_curated/architecture.md")
        first = self.run_builder()
        alias = self.vault / "AgentDocs/docs/_curated/architecture.md"
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(alias.is_symlink())
        before = self.snapshot(alias)

        second = self.run_builder()

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(before, self.snapshot(alias))
        self.assertIn("0 linked", second.stdout)

    @platform_skips.requires_symlinks
    def test_audit_is_read_only(self) -> None:
        runtime = self.write(".claude/notes/milestones/m1/report.md")
        existing = self.alias(runtime, "claude/notes/milestones/m1/report.md")
        before = self.snapshot(existing)

        result = self.run_builder("--audit-policy")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"], "read-only-audit")
        self.assertEqual(before, self.snapshot(existing))

    def test_legacy_top_level_projection_is_frozen(self) -> None:
        handoff = self.write("HANDOFF-new-session.md")

        result = self.run_builder()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(handoff.exists())
        self.assertFalse((self.vault / handoff.name).exists())
        self.assertIn("0 linked", result.stdout)

    @platform_skips.requires_symlinks
    def test_symlinked_destination_parent_cannot_escape_vault(self) -> None:
        self.write("docs/_curated/architecture.md")
        outside = self.root / "outside"
        outside.mkdir()
        os.symlink(outside, self.vault / "AgentDocs/docs")

        result = self.run_builder()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("destination parent is not a real directory", result.stderr)
        self.assertFalse((outside / "_curated/architecture.md").exists())
        self.assertEqual(
            os.readlink(self.vault / "AgentDocs/docs"),
            str(outside),
        )

    def test_invalid_policy_causes_zero_vault_mutation(self) -> None:
        sentinel = self.vault / "sentinel.md"
        sentinel.write_text("keep\n", encoding="utf-8")
        before = sentinel.stat().st_mtime_ns
        self.policy.write_text("{ invalid", encoding="utf-8")

        result = self.run_builder()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")
        self.assertEqual(sentinel.stat().st_mtime_ns, before)
        self.assertEqual(list((self.vault / "AgentDocs").iterdir()), [])

    def test_audit_output_is_external_and_never_replaced(self) -> None:
        report = self.root / "report.json"
        first = self.run_builder("--audit-policy", "--output", str(report))
        self.assertEqual(first.returncode, 0, first.stderr)
        original = report.read_bytes()

        second = self.run_builder("--audit-policy", "--output", str(report))
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(report.read_bytes(), original)
        self.assertIn("refusing to replace", second.stderr)

        inside = self.workspace / "report.json"
        rejected = self.run_builder("--audit-policy", "--output", str(inside))
        self.assertNotEqual(rejected.returncode, 0)
        self.assertFalse(inside.exists())
        self.assertIn("outside the source workspace", rejected.stderr)

    def test_output_without_audit_is_rejected(self) -> None:
        report = self.root / "report.json"

        result = self.run_builder("--output", str(report))

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(report.exists())
        self.assertIn("--output requires --audit-policy", result.stderr)


if __name__ == "__main__":
    unittest.main()
