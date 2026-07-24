from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import artifact_outbox_retention as retention  # noqa: E402
import artifact_unit_archive as archive_mod  # noqa: E402


TARGET = "url:test-collection"
TARGET_LIKE = "url:%test-collection%"

REV_FROZEN = "revision:frozen"
REV_CURRENT = "revision:current"
REV_ORPHANED = "revision:orphaned"      # serving, superseded, outbox-only
REV_LOST = "revision:lost"              # serving, superseded, in NO outbox


class UnitArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.outbox = self.root / "outbox"
        self.outbox.mkdir()
        self.catalog = self.root / "catalog.sqlite3"
        self.ingestion = self.root / "ingestion.sqlite3"
        self.replay = self.root / "replay.sqlite3"
        self.archive = self.root / "archive.sqlite3"
        self.consumer = self.root / "consumer.sqlite3"

        self.holder = self._outbox_dir("catalog-run-2-chunks-v2", [REV_ORPHANED])
        self._outbox_dir("catalog-run-3-chunks-v2", [REV_FROZEN])

        self._sqlite(
            self.catalog,
            "CREATE TABLE current_artifact_revisions (revision_id TEXT)",
            "INSERT INTO current_artifact_revisions VALUES (?)",
            [(REV_CURRENT,)],
        )
        self._sqlite(
            self.ingestion,
            "CREATE TABLE sink_units (sink TEXT, target TEXT, unit_id TEXT, "
            "revision_id TEXT, status TEXT)",
            "INSERT INTO sink_units VALUES ('qdrant', ?, ?, ?, 'completed')",
            # unit_ids must match what the outbox actually renders, because
            # attestation compares the archived set against the SERVING set.
            [
                (TARGET, "catalog-run-2-chunks-v2:0", REV_ORPHANED),
                (TARGET, "catalog-run-3-chunks-v2:0", REV_FROZEN),
                (TARGET, "u-current", REV_CURRENT),
                (TARGET, "u-lost", REV_LOST),
            ],
        )
        self._sqlite(
            self.replay,
            "CREATE TABLE selected_units (unit_id TEXT, revision_id TEXT, "
            "source_outbox TEXT, unit_json TEXT)",
            "INSERT INTO selected_units VALUES (?, ?, ?, '{}')",
            [("s0", REV_FROZEN, str(self.root / "outbox/catalog-run-3-chunks-v2"))],
        )
        self._sqlite(
            self.consumer,
            "CREATE TABLE consumer_events (event_id TEXT, outbox_path TEXT)",
            "INSERT INTO consumer_events VALUES (?, ?)",
            [],
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _sqlite(self, path: Path, ddl: str, insert: str, rows: list) -> None:
        with sqlite3.connect(path) as conn:
            conn.execute(ddl)
            if rows:
                conn.executemany(insert, rows)

    def _outbox_dir(
        self, name: str, revisions: list[str], *, complete: bool = True
    ) -> Path:
        """Build a REAL outbox: the archive now reads through the verified
        reader, which enforces the outbox schema and recomputes the units
        digest against the manifest. A fixture with a made-up units_sha256
        would simply be refused — and would silently stop testing anything."""
        path = self.outbox / name
        path.mkdir()
        digest = hashlib.sha256()
        raw = b""
        for index, revision in enumerate(revisions):
            line = (
                json.dumps(
                    {
                        "schema_version": 2,
                        "unit_id": f"{name}:{index}",
                        "revision_id": revision,
                        "qdrant_point_id": f"{name}-p{index}",
                        "embedding_text": "x",
                    },
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                + "\n"
            ).encode("utf-8")
            raw += line
            digest.update(line)
        with gzip.open(path / "ingest-units.jsonl.gz", "wb") as fh:
            fh.write(raw)
        manifest = {
            "schema_version": 1,
            "outbox_schema_version": 2,
            "units_file": "ingest-units.jsonl.gz",
            "units_sha256": digest.hexdigest(),
            "catalog_run_id": 7,
            "complete": complete,
        }
        (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        # Real outboxes are written under a private umask and the verified
        # reader ENFORCES 0700/0600; a 0755 fixture is simply refused.
        for entry in path.iterdir():
            os.chmod(entry, 0o600)
        os.chmod(path, 0o700)
        return path

    def kwargs(self, **overrides):
        base = {
            "outbox_root": self.outbox,
            "catalog": self.catalog,
            "ingestion_state": self.ingestion,
            "replay": self.replay,
            "archive": self.archive,
            "target_like": TARGET_LIKE,
        }
        base.update(overrides)
        return base

    def status_kwargs(self, **overrides):
        """status() reads databases only — it never walks the outbox."""
        base = self.kwargs(**overrides)
        base.pop("outbox_root", None)
        return base

    # --- status ---------------------------------------------------------------

    def test_status_counts_the_unrepresented_set(self) -> None:
        payload = archive_mod.status(**self.status_kwargs())
        self.assertFalse(payload["archive_exists"])
        # orphaned + lost are serving but neither frozen nor current
        self.assertEqual(payload["unrepresented"], 2)
        self.assertEqual(payload["covered_by_archive"], 0)

    # --- backfill -------------------------------------------------------------

    def test_plan_mode_writes_nothing(self) -> None:
        payload = archive_mod.backfill(**self.kwargs(), apply=False)
        self.assertEqual(payload["mode"], "plan")
        self.assertFalse(self.archive.exists())

    def test_apply_archives_and_clears_the_representable_set(self) -> None:
        payload = archive_mod.backfill(**self.kwargs(), apply=True)
        self.assertEqual(payload["mode"], "apply")
        self.assertTrue(self.archive.exists())
        self.assertEqual(payload["units_archived"], 1)
        self.assertEqual(payload["revisions_archived"], 1)
        after = archive_mod.status(**self.status_kwargs())
        # only the genuinely lost revision remains unrepresented
        self.assertEqual(after["unrepresented"], 1)
        self.assertEqual(after["covered_by_archive"], 1)

    def test_revisions_in_no_outbox_are_surfaced_not_silently_dropped(self) -> None:
        payload = archive_mod.backfill(**self.kwargs(), apply=True)
        self.assertEqual(payload["unrecoverable_count"], 1)
        self.assertIn(REV_LOST, payload["unrecoverable"])

    def test_backfill_is_idempotent(self) -> None:
        archive_mod.backfill(**self.kwargs(), apply=True)
        second = archive_mod.backfill(**self.kwargs(), apply=True)
        # nothing left to do for the already-archived revision
        self.assertEqual(second["units_archived"], 0)
        with sqlite3.connect(self.archive) as conn:
            total = conn.execute("SELECT COUNT(*) FROM archived_units").fetchone()[0]
        self.assertEqual(total, 1)

    def test_archived_row_records_its_provenance(self) -> None:
        archive_mod.backfill(**self.kwargs(), apply=True)
        with sqlite3.connect(self.archive) as conn:
            row = conn.execute(
                "SELECT revision_id, source_outbox, source_units_sha256, "
                "catalog_run_id, unit_json FROM archived_units"
            ).fetchone()
        self.assertEqual(row[0], REV_ORPHANED)
        self.assertEqual(row[1], "catalog-run-2-chunks-v2")
        self.assertRegex(str(row[2]), r"^[0-9a-f]{64}$")  # real manifest digest
        self.assertEqual(row[3], 7)
        self.assertEqual(json.loads(row[4])["revision_id"], REV_ORPHANED)

    # --- guard rails ----------------------------------------------------------

    def test_refuses_to_write_into_the_frozen_replay_set(self) -> None:
        # Appending would invalidate the digests ADR-002 relies on.
        with self.assertRaises(archive_mod.ArchiveError):
            archive_mod.open_archive(archive_mod.DEFAULT_REPLAY, create=True)

    def test_refuses_to_archive_on_incomplete_inputs(self) -> None:
        with self.assertRaises(archive_mod.ArchiveError):
            archive_mod.backfill(
                **self.kwargs(replay=self.root / "missing.sqlite3"), apply=True
            )

    def test_interrupted_render_is_never_an_archive_source(self) -> None:
        # sorted() places .tmp-* FIRST, so an interrupted render is the first
        # candidate source for every revision it contains. This fixture is a
        # fully VALID, complete outbox that differs only by name -- a real
        # interrupted render crashes after the manifest is written, so
        # `complete: true` is present and ONLY the name check catches it. An
        # earlier version of this test used a manifest-less directory and so
        # passed via the "no manifest" branch whether or not the name check
        # existed: it did not pin the guard it was named for.
        tmp = self._outbox_dir(
            ".tmp-catalog-run-2-chunks-v2-999-deadbeef", [REV_ORPHANED]
        )
        payload = archive_mod.backfill(**self.kwargs(), apply=True)
        self.assertNotIn(tmp.name, {s["outbox"] for s in payload["sources"]})
        skipped = {s["outbox"]: s["reason"] for s in payload["skipped_sources"]}
        self.assertIn(tmp.name, skipped)
        self.assertIn("interrupted render", skipped[tmp.name])
        with sqlite3.connect(self.archive) as conn:
            origins = {
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT source_outbox FROM archived_units"
                )
            }
        self.assertEqual(origins, {"catalog-run-2-chunks-v2"})

    def test_outbox_not_asserting_complete_is_skipped(self) -> None:
        (self.holder / "manifest.json").write_text(
            json.dumps({"catalog_run_id": 7, "complete": False}), encoding="utf-8"
        )
        payload = archive_mod.backfill(**self.kwargs(), apply=True)
        self.assertEqual(payload["units_archived"], 0)
        self.assertIn(
            "catalog-run-2-chunks-v2",
            {s["outbox"] for s in payload["skipped_sources"]},
        )
        # and the revision is honestly reported as still unrepresented
        self.assertIn(REV_ORPHANED, payload["unrecoverable"])

    def test_declares_it_is_not_replay_authority(self) -> None:
        payload = archive_mod.backfill(**self.kwargs(), apply=False)
        self.assertIn("NOT replay authority", payload["authority"])

    # --- integration with the retention classifier -----------------------------

    def test_archiving_unpins_the_directory_that_held_the_revision(self) -> None:
        def classify():
            return retention.classify(
                outbox_root=self.outbox,
                catalog=self.catalog,
                ingestion_state=self.ingestion,
                consumer_state=self.consumer,
                replay=self.replay,
                archive=self.archive,
                target_like=TARGET_LIKE,
                scan=True,
            )

        before = classify()
        holder = next(
            e for e in before["entries"] if e["name"] == "catalog-run-2-chunks-v2"
        )
        self.assertEqual(holder["verdict"], retention.VERDICT_RETAIN)
        self.assertEqual(holder["irreproducible_held"], 1)

        archive_mod.backfill(**self.kwargs(), apply=True)

        after = classify()
        holder_after = next(
            e for e in after["entries"] if e["name"] == "catalog-run-2-chunks-v2"
        )
        self.assertEqual(holder_after["verdict"], retention.VERDICT_PRUNABLE)
        self.assertEqual(holder_after["irreproducible_held"], 0)
        self.assertEqual(after["revisions"]["unit_archive"], 1)

    # --- V-C1 / H1: partial archives must never count as coverage -------------

    def test_truncated_source_commits_nothing_and_grants_no_coverage(self) -> None:
        # THE critical regression. A truncated units file used to leave already
        # -executed INSERTs behind, which the final commit persisted; the next
        # run then saw the revision as covered and the classifier un-pinned the
        # directory holding the ONLY complete copy -- a forged durability proof
        # authorising destruction of the last good bytes.
        units = self.holder / "ingest-units.jsonl.gz"
        payload = units.read_bytes()
        units.write_bytes(payload[: len(payload) // 2])   # corrupt tail
        os.chmod(units, 0o600)

        result = archive_mod.backfill(**self.kwargs(), apply=True)
        self.assertTrue(
            any("error" in entry for entry in result["sources"]), result["sources"]
        )
        with sqlite3.connect(self.archive) as conn:
            units_rows = conn.execute("SELECT COUNT(*) FROM archived_units").fetchone()[0]
            attested = conn.execute(
                "SELECT COUNT(*) FROM archived_revisions"
            ).fetchone()[0]
        self.assertEqual(units_rows, 0, "partial rows must be rolled back")
        self.assertEqual(attested, 0)
        # and every downstream surface still treats the revision as at risk
        self.assertIn(REV_ORPHANED, result["unrecoverable"])
        after = archive_mod.status(**self.status_kwargs())
        self.assertEqual(after["covered_by_archive"], 0)

    def test_wrong_chunk_profile_does_not_attest_the_revision(self) -> None:
        # Live outboxes mix chunk profiles (catalog 5000 vs event 4000) so the
        # same revision renders under DIFFERENT unit_ids. Archiving the wrong
        # profile preserves units that are not the ones being served.
        other = self._outbox_dir("catalog-run-1-chunks-v2", [REV_LOST])
        self.assertTrue(other.exists())
        result = archive_mod.backfill(**self.kwargs(), apply=True)
        # REV_LOST's serving unit_id is "u-lost"; the outbox renders it as
        # "catalog-run-1-chunks-v2:0", so parity fails and it is NOT attested.
        self.assertIn(REV_LOST, result["unrecoverable"])
        with sqlite3.connect(self.archive) as conn:
            attested = {
                r[0] for r in conn.execute("SELECT revision_id FROM archived_revisions")
            }
        self.assertNotIn(REV_LOST, attested)

    # --- V-M1 / M1: the guard protects the stores, not one pathname -----------

    def test_refuses_when_archive_targets_a_passed_precious_path(self) -> None:
        for target in ("replay", "catalog", "ingestion_state"):
            with self.subTest(target=target):
                with self.assertRaises(archive_mod.ArchiveError):
                    archive_mod.backfill(
                        **self.kwargs(archive=self.kwargs()[target]), apply=True
                    )

    def test_refuses_to_initialise_schema_inside_a_foreign_database(self) -> None:
        with self.assertRaises(archive_mod.ArchiveError):
            archive_mod.open_archive(self.catalog, create=True)

    # --- V-M2: unreadable archive is a problem, not "nothing archived" --------

    def test_corrupt_archive_fails_closed_everywhere(self) -> None:
        self.archive.write_bytes(b"this is not a sqlite database")
        payload = archive_mod.status(**self.status_kwargs())
        self.assertFalse(payload["inputs_complete"])
        with self.assertRaises(archive_mod.ArchiveError):
            archive_mod.backfill(**self.kwargs(), apply=True)
        # the retention classifier must also refuse to call anything prunable
        report = retention.classify(
            outbox_root=self.outbox,
            catalog=self.catalog,
            ingestion_state=self.ingestion,
            consumer_state=self.consumer,
            replay=self.replay,
            archive=self.archive,
            target_like=TARGET_LIKE,
            scan=True,
        )
        self.assertFalse(report["inputs_complete"])
        self.assertEqual(report["totals"]["prunable"]["dirs"], 0)

    # --- V-C1 (classifier side) + H2: coverage recomputed, never forged --------

    def test_classifier_does_not_unpin_on_wrong_profile_only_archive(self) -> None:
        # THE classifier-side CRITICAL regression. A wrong-profile / partial row
        # for REV_ORPHANED lands in archived_units but is NEVER attested (its
        # unit_id is not the served one). The classifier must keep the directory
        # holding the real payload RETAINed -- reading raw archived_units
        # row-existence forged coverage and marked it PRUNABLE.
        connection = archive_mod.open_archive(self.archive, create=True)
        try:
            connection.execute(
                "INSERT INTO archived_units(unit_id, revision_id, source_outbox, "
                "archived_at, unit_json) VALUES (?, ?, ?, ?, ?)",
                (
                    "wrong-profile-unit",
                    REV_ORPHANED,
                    "catalog-run-9-chunks-v2",
                    "2026-01-01T00:00:00Z",
                    "{}",
                ),
            )
            connection.commit()
        finally:
            connection.close()
        with sqlite3.connect(self.archive) as conn:
            attested = conn.execute(
                "SELECT COUNT(*) FROM archived_revisions"
            ).fetchone()[0]
        self.assertEqual(attested, 0, "wrong-profile row must not attest")

        report = retention.classify(
            outbox_root=self.outbox,
            catalog=self.catalog,
            ingestion_state=self.ingestion,
            consumer_state=self.consumer,
            replay=self.replay,
            archive=self.archive,
            target_like=TARGET_LIKE,
            scan=True,
        )
        holder = next(
            e for e in report["entries"] if e["name"] == "catalog-run-2-chunks-v2"
        )
        self.assertEqual(holder["verdict"], retention.VERDICT_RETAIN)
        self.assertEqual(holder["irreproducible_held"], 1)
        # the archive contributes ZERO real coverage despite holding a row
        self.assertEqual(report["revisions"]["unit_archive"], 0)

    def test_grown_serving_set_re_evaluates_prior_coverage(self) -> None:
        # H2 regression. After REV_ORPHANED is fully archived and covered, the
        # sink serves an ADDITIONAL unit for it whose bytes are not archived.
        # Coverage is recomputed from live state, so the revision is no longer
        # covered -- a once-valid attestation cannot linger and un-pin new bytes.
        archive_mod.backfill(**self.kwargs(), apply=True)
        covered, ok = archive_mod.covered_revisions(
            self.archive, self.ingestion, TARGET_LIKE, {REV_ORPHANED}
        )
        self.assertTrue(ok)
        self.assertIn(REV_ORPHANED, covered)

        with sqlite3.connect(self.ingestion) as conn:
            conn.execute(
                "INSERT INTO sink_units VALUES ('qdrant', ?, ?, ?, 'completed')",
                (TARGET, "u-extra-for-orphan", REV_ORPHANED),
            )
        covered_after, ok_after = archive_mod.covered_revisions(
            self.archive, self.ingestion, TARGET_LIKE, {REV_ORPHANED}
        )
        self.assertTrue(ok_after)
        self.assertNotIn(REV_ORPHANED, covered_after)
        after = archive_mod.status(**self.status_kwargs())
        self.assertEqual(after["covered_by_archive"], 0)

    def test_source_failing_after_insert_rolls_back_its_units(self) -> None:
        # SAVEPOINT guard. The existing truncated-source test fails BEFORE any
        # row is inserted (0 units yielded before EOF), so it never exercises the
        # post-insert rollback. Here the gzip is intact -- the unit is yielded
        # and INSERTed -- and the manifest digest is wrong, so the verified
        # reader raises AFTER the yield. Only ROLLBACK TO SAVEPOINT keeps the
        # store clean; deleting it persists the row.
        manifest_path = self.holder / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["units_sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(manifest_path, 0o600)

        result = archive_mod.backfill(**self.kwargs(), apply=True)
        self.assertTrue(
            any("error" in entry for entry in result["sources"]), result["sources"]
        )
        with sqlite3.connect(self.archive) as conn:
            units_rows = conn.execute(
                "SELECT COUNT(*) FROM archived_units"
            ).fetchone()[0]
            attested = conn.execute(
                "SELECT COUNT(*) FROM archived_revisions"
            ).fetchone()[0]
        self.assertEqual(units_rows, 0, "post-insert rows must be rolled back")
        self.assertEqual(attested, 0)
        self.assertIn(REV_ORPHANED, result["unrecoverable"])

    # --- Sol #5: coverage validates the PAYLOAD, not just unit_id membership ----

    def test_corrupted_archived_payload_does_not_forge_coverage(self) -> None:
        # The reviewer's exact scenario: attesting on unit_id MEMBERSHIP alone let a
        # corrupted `unit_json` (bit rot / bad write / tampering) still count as
        # coverage, un-pinning the directory holding the only complete copy. A
        # payload whose stored digest no longer matches must drop out of coverage.
        archive_mod.backfill(**self.kwargs(), apply=True)
        covered, ok = archive_mod.covered_revisions(
            self.archive, self.ingestion, TARGET_LIKE, {REV_ORPHANED}
        )
        self.assertTrue(ok)
        self.assertIn(REV_ORPHANED, covered)  # intact -> covered

        # corrupt the stored payload WITHOUT updating its recorded digest
        with sqlite3.connect(self.archive) as conn:
            conn.execute(
                "UPDATE archived_units SET unit_json='{}' WHERE revision_id=?",
                (REV_ORPHANED,),
            )
        covered_after, ok_after = archive_mod.covered_revisions(
            self.archive, self.ingestion, TARGET_LIKE, {REV_ORPHANED}
        )
        self.assertTrue(ok_after)
        self.assertNotIn(REV_ORPHANED, covered_after)  # corrupted -> NOT covered
        self.assertEqual(
            archive_mod.status(**self.status_kwargs())["covered_by_archive"], 0
        )

    def test_self_consistent_but_wrong_identity_payload_is_not_coverage(self) -> None:
        # Defense-in-depth: a corrupted payload whose digest was ALSO recomputed
        # (self-consistent) still fails, because its EMBEDDED ids no longer match the
        # row -- so a re-hashed `{}` or a swapped-identity payload cannot forge
        # coverage on a row whose unit_id key is still the served one.
        archive_mod.backfill(**self.kwargs(), apply=True)
        forged = json.dumps(
            {"unit_id": "not-the-served-one", "revision_id": REV_ORPHANED}
        )
        with sqlite3.connect(self.archive) as conn:
            conn.execute(
                "UPDATE archived_units SET unit_json=?, unit_sha256=? "
                "WHERE revision_id=?",
                (
                    forged,
                    hashlib.sha256(forged.encode("utf-8")).hexdigest(),
                    REV_ORPHANED,
                ),
            )
        covered, ok = archive_mod.covered_revisions(
            self.archive, self.ingestion, TARGET_LIKE, {REV_ORPHANED}
        )
        self.assertTrue(ok)
        self.assertNotIn(REV_ORPHANED, covered)

    def test_backfill_self_heals_a_corrupted_archived_row(self) -> None:
        # V-H1: the backfill attestation used raw unit_id membership, so a second
        # run over a corrupted row re-attested it and reported success while the
        # payload stayed corrupt and covered_revisions() reported 0 -- a divergence
        # from the "ONE coverage query". Now the attestation is payload-validated AND
        # OR REPLACE repairs the corrupt row from the still-present source outbox.
        archive_mod.backfill(**self.kwargs(), apply=True)
        with sqlite3.connect(self.archive) as conn:
            conn.execute(
                "UPDATE archived_units SET unit_json='{}' WHERE revision_id=?",
                (REV_ORPHANED,),
            )
        covered, _ = archive_mod.covered_revisions(
            self.archive, self.ingestion, TARGET_LIKE, {REV_ORPHANED}
        )
        self.assertNotIn(REV_ORPHANED, covered)  # read authority: not covered

        second = archive_mod.backfill(**self.kwargs(), apply=True)
        with sqlite3.connect(self.archive) as conn:
            row = conn.execute(
                "SELECT unit_sha256, unit_json FROM archived_units WHERE revision_id=?",
                (REV_ORPHANED,),
            ).fetchone()
        self.assertNotEqual(row[1], "{}", "corrupt payload was not repaired")  # self-heal
        self.assertEqual(row[0], hashlib.sha256(row[1].encode("utf-8")).hexdigest())
        covered_after, _ = archive_mod.covered_revisions(
            self.archive, self.ingestion, TARGET_LIKE, {REV_ORPHANED}
        )
        self.assertIn(REV_ORPHANED, covered_after)  # coverage regained
        self.assertNotIn(REV_ORPHANED, second["unrecoverable"])  # honest report

    def test_payload_intact_fail_closed_branches(self) -> None:
        # M1/V-L2: every fail-closed branch of _payload_intact drops the row from
        # coverage -- pinned so a future refactor cannot silently reopen one.
        cases = {
            "null-digest": ("{}", None),  # unit_sha256 NULL (schema permits)
            "empty-json": ("", "whatever"),  # empty unit_json
            "non-json": ("not json", hashlib.sha256(b"not json").hexdigest()),
            "non-dict": ("123", hashlib.sha256(b"123").hexdigest()),  # self-consistent 123
        }
        for label, (unit_json, unit_sha256) in cases.items():
            with self.subTest(branch=label):
                if self.archive.exists():
                    self.archive.unlink()
                archive_mod.backfill(**self.kwargs(), apply=True)
                with sqlite3.connect(self.archive) as conn:
                    conn.execute(
                        "UPDATE archived_units SET unit_json=?, unit_sha256=? "
                        "WHERE revision_id=?",
                        (unit_json, unit_sha256, REV_ORPHANED),
                    )
                covered, ok = archive_mod.covered_revisions(
                    self.archive, self.ingestion, TARGET_LIKE, {REV_ORPHANED}
                )
                self.assertTrue(ok)
                self.assertNotIn(REV_ORPHANED, covered, label)


if __name__ == "__main__":
    unittest.main()
