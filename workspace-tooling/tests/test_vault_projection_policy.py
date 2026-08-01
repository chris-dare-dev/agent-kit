from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

import platform_skips
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import vault_projection_policy as projection  # noqa: E402


class VaultProjectionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.vault = self.root / "vault"
        self.workspace.mkdir()
        self.vault.mkdir()
        self.policy_path = self.root / "policy.json"
        self.policy_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "vault_projection": {
                        "default_action": "exclude",
                        "candidate_roots": ["docs"],
                        "prune_directory_names": [".aggregate", ".git"],
                        "exclude_rules": [
                            {
                                "id": "generated",
                                "globs": ["docs/generated/**"],
                            }
                        ],
                        "allow_rules": [
                            {
                                "id": "curated-docs",
                                "globs": ["docs/*.md", "docs/**/*.md"],
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        self.policy = projection.load_policy(self.policy_path)

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

    def test_exclude_rules_win_over_allow_rules(self) -> None:
        self.assertEqual(
            projection.classify("docs/generated/output.md", self.policy),
            projection.Decision("exclude", "generated"),
        )
        self.assertEqual(
            projection.classify("docs/architecture/overview.md", self.policy),
            projection.Decision("allow", "curated-docs"),
        )
        self.assertEqual(
            projection.classify("repos/repo/README.md", self.policy),
            projection.Decision("exclude", "default-exclude"),
        )

    @platform_skips.requires_symlinks
    def test_plan_scans_only_allowlisted_regular_files(self) -> None:
        wanted = self.write("docs/architecture/overview.md")
        self.write("docs/generated/output.md")
        self.write("repos/repo/README.md")
        hidden = self.write("outside/hidden.md")
        symlink = self.workspace / "docs/symlink.md"
        symlink.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(hidden, symlink)

        plan = projection.build_plan(self.workspace, self.vault, self.policy)

        self.assertEqual([entry.source.resolve() for entry in plan], [wanted.resolve()])
        self.assertEqual(
            plan[0].vault_relative,
            "AgentDocs/docs/architecture/overview.md",
        )

    @platform_skips.requires_symlinks
    def test_audit_reports_but_does_not_mutate_existing_aliases(self) -> None:
        planned = self.write("docs/current.md")
        excluded = self.write(".claude/notes/milestones/m1/report.md")
        allowed_alias = self.alias(planned, "docs/current.md")
        excluded_alias = self.alias(
            excluded, "claude/notes/milestones/m1/report.md"
        )
        broken_alias = self.vault / "AgentDocs/repos/repo/missing.md"
        broken_alias.parent.mkdir(parents=True, exist_ok=True)
        os.symlink("../../../../workspace/repos/repo/missing.md", broken_alias)
        before = {
            path: (os.readlink(path), path.lstat().st_mtime_ns)
            for path in (allowed_alias, excluded_alias, broken_alias)
        }

        report = projection.build_audit(self.workspace, self.vault, self.policy)

        self.assertEqual(report["counts"]["existing_allowed"], 1)
        self.assertEqual(report["counts"]["live_excluded_future_prune"], 1)
        self.assertEqual(report["counts"]["broken_review_only"], 1)
        after = {
            path: (os.readlink(path), path.lstat().st_mtime_ns)
            for path in (allowed_alias, excluded_alias, broken_alias)
        }
        self.assertEqual(before, after)

    def test_audit_preserves_and_reports_real_destination_collision(self) -> None:
        self.write("docs/current.md")
        collision = self.vault / "AgentDocs/docs/current.md"
        collision.parent.mkdir(parents=True, exist_ok=True)
        collision.write_text("user owned\n", encoding="utf-8")

        report = projection.build_audit(self.workspace, self.vault, self.policy)

        self.assertEqual(report["counts"]["destination_collision"], 1)
        self.assertEqual(collision.read_text(encoding="utf-8"), "user owned\n")

    def test_policy_rejects_parent_traversal(self) -> None:
        raw = json.loads(self.policy_path.read_text(encoding="utf-8"))
        raw["vault_projection"]["candidate_roots"] = ["../escape"]
        self.policy_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(projection.PolicyError, "normalized"):
            projection.load_policy(self.policy_path)


if __name__ == "__main__":
    unittest.main()
