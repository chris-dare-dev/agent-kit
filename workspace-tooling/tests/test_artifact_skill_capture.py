from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

import platform_skips
from argparse import Namespace
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import artifact_skill_capture as capture  # noqa: E402


class ArtifactSkillCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.receipts = self.root / "receipts"
        self.workspace.mkdir()
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
                            ".claude/notes/**",
                            "docs/**",
                            "repos/**/plans/**",
                        ],
                        "exclude_roots": ["Vault", "Notes", ".worktrees"],
                        "prune_directory_names": [
                            ".aggregate",
                            ".git",
                            "node_modules",
                        ],
                        "prune_path_globs": ["**/.aggregate/**", "**/vendor/**"],
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, relative: str, content: str = "# artifact\n") -> Path:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def args(
        self,
        producer: str,
        run_id: str,
        *,
        paths: list[Path] | None = None,
        roots: list[Path] | None = None,
        apply: bool = False,
        receipt_root: Path | None = None,
    ) -> Namespace:
        return Namespace(
            workspace=self.workspace,
            policy=Path("scripts/artifact-policy.json"),
            producer=producer,
            run_id=run_id,
            paths=paths or [],
            roots=roots or [],
            receipt_root=receipt_root or self.receipts,
            apply=apply,
        )

    def test_plan_is_no_write_and_routes_handoff_as_candidate(self) -> None:
        handoff = self.write("plans/HANDOFF-alpha.md", "status: requested\n")
        result = capture.emit(
            self.args("handoff", "alpha-1", paths=[handoff], apply=False)
        )
        self.assertEqual(result["status"], "planned")
        self.assertFalse(self.receipts.exists())
        artifact = result["artifacts"][0]
        self.assertEqual(artifact["artifact_type"], "handoff")
        self.assertEqual(artifact["routing"]["qdrant"], "eligible")
        self.assertEqual(artifact["routing"]["graphiti"], "candidate")
        self.assertEqual(artifact["routing"]["graphiti_bulk"], "disabled")
        self.assertEqual(result["safety"]["sink_writes"], "none")

    @platform_skips.requires_posix_modes
    def test_apply_is_exclusive_and_idempotent(self) -> None:
        roadmap = self.write("plans/alpha-roadmap.md")
        arguments = self.args("roadmap", "alpha", paths=[roadmap], apply=True)
        first = capture.emit(arguments)
        receipt_path = Path(first["receipt_path"])
        first_bytes = receipt_path.read_bytes()
        first_stat = receipt_path.stat()
        second = capture.emit(arguments)
        self.assertEqual(first["status"], "created")
        self.assertEqual(second["status"], "idempotent")
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(receipt_path.read_bytes(), first_bytes)
        self.assertEqual(receipt_path.stat().st_mtime_ns, first_stat.st_mtime_ns)
        self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)

    @platform_skips.requires_symlinks
    def test_recursive_capture_is_sorted_and_skips_non_documents_and_symlinks(self) -> None:
        spike_root = self.workspace / ".claude/notes/spikes/S-1"
        second = self.write(".claude/notes/spikes/S-1/z.md")
        first = self.write(".claude/notes/spikes/S-1/a.json", "{}\n")
        self.write(".claude/notes/spikes/S-1/code.py", "print('no')\n")
        external = self.root / "external.md"
        external.write_text("outside\n", encoding="utf-8")
        os.symlink(external, spike_root / "linked.md")
        result = capture.emit(
            self.args("spike", "S-1", roots=[spike_root], apply=False)
        )
        self.assertEqual(
            [artifact["relative_path"] for artifact in result["artifacts"]],
            [
                first.relative_to(self.workspace).as_posix(),
                second.relative_to(self.workspace).as_posix(),
            ],
        )
        self.assertTrue(
            all(
                artifact["routing"]["graphiti"] == "ineligible"
                for artifact in result["artifacts"]
            )
        )

    @platform_skips.requires_symlinks
    def test_refuses_external_symlink_excluded_and_wrong_producer_type(self) -> None:
        external = self.root / "external.md"
        external.write_text("# outside\n", encoding="utf-8")
        with self.assertRaisesRegex(capture.CaptureError, "inside the workspace"):
            capture.emit(self.args("handoff", "x", paths=[external]))

        plans = self.workspace / "plans"
        plans.mkdir()
        os.symlink(external, plans / "linked-handoff.md")
        with self.assertRaisesRegex(capture.CaptureError, "symlink"):
            capture.emit(
                self.args("handoff", "x", paths=[plans / "linked-handoff.md"])
            )

        excluded = self.write("Vault/HANDOFF-no.md")
        with self.assertRaisesRegex(capture.CaptureError, "excluded"):
            capture.emit(self.args("handoff", "x", paths=[excluded]))

        document = self.write("docs/reference.md")
        with self.assertRaisesRegex(capture.CaptureError, "cannot capture document"):
            capture.emit(self.args("handoff", "x", paths=[document]))
        self.assertFalse(self.receipts.exists())

    def test_refuses_receipt_root_inside_workspace(self) -> None:
        handoff = self.write("plans/HANDOFF-alpha.md")
        with self.assertRaisesRegex(capture.CaptureError, "outside the source workspace"):
            capture.emit(
                self.args(
                    "handoff",
                    "alpha",
                    paths=[handoff],
                    receipt_root=self.workspace / "derived",
                )
            )

    def test_content_change_creates_new_event_without_replacing_old_receipt(self) -> None:
        roadmap = self.write("plans/alpha-roadmap.md", "one\n")
        first = capture.emit(
            self.args("roadmap", "alpha", paths=[roadmap], apply=True)
        )
        roadmap.write_text("two\n", encoding="utf-8")
        second = capture.emit(
            self.args("roadmap", "alpha", paths=[roadmap], apply=True)
        )
        self.assertNotEqual(first["event_id"], second["event_id"])
        self.assertTrue(Path(first["receipt_path"]).exists())
        self.assertTrue(Path(second["receipt_path"]).exists())

    def test_existing_receipt_safety_mismatch_fails_closed(self) -> None:
        handoff = self.write("plans/HANDOFF-alpha.md")
        arguments = self.args("handoff", "alpha", paths=[handoff], apply=True)
        first = capture.emit(arguments)
        receipt_path = Path(first["receipt_path"])
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["safety"]["graphiti_bulk"] = "enabled"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(capture.CaptureError, "identity mismatch"):
            capture.emit(arguments)

    @platform_skips.requires_symlinks
    def test_symlinked_receipt_shard_cannot_redirect_write(self) -> None:
        handoff = self.write("plans/HANDOFF-alpha.md")
        planned = capture.emit(
            self.args("handoff", "alpha", paths=[handoff], apply=False)
        )
        event_hex = planned["event_id"].removeprefix("event:")
        external = self.root / "external-receipts"
        external.mkdir()
        self.receipts.mkdir()
        os.symlink(external, self.receipts / event_hex[:2])
        with self.assertRaisesRegex(capture.CaptureError, "symlink"):
            capture.emit(
                self.args("handoff", "alpha", paths=[handoff], apply=True)
            )
        self.assertEqual(list(external.iterdir()), [])

    def test_atomic_receipt_crash_never_exposes_partial_final(self) -> None:
        handoff = self.write("plans/HANDOFF-crash.md")
        planned = capture.emit(
            self.args("handoff", "crash", paths=[handoff], apply=False)
        )
        event_hex = planned["event_id"].removeprefix("event:")
        artifacts = planned["artifacts"]

        def before_publish(stage: str) -> None:
            if stage == "after_file_fsync":
                raise RuntimeError("simulated crash")

        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            capture.write_receipt(
                self.receipts,
                "handoff",
                "crash",
                event_hex,
                artifacts,
                fault=before_publish,
            )
        final = self.receipts / event_hex[:2] / f"{event_hex}.json"
        self.assertFalse(final.exists())
        self.assertEqual(list(final.parent.iterdir()), [])

        def after_publish(stage: str) -> None:
            if stage == "after_publish":
                raise RuntimeError("simulated crash")

        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            capture.write_receipt(
                self.receipts,
                "handoff",
                "crash",
                event_hex,
                artifacts,
                fault=after_publish,
            )
        self.assertTrue(final.exists())
        status, replay_path, _receipt = capture.write_receipt(
            self.receipts,
            "handoff",
            "crash",
            event_hex,
            artifacts,
        )
        self.assertEqual(status, "idempotent")
        self.assertEqual(replay_path, final)


if __name__ == "__main__":
    unittest.main()
