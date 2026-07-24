from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import agentdocs_alias_dedupe as dedupe  # noqa: E402


class AgentDocsAliasDedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.vault = self.root / "Presentation"
        self.workspace.mkdir()
        self.vault.mkdir()
        self.config = dedupe.Config(
            workspace=self.workspace,
            vault=self.vault,
            projects_root=self.vault / "Notes/Projects",
            source_alias_dir="_sources",
            farm=self.vault / "AgentDocs",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write(self, root: Path, relative: str, content: str = "# Source\n") -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def aliases(self, *, create_farm: bool = True) -> tuple[Path, Path, Path]:
        source = self.write(self.workspace, "GitLab/repo/plans/alpha.md")
        canonical = self.vault / "Notes/Projects/Alpha/_sources/plans/alpha.md"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(source, canonical)
        farm = self.vault / "AgentDocs/GitLab/repo/plans/alpha.md"
        if create_farm:
            farm.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(source, farm)
        return source, canonical, farm

    def test_dry_run_proves_safe_duplicate_without_mutating(self) -> None:
        _source, canonical, farm = self.aliases()

        report = dedupe.build_report(self.config)

        self.assertEqual(len(report.candidates), 1)
        self.assertTrue(report.candidates[0].safe)
        self.assertTrue(farm.is_symlink())
        self.assertTrue(canonical.is_symlink())
        self.assertEqual(report.as_dict()["summary"]["safe_existing_removals"], 1)

    def test_indexed_reference_blocks_removal(self) -> None:
        _source, _canonical, farm = self.aliases()
        self.write(
            self.vault,
            "Notes/Dashboard.md",
            "[legacy](../agentdocs/GitLab/repo/plans/alpha.md)\n",
        )

        report = dedupe.build_report(self.config)
        candidate = report.candidates[0]

        self.assertFalse(candidate.safe)
        self.assertIn("indexed_reference", candidate.blockers)
        self.assertEqual(candidate.indexed_references, {"Notes/Dashboard.md"})
        dedupe.apply_safe_removals(report)
        self.assertTrue(farm.is_symlink())

    def test_workspace_state_reference_blocks_removal(self) -> None:
        _source, _canonical, farm = self.aliases()
        self.write(
            self.vault,
            ".obsidian/workspace.json",
            '{"recentFiles":["AgentDocs/GitLab/repo/plans/alpha.md"]}\n',
        )

        report = dedupe.build_report(self.config)
        candidate = report.candidates[0]

        self.assertFalse(candidate.safe)
        self.assertIn("workspace_state_reference", candidate.blockers)
        self.assertEqual(candidate.state_references, {".obsidian/workspace.json"})
        dedupe.apply_safe_removals(report)
        self.assertTrue(farm.is_symlink())

    def test_apply_unlinks_only_farm_alias_and_preserves_canonical(self) -> None:
        source, canonical, farm = self.aliases()
        report = dedupe.build_report(self.config, mode="apply")

        dedupe.apply_safe_removals(report)

        self.assertFalse(farm.is_symlink())
        self.assertTrue(canonical.is_symlink())
        self.assertEqual(canonical.resolve(strict=True), source.resolve(strict=True))
        self.assertTrue(report.candidates[0].removed)

    def test_build_filter_suppresses_absent_duplicate_but_not_user_file(self) -> None:
        source, _canonical, farm = self.aliases(create_farm=False)
        plan = self.root / "farm-plan.tsv"
        plan.write_text(f"{source}\t{farm}\n", encoding="utf-8")

        report = dedupe.build_report(self.config, mode="build-filter", farm_plan=plan)

        self.assertEqual(len(report.safe_candidates), 1)
        self.assertFalse(report.safe_candidates[0].exists)

        farm.parent.mkdir(parents=True, exist_ok=True)
        farm.write_text("user-owned\n", encoding="utf-8")
        blocked = dedupe.build_report(self.config, mode="build-filter", farm_plan=plan)
        self.assertEqual(len(blocked.safe_candidates), 0)
        self.assertIn("alias_not_owned_symlink", blocked.candidates[0].blockers)


if __name__ == "__main__":
    unittest.main()
