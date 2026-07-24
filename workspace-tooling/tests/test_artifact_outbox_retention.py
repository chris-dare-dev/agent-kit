from __future__ import annotations

import gzip
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import artifact_outbox_retention as retention  # noqa: E402


TARGET = "url:test-collection"
TARGET_LIKE = "url:%test-collection%"

REV_FROZEN = "revision:frozen"
REV_CURRENT = "revision:current"
REV_IRREPRODUCIBLE = "revision:irreproducible"


class OutboxRetentionTests(unittest.TestCase):
    """The classifier must be fail-closed: uncertainty always means RETAIN."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.outbox = self.root / "outbox"
        self.outbox.mkdir()
        self.catalog = self.root / "catalog.sqlite3"
        self.ingestion = self.root / "ingestion.sqlite3"
        self.consumer = self.root / "consumer.sqlite3"
        self.replay = self.root / "replay.sqlite3"

        # clean: only a still-current revision -> reproducible from canonical
        self.clean = self._outbox_dir("catalog-run-1-chunks-v2", [REV_CURRENT])
        # holds a serving revision with no other representation
        self.irreproducible = self._outbox_dir(
            "catalog-run-2-chunks-v2", [REV_IRREPRODUCIBLE]
        )
        # named by the frozen replay manifest (content materialised elsewhere)
        self.manifest_named = self._outbox_dir("catalog-run-3-chunks-v2", [REV_FROZEN])
        # referenced by consumer event state
        self.state_bound = self._outbox_dir("skill-event-abc-chunks-v2", [REV_CURRENT])

        self._write_catalog([REV_CURRENT])
        self._write_ingestion([REV_FROZEN, REV_CURRENT, REV_IRREPRODUCIBLE])
        self._write_consumer([str(self.state_bound)])
        self._write_replay({REV_FROZEN: str(self.manifest_named)})

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _outbox_dir(self, name: str, revisions: list[str]) -> Path:
        path = self.outbox / name
        path.mkdir()
        (path / "manifest.json").write_text(
            json.dumps({"catalog_run_id": 1, "complete": True}), encoding="utf-8"
        )
        with gzip.open(path / "ingest-units.jsonl.gz", "wt", encoding="utf-8") as fh:
            for index, revision in enumerate(revisions):
                fh.write(json.dumps({"unit_id": f"u{index}", "revision_id": revision}) + "\n")
        return path

    def _write_catalog(self, current: list[str]) -> None:
        with sqlite3.connect(self.catalog) as conn:
            conn.execute("CREATE TABLE current_artifact_revisions (revision_id TEXT)")
            conn.executemany(
                "INSERT INTO current_artifact_revisions VALUES (?)",
                [(r,) for r in current],
            )

    def _write_ingestion(self, serving: list[str]) -> None:
        with sqlite3.connect(self.ingestion) as conn:
            conn.execute(
                "CREATE TABLE sink_units (sink TEXT, target TEXT, unit_id TEXT, "
                "revision_id TEXT, status TEXT)"
            )
            conn.executemany(
                "INSERT INTO sink_units VALUES ('qdrant', ?, ?, ?, 'completed')",
                [(TARGET, f"u{i}", r) for i, r in enumerate(serving)],
            )

    def _write_consumer(self, outbox_paths: list[str]) -> None:
        with sqlite3.connect(self.consumer) as conn:
            conn.execute("CREATE TABLE consumer_events (event_id TEXT, outbox_path TEXT)")
            conn.executemany(
                "INSERT INTO consumer_events VALUES (?, ?)",
                [(f"e{i}", p) for i, p in enumerate(outbox_paths)],
            )

    def _write_replay(self, revision_to_outbox: dict[str, str]) -> None:
        with sqlite3.connect(self.replay) as conn:
            conn.execute(
                "CREATE TABLE selected_units (unit_id TEXT, revision_id TEXT, "
                "source_outbox TEXT, unit_json TEXT)"
            )
            conn.executemany(
                "INSERT INTO selected_units VALUES (?, ?, ?, '{}')",
                [(f"s{i}", r, o) for i, (r, o) in enumerate(revision_to_outbox.items())],
            )

    def classify(self, **overrides: object) -> dict:
        kwargs = {
            "outbox_root": self.outbox,
            "catalog": self.catalog,
            "ingestion_state": self.ingestion,
            "consumer_state": self.consumer,
            "replay": self.replay,
            "target_like": TARGET_LIKE,
            "scan": True,
        }
        kwargs.update(overrides)
        return retention.classify(**kwargs)  # type: ignore[arg-type]

    def verdict_for(self, report: dict, name: str) -> str:
        for entry in report["entries"]:
            if entry["name"] == name:
                return entry["verdict"]
        raise AssertionError(f"{name} missing from report")

    # --- the three predicates -------------------------------------------------

    def test_state_bound_directory_is_retained(self) -> None:
        # Removing it converts a completed event into a poison/dead-letter.
        report = self.classify()
        self.assertEqual(
            self.verdict_for(report, "skill-event-abc-chunks-v2"), retention.VERDICT_RETAIN
        )

    def test_directory_holding_an_irreproducible_revision_is_retained(self) -> None:
        # ADR-002 line 44: the outbox is that revision's only representation.
        report = self.classify()
        entry = next(
            e for e in report["entries"] if e["name"] == "catalog-run-2-chunks-v2"
        )
        self.assertEqual(entry["verdict"], retention.VERDICT_RETAIN)
        self.assertEqual(entry["irreproducible_held"], 1)
        self.assertEqual(report["revisions"]["irreproducible"], 1)

    def test_manifest_named_directory_is_retained_by_default(self) -> None:
        # Content is materialised in selected_units, so line 44 does not bind —
        # but pruning forfeits source re-verification, so a human must decide.
        report = self.classify()
        self.assertEqual(
            self.verdict_for(report, "catalog-run-3-chunks-v2"),
            retention.VERDICT_RETAIN_DEFAULT,
        )

    def test_unencumbered_directory_is_prunable(self) -> None:
        report = self.classify()
        self.assertEqual(
            self.verdict_for(report, "catalog-run-1-chunks-v2"),
            retention.VERDICT_PRUNABLE,
        )
        self.assertEqual(report["totals"]["prunable"]["dirs"], 1)

    # --- fail-closed ----------------------------------------------------------

    def test_missing_input_database_retains_everything(self) -> None:
        # Cannot prove absence of obligation -> nothing is prunable.
        report = self.classify(replay=self.root / "does-not-exist.sqlite3")
        self.assertFalse(report["inputs_complete"])
        self.assertEqual(report["totals"]["prunable"]["dirs"], 0)
        self.assertTrue(report["problems"])
        for entry in report["entries"]:
            self.assertEqual(entry["verdict"], retention.VERDICT_RETAIN)

    def test_unreadable_units_file_retains_that_directory(self) -> None:
        # A corrupt gzip must not read as "holds nothing".
        (self.clean / "ingest-units.jsonl.gz").write_bytes(b"not a gzip stream")
        report = self.classify()
        entry = next(
            e for e in report["entries"] if e["name"] == "catalog-run-1-chunks-v2"
        )
        self.assertEqual(entry["predicates"]["scan"], "error")
        self.assertIsNone(entry["predicates"]["holds_irreproducible"])
        self.assertEqual(entry["verdict"], retention.VERDICT_RETAIN)

    def test_no_scan_mode_retains_everything(self) -> None:
        report = self.classify(scan=False)
        self.assertEqual(report["totals"]["prunable"]["dirs"], 0)
        for entry in report["entries"]:
            self.assertEqual(entry["predicates"]["scan"], "skipped")
            self.assertEqual(entry["verdict"], retention.VERDICT_RETAIN)

    def test_symlinked_entry_is_never_classified(self) -> None:
        link = self.outbox / "catalog-run-9-chunks-v2"
        link.symlink_to(self.clean)
        report = self.classify()
        self.assertNotIn(
            "catalog-run-9-chunks-v2", [e["name"] for e in report["entries"]]
        )

    # --- contract -------------------------------------------------------------

    def test_report_declares_itself_non_deleting(self) -> None:
        report = self.classify()
        self.assertEqual(report["mode"], "report-only")
        self.assertIn("no deletion authority", report["deletion"])

    def test_human_render_marks_incomplete_inputs_loudly(self) -> None:
        report = self.classify(consumer_state=self.root / "missing.sqlite3")
        text = retention.render_human(report)
        self.assertIn("INPUTS INCOMPLETE", text)


if __name__ == "__main__":
    unittest.main()
