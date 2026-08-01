from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
import stat
import sys
import tempfile
import unittest

import platform_skips
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import artifact_catalog as catalog  # noqa: E402
import artifact_event_consumer as consumer  # noqa: E402
import artifact_ingestion as ingestion  # noqa: E402
import artifact_skill_capture as capture  # noqa: E402
import artifact_watermark as watermark  # noqa: E402


class _ConsumerTestSupport(unittest.TestCase):
    """Shared setUp + fakes for the consumer test suites.

    Carries NO ``test_`` methods so subclasses (e.g. CatalogStalenessRefreshTests)
    do not silently re-run the base suite's cases — a non-test support base is
    the fix for the inherited-re-run inflation.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.derived = self.root / "derived"
        self.receipts = self.derived / "skill-events"
        self.outboxes = self.derived / "outbox"
        self.state = self.derived / "consumer.sqlite3"
        self.ingestion_state = self.derived / "ingestion.sqlite3"
        self.qdrant = self.derived / "qdrant"
        (self.workspace / "scripts").mkdir(parents=True)
        (self.workspace / "plans").mkdir()
        self.policy = self.workspace / "scripts/artifact-policy.json"
        self.policy.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "catalog": {
                        "canonical_roots": ["plans"],
                        "top_level_globs": [],
                        "include_path_globs": ["plans/**"],
                        "exclude_roots": [],
                        "prune_directory_names": [".git"],
                        "prune_path_globs": [],
                    },
                }
            ),
            encoding="utf-8",
        )
        self.handoff = self.workspace / "plans/HANDOFF-alpha.md"
        self.handoff.write_text(
            "# Handoff\n\nstatus: requested\nowner: platform\n",
            encoding="utf-8",
        )
        catalog.run_catalog(self.workspace, self.derived, self.policy, False)
        result = capture.emit(
            Namespace(
                workspace=self.workspace,
                policy=Path("scripts/artifact-policy.json"),
                producer="handoff",
                run_id="alpha-1",
                paths=[self.handoff],
                roots=[],
                receipt_root=self.receipts,
                apply=True,
            )
        )
        self.receipt_path = Path(result["receipt_path"])
        self.qdrant_calls: list[dict[str, object]] = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def args(self, *, apply: bool, refresh: bool = False) -> Namespace:
        return Namespace(
            workspace=self.workspace,
            receipt_root=self.receipts,
            state=self.state,
            catalog=self.derived / "artifact-catalog.sqlite3",
            policy=self.policy,
            outbox_root=self.outboxes,
            ingestion_state=self.ingestion_state,
            qdrant_path=self.qdrant,
            collection=ingestion.DEFAULT_COLLECTION,
            embedding_model=ingestion.DEFAULT_EMBEDDING_MODEL,
            # Hermetic: without this the sink resolver would read the
            # INSTALLED runtime configuration and bind these tests to
            # whichever backend the host happens to be serving.
            runtime_config=None,
            no_runtime_config=True,
            batch_size=8,
            refresh_catalog=refresh,
            health_file=self.derived / "consumer-health.json",
            # Hermetic: status() otherwise falls back to the REAL
            # DEFAULT_DEAD_LETTER_ROOT and reads the host's live quarantine.
            dead_letter_root=self.derived / "outbox-dead-letter",
            reconcile_retry_seconds=0,
            desktop_notify=False,
            apply=apply,
        )

    def fake_ingest(self, **kwargs: object) -> dict[str, object]:
        self.qdrant_calls.append(kwargs)
        outbox = Path(str(kwargs["outbox"]))
        return {
            "mode": "applied",
            "ingested": sum(1 for _ in ingestion.iter_outbox_units(outbox)),
            "collection_points": 1,
        }

    def fake_reconcile(self, **kwargs: object) -> dict[str, object]:
        return {
            "mode": "applied",
            "points_scanned": len(self.qdrant_calls),
            "point_deletion": "disabled",
        }


class ArtifactEventConsumerTests(_ConsumerTestSupport):
    def test_plan_writes_nothing(self) -> None:
        result, failed = consumer.consume(
            self.args(apply=False),
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        self.assertFalse(failed)
        self.assertEqual(result["would_consume"], 1)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.outboxes.exists())
        self.assertEqual(self.qdrant_calls, [])

    @platform_skips.requires_posix_modes
    def test_apply_creates_immutable_outbox_and_is_idempotent(self) -> None:
        first, failed = consumer.consume(
            self.args(apply=True),
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        self.assertFalse(failed)
        self.assertEqual(first["results"][0]["status"], "completed")
        self.assertEqual(first["results"][0]["graphiti_write"], "disabled")
        outbox = Path(first["results"][0]["outbox"])
        manifest_path = outbox / "manifest.json"
        before = manifest_path.read_bytes()
        manifest = ingestion.load_outbox_manifest(outbox)
        self.assertEqual(manifest["graphiti_write"], "disabled")
        self.assertEqual(manifest["graphiti_candidate_count"], 1)
        self.assertGreater(manifest["counts"]["units"], 0)
        self.assertEqual(stat.S_IMODE(outbox.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE((self.derived / "consumer-health.json").stat().st_mode),
            0o600,
        )

        second, failed = consumer.consume(
            self.args(apply=True),
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        self.assertFalse(failed)
        self.assertEqual(second["results"][0]["status"], "already-completed")
        self.assertEqual(manifest_path.read_bytes(), before)
        self.assertEqual(len(self.qdrant_calls), 1)

    def test_tampered_receipt_is_dead_lettered_before_sink_write(self) -> None:
        value = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        value["safety"]["graphiti_write"] = "enabled"
        self.receipt_path.write_text(json.dumps(value), encoding="utf-8")
        result, failed = consumer.consume(
            self.args(apply=True),
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        self.assertTrue(failed)
        self.assertEqual(result["poison_count"], 1)
        self.assertEqual(len(result["dead_letter_ids"]), 1)
        self.assertEqual(self.qdrant_calls, [])
        with closing(sqlite3.connect(self.state)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM dead_letters WHERE status='open'"
                ).fetchone()[0],
                1,
            )

    def test_stale_catalog_receipt_records_failure_without_sink_write(self) -> None:
        self.handoff.write_text("# Handoff\n\nchanged\n", encoding="utf-8")
        result, failed = consumer.consume(
            self.args(apply=True),
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        self.assertTrue(failed)
        self.assertEqual(result["results"][0]["status"], "stale")
        self.assertEqual(self.qdrant_calls, [])

    def test_refresh_catalog_allows_new_receipt_revision(self) -> None:
        self.handoff.write_text(
            "# Handoff\n\nstatus: complete\nowner: platform\n",
            encoding="utf-8",
        )
        refreshed_receipt = capture.emit(
            Namespace(
                workspace=self.workspace,
                policy=Path("scripts/artifact-policy.json"),
                producer="handoff",
                run_id="alpha-2",
                paths=[self.handoff],
                roots=[],
                receipt_root=self.receipts,
                apply=True,
            )
        )
        result, failed = consumer.consume(
            self.args(apply=True, refresh=True),
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        # The unconsumed older receipt becomes terminally stale when the
        # catalog advances, while the new current revision is still consumed.
        self.assertTrue(failed)
        completed = {
            item["event_id"]
            for item in result["results"]
            if item["status"] == "completed"
        }
        self.assertIn(refreshed_receipt["event_id"], completed)

    def test_stray_entry_is_dead_lettered_while_valid_event_completes(self) -> None:
        stray = self.receipts / ".DS_Store"
        stray.write_bytes(b"metadata")
        stray.chmod(0o600)
        result, failed = consumer.consume(
            self.args(apply=True),
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        self.assertTrue(failed)
        self.assertEqual(result["poison_count"], 1)
        self.assertIn("completed", {item["status"] for item in result["results"]})
        self.assertEqual(len(self.qdrant_calls), 1)
        self.assertTrue((self.derived / "consumer-health.json").exists())

    def test_health_notification_is_deduplicated_for_repeated_poison(
        self,
    ) -> None:
        stray = self.receipts / ".DS_Store"
        stray.write_bytes(b"metadata")
        stray.chmod(0o600)
        args = self.args(apply=True)
        args.desktop_notify = True
        notifications: list[list[str]] = []

        def notify(issue_codes: list[str]) -> None:
            notifications.append(list(issue_codes))

        first, first_failed = consumer.consume(
            args,
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=self.fake_reconcile,
            notifier=notify,
        )
        second, second_failed = consumer.consume(
            args,
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=self.fake_reconcile,
            notifier=notify,
        )

        self.assertTrue(first_failed)
        self.assertTrue(second_failed)
        self.assertEqual(len(first["dead_letter_ids"]), 1)
        self.assertEqual(len(second["dead_letter_ids"]), 1)
        self.assertEqual(notifications, [["dead_letters_open"]])
        health = json.loads(
            (self.derived / "consumer-health.json").read_text(encoding="utf-8")
        )
        self.assertFalse(health["healthy"])
        self.assertEqual(health["issue_codes"], ["dead_letters_open"])
        self.assertNotIn(str(stray), json.dumps(health))

    def test_dead_letter_replay_requires_restored_receipt_and_is_audited(
        self,
    ) -> None:
        original = self.receipt_path.read_bytes()
        value = json.loads(original)
        value["safety"]["graphiti_write"] = "enabled"
        self.receipt_path.write_text(json.dumps(value), encoding="utf-8")
        poisoned, failed = consumer.consume(
            self.args(apply=True),
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        self.assertTrue(failed)
        dead_id = poisoned["dead_letter_ids"][0]

        self.receipt_path.write_bytes(original)
        self.receipt_path.chmod(0o600)
        action, action_failed = consumer.dead_letter_action(
            Namespace(
                workspace=self.workspace,
                state=self.state,
                dead_letter_id=dead_id,
                dead_letter_action="replay",
                resolution="canonical receipt restored",
                apply=True,
            )
        )
        self.assertFalse(action_failed)
        self.assertEqual(action["status"], "replay-requested")

        replayed, replay_failed = consumer.consume(
            self.args(apply=True),
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        self.assertFalse(replay_failed)
        self.assertEqual(replayed["results"][0]["status"], "completed")
        with closing(sqlite3.connect(self.state)) as connection, connection:
            dead_status = connection.execute(
                "SELECT status FROM dead_letters WHERE dead_letter_id=?",
                (dead_id,),
            ).fetchone()[0]
            actions = [
                row[0]
                for row in connection.execute(
                    "SELECT action FROM dead_letter_audit "
                    "WHERE dead_letter_id=? ORDER BY audit_id",
                    (dead_id,),
                )
            ]
        self.assertEqual(dead_status, "resolved")
        self.assertIn("replay", actions)
        self.assertIn("resolved", actions)

    def test_failed_reconcile_retries_without_a_new_event(self) -> None:
        attempts = 0

        def flaky_reconcile(**kwargs: object) -> dict[str, object]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary reconcile failure")
            return {
                "mode": "applied",
                "points_scanned": 1,
                "point_deletion": "disabled",
            }

        first, failed = consumer.consume(
            self.args(apply=True),
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=flaky_reconcile,
        )
        self.assertTrue(failed)
        self.assertEqual(first["qdrant_reconcile"]["status"], "error")
        self.assertEqual(len(self.qdrant_calls), 1)

        second, failed = consumer.consume(
            self.args(apply=True),
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=flaky_reconcile,
        )
        self.assertFalse(failed)
        self.assertEqual(second["qdrant_reconcile"]["mode"], "applied")
        self.assertEqual(len(self.qdrant_calls), 1)
        self.assertEqual(attempts, 2)

    def test_v1_state_migration_preserves_completed_and_recovers_processing(
        self,
    ) -> None:
        with closing(sqlite3.connect(self.state)) as connection, connection:
            connection.executescript(
                """
                PRAGMA user_version=1;
                CREATE TABLE consumer_events (
                    event_id TEXT PRIMARY KEY,
                    receipt_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    outbox_path TEXT,
                    artifact_count INTEGER NOT NULL,
                    unit_count INTEGER NOT NULL,
                    detail TEXT,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO consumer_events VALUES
                  ('event:complete', 'sha-complete', 'completed', '/tmp/outbox',
                   1, 2, NULL, '2026-07-17T00:00:00+00:00'),
                  ('event:processing', 'sha-processing', 'processing', NULL,
                   1, 0, NULL, '2026-07-17T00:00:00+00:00');
                """
            )
        self.state.chmod(0o600)

        state = consumer.ConsumerState(self.state, self.workspace)
        state.close()

        with closing(sqlite3.connect(self.state)) as connection, connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            rows = dict(
                connection.execute(
                    "SELECT event_id, status FROM consumer_events"
                )
            )
            dead_letter_table = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='dead_letters'"
            ).fetchone()[0]
        self.assertEqual(version, consumer.SCHEMA_VERSION)
        self.assertEqual(rows["event:complete"], "completed")
        self.assertEqual(rows["event:processing"], "failed")
        self.assertEqual(dead_letter_table, 1)


class SinkResolutionTests(unittest.TestCase):
    """The write sink is resolved from the runtime, and disagreement refuses.

    These cover the defect where the consumer's sink came from CLI flags
    alone, so it would happily publish into the retired embedded store while
    the runtime served a different generation.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True)
        self.derived = self.root / "derived"
        self.derived.mkdir()
        self.embedded = self.derived / "qdrant"
        self.embedded.mkdir()
        self.digest = "b" * 64
        self.manifest = self.derived / "build-manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "embedding": {
                        "name": ingestion.DEFAULT_EMBEDDING_MODEL,
                        "manifest_sha256": self.digest,
                    }
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def runtime(self, *, backend: str, rollback: str = "read-only") -> object:
        return SimpleNamespace(
            active_backend=backend,
            rollback_mode=rollback,
            qdrant_url="http://127.0.0.1:6333",
            qdrant_collection="personal_artifact_chunks_p20260721v1",
            qdrant_generation="p20260721v1",
            embedded_path=self.embedded,
            build_manifest=self.manifest,
            qdrant_admin_key=lambda: "k" * 40,
        )

    def args(self, **overrides: object) -> Namespace:
        base = dict(
            workspace=self.workspace,
            qdrant_path=None,
            collection=None,
            embedding_model=ingestion.DEFAULT_EMBEDDING_MODEL,
            runtime_config=None,
            no_runtime_config=False,
        )
        base.update(overrides)
        return Namespace(**base)

    def resolve(self, args: Namespace, runtime: object) -> object:
        with mock.patch.object(
            consumer.artifact_runtime, "load_runtime", return_value=runtime
        ):
            return consumer.resolve_sink(args)

    def test_server_backend_resolves_to_the_active_generation(self) -> None:
        sink = self.resolve(self.args(), self.runtime(backend="server"))
        self.assertIsNone(sink.refusal)
        self.assertEqual(sink.mode, "server")
        self.assertEqual(sink.url, "http://127.0.0.1:6333")
        self.assertEqual(sink.collection, "personal_artifact_chunks_p20260721v1")
        self.assertIsNone(sink.local_path)
        self.assertEqual(sink.embedding_model_digest, self.digest)
        self.assertEqual(sink.credential, "runtime-admin-key")

    def test_sink_identity_matches_active_generation(self) -> None:
        """Pins the exact target string ingestion writes under.

        The identity is built by the ingestion module's own target builder;
        this asserts the composed result so a change there cannot silently
        re-point the consumer at a different logical target (which would
        re-ingest the entire corpus under a new identity).
        """
        sink = self.resolve(self.args(), self.runtime(backend="server"))
        self.assertEqual(
            sink.identity,
            "url:http://127.0.0.1:6333"
            "|personal_artifact_chunks_p20260721v1"
            f"|{ingestion.DEFAULT_EMBEDDING_MODEL}"
            "|generation:p20260721v1"
            f"|model-digest:{self.digest}"
            f"|chunk-profile:{ingestion.DEFAULT_CHUNK_PROFILE_ID}"
            f"|normalization:{ingestion.DEFAULT_NORMALIZATION_VERSION}",
        )

    def test_local_sink_is_refused_when_runtime_says_server(self) -> None:
        sink = self.resolve(
            self.args(qdrant_path=self.embedded),
            self.runtime(backend="server"),
        )
        self.assertIsNotNone(sink.refusal)
        self.assertIn("active_backend=server", sink.refusal)

    def test_mismatched_collection_is_refused(self) -> None:
        sink = self.resolve(
            self.args(collection="personal_artifact_chunks_v1"),
            self.runtime(backend="server"),
        )
        self.assertIsNotNone(sink.refusal)
        self.assertIn("does not match", sink.refusal)

    def test_read_only_embedded_store_refuses_writes(self) -> None:
        sink = self.resolve(
            self.args(), self.runtime(backend="embedded", rollback="read-only")
        )
        self.assertIsNotNone(sink.refusal)
        self.assertIn("read-only", sink.refusal)

    def test_writable_embedded_backend_is_allowed(self) -> None:
        sink = self.resolve(
            self.args(), self.runtime(backend="embedded", rollback="read-write")
        )
        self.assertIsNone(sink.refusal)
        self.assertEqual(sink.mode, "local")
        self.assertEqual(sink.local_path, self.embedded)

    def test_mismatched_embedding_model_is_refused(self) -> None:
        sink = self.resolve(
            self.args(embedding_model="some/other-model"),
            self.runtime(backend="server"),
        )
        self.assertIsNotNone(sink.refusal)
        self.assertIn("does not match", sink.refusal)

    def test_missing_build_manifest_refuses_rather_than_guessing(self) -> None:
        self.manifest.unlink()
        sink = self.resolve(self.args(), self.runtime(backend="server"))
        self.assertIsNotNone(sink.refusal)
        self.assertIn("build manifest", sink.refusal)

    def test_unreadable_runtime_refuses_instead_of_falling_back(self) -> None:
        with mock.patch.object(
            consumer.artifact_runtime,
            "load_runtime",
            side_effect=consumer.artifact_runtime.RuntimeConfigError("nope"),
        ):
            with self.assertRaises(consumer.ConsumerError) as caught:
                consumer.resolve_sink(self.args())
        self.assertIn("runtime configuration is required", str(caught.exception))

    def test_no_runtime_config_requires_an_explicit_path(self) -> None:
        with self.assertRaises(consumer.ConsumerError):
            consumer.resolve_sink(self.args(no_runtime_config=True))

    def test_describe_never_exposes_credential_bytes(self) -> None:
        sink = self.resolve(self.args(), self.runtime(backend="server"))
        self.assertNotIn("k" * 40, json.dumps(sink.describe()))
        self.assertEqual(sink.describe()["credential"], "runtime-admin-key")


