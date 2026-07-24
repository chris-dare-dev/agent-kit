from __future__ import annotations

import json
import plistlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import artifact_watchdog as watchdog  # noqa: E402
import artifact_watermark as watermark  # noqa: E402


NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
WATCHDOG_PLIST = SCRIPT_DIR / "com.personal.artifact-watchdog.plist"


def _ago(seconds: float) -> datetime:
    return NOW - timedelta(seconds=seconds)


class _Notifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, title: str, message: str) -> bool:
        self.calls.append((title, message))
        return True


class WatchdogVerdictTests(unittest.TestCase):
    def _watermark(self, **consumer: object) -> dict[str, object]:
        base = {
            "loaded": True,
            "age_seconds": 120.0,
            "last_success_at": NOW.isoformat(),
        }
        base.update(consumer)
        return {
            "issue_codes": [],
            "healthy": True,
            "components": {
                "consumer": base,
                "receipts": {"unobserved": 0, "oldest_unobserved_age_seconds": None},
                "writer": {"aligned": True},
            },
        }

    def test_loaded_and_recent_is_healthy(self) -> None:
        verdict = watchdog.evaluate(self._watermark(), consumer_max_age_seconds=2700)
        self.assertEqual(verdict["status"], "healthy")
        self.assertEqual(verdict["failures"], [])

    def test_unloaded_consumer_is_unhealthy(self) -> None:
        verdict = watchdog.evaluate(
            self._watermark(loaded=False), consumer_max_age_seconds=2700
        )
        self.assertEqual(verdict["status"], "unhealthy")
        self.assertIn("not loaded", verdict["failures"][0])

    def test_consumer_past_cadence_is_unhealthy(self) -> None:
        verdict = watchdog.evaluate(
            self._watermark(age_seconds=13 * 3600.0), consumer_max_age_seconds=2700
        )
        self.assertEqual(verdict["status"], "unhealthy")
        self.assertTrue(any("past its" in item for item in verdict["failures"]))

    def test_consumer_that_never_ran_is_unhealthy(self) -> None:
        verdict = watchdog.evaluate(
            self._watermark(age_seconds=None), consumer_max_age_seconds=2700
        )
        self.assertEqual(verdict["status"], "unhealthy")

    def test_misaligned_writer_is_unhealthy(self) -> None:
        payload = self._watermark()
        payload["components"]["writer"] = {"aligned": False, "detail": "local vs server"}
        verdict = watchdog.evaluate(payload, consumer_max_age_seconds=2700)
        self.assertEqual(verdict["status"], "unhealthy")

    def test_watermark_crash_still_writes_a_loud_verdict(self) -> None:
        # H1: an unreadable directory anywhere in the watermark walk used to
        # raise straight through run(), which then died BEFORE writing its log
        # line, health file, or notification — leaving the previous (possibly
        # "healthy") health file as the last word, with nothing else watching.
        def boom(**_kwargs: object) -> dict[str, object]:
            raise PermissionError(13, "Permission denied")

        with tempfile.TemporaryDirectory() as tmp:
            derived = Path(tmp)
            with mock.patch.object(watchdog.watermark, "compute_watermark", boom):
                record = watchdog.run(
                    derived_root=derived,
                    log_path=derived / "watchdog.jsonl",
                    consumer_max_age_seconds=2700,
                    notify=False,
                )
            self.assertEqual(record["status"], "unhealthy")
            self.assertTrue(record["log_written"])
            self.assertTrue((derived / watchdog.HEALTH_FILENAME).exists())
            self.assertTrue(
                any("crashed" in item for item in record["failures"]),
                record["failures"],
            )

    def test_unreadable_quarantine_raises_the_alarm(self) -> None:
        # H5: an UNKNOWN quarantine component means the surface could not be
        # READ — a can't-verify condition. It previously reached no alarm at
        # all: quarantine codes are excluded from PIPELINE_DOWN_CODES, and
        # pipeline_issue_codes never affect status (which keys on `failures`).
        payload = self._watermark()
        payload["components"]["outbox_quarantine"] = {
            "state": watermark.STATE_UNKNOWN,
            "detail": "quarantine root is unreadable: [Errno 13]",
        }
        verdict = watchdog.evaluate(payload, consumer_max_age_seconds=2700)
        self.assertEqual(verdict["status"], "unhealthy")
        self.assertTrue(
            any("quarantine" in item for item in verdict["failures"]),
            verdict["failures"],
        )

    def test_retained_residue_stays_quiet(self) -> None:
        # Deliberate scope: DEGRADED (open residue awaiting disposition) is
        # "downstream behind", not "the write path is down". The watchdog must
        # not go forever-red over residue the operator already knows about.
        payload = self._watermark()
        payload["components"]["outbox_quarantine"] = {
            "state": watermark.STATE_DEGRADED,
            "detail": "1 quarantined outbox director(ies) await disposition",
        }
        verdict = watchdog.evaluate(payload, consumer_max_age_seconds=2700)
        self.assertEqual(verdict["status"], "healthy")

    def test_backlogged_receipts_are_unhealthy(self) -> None:
        payload = self._watermark()
        payload["components"]["receipts"] = {
            "unobserved": 3,
            "oldest_unobserved_age_seconds": 5000.0,
        }
        verdict = watchdog.evaluate(payload, consumer_max_age_seconds=2700)
        self.assertEqual(verdict["status"], "unhealthy")


