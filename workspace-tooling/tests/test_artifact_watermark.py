from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import artifact_watermark as watermark  # noqa: E402


NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


def _ago(seconds: float) -> datetime:
    return NOW - timedelta(seconds=seconds)


def _touch(path: Path, moment: datetime, payload: str = "{}") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    stamp = moment.timestamp()
    os.utime(path, (stamp, stamp))
    return path


class MaxAgeRuleTests(unittest.TestCase):
    """A health file older than its cadence is unhealthy, not stale-green."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _health(self, observed: datetime, healthy: bool = True) -> Path:
        path = self.root / "health.json"
        path.write_text(
            json.dumps({"healthy": healthy, "observed_at": observed.isoformat()}),
            encoding="utf-8",
        )
        return path

    def test_fresh_and_self_reported_healthy_is_ok(self) -> None:
        component = watermark._health_file_component(
            self._health(_ago(60)),
            max_age_seconds=900,
            healthy_key="healthy",
            healthy_values=(True,),
            timestamp_keys=("observed_at",),
            now=NOW,
        )
        self.assertEqual(component["state"], watermark.STATE_OK)

    def test_stale_file_claiming_healthy_is_reported_stale(self) -> None:
        """The exact 2026-07-18 condition: 13 h old, still says healthy:true."""
        component = watermark._health_file_component(
            self._health(_ago(13 * 3600), healthy=True),
            max_age_seconds=watermark.CONSUMER_MAX_AGE_SECONDS,
            healthy_key="healthy",
            healthy_values=(True,),
            timestamp_keys=("observed_at",),
            now=NOW,
        )
        self.assertEqual(component["state"], watermark.STATE_STALE)
        self.assertTrue(component["reported"])  # contradiction stays visible
        self.assertIn("not trustworthy", component["detail"])

    def test_absent_file_is_missing_not_ok(self) -> None:
        component = watermark._health_file_component(
            self.root / "absent.json",
            max_age_seconds=900,
            healthy_key="healthy",
            healthy_values=(True,),
            timestamp_keys=("observed_at",),
            now=NOW,
        )
        self.assertEqual(component["state"], watermark.STATE_MISSING)

    def test_epoch_timestamps_are_accepted(self) -> None:
        path = self.root / "service.json"
        path.write_text(
            json.dumps({"status": "healthy", "updated_unix": _ago(30).timestamp()}),
            encoding="utf-8",
        )
        component = watermark._health_file_component(
            path,
            max_age_seconds=600,
            healthy_key="status",
            healthy_values=("healthy",),
            timestamp_keys=("updated_unix", "updated_at"),
            now=NOW,
        )
        self.assertEqual(component["state"], watermark.STATE_OK)

    def test_timestampless_file_is_unknown(self) -> None:
        path = self.root / "no-time.json"
        path.write_text(json.dumps({"healthy": True}), encoding="utf-8")
        component = watermark._health_file_component(
            path,
            max_age_seconds=600,
            healthy_key="healthy",
            healthy_values=(True,),
            timestamp_keys=("observed_at",),
            now=NOW,
        )
        self.assertEqual(component["state"], watermark.STATE_UNKNOWN)


class ReceiptWatermarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.receipts = self.root / "skill-events"
        self.receipts.mkdir()
        self.state = self.root / "consumer.sqlite3"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _state(self, observed_events: list[str]) -> None:
        with sqlite3.connect(self.state) as connection:
            connection.execute(
                "CREATE TABLE consumer_events (event_id TEXT PRIMARY KEY,"
                " status TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO consumer_events VALUES (?, 'completed', ?)",
                [(event, NOW.isoformat()) for event in observed_events],
            )

    def test_all_receipts_observed_is_ok(self) -> None:
        _touch(self.receipts / "aaa.json", _ago(60))
        self._state(["event:aaa"])
        component = watermark.receipts_component(self.receipts, self.state, now=NOW)
        self.assertEqual(component["state"], watermark.STATE_OK)
        self.assertEqual(component["unobserved"], 0)

    def test_old_unobserved_receipt_breaches_the_slo(self) -> None:
        _touch(self.receipts / "aaa.json", _ago(60))
        _touch(self.receipts / "bbb.json", _ago(14 * 3600))
        self._state(["event:aaa"])
        component = watermark.receipts_component(self.receipts, self.state, now=NOW)
        self.assertEqual(component["state"], watermark.STATE_STALE)
        self.assertEqual(component["unobserved"], 1)
        self.assertEqual(component["oldest_unobserved_event_id"], "event:bbb")
        self.assertGreater(component["oldest_unobserved_age_seconds"], 900)

    def test_recent_unobserved_receipt_is_degraded_not_stale(self) -> None:
        _touch(self.receipts / "ccc.json", _ago(30))
        self._state([])
        component = watermark.receipts_component(self.receipts, self.state, now=NOW)
        self.assertEqual(component["state"], watermark.STATE_DEGRADED)

    def test_unreadable_state_is_unknown_not_ok(self) -> None:
        _touch(self.receipts / "aaa.json", _ago(60))
        self.state.write_text("not a database", encoding="utf-8")
        component = watermark.receipts_component(self.receipts, self.state, now=NOW)
        self.assertEqual(component["state"], watermark.STATE_UNKNOWN)


class ConsumerLivenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.health = self.root / "consumer-health.json"
        self.state = self.root / "consumer.sqlite3"
        self.health.write_text(
            json.dumps({"healthy": True, "observed_at": _ago(60).isoformat()}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_unloaded_agent_is_missing_even_with_a_fresh_green_file(self) -> None:
        component = watermark.consumer_component(
            self.health, self.state, loaded_probe=lambda label: False, now=NOW
        )
        self.assertEqual(component["state"], watermark.STATE_MISSING)
        self.assertIn("NOT loaded", component["detail"])

    def test_loaded_and_fresh_is_ok(self) -> None:
        component = watermark.consumer_component(
            self.health, self.state, loaded_probe=lambda label: True, now=NOW
        )
        self.assertEqual(component["state"], watermark.STATE_OK)

    def test_unconsultable_launchctl_is_unknown(self) -> None:
        component = watermark.consumer_component(
            self.health, self.state, loaded_probe=lambda label: None, now=NOW
        )
        self.assertEqual(component["state"], watermark.STATE_UNKNOWN)


class WriterAlignmentTests(unittest.TestCase):
    class _Runtime:
        active_backend = "server"
        qdrant_generation = "p20260721v1"
        qdrant_collection = "personal_artifact_chunks_p20260721v1"
        rollback_mode = "read-only"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _plist(self, arguments: list[str]) -> Path:
        import plistlib

        path = self.root / "consumer.plist"
        with path.open("wb") as handle:
            plistlib.dump({"Label": "test", "ProgramArguments": arguments}, handle)
        return path

    def test_embedded_writer_against_server_reader_is_degraded(self) -> None:
        """The installed plist's actual trap: local sink, server readers."""
        plist = self._plist(
            [
                "python",
                "consumer.py",
                "consume",
                "--qdrant-path",
                "/tmp/qdrant",
                "--collection",
                "personal_artifact_chunks_v1",
            ]
        )
        component = watermark.writer_component(self._Runtime(), plist)
        self.assertEqual(component["state"], watermark.STATE_DEGRADED)
        self.assertFalse(component["aligned"])
        self.assertTrue(component["configured_sink"].startswith("local:"))
        self.assertIn("read-only", component["detail"])

    def test_aligned_server_writer_is_ok(self) -> None:
        plist = self._plist(
            [
                "python",
                "consumer.py",
                "consume",
                "--qdrant-url",
                "http://127.0.0.1:6333",
                "--collection",
                "personal_artifact_chunks_p20260721v1",
            ]
        )
        component = watermark.writer_component(self._Runtime(), plist)
        self.assertEqual(component["state"], watermark.STATE_OK)
        self.assertTrue(component["aligned"])

    def test_absent_runtime_is_unknown(self) -> None:
        component = watermark.writer_component(None, self.root / "absent.plist")
        self.assertEqual(component["state"], watermark.STATE_UNKNOWN)


class OutboxQuarantineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "outbox-dead-letter"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_absent_root_is_ok(self) -> None:
        component = watermark.outbox_quarantine_component(self.root)
        self.assertEqual(component["state"], watermark.STATE_OK)
        self.assertEqual(component["count"], 0)

    def test_quarantined_directory_is_counted_and_degraded(self) -> None:
        entry = self.root / "catalog-run-6-chunks-v2-incomplete"
        entry.mkdir(parents=True)
        (entry / "audit.json").write_text(
            json.dumps({"reason": "incomplete manifest"}), encoding="utf-8"
        )
        component = watermark.outbox_quarantine_component(self.root)
        self.assertEqual(component["state"], watermark.STATE_DEGRADED)
        self.assertEqual(component["count"], 1)
        self.assertEqual(
            component["entries"][0]["name"], "catalog-run-6-chunks-v2-incomplete"
        )
        self.assertEqual(component["entries"][0]["reason"], "incomplete manifest")

    def test_loose_files_are_not_counted_as_quarantine(self) -> None:
        self.root.mkdir(parents=True)
        (self.root / "README").write_text("notes", encoding="utf-8")
        component = watermark.outbox_quarantine_component(self.root)
        self.assertEqual(component["count"], 0)


class CatalogAndSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _catalog(self, rows: list[tuple[int, str, str]]) -> Path:
        path = self.root / "catalog.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE scan_runs (run_id INTEGER PRIMARY KEY,"
                " finished_at TEXT, status TEXT)"
            )
            connection.executemany("INSERT INTO scan_runs VALUES (?, ?, ?)", rows)
        return path

    def test_fresh_complete_run_is_ok(self) -> None:
        path = self._catalog([(18, _ago(600).isoformat(), "complete")])
        component = watermark.catalog_component(path, now=NOW)
        self.assertEqual(component["state"], watermark.STATE_OK)
        self.assertEqual(component["authoritative_run_id"], 18)

    def test_old_complete_run_is_stale(self) -> None:
        path = self._catalog([(18, _ago(3 * 86400).isoformat(), "complete")])
        component = watermark.catalog_component(path, now=NOW)
        self.assertEqual(component["state"], watermark.STATE_STALE)

    def test_failed_latest_attempt_is_degraded(self) -> None:
        path = self._catalog(
            [
                (18, _ago(600).isoformat(), "complete"),
                (19, _ago(300).isoformat(), "failed"),
            ]
        )
        component = watermark.catalog_component(path, now=NOW)
        self.assertEqual(component["state"], watermark.STATE_DEGRADED)

    def test_missing_catalog_is_missing(self) -> None:
        component = watermark.catalog_component(self.root / "absent.sqlite3", now=NOW)
        self.assertEqual(component["state"], watermark.STATE_MISSING)

    def test_snapshot_age_and_probe_exclusion(self) -> None:
        snapshots = self.root / "snapshots"
        _touch(snapshots / "collection-1.snapshot", _ago(2 * 86400))
        _touch(snapshots / "corrupt-restore-probe.snapshot", _ago(60))
        component = watermark.snapshots_component(snapshots, now=NOW)
        self.assertEqual(component["count"], 1)  # the probe is evidence, not a backup
        self.assertEqual(component["state"], watermark.STATE_STALE)