class SupervisorEventTests(unittest.TestCase):
    """Supervisor transitions leave an append-only trace."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True)
        self.log = self.root / "derived" / "supervisor-events.jsonl"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def args(self, **overrides: object) -> Namespace:
        base = dict(
            workspace=self.workspace,
            label="com.personal.artifact-event-consumer",
            transition="redefined",
            reason="retargeted to the active server generation",
            actor="test",
            definition=None,
            supervisor_log=self.log,
            apply=True,
        )
        base.update(overrides)
        return Namespace(**base)

    def test_plan_writes_nothing(self) -> None:
        result, failed = consumer.supervisor_event(self.args(apply=False))
        self.assertFalse(failed)
        self.assertEqual(result["mode"], "plan")
        self.assertFalse(self.log.exists())

    def test_records_are_appended_never_replaced(self) -> None:
        consumer.supervisor_event(self.args(transition="unloaded"))
        consumer.supervisor_event(self.args(transition="loaded"))
        lines = self.log.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["transition"], "unloaded")
        self.assertEqual(json.loads(lines[1])["transition"], "loaded")

    @platform_skips.requires_posix_modes
    def test_log_is_private(self) -> None:
        consumer.supervisor_event(self.args())
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)

    def test_definition_is_bound_by_digest(self) -> None:
        plist = self.root / "job.plist"
        plist.write_bytes(b"<plist/>")
        consumer.supervisor_event(self.args(definition=plist))
        record = json.loads(self.log.read_text(encoding="utf-8").strip())
        self.assertEqual(
            record["definition_sha256"],
            hashlib.sha256(b"<plist/>").hexdigest(),
        )


class CatalogStalenessRefreshTests(_ConsumerTestSupport):
    """Staleness-driven refresh + catalog-diff publication (F-01 closure)."""

    def _backdate_catalog(self) -> None:
        with closing(sqlite3.connect(self.derived / "artifact-catalog.sqlite3")) as conn, conn:
            conn.execute(
                "UPDATE scan_runs SET finished_at='2026-01-01T00:00:00+00:00'"
            )

    def test_fresh_catalog_with_no_receipts_skips_refresh(self) -> None:
        consumer.consume(
            self.args(apply=True),
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        plan, failed = consumer.consume(
            self.args(apply=False, refresh=True),
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        self.assertFalse(failed)
        self.assertFalse(plan["would_refresh_catalog"])
        self.assertEqual(plan["qdrant_write"], "none")

    def test_stale_catalog_triggers_refresh_and_publication(self) -> None:
        consumer.consume(
            self.args(apply=True),
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        self._backdate_catalog()
        plan, plan_failed = consumer.consume(
            self.args(apply=False, refresh=True),
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        self.assertFalse(plan_failed)
        self.assertTrue(plan["would_refresh_catalog"])
        self.assertEqual(plan["would_consume"], 0)
        self.assertEqual(plan["qdrant_write"], "planned")
        before = len(self.qdrant_calls)
        result, apply_failed = consumer.consume(
            self.args(apply=True, refresh=True),
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        self.assertFalse(apply_failed)
        self.assertIsNotNone(result["catalog_publication"])
        publication_calls = self.qdrant_calls[before:]
        self.assertTrue(
            any(
                "catalog-run-" in str(call["outbox"])
                for call in publication_calls
            ),
            "stale refresh must publish the catalog-run outbox",
        )

    def test_pending_refresh_publishes_even_when_not_stale(self) -> None:
        # H1 regression: setUp leaves a FRESH (non-stale) catalog plus a pending
        # receipt. The pending receipt drives a refresh that folds ordinary
        # (receipt-less) edits into the catalog; the full-catalog publication
        # MUST fire even though the catalog was not stale before the refresh —
        # otherwise that content strands in the catalog but never in the store.
        result, failed = consumer.consume(
            self.args(apply=True, refresh=True),
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        self.assertFalse(failed)
        self.assertIsNotNone(result["catalog_publication"])
        self.assertTrue(
            any("catalog-run-" in str(call["outbox"]) for call in self.qdrant_calls),
            "a pending-receipt refresh that advances the catalog must publish it",
        )

    def test_publication_failure_is_durable_and_skips_reconcile(self) -> None:
        # H2: a failed publication must (a) fail the run, (b) leave a DURABLE red
        # health signal so the next run is not falsely green, and (c) skip
        # reconcile so lifecycle is not marked against unpublished content.
        self._backdate_catalog()

        def failing_ingest(**kwargs: object) -> dict[str, object]:
            if "catalog-run-" in str(kwargs["outbox"]):
                raise ingestion.IngestionError("simulated publish failure")
            return self.fake_ingest(**kwargs)

        result, failed = consumer.consume(
            self.args(apply=True, refresh=True),
            qdrant_ingest=failing_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        self.assertTrue(failed)
        self.assertEqual(result["catalog_publication"]["status"], "failed")
        self.assertEqual(
            result["qdrant_reconcile"]["status"], "skipped-unpublished-catalog"
        )
        health = json.loads((self.derived / "consumer-health.json").read_text())
        self.assertFalse(health["healthy"])
        self.assertIn("publication_failed", health["issue_codes"])

    def test_failed_publication_is_retried_off_live_catalog(self) -> None:
        # H2: a durable failure forces a publication retry on a later run even
        # without a fresh refresh; a success clears it and health recovers.
        self._backdate_catalog()
        attempts = {"catalog": 0}

        def flaky_ingest(**kwargs: object) -> dict[str, object]:
            if "catalog-run-" in str(kwargs["outbox"]):
                attempts["catalog"] += 1
                if attempts["catalog"] == 1:
                    raise ingestion.IngestionError("transient publish failure")
            return self.fake_ingest(**kwargs)

        _r1, first_failed = consumer.consume(
            self.args(apply=True, refresh=True),
            qdrant_ingest=flaky_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        self.assertTrue(first_failed)
        # Second run: no refresh, receipt already completed — the ONLY reason to
        # publish is the durable-failure retry.
        result, failed = consumer.consume(
            self.args(apply=True, refresh=False),
            qdrant_ingest=flaky_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        self.assertGreaterEqual(attempts["catalog"], 2)
        self.assertIsNotNone(result["catalog_publication"])
        self.assertNotEqual(result["catalog_publication"].get("status"), "failed")
        self.assertFalse(failed)
        health = json.loads((self.derived / "consumer-health.json").read_text())
        self.assertTrue(health["healthy"])

    def test_source_changed_failure_forces_a_catalog_refresh(self) -> None:
        # A SourceChangedError says the CATALOG is out of date. H2's plain retry
        # rebuilds off the same unrefreshed catalog, so the hash still disagrees
        # and the retry can never clear -- observed looping 18 times on a live
        # vault with everything unpublished (2026-07-22). The failure must
        # therefore ALSO trigger the rescan that can reconcile it, even when the
        # catalog is not yet age-stale.
        state = consumer.ConsumerState(self.state, self.workspace)
        state.record_publication_failure(
            catalog_run_id=None,
            error=(
                "SourceChangedError: source hash differs from catalog revision: "
                "Source Code/arXMCP/.claude/docs/orchestrator-rules.md"
            ),
        )
        self.assertTrue(state.publication_failure_requires_refresh())
        state.connection.close()

        # Catalog deliberately left FRESH: age-staleness must not be what saves us.
        refreshes = {"count": 0}
        real_run_catalog = consumer.catalog.run_catalog

        def counting_run_catalog(*args: object, **kwargs: object) -> dict[str, object]:
            refreshes["count"] += 1
            return real_run_catalog(*args, **kwargs)

        with mock.patch.object(consumer.catalog, "run_catalog", counting_run_catalog):
            consumer.consume(
                self.args(apply=True, refresh=True),
                qdrant_ingest=self.fake_ingest,
                qdrant_reconcile=self.fake_reconcile,
            )
        self.assertEqual(
            refreshes["count"], 1, "a source-changed failure must force a rescan"
        )

    def test_non_source_changed_failure_does_not_force_a_refresh(self) -> None:
        # The escape hatch is scoped: an ordinary transient publish failure keeps
        # the cheap retry path (a full rescan costs ~10 min over /mnt/c, so it
        # must not fire for failures a plain retry can clear).
        state = consumer.ConsumerState(self.state, self.workspace)
        state.record_publication_failure(
            catalog_run_id=None, error="IngestionError: transient publish failure"
        )
        self.assertFalse(state.publication_failure_requires_refresh())
        state.connection.close()

    def test_publication_exception_does_not_escape_or_skip_receipts(self) -> None:
        # H3: a raw Qdrant ApiException (NOT an IngestionError/OSError/sqlite3)
        # must be caught at the publication stage boundary — it must NOT escape
        # consume() and skip the receipt loop that runs after publication.
        self._backdate_catalog()

        class FakeApiException(Exception):
            """Stand-in for qdrant_client UnexpectedResponse / ResponseHandling."""

        def raising_ingest(**kwargs: object) -> dict[str, object]:
            if "catalog-run-" in str(kwargs["outbox"]):
                raise FakeApiException("simulated raw Qdrant ApiException")
            return self.fake_ingest(**kwargs)

        result, failed = consumer.consume(
            self.args(apply=True, refresh=True),
            qdrant_ingest=raising_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        self.assertTrue(failed)
        self.assertEqual(result["catalog_publication"]["status"], "failed")
        # The pending receipt was STILL processed despite the publication error.
        self.assertEqual(result["processed"], 1)
        self.assertTrue(
            any(item["status"] == "completed" for item in result["results"]),
            "receipt processing must not be skipped by a publication exception",
        )

    def _failing_catalog_ingest(self, **kwargs: object) -> dict[str, object]:
        """Fail only the catalog-run publication; receipts still publish."""
        if "catalog-run-" in str(kwargs["outbox"]):
            raise ingestion.IngestionError("simulated publish failure")
        return self.fake_ingest(**kwargs)

    def test_crash_mid_publication_leaves_a_durable_failure(self) -> None:
        # M1: the durable row is written BEFORE the attempt, so a hard crash
        # (BaseException — deliberately NOT caught) still leaves the fail-safe
        # marker. Recording only in the `except` left a window where the refresh
        # had already reset the catalog age but nothing recorded the failure —
        # next run GREEN plus an UNGUARDED reconcile against an unpublished
        # catalog, which is the original H2 harm.
        self._backdate_catalog()

        def crashing_ingest(**kwargs: object) -> dict[str, object]:
            if "catalog-run-" in str(kwargs["outbox"]):
                raise KeyboardInterrupt("simulated hard interrupt")
            return self.fake_ingest(**kwargs)

        with self.assertRaises(KeyboardInterrupt):
            consumer.consume(
                self.args(apply=True, refresh=True),
                qdrant_ingest=crashing_ingest,
                qdrant_reconcile=self.fake_reconcile,
            )
        with closing(sqlite3.connect(self.state)) as connection, connection:
            surviving = connection.execute(
                "SELECT COUNT(*) FROM publication_failures"
            ).fetchone()[0]
        self.assertEqual(surviving, 1, "intent row must survive a hard crash")

    def test_plan_reports_the_armed_publication_retry(self) -> None:
        # M2: after a failed publication the next apply run WILL write via the
        # retry path — a dry run must say so instead of reporting "none".
        self._backdate_catalog()
        consumer.consume(
            self.args(apply=True, refresh=True),
            qdrant_ingest=self._failing_catalog_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        # The catalog is fresh again and no receipt is pending, so the durable
        # retry is the ONLY reason the next apply run would write.
        plan, _failed = consumer.consume(
            self.args(apply=False, refresh=True),
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        self.assertTrue(plan["would_retry_publication"])
        self.assertEqual(plan["qdrant_write"], "planned")

    def test_status_reports_the_durable_publication_failure(self) -> None:
        # L2: status() is the operator/watchdog surface for the durable-RED
        # promise — assert it, not just the consume-written health file.
        self._backdate_catalog()
        consumer.consume(
            self.args(apply=True, refresh=True),
            qdrant_ingest=self._failing_catalog_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        payload, failed = consumer.status(self.args(apply=False))
        self.assertTrue(failed)
        self.assertTrue(payload["publication_failed"])
        self.assertFalse(payload["healthy"])

    def test_status_tolerates_a_state_db_without_the_new_table(self) -> None:
        # L2: the FIRST status() call against a live pre-fix state DB runs
        # before any consume has created the publication_failures table.
        consumer.consume(
            self.args(apply=True),
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        connection = sqlite3.connect(self.state)
        try:
            connection.execute("DROP TABLE publication_failures")
            connection.commit()
        finally:
            connection.close()
        payload, _failed = consumer.status(self.args(apply=False))
        self.assertFalse(payload["publication_failed"])


class QuarantineAckHardeningTests(_ConsumerTestSupport):
    """S2.3 — --name containment, review-date contract, and apply binding.

    quarantine_ack previously had NO test coverage at all.
    """

    def setUp(self) -> None:
        super().setUp()
        self.quarantine_root = self.derived / "outbox-dead-letter"
        self.entry_dir = self.quarantine_root / "catalog-run-6-chunks-v2-incomplete"
        self.entry_dir.mkdir(parents=True)
        (self.entry_dir / "manifest.json").write_text("{}", encoding="utf-8")
        self.ack_ledger = self.derived / watermark.QUARANTINE_ACK_FILENAME

    def ack_args(self, **overrides: object) -> Namespace:
        base: dict[str, object] = {
            "workspace": self.workspace,
            "dead_letter_root": self.quarantine_root,
            "ack_ledger": self.ack_ledger,
            "name": self.entry_dir.name,
            "reason": "retained residue per the residue ledger",
            "review_by": "2099-01-01T00:00:00+00:00",
            "expected_fingerprint": None,
            "replace": False,
            "apply": False,
        }
        base.update(overrides)
        return Namespace(**base)

    def test_name_must_be_a_direct_child_not_a_path(self) -> None:
        # `root / "/etc"` yields "/etc" and `root / ".."` escapes upward, so a
        # bare join was never a boundary.
        for hostile in (
            "/etc", "..", ".", "../outbox", "nested/child", "", "x\x00y",
        ):
            with self.subTest(name=hostile):
                with self.assertRaises(consumer.ConsumerError):
                    consumer.quarantine_ack(self.ack_args(name=hostile))

    def test_review_by_is_required_and_must_be_in_the_future(self) -> None:
        with self.assertRaises(consumer.ConsumerError):
            consumer.quarantine_ack(self.ack_args(review_by=None))
        with self.assertRaises(consumer.ConsumerError):
            consumer.quarantine_ack(
                self.ack_args(review_by="2020-01-01T00:00:00+00:00")
            )
        with self.assertRaises(consumer.ConsumerError):
            consumer.quarantine_ack(self.ack_args(review_by="not-a-time"))

    def test_expected_fingerprint_binds_apply_to_the_reviewed_state(self) -> None:
        plan, _failed = consumer.quarantine_ack(self.ack_args())
        reviewed = plan["would_acknowledge"]["fingerprint"]
        # Matching fingerprint is accepted.
        consumer.quarantine_ack(self.ack_args(expected_fingerprint=reviewed))
        # A directory that changed since review is refused.
        (self.entry_dir / "manifest.json").write_text("[]", encoding="utf-8")
        with self.assertRaises(consumer.ConsumerError):
            consumer.quarantine_ack(self.ack_args(expected_fingerprint=reviewed))

    def test_status_fails_closed_when_quarantine_is_unreadable(self) -> None:
        # H5: the component early-returns state=unknown with open/count at 0,
        # so status() keying on `open` alone read a can't-tell as an all-clear.
        consumer.consume(
            self.args(apply=True),
            qdrant_ingest=self.fake_ingest,
            qdrant_reconcile=self.fake_reconcile,
        )
        os.chmod(self.quarantine_root, 0o000)
        try:
            payload, failed = consumer.status(self.args(apply=False))
        finally:
            os.chmod(self.quarantine_root, 0o700)
        self.assertEqual(
            payload["outbox_quarantine"]["state"], watermark.STATE_UNKNOWN
        )
        self.assertEqual(payload["outbox_quarantine"]["open"], 0)
        self.assertTrue(failed, "an unreadable quarantine must not report healthy")
        self.assertFalse(payload["healthy"])

    def test_apply_requires_the_reviewed_fingerprint(self) -> None:
        # M3: an opt-in binding leaves the UNBOUND path as the default, so the
        # review-time/apply-time TOCTOU this milestone claims to close stays
        # open. Plan must also surface the exact apply invocation.
        with self.assertRaises(consumer.ConsumerError):
            consumer.quarantine_ack(self.ack_args(apply=True))
        plan, _plan_failed = consumer.quarantine_ack(self.ack_args())
        self.assertIn("--expected-fingerprint", plan["suggested_apply"])
        self.assertFalse(plan["requires_replace"])

    def test_corrupt_ledger_refuses_rather_than_clobbering_other_acks(self) -> None:
        # V-M1: the loader fails OPEN to {} on a corrupt ledger — correct for
        # the read path, DESTRUCTIVE here, because apply rewrites the whole
        # file and would delete every other acknowledgment (and bypass the
        # --replace gate) without a word.
        plan, _p = consumer.quarantine_ack(self.ack_args())
        fingerprint = plan["would_acknowledge"]["fingerprint"]
        self.ack_ledger.write_text("{ this is not json", encoding="utf-8")
        with self.assertRaises(consumer.ConsumerError):
            consumer.quarantine_ack(
                self.ack_args(apply=True, expected_fingerprint=fingerprint)
            )
        # The corrupt file is left untouched for inspection/restore.
        self.assertEqual(self.ack_ledger.read_text(encoding="utf-8"), "{ this is not json")
        # A legitimately EMPTY ledger is still writable.
        self.ack_ledger.write_text(
            json.dumps({"schema_version": 1, "entries": []}), encoding="utf-8"
        )
        applied, _f = consumer.quarantine_ack(
            self.ack_args(apply=True, expected_fingerprint=fingerprint)
        )
        self.assertIn("actor_uid", applied["acknowledged"])

    def test_apply_records_actor_and_refuses_a_silent_clobber(self) -> None:
        plan, _p = consumer.quarantine_ack(self.ack_args())
        first = plan["would_acknowledge"]["fingerprint"]
        applied, _failed = consumer.quarantine_ack(
            self.ack_args(apply=True, expected_fingerprint=first)
        )
        self.assertIn("actor_uid", applied["acknowledged"])
        self.assertEqual(applied["quarantine"]["open"], 0)
        # Content changes -> the existing approval covers DIFFERENT bytes.
        (self.entry_dir / "manifest.json").write_text("[]", encoding="utf-8")
        replan, _rp = consumer.quarantine_ack(self.ack_args())
        second = replan["would_acknowledge"]["fingerprint"]
        self.assertTrue(replan["requires_replace"])
        self.assertIn("--replace", replan["suggested_apply"])
        with self.assertRaises(consumer.ConsumerError):
            consumer.quarantine_ack(
                self.ack_args(apply=True, expected_fingerprint=second)
            )
        # --replace supersedes it, and the lineage is retained.
        superseded, _f = consumer.quarantine_ack(
            self.ack_args(apply=True, replace=True, expected_fingerprint=second)
        )
        self.assertIn("supersedes", superseded["acknowledged"])


if __name__ == "__main__":
    unittest.main()
