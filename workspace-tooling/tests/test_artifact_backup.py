from __future__ import annotations

import collections
import contextlib
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

import platform_skips
from pathlib import Path
from unittest import mock

import artifact_backup_offdevice as offdevice
import artifact_backup_snapshot as snapshot
import artifact_memory


SCRIPT_DIR = Path(__file__).resolve().parents[1]
PLIST = SCRIPT_DIR / "com.personal.artifact-backup-daily.plist"


def _make_snapshots(root: Path, count: int, size: int = 1024) -> None:
    for index in range(count):
        (root / f"collection-{index:03d}.snapshot").write_bytes(b"x" * size)


class SnapshotCadenceLaunchdTests(unittest.TestCase):
    def test_plist_is_scheduled_not_run_at_load(self) -> None:
        with PLIST.open("rb") as handle:
            value = plistlib.load(handle)
        # RunAtLoad would fire a ~536 MB write the moment the agent is
        # bootstrapped; the cadence is the calendar interval only.
        self.assertFalse(value["RunAtLoad"])
        self.assertIn("StartCalendarInterval", value)
        self.assertEqual(value["Umask"], 0o77)

    def test_plist_uses_the_venv_interpreter(self) -> None:
        with PLIST.open("rb") as handle:
            value = plistlib.load(handle)
        # The system python3 has no qdrant_client, so a system-python plist
        # would fail every night and only show up in a log nobody reads.
        self.assertIn("personal-artifacts/venv/bin/python", value["ProgramArguments"][0])
        self.assertTrue(value["ProgramArguments"][1].endswith("artifact_backup_offdevice.py"))