class WatchdogRunTests(unittest.TestCase):
    """The watchdog must alert about a consumer that cannot alert about itself."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.derived = Path(self.temp.name) / "derived"
        self.derived.mkdir(parents=True)
        self.log = Path(self.temp.name) / "watchdog.jsonl"
        # The exact 2026-07-18 state: a stale-but-green consumer health file.
        (self.derived / "artifact-event-consumer-health.json").write_text(
            json.dumps({"healthy": True, "observed_at": _ago(13 * 3600).isoformat()}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run(self, notifier: _Notifier, **kwargs: object) -> dict[str, object]:
        return watchdog.run(
            derived_root=self.derived,
            log_path=self.log,
            notifier=notifier,
            loaded_probe=lambda label: False,
            now=NOW,
            **kwargs,
        )

    def test_absent_consumer_is_detected_logged_and_notified(self) -> None:
        notifier = _Notifier()
        record = self._run(notifier)
        self.assertEqual(record["status"], "unhealthy")
        self.assertTrue(record["log_written"])
        self.assertTrue(record["notified"])
        self.assertEqual(len(notifier.calls), 1)
        self.assertIn("Pipeline down", notifier.calls[0][1])

    def test_log_is_append_only_and_records_every_run(self) -> None:
        notifier = _Notifier()
        self._run(notifier)
        self._run(notifier)
        lines = watchdog._log_lines(self.log)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(item["status"] == "unhealthy" for item in lines))

    def test_log_records_whether_the_notification_actually_fired(self) -> None:
        """A notification that silently failed must be provable after the fact."""

        def _failing(title: str, message: str) -> bool:
            return False

        watchdog.run(
            derived_root=self.derived,
            log_path=self.log,
            notifier=_failing,
            loaded_probe=lambda label: False,
            now=NOW,
        )
        line = watchdog._log_lines(self.log)[-1]
        self.assertTrue(line["notify_attempted"])
        self.assertFalse(line["notified"])

    def test_healthy_run_records_no_notification_attempt(self) -> None:
        notifier = _Notifier()
        record = watchdog.run(
            derived_root=self.derived,
            log_path=self.log,
            notifier=notifier,
            loaded_probe=lambda label: True,
            consumer_max_age_seconds=14 * 3600,
            now=NOW,
        )
        self.assertEqual(record["status"], "healthy")
        self.assertFalse(record["notify_attempted"])
        self.assertEqual(notifier.calls, [])
        self.assertIn("notified", watchdog._log_lines(self.log)[-1])

    def test_notification_fires_on_transition_not_every_run(self) -> None:
        notifier = _Notifier()
        self._run(notifier)
        self._run(notifier)
        self.assertEqual(len(notifier.calls), 1)

    def test_force_notify_repeats_the_alert(self) -> None:
        notifier = _Notifier()
        self._run(notifier)
        self._run(notifier, force_notify=True)
        self.assertEqual(len(notifier.calls), 2)

    def test_health_file_is_written_for_the_composite_surface(self) -> None:
        self._run(_Notifier())
        payload = json.loads(
            (self.derived / watchdog.HEALTH_FILENAME).read_text(encoding="utf-8")
        )
        self.assertEqual(payload["status"], "unhealthy")
        self.assertIn("watermark", payload)
        self.assertIn("consumer", payload["watermark"]["components"])

    def test_watchdog_does_not_depend_on_the_consumer_being_alive(self) -> None:
        """No consumer process, no consumer state DB — the watchdog still reports."""
        (self.derived / "artifact-event-consumer-health.json").unlink()
        record = self._run(_Notifier())
        self.assertEqual(record["status"], "unhealthy")
        self.assertTrue(record["log_written"])


class WatchdogLaunchdTests(unittest.TestCase):
    def test_sibling_agent_is_independent_and_frequent(self) -> None:
        with WATCHDOG_PLIST.open("rb") as handle:
            value = plistlib.load(handle)
        self.assertEqual(value["Label"], watermark.WATCHDOG_LABEL)
        self.assertEqual(value["Umask"], 0o77)
        self.assertLessEqual(value["StartInterval"], 600)
        self.assertTrue(value["RunAtLoad"])
        self.assertNotEqual(value["StandardOutPath"], value["StandardErrorPath"])
        arguments = value["ProgramArguments"]
        self.assertTrue(arguments[-1].endswith("artifact_watchdog.py"))
        # Must run on the system interpreter: the watchdog cannot depend on the
        # same virtualenv whose absence it may need to report.
        self.assertEqual(arguments[0], "/usr/bin/python3")


if __name__ == "__main__":
    unittest.main()
