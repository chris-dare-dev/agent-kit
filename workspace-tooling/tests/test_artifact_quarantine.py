from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import artifact_catalog as catalog  # noqa: E402
import artifact_quarantine as quarantine  # noqa: E402


class ArtifactQuarantineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        for required_root in ("plans", ".claude", "docs", "GitLab"):
            (self.workspace / required_root).mkdir()
        self.output = self.root / "derived"
        self.quarantine_root = self.root / "quarantine"
        scripts = self.workspace / "scripts"
        scripts.mkdir()
        self.policy = scripts / "artifact-policy.json"
        self.policy.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "catalog": {
                        "canonical_roots": ["plans", ".claude", "docs", "GitLab"],
                        "top_level_globs": ["*.md"],
                        "include_path_globs": [
                            "plans/**",
                            "repos/**/plans/**",
                            "docs/**",
                        ],
                        "exclude_roots": ["Vault", "Notes", ".worktrees"],
                        "prune_directory_names": [
                            ".git",
                            ".aggregate",
                            "node_modules",
                        ],
                        "prune_path_globs": ["**/.aggregate/**", "**/vendor/**"],
                    },
                    "quarantine": {
                        "candidate_path_globs": [
                            "plans/**",
                            "repos/**/plans/**",
                        ],
                        "eligible_artifact_types": [
                            "decision",
                            "handoff",
                            "plan",
                            "research",
                            "roadmap",
                        ],
                        "terminal_states": [
                            "archived",
                            "cancelled",
                            "obsolete",
                            "superseded",
                        ],
                        "review_only_states": ["closed"],
                        "minimum_age_days": 14,
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @property
    def database(self) -> Path:
        return self.output / "artifact-catalog.sqlite3"

    def write(
        self,
        relative: str,
        content: str,
        *,
        age_days: int = 20,
    ) -> Path:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        timestamp = time.time() - age_days * 86_400
        os.utime(path, (timestamp, timestamp))
        return path

    def refresh_catalog(self) -> None:
        catalog.run_catalog(self.workspace, self.output, self.policy, dry_run=False)

    def plan(self, *, limit: int | None = None) -> dict:
        return quarantine.build_plan(
            quarantine._validate_workspace(self.workspace),
            self.database,
            self.policy,
            None,
            limit,
        )

    def terminal_roadmap(
        self,
        relative: str = "plans/SUPERSEDED-old-roadmap.md",
        *,
        age_days: int = 20,
    ) -> Path:
        return self.write(
            relative,
            "---\nstatus: superseded\n---\n# Old roadmap\n\nTerminal evidence.\n",
            age_days=age_days,
        )

    def test_plan_requires_strong_terminal_evidence(self) -> None:
        eligible = self.terminal_roadmap()
        broad_hint = self.write(
            "plans/current-roadmap.md",
            "# Current roadmap\n"
            + "\n" * 45
            + "A predecessor was superseded; this document remains current.\n",
        )
        closed = self.write(
            "plans/HANDOFF-closed.md",
            "---\nreview_status: closed\n---\n# Historical handoff\n",
        )
        self.refresh_catalog()

        plan = self.plan()
        self.assertEqual(
            [item["relative_path"] for item in plan["eligible"]],
            [eligible.relative_to(self.workspace).as_posix()],
        )
        review_paths = {item["relative_path"] for item in plan["review"]}
        self.assertIn(broad_hint.relative_to(self.workspace).as_posix(), review_paths)
        self.assertIn(closed.relative_to(self.workspace).as_posix(), review_paths)
        self.assertTrue(
            all(
                "strong-terminal-evidence" not in item["reason_codes"]
                for item in plan["review"]
            )
        )

    def test_young_active_canonical_and_tracked_candidates_are_blocked(self) -> None:
        young = self.terminal_roadmap("plans/SUPERSEDED-young-roadmap.md", age_days=2)
        active = self.write(
            "plans/SUPERSEDED-active-roadmap.md",
            "---\nstatus: superseded\nreview_status: requested\n---\n"
            "# Canonical\n\nThis is the active canonical roadmap.\n",
        )
        tracked = self.terminal_roadmap("plans/SUPERSEDED-tracked-roadmap.md")
        subprocess.run(
            ["git", "init", "-q", str(self.workspace / "plans")],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.workspace / "plans"), "add", tracked.name],
            check=True,
        )
        self.refresh_catalog()

        plan = self.plan()
        blocked = {
            item["relative_path"]: item["reason_codes"] for item in plan["blocked"]
        }
        self.assertIn(
            "minimum-age-not-met",
            blocked[young.relative_to(self.workspace).as_posix()],
        )
        active_reasons = blocked[active.relative_to(self.workspace).as_posix()]
        self.assertIn("active-frontmatter:review_status=requested", active_reasons)
        self.assertIn("opening-banner:canonical", active_reasons)
        self.assertIn(
            "git-tracked",
            blocked[tracked.relative_to(self.workspace).as_posix()],
        )

    def test_stage_is_copy_only_immutable_and_idempotent(self) -> None:
        source = self.terminal_roadmap()
        self.refresh_catalog()
        plan = self.plan()
        dry = quarantine.stage_plan(plan, self.quarantine_root, apply=False)
        self.assertEqual(dry["mode"], "stage-plan")
        self.assertFalse(self.quarantine_root.exists())

        first = quarantine.stage_plan(plan, self.quarantine_root, apply=True)
        second = quarantine.stage_plan(plan, self.quarantine_root, apply=True)
        self.assertEqual(first["status"], "created")
        self.assertEqual(second["status"], "idempotent")
        self.assertTrue(source.exists())
        manifest = Path(first["manifest"])
        staged = manifest.parent / "files" / source.relative_to(self.workspace)
        self.assertEqual(staged.read_bytes(), source.read_bytes())
        self.assertEqual(staged.stat().st_mode & 0o777, 0o400)
        self.assertNotIn(
            "Terminal evidence.",
            manifest.read_text(encoding="utf-8"),
        )
        status = quarantine.set_status(self.quarantine_root, plan["set_id"])
        self.assertEqual(status["counts"], {"staged": 1})

    def test_manifest_operational_fields_are_authenticated_by_set_id(self) -> None:
        self.terminal_roadmap()
        self.refresh_catalog()
        plan = self.plan()
        result = quarantine.stage_plan(plan, self.quarantine_root, apply=True)
        manifest_path = Path(result["manifest"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"][0]["content_sha256"] = "0" * 64
        manifest_path.chmod(0o600)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(quarantine.QuarantineError, "digest mismatch"):
            quarantine.set_status(self.quarantine_root, plan["set_id"])

    def test_review_only_set_is_sealed_without_moving_or_copying_sources(self) -> None:
        source = self.write(
            "plans/historical-plan.md",
            "# Historical plan\n\nThis superseded an older approach, but has no terminal declaration.\n",
        )
        self.refresh_catalog()
        plan = self.plan()
        self.assertEqual(plan["eligible"], [])
        self.assertEqual(len(plan["review"]), 1)
        result = quarantine.stage_plan(plan, self.quarantine_root, apply=True)
        self.assertEqual(result["artifact_count"], 0)
        self.assertEqual(result["review_count"], 1)
        self.assertTrue(source.exists())
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["review_queue"]), 1)
        self.assertEqual(manifest["artifacts"], [])
        with self.assertRaisesRegex(quarantine.QuarantineError, "no move-eligible"):
            quarantine.quarantine_set(
                self.quarantine_root,
                plan["set_id"],
                self.database,
                plan["set_id"],
                apply=True,
            )

    def test_quarantine_and_restore_use_rename_without_deletion(self) -> None:
        source = self.terminal_roadmap()
        original = source.read_bytes()
        self.refresh_catalog()
        plan = self.plan()
        quarantine.stage_plan(plan, self.quarantine_root, apply=True)
        dry = quarantine.quarantine_set(
            self.quarantine_root,
            plan["set_id"],
            self.database,
            None,
            apply=False,
        )
        self.assertEqual(len(dry["planned"]), 1)
        self.assertTrue(source.exists())

        applied = quarantine.quarantine_set(
            self.quarantine_root,
            plan["set_id"],
            self.database,
            plan["set_id"],
            apply=True,
        )
        self.assertEqual(applied["status"], "complete")
        self.assertFalse(source.exists())
        status = quarantine.set_status(self.quarantine_root, plan["set_id"])
        self.assertEqual(status["counts"], {"quarantined": 1})

        restore_plan = quarantine.restore_set(
            self.quarantine_root,
            plan["set_id"],
            None,
            apply=False,
        )
        self.assertEqual(len(restore_plan["planned"]), 1)
        restored = quarantine.restore_set(
            self.quarantine_root,
            plan["set_id"],
            plan["set_id"],
            apply=True,
        )
        self.assertEqual(restored["status"], "complete")
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(
            quarantine.set_status(self.quarantine_root, plan["set_id"])["counts"],
            {"staged": 1},
        )
        events = (
            self.quarantine_root
            / "sets"
            / plan["set_id"].removeprefix("quarantine-set:")
            / "events.jsonl"
        ).read_text(encoding="utf-8")
        self.assertIn('"action":"quarantined"', events)
        self.assertIn('"action":"restored"', events)

    def test_alias_and_backlink_block_relocation_before_any_move(self) -> None:
        aliased = self.terminal_roadmap("plans/SUPERSEDED-aliased-roadmap.md")
        referenced = self.terminal_roadmap("plans/SUPERSEDED-referenced-roadmap.md")
        self.write(
            "plans/current-plan.md",
            "# Current\n\nSee plans/SUPERSEDED-referenced-roadmap.md before migration.\n",
        )
        alias = self.workspace / "Notes/Projects/Test/old.md"
        alias.parent.mkdir(parents=True)
        os.symlink(aliased, alias)
        self.refresh_catalog()
        plan = self.plan()
        quarantine.stage_plan(plan, self.quarantine_root, apply=True)

        dry = quarantine.quarantine_set(
            self.quarantine_root,
            plan["set_id"],
            self.database,
            None,
            apply=False,
        )
        blocked = {
            item["relative_path"]: item["reason_codes"] for item in dry["blocked"]
        }
        self.assertIn(
            "live-symlink-alias",
            blocked[aliased.relative_to(self.workspace).as_posix()],
        )
        self.assertIn(
            "literal-backlink",
            blocked[referenced.relative_to(self.workspace).as_posix()],
        )
        with self.assertRaisesRegex(quarantine.QuarantineError, "no sources moved"):
            quarantine.quarantine_set(
                self.quarantine_root,
                plan["set_id"],
                self.database,
                plan["set_id"],
                apply=True,
            )
        self.assertTrue(aliased.exists())
        self.assertTrue(referenced.exists())

    def test_source_drift_blocks_staging_and_relocation_preflight_is_all_or_nothing(self) -> None:
        first = self.terminal_roadmap("plans/SUPERSEDED-first-roadmap.md")
        second = self.terminal_roadmap("plans/SUPERSEDED-second-roadmap.md")
        self.refresh_catalog()
        initial_plan = self.plan()
        quarantine.stage_plan(initial_plan, self.quarantine_root, apply=True)
        second.write_text(second.read_text() + "changed\n", encoding="utf-8")

        refreshed_plan = self.plan()
        self.assertIn(
            "catalog-source-drift",
            next(
                item["reason_codes"]
                for item in refreshed_plan["blocked"]
                if item["relative_path"] == second.relative_to(self.workspace).as_posix()
            ),
        )
        with self.assertRaisesRegex(quarantine.QuarantineError, "no sources moved"):
            quarantine.quarantine_set(
                self.quarantine_root,
                initial_plan["set_id"],
                self.database,
                initial_plan["set_id"],
                apply=True,
            )
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_interrupted_rename_is_recovered_and_audited(self) -> None:
        source = self.terminal_roadmap()
        self.refresh_catalog()
        plan = self.plan()
        quarantine.stage_plan(plan, self.quarantine_root, apply=True)
        set_dir = (
            self.quarantine_root
            / "sets"
            / plan["set_id"].removeprefix("quarantine-set:")
        )
        relocated = set_dir / "relocated" / source.relative_to(self.workspace)
        relocated.parent.mkdir(parents=True)
        os.rename(source, relocated)

        result = quarantine.quarantine_set(
            self.quarantine_root,
            plan["set_id"],
            self.database,
            plan["set_id"],
            apply=True,
        )
        self.assertEqual(result["status"], "complete")
        event = json.loads((set_dir / "events.jsonl").read_text().strip())
        self.assertTrue(event["recovered_after_interruption"])

    def test_acknowledgement_and_output_boundaries_fail_closed(self) -> None:
        self.terminal_roadmap()
        self.refresh_catalog()
        plan = self.plan()
        quarantine.stage_plan(plan, self.quarantine_root, apply=True)
        with self.assertRaisesRegex(quarantine.QuarantineError, "exactly match"):
            quarantine.quarantine_set(
                self.quarantine_root,
                plan["set_id"],
                self.database,
                "wrong",
                apply=True,
            )
        with self.assertRaisesRegex(quarantine.QuarantineError, "outside"):
            quarantine._validate_external_root(
                self.workspace / "quarantine",
                quarantine._validate_workspace(self.workspace),
            )
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                quarantine.build_parser().parse_args(["cleanup"])


class RelativeNameContainmentTests(unittest.TestCase):
    """S2.3 / KR3: the ack ``--name`` path is constrained to a safe
    workspace-relative child; ``_validate_relative`` is the containment guard.
    Feed it every traversal shape an operator (or an attacker) could type and
    assert each is rejected, and that a normal relative name passes unchanged —
    the guard existed but had no adversarial test before this."""

    def test_absolute_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            quarantine.QuarantineError, "unsafe workspace-relative"
        ):
            quarantine._validate_relative("/etc/passwd")

    def test_parent_traversal_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            quarantine.QuarantineError, "unsafe workspace-relative"
        ):
            quarantine._validate_relative("../../secrets")

    def test_embedded_dotdot_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            quarantine.QuarantineError, "unsafe workspace-relative"
        ):
            quarantine._validate_relative("a/../../b")

    def test_empty_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            quarantine.QuarantineError, "unsafe workspace-relative"
        ):
            quarantine._validate_relative("")

    def test_safe_relative_name_passes_unchanged(self) -> None:
        self.assertEqual(
            quarantine._validate_relative("catalog-run-6-chunks-v2"),
            "catalog-run-6-chunks-v2",
        )


if __name__ == "__main__":
    unittest.main()