class RetentionAssessmentTests(unittest.TestCase):
    def test_levels_escalate_with_count(self) -> None:
        for count, expected in (
            (snapshot.WARN_COUNT, "ok"),
            (snapshot.WARN_COUNT + 1, "warn"),
            (snapshot.ALARM_COUNT + 1, "alarm"),
        ):
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                _make_snapshots(root, count)
                self.assertEqual(assess_level(root), expected, f"count={count}")

    def test_assessment_never_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _make_snapshots(root, snapshot.ALARM_COUNT + 5)
            before = sorted(p.name for p in root.iterdir())
            result = snapshot.assess_retention(root)
            after = sorted(p.name for p in root.iterdir())
            # Pruning is gated on separate approval (ADR-002 §7); the job may
            # only name candidates, never remove them.
            self.assertEqual(before, after)
            self.assertEqual(result["auto_delete"], "disabled")
            self.assertEqual(result["pruning"], "gated-human-action")
            self.assertTrue(result["prune_candidates"])

    def test_corrupt_probe_is_not_a_recovery_point(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _make_snapshots(root, 2)
            # The drill leaves a deliberately truncated file here; counting it
            # would overstate how many real recovery points exist.
            (root / "corrupt-restore-probe.snapshot").write_bytes(b"junk")
            self.assertEqual(snapshot.assess_retention(root)["count"], 2)


class SnapshotRotationTests(unittest.TestCase):
    def test_keeps_newest_and_removes_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _make_snapshots(root, 10)
            result = snapshot.prune(root, keep=3)
            remaining = sorted(p.name for p in root.glob("*.snapshot"))
            self.assertEqual(len(remaining), 3)
            self.assertEqual(len(result["pruned"]), 7)

    def test_drill_evidence_is_never_rotated_away(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _make_snapshots(root, 6)
            keeper = "drill-verified.snapshot"
            (root / keeper).write_bytes(b"evidence")
            (root / "snapshot-restore-evidence.json").write_text(
                '{"snapshot": {"name": "%s"}}' % keeper, encoding="utf-8"
            )
            # Oldest by mtime, so ordinary rotation would remove it first.
            snapshot.prune(root, keep=1)
            self.assertTrue((root / keeper).exists())

    def test_unreadable_evidence_refuses_to_rotate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _make_snapshots(root, 6)
            (root / "snapshot-restore-evidence.json").write_text("{ broken", encoding="utf-8")
            result = snapshot.prune(root, keep=1)
            # Failing open here would delete the very snapshot the unreadable
            # evidence was protecting.
            self.assertEqual(result["pruned"], [])
            self.assertEqual(len(list(root.glob("*.snapshot"))), 6)

    def test_keep_zero_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(snapshot.SnapshotCadenceError):
                snapshot.prune(Path(raw), keep=0)


class BundleRotationTests(unittest.TestCase):
    def test_only_this_scripts_bundles_are_rotated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for index in range(5):
                (root / f"artifact-memory-2026071{index}T000000Z.tar.age").write_bytes(b"x")
            unrelated = root / "someone-elses-archive.tar.age"
            unrelated.write_bytes(b"keep me")
            offdevice._prune_bundles(root, keep=2)
            self.assertTrue(unrelated.exists())
            self.assertEqual(len(list(root.glob("artifact-memory-*.tar.age"))), 2)


class OffDeviceDestinationTests(unittest.TestCase):
    def test_remote_destinations_are_refused(self) -> None:
        for destination in (
            "s3://workspace-backups/artifact-memory",
            "gs://bucket/path",
            "https://example.invalid/upload",
            "ssh://host/path",
        ):
            with self.subTest(destination=destination):
                # Uploading is an external write under the External System
                # Write Policy -- it must be a human decision, not a flag.
                code = offdevice.main(["--destination", destination, "--apply"])
                self.assertEqual(code, 3)

    def test_local_destination_is_not_refused(self) -> None:
        self.assertFalse("/Volumes/backup".startswith(offdevice.REMOTE_PREFIXES))


class OffDeviceBundleContractTests(unittest.TestCase):
    def test_apply_requires_recipients_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(offdevice.BackupError):
                offdevice.build(
                    runtime=_FakeRuntime(Path(raw)),
                    destination=Path(raw),
                    recipients_file=None,
                    apply=True,
                )

    def test_dry_run_writes_nothing_and_excludes_keys(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            report = offdevice.build(
                runtime=_FakeRuntime(root),
                destination=root / "out",
                recipients_file=None,
                apply=False,
            )
            self.assertFalse(report["applied"])
            self.assertFalse((root / "out").exists())
            # Shipping the key inside the blob it protects defeats the point.
            self.assertFalse(report["keys_in_bundle"])
            self.assertIn("refused", report["remote_upload"])


class PlanRequiredInventoryTests(unittest.TestCase):
    """#4: the plan is a REQUIRED inventory, not just what the filesystem shows."""

    def test_required_evidence_and_snapshot_are_represented_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "services" / "snapshots").mkdir(parents=True)
            items = offdevice._plan(_FakeRuntime(root))
            names = {i["name"] for i in items}
            # required evidence represented even though nothing is on disk
            self.assertIn("evidence/snapshot-restore-evidence.json", names)
            self.assertIn("evidence/qdrant-shadow-verification.json", names)
            # the required latest snapshot is represented as ABSENT, not omitted
            snap = next(i for i in items if i["name"] == "qdrant-snapshot")
            self.assertFalse(snap["present"])

    def test_snapshot_restore_evidence_is_bundled_from_its_real_subdir(self) -> None:
        # The exact file root.glob("*.json") always missed (it is depth-3).
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            snapdir = root / "services" / "snapshots"
            snapdir.mkdir(parents=True)
            (snapdir / "snapshot-restore-evidence.json").write_text("{}", encoding="utf-8")
            (snapdir / "collection-000.snapshot").write_bytes(b"x")
            items = {i["name"]: i for i in offdevice._plan(_FakeRuntime(root))}
            self.assertTrue(items["evidence/snapshot-restore-evidence.json"]["present"])
            self.assertEqual(
                items["evidence/snapshot-restore-evidence.json"]["path"],
                snapdir / "snapshot-restore-evidence.json",
            )
            self.assertTrue(items["qdrant-snapshot"]["present"])

    def test_empty_evidence_categories_are_represented_as_absent_sentinels(self) -> None:
        # Sol #4 + Chris's decision: the three wildcard evidence categories are
        # represented (so a producer that drops one is caught at verify) but as
        # NON-failing present:False sentinels on an evaluation-quiet night.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "services" / "snapshots").mkdir(parents=True)
            items = {i["name"]: i for i in offdevice._plan(_FakeRuntime(root))}
            for cat in (
                "evidence-category/artifact-retrieval-eval",
                "evidence-category/artifact-retrieval-migration",
                "evidence-category/phase0-projection-audit",
            ):
                self.assertIn(cat, items)
                self.assertFalse(items[cat]["present"])

    def test_populated_evidence_category_emits_no_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "services" / "snapshots").mkdir(parents=True)
            (root / "artifact-retrieval-eval-dev-x.json").write_text("{}", encoding="utf-8")
            items = {i["name"]: i for i in offdevice._plan(_FakeRuntime(root))}
            # the matching file is bundled individually...
            self.assertIn("evidence/artifact-retrieval-eval-dev-x.json", items)
            self.assertTrue(items["evidence/artifact-retrieval-eval-dev-x.json"]["present"])
            # ...and no empty-category sentinel is emitted for it
            self.assertNotIn("evidence-category/artifact-retrieval-eval", items)


class IncompleteBackupHonestyTests(unittest.TestCase):
    """#3: an incomplete tier-1 backup must not look fresh + successful."""

    def test_rpo_clock_advances_only_on_a_complete_backup(self) -> None:
        self.assertEqual(
            offdevice._rpo_completed_at(complete=True, run_at="T2", prior_completed_at="T1"),
            "T2",
        )
        # incomplete run keeps the prior successful timestamp -- not a fresh one
        self.assertEqual(
            offdevice._rpo_completed_at(complete=False, run_at="T2", prior_completed_at="T1"),
            "T1",
        )
        self.assertIsNone(
            offdevice._rpo_completed_at(complete=False, run_at="T2", prior_completed_at=None)
        )

    def test_incomplete_tier1_backup_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with mock.patch.object(
                offdevice.artifact_runtime, "load_runtime", return_value=_FakeRuntime(Path(raw))
            ), mock.patch.object(
                offdevice,
                "build",
                return_value={"applied": True, "tier1_complete": False, "tier1_absent": ["evals"]},
            ):
                code = offdevice.main(
                    ["--destination", raw, "--apply", "--recipients-file", str(Path(raw) / "r")]
                )
            self.assertEqual(code, 5)

    def test_complete_backup_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with mock.patch.object(
                offdevice.artifact_runtime, "load_runtime", return_value=_FakeRuntime(Path(raw))
            ), mock.patch.object(
                offdevice, "build", return_value={"applied": True, "tier1_complete": True}
            ):
                code = offdevice.main(
                    ["--destination", raw, "--apply", "--recipients-file", str(Path(raw) / "r")]
                )
            self.assertEqual(code, 0)

    def test_incomplete_tier2_backup_exits_nonzero(self) -> None:
        # Sol #3: a tier-2 gap keeps tier1_complete True, so pre-fix main() exited 0.
        # bundle_complete=False must now drive the same nonzero exit.
        with tempfile.TemporaryDirectory() as raw:
            with mock.patch.object(
                offdevice.artifact_runtime, "load_runtime", return_value=_FakeRuntime(Path(raw))
            ), mock.patch.object(
                offdevice,
                "build",
                return_value={
                    "applied": True,
                    "tier1_complete": True,
                    "bundle_complete": False,
                    "incomplete_items": ["unit-archive"],
                },
            ):
                code = offdevice.main(
                    ["--destination", raw, "--apply", "--recipients-file", str(Path(raw) / "r")]
                )
            self.assertEqual(code, 5)


class TreeIntegrityManifestTests(unittest.TestCase):
    """H6: tree entries carry per-entry sha256 + type + mode + explicit symlink
    policy, and verification catches corruption, omission, and addition -- none of
    which the old count+bytes manifest could see."""

    def _make_source_tree(self, root: Path) -> Path:
        src = root / "src-tree"
        (src / "sub").mkdir(parents=True)
        (src / "a.json").write_text('{"k": 1}', encoding="utf-8")
        (src / "sub" / "b.txt").write_text("hello", encoding="utf-8")
        (src / "sub" / "empty").mkdir()          # an empty dir must be recorded too
        os.symlink("a.json", src / "link-to-a")  # relative symlink, recorded by target
        return src

    def _copied(self, root: Path) -> tuple[Path, dict]:
        src = self._make_source_tree(root)
        dest = root / "copied"
        return dest, offdevice._copy_tree(src, dest)

    @platform_skips.requires_symlinks
    def test_manifest_records_per_entry_sha256_type_mode_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, manifest = self._copied(Path(raw))
            self.assertEqual(manifest["kind"], "tree")
            self.assertRegex(manifest["tree_sha256"], r"^[0-9a-f]{64}$")
            by_path = {e["path"]: e for e in manifest["entries"]}
            self.assertEqual(by_path["a.json"]["type"], "file")
            self.assertRegex(by_path["a.json"]["sha256"], r"^[0-9a-f]{64}$")
            self.assertIn("mode", by_path["a.json"])
            self.assertEqual(by_path["sub/b.txt"]["type"], "file")
            self.assertEqual(by_path["sub"]["type"], "dir")
            self.assertEqual(by_path["sub/empty"]["type"], "dir")  # empty dir preserved
            link = by_path["link-to-a"]
            self.assertEqual(link["type"], "symlink")
            self.assertEqual(link["target"], "a.json")
            self.assertNotIn("sha256", link)  # never followed / digested

    @platform_skips.requires_symlinks
    def test_intact_tree_verifies_clean(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            dest, manifest = self._copied(Path(raw))
            problems, _, _ = offdevice._verify_tree(manifest, dest)
            self.assertEqual(problems, [])

    @platform_skips.requires_symlinks
    def test_single_byte_corruption_is_caught(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            dest, manifest = self._copied(Path(raw))
            (dest / "a.json").write_text('{"k": 2}', encoding="utf-8")  # same length
            problems, _, _ = offdevice._verify_tree(manifest, dest)
            self.assertTrue(
                any("sha256 mismatch" in p and "a.json" in p for p in problems), problems
            )

    @platform_skips.requires_symlinks
    def test_omitted_entry_is_caught(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            dest, manifest = self._copied(Path(raw))
            (dest / "sub" / "b.txt").unlink()
            problems, _, _ = offdevice._verify_tree(manifest, dest)
            self.assertTrue(
                any("missing entry" in p and "sub/b.txt" in p for p in problems), problems
            )

    @platform_skips.requires_symlinks
    def test_unexpected_entry_is_caught(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            dest, manifest = self._copied(Path(raw))
            (dest / "sneaked.txt").write_text("surprise", encoding="utf-8")
            problems, _, _ = offdevice._verify_tree(manifest, dest)
            self.assertTrue(
                any("unexpected entry" in p and "sneaked.txt" in p for p in problems), problems
            )

    @platform_skips.requires_symlinks
    def test_symlink_repoint_is_caught(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            dest, manifest = self._copied(Path(raw))
            link = dest / "link-to-a"
            link.unlink()
            os.symlink("sub/b.txt", link)
            problems, _, _ = offdevice._verify_tree(manifest, dest)
            self.assertTrue(any("symlink target changed" in p for p in problems), problems)

    @platform_skips.requires_symlinks
    def test_mode_only_change_is_advisory_not_a_defect(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            dest, manifest = self._copied(Path(raw))
            recorded_mode = {e["path"]: e for e in manifest["entries"]}["a.json"]["mode"]
            new_mode = 0o640 if recorded_mode != "0640" else 0o600
            (dest / "a.json").chmod(new_mode)
            problems, advisories, _ = offdevice._verify_tree(manifest, dest)
            self.assertEqual(problems, [], problems)  # restore re-applies modes
            self.assertTrue(any("a.json" in a for a in advisories), advisories)

    def test_empty_tree_is_handled(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            src = root / "empty-src"
            src.mkdir()
            dest = root / "empty-dest"
            manifest = offdevice._copy_tree(src, dest)
            self.assertEqual(manifest["entries"], [])
            self.assertEqual(manifest["files"], 0)
            self.assertEqual(offdevice._verify_tree(manifest, dest)[0], [])

    @platform_skips.requires_symlinks
    def test_symlink_in_tree_is_surfaced_as_advisory(self) -> None:
        # #7: derived-state symlinks are what the restore `--repair` rejects. They
        # are not a hard verify failure (H6 records them by target), but MUST be
        # surfaced so a symlink-bearing bundle is not a silent clean-verify ->
        # restore-abort surprise.
        with tempfile.TemporaryDirectory() as raw:
            dest, manifest = self._copied(Path(raw))  # source tree includes link-to-a
            problems, advisories, _ = offdevice._verify_tree(manifest, dest)
            self.assertEqual(problems, [], problems)
            self.assertTrue(
                any("link-to-a" in a and "repair" in a for a in advisories), advisories
            )


class ExtractedBundleVerifyTests(unittest.TestCase):
    """The runbook's 're-digest each restored entry and compare' step, automated
    and now covering tree entries (H6). Fail-closed on unreadable/absent inputs."""

    def _bundle(self, root: Path) -> Path:
        """A COMPLETE bundle: every hard-required inventory item present + on disk +
        in the plan, plus the three represent-when-absent evidence-category
        sentinels. A real bundle carries the full REQUIRED_INVENTORY, so the fixture
        must too -- else verify's canonical reconciliation would (correctly) flag it
        incomplete. The mutation tests below delete/corrupt ONE item from this
        baseline. skill-events (receipts/r1.json+r2.json) and artifact-catalog are
        kept verbatim for the tree-/file-item tests."""
        bundle = root / "artifact-memory-20260720T000000Z"
        bundle.mkdir(parents=True)
        items: dict = {}
        plan: list = []

        def _file(name: str, tier: int, data: bytes) -> None:
            src = root / ("src-" + name.replace("/", "_"))
            src.write_bytes(data)
            target = bundle / name
            target.parent.mkdir(parents=True, exist_ok=True)
            items[name] = offdevice._copy_file(src, target)
            items[name]["tier"] = tier
            plan.append({"name": name, "tier": tier, "present": True})

        def _tree(name: str, tier: int, files: dict) -> None:
            src = root / ("srctree-" + name.replace("/", "_"))
            for rel, text in files.items():
                path = src / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            items[name] = offdevice._copy_tree(src, bundle / name)
            items[name]["tier"] = tier
            plan.append({"name": name, "tier": tier, "present": True})

        # tier-1 trees
        _tree("skill-events", 1, {"receipts/r1.json": '{"id": 1}', "receipts/r2.json": '{"id": 2}'})
        _tree("evals", 1, {"holdout.json": '{"h": 1}'})
        _tree("graphiti-pilots", 1, {"pilot.json": '{"p": 1}'})
        # tier-1 state (verify treats "file" and "sqlite-online-backup" identically)
        _file("consumer-state", 1, b"CONSUMER")
        _file("ingestion-state", 1, b"INGEST")
        # tier-1 enumerated evidence files
        _file("evidence/qdrant-shadow-verification.json", 1, b"{}")
        _file("evidence/snapshot-restore-evidence.json", 1, b"{}")
        # tier-2 rebuild authority
        _file("artifact-catalog", 2, b"CATALOG-BYTES")
        _file("outbox", 2, b"OUTBOX")
        _file("shadow-replay", 2, b"REPLAY")
        _file("unit-archive", 2, b"ARCHIVE")
        _file("runtime-config", 2, b"{}")
        _file("qdrant-snapshot", 2, b"SNAPSHOT")
        # the three represent-when-absent evidence categories, empty this run
        for cat in (
            "evidence-category/artifact-retrieval-eval",
            "evidence-category/artifact-retrieval-migration",
            "evidence-category/phase0-projection-audit",
        ):
            plan.append({"name": cat, "tier": 1, "present": False})

        manifest = {
            "schema_version": 1,
            # verify asserts completeness against the plan AND that keys never
            # travelled -- a valid fixture must carry both.
            "plan": plan,
            "items": items,
            "keys_in_bundle": False,
            "key_digests": {},
        }
        (bundle / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
        return bundle

    @staticmethod
    def _rewrite_manifest(bundle: Path, mutate) -> None:
        manifest = json.loads((bundle / "MANIFEST.json").read_text(encoding="utf-8"))
        mutate(manifest)
        (bundle / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_intact_bundle_verifies_ok(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            report = offdevice.verify_extracted_bundle(self._bundle(Path(raw)))
            self.assertTrue(report["ok"], report["problems"])
            # skill-events, evals, graphiti-pilots -- a complete bundle carries all
            self.assertEqual(report["trees_verified"], 3)

    def test_corrupted_tree_file_fails_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            (bundle / "skill-events" / "receipts" / "r1.json").write_text('{"id": 9}', encoding="utf-8")
            report = offdevice.verify_extracted_bundle(bundle)
            self.assertFalse(report["ok"])
            self.assertTrue(any("sha256 mismatch" in p for p in report["problems"]), report["problems"])

    def test_omitted_tree_file_fails_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            (bundle / "skill-events" / "receipts" / "r2.json").unlink()
            report = offdevice.verify_extracted_bundle(bundle)
            self.assertFalse(report["ok"])
            self.assertTrue(any("missing entry" in p for p in report["problems"]), report["problems"])

    def test_corrupted_file_item_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            (bundle / "artifact-catalog").write_bytes(b"TAMPERED")
            report = offdevice.verify_extracted_bundle(bundle)
            self.assertFalse(report["ok"])
            self.assertTrue(
                any("artifact-catalog" in p and "sha256" in p for p in report["problems"]),
                report["problems"],
            )

    def test_missing_file_item_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            (bundle / "artifact-catalog").unlink()
            report = offdevice.verify_extracted_bundle(bundle)
            self.assertFalse(report["ok"])
            self.assertTrue(
                any("artifact-catalog" in p and "missing" in p for p in report["problems"]),
                report["problems"],
            )

    def test_unexpected_top_level_entry_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            (bundle / "stowaway.bin").write_bytes(b"x")
            report = offdevice.verify_extracted_bundle(bundle)
            self.assertFalse(report["ok"])
            self.assertTrue(
                any("unexpected entry" in p and "stowaway" in p for p in report["problems"]),
                report["problems"],
            )

    def test_missing_manifest_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw) / "no-manifest"
            bundle.mkdir()
            with self.assertRaises(offdevice.BackupError):
                offdevice.verify_extracted_bundle(bundle)

    def test_legacy_tree_without_entries_is_flagged_once(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            manifest = json.loads((bundle / "MANIFEST.json").read_text(encoding="utf-8"))
            manifest["items"]["skill-events"].pop("entries", None)
            (bundle / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
            report = offdevice.verify_extracted_bundle(bundle)
            self.assertFalse(report["ok"])
            self.assertTrue(
                any("not content-verifiable" in p for p in report["problems"]), report["problems"]
            )
            # ...and its own files are NOT double-reported as unexpected.
            self.assertFalse(
                any("unexpected entry" in p for p in report["problems"]), report["problems"]
            )

    def test_cli_verify_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            self.assertEqual(offdevice.main(["--verify", str(bundle)]), 0)
            (bundle / "skill-events" / "receipts" / "r1.json").write_text("x", encoding="utf-8")
            self.assertEqual(offdevice.main(["--verify", str(bundle)]), 4)

    # --- completeness: an INCOMPLETE bundle must never verify clean --------------

    def test_whole_item_absent_at_backup_is_caught(self) -> None:
        # An item not present at backup time never enters `items`; the plan still
        # names it, so verify must flag the bundle incomplete (the CRITICAL gap:
        # a transiently-absent irreplaceable tree otherwise verified ok:true).
        with tempfile.TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            shutil.rmtree(bundle / "skill-events")

            def mutate(m: dict) -> None:
                m["items"].pop("skill-events")
                for planned in m["plan"]:
                    if planned["name"] == "skill-events":
                        planned["present"] = False

            self._rewrite_manifest(bundle, mutate)
            report = offdevice.verify_extracted_bundle(bundle)
            self.assertFalse(report["ok"])
            self.assertTrue(
                any("skill-events" in p and "incomplete" in p for p in report["problems"]),
                report["problems"],
            )

    def test_planned_item_dropped_from_items_is_caught(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            shutil.rmtree(bundle / "skill-events")
            self._rewrite_manifest(bundle, lambda m: m["items"].pop("skill-events"))
            report = offdevice.verify_extracted_bundle(bundle)
            self.assertFalse(report["ok"])
            self.assertTrue(
                any("skill-events" in p and "manifest inconsistent" in p for p in report["problems"]),
                report["problems"],
            )

    def test_manifest_without_plan_cannot_prove_completeness(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            self._rewrite_manifest(bundle, lambda m: m.pop("plan"))
            report = offdevice.verify_extracted_bundle(bundle)
            self.assertFalse(report["ok"])
            self.assertTrue(any("no plan" in p for p in report["problems"]), report["problems"])

    def test_required_item_dropped_from_plan_entirely_is_caught(self) -> None:
        # Sol #4 -- the reviewer's exact repro: remove a required item from the
        # plan, the items map, AND disk. The self-consistent plan checks cannot see
        # it (it never enters the plan); the canonical-inventory reconciliation must.
        with tempfile.TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            shutil.rmtree(bundle / "skill-events")

            def mutate(m: dict) -> None:
                m["items"].pop("skill-events")
                m["plan"] = [p for p in m["plan"] if p.get("name") != "skill-events"]

            self._rewrite_manifest(bundle, mutate)
            report = offdevice.verify_extracted_bundle(bundle)
            self.assertFalse(report["ok"])
            self.assertTrue(
                any(
                    "skill-events" in p and "canonical inventory requires it" in p
                    for p in report["problems"]
                ),
                report["problems"],
            )

    def test_required_evidence_category_dropped_from_plan_is_caught(self) -> None:
        # Sol #4 for a wildcard category: dropping the whole category from the plan
        # (neither a matching file nor its sentinel) is a producer regression.
        with tempfile.TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            cat = "evidence-category/artifact-retrieval-eval"
            self._rewrite_manifest(
                bundle,
                lambda m: m.__setitem__("plan", [p for p in m["plan"] if p.get("name") != cat]),
            )
            report = offdevice.verify_extracted_bundle(bundle)
            self.assertFalse(report["ok"])
            self.assertTrue(
                any("evidence category unrepresented" in p for p in report["problems"]),
                report["problems"],
            )

    def test_empty_category_sentinel_alone_verifies_clean(self) -> None:
        # Chris's decision: an empty wildcard category is a represented, NON-failing
        # sentinel -- the complete fixture carries all three as present:False and
        # must still verify ok, with no category problem.
        with tempfile.TemporaryDirectory() as raw:
            report = offdevice.verify_extracted_bundle(self._bundle(Path(raw)))
            self.assertTrue(report["ok"], report["problems"])
            self.assertFalse(
                any("evidence-category" in p for p in report["problems"]), report["problems"]
            )

    def test_matched_evidence_file_satisfies_its_category(self) -> None:
        # A populated category (a matching evidence file named in the plan) needs no
        # sentinel; verify must accept the category as satisfied by the file.
        with tempfile.TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            evname = "evidence/artifact-retrieval-eval-dev-x.json"
            (bundle / "evidence").mkdir(exist_ok=True)
            src = Path(raw) / "eval-src.json"
            src.write_text("{}", encoding="utf-8")
            item = offdevice._copy_file(src, bundle / evname)

            def mutate(m: dict) -> None:
                cat = "evidence-category/artifact-retrieval-eval"
                m["plan"] = [p for p in m["plan"] if p.get("name") != cat]
                m["plan"].append({"name": evname, "tier": 1, "present": True})
                m["items"][evname] = item

            self._rewrite_manifest(bundle, mutate)
            report = offdevice.verify_extracted_bundle(bundle)
            self.assertTrue(report["ok"], report["problems"])

    def test_keys_in_bundle_true_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            self._rewrite_manifest(bundle, lambda m: m.__setitem__("keys_in_bundle", True))
            report = offdevice.verify_extracted_bundle(bundle)
            self.assertFalse(report["ok"])
            self.assertTrue(any("keys_in_bundle" in p for p in report["problems"]), report["problems"])

    def test_traversal_item_name_is_refused_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "SECRET.txt").write_bytes(b"outside-the-bundle")
            bundle = self._bundle(root)
            secret_sha = offdevice._digest_file(root / "SECRET.txt")
            self._rewrite_manifest(
                bundle,
                lambda m: m["items"].__setitem__(
                    "../SECRET.txt", {"kind": "file", "sha256": secret_sha}
                ),
            )
            report = offdevice.verify_extracted_bundle(bundle)
            self.assertFalse(report["ok"])
            self.assertTrue(
                any("unsafe item name" in p for p in report["problems"]), report["problems"]
            )

    def test_cli_missing_manifest_exits_4(self) -> None:
        # A destroyed/absent manifest is the WORST failure, not a softer exit 2.
        with tempfile.TemporaryDirectory() as raw:
            empty = Path(raw) / "no-manifest"
            empty.mkdir()
            self.assertEqual(offdevice.main(["--verify", str(empty)]), 4)

    # --- #6: malformed / untraversable bundles must NOT verify clean ------------

    def test_unknown_schema_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            self._rewrite_manifest(bundle, lambda m: m.__setitem__("schema_version", 999))
            with self.assertRaises(offdevice.BackupError):
                offdevice.verify_extracted_bundle(bundle)

    def test_unknown_item_kind_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            (bundle / "mystery").write_bytes(b"x")
            sha = offdevice._digest_file(bundle / "mystery")
            self._rewrite_manifest(
                bundle,
                lambda m: m["items"].__setitem__("mystery", {"kind": "banana", "sha256": sha}),
            )
            report = offdevice.verify_extracted_bundle(bundle)
            self.assertFalse(report["ok"])
            self.assertTrue(
                any("unknown item kind" in p for p in report["problems"]), report["problems"]
            )

    def test_empty_plan_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            self._rewrite_manifest(bundle, lambda m: m.__setitem__("plan", []))
            report = offdevice.verify_extracted_bundle(bundle)
            self.assertFalse(report["ok"])
            self.assertTrue(any("plan is empty" in p for p in report["problems"]), report["problems"])

    @platform_skips.requires_symlinks
    def test_symlinked_manifest_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            real = bundle / "MANIFEST.json"
            moved = bundle / "REAL-MANIFEST.json"
            real.rename(moved)
            os.symlink(moved, real)
            with self.assertRaises(offdevice.BackupError):
                offdevice.verify_extracted_bundle(bundle)

    @platform_skips.requires_chmod_enforcement
    def test_untraversable_dir_fails_verify(self) -> None:
        # A stowaway hidden under a 000-perm dir used to escape os.walk silently.
        with tempfile.TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))
            blocked = bundle / "blocked"
            blocked.mkdir()
            (blocked / "stowaway.txt").write_bytes(b"hidden")
            os.chmod(blocked, 0o000)
            try:
                report = offdevice.verify_extracted_bundle(bundle)
            finally:
                os.chmod(blocked, 0o755)  # restore so tempdir cleanup works
            self.assertFalse(report["ok"])
            self.assertTrue(
                any("traversal failed" in p for p in report["problems"]), report["problems"]
            )

    def test_pre_sentinel_bundle_fails_with_honest_era_attribution(self) -> None:
        # M1: a pre-M4 bundle from a category-quiet night has no sentinels and no
        # matching evidence files. Verify must STILL fail (fail-closed) but must not
        # flatly misattribute it as "producer regression" -- it names the era so an
        # operator does not discard a valid recovery point during DR.
        with tempfile.TemporaryDirectory() as raw:
            bundle = self._bundle(Path(raw))

            def mutate(m: dict) -> None:
                m["created_at"] = "2026-07-01T00:00:00+00:00"
                m["plan"] = [
                    p
                    for p in m["plan"]
                    if not str(p.get("name")).startswith("evidence-category/")
                ]

            self._rewrite_manifest(bundle, mutate)
            report = offdevice.verify_extracted_bundle(bundle)
            self.assertFalse(report["ok"])
            self.assertTrue(
                any("predating the M4 sentinel contract" in p for p in report["problems"]),
                report["problems"],
            )


class ManifestSummaryAndMirrorTests(unittest.TestCase):
    def test_stdout_summary_strips_per_entry_tree_manifests(self) -> None:
        # HIGH-3: per-holdout-file digests of the sealed evals tree must never reach
        # stdout/logs; the summary the report prints carries no `entries`.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            src = root / "evals"
            src.mkdir()
            (src / "holdout.json").write_text('{"secret": 1}', encoding="utf-8")
            tree = offdevice._copy_tree(src, root / "copied-evals")
            self.assertIn("entries", tree)  # the FULL manifest carries entries
            summary = offdevice._manifest_summary({"evals": tree})
            self.assertNotIn("entries", summary["evals"])  # the printed summary does not
            self.assertIn("tree_sha256", summary["evals"])  # aggregate is safe to show
            self.assertIn("files", summary["evals"])

    def test_offdevice_mirror_is_byte_identical(self) -> None:
        # F-10 (upstream the kit): guarded drift between the live scripts/ copy
        # and workspace-tooling/ in the dual-tree Mac layout. This fork has a
        # single source tree and no scripts/ mirror, so the dual-tree defect the
        # guard exists for cannot occur; kept as an explicit skip so the
        # provenance of the removal stays visible.
        self.skipTest("fork has a single source tree; no scripts/ mirror (F-10 n/a)")


def _seed_all_required(root: Path) -> None:
    """Create every hard-required source input under a _FakeRuntime(root)'s path
    layout so _plan() marks all 13 hard items present -- the complete baseline a
    completeness/rotation test then removes ONE item from. The five sqlite-mode
    items must be real databases: build() copies them with the sqlite online-backup
    API, which rejects a non-database file."""
    (root / "skill-events").mkdir(parents=True, exist_ok=True)
    (root / "skill-events" / "r.json").write_text("{}", encoding="utf-8")
    (root / "evals").mkdir(exist_ok=True)
    (root / "evals" / "h.json").write_text("{}", encoding="utf-8")
    (root / "graphiti-pilots").mkdir(exist_ok=True)
    (root / "graphiti-pilots" / "p.json").write_text("{}", encoding="utf-8")
    (root / "outbox").mkdir(exist_ok=True)
    for db_name in (
        "consumer.sqlite3",
        "ingestion-state.sqlite3",
        "artifact-catalog.sqlite3",
        "qdrant-shadow-replay.sqlite3",
        "artifact-unit-archive.sqlite3",
    ):
        conn = sqlite3.connect(str(root / db_name))
        conn.execute("CREATE TABLE IF NOT EXISTS seed(x)")
        conn.commit()
        conn.close()
    (root / "artifact-memory-runtime.json").write_text("{}", encoding="utf-8")
    (root / "qdrant-shadow-verification.json").write_text("{}", encoding="utf-8")
    snaps = root / "services" / "snapshots"
    snaps.mkdir(mode=0o700, parents=True, exist_ok=True)  # validated as a private dir
    (snaps / "snapshot-restore-evidence.json").write_text("{}", encoding="utf-8")
    (snaps / "collection-000.snapshot").write_bytes(b"SNAP")


class BundleCompletenessTests(unittest.TestCase):
    """#3: bundle_complete spans tier-1 AND tier-2, so a missing tier-2 rebuild
    item is an incomplete bundle -- not a silent tier-1-complete exit-0. Empty
    evidence categories never count against it (represent-when-absent)."""

    def test_all_required_present_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _seed_all_required(root)
            items = offdevice._plan(_FakeRuntime(root))
            self.assertEqual(offdevice.missing_required(items), [])
            self.assertTrue(offdevice.bundle_complete(items))

    def test_missing_tier2_item_is_incomplete(self) -> None:
        # A missing tier-2 rebuild-authority item must make the bundle
        # incomplete (pre-fix such a gap left tier1_complete True). Uses
        # artifact-catalog: shadow-replay/unit-archive became represent-when-
        # absent for this fresh-lineage deployment, so they no longer carry
        # this guarantee -- artifact-catalog/outbox/runtime-config/snapshot do.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _seed_all_required(root)
            (root / "artifact-catalog.sqlite3").unlink()
            items = offdevice._plan(_FakeRuntime(root))
            self.assertIn("artifact-catalog", offdevice.missing_required(items))
            self.assertFalse(offdevice.bundle_complete(items))

    def test_absent_fresh_lineage_items_do_not_break_completeness(self) -> None:
        # The mirror of the test above: a deployment that never had an eval
        # suite, a Graphiti pilot, or a migration's shadow-replay/unit-archive
        # is COMPLETE, not perpetually incomplete. Holding these hard withheld
        # rotation on every nightly bundle (2026-07-22).
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _seed_all_required(root)
            for name in ("evals", "graphiti-pilots"):
                shutil.rmtree(root / name)
            for name in (
                "qdrant-shadow-replay.sqlite3",
                "artifact-unit-archive.sqlite3",
                "qdrant-shadow-verification.json",
            ):
                (root / name).unlink()
            (root / "services" / "snapshots" / "snapshot-restore-evidence.json").unlink()
            items = offdevice._plan(_FakeRuntime(root))
            self.assertEqual(offdevice.missing_required(items), [])
            self.assertTrue(offdevice.bundle_complete(items))

    def test_empty_evidence_categories_do_not_break_completeness(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _seed_all_required(root)  # no eval/migration/audit files -> 3 empty sentinels
            items = offdevice._plan(_FakeRuntime(root))
            sentinels = [i for i in items if i["name"].startswith("evidence-category/")]
            self.assertEqual(len(sentinels), 3)
            self.assertTrue(all(not s["present"] for s in sentinels))
            self.assertTrue(offdevice.bundle_complete(items))  # representational, non-failing


class RotationGateTests(unittest.TestCase):
    """#2: rotation must not evict a good recovery point when the NEW bundle is
    incomplete. build() gates BOTH bundle and snapshot rotation on bundle_complete,
    computed before the rotation call (main()'s exit-5 runs too late -- after
    build() has already pruned)."""

    def _run_build(self, root: Path, *, prune_keep: int) -> dict:
        rt = _FakeRuntime(root)
        dest = root / "offdevice-staging"
        dest.mkdir(mode=0o700, parents=True, exist_ok=True)  # ensure_private_directory requires 0700
        for i in range(4):
            (dest / f"artifact-memory-2026070{i}T000000Z.tar.age").write_bytes(b"old")
        snaps = snapshot.snapshot_root(rt)
        for i in range(4):
            (snaps / f"collection-10{i}.snapshot").write_bytes(b"old")
        recipients = root / "recipients.txt"
        recipients.write_text("age1testrecipient\n", encoding="utf-8")
        prod_standin = root / "PROD-standin"
        prod_standin.mkdir()
        with mock.patch.object(
            offdevice.artifact_runtime, "DEFAULT_DERIVED_ROOT", prod_standin
        ), mock.patch.object(
            offdevice,
            "_encrypt",
            return_value={"bytes": 0, "sha256": "stub", "recipients_file": str(recipients)},
        ):
            report = offdevice.build(
                runtime=rt,
                destination=dest,
                recipients_file=recipients,
                apply=True,
                prune_keep=prune_keep,
            )
        # the Sol #6 fix routes health to the runtime root, never the (patched)
        # default -- so no build(apply=True) test can pollute the real derived root.
        self.assertFalse((prod_standin / "artifact-backup-offdevice-health.json").exists())
        return report

    def test_incomplete_run_withholds_all_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _seed_all_required(root)
            shutil.rmtree(root / "skill-events")  # force incomplete (a tier-1 gap)
            report = self._run_build(root, prune_keep=2)
            self.assertFalse(report["bundle_complete"])
            self.assertIn("skipped", report["bundle_rotation"])
            self.assertIn("skipped", report["snapshot_rotation"])
            dest = root / "offdevice-staging"
            snaps = snapshot.snapshot_root(_FakeRuntime(root))
            self.assertEqual(len(list(dest.glob("artifact-memory-*.tar.age"))), 4)  # 0 pruned
            self.assertEqual(len(list(snaps.glob("*.snapshot"))), 5)  # 0 pruned (1 seeded + 4)

    def test_complete_run_rotates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _seed_all_required(root)
            report = self._run_build(root, prune_keep=2)
            self.assertTrue(report["bundle_complete"], report.get("incomplete_items"))
            self.assertIn("pruned", report["bundle_rotation"])
            self.assertIn("pruned", report["snapshot_rotation"])
            dest = root / "offdevice-staging"
            self.assertEqual(len(list(dest.glob("artifact-memory-*.tar.age"))), 2)  # kept newest 2

    def test_quiet_night_reports_complete_and_denoised(self) -> None:
        # H1/L1: an evaluation-quiet night (every hard item present, all three
        # categories empty) must NOT flip tier1_complete false, must NOT list the
        # sentinels in report["missing"], and reports them under empty_categories.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _seed_all_required(root)  # no eval/migration/audit files -> 3 empty categories
            report = self._run_build(root, prune_keep=2)
            self.assertTrue(report["bundle_complete"])
            self.assertTrue(report["tier1_complete"])  # H1: NOT flipped false by sentinels
            self.assertNotIn("tier1_absent", report)
            self.assertEqual(report["missing"], [])  # L1: sentinels de-noised
            # Only the categories ACTUALLY empty in this fixture are reported.
            # _REPRESENTATIONAL also carries the fresh-lineage items (evals,
            # graphiti-pilots, shadow-replay, unit-archive, the two evidence
            # JSONs), which _seed_all_required DOES create — so they are present
            # here and must not be listed as empty.
            self.assertEqual(
                sorted(report["empty_categories"]),
                sorted(
                    name
                    for name in offdevice._REPRESENTATIONAL
                    if name.startswith("evidence-category/")
                ),
            )
            self.assertTrue(
                set(report["empty_categories"]) <= set(offdevice._REPRESENTATIONAL)
            )
            self.assertIn("pruned", report["bundle_rotation"])  # complete -> rotation runs


class HermeticHealthPathTests(unittest.TestCase):
    """#6: the health watermark is written under the RUNTIME's root, so a
    non-default --config apply cannot stamp the production health file. Covers the
    off-device writer AND the folded-in snapshot writer twin."""

    def test_offdevice_health_under_runtime_root_not_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            isolated = Path(raw) / "isolated"
            isolated.mkdir(mode=0o700)  # the derived root must be a private dir
            prod = Path(raw) / "prod-standin"
            prod.mkdir(mode=0o700)
            _seed_all_required(isolated)
            rt = _FakeRuntime(isolated)  # config_path.parent == isolated
            dest = isolated / "staging"
            dest.mkdir(mode=0o700)  # ensure_private_directory requires 0700
            recipients = isolated / "r.txt"
            recipients.write_text("age1x\n", encoding="utf-8")
            with mock.patch.object(
                offdevice.artifact_runtime, "DEFAULT_DERIVED_ROOT", prod
            ), mock.patch.object(
                offdevice,
                "_encrypt",
                return_value={"bytes": 0, "sha256": "s", "recipients_file": str(recipients)},
            ):
                offdevice.build(
                    runtime=rt, destination=dest, recipients_file=recipients, apply=True
                )
            self.assertTrue((isolated / "artifact-backup-offdevice-health.json").exists())
            self.assertFalse((prod / "artifact-backup-offdevice-health.json").exists())

    def test_snapshot_health_under_runtime_root_not_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            isolated = Path(raw) / "isolated"
            isolated.mkdir(mode=0o700)  # the derived root must be a private dir
            prod = Path(raw) / "prod-standin"
            prod.mkdir(mode=0o700)
            (isolated / "services" / "snapshots").mkdir(mode=0o700, parents=True)
            rt = _FakeRuntime(isolated)
            with mock.patch.object(
                snapshot.artifact_runtime, "DEFAULT_DERIVED_ROOT", prod
            ), mock.patch.object(
                snapshot.artifact_runtime, "load_runtime", return_value=rt
            ):
                snapshot.run(config=isolated / "cfg.json", check_only=True)
            self.assertTrue((isolated / "artifact-backup-snapshot-health.json").exists())
            self.assertFalse((prod / "artifact-backup-snapshot-health.json").exists())


class MainExitIntegrationTests(unittest.TestCase):
    """V-M2: the existing exit-code tests all mock build() wholesale, so the
    build->main integration (where H1 broke) was unpinned. These drive main()
    through the REAL build() end-to-end."""

    def _run_main(self, root: Path, *, remove: str | None = None) -> int:
        _seed_all_required(root)
        if remove is not None:
            target = root / remove
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        dest = root / "staging"
        dest.mkdir(mode=0o700)  # ensure_private_directory requires 0700
        recipients = root / "r.txt"
        recipients.write_text("age1x\n", encoding="utf-8")
        prod = root / "PROD-standin"
        prod.mkdir(mode=0o700)
        with mock.patch.object(
            offdevice.artifact_runtime, "DEFAULT_DERIVED_ROOT", prod
        ), mock.patch.object(
            offdevice.artifact_runtime, "load_runtime", return_value=_FakeRuntime(root)
        ), mock.patch.object(
            offdevice,
            "_encrypt",
            return_value={"bytes": 0, "sha256": "s", "recipients_file": str(recipients)},
        ):
            return offdevice.main(
                ["--destination", str(dest), "--apply", "--recipients-file", str(recipients)]
            )

    def test_quiet_night_exits_zero(self) -> None:
        # H1: empty evidence categories are NOT a failure -- pre-fix this exited 5.
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(self._run_main(Path(raw)), 0)

    def test_tier2_gap_exits_five(self) -> None:
        # artifact-catalog, not unit-archive: see the completeness test above.
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(
                self._run_main(Path(raw), remove="artifact-catalog.sqlite3"), 5
            )

    def test_tier1_gap_exits_five(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(self._run_main(Path(raw), remove="skill-events"), 5)


class OffDeviceStatusReaderTests(unittest.TestCase):
    """M2: the status surface (`artifact_memory._retention_status`) must reflect
    bundle_complete (full tier-1+tier-2), not tier1_complete alone -- else a
    tier-2-only gap (Sol #3) reports healthy on the dashboard for a full RPO window."""

    def _off_device(self, health: dict) -> dict:
        with tempfile.TemporaryDirectory() as raw:
            hp = Path(raw) / "offdevice-health.json"
            health.setdefault("bundle", str(hp))
            hp.write_text(json.dumps(health), encoding="utf-8")
            with mock.patch.object(artifact_memory, "OFFDEVICE_HEALTH", hp), mock.patch.object(
                artifact_memory, "SNAPSHOT_HEALTH", Path(raw) / "absent.json"
            ):
                return artifact_memory._retention_status(_FakeRuntime(Path(raw)))["off_device"]

    def test_tier2_gap_reports_incomplete(self) -> None:
        od = self._off_device(
            {
                "tier1_complete": True,
                "bundle_complete": False,
                "incomplete_items": ["unit-archive"],
                "completed_at": "2026-07-20T00:00:00+00:00",
                "last_run_at": "2026-07-20T00:00:00+00:00",
                "absent_items": [],
            }
        )
        self.assertTrue(od["last_run_incomplete"])
        self.assertIn("unit-archive", od.get("incomplete_items", []))

    def test_complete_run_reports_healthy(self) -> None:
        od = self._off_device(
            {
                "tier1_complete": True,
                "bundle_complete": True,
                "incomplete_items": [],
                "completed_at": "2026-07-20T00:00:00+00:00",
                "last_run_at": "2026-07-20T00:00:00+00:00",
                "absent_items": [],
            }
        )
        self.assertFalse(od["last_run_incomplete"])

    def test_pre_m4_watermark_falls_back_to_tier1(self) -> None:
        # A watermark written before bundle_complete existed keys off tier1_complete.
        od = self._off_device(
            {
                "tier1_complete": False,
                "completed_at": None,
                "last_run_at": "2026-07-20T00:00:00+00:00",
                "absent_items": ["evals"],
            }
        )
        self.assertTrue(od["last_run_incomplete"])


class EvalCustodyStagingTests(unittest.TestCase):
    """Sol #1: the plaintext staging (copy tree + tar, including the sealed evals)
    is SOURCE-LOCAL; only the encrypted `.tar.age` reaches the destination -- so a
    non-default/external destination never sees plaintext holdout data."""

    def test_no_plaintext_on_destination_during_encrypt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _seed_all_required(root)
            (root / "evals" / "holdout.json").write_text('{"sealed": 1}', encoding="utf-8")
            dest = root / "external-dest"
            dest.mkdir(mode=0o700)
            recipients = root / "r.txt"
            recipients.write_text("age1x\n", encoding="utf-8")
            seen: dict = {}

            def inspect(archive, bundle, recipients_file):
                # captured AT the moment of encryption, before any .tar.age exists:
                # the plaintext tar must be source-local (under the derived root) and
                # NOT on the destination.
                seen["archive_under_dest"] = Path(archive).resolve().is_relative_to(
                    dest.resolve()
                )
                # V-M2: positively pin "on the derived root" -- a regression to
                # mkdtemp(dir=None) (system /tmp) would otherwise keep this green
                # while voiding the stated custody property.
                seen["archive_under_root"] = Path(archive).resolve().is_relative_to(
                    root.resolve()
                )
                seen["dest_plaintext"] = [
                    p.name for p in dest.rglob("*") if not p.name.endswith(".tar.age")
                ]
                return {"bytes": 0, "sha256": "stub", "recipients_file": str(recipients_file)}

            with mock.patch.object(
                offdevice.artifact_runtime, "DEFAULT_DERIVED_ROOT", root / "PROD"
            ), mock.patch.object(offdevice, "_encrypt", side_effect=inspect):
                (root / "PROD").mkdir(mode=0o700)
                report = offdevice.build(
                    runtime=_FakeRuntime(root),
                    destination=dest,
                    recipients_file=recipients,
                    apply=True,
                )
            self.assertFalse(
                seen["archive_under_dest"], "plaintext tar was staged on the destination"
            )
            self.assertTrue(
                seen["archive_under_root"], "staging is not under the derived root"
            )
            self.assertEqual(
                seen["dest_plaintext"],
                [],
                f"plaintext staged on the destination during encrypt: {seen['dest_plaintext']}",
            )
            # after build returns (mocked _encrypt writes nothing), the destination
            # must hold no residue at all -- any file is a destination write leak.
            self.assertEqual([p.name for p in dest.rglob("*")], [])
            self.assertTrue(str(report["bundle"]).startswith(str(dest)))  # bundle targets dest

    def _build_raises(self, root, dest, recipients, extra_patches=(), **build_kw):
        """Run build(apply=True) with the standard stubs, expecting a BackupError;
        return the exception so the caller can assert on its message + side-effects."""
        (root / "PROD").mkdir(mode=0o700, exist_ok=True)
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    offdevice.artifact_runtime, "DEFAULT_DERIVED_ROOT", root / "PROD"
                )
            )
            stack.enter_context(
                mock.patch.object(
                    offdevice,
                    "_encrypt",
                    return_value={"bytes": 0, "sha256": "s", "recipients_file": "r"},
                )
            )
            for patch in extra_patches:
                stack.enter_context(patch)
            with self.assertRaises(offdevice.BackupError) as ctx:
                offdevice.build(
                    runtime=_FakeRuntime(root),
                    destination=dest,
                    recipients_file=recipients,
                    apply=True,
                    **build_kw,
                )
        return ctx.exception

    @platform_skips.requires_symlinks
    def test_build_refuses_a_source_tree_symlink(self) -> None:
        # Sol #7 (owner decision: refuse at build): derived state must contain no
        # symlinks; build() fails loudly BEFORE any watermark/rotation (V-M3 pins the
        # three non-mutation side-effects), rather than create a bundle that restore
        # --repair/--sweep would reject.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _seed_all_required(root)
            os.symlink("h.json", root / "evals" / "sneaky-link")  # symlink in a backed-up tree
            dest = root / "dest"
            dest.mkdir(mode=0o700)
            old_bundle = dest / "artifact-memory-20260701T000000Z.tar.age"
            old_bundle.write_bytes(b"old")  # a good recovery point rotation must NOT evict
            recipients = root / "r.txt"
            recipients.write_text("age1x\n", encoding="utf-8")
            exc = self._build_raises(root, dest, recipients, prune_keep=1)
            self.assertIn("symlink", str(exc).lower())
            self.assertFalse(
                (root / "artifact-backup-offdevice-health.json").exists()
            )  # RPO clock never stamped
            self.assertTrue(old_bundle.exists())  # rotation never ran
            self.assertEqual(
                list((root / ".offdevice-build-staging").rglob("*")), []
            )  # staging cleaned

    @platform_skips.requires_symlinks
    def test_build_refuses_nested_and_second_tree_symlinks(self) -> None:
        # Sol #7 genericity (L2): fires for a symlink at ANY depth in ANY of the four
        # backed-up trees, not just a top-level evals link.
        for tree, rel in (("outbox", "a/b/deep-link"), ("skill-events", "nested/link")):
            with self.subTest(tree=tree):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    _seed_all_required(root)
                    (root / tree / rel).parent.mkdir(parents=True, exist_ok=True)
                    os.symlink("target", root / tree / rel)
                    dest = root / "dest"
                    dest.mkdir(mode=0o700)
                    recipients = root / "r.txt"
                    recipients.write_text("age1x\n", encoding="utf-8")
                    exc = self._build_raises(root, dest, recipients)
                    self.assertIn("symlink", str(exc).lower())

    @platform_skips.requires_symlinks
    def test_build_refuses_a_symlinked_plan_item_path(self) -> None:
        # V-M4: a plan-item PATH that is ITSELF a symlink (here the required evidence
        # file) is silently dereferenced by copy2/copytree pre-fix; build() now
        # refuses it, closing Sol #7 to its stated "derived state must contain none".
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _seed_all_required(root)
            real = root / "real-verification.json"
            real.write_text("{}", encoding="utf-8")
            target = root / "qdrant-shadow-verification.json"
            target.unlink()
            os.symlink(real, target)  # the plan item's own path is now a symlink
            dest = root / "dest"
            dest.mkdir(mode=0o700)
            recipients = root / "r.txt"
            recipients.write_text("age1x\n", encoding="utf-8")
            exc = self._build_raises(root, dest, recipients)
            self.assertIn("symlink", str(exc).lower())

    def test_build_refuses_when_source_volume_lacks_space(self) -> None:
        # M1/V-L2: an external-destination run fails CLEAN when the source volume
        # cannot hold the ~2x-plaintext-set staging, instead of ENOSPC mid-copy.
        Usage = collections.namedtuple("Usage", "total used free")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _seed_all_required(root)
            dest = root / "dest"
            dest.mkdir(mode=0o700)
            recipients = root / "r.txt"
            recipients.write_text("age1x\n", encoding="utf-8")
            exc = self._build_raises(
                root,
                dest,
                recipients,
                extra_patches=[
                    mock.patch.object(
                        offdevice.shutil, "disk_usage", return_value=Usage(100, 99, 1)
                    )
                ],
            )
            self.assertIn("insufficient source-volume space", str(exc).lower())
            self.assertEqual(  # failed BEFORE staging anything
                list((root / ".offdevice-build-staging").glob("personal-backup-*")), []
            )

    def test_build_reclaims_stale_crash_staging(self) -> None:
        # L1/V-M1: a personal-backup-* dir stranded by a prior hard crash (>24h old) is
        # swept on the next build; a recent one is left alone.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _seed_all_required(root)
            staging_root = root / ".offdevice-build-staging"
            staging_root.mkdir(mode=0o700)
            stale = staging_root / "personal-backup-STALE"
            stale.mkdir(mode=0o700)
            (stale / "plaintext.bin").write_bytes(b"sealed-eval-copy")
            os.utime(stale, (1000000000.0, 1000000000.0))  # 2001 -- far older than 24h
            fresh = staging_root / "personal-backup-FRESH"
            fresh.mkdir(mode=0o700)
            dest = root / "dest"
            dest.mkdir(mode=0o700)
            recipients = root / "r.txt"
            recipients.write_text("age1x\n", encoding="utf-8")
            (root / "PROD").mkdir(mode=0o700)
            with mock.patch.object(
                offdevice.artifact_runtime, "DEFAULT_DERIVED_ROOT", root / "PROD"
            ), mock.patch.object(
                offdevice,
                "_encrypt",
                return_value={"bytes": 0, "sha256": "s", "recipients_file": "r"},
            ):
                report = offdevice.build(
                    runtime=_FakeRuntime(root),
                    destination=dest,
                    recipients_file=recipients,
                    apply=True,
                )
            self.assertFalse(stale.exists(), "stale crash-staging was not reclaimed")
            self.assertTrue(fresh.exists(), "a recent staging dir must not be swept")
            self.assertEqual(report.get("reclaimed_stale_staging"), 1)

    def test_encrypt_removes_partial_ciphertext_on_failure(self) -> None:
        # V-L1/M2: a failed age run must not leave a truncated .tar.age that rotation
        # could keep as a recovery point.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "in.tar"
            archive.write_bytes(b"PLAINTEXT")
            out = root / "out.tar.age"
            recipients = root / "r.txt"
            recipients.write_text("age1x\n", encoding="utf-8")

            def fake_run(cmd, **kwargs):
                # age writes a partial output, then exits non-zero
                Path(cmd[cmd.index("--output") + 1]).write_bytes(b"PARTIAL")
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

            with mock.patch.object(offdevice, "AGE", str(archive)), mock.patch.object(
                offdevice.subprocess, "run", side_effect=fake_run
            ):
                with self.assertRaises(offdevice.BackupError):
                    offdevice._encrypt(archive, out, recipients)
            self.assertFalse(out.exists(), "partial ciphertext was left on failure")


class _FakeRuntime:
    """Minimal stand-in; the bundle plan only reads paths and identifiers."""

    def __init__(self, root: Path) -> None:
        self.config_path = root / "artifact-memory-runtime.json"
        self.catalog = root / "artifact-catalog.sqlite3"
        self.outbox_root = root / "outbox"
        self.ingestion_state = root / "ingestion-state.sqlite3"
        self.consumer_state = root / "consumer.sqlite3"
        self.receipt_root = root / "skill-events"
        self.qdrant_admin_key_file = root / "services" / "admin-api-key"
        self.qdrant_read_key_file = root / "services" / "read-only-api-key"
        self.qdrant_collection = "test_collection"
        self.qdrant_generation = "gtest"
        self.rollback_until = "2026-08-17T05:28:30.320460+00:00"


def assess_level(root: Path) -> str:
    return str(snapshot.assess_retention(root)["level"])


if __name__ == "__main__":
    unittest.main()
