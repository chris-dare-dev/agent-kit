from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import artifact_supervisor_receipt as supervisor  # noqa: E402


def _args(**overrides):
    import argparse

    base = {
        "label": "com.personal.artifact-event-consumer",
        "action": "unload",
        "reason": "quarantined pending F-02 write-target review",
        "observed_at": "2026-07-18T00:57:00+00:00",
        "run_id": None,
        "plist": None,
        "no_probe": True,
        "receipt_root": Path("/nonexistent"),
        "apply": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class ValidationTests(unittest.TestCase):
    def test_label_outside_artifact_memory_scope_is_rejected(self) -> None:
        with self.assertRaisesRegex(supervisor.SupervisorReceiptError, "label must match"):
            supervisor.validate_label("com.apple.Finder")

    def test_valid_label_accepted(self) -> None:
        self.assertEqual(
            supervisor.validate_label("com.personal.artifact-memory-service"),
            "com.personal.artifact-memory-service",
        )

    def test_unsupported_action_is_rejected(self) -> None:
        with self.assertRaisesRegex(supervisor.SupervisorReceiptError, "unsupported action"):
            supervisor.validate_action("restart")

    def test_empty_or_trivial_reason_is_rejected(self) -> None:
        """The intent field is the entire point of F-18."""
        for bad in ("", "   ", "n/a", "fix"):
            with self.assertRaisesRegex(supervisor.SupervisorReceiptError, "intent"):
                supervisor.validate_reason(bad)

    def test_naive_timestamp_is_rejected(self) -> None:
        with self.assertRaisesRegex(supervisor.SupervisorReceiptError, "explicit offset"):
            supervisor.validate_observed_at("2026-07-18T00:57:00")

    def test_timestamp_normalized_to_utc(self) -> None:
        self.assertEqual(
            supervisor.validate_observed_at("2026-07-17T20:57:00-04:00"),
            "2026-07-18T00:57:00+00:00",
        )


class IdentityTests(unittest.TestCase):
    def test_same_instant_is_idempotent(self) -> None:
        first, _ = supervisor.build_receipt(
            label="com.personal.artifact-event-consumer",
            action="unload",
            reason="protective unload pending review",
            observed_at="2026-07-18T00:57:00+00:00",
            run_id=None,
            plist=None,
            observed_state={"probed": False},
        )
        second, _ = supervisor.build_receipt(
            label="com.personal.artifact-event-consumer",
            action="unload",
            reason="protective unload pending review",
            observed_at="2026-07-18T00:57:00+00:00",
            run_id=None,
            plist=None,
            observed_state={"probed": False},
        )
        self.assertEqual(first, second)

    def test_different_instant_is_a_distinct_event(self) -> None:
        """Two loads of one agent are two facts, not one — unlike a skill capture."""
        first, _ = supervisor.build_receipt(
            label="com.personal.artifact-event-consumer",
            action="load",
            reason="reload after server-sink fix",
            observed_at="2026-07-18T00:57:00+00:00",
            run_id=None,
            plist=None,
            observed_state={"probed": False},
        )
        second, _ = supervisor.build_receipt(
            label="com.personal.artifact-event-consumer",
            action="load",
            reason="reload after server-sink fix",
            observed_at="2026-07-18T09:00:00+00:00",
            run_id=None,
            plist=None,
            observed_state={"probed": False},
        )
        self.assertNotEqual(first, second)

    def test_reason_is_part_of_identity(self) -> None:
        first, _ = supervisor.build_receipt(
            label="com.personal.artifact-event-consumer",
            action="unload",
            reason="reason one, stated fully",
            observed_at="2026-07-18T00:57:00+00:00",
            run_id=None,
            plist=None,
            observed_state={"probed": False},
        )
        second, _ = supervisor.build_receipt(
            label="com.personal.artifact-event-consumer",
            action="unload",
            reason="reason two, stated fully",
            observed_at="2026-07-18T00:57:00+00:00",
            run_id=None,
            plist=None,
            observed_state={"probed": False},
        )
        self.assertNotEqual(first, second)


class EmitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "supervisor-events"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_plan_mode_writes_nothing(self) -> None:
        result = supervisor.emit(_args(receipt_root=self.root))
        self.assertEqual(result["mode"], "plan")
        self.assertEqual(result["status"], "planned")
        self.assertFalse(self.root.exists())

    def test_apply_creates_then_is_idempotent(self) -> None:
        first = supervisor.emit(_args(receipt_root=self.root, apply=True))
        self.assertEqual(first["status"], "created")
        written = Path(first["receipt_path"])
        self.assertTrue(written.exists())

        second = supervisor.emit(_args(receipt_root=self.root, apply=True))
        self.assertEqual(second["status"], "idempotent")
        self.assertEqual(second["receipt_path"], first["receipt_path"])

    def test_receipt_records_intent_and_safety(self) -> None:
        result = supervisor.emit(_args(receipt_root=self.root, apply=True))
        record = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(record["action"], "unload")
        self.assertEqual(record["observed_at"], "2026-07-18T00:57:00+00:00")
        self.assertIn("F-02", record["reason"])
        self.assertEqual(record["safety"]["supervisor_mutation"], "none")
        self.assertEqual(record["safety"]["receipt_mode"], "append-only")

    def test_receipt_is_append_only_never_overwritten(self) -> None:
        result = supervisor.emit(_args(receipt_root=self.root, apply=True))
        path = Path(result["receipt_path"])
        original = path.read_bytes()
        supervisor.emit(_args(receipt_root=self.root, apply=True))
        self.assertEqual(path.read_bytes(), original)

    def test_plist_binding_records_digest(self) -> None:
        plist = Path(self.temp.name) / "com.personal.artifact-event-consumer.plist"
        plist.write_bytes(b"<plist/>")
        result = supervisor.emit(
            _args(receipt_root=self.root, apply=True, plist=plist)
        )
        record = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(len(record["plist"]["sha256"]), 64)
        self.assertEqual(record["plist"]["byte_size"], 8)

    def test_probe_failure_does_not_block_the_receipt(self) -> None:
        """A receipt that cannot be written because the probe failed is worse
        than a receipt with an unprobed state field."""
        with mock.patch.object(
            supervisor.subprocess, "run", side_effect=OSError("no launchctl")
        ):
            result = supervisor.emit(
                _args(receipt_root=self.root, apply=True, no_probe=False)
            )
        record = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
        self.assertFalse(record["observed_state"]["probed"])
        self.assertIn("no launchctl", record["observed_state"]["error"])


class HistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "supervisor-events"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_absent_root_is_empty_not_an_error(self) -> None:
        import argparse

        result = supervisor.history(
            argparse.Namespace(receipt_root=self.root, label=None)
        )
        self.assertEqual(result["count"], 0)

    def test_history_replays_in_chronological_order(self) -> None:
        import argparse

        supervisor.emit(
            _args(
                receipt_root=self.root,
                apply=True,
                action="unload",
                observed_at="2026-07-18T00:57:00+00:00",
                reason="protective unload pending F-02",
            )
        )
        supervisor.emit(
            _args(
                receipt_root=self.root,
                apply=True,
                action="load",
                observed_at="2026-07-18T09:00:00+00:00",
                reason="reload after server-sink fix",
            )
        )
        result = supervisor.history(
            argparse.Namespace(receipt_root=self.root, label=None)
        )
        self.assertEqual(result["count"], 2)
        self.assertEqual(
            [event["action"] for event in result["events"]], ["unload", "load"]
        )

    def test_history_filters_by_label(self) -> None:
        import argparse

        supervisor.emit(_args(receipt_root=self.root, apply=True))
        supervisor.emit(
            _args(
                receipt_root=self.root,
                apply=True,
                label="com.personal.artifact-memory-service",
                reason="bootstrap after provisioning",
            )
        )
        result = supervisor.history(
            argparse.Namespace(
                receipt_root=self.root, label="com.personal.artifact-memory-service"
            )
        )
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["events"][0]["label"], "com.personal.artifact-memory-service")


if __name__ == "__main__":
    unittest.main()