class CompositeWatermarkTests(unittest.TestCase):
    """The composite must be unhealthy when any stage is, even if files are green."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.derived = Path(self.temp.name) / "derived"
        self.derived.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_all_green_files_still_report_unhealthy_when_consumer_is_gone(self) -> None:
        (self.derived / "artifact-event-consumer-health.json").write_text(
            json.dumps({"healthy": True, "observed_at": _ago(13 * 3600).isoformat()}),
            encoding="utf-8",
        )
        (self.derived / "artifact-qdrant-bootstrap-health.json").write_text(
            json.dumps({"status": "healthy", "updated_at": _ago(60).isoformat()}),
            encoding="utf-8",
        )
        (self.derived / "artifact-memory-service-health.json").write_text(
            json.dumps({"status": "healthy", "updated_unix": _ago(30).timestamp()}),
            encoding="utf-8",
        )
        payload = watermark.compute_watermark(
            derived_root=self.derived,
            loaded_probe=lambda label: False,
            now=NOW,
        )
        self.assertFalse(payload["healthy"])
        self.assertIn("consumer_missing", payload["issue_codes"])
        self.assertEqual(
            payload["components"]["bootstrap"]["state"], watermark.STATE_OK
        )
        self.assertEqual(payload["components"]["service"]["state"], watermark.STATE_OK)

    def test_render_human_is_stable_and_names_the_verdict(self) -> None:
        payload = watermark.compute_watermark(
            derived_root=self.derived, loaded_probe=lambda label: False, now=NOW
        )
        text = watermark.render_human(payload)
        self.assertIn("UNHEALTHY", text)
        self.assertIn("consumer", text)

    def test_unreadable_runtime_is_an_issue_code_not_a_crash(self) -> None:
        (self.derived / "artifact-memory-runtime.json").write_text(
            "{ not json", encoding="utf-8"
        )
        payload = watermark.compute_watermark(
            derived_root=self.derived, loaded_probe=lambda label: True, now=NOW
        )
        self.assertIn("runtime_config_unreadable", payload["issue_codes"])
        self.assertIsNotNone(payload["runtime_error"])


class QuarantineAcknowledgmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "outbox-dead-letter"
        self.entry = self.root / "catalog-run-6-chunks-v2-incomplete"
        self.entry.mkdir(parents=True)
        (self.entry / "manifest.json").write_text("{}", encoding="utf-8")
        self.ledger = self.base / watermark.QUARANTINE_ACK_FILENAME

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _acknowledge(self, review_by: str | None = None) -> None:
        entry = {
            "name": self.entry.name,
            "fingerprint": watermark.quarantine_fingerprint(self.entry),
            "acknowledged_at": "2026-07-18T00:00:00+00:00",
            "reason": "retained residue per the residue ledger",
            "review_by": review_by,
        }
        self.ledger.write_text(
            json.dumps({"schema_version": 1, "entries": [entry]}),
            encoding="utf-8",
        )

    def test_unacknowledged_directory_is_open_and_degraded(self) -> None:
        component = watermark.outbox_quarantine_component(self.root, self.ledger)
        self.assertEqual(component["open"], 1)
        self.assertEqual(component["state"], watermark.STATE_DEGRADED)
        self.assertEqual(component["entries"][0]["ack"], "open")

    def test_acknowledged_directory_is_ok_but_still_surfaced(self) -> None:
        # An acknowledgment must now carry a FUTURE review date to suppress.
        self._acknowledge(review_by="2099-01-01T00:00:00+00:00")
        component = watermark.outbox_quarantine_component(self.root, self.ledger)
        self.assertEqual(component["count"], 1)
        self.assertEqual(component["open"], 0)
        self.assertEqual(component["acknowledged"], 1)
        self.assertEqual(component["state"], watermark.STATE_OK)
        self.assertEqual(component["entries"][0]["ack"], "acknowledged")

    def test_missing_review_date_reopens_acknowledgment(self) -> None:
        # H5 fail-closed: an acknowledgment with NO review date can never age
        # out, so it would suppress the entry forever. Previously it fell
        # through to "acknowledged"; it must now demand re-review.
        self._acknowledge()
        component = watermark.outbox_quarantine_component(self.root, self.ledger)
        self.assertEqual(component["open"], 1)
        self.assertEqual(component["state"], watermark.STATE_DEGRADED)
        self.assertEqual(component["entries"][0]["ack"], "reopened-invalid-review")

    def test_unparseable_review_date_reopens_acknowledgment(self) -> None:
        # H5 fail-closed: garbage parsed to None and silently disabled the
        # deadline — the original review probe used exactly this value.
        self._acknowledge(review_by="definitely-not-a-time")
        component = watermark.outbox_quarantine_component(self.root, self.ledger)
        self.assertEqual(component["open"], 1)
        self.assertEqual(component["entries"][0]["ack"], "reopened-invalid-review")

    def test_same_size_byte_flip_reopens_acknowledgment(self) -> None:
        # H4: the v1 fingerprint hashed only relative path + size, so a
        # same-size edit kept its acknowledgment. v2 hashes CONTENT.
        self._acknowledge(review_by="2099-01-01T00:00:00+00:00")
        steady = watermark.outbox_quarantine_component(self.root, self.ledger)
        self.assertEqual(steady["entries"][0]["ack"], "acknowledged")
        # same byte count, different bytes
        (self.entry / "manifest.json").write_text("[]", encoding="utf-8")
        component = watermark.outbox_quarantine_component(self.root, self.ledger)
        self.assertEqual(component["open"], 1)
        self.assertEqual(component["entries"][0]["ack"], "reopened-changed")

    def test_added_directory_reopens_acknowledgment(self) -> None:
        # H4: v1 skipped every non-file, so an added directory (or symlink)
        # was invisible to the fingerprint.
        self._acknowledge(review_by="2099-01-01T00:00:00+00:00")
        (self.entry / "sneaky").mkdir()
        component = watermark.outbox_quarantine_component(self.root, self.ledger)
        self.assertEqual(component["entries"][0]["ack"], "reopened-changed")

    def test_v1_name_size_ack_is_invalidated_by_the_v2_fingerprint(self) -> None:
        # S2.2 / KR3: an acknowledgment minted under the retired v1 (name+size)
        # fingerprint must NOT suppress under v2. The v1 digest can never equal
        # the v2 content digest (QUARANTINE_FINGERPRINT_VERSION is folded in), so
        # the entry reopens even though the directory is byte-for-byte unchanged
        # and carries a FUTURE review date that WOULD suppress if it matched.
        size = (self.entry / "manifest.json").stat().st_size
        v1_fingerprint = hashlib.sha256(
            f"{self.entry.name}/manifest.json\0{size}".encode("utf-8")
        ).hexdigest()
        self.assertNotEqual(v1_fingerprint, watermark.quarantine_fingerprint(self.entry))
        self.ledger.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "entries": [
                        {
                            "name": self.entry.name,
                            "fingerprint": v1_fingerprint,
                            "acknowledged_at": "2026-07-18T00:00:00+00:00",
                            "reason": "acknowledged under the retired v1 name+size fingerprint",
                            "review_by": "2099-01-01T00:00:00+00:00",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        component = watermark.outbox_quarantine_component(self.root, self.ledger)
        self.assertEqual(component["open"], 1)
        self.assertNotEqual(component["state"], watermark.STATE_OK)
        self.assertEqual(component["entries"][0]["ack"], "reopened-changed")

    def test_unstattable_child_reports_unknown_rather_than_raising(self) -> None:
        # H1: an r-without-x root LISTS names but cannot stat them, so the
        # per-child is_symlink()/is_dir() raised EACCES. That leg was
        # unguarded: the PermissionError escaped the component, propagated
        # through compute_watermark, and killed watchdog.run() before it wrote
        # its log, health file, or notification.
        os.chmod(self.root, 0o400)
        try:
            component = watermark.outbox_quarantine_component(self.root, self.ledger)
        finally:
            os.chmod(self.root, 0o700)
        self.assertEqual(component["state"], watermark.STATE_UNKNOWN)
        self.assertIn("unreadable", str(component["detail"]))

    def test_unreadable_subdirectory_fails_the_fingerprint_closed(self) -> None:
        # M1: the directory walk silently omitted an unreadable subtree, so the
        # fingerprint stayed STABLE while hidden content changed. An unreadable
        # FILE already failed closed — the guarantee must not depend on which
        # node type loses permissions.
        nested = self.entry / "nested"
        nested.mkdir()
        (nested / "payload.bin").write_bytes(b"secret")
        os.chmod(nested, 0o000)
        try:
            fingerprint = watermark.quarantine_fingerprint(self.entry)
        finally:
            os.chmod(nested, 0o700)
        self.assertIsNone(fingerprint, "an unreadable subtree must fail closed")

    def test_unreadable_root_reports_unknown_rather_than_all_clear(self) -> None:
        # H5: the early return leaves open/count at 0 with state=unknown — the
        # trap that let consumers read a can't-tell as an all-clear.
        os.chmod(self.root, 0o000)
        try:
            component = watermark.outbox_quarantine_component(self.root, self.ledger)
        finally:
            os.chmod(self.root, 0o700)
        self.assertEqual(component["state"], watermark.STATE_UNKNOWN)
        self.assertEqual(component["open"], 0)
        self.assertIn(watermark.STATE_UNKNOWN, watermark.FAILING_STATES)

    def test_content_change_reopens_acknowledgment(self) -> None:
        self._acknowledge()
        (self.entry / "extra.bin").write_bytes(b"x" * 8)
        component = watermark.outbox_quarantine_component(self.root, self.ledger)
        self.assertEqual(component["open"], 1)
        self.assertEqual(component["state"], watermark.STATE_DEGRADED)
        self.assertEqual(component["entries"][0]["ack"], "reopened-changed")

    def test_past_review_date_reopens_acknowledgment(self) -> None:
        self._acknowledge(review_by="2026-01-01T00:00:00+00:00")
        component = watermark.outbox_quarantine_component(self.root, self.ledger)
        self.assertEqual(component["entries"][0]["ack"], "reopened-review-due")
        self.assertEqual(component["state"], watermark.STATE_DEGRADED)

    def test_default_ledger_lives_beside_the_root(self) -> None:
        component = watermark.outbox_quarantine_component(self.root)
        self.assertEqual(
            component["ack_ledger"],
            str(self.base / watermark.QUARANTINE_ACK_FILENAME),
        )


if __name__ == "__main__":
    unittest.main()
